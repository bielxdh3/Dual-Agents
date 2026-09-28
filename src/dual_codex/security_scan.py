from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import threading
import time
from typing import Any, Mapping

from .config import AgentConfig
from .process import codex_environment


PLUGIN_ID = "codex-security@openai-curated-remote"
SCAN_STATUSES = frozenset({"running", "complete", "failed", "canceled"})
SCAN_ACTIONS = frozenset({"reused", "resumed", "started", "awaited", "conflict"})
_READ_TOOLS = frozenset(
    {
        "list_codex_security_scans",
        "get_codex_security_scan",
        "get_codex_security_scan_context",
        "get_codex_security_completed_scan",
    }
)
_MAX_MCP_LINE = 2 * 1024 * 1024
_MCP_TIMEOUT_SECONDS = 20.0


class SecurityScanError(RuntimeError):
    def __init__(self, message: str, *, failure_class: str):
        super().__init__(message)
        self.failure_class = failure_class


def stable_target_id(repository: Path) -> str:
    """Return the Codex Security stable local-workspace identity for a path."""

    resolved = str(repository.expanduser().resolve())
    digest = hashlib.sha256(f"local-workspace\0{resolved}".encode("utf-8")).hexdigest()
    return f"target_sha256_{digest}"


def _normal_path(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        return os.path.normcase(str(Path(value).expanduser().resolve(strict=False)))
    except (OSError, RuntimeError, ValueError):
        return ""


def _scan_status(scan: Mapping[str, Any]) -> str:
    progress = scan.get("progress")
    value = progress.get("status") if isinstance(progress, Mapping) else scan.get("status")
    return str(value or "unknown").casefold()


def _scope(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    return value.replace("\\", "/").strip("/") or "."


def _scope_covers(scan_scope: str, requested_scope: str) -> bool:
    scan_root = _scope(scan_scope)
    requested = _scope(requested_scope)
    return scan_root == "." or requested == scan_root or requested.startswith(scan_root + "/")


def _supports_mode(scan_mode: str, requested_mode: str) -> bool:
    return scan_mode == requested_mode or (requested_mode == "standard" and scan_mode == "deep")


@dataclass(frozen=True)
class ScanDecision:
    action: str
    plugin_id: str
    plugin_version: str
    target_path: str
    target_id: str
    target_revision: str
    required_mode: str
    required_scope: str
    selected_scan: Mapping[str, Any] | None
    observed_scans: tuple[Mapping[str, Any], ...]
    reason: str = ""

    @property
    def failure_class(self) -> str:
        return "SECURITY_SCAN_CONFLICT" if self.action == "conflict" else ""

    def public_record(self) -> dict[str, Any]:
        selected = self.selected_scan
        return {
            "plugin_id": self.plugin_id,
            "plugin_version": self.plugin_version,
            "target_identity": {
                "path": self.target_path,
                "target_id": self.target_id,
                "revision": self.target_revision,
                "scope": self.required_scope,
            },
            "decision": self.action,
            "reason": self.reason,
            "selected_scan": _safe_scan_summary(selected) if selected else None,
            "observed_scans": [_safe_scan_summary(scan) for scan in self.observed_scans],
        }

    def executor_instruction(self) -> str:
        selected = _safe_scan_summary(self.selected_scan) if self.selected_scan else None
        policy = {
            "plugin_id": self.plugin_id,
            "plugin_version": self.plugin_version,
            "target_identity": {
                "path": self.target_path,
                "target_id": self.target_id,
                "revision": self.target_revision,
                "scope": self.required_scope,
            },
            "required_mode": self.required_mode,
            "decision": self.action,
            "selected_scan": selected,
            "observed_scans": [_safe_scan_summary(scan) for scan in self.observed_scans],
            "reason": self.reason,
        }
        return (
            "TRUSTED CODEX SECURITY SCAN POLICY (control-plane preflight):\n"
            f"{json.dumps(policy, ensure_ascii=False, separators=(',', ':'))}\n"
            "Follow this decision before using any Codex Security start tool. If the decision is "
            "'awaited', inspect and await the selected scan through its supported provider API; "
            "if the decision is 'reused', use the selected completed scan as the run's authority. "
            "For either selected-scan decision, do not start a competing scan. If the decision is 'start', re-list scans immediately "
            "before starting exactly one scan for this exact target and scope, and proceed only if the exact-target ledger still "
            "matches the host-observed baseline. If any exact-target scan appeared after that baseline, do not start or adopt it; "
            "report a conflict. Never cancel an "
            "existing scan. Return a security_scan_provenance object containing only plugin_id, "
            "plugin_version, target_identity (path, target_id, revision, scope), scan_id, mode, "
            "initial_status, action, and final_status. Map a provider response action of 'created' "
            "to the provenance action 'started'. Do not include continuation tokens or other "
            "credentials in the report. Wait for a final provider status before claiming completion."
        )

    def architect_summary(self) -> str:
        selected = None
        if self.selected_scan:
            selected = {
                "scan_id": str(self.selected_scan.get("scanId", self.selected_scan.get("scan_id", ""))),
                "mode": str(self.selected_scan.get("mode", "")),
                "status": _scan_status(self.selected_scan),
            }
        summary = {
            "required": True,
            "required_mode": self.required_mode,
            "required_scope": self.required_scope,
            "target_identity": {
                "path": self.target_path,
                "target_id": self.target_id,
                "revision": self.target_revision,
            },
            "host_decision": self.action,
            "selected_scan": selected,
            "requirement": "Preserve this mandatory Security gate in the plan.",
            "role_boundary": (
                "Architect must not start, resume, cancel, await operationally, or claim completion "
                "of any Security scan. The host and configured Executor control scan operations."
            ),
        }
        return json.dumps(summary, ensure_ascii=False, separators=(",", ":"))


def _safe_scan_summary(scan: Mapping[str, Any] | None) -> dict[str, str]:
    if not isinstance(scan, Mapping):
        return {}
    return {
        "scan_id": str(scan.get("scanId", scan.get("scan_id", ""))),
        "mode": str(scan.get("mode", "")),
        "status": _scan_status(scan),
        "target_id": str(scan.get("targetId", scan.get("target_id", ""))),
        "target_path": str(scan.get("targetPath", scan.get("target_path", ""))),
        "target_revision": str(scan.get("targetRevision", scan.get("target_revision", ""))),
        "scope": _scope(scan.get("scope", "")),
        "updated_at": str(scan.get("updatedAt", scan.get("updated_at", ""))),
    }


def arbitrate_security_scans(
    scans: list[Mapping[str, Any]],
    *,
    plugin_id: str,
    plugin_version: str,
    target_path: Path,
    target_revision: str,
    required_mode: str = "standard",
    required_scope: str = ".",
    allow_completed_reuse: bool = False,
) -> ScanDecision:
    """Choose one exact-target scan or fail closed before Executor work begins."""

    if required_mode not in {"standard", "deep"}:
        raise ValueError("required_mode must be standard or deep")
    current_path = str(target_path.expanduser().resolve())
    target_id = stable_target_id(Path(current_path))
    same_path = [scan for scan in scans if _normal_path(scan.get("targetPath", scan.get("target_path"))) == _normal_path(current_path)]
    identity_mismatches = [
        scan for scan in same_path
        if str(scan.get("targetId", scan.get("target_id", ""))) != target_id
    ]
    if identity_mismatches:
        observed = tuple(identity_mismatches)
        return ScanDecision(
            "conflict", plugin_id, plugin_version, current_path, target_id, target_revision,
            required_mode, _scope(required_scope), None, observed,
            "The provider has scans for this path with a different stable target identity.",
        )

    exact = [
        scan for scan in same_path
        if str(scan.get("targetId", scan.get("target_id", ""))) == target_id
    ]
    active = [scan for scan in exact if _scan_status(scan) == "running"]
    if len(active) > 1:
        return ScanDecision(
            "conflict", plugin_id, plugin_version, current_path, target_id, target_revision,
            required_mode, _scope(required_scope), None, tuple(active),
            "Multiple active scans target the same repository; none was selected or canceled.",
        )
    if active:
        selected = active[0]
        compatible = (
            str(selected.get("targetRevision", selected.get("target_revision", ""))) == target_revision
            and _scope_covers(str(selected.get("scope", "")), required_scope)
            and _supports_mode(str(selected.get("mode", "")), required_mode)
        )
        if not compatible:
            return ScanDecision(
                "conflict", plugin_id, plugin_version, current_path, target_id, target_revision,
                required_mode, _scope(required_scope), None, tuple(active),
                "The active exact-target scan has an incompatible revision, scope, or mode.",
            )
        return ScanDecision(
            "awaited", plugin_id, plugin_version, current_path, target_id, target_revision,
            required_mode, _scope(required_scope), selected, tuple(exact),
            "An active exact-target scan already satisfies the requested scope.",
        )

    if allow_completed_reuse:
        reusable = [
            scan for scan in exact
            if _scan_status(scan) == "complete"
            and str(scan.get("targetRevision", scan.get("target_revision", ""))) == target_revision
            and _scope_covers(str(scan.get("scope", "")), required_scope)
            and _supports_mode(str(scan.get("mode", "")), required_mode)
            and bool(scan.get("targetSnapshotDigest"))
            and scan.get("targetSnapshotDigest") == scan.get("currentSnapshotDigest")
            and not scan.get("warnings")
        ]
        if reusable:
            reusable.sort(
                key=lambda scan: (
                    str(scan.get("mode", "")) == "deep",
                    str(scan.get("updatedAt", scan.get("updated_at", ""))),
                    str(scan.get("scanId", scan.get("scan_id", ""))),
                ),
                reverse=True,
            )
            return ScanDecision(
                "reused", plugin_id, plugin_version, current_path, target_id, target_revision,
                required_mode, _scope(required_scope), reusable[0], tuple(exact),
                "A completed scan matches the explicit freshness and scope reuse policy.",
            )

    return ScanDecision(
        "start", plugin_id, plugin_version, current_path, target_id, target_revision,
        required_mode, _scope(required_scope), None, tuple(exact),
        "No active or explicitly reusable exact-target scan was found.",
    )


def continue_security_scan_authority(
    scans: list[Mapping[str, Any]],
    *,
    authority: Mapping[str, Any],
    plugin_id: str,
    plugin_version: str,
    target_path: Path,
    target_revision: str,
    required_mode: str,
    required_scope: str,
) -> ScanDecision:
    """Continue one persisted run-local scan authority after re-reading the ledger."""

    current_path = str(target_path.expanduser().resolve())
    target_id = stable_target_id(Path(current_path))
    scope = _scope(required_scope)
    if (
        authority.get("plugin_id") != plugin_id
        or authority.get("plugin_version") != plugin_version
        or authority.get("target_path") != current_path
        or authority.get("target_id") != target_id
        or authority.get("target_revision") != target_revision
        or authority.get("required_mode") != required_mode
        or authority.get("required_scope") != scope
    ):
        raise SecurityScanError(
            "SECURITY_SCAN_AUTHORITY_INVALID: the persisted run authority no longer matches the required plugin, target, revision, mode, or scope.",
            failure_class="SECURITY_SCAN_AUTHORITY_INVALID",
        )

    same_path = [
        scan for scan in scans
        if _normal_path(scan.get("targetPath", scan.get("target_path"))) == _normal_path(current_path)
    ]
    mismatched_target = [
        scan for scan in same_path
        if str(scan.get("targetId", scan.get("target_id", ""))) != target_id
    ]
    if mismatched_target:
        raise SecurityScanError(
            "SECURITY_SCAN_CONFLICT: the scan ledger contains a path match with a different target identity.",
            failure_class="SECURITY_SCAN_CONFLICT",
        )
    exact = [
        scan for scan in same_path
        if str(scan.get("targetId", scan.get("target_id", ""))) == target_id
    ]
    baseline_ids = {
        str(item)
        for item in authority.get(
            "generation_observed_scan_ids",
            authority.get("initial_observed_scan_ids", []),
        )
        if item
    }
    selected_id = str(authority.get("selected_scan_id") or "")
    authority_state = str(authority.get("authority_state") or "")

    def validate_identity(scan: Mapping[str, Any], *, selected: bool) -> None:
        scan_id = str(scan.get("scanId", scan.get("scan_id", "")))
        if (
            not scan_id
            or str(scan.get("targetRevision", scan.get("target_revision", ""))) != target_revision
            or not _scope_covers(str(scan.get("scope", "")), scope)
            or not _supports_mode(str(scan.get("mode", "")), required_mode)
        ):
            classification = "SECURITY_SCAN_AUTHORITY_INVALID" if selected else "SECURITY_SCAN_CONFLICT"
            raise SecurityScanError(
                f"{classification}: scan {scan_id or 'unknown'} no longer matches the exact required revision, scope, or mode.",
                failure_class=classification,
            )

    if selected_id:
        selected_matches = [
            scan for scan in exact
            if str(scan.get("scanId", scan.get("scan_id", ""))) == selected_id
        ]
        if len(selected_matches) != 1:
            raise SecurityScanError(
                f"SECURITY_SCAN_AUTHORITY_LOST: authoritative scan {selected_id} is missing or ambiguous in the provider ledger.",
                failure_class="SECURITY_SCAN_AUTHORITY_LOST",
            )
        selected = selected_matches[0]
        new_competitors = [
            scan for scan in exact
            if str(scan.get("scanId", scan.get("scan_id", ""))) != selected_id
            and str(scan.get("scanId", scan.get("scan_id", ""))) not in baseline_ids
        ]
        active_competitors = [scan for scan in exact if _scan_status(scan) == "running" and scan is not selected]
        if new_competitors or active_competitors:
            raise SecurityScanError(
                "SECURITY_SCAN_CONFLICT: a competing exact-target scan appeared after run authority was selected.",
                failure_class="SECURITY_SCAN_CONFLICT",
            )
        validate_identity(selected, selected=True)
        selected_mode = str(selected.get("mode", ""))
        if not authority.get("selected_scan_mode") or selected_mode != authority.get("selected_scan_mode"):
            raise SecurityScanError(
                f"SECURITY_SCAN_AUTHORITY_INVALID: authoritative scan {selected_id} changed its selected mode.",
                failure_class="SECURITY_SCAN_AUTHORITY_INVALID",
            )
        selected_scope = _scope(selected.get("scope", ""))
        if not authority.get("selected_scan_scope") or selected_scope != authority.get("selected_scan_scope"):
            raise SecurityScanError(
                f"SECURITY_SCAN_AUTHORITY_INVALID: authoritative scan {selected_id} changed its selected scope.",
                failure_class="SECURITY_SCAN_AUTHORITY_INVALID",
            )
        status = _scan_status(selected)
        if status in {"failed", "canceled"}:
            raise SecurityScanError(
                f"SECURITY_SCAN_AUTHORITY_FAILED: authoritative scan {selected_id} ended with status {status}.",
                failure_class="SECURITY_SCAN_AUTHORITY_FAILED",
            )
        if status == "running":
            action = "awaited"
        elif status == "complete":
            completed_validation = authority.get("completed_validation")
            target_digest = selected.get("targetSnapshotDigest")
            current_digest = selected.get("currentSnapshotDigest")
            if not isinstance(target_digest, str) or not target_digest:
                raise SecurityScanError(
                    f"SECURITY_SCAN_AUTHORITY_INVALID: completed authoritative scan {selected_id} has no target snapshot identity.",
                    failure_class="SECURITY_SCAN_AUTHORITY_INVALID",
                )
            if selected.get("warnings"):
                raise SecurityScanError(
                    f"SECURITY_SCAN_AUTHORITY_INVALID: completed authoritative scan {selected_id} contains provider warnings.",
                    failure_class="SECURITY_SCAN_AUTHORITY_INVALID",
                )
            pinned_digest_identity = (
                hashlib.sha256(str(target_digest).encode("utf-8")).hexdigest() if target_digest else ""
            )
            if completed_validation:
                still_same_scan = (
                    isinstance(completed_validation, Mapping)
                    and completed_validation.get("scan_id") == selected_id
                    and completed_validation.get("target_snapshot_identity") == pinned_digest_identity
                    and completed_validation.get("current_snapshot_identity", pinned_digest_identity) == pinned_digest_identity
                )
                if not still_same_scan:
                    raise SecurityScanError(
                        f"SECURITY_SCAN_AUTHORITY_INVALID: completed authoritative scan {selected_id} changed its validated snapshot identity or warnings.",
                        failure_class="SECURITY_SCAN_AUTHORITY_INVALID",
                    )
            if not isinstance(current_digest, str) or not current_digest or target_digest != current_digest:
                raise SecurityScanError(
                    f"SECURITY_SCAN_SNAPSHOT_STALE: completed authoritative scan {selected_id} no longer matches the provider's current repository snapshot.",
                    failure_class="SECURITY_SCAN_SNAPSHOT_STALE",
                )
            action = "reused"
        else:
            raise SecurityScanError(
                f"SECURITY_SCAN_AUTHORITY_INVALID: authoritative scan {selected_id} has unsupported status {status}.",
                failure_class="SECURITY_SCAN_AUTHORITY_INVALID",
            )
        return ScanDecision(
            action, plugin_id, plugin_version, current_path, target_id, target_revision,
            required_mode, scope, selected, tuple(exact),
            "The persisted run-local scan authority remains valid.",
        )

    new_scans = [
        scan for scan in exact
        if str(scan.get("scanId", scan.get("scan_id", ""))) not in baseline_ids
    ]
    if authority_state == "start_authorized_unclaimed":
        active_scans = [scan for scan in exact if _scan_status(scan) == "running"]
        if active_scans:
            scan_ids = ", ".join(
                str(scan.get("scanId", scan.get("scan_id", "unknown")))
                for scan in active_scans
            )
            raise SecurityScanError(
                f"SECURITY_SCAN_UNOWNED_ACTIVITY: an exact-target scan became active before Executor ownership was established: {scan_ids}.",
                failure_class="SECURITY_SCAN_UNOWNED_ACTIVITY",
            )
    if new_scans:
        classification = (
            "SECURITY_SCAN_UNOWNED_ACTIVITY"
            if authority_state == "start_authorized_unclaimed"
            else "SECURITY_SCAN_CONFLICT"
        )
        raise SecurityScanError(
            f"{classification}: an exact-target scan appeared before Executor ownership was established.",
            failure_class=classification,
        )

    return ScanDecision(
        "start", plugin_id, plugin_version, current_path, target_id, target_revision,
        required_mode, scope, None, tuple(exact),
        "The run's initial start authority is unchanged; no scan has appeared in the provider ledger.",
    )


_SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


def _version_key(value: str) -> tuple[object, ...] | None:
    """Return a SemVer precedence key, ignoring build metadata."""

    if not isinstance(value, str) or len(value) > 64:
        return None
    match = _SEMVER.fullmatch(value)
    if match is None:
        return None
    major, minor, patch, prerelease, _build = match.groups()
    identifiers = []
    if prerelease is not None:
        for identifier in prerelease.split("."):
            if identifier.isdigit():
                if len(identifier) > 1 and identifier.startswith("0"):
                    return None
                identifiers.append((0, int(identifier)))
            else:
                identifiers.append((1, identifier))
    return (int(major), int(minor), int(patch), 1 if prerelease is None else 0, tuple(identifiers))


def _installed_plugin(codex_home: Path) -> tuple[Path, str, str]:
    cache = codex_home.expanduser().resolve() / "plugins" / "cache"
    matches: list[tuple[Path, str, str]] = []
    if cache.is_dir():
        for manifest in cache.glob("*/codex-security/*/.codex-plugin/plugin.json"):
            plugin_root = manifest.parent.parent
            try:
                plugin = json.loads(manifest.read_text(encoding="utf-8"))
                installed = plugin_root.parent / ".codex-remote-plugin-install.json"
                mcp_manifest = json.loads((plugin_root / ".mcp.json").read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            servers = mcp_manifest.get("mcpServers") if isinstance(mcp_manifest, Mapping) else None
            if (
                isinstance(plugin, Mapping)
                and plugin.get("name") == "codex-security"
                and isinstance(plugin.get("version"), str)
                and installed.is_file()
                and isinstance(servers, Mapping)
                and isinstance(servers.get("codex-security"), Mapping)
                and (plugin_root / "mcp" / "server.mjs").is_file()
            ):
                marketplace = plugin_root.parent.parent.name
                matches.append((plugin_root, f"codex-security@{marketplace}", plugin["version"]))
    matches = [item for item in matches if item[1] == PLUGIN_ID and _version_key(item[2]) is not None]
    if not matches:
        raise SecurityScanError(
            "The Executor profile has no installed Codex Security MCP server.",
            failure_class="SECURITY_SCAN_PROVIDER_UNAVAILABLE",
        )
    matches.sort(
        key=lambda item: (_version_key(item[2]), "+" not in item[2], item[1], str(item[0]).casefold()),
        reverse=True,
    )
    return matches[0]


def _node_executable(env: Mapping[str, str]) -> str:
    candidates: list[str] = []
    for key in ("CODEX_MCP_NODE_PATH", "CODEX_BROWSER_USE_NODE_PATH"):
        if env.get(key):
            candidates.append(env[key])
    electron = env.get("CODEX_ELECTRON_RESOURCES_PATH")
    if electron:
        candidates.append(str(Path(electron) / "cua_node" / "bin" / ("node.exe" if os.name == "nt" else "node")))
    codex_cli = env.get("CODEX_CLI_PATH")
    if codex_cli:
        candidates.append(str(Path(codex_cli).parent / "cua_node" / "bin" / ("node.exe" if os.name == "nt" else "node")))
    for base in (env.get("XDG_CACHE_HOME", ""), env.get("USERPROFILE", ""), env.get("LOCALAPPDATA", "")):
        if not base:
            continue
        root = Path(base)
        if root.name.casefold() == "local" and os.name == "nt":
            candidates.extend(str(path / "bin" / "node.exe") for path in (root / "OpenAI" / "Codex" / "runtimes" / "cua_node").glob("*"))
        candidates.extend(
            [
                str(root / "codex-runtimes" / "codex-primary-runtime" / "dependencies" / "node" / "bin" / ("node.exe" if os.name == "nt" else "node")),
                str(root / ".cache" / "codex-runtimes" / "codex-primary-runtime" / "dependencies" / "node" / "bin" / ("node.exe" if os.name == "nt" else "node")),
            ]
        )
    resolved = next((str(Path(item).resolve()) for item in candidates if Path(item).is_file()), None)
    return resolved or shutil.which("node", path=env.get("PATH")) or ""


class _McpReadClient:
    def __init__(self, executable: str, server_path: Path, cwd: Path, env: Mapping[str, str]):
        if not executable:
            raise SecurityScanError(
                "The Codex Security MCP Node runtime is unavailable.",
                failure_class="SECURITY_SCAN_PROVIDER_UNAVAILABLE",
            )
        self.process = subprocess.Popen(
            [executable, str(server_path), "--stdio"],
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            shell=False,
        )
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._request_id = 0
        self._thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._thread.start()
        try:
            init = self._request(
                "initialize",
                {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "dual-codex-security-arbitrator", "version": "1"},
                },
            )
            server = init.get("serverInfo") if isinstance(init, Mapping) else None
            if not isinstance(server, Mapping) or server.get("name") != "codex-security":
                raise SecurityScanError(
                    "The installed MCP process did not identify as Codex Security.",
                    failure_class="SECURITY_SCAN_PROVIDER_UNAVAILABLE",
                )
            self.server_version = str(server.get("version", ""))
            self._notify("notifications/initialized", {})
            listed = self._request("tools/list", {})
            tools = listed.get("tools") if isinstance(listed, Mapping) else None
            tool_names = {item.get("name") for item in tools if isinstance(item, Mapping)} if isinstance(tools, list) else set()
            if not _READ_TOOLS.issubset(tool_names):
                raise SecurityScanError(
                    "The installed Codex Security MCP server is missing required read APIs.",
                    failure_class="SECURITY_SCAN_PROVIDER_UNAVAILABLE",
                )
        except BaseException:
            self.close()
            raise

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            if len(line.encode("utf-8", errors="replace")) > _MAX_MCP_LINE:
                self._lines.put(None)
                return
            self._lines.put(line)
        self._lines.put(None)

    def _send(self, message: Mapping[str, Any]) -> None:
        if self.process.poll() is not None or self.process.stdin is None:
            raise SecurityScanError(
                "The Codex Security MCP server stopped unexpectedly.",
                failure_class="SECURITY_SCAN_PROVIDER_UNAVAILABLE",
            )
        self.process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        self.process.stdin.flush()

    def _notify(self, method: str, params: Mapping[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": dict(params)})

    def _request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        self._request_id += 1
        request_id = self._request_id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)})
        deadline = time.monotonic() + _MCP_TIMEOUT_SECONDS
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SecurityScanError(
                    "The Codex Security MCP read API timed out.",
                    failure_class="SECURITY_SCAN_PROVIDER_UNAVAILABLE",
                )
            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty as exc:
                raise SecurityScanError(
                    "The Codex Security MCP read API timed out.",
                    failure_class="SECURITY_SCAN_PROVIDER_UNAVAILABLE",
                ) from exc
            if line is None:
                raise SecurityScanError(
                    "The Codex Security MCP server closed its output stream.",
                    failure_class="SECURITY_SCAN_PROVIDER_UNAVAILABLE",
                )
            try:
                message = json.loads(line)
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise SecurityScanError(
                    "The Codex Security MCP server returned invalid protocol data.",
                    failure_class="SECURITY_SCAN_PROVIDER_UNAVAILABLE",
                ) from exc
            if not isinstance(message, Mapping):
                raise SecurityScanError(
                    "The Codex Security MCP server returned invalid protocol data.",
                    failure_class="SECURITY_SCAN_PROVIDER_UNAVAILABLE",
                )
            if message.get("id") != request_id:
                continue
            if "error" in message or not isinstance(message.get("result"), Mapping):
                raise SecurityScanError(
                    "The Codex Security MCP read API returned an error.",
                    failure_class="SECURITY_SCAN_PROVIDER_UNAVAILABLE",
                )
            return dict(message["result"])

    def call_read_tool(self, name: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        if name not in _READ_TOOLS:
            raise SecurityScanError(
                "The scan arbitrator attempted a non-read Codex Security API.",
                failure_class="SECURITY_SCAN_PROVIDER_UNAVAILABLE",
            )
        result = self._request("tools/call", {"name": name, "arguments": dict(arguments)})
        if result.get("isError") is True:
            raise SecurityScanError(
                "The Codex Security scan ledger could not be read.",
                failure_class="SECURITY_SCAN_PROVIDER_UNAVAILABLE",
            )
        structured = result.get("structuredContent")
        if isinstance(structured, Mapping):
            return structured
        for item in result.get("content", []):
            if isinstance(item, Mapping) and isinstance(item.get("text"), str):
                try:
                    value = json.loads(item["text"])
                except (TypeError, json.JSONDecodeError):
                    continue
                if isinstance(value, Mapping):
                    return value
        raise SecurityScanError(
            "The Codex Security scan ledger returned no structured scan records.",
            failure_class="SECURITY_SCAN_PROVIDER_UNAVAILABLE",
        )

    def close(self) -> None:
        if self.process.poll() is None:
            if self.process.stdin is not None:
                self.process.stdin.close()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2)

    def __enter__(self) -> "_McpReadClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class CodexSecurityProvider:
    """Read the installed profile's machine-readable Codex Security ledger."""

    def __init__(self, agent: AgentConfig):
        self.agent = agent
        self.plugin_root, self.plugin_id, self.plugin_version = _installed_plugin(agent.codex_home)

    def list_target_scans(self, target_path: Path) -> list[dict[str, Any]]:
        env = codex_environment(self.agent, isolate_desktop_bridge=True)
        executable = _node_executable(env)
        scans: list[dict[str, Any]] = []
        offset = 0
        with _McpReadClient(executable, self.plugin_root / "mcp" / "server.mjs", self.plugin_root, env) as client:
            while True:
                result = client.call_read_tool(
                    "list_codex_security_scans",
                    {"query": str(target_path.expanduser().resolve()), "limit": 50, "offset": offset},
                )
                page = result.get("scans")
                if not isinstance(page, list):
                    raise SecurityScanError(
                        "The Codex Security scan ledger response omitted its scan list.",
                        failure_class="SECURITY_SCAN_PROVIDER_UNAVAILABLE",
                    )
                scans.extend(item for item in page if isinstance(item, Mapping))
                next_offset = result.get("nextOffset")
                if not isinstance(next_offset, int):
                    break
                if next_offset <= offset or next_offset > 1000:
                    raise SecurityScanError(
                        "The Codex Security scan ledger pagination is inconsistent or exceeded its bound.",
                        failure_class="SECURITY_SCAN_PROVIDER_UNAVAILABLE",
                    )
                offset = next_offset
        return [dict(scan) for scan in scans]

    def arbitrate(
        self,
        *,
        repository: Path,
        target_revision: str,
        required_mode: str = "standard",
        required_scope: str = ".",
        allow_completed_reuse: bool = False,
    ) -> ScanDecision:
        scans = self.list_target_scans(repository)
        return arbitrate_security_scans(
            scans,
            plugin_id=self.plugin_id,
            plugin_version=self.plugin_version,
            target_path=repository,
            target_revision=target_revision,
            required_mode=required_mode,
            required_scope=required_scope,
            allow_completed_reuse=allow_completed_reuse,
        )


def validate_scan_provenance(
    value: Any,
    *,
    decision: ScanDecision,
    final_scans: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Validate model evidence against the host decision and final provider ledger."""

    fields = {
        "plugin_id", "plugin_version", "target_identity", "scan_id", "mode",
        "initial_status", "action", "final_status",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise SecurityScanError(
            "Executor omitted the required structured Codex Security scan provenance.",
            failure_class="SECURITY_SCAN_EVIDENCE_INVALID",
        )
    target = value.get("target_identity")
    expected_target = {
        "path": decision.target_path,
        "target_id": decision.target_id,
        "revision": decision.target_revision,
        "scope": decision.required_scope,
    }
    if (
        value.get("plugin_id") != decision.plugin_id
        or value.get("plugin_version") != decision.plugin_version
        or target != expected_target
        or value.get("action") not in SCAN_ACTIONS
        or value.get("initial_status") not in SCAN_STATUSES
        or value.get("final_status") not in SCAN_STATUSES
        or not isinstance(value.get("scan_id"), str)
        or not value["scan_id"]
        or value.get("mode") not in {"standard", "deep"}
    ):
        raise SecurityScanError(
            "Executor scan provenance does not match the host-selected profile, target, or schema.",
            failure_class="SECURITY_SCAN_EVIDENCE_INVALID",
        )
    selected_id = str(value["scan_id"])
    before_ids = {str(scan.get("scanId", scan.get("scan_id", ""))) for scan in decision.observed_scans}
    same_path_final = [
        scan for scan in final_scans
        if _normal_path(scan.get("targetPath", scan.get("target_path"))) == _normal_path(decision.target_path)
    ]
    if any(
        str(scan.get("targetId", scan.get("target_id", ""))) != decision.target_id
        for scan in same_path_final
    ):
        raise SecurityScanError(
            "SECURITY_SCAN_CONFLICT: the final ledger contains a same-path scan with a different target identity.",
            failure_class="SECURITY_SCAN_CONFLICT",
        )
    exact_final = [
        scan for scan in same_path_final
        if str(scan.get("targetId", scan.get("target_id", ""))) == decision.target_id
    ]
    new_scans = [
        scan for scan in exact_final
        if str(scan.get("scanId", scan.get("scan_id", ""))) not in before_ids
    ]
    final_ids = {str(scan.get("scanId", scan.get("scan_id", ""))) for scan in exact_final}
    missing_observed = [
        scan for scan in decision.observed_scans
        if _scan_status(scan) == "running"
        and str(scan.get("scanId", scan.get("scan_id", ""))) not in final_ids
    ]
    if missing_observed:
        names = ", ".join(str(scan.get("scanId", scan.get("scan_id", "unknown"))) for scan in missing_observed)
        raise SecurityScanError(
            f"SECURITY_SCAN_CONFLICT: an existing active scan disappeared from the final provider ledger: {names}.",
            failure_class="SECURITY_SCAN_CONFLICT",
        )
    final_active = [scan for scan in exact_final if _scan_status(scan) == "running"]
    if len(final_active) > 1:
        names = ", ".join(
            f"{scan.get('scanId', scan.get('scan_id', 'unknown'))} "
            f"(mode={scan.get('mode', 'unknown')}, status={_scan_status(scan)})"
            for scan in final_active
        )
        raise SecurityScanError(
            f"SECURITY_SCAN_CONFLICT: multiple exact-target scans remain active: {names}.",
            failure_class="SECURITY_SCAN_CONFLICT",
        )
    canceled_observed = [
        scan for scan in decision.observed_scans
        if _scan_status(scan) == "running"
        and any(
            str(final.get("scanId", final.get("scan_id", "")))
            == str(scan.get("scanId", scan.get("scan_id", "")))
            and _scan_status(final) == "canceled"
            for final in exact_final
        )
    ]
    if canceled_observed:
        names = ", ".join(str(scan.get("scanId", scan.get("scan_id", "unknown"))) for scan in canceled_observed)
        raise SecurityScanError(
            f"SECURITY_SCAN_CONFLICT: an existing active scan was canceled during Executor work: {names}.",
            failure_class="SECURITY_SCAN_CONFLICT",
        )
    if decision.action == "start":
        if len(new_scans) > 1:
            names = ", ".join(
                f"{scan.get('scanId')} ({scan.get('mode')}/{_scan_status(scan)})" for scan in new_scans
            )
            raise SecurityScanError(
                f"SECURITY_SCAN_CONFLICT: Executor created multiple exact-target scans: {names}.",
                failure_class="SECURITY_SCAN_CONFLICT",
            )
        selected_matches = [
            scan for scan in new_scans
            if str(scan.get("scanId", scan.get("scan_id", ""))) == selected_id
        ]
        action_ok = value.get("action") == "started"
    else:
        selected_matches = [
            scan for scan in exact_final
            if str(scan.get("scanId", scan.get("scan_id", ""))) == selected_id
        ]
        if new_scans:
            names = ", ".join(
                f"{scan.get('scanId', scan.get('scan_id', 'unknown'))} "
                f"(mode={scan.get('mode', 'unknown')}, status={_scan_status(scan)})"
                for scan in new_scans
            )
            raise SecurityScanError(
                f"SECURITY_SCAN_CONFLICT: Executor created a scan after preflight selected an existing scan: {names}.",
                failure_class="SECURITY_SCAN_CONFLICT",
            )
        action_ok = (
            selected_id == str((decision.selected_scan or {}).get("scanId", ""))
            and value.get("action") in ({"awaited", "resumed"} if decision.action == "awaited" else {"reused"})
        )
    if len(selected_matches) != 1 or not action_ok:
        raise SecurityScanError(
            "Executor scan provenance is not backed by the selected exact-target provider scan.",
            failure_class="SECURITY_SCAN_EVIDENCE_INVALID",
        )
    selected = selected_matches[0]
    actual_status = _scan_status(selected)
    mode = str(selected.get("mode", ""))
    revision = str(selected.get("targetRevision", selected.get("target_revision", "")))
    scope = _scope(selected.get("scope", ""))
    if (
        value.get("mode") != mode
        or (
            decision.action != "start"
            and value.get("initial_status") != _scan_status(decision.selected_scan or {})
        )
        or revision != decision.target_revision
        or not _scope_covers(scope, decision.required_scope)
        or not _supports_mode(mode, decision.required_mode)
        or (
            decision.action == "start"
            and (mode != decision.required_mode or scope != decision.required_scope)
        )
        or (
            decision.action != "start"
            and (
                mode != str((decision.selected_scan or {}).get("mode", ""))
                or scope != _scope((decision.selected_scan or {}).get("scope", ""))
            )
        )
        or value.get("final_status") != actual_status
    ):
        raise SecurityScanError(
            "The selected provider scan changed mode, target, scope, or final status from the Executor evidence.",
            failure_class="SECURITY_SCAN_EVIDENCE_INVALID",
        )
    if actual_status != "complete":
        raise SecurityScanError(
            f"SECURITY_SCAN_INCOMPLETE: selected scan {selected_id} ended with status {actual_status}.",
            failure_class="SECURITY_SCAN_INCOMPLETE",
        )
    return {
        "plugin_id": decision.plugin_id,
        "plugin_version": decision.plugin_version,
        "target_identity": expected_target,
        "scan_id": selected_id,
        "scan_mode": mode,
        "initial_status": str(value["initial_status"]),
        "action": str(value["action"]),
        "final_status": actual_status,
    }
