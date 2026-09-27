from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
import threading
from typing import Mapping
from uuid import uuid4

from .bootstrap import (
    canonical_bootstrap_artifacts,
    canonical_bootstrap_control_paths,
    canonical_instructions_root,
    reconcile_orphan_canonical_bootstrap,
)
from .codex import _delegate_to_configured_actor
from .codex import configured_actor_provenance, run_codex_for_role
from .config import ConfigError, OrchestratorConfig, SUPPORTED_ROLES
from .delegation import RepositoryLock
from .git import (
    attribute_git_mutations,
    capture_git_baseline,
    ensure_git_repository,
    git_top_level,
    status_and_diff,
)
from .paths import same_path
from .report import atomic_write_json, dump_json, load_json, render_markdown
from .security_scan import (
    CodexSecurityProvider,
    ScanDecision,
    SecurityScanError,
    arbitrate_security_scans,
    continue_security_scan_authority,
    validate_scan_provenance,
)


@dataclass(frozen=True)
class RunOutcome:
    run_dir: Path
    verdict: str
    correction_cycles: int
    phase_provenance: tuple[dict, ...] = ()
    run_result: dict = field(default_factory=dict)


def _control_parent_is_safe(path: Path) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    for parent in reversed(path.parents):
        try:
            info = parent.lstat()
        except OSError:
            return False
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & reparse_flag
        ):
            return False
    return True


def _safe_regular_control_file(path: Path) -> bool:
    path = Path(os.path.abspath(path.expanduser()))
    if not _control_parent_is_safe(path):
        return False
    try:
        info = path.lstat()
    except OSError:
        return False
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return (
        stat.S_ISREG(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and not (getattr(info, "st_file_attributes", 0) & reparse_flag)
    )


def _safe_atomic_write_control_json(path: Path, data: dict) -> None:
    """Atomically write host state without resolving or following the target path."""

    path = Path(os.path.abspath(path.expanduser()))
    if not _control_parent_is_safe(path):
        raise OSError("control artifact parent is not a regular directory")
    try:
        existing = path.lstat()
    except FileNotFoundError:
        existing = None
    if existing is not None and not _safe_regular_control_file(path):
        raise OSError("control artifact is not a regular non-reparse file")

    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid4().hex}")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(dump_json(data) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        if not _control_parent_is_safe(path):
            raise OSError("control artifact parent changed during write")
        try:
            current = path.lstat()
        except FileNotFoundError:
            current = None
        if current is not None and not _safe_regular_control_file(path):
            raise OSError("control artifact changed to a link or non-file during write")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def delegate_to_configured_actor(**kwargs):
    """Compatibility wrapper that keeps the registry as the source of truth.

    The runner is passed explicitly so deterministic tests can replace the
    provider boundary without changing role resolution or actor provenance.
    """

    return _delegate_to_configured_actor(**kwargs, runner=run_codex_for_role)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _prompt(config: OrchestratorConfig, name: str, **values: str) -> str:
    template = _read(config.project_root / "prompts" / name)
    return template.format(**values)


def _schema(config: OrchestratorConfig, name: str) -> Path:
    return config.project_root / "schemas" / name


def _reviewer_phase_context(config: OrchestratorConfig, phase_provenance: list[dict], run_state: dict) -> dict:
    fields = (
        "role",
        "primary_actor",
        "actual_actor",
        "provider",
        "backend",
        "fallback_enabled",
        "fallback_used",
        "repository",
    )
    reviewer = config.agent_for_role("reviewer")
    authority = run_state.get("security_scan_authority")
    authority = authority if isinstance(authority, Mapping) else {}
    requirement = run_state.get("mission_security_requirement")
    requirement = requirement if isinstance(requirement, Mapping) else {}
    history = run_state.get("security_scan_authority_history")
    history = history if isinstance(history, list) else []
    security_history = [
        {
            field: event.get(field)
            for field in ("event", "generation", "selected_scan_id", "failure_class")
            if event.get(field) is not None
        }
        for event in history
        if isinstance(event, Mapping)
        and event.get("event") in {"run_owned", "completed_fresh", "completed_stale", "generation_authorized", "rescan_limit"}
    ]
    security_gate = {
        "required": bool(requirement.get("required", False)),
        "authority_state": str(authority.get("authority_state") or "missing"),
        "fresh_for_acceptance": authority.get("authority_state") == "completed_fresh",
        "generation": authority.get("generation"),
        "max_generations": authority.get("max_generations"),
        "required_mode": str(authority.get("required_mode") or requirement.get("mode") or ""),
        "required_scope": str(authority.get("required_scope") or requirement.get("scope") or ""),
        "target_identity": {
            field: str(authority.get(field) or "")
            for field in ("target_path", "target_id", "target_revision")
        },
        "selected_scan": {
            "scan_id": str(authority.get("selected_scan_id") or ""),
            "mode": str(authority.get("selected_scan_mode") or ""),
            "scope": str(authority.get("selected_scan_scope") or ""),
        },
        "coverage_history": security_history,
        "authority_note": (
            "This host-controlled gate summary determines whether Security coverage is fresh. "
            "Executor scan evidence is historical and does not override this summary."
        ),
    }
    return {
        "completed_phases": [
            {field: item.get(field, "") for field in fields}
            for item in phase_provenance
            if item.get("role") in {"architect", "executor"}
        ],
        "current_phase": {
            "role": "reviewer",
            "configured_actor": reviewer.account_name,
            "provider": reviewer.provider_type,
            "backend": reviewer.backend,
            "actual_runtime_identity": "recorded by the control plane after this phase completes",
        },
        "host_security_gate": security_gate,
    }


def _write_provenance(
    config: OrchestratorConfig,
    canonical_root: Path,
    run_dir: Path,
    phase_provenance: list[dict],
    run_state: dict | None = None,
) -> None:
    try:
        orchestrator_metadata = configured_actor_provenance(
            agent=config.agent_for_role("orchestrator"),
            role="orchestrator",
            repository=config.repository,
            canonical_root=canonical_root,
        )
    except ConfigError as exc:
        orchestrator_metadata = {
            "phase": "orchestrator",
            "role": "orchestrator",
            "configured_actor": False,
            "routing_error": str(exc),
            "fallback_used": False,
        }
    payload = {
        "schema_version": 2,
        "configured_actor_routing": phase_provenance,
        "orchestrator": orchestrator_metadata,
    }
    if run_state is not None:
        payload.update(
            {
                "run_id": run_state.get("run_id", ""),
                "repository": run_state.get("repository", ""),
                "status": run_state.get("status", "running"),
                "current_phase": run_state.get("current_phase"),
                "last_phase": run_state.get("last_phase"),
                "current_actor": run_state.get("current_actor"),
                "current_backend": run_state.get("current_backend"),
                "last_actor": run_state.get("last_actor"),
                "last_backend": run_state.get("last_backend"),
                "provider_status": run_state.get("provider_status", "unknown"),
                "last_progress_at": run_state.get("last_progress_at"),
                "initial_git_baseline": run_state.get("initial_git_baseline"),
                "mutation_attribution": run_state.get("mutation_attribution"),
                "security_scan_arbitrations": run_state.get("security_scan_arbitrations", []),
                "security_scan_authority": run_state.get("security_scan_authority"),
                "security_scan_authority_history": run_state.get("security_scan_authority_history", []),
                "security_scan_provenance": run_state.get("security_scan_provenance", []),
                "security_scan_only_mutation_history": run_state.get("security_scan_only_mutation_history", []),
                "app_server_dispatch_provenance": run_state.get("app_server_dispatch_provenance", []),
                "dual_agents_bootstrap_artifacts": run_state.get("dual_agents_bootstrap_artifacts", []),
                "failure": run_state.get("failure"),
            }
        )
    _safe_atomic_write_control_json(run_dir / "provenance.json", payload)


_APP_SERVER_PROGRESS = re.compile(r"^app-server turn [A-Za-z0-9._:-]{1,100} still running$")


def _progress_event(progress, **values) -> None:
    if progress is None:
        return
    try:
        progress("DUAL_CODEX_PROGRESS " + json.dumps(values, ensure_ascii=True, separators=(",", ":")))
    except (BrokenPipeError, OSError):
        # Progress is observational. A closed console must not change provider
        # dispatch or its configured hard timeout.
        return


def _safe_backend_detail(value: str) -> str | None:
    detail = str(value).strip()
    if _APP_SERVER_PROGRESS.fullmatch(detail):
        return detail
    if detail in {
        "Claude Code turn started",
        "Antigravity executor still running",
    }:
        return detail
    return None


def _atomic_write_text(path: Path, value: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid4().hex}")
    try:
        temporary.write_text(value, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _persist_run_state(
    config: OrchestratorConfig,
    canonical_root: Path,
    run_dir: Path,
    phase_provenance: list[dict],
    run_state: dict,
    *,
    required: bool = False,
) -> None:
    run_state["updated_at"] = datetime.now(timezone.utc).isoformat()
    try:
        _safe_atomic_write_control_json(run_dir / "run_state.json", run_state)
        _write_provenance(config, canonical_root, run_dir, phase_provenance, run_state)
        return
    except OSError as exc:
        run_state["evidence_write_error"] = type(exc).__name__
        if required:
            raise


def _phase_failure_state(exc: BaseException) -> str:
    failure_class = str(getattr(exc, "failure_class", ""))
    if (
        isinstance(exc, TimeoutError)
        or "timeout" in type(exc).__name__.casefold()
        or "timeout" in failure_class.casefold()
        or "timed out" in str(exc).casefold()
    ):
        return "timeout"
    return "failed"


_SECURITY_SCAN_INTENT = re.compile(
    r"^\s*(?:(?:please|you\s+(?:must|should|need\s+to))\s+)?"
    r"(?:run|start|perform|conduct|execute|launch|await|wait\s+for|complete)\b.{0,100}"
    r"\b(?:codex\s+security\s+(?:standard\s+|deep\s+)?scan|"
    r"(?:standard\s+|deep\s+)?security\s+scan)\b",
    re.IGNORECASE,
)


_SECURITY_TOOL_REQUIRED = re.compile(
    r"\bcodex\s+security\s+(?:plugin(?:/tool)?|tool)\b.{0,160}"
    r"\b(?:must|shall|required|mandatory)\s+(?:(?:be|also)\s+)?(?:run|execute|perform|conduct|start)\b",
    re.IGNORECASE,
)

_DEEP_SCAN_REQUIRED = re.compile(
    r"^\s*(?:(?:please|you\s+(?:must|should|need\s+to))\s+)?"
    r"(?:run|start|perform|conduct|execute|launch|await|wait\s+for|complete)\b"
    r".{0,100}\bdeep\s+(?:(?:codex\s+)?security\s+)?scan\b|"
    r"^\s*(?:the\s+)?deep\s+(?:(?:codex\s+)?security\s+)?scan\b.{0,60}\b(?:must|required|mandatory)\b",
    re.IGNORECASE,
)

_NEGATED_DEEP_SCAN = re.compile(
    r"\b(?:do\s+not|don't|never|must\s+not|should\s+not|not)\b.{0,80}"
    r"\bdeep\s+(?:(?:codex\s+)?security\s+)?scan\b|"
    r"\bdeep\s+(?:(?:codex\s+)?security\s+)?scan\b.{0,80}"
    r"\b(?:not|required against|avoid|forbid|prohibit)\b",
    re.IGNORECASE,
)

_MISSION_SECURITY_SCOPE = re.compile(
    r"^\s*(?:codex\s+)?security\s+scan\s+scope\s*[:=]\s*([^\s#]+)",
    re.IGNORECASE | re.MULTILINE,
)


def _security_scan_request(task: str) -> tuple[bool, str, str]:
    mission_lines = [re.sub(r"^\s*(?:[-*]|\d+[.)])\s*", "", line) for line in task.splitlines()]
    requested = any(
        _SECURITY_SCAN_INTENT.search(line) for line in mission_lines
    ) or bool(_SECURITY_TOOL_REQUIRED.search(task))
    if not requested:
        return False, "standard", "."
    mode = "deep" if any(
        _DEEP_SCAN_REQUIRED.search(line) and not _NEGATED_DEEP_SCAN.search(line)
        for line in mission_lines
    ) else "standard"
    scope_match = _MISSION_SECURITY_SCOPE.search(task)
    if scope_match is None:
        return True, mode, "."
    requested_scope = scope_match.group(1).strip("`\"'").replace("\\", "/")
    scope_path = PurePosixPath(requested_scope)
    if (
        not requested_scope
        or scope_path.is_absolute()
        or ":" in requested_scope
        or ".." in scope_path.parts
    ):
        raise SecurityScanError(
            "The original mission contains an invalid Codex Security scan scope.",
            failure_class="SECURITY_SCAN_REQUIREMENT_INVALID",
        )
    return True, mode, scope_path.as_posix() or "."


def _architect_security_gate_policy(
    requirement: tuple[bool, str, str],
    decision: ScanDecision | None,
) -> str:
    required, mode, scope = requirement
    if not required:
        return (
            "The original mission does not require a Codex Security scan. Architect must not start, resume, cancel, "
            "await operationally, or claim completion of any Security scan. Do not add or claim a host-mandated scan."
        )
    if decision is None:
        return (
            f"The original mission requires a Codex Security {mode} scan for scope {scope!r}. "
            "The host controls scan operations; do not operate or claim completion of a scan."
        )
    return (
        "HOST SECURITY GATE SUMMARY (declarative, read-only): "
        + decision.architect_summary()
    )


def _executor_security_gate_policy(policy: str) -> str:
    if not policy:
        return ""
    return (
        "The host Security arbitration below is authoritative and supersedes any conflicting Architect plan. "
        "Follow its exact mode, scope, target, and selected scan.\n"
        + policy
    )


def _max_security_generations(config: OrchestratorConfig) -> int:
    """Allow an initial scan, its implementation replacement, and one per correction."""

    correction_cycles = int(getattr(config, "max_correction_cycles", 1))
    return max(2, correction_cycles + 2)


def _load_security_scan_only_output(path: Path):
    """Read the Executor report only from a stable, regular file, without following links."""

    def regular_file(info) -> bool:
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        return (
            stat.S_ISREG(info.st_mode)
            and not stat.S_ISLNK(info.st_mode)
            and not (getattr(info, "st_file_attributes", 0) & reparse_flag)
        )

    def identity(info) -> tuple[int, int, int, int, int]:
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_mode)

    def safe_parent_chain() -> bool:
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        for parent in reversed(Path(os.path.abspath(path)).parents):
            try:
                info = parent.lstat()
            except OSError:
                return False
            if (
                not stat.S_ISDIR(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & reparse_flag
            ):
                return False
        return True

    try:
        if not safe_parent_chain():
            raise OSError("scan-only output has an unsafe parent directory")
        before = path.lstat()
        if not regular_file(before):
            raise OSError("scan-only output is not a regular non-reparse file")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if not regular_file(opened) or identity(opened) != identity(before):
                raise OSError("scan-only output changed before it was opened")
            payload = stream.read()
            after = os.fstat(stream.fileno())
            current = path.lstat()
            if (
                not safe_parent_chain()
                or not regular_file(after)
                or identity(after) != identity(opened)
                or not regular_file(current)
                or identity(current) != identity(opened)
            ):
                raise OSError("scan-only output changed while it was read")
        return json.loads(payload.decode("utf-8"))
    except SecurityScanError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SecurityScanError(
            "SECURITY_SCAN_ONLY_OUTPUT_INVALID: scan-only Executor output is missing, linked, unstable, or invalid JSON.",
            failure_class="SECURITY_SCAN_ONLY_OUTPUT_INVALID",
        ) from exc


def _prepare_security_scan(
    *,
    config: OrchestratorConfig,
    requirement: tuple[bool, str, str],
    target_revision: str,
    run_state: dict,
    canonical_root: Path,
    run_dir: Path,
    phase_provenance: list[dict],
    checkpoint: str,
    authorize_executor_dispatch: bool = False,
) -> tuple[CodexSecurityProvider | None, ScanDecision | None, str]:
    required, mode, scope = requirement
    if not required:
        return None, None, ""
    provider = CodexSecurityProvider(config.agent_for_role("executor"))
    authority = run_state.get("security_scan_authority")
    try:
        if not isinstance(authority, Mapping):
            decision = provider.arbitrate(
                repository=config.repository,
                target_revision=target_revision,
                required_mode=mode,
                required_scope=scope,
                allow_completed_reuse=False,
            )
            if decision.action != "conflict":
                authority = {
                    "plugin_id": decision.plugin_id,
                    "plugin_version": decision.plugin_version,
                    "target_path": decision.target_path,
                    "target_id": decision.target_id,
                    "target_revision": decision.target_revision,
                    "required_mode": decision.required_mode,
                    "required_scope": decision.required_scope,
                    "generation": 1,
                    "max_generations": _max_security_generations(config),
                    "authority_state": (
                        "start_authorized_unclaimed" if decision.action == "start" else "existing_authority"
                    ),
                    "ownership_state": "unclaimed" if decision.action == "start" else "existing_authority",
                    "initial_checkpoint": checkpoint,
                    "initial_decision": decision.action,
                    "initial_observed_scan_ids": [
                        str(scan.get("scanId", scan.get("scan_id", "")))
                        for scan in decision.observed_scans
                        if scan.get("scanId", scan.get("scan_id", ""))
                    ],
                    "generation_observed_scan_ids": [
                        str(scan.get("scanId", scan.get("scan_id", "")))
                        for scan in decision.observed_scans
                        if scan.get("scanId", scan.get("scan_id", ""))
                    ],
                    "executor_dispatch_authorized": False,
                    "selected_scan_id": str(
                        (decision.selected_scan or {}).get("scanId", (decision.selected_scan or {}).get("scan_id", ""))
                    ),
                    "selected_scan_mode": str((decision.selected_scan or {}).get("mode", "")),
                    "selected_scan_scope": str((decision.selected_scan or {}).get("scope", "")),
                    "initial_selected_status": (
                        str((decision.selected_scan or {}).get("progress", {}).get("status", ""))
                        if isinstance((decision.selected_scan or {}).get("progress", {}), Mapping)
                        else str((decision.selected_scan or {}).get("status", ""))
                    ),
                }
                run_state["security_scan_authority"] = dict(authority)
        else:
            scans = provider.list_target_scans(config.repository)
            try:
                decision = continue_security_scan_authority(
                    scans,
                    authority=authority,
                    plugin_id=provider.plugin_id,
                    plugin_version=provider.plugin_version,
                    target_path=config.repository,
                    target_revision=target_revision,
                    required_mode=mode,
                    required_scope=scope,
                )
            except SecurityScanError as stale_error:
                if stale_error.failure_class != "SECURITY_SCAN_SNAPSHOT_STALE":
                    raise
                stale_authority = dict(authority)
                authority = stale_authority
                generation = int(stale_authority.get("generation", 1))
                validation = stale_authority.get("completed_validation")
                selected_id = str(stale_authority.get("selected_scan_id") or "")
                selected_scan = next(
                    (
                        item for item in scans
                        if str(item.get("scanId", item.get("scan_id", ""))) == selected_id
                    ),
                    {},
                )
                stale_event = {
                    "checkpoint": checkpoint,
                    "event": "completed_stale",
                    "generation": generation,
                    "selected_scan_id": selected_id,
                    "failure_class": stale_error.failure_class,
                }
                if isinstance(validation, Mapping) and validation.get("target_snapshot_identity"):
                    stale_event["target_snapshot_identity"] = validation["target_snapshot_identity"]
                current_digest = selected_scan.get("currentSnapshotDigest") if selected_scan else None
                if isinstance(current_digest, str) and current_digest:
                    stale_event["current_snapshot_identity"] = hashlib.sha256(
                        current_digest.encode("utf-8")
                    ).hexdigest()
                run_state.setdefault("security_scan_authority_history", []).append(stale_event)
                stale_authority["authority_state"] = "completed_stale"
                run_state["security_scan_authority"] = stale_authority

                max_generations = _max_security_generations(config)
                stale_authority["max_generations"] = max_generations
                if generation >= max_generations:
                    limit_event = {
                        "checkpoint": checkpoint,
                        "event": "rescan_limit",
                        "generation": generation,
                        "selected_scan_id": selected_id,
                    }
                    run_state.setdefault("security_scan_authority_history", []).append(limit_event)
                    stale_authority["authority_state"] = "completed_stale"
                    _persist_run_state(config, canonical_root, run_dir, phase_provenance, run_state)
                    raise SecurityScanError(
                        f"SECURITY_SCAN_RESCAN_LIMIT: scan generation {generation} became stale and the run reached its bounded limit of {max_generations} generations.",
                        failure_class="SECURITY_SCAN_RESCAN_LIMIT",
                    )

                replacement = arbitrate_security_scans(
                    scans,
                    plugin_id=provider.plugin_id,
                    plugin_version=provider.plugin_version,
                    target_path=config.repository,
                    target_revision=target_revision,
                    required_mode=mode,
                    required_scope=scope,
                    allow_completed_reuse=False,
                )
                if replacement.action != "start":
                    stale_authority["authority_state"] = "conflict"
                    run_state.setdefault("security_scan_authority_history", []).append(
                        {
                            "checkpoint": checkpoint,
                            "event": "replacement_conflict",
                            "generation": generation + 1,
                            "failure_class": "SECURITY_SCAN_CONFLICT",
                        }
                    )
                    _persist_run_state(config, canonical_root, run_dir, phase_provenance, run_state)
                    raise SecurityScanError(
                        "SECURITY_SCAN_CONFLICT: a replacement Security generation cannot start while the exact-target ledger is ambiguous or active.",
                        failure_class="SECURITY_SCAN_CONFLICT",
                    )

                decision = replacement
                stale_authority.update(
                    {
                        "generation": generation + 1,
                        "authority_state": "start_authorized_unclaimed",
                        "ownership_state": "unclaimed",
                        "generation_observed_scan_ids": [
                            str(scan.get("scanId", scan.get("scan_id", "")))
                            for scan in decision.observed_scans
                            if scan.get("scanId", scan.get("scan_id", ""))
                        ],
                        "selected_scan_id": "",
                        "selected_scan_mode": "",
                        "selected_scan_scope": "",
                        "completed_validation": None,
                        "executor_dispatch_authorized": False,
                    }
                )
                run_state["security_scan_authority"] = stale_authority
                run_state.setdefault("security_scan_authority_history", []).append(
                    {
                        "checkpoint": checkpoint,
                        "event": "generation_authorized",
                        "generation": generation + 1,
                        "previous_scan_id": selected_id,
                        "decision": "start",
                        "required_mode": mode,
                        "required_scope": scope,
                    }
                )
            authority = dict(run_state.get("security_scan_authority", authority))
            if decision.selected_scan is not None:
                authority["selected_scan_id"] = str(
                    decision.selected_scan.get("scanId", decision.selected_scan.get("scan_id", ""))
                )
                if not authority.get("selected_scan_mode"):
                    authority["selected_scan_mode"] = str(decision.selected_scan.get("mode", ""))
                if not authority.get("selected_scan_scope"):
                    authority["selected_scan_scope"] = str(decision.selected_scan.get("scope", ""))
                if decision.action == "reused" and not authority.get("completed_validation"):
                    snapshot_digest = decision.selected_scan.get("targetSnapshotDigest")
                    current_digest = decision.selected_scan.get("currentSnapshotDigest")
                    if not isinstance(snapshot_digest, str) or not snapshot_digest:
                        raise SecurityScanError(
                            "SECURITY_SCAN_AUTHORITY_INVALID: completed scan has no target snapshot identity.",
                            failure_class="SECURITY_SCAN_AUTHORITY_INVALID",
                        )
                    if not isinstance(current_digest, str) or not current_digest or snapshot_digest != current_digest:
                        raise SecurityScanError(
                            "SECURITY_SCAN_SNAPSHOT_STALE: completed scan no longer matches the provider's current repository snapshot.",
                            failure_class="SECURITY_SCAN_SNAPSHOT_STALE",
                        )
                    authority["completed_validation"] = {
                        "scan_id": authority["selected_scan_id"],
                        "target_snapshot_identity": hashlib.sha256(
                            str(snapshot_digest).encode("utf-8")
                        ).hexdigest(),
                        "current_snapshot_identity": hashlib.sha256(
                            current_digest.encode("utf-8")
                        ).hexdigest(),
                        "validated_at_checkpoint": checkpoint,
                    }
                authority["authority_state"] = "completed_fresh" if decision.action == "reused" else authority.get("ownership_state", "existing_authority")
                run_state["security_scan_authority"] = authority
        if authorize_executor_dispatch and isinstance(authority, dict) and authority.get("authority_state") == "start_authorized_unclaimed":
            if authority.get("executor_dispatch_authorized"):
                raise SecurityScanError(
                    "SECURITY_SCAN_AUTHORITY_INVALID: a start generation already has an Executor dispatch authorized.",
                    failure_class="SECURITY_SCAN_AUTHORITY_INVALID",
                )
            authority["executor_dispatch_authorized"] = True
            authority["executor_dispatch_checkpoint"] = checkpoint
            run_state["security_scan_authority"] = authority
    except SecurityScanError as exc:
        failure_record = {
            "checkpoint": checkpoint,
            "failure_class": exc.failure_class,
            "authority_scan_id": str((authority or {}).get("selected_scan_id", "")) if isinstance(authority, Mapping) else "",
        }
        if isinstance(authority, dict):
            if exc.failure_class in {"SECURITY_SCAN_CONFLICT", "SECURITY_SCAN_UNOWNED_ACTIVITY"}:
                authority["authority_state"] = "conflict"
            elif exc.failure_class == "SECURITY_SCAN_AUTHORITY_FAILED":
                authority["authority_state"] = "failed"
            elif exc.failure_class == "SECURITY_SCAN_AUTHORITY_LOST":
                authority["authority_state"] = "lost"
            elif exc.failure_class in {"SECURITY_SCAN_SNAPSHOT_STALE", "SECURITY_SCAN_RESCAN_LIMIT"}:
                authority["authority_state"] = "completed_stale"
            else:
                authority["authority_state"] = "failed"
            run_state["security_scan_authority"] = authority
        run_state.setdefault("security_scan_authority_history", []).append(failure_record)
        run_state.setdefault("security_scan_arbitrations", []).append(dict(failure_record))
        _persist_run_state(config, canonical_root, run_dir, phase_provenance, run_state)
        raise

    record = decision.public_record()
    record["checkpoint"] = checkpoint
    run_state.setdefault("security_scan_arbitrations", []).append(record)
    history_entry = dict(record)
    history_entry["selected_scan_id"] = str(
        (decision.selected_scan or {}).get("scanId", (decision.selected_scan or {}).get("scan_id", ""))
    )
    history_entry["generation"] = int(authority.get("generation", 1)) if isinstance(authority, Mapping) else 1
    history_entry["authority_state"] = str(authority.get("authority_state", "")) if isinstance(authority, Mapping) else ""
    history_entry["ownership_state"] = str(authority.get("ownership_state", "")) if isinstance(authority, Mapping) else ""
    if decision.selected_scan is not None:
        progress = decision.selected_scan.get("progress")
        history_entry["selected_status"] = str(
            progress.get("status", "") if isinstance(progress, Mapping) else decision.selected_scan.get("status", "")
        )
    run_state.setdefault("security_scan_authority_history", []).append(history_entry)
    _persist_run_state(config, canonical_root, run_dir, phase_provenance, run_state)
    if decision.action == "conflict":
        observed = ", ".join(
            f"{scan.get('scanId', scan.get('scan_id', 'unknown'))} "
            f"(mode={scan.get('mode', 'unknown')}, status={scan.get('progress', {}).get('status', scan.get('status', 'unknown'))})"
            for scan in decision.observed_scans
        )
        raise SecurityScanError(
            "SECURITY_SCAN_CONFLICT: " + decision.reason + (f" Active scans: {observed}." if observed else ""),
            failure_class="SECURITY_SCAN_CONFLICT",
        )
    return provider, decision, decision.executor_instruction()


def _record_security_scan_result_impl(
    *,
    provider: CodexSecurityProvider | None,
    decision: ScanDecision | None,
    checkpoint: str = "executor_postflight",
    implementation: Mapping[str, Any],
    run_state: dict,
    phase_provenance: list[dict],
    config: OrchestratorConfig,
    canonical_root: Path,
    run_dir: Path,
) -> None:
    if provider is None or decision is None:
        if implementation.get("security_scan_provenance") is not None:
            raise SecurityScanError(
                "Executor reported Codex Security use without a host scan preflight decision.",
                failure_class="SECURITY_SCAN_EVIDENCE_INVALID",
            )
        return
    final_scans = provider.list_target_scans(config.repository)
    authority = run_state.get("security_scan_authority")
    if not isinstance(authority, dict):
        raise SecurityScanError(
            "SECURITY_SCAN_AUTHORITY_INVALID: the run-local scan authority is missing after Executor dispatch.",
            failure_class="SECURITY_SCAN_AUTHORITY_INVALID",
        )
    actor = config.agent_for_role("executor")
    executor_phase = next(
        (item for item in reversed(phase_provenance) if item.get("role") == "executor"),
        None,
    )
    if (
        not isinstance(executor_phase, Mapping)
        or executor_phase.get("phase_state") != "completed"
        or executor_phase.get("configured_actor") is not True
        or executor_phase.get("actor_id") != actor.account_name
        or executor_phase.get("profile_id") != actor.account_name
        or executor_phase.get("primary_actor") != actor.account_name
        or executor_phase.get("actual_actor") != actor.account_name
        or executor_phase.get("provider") != actor.provider_type
        or executor_phase.get("backend") != actor.backend
        or executor_phase.get("fallback_used") is not False
        or executor_phase.get("dispatch_failed") is not False
    ):
        raise SecurityScanError(
            "SECURITY_SCAN_EVIDENCE_INVALID: Security scan evidence must come from the exact configured Executor without fallback or substitution.",
            failure_class="SECURITY_SCAN_EVIDENCE_INVALID",
        )
    if authority.get("authority_state") == "start_authorized_unclaimed":
        if not authority.get("executor_dispatch_authorized"):
            raise SecurityScanError(
                "SECURITY_SCAN_AUTHORITY_INVALID: the host did not authorize an Executor dispatch for this scan generation.",
                failure_class="SECURITY_SCAN_AUTHORITY_INVALID",
            )
        generation_baseline = {
            str(item)
            for item in authority.get(
                "generation_observed_scan_ids",
                authority.get("initial_observed_scan_ids", []),
            )
            if item
        }
        exact_new = [
            scan for scan in final_scans
            if scan.get("targetPath", scan.get("target_path"))
            and same_path(Path(str(scan.get("targetPath", scan.get("target_path")))), Path(decision.target_path))
            and str(scan.get("targetId", scan.get("target_id", ""))) == decision.target_id
            and str(scan.get("scanId", scan.get("scan_id", ""))) not in generation_baseline
        ]
        reported = implementation.get("security_scan_provenance")
        reported_id = str(reported.get("scan_id") or "") if isinstance(reported, Mapping) else ""
        if len(exact_new) != 1 or not reported_id:
            if exact_new:
                raise SecurityScanError(
                    "SECURITY_SCAN_CONFLICT: newly appearing exact-target scan activity cannot be attributed without one matching Executor report.",
                    failure_class="SECURITY_SCAN_CONFLICT",
                )
    evidence = validate_scan_provenance(
        implementation.get("security_scan_provenance"),
        decision=decision,
        final_scans=final_scans,
    )
    scan_id = str(evidence["scan_id"])
    if authority.get("selected_scan_id") and authority["selected_scan_id"] != scan_id:
        raise SecurityScanError(
            "SECURITY_SCAN_AUTHORITY_INVALID: Executor evidence names a scan other than the persisted run authority.",
            failure_class="SECURITY_SCAN_AUTHORITY_INVALID",
        )
    selected_matches = [
        scan for scan in final_scans
        if str(scan.get("scanId", scan.get("scan_id", ""))) == scan_id
        and str(scan.get("targetId", scan.get("target_id", ""))) == decision.target_id
        and scan.get("targetPath", scan.get("target_path"))
        and same_path(Path(str(scan.get("targetPath", scan.get("target_path")))), Path(decision.target_path))
    ]
    if len(selected_matches) != 1:
        raise SecurityScanError(
            "SECURITY_SCAN_AUTHORITY_LOST: the validated scan is missing or ambiguous while recording completion.",
            failure_class="SECURITY_SCAN_AUTHORITY_LOST",
        )
    selected = selected_matches[0]
    selected_mode = str(selected.get("mode", ""))
    selected_scope = str(selected.get("scope", "")).replace("\\", "/").strip("/") or "."
    pinned_mode = str(authority.get("selected_scan_mode") or "")
    pinned_scope_raw = str(authority.get("selected_scan_scope") or "")
    pinned_scope = (pinned_scope_raw.replace("\\", "/").strip("/") or ".") if pinned_scope_raw else ""
    if (pinned_mode and pinned_mode != selected_mode) or (pinned_scope and pinned_scope != selected_scope):
        raise SecurityScanError(
            "SECURITY_SCAN_AUTHORITY_INVALID: completed scan changed its run-pinned mode or scope.",
            failure_class="SECURITY_SCAN_AUTHORITY_INVALID",
        )
    snapshot_digest = selected.get("targetSnapshotDigest")
    current_digest = selected.get("currentSnapshotDigest")
    if not isinstance(snapshot_digest, str) or not snapshot_digest:
        raise SecurityScanError(
            "SECURITY_SCAN_AUTHORITY_INVALID: completed scan has no target snapshot identity.",
            failure_class="SECURITY_SCAN_AUTHORITY_INVALID",
        )
    if selected.get("warnings"):
        raise SecurityScanError(
            "SECURITY_SCAN_AUTHORITY_INVALID: completed scan contains provider warnings.",
            failure_class="SECURITY_SCAN_AUTHORITY_INVALID",
        )
    snapshot_identity = hashlib.sha256(str(snapshot_digest).encode("utf-8")).hexdigest()
    current_snapshot_identity = (
        hashlib.sha256(current_digest.encode("utf-8")).hexdigest()
        if isinstance(current_digest, str) and current_digest
        else ""
    )
    completed_validation = authority.get("completed_validation")
    if completed_validation:
        if (
            not isinstance(completed_validation, Mapping)
            or completed_validation.get("scan_id") != scan_id
            or completed_validation.get("target_snapshot_identity") != snapshot_identity
            or completed_validation.get("current_snapshot_identity", snapshot_identity) != snapshot_identity
            or selected.get("warnings")
        ):
            raise SecurityScanError(
                "SECURITY_SCAN_AUTHORITY_INVALID: completed authoritative scan changed its validated snapshot identity or warnings.",
                failure_class="SECURITY_SCAN_AUTHORITY_INVALID",
            )
    if authority.get("authority_state") == "start_authorized_unclaimed":
        authority["ownership_state"] = "run_owned"
        authority["authority_state"] = "run_owned"
        authority["acquisition_checkpoint"] = checkpoint
        executor_phase = next(
            (item for item in reversed(phase_provenance) if item.get("role") == "executor"),
            {},
        )
        authority["acquisition_actor"] = str(executor_phase.get("actual_actor") or "")
        run_state.setdefault("security_scan_authority_history", []).append(
            {
                "checkpoint": checkpoint,
                "event": "run_owned",
                "generation": int(authority.get("generation", 1)),
                "selected_scan_id": scan_id,
                "selected_scan_mode": selected_mode,
                "selected_scan_scope": selected_scope,
                "acquisition_actor": authority["acquisition_actor"],
            }
        )
    if not isinstance(current_digest, str) or not current_digest or snapshot_digest != current_digest:
        authority["selected_scan_id"] = scan_id
        authority["selected_scan_mode"] = selected_mode
        authority["selected_scan_scope"] = selected_scope
        authority["authority_state"] = "completed_stale"
        run_state.setdefault("security_scan_authority_history", []).append(
            {
                "checkpoint": checkpoint,
                "event": "completed_stale",
                "generation": int(authority.get("generation", 1)),
                "selected_scan_id": scan_id,
                "failure_class": "SECURITY_SCAN_SNAPSHOT_STALE",
                "target_snapshot_identity": snapshot_identity,
                "current_snapshot_identity": current_snapshot_identity,
            }
        )
        run_state.setdefault("security_scan_provenance", []).append(evidence)
        phase = next((item for item in reversed(phase_provenance) if item.get("role") == "executor"), None)
        if phase is not None:
            phase["security_scan_provenance"] = evidence
        run_state["security_scan_authority"] = authority
        _persist_run_state(config, canonical_root, run_dir, phase_provenance, run_state)
        return
    if not completed_validation:
        authority["completed_validation"] = {
            "scan_id": scan_id,
            "target_snapshot_identity": snapshot_identity,
            "current_snapshot_identity": current_snapshot_identity,
            "validated_at_checkpoint": checkpoint,
        }
    authority["authority_state"] = "completed_fresh"
    run_state.setdefault("security_scan_authority_history", []).append(
        {
            "checkpoint": checkpoint,
            "event": "completed_fresh",
            "generation": int(authority.get("generation", 1)),
            "selected_scan_id": scan_id,
            "selected_status": "complete",
            "target_snapshot_identity": snapshot_identity,
            "current_snapshot_identity": current_snapshot_identity,
        }
    )
    authority["selected_scan_id"] = scan_id
    authority["selected_scan_mode"] = selected_mode
    authority["selected_scan_scope"] = selected_scope
    run_state.setdefault("security_scan_provenance", []).append(evidence)
    phase = next((item for item in reversed(phase_provenance) if item.get("role") == "executor"), None)
    if phase is not None:
        phase["security_scan_provenance"] = evidence
    _persist_run_state(config, canonical_root, run_dir, phase_provenance, run_state)


def _record_security_scan_result(**kwargs) -> None:
    try:
        _record_security_scan_result_impl(**kwargs)
    except SecurityScanError as exc:
        run_state = kwargs["run_state"]
        authority = run_state.get("security_scan_authority")
        if isinstance(authority, dict):
            if exc.failure_class in {"SECURITY_SCAN_CONFLICT", "SECURITY_SCAN_UNOWNED_ACTIVITY"}:
                authority["authority_state"] = "conflict"
            elif exc.failure_class == "SECURITY_SCAN_AUTHORITY_LOST":
                authority["authority_state"] = "lost"
            elif exc.failure_class == "SECURITY_SCAN_SNAPSHOT_STALE":
                authority["authority_state"] = "completed_stale"
            else:
                authority["authority_state"] = "failed"
            run_state.setdefault("security_scan_authority_history", []).append(
                {
                    "checkpoint": kwargs.get("checkpoint", "executor_postflight"),
                    "event": "authority_failure",
                    "generation": int(authority.get("generation", 1)),
                    "selected_scan_id": str(authority.get("selected_scan_id") or ""),
                    "failure_class": exc.failure_class,
                }
            )
            run_state["security_scan_authority"] = authority
        try:
            _persist_run_state(
                kwargs["config"],
                kwargs["canonical_root"],
                kwargs["run_dir"],
                kwargs["phase_provenance"],
                run_state,
            )
        except Exception:
            pass
        raise


def _run_security_scan_only_continuation(
    *,
    config: OrchestratorConfig,
    provider: CodexSecurityProvider,
    decision: ScanDecision,
    policy: str,
    requirement: tuple[bool, str, str],
    target_revision: str,
    scan_task: str,
    run_state: dict,
    canonical_root: Path,
    run_dir: Path,
    phase_provenance: list[dict],
    state_lock,
    progress,
    repository_trust_authorized: bool,
    checkpoint: str,
    generation: int,
    output_path: Path,
) -> None:
    control_paths = (run_dir / "run_state.json", run_dir / "provenance.json")
    initial_control_exclusions = {
        relative
        for path in control_paths
        if _safe_regular_control_file(path)
        and (relative := _relative_repository_path_lexical(path, config.repository)) is not None
    }
    try:
        mutation_baseline = capture_git_baseline(config.repository, include_ignored=True)
    except BaseException as exc:
        _record_security_scan_only_mutation(
            config=config,
            canonical_root=canonical_root,
            run_dir=run_dir,
            run_state=run_state,
            phase_provenance=phase_provenance,
            checkpoint=checkpoint,
            generation=generation,
            attribution=None,
            attribution_status="baseline_capture_failed",
            failure_class="SECURITY_SCAN_ONLY_MUTATION_UNKNOWN",
        )
        raise SecurityScanError(
            "SECURITY_SCAN_ONLY_MUTATION_UNKNOWN: repository state could not be captured before the scan-only Executor turn.",
            failure_class="SECURITY_SCAN_ONLY_MUTATION_UNKNOWN",
        ) from exc
    if not isinstance(mutation_baseline, Mapping) or mutation_baseline.get("complete") is not True:
        _record_security_scan_only_mutation(
            config=config,
            canonical_root=canonical_root,
            run_dir=run_dir,
            run_state=run_state,
            phase_provenance=phase_provenance,
            checkpoint=checkpoint,
            generation=generation,
            attribution=None,
            attribution_status="baseline_incomplete",
            failure_class="SECURITY_SCAN_ONLY_MUTATION_UNKNOWN",
        )
        raise SecurityScanError(
            "SECURITY_SCAN_ONLY_MUTATION_UNKNOWN: repository state was incomplete before the scan-only Executor turn.",
            failure_class="SECURITY_SCAN_ONLY_MUTATION_UNKNOWN",
        )
    dispatch_error = None
    try:
        _dispatch_phase(
            config=config,
            canonical_root=canonical_root,
            run_dir=run_dir,
            phase_provenance=phase_provenance,
            run_state=run_state,
            state_lock=state_lock,
            progress=progress,
            role="executor",
            repository_trust_authorized=repository_trust_authorized,
            task=_prompt(
                config,
                "security-rescan.txt",
                task=scan_task,
                security_scan_policy=_executor_security_gate_policy(policy),
            ),
            repository=config.repository,
            output_path=output_path,
            schema_path=_schema(config, "implementation.schema.json"),
        )
    except BaseException as exc:
        dispatch_error = exc

    exact_excluded_paths = {
        relative
        for path in control_paths
        if _safe_regular_control_file(path)
        and (relative := _relative_repository_path_lexical(path, config.repository)) is not None
    }
    exact_excluded_paths.intersection_update(initial_control_exclusions)
    try:
        mutation_attribution = attribute_git_mutations(
            config.repository,
            mutation_baseline,
            exact_excluded_paths=exact_excluded_paths,
            include_ignored=True,
        )
    except BaseException as exc:
        _record_security_scan_only_mutation(
            config=config,
            canonical_root=canonical_root,
            run_dir=run_dir,
            run_state=run_state,
            phase_provenance=phase_provenance,
            checkpoint=checkpoint,
            generation=generation,
            attribution=None,
            attribution_status="attribution_failed",
            failure_class="SECURITY_SCAN_ONLY_MUTATION_UNKNOWN",
        )
        raise SecurityScanError(
            "SECURITY_SCAN_ONLY_MUTATION_UNKNOWN: repository mutation attribution failed after the scan-only Executor turn.",
            failure_class="SECURITY_SCAN_ONLY_MUTATION_UNKNOWN",
        ) from exc
    if not isinstance(mutation_attribution, Mapping):
        _record_security_scan_only_mutation(
            config=config,
            canonical_root=canonical_root,
            run_dir=run_dir,
            run_state=run_state,
            phase_provenance=phase_provenance,
            checkpoint=checkpoint,
            generation=generation,
            attribution=None,
            attribution_status="attribution_invalid",
            failure_class="SECURITY_SCAN_ONLY_MUTATION_UNKNOWN",
        )
        raise SecurityScanError(
            "SECURITY_SCAN_ONLY_MUTATION_UNKNOWN: repository mutation attribution returned invalid state after the scan-only Executor turn.",
            failure_class="SECURITY_SCAN_ONLY_MUTATION_UNKNOWN",
        ) from dispatch_error
    attribution_failure = _security_scan_only_failure_class(mutation_attribution)
    _record_security_scan_only_mutation(
        config=config,
        canonical_root=canonical_root,
        run_dir=run_dir,
        run_state=run_state,
        phase_provenance=phase_provenance,
        checkpoint=checkpoint,
        generation=generation,
        attribution=mutation_attribution,
        failure_class=attribution_failure,
    )
    if attribution_failure:
        raise SecurityScanError(
            f"{attribution_failure}: scan-only Executor turn did not establish a mutation-free repository state.",
            failure_class=attribution_failure,
        ) from dispatch_error
    if dispatch_error is not None:
        raise dispatch_error
    implementation = _load_security_scan_only_output(output_path)
    _record_security_scan_result(
        provider=provider,
        decision=decision,
        checkpoint=checkpoint,
        implementation=implementation,
        run_state=run_state,
        phase_provenance=phase_provenance,
        config=config,
        canonical_root=canonical_root,
        run_dir=run_dir,
    )


def _ensure_security_gate_fresh(
    *,
    config: OrchestratorConfig,
    requirement: tuple[bool, str, str],
    target_revision: str,
    run_state: dict,
    canonical_root: Path,
    run_dir: Path,
    phase_provenance: list[dict],
    state_lock,
    progress=None,
    repository_trust_authorized: bool = False,
    checkpoint: str,
) -> bool:
    """Ensure completed, current Security coverage before or after Reviewer work.

    Returns whether a scan-only Executor continuation ran during this check.
    """

    if not requirement[0]:
        return False
    dispatched = False
    max_generations = _max_security_generations(config)
    # The extra bounded pass re-reads the ledger after the final allowed scan.
    for attempt in range(max_generations + 1):
        generation = int(
            (run_state.get("security_scan_authority") or {}).get("generation", 1)
        )
        check_name = f"{checkpoint}_{attempt + 1}"
        provider, decision, policy = _prepare_security_scan(
            config=config,
            requirement=requirement,
            target_revision=target_revision,
            run_state=run_state,
            canonical_root=canonical_root,
            run_dir=run_dir,
            phase_provenance=phase_provenance,
            checkpoint=check_name,
            authorize_executor_dispatch=attempt < max_generations,
        )
        if provider is None or decision is None:
            return dispatched
        if decision.action == "reused":
            return dispatched
        if decision.action not in {"start", "awaited"}:
            raise SecurityScanError(
                "SECURITY_SCAN_AUTHORITY_INVALID: final Reviewer gate has no completed fresh Security scan.",
                failure_class="SECURITY_SCAN_AUTHORITY_INVALID",
            )
        if attempt >= max_generations:
            raise SecurityScanError(
                f"SECURITY_SCAN_RESCAN_LIMIT: no fresh Security coverage was established after {max_generations} bounded scan generations.",
                failure_class="SECURITY_SCAN_RESCAN_LIMIT",
            )
        scan_task = (
            f"Complete one host-authorized Codex Security {requirement[1]} scan for scope "
            f"{requirement[2]!r} at revision {target_revision}. This is a scan-only continuation; "
            "do not modify repository source or create any additional scan."
        )
        with tempfile.TemporaryDirectory(prefix="dual-codex-security-rescan-") as output_directory:
            output_path = Path(output_directory) / "executor-report.json"
            if _relative_repository_path_lexical(output_path, config.repository) is not None:
                raise SecurityScanError(
                    "SECURITY_SCAN_ONLY_OUTPUT_INVALID: scan-only Executor output must be outside the repository workspace.",
                    failure_class="SECURITY_SCAN_ONLY_OUTPUT_INVALID",
                )
            _run_security_scan_only_continuation(
                config=config,
                provider=provider,
                decision=decision,
                policy=policy,
                requirement=requirement,
                target_revision=target_revision,
                scan_task=scan_task,
                run_state=run_state,
                canonical_root=canonical_root,
                run_dir=run_dir,
                phase_provenance=phase_provenance,
                state_lock=state_lock,
                progress=progress,
                repository_trust_authorized=repository_trust_authorized,
                checkpoint=check_name,
                generation=generation,
                output_path=output_path,
            )
        dispatched = True
    raise SecurityScanError(
        f"SECURITY_SCAN_RESCAN_LIMIT: no fresh Security coverage was established after {max_generations} bounded scan generations.",
        failure_class="SECURITY_SCAN_RESCAN_LIMIT",
    )


def _security_scan_only_failure_class(attribution: Mapping[str, object]) -> str | None:
    if any(
        attribution.get(field)
        for field in (
            "run_touched_paths",
            "run_created_paths",
            "run_removed_paths",
            "head_changed",
            "branch_changed",
            "staged_state_changed",
            "repository_metadata_changed",
        )
    ):
        return "SECURITY_SCAN_ONLY_MUTATION"
    if attribution.get("status") != "complete" or attribution.get("unknown_paths"):
        return "SECURITY_SCAN_ONLY_MUTATION_UNKNOWN"
    return None


def _security_scan_only_mutation_record(
    *,
    checkpoint: str,
    generation: int,
    attribution: Mapping[str, object] | None,
    attribution_status: str | None,
    failure_class: str | None,
) -> dict:
    attribution = attribution if isinstance(attribution, Mapping) else {}
    path_fields = {
        "modified": "run_touched_paths",
        "added": "run_created_paths",
        "removed": "run_removed_paths",
        "unknown": "unknown_paths",
    }
    mutated_paths: dict[str, list[str]] = {}
    paths_truncated = False
    for category, field in path_fields.items():
        raw_paths = attribution.get(field, [])
        raw_paths = raw_paths if isinstance(raw_paths, (list, tuple)) else []
        safe_paths = []
        for raw_path in raw_paths:
            if not isinstance(raw_path, str):
                continue
            path = raw_path.replace("\\", "/")
            if path.startswith("/") or re.match(r"^[A-Za-z]:/", path) or ".." in PurePosixPath(path).parts:
                paths_truncated = True
                continue
            if len(path) > 240:
                path = path[:240]
                paths_truncated = True
            if len(safe_paths) < 20:
                safe_paths.append(path)
            else:
                paths_truncated = True
        mutated_paths[category] = safe_paths
    mutation_categories = []
    category_fields = (
        ("existing_path_changed", "run_touched_paths"),
        ("path_added", "run_created_paths"),
        ("path_removed", "run_removed_paths"),
        ("head_changed", "head_changed"),
        ("branch_changed", "branch_changed"),
        ("index_changed", "staged_state_changed"),
        ("git_metadata_changed", "repository_metadata_changed"),
        ("unknown_path", "unknown_paths"),
    )
    for category, field in category_fields:
        if attribution.get(field):
            mutation_categories.append(category)
    if attribution.get("status") != "complete":
        mutation_categories.append("attribution_unknown")
    if attribution_status in {"baseline_capture_failed", "baseline_incomplete", "attribution_failed"}:
        mutation_categories.append("repository_state_unknown")
    return {
        "checkpoint": str(checkpoint)[:120],
        "generation": int(generation),
        "attribution_status": str(attribution_status or attribution.get("status") or "unknown")[:40],
        "mutation_categories": mutation_categories,
        "mutated_paths": mutated_paths,
        "paths_truncated": paths_truncated,
        "observed_at": str(attribution.get("observed_at") or "")[:40],
        "failure_class": failure_class,
    }


def _record_security_scan_only_mutation(
    *,
    config: OrchestratorConfig,
    canonical_root: Path,
    run_dir: Path,
    run_state: dict,
    phase_provenance: list[dict],
    checkpoint: str,
    generation: int,
    attribution: Mapping[str, object] | None,
    attribution_status: str | None = None,
    failure_class: str | None,
) -> None:
    record = _security_scan_only_mutation_record(
        checkpoint=checkpoint,
        generation=generation,
        attribution=attribution,
        attribution_status=attribution_status,
        failure_class=failure_class,
    )
    run_state.setdefault("security_scan_only_mutation_history", []).append(record)
    if failure_class:
        authority = run_state.get("security_scan_authority")
        if isinstance(authority, dict):
            authority["authority_state"] = "failed"
            authority["failure_class"] = failure_class
        run_state.setdefault("security_scan_authority_history", []).append(
            {
                "checkpoint": str(checkpoint)[:120],
                "event": "scan_only_mutation_rejected",
                "generation": int(generation),
                "failure_class": failure_class,
            }
        )
        checkpoint_token = re.sub(r"[^A-Za-z0-9_-]", "_", str(checkpoint))[:80]
        sidecar = run_dir / f"security-scan-only-mutation-{int(generation)}-{checkpoint_token}.json"
        try:
            _safe_atomic_write_control_json(sidecar, record)
        except OSError as exc:
            raise SecurityScanError(
                f"{failure_class}: scan-only mutation evidence could not be persisted safely.",
                failure_class=failure_class,
            ) from exc
    try:
        _persist_run_state(config, canonical_root, run_dir, phase_provenance, run_state, required=True)
    except Exception as exc:
        if not failure_class:
            if isinstance(run_state.get("security_scan_authority"), dict):
                run_state["security_scan_authority"]["authority_state"] = "failed"
                run_state["security_scan_authority"]["failure_class"] = "SECURITY_SCAN_ONLY_MUTATION_UNKNOWN"
            record["failure_class"] = "SECURITY_SCAN_ONLY_MUTATION_UNKNOWN"
            run_state.setdefault("security_scan_authority_history", []).append(
                {
                    "checkpoint": str(checkpoint)[:120],
                    "event": "scan_only_attribution_persistence_failed",
                    "generation": int(generation),
                    "failure_class": "SECURITY_SCAN_ONLY_MUTATION_UNKNOWN",
                }
            )
            checkpoint_token = re.sub(r"[^A-Za-z0-9_-]", "_", str(checkpoint))[:80]
            sidecar = run_dir / f"security-scan-only-mutation-{int(generation)}-{checkpoint_token}.json"
            try:
                _safe_atomic_write_control_json(sidecar, record)
            except OSError:
                pass
        raise SecurityScanError(
            "SECURITY_SCAN_ONLY_MUTATION_UNKNOWN: scan-only mutation evidence could not be persisted safely.",
            failure_class=failure_class or "SECURITY_SCAN_ONLY_MUTATION_UNKNOWN",
        ) from exc


def _failure_reason(exc: BaseException) -> str:
    detail = " ".join(str(exc).split())
    if detail:
        return f"{type(exc).__name__}: {detail[:240]}"
    return type(exc).__name__


def _run_result_record(run_dir: Path, run_state: dict) -> dict:
    def evidence_path(name: str) -> str | None:
        path = run_dir / name
        try:
            return str(path.resolve(strict=False)) if path.is_file() else None
        except OSError:
            return None

    failure = run_state.get("failure")
    failure = failure if isinstance(failure, dict) else {}
    attribution = run_state.get("mutation_attribution")
    attribution = attribution if isinstance(attribution, dict) else {}
    cleanup = run_state.get("bootstrap_cleanup")
    if isinstance(cleanup, dict):
        removed_items = cleanup.get("removed", [])
        retained_items = cleanup.get("retained", [])
        if not isinstance(removed_items, list):
            removed_items = []
        if not isinstance(retained_items, list):
            retained_items = []
        allowed_name = re.compile(
            rf"^\.canonical-bootstrap-(?:{'|'.join(re.escape(role) for role in SUPPORTED_ROLES)})-[A-Za-z0-9_]{{8}}\.md$"
        )
        removed = []
        for item in removed_items[:50]:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            run_id = item.get("run_id")
            record = {}
            if isinstance(name, str) and allowed_name.fullmatch(name):
                record["name"] = name
            if isinstance(run_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", run_id):
                record["run_id"] = run_id
            if record:
                removed.append(record)
        cleanup_result = {
            "removed": removed,
            "retained_count": len(retained_items),
        }
    else:
        cleanup_result = None
    status = run_state.get("status")
    if status not in ("completed", "failed", "blocked", "interrupted"):
        status = "failed"
    try:
        run_directory = str(run_dir.resolve(strict=False))
    except OSError:
        run_directory = str(run_dir)
    return {
        "status": status,
        "run_directory": run_directory,
        "run_state_path": evidence_path("run_state.json"),
        "provenance_path": evidence_path("provenance.json"),
        "initial_git_baseline_path": evidence_path("initial_git_baseline.json"),
        "mutation_attribution_path": evidence_path("mutation-attribution.json"),
        "last_phase": run_state.get("last_phase"),
        "last_actor": run_state.get("last_actor"),
        "last_backend": run_state.get("last_backend"),
        "provider_status": run_state.get("provider_status"),
        "failure_type": failure.get("failure_type"),
        "failure_class": failure.get("failure_class"),
        "app_server_turn_provenance": failure.get("app_server_turn_provenance"),
        "verdict": run_state.get("verdict") if run_state.get("verdict") in ("approved", "changes_requested") else None,
        "correction_cycles": run_state.get("correction_cycles", 0),
        "mutation_attribution_status": attribution.get("status"),
        "mutation_attribution_reason": attribution.get("reason"),
        "bootstrap_cleanup": cleanup_result,
    }


def _attach_run_result(exc: BaseException, run_dir: Path, run_state: dict) -> None:
    setattr(exc, "dual_codex_run_result", _run_result_record(run_dir, run_state))


def _run_owner_process_start() -> str | None:
    try:
        from .delegation import _safe_process_start_token

        return _safe_process_start_token(os.getpid())
    except Exception:
        return None


def _relative_control_path(path: Path, repository: Path) -> str | None:
    try:
        return path.resolve(strict=False).relative_to(repository).as_posix()
    except (OSError, ValueError):
        return None


def _relative_repository_path_lexical(path: Path, repository: Path) -> str | None:
    """Return a repository-relative path without following an untrusted symlink."""

    try:
        absolute_path = Path(os.path.abspath(path))
        absolute_repository = Path(os.path.abspath(repository))
        return absolute_path.relative_to(absolute_repository).as_posix()
    except (OSError, ValueError):
        return None


def _recover_interrupted_runs(
    config: OrchestratorConfig,
    repository: Path,
    *,
    current_run_id: str,
    bootstrap_cleanup: dict[str, list[dict[str, str]]],
) -> None:
    """Finalize prior run records left running after their owner process ended."""

    runs_dir = config.runs_dir.expanduser().resolve()
    if not runs_dir.is_dir():
        return
    try:
        from .delegation import _pid_alive, _safe_process_start_token
    except Exception:
        _pid_alive = lambda _pid: True
        _safe_process_start_token = lambda _pid: None

    for item in runs_dir.iterdir():
        try:
            directory_info = item.lstat()
            if not stat.S_ISDIR(directory_info.st_mode) or stat.S_ISLNK(directory_info.st_mode):
                continue
            state_path = item / "run_state.json"
            state_info = state_path.lstat()
            if not stat.S_ISREG(state_info.st_mode) or stat.S_ISLNK(state_info.st_mode):
                continue
            run_state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
            continue
        if (
            not isinstance(run_state, dict)
            or run_state.get("status") != "running"
            or run_state.get("repository") != str(repository)
            or run_state.get("run_id") == current_run_id
        ):
            continue
        try:
            owner_pid = int(run_state.get("pid", 0))
        except (ValueError, TypeError):
            owner_pid = 0
        stored_start = run_state.get("process_start")
        if owner_pid == os.getpid():
            owner_active = False
        else:
            try:
                owner_alive = _pid_alive(owner_pid)
                live_start = _safe_process_start_token(owner_pid) if owner_alive else None
                owner_active = owner_alive and (
                    not isinstance(stored_start, str)
                    or not stored_start
                    or live_start is None
                    or live_start == stored_start
                )
            except Exception:
                owner_active = True
        if owner_active:
            continue

        baseline_path = item / "initial_git_baseline.json"
        try:
            baseline_info = baseline_path.lstat()
            if not stat.S_ISREG(baseline_info.st_mode) or stat.S_ISLNK(baseline_info.st_mode):
                raise OSError("unsafe baseline file")
            baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
            run_id = str(run_state.get("run_id", ""))
            if not isinstance(baseline, dict) or baseline.get("repository") != str(repository):
                raise ValueError("baseline repository mismatch")
            excluded = _relative_control_path(item, repository)
            excluded_paths = [excluded] if excluded else []
            excluded_paths.extend(canonical_bootstrap_control_paths(repository))
            mutation = attribute_git_mutations(
                repository,
                baseline,
                excluded_paths=excluded_paths,
            )
            removed_artifacts = [
                {"name": record["name"], "state": "reconciled_orphan"}
                for record in bootstrap_cleanup.get("removed", [])
                if record.get("run_id") == run_id or record.get("run_id") == "legacy"
            ]
            run_state["mutation_attribution"] = mutation
            run_state["dual_agents_bootstrap_artifacts"] = [
                *removed_artifacts,
                *[
                    {"name": name, "state": "retained_ambiguous"}
                    for name in canonical_bootstrap_artifacts(repository)
                ],
            ]
            run_state["status"] = "interrupted"
            run_state["provider_status"] = "unknown_after_interruption"
            run_state["failure"] = {
                "failure_type": "ProcessInterrupted",
                "observed_at": datetime.now(timezone.utc).isoformat(),
            }
            mutation_path = item / "mutation-attribution.json"
            _safe_atomic_write_control_json(mutation_path, mutation)
        except Exception as exc:
            run_state["status"] = "interrupted"
            run_state["provider_status"] = "unknown_after_interruption"
            run_state["mutation_attribution"] = {
                "status": "unknown",
                "reason": type(exc).__name__,
            }
            run_state["failure"] = {
                "failure_type": "ProcessInterrupted",
                "evidence_error": type(exc).__name__,
                "observed_at": datetime.now(timezone.utc).isoformat(),
            }
        interrupted_phase = run_state.get("current_phase")
        run_state["last_phase"] = interrupted_phase or run_state.get("last_phase")
        if interrupted_phase:
            run_state["last_actor"] = run_state.get("current_actor") or run_state.get("last_actor")
            run_state["last_backend"] = run_state.get("current_backend") or run_state.get("last_backend")
        run_state["current_phase"] = None
        run_state["current_actor"] = None
        run_state["current_backend"] = None
        run_state["finalized_by_run_id"] = current_run_id
        try:
            atomic_write_json(state_path, run_state)
            provenance_path = item / "provenance.json"
            if provenance_path.is_file() and not provenance_path.is_symlink():
                provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
                if isinstance(provenance, dict):
                    phases = provenance.get("configured_actor_routing")
                    if isinstance(phases, list) and phases and isinstance(phases[-1], dict):
                        if phases[-1].get("phase_state") in {"started", "running"}:
                            phases[-1]["phase_state"] = "interrupted"
                            phases[-1]["provider_status"] = "unknown_after_interruption"
                    provenance.update(
                        {
                            "status": "interrupted",
                            "current_phase": None,
                            "last_phase": run_state.get("last_phase"),
                            "current_actor": None,
                            "current_backend": None,
                            "last_actor": run_state.get("last_actor"),
                            "last_backend": run_state.get("last_backend"),
                            "provider_status": "unknown_after_interruption",
                            "mutation_attribution": run_state.get("mutation_attribution"),
                            "dual_agents_bootstrap_artifacts": run_state.get("dual_agents_bootstrap_artifacts", []),
                            "failure": run_state.get("failure"),
                        }
                    )
                    atomic_write_json(provenance_path, provenance)
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass


def _dispatch_phase(
    *,
    config: OrchestratorConfig,
    canonical_root: Path,
    run_dir: Path,
    phase_provenance: list[dict],
    run_state: dict,
    state_lock,
    progress=None,
    role: str,
    repository_trust_authorized: bool = False,
    **kwargs,
):
    started_at = datetime.now(timezone.utc).isoformat()
    actor = None
    entry: dict = {
        "phase": role,
        "role": role,
        "phase_state": "started",
        "started_at": started_at,
        "provider_status": "starting",
        "last_progress_at": None,
    }
    try:
        actor = config.agent_for_role(role)
        entry.update(
            {
                "configured_actor": actor.account_name,
                "actor_id": actor.account_name,
                "provider": actor.provider_type,
                "backend": actor.backend,
                "fallback_enabled": bool(getattr(config, "fallback_enabled", False)),
                "fallback_used": False,
                "repository": str(config.repository),
            }
        )
        try:
            entry.update(
                configured_actor_provenance(
                    agent=actor,
                    role=role,
                    repository=config.repository,
                    canonical_root=canonical_root,
                )
            )
        except Exception:
            pass
        with state_lock:
            phase_provenance.append(entry)
            run_state["current_phase"] = role
            run_state["last_phase"] = role
            run_state["current_actor"] = actor.account_name
            run_state["current_backend"] = actor.backend
            run_state["last_actor"] = actor.account_name
            run_state["last_backend"] = actor.backend
            run_state["provider_status"] = "starting"
            _persist_run_state(config, canonical_root, run_dir, phase_provenance, run_state, required=True)
        _progress_event(
            progress,
            phase=role,
            actor=actor.account_name,
            backend=actor.backend,
            state="started",
        )

        def backend_progress(detail: str) -> None:
            safe_detail = _safe_backend_detail(detail)
            timestamp = datetime.now(timezone.utc).isoformat()
            with state_lock:
                entry["phase_state"] = "running"
                entry["provider_status"] = "alive"
                entry["last_progress_at"] = timestamp
                run_state["provider_status"] = "alive"
                run_state["last_progress_at"] = timestamp
                _persist_run_state(config, canonical_root, run_dir, phase_provenance, run_state)
            event = {
                "phase": role,
                "actor": actor.account_name,
                "backend": actor.backend,
                "state": "running",
            }
            if safe_detail is not None:
                event["detail"] = safe_detail
            _progress_event(progress, **event)

        def app_server_dispatch_started(details: dict) -> None:
            safe_details = {
                key: details.get(key)
                for key in (
                    "turn_timeout_seconds",
                    "timeout_source",
                    "runtime_config_identity",
                    "process_reuse_state",
                    "reused_process_runtime_identity_matched",
                )
            }
            with state_lock:
                entry["app_server_dispatch_provenance"] = safe_details
                run_state.setdefault("app_server_dispatch_provenance", []).append(
                    {"phase": role, **safe_details}
                )
                _persist_run_state(config, canonical_root, run_dir, phase_provenance, run_state, required=True)

        result = delegate_to_configured_actor(
            config=config,
            role=role,
            repository_trust_authorized=repository_trust_authorized,
            run_id=run_state["run_id"],
            progress=backend_progress,
            dispatch_started=app_server_dispatch_started,
            **kwargs,
        )
    except Exception as exc:
        if entry not in phase_provenance:
            phase_provenance.append(entry)
        failure_state = _phase_failure_state(exc)
        entry.update(getattr(exc, "metadata", {}))
        entry.update(
            {
                "phase_state": failure_state,
                "provider_status": "failed",
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "dispatch_failed": True,
                "failure_type": type(exc).__name__,
            }
        )
        if actor is None:
            entry["configured_actor"] = False
            entry["routing_error"] = type(exc).__name__
        if getattr(exc, "failure_class", None):
            entry["failure_class"] = str(exc.failure_class)
        with state_lock:
            app_server_turn = entry.get("app_server_turn_provenance")
            if isinstance(app_server_turn, dict):
                run_state.setdefault("app_server_turn_provenance", []).append(dict(app_server_turn))
            run_state["current_phase"] = None
            run_state["current_actor"] = None
            run_state["current_backend"] = None
            run_state["last_phase"] = role
            if actor is not None:
                run_state["last_actor"] = actor.account_name
                run_state["last_backend"] = actor.backend
            run_state["provider_status"] = "failed"
            _persist_run_state(config, canonical_root, run_dir, phase_provenance, run_state)
        _progress_event(
            progress,
            phase=role,
            actor=actor.account_name if actor is not None else "unknown",
            backend=actor.backend if actor is not None else "unknown",
            state=failure_state,
            failure_type=type(exc).__name__,
        )
        raise
    entry.update(dict(result.metadata))
    completed_at = datetime.now(timezone.utc).isoformat()
    entry.update(
        {
            "phase": role,
            "role": role,
            "phase_state": "completed",
            "provider_status": "completed",
            "completed_at": completed_at,
            "dispatch_failed": False,
        }
    )
    with state_lock:
        app_server_turn = entry.get("app_server_turn_provenance")
        if isinstance(app_server_turn, dict):
            run_state.setdefault("app_server_turn_provenance", []).append(dict(app_server_turn))
        run_state["current_phase"] = None
        run_state["current_actor"] = None
        run_state["current_backend"] = None
        run_state["last_phase"] = role
        run_state["last_actor"] = actor.account_name
        run_state["last_backend"] = actor.backend
        run_state["provider_status"] = "completed"
        _persist_run_state(config, canonical_root, run_dir, phase_provenance, run_state)
    _progress_event(
        progress,
        phase=role,
        actor=actor.account_name,
        backend=actor.backend,
        state="completed",
    )
    return result


def execute(
    config: OrchestratorConfig,
    task_file: Path,
    *,
    explicit_repository: bool = False,
    progress=None,
) -> RunOutcome:
    try:
        target_root = git_top_level(config.repository)
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError(f"run requires a valid Git repository root: {config.repository}") from exc
    if not same_path(target_root, config.repository):
        raise RuntimeError(
            f"run repository must be the exact Git top-level '{target_root}', not '{config.repository}'."
        )
    config = replace(config, repository=target_root)
    run_id = uuid4().hex
    lock = RepositoryLock(config.runs_dir, config.repository, "run-" + run_id, run_id)
    with lock:
        return _execute_locked(
            config,
            task_file,
            repository_trust_authorized=explicit_repository,
            repository_lock=lock,
            run_id=run_id,
            progress=progress,
        )


def _execute_locked(
    config: OrchestratorConfig,
    task_file: Path,
    *,
    repository_trust_authorized: bool = False,
    repository_lock: RepositoryLock,
    run_id: str,
    progress=None,
) -> RunOutcome:
    task_file = task_file.expanduser().resolve()
    task = _read(task_file).strip()
    if not task:
        raise ValueError("Task file is empty")

    canonical_root = canonical_instructions_root()
    ensure_git_repository(config.repository)
    from .terminal import reconcile_deferred_task_artifact_cleanup

    reconcile_deferred_task_artifact_cleanup(config, config.repository)
    configured_backends = {
        role: config.accounts[account_name].backend
        for role, account_name in config.roles.items()
        if account_name in config.accounts
    }
    bootstrap_cleanup = reconcile_orphan_canonical_bootstrap(
        config.repository,
        repository_lock=repository_lock,
        run_id=run_id,
        configured_backends=configured_backends,
        canonical_root=canonical_root,
    )
    _recover_interrupted_runs(
        config,
        config.repository,
        current_run_id=run_id,
        bootstrap_cleanup=bootstrap_cleanup,
    )
    baseline = capture_git_baseline(config.repository)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = config.runs_dir / f"{timestamp}-{run_id[:8]}"
    run_dir.mkdir(parents=True, exist_ok=False)
    run_dir = run_dir.resolve(strict=True)
    baseline_path = run_dir / "initial_git_baseline.json"
    phase_provenance: list[dict] = []
    state_lock = threading.RLock()
    correction_cycles = 0
    initial_bootstrap_artifacts: list[str] = []
    initial_bootstrap_control_paths: list[str] = []
    run_state: dict = {
        "schema_version": 1,
        "run_id": run_id,
        "repository": str(config.repository),
        "pid": os.getpid(),
        "process_start": _run_owner_process_start(),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "status": "running",
        "current_phase": None,
        "last_phase": None,
        "current_actor": None,
        "current_backend": None,
        "last_actor": None,
        "last_backend": None,
        "provider_status": "not_started",
        "last_progress_at": None,
        "app_server_turn_provenance": [],
        "app_server_dispatch_provenance": [],
        "security_scan_arbitrations": [],
        "security_scan_authority": None,
        "security_scan_authority_history": [],
        "security_scan_provenance": [],
        "task_sha256": hashlib.sha256(task.encode("utf-8")).hexdigest(),
        "initial_git_baseline": {
            "path": baseline_path.name,
            "head": baseline.get("head"),
            "branch": baseline.get("branch"),
            "detached": baseline.get("detached"),
            "captured_at": baseline.get("captured_at"),
        },
        "mutation_attribution": {"status": "pending", "path": "mutation-attribution.json"},
        "bootstrap_cleanup": bootstrap_cleanup,
        "dual_agents_bootstrap_artifacts": initial_bootstrap_artifacts,
        "correction_cycles": correction_cycles,
        "verdict": None,
        "failure": None,
    }
    try:
        initial_bootstrap_artifacts = canonical_bootstrap_artifacts(config.repository)
        initial_bootstrap_control_paths = canonical_bootstrap_control_paths(config.repository)
        run_state["dual_agents_bootstrap_artifacts"] = initial_bootstrap_artifacts
        _atomic_write_text(run_dir / "task.md", task + "\n")
        atomic_write_json(baseline_path, baseline)
        run_state["initial_git_baseline"]["sha256"] = hashlib.sha256(baseline_path.read_bytes()).hexdigest()
        _persist_run_state(config, canonical_root, run_dir, phase_provenance, run_state, required=True)
    except BaseException as exc:
        run_state["status"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
        run_state["provider_status"] = "unknown_after_interruption" if isinstance(exc, KeyboardInterrupt) else "failed"
        run_state["failure"] = {
            "failure_type": type(exc).__name__,
            "failure_class": str(getattr(exc, "failure_class", "")),
            "failed_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            _persist_run_state(config, canonical_root, run_dir, phase_provenance, run_state)
        except Exception:
            pass
        _attach_run_result(exc, run_dir, run_state)
        raise

    def mutation_summary() -> dict:
        final_bootstrap_control_paths = canonical_bootstrap_control_paths(config.repository)
        excluded = []
        run_relative = _relative_control_path(run_dir, config.repository)
        if run_relative:
            excluded.append(run_relative)
        excluded.extend(set(initial_bootstrap_control_paths) | set(final_bootstrap_control_paths))
        result = attribute_git_mutations(config.repository, baseline, excluded_paths=excluded)
        result["dual_agents_ephemeral_artifacts"] = {
            "initial": initial_bootstrap_control_paths,
            "final": final_bootstrap_control_paths,
            "reconciled_before_baseline": bootstrap_cleanup.get("removed", []),
        }
        return result

    try:
        if config.require_clean_git and baseline.get("status_entries"):
            run_state["status"] = "blocked"
            run_state["failure"] = {"failure_type": "DirtyRepository"}
            run_state["mutation_attribution"] = mutation_summary()
            _safe_atomic_write_control_json(run_dir / "mutation-attribution.json", run_state["mutation_attribution"])
            _persist_run_state(config, canonical_root, run_dir, phase_provenance, run_state)
            _progress_event(progress, phase="run", actor="", backend="", state="failed", failure_type="DirtyRepository")
            raise RuntimeError(
                "Repository has uncommitted changes. Commit/stash them or set "
                "require_clean_git = false explicitly."
            )

        plan_path = run_dir / "plan.json"
        security_requirement = _security_scan_request(task)
        run_state["mission_security_requirement"] = {
            "required": security_requirement[0],
            "mode": security_requirement[1],
            "scope": security_requirement[2],
            "source": "original_task",
        }
        _persist_run_state(config, canonical_root, run_dir, phase_provenance, run_state)
        security_provider, scan_decision, _ = _prepare_security_scan(
            config=config,
            requirement=security_requirement,
            target_revision=str(baseline.get("head") or ""),
            run_state=run_state,
            canonical_root=canonical_root,
            run_dir=run_dir,
            phase_provenance=phase_provenance,
            checkpoint="before_architect",
        )
        architect_gate_policy = _architect_security_gate_policy(security_requirement, scan_decision)
        _dispatch_phase(
            config=config,
            canonical_root=canonical_root,
            run_dir=run_dir,
            phase_provenance=phase_provenance,
            run_state=run_state,
            state_lock=state_lock,
            progress=progress,
            role="architect",
            repository_trust_authorized=repository_trust_authorized,
            task=_prompt(
                config,
                "architect.txt",
                task=task,
                security_gate_policy=architect_gate_policy,
            ),
            repository=config.repository,
            output_path=plan_path,
            schema_path=_schema(config, "architect-plan.schema.json"),
        )
        plan = load_json(plan_path)

        implementation_path = run_dir / "implementation.json"
        security_provider, scan_decision, scan_policy = _prepare_security_scan(
            config=config,
            requirement=security_requirement,
            target_revision=str(baseline.get("head") or ""),
            run_state=run_state,
            canonical_root=canonical_root,
            run_dir=run_dir,
            phase_provenance=phase_provenance,
            checkpoint="before_executor",
            authorize_executor_dispatch=True,
        )
        scan_policy = _executor_security_gate_policy(scan_policy)
        _dispatch_phase(
            config=config,
            canonical_root=canonical_root,
            run_dir=run_dir,
            phase_provenance=phase_provenance,
            run_state=run_state,
            state_lock=state_lock,
            progress=progress,
            role="executor",
            repository_trust_authorized=repository_trust_authorized,
            task=_prompt(
                config,
                "executor.txt",
                task=task,
                plan=dump_json(plan),
                security_scan_policy=scan_policy,
            ),
            repository=config.repository,
            output_path=implementation_path,
            schema_path=_schema(config, "implementation.schema.json"),
        )
        implementation = load_json(implementation_path)
        _record_security_scan_result(
            provider=security_provider,
            decision=scan_decision,
            checkpoint="before_executor",
            implementation=implementation,
            run_state=run_state,
            phase_provenance=phase_provenance,
            config=config,
            canonical_root=canonical_root,
            run_dir=run_dir,
        )

        review_attempt = 0
        while True:
            _ensure_security_gate_fresh(
                config=config,
                requirement=security_requirement,
                target_revision=str(baseline.get("head") or ""),
                run_state=run_state,
                canonical_root=canonical_root,
                run_dir=run_dir,
                phase_provenance=phase_provenance,
                state_lock=state_lock,
                progress=progress,
                repository_trust_authorized=repository_trust_authorized,
                checkpoint=f"before_reviewer_{review_attempt}",
            )
            diff_text = status_and_diff(config.repository)
            (run_dir / f"diff-{correction_cycles}-{review_attempt}.md").write_text(diff_text, encoding="utf-8")
            review_path = run_dir / f"review-{correction_cycles}-{review_attempt}.json"
            _dispatch_phase(
                config=config,
                canonical_root=canonical_root,
                run_dir=run_dir,
                phase_provenance=phase_provenance,
                run_state=run_state,
                state_lock=state_lock,
                progress=progress,
                role="reviewer",
                repository_trust_authorized=repository_trust_authorized,
                task=_prompt(
                    config,
                    "reviewer.txt",
                    task=task,
                    plan=dump_json(plan),
                    implementation=dump_json(implementation),
                    diff=diff_text,
                    phase_provenance=dump_json(_reviewer_phase_context(config, phase_provenance, run_state)),
                ),
                repository=config.repository,
                output_path=review_path,
                schema_path=_schema(config, "review.schema.json"),
            )
            review = load_json(review_path)
            rescanned_after_review = _ensure_security_gate_fresh(
                config=config,
                requirement=security_requirement,
                target_revision=str(baseline.get("head") or ""),
                run_state=run_state,
                canonical_root=canonical_root,
                run_dir=run_dir,
                phase_provenance=phase_provenance,
                state_lock=state_lock,
                progress=progress,
                repository_trust_authorized=repository_trust_authorized,
                checkpoint=f"after_reviewer_{review_attempt}",
            )
            if rescanned_after_review:
                review_attempt += 1
                continue
            if review["verdict"] == "approved":
                break
            if correction_cycles >= config.max_correction_cycles:
                break

            correction_cycles += 1
            run_state["correction_cycles"] = correction_cycles
            implementation_path = run_dir / f"correction-{correction_cycles}.json"
            security_provider, scan_decision, scan_policy = _prepare_security_scan(
                config=config,
                requirement=security_requirement,
                target_revision=str(baseline.get("head") or ""),
                run_state=run_state,
                canonical_root=canonical_root,
                run_dir=run_dir,
                phase_provenance=phase_provenance,
                checkpoint=f"before_correction_{correction_cycles}",
                authorize_executor_dispatch=True,
            )
            scan_policy = _executor_security_gate_policy(scan_policy)
            _dispatch_phase(
                config=config,
                canonical_root=canonical_root,
                run_dir=run_dir,
                phase_provenance=phase_provenance,
                run_state=run_state,
                state_lock=state_lock,
                progress=progress,
                role="executor",
                repository_trust_authorized=repository_trust_authorized,
                task=_prompt(
                    config,
                    "correction.txt",
                    task=task,
                    plan=dump_json(plan),
                    review=dump_json(review),
                    security_scan_policy=scan_policy,
                ),
                repository=config.repository,
                output_path=implementation_path,
                schema_path=_schema(config, "implementation.schema.json"),
            )
            implementation = load_json(implementation_path)
            _record_security_scan_result(
                provider=security_provider,
                decision=scan_decision,
                checkpoint=f"before_correction_{correction_cycles}",
                implementation=implementation,
                run_state=run_state,
                phase_provenance=phase_provenance,
                config=config,
                canonical_root=canonical_root,
                run_dir=run_dir,
            )
            review_attempt += 1

        mutation = mutation_summary()
        _safe_atomic_write_control_json(run_dir / "mutation-attribution.json", mutation)
        run_state["mutation_attribution"] = mutation
        run_state["status"] = "completed"
        run_state["provider_status"] = "completed"
        run_state["current_phase"] = None
        run_state["verdict"] = review["verdict"]
        run_state["correction_cycles"] = correction_cycles
        run_state["completed_at"] = datetime.now(timezone.utc).isoformat()
        _persist_run_state(config, canonical_root, run_dir, phase_provenance, run_state)
        report = render_markdown(
            task_file=task_file,
            plan=plan,
            implementation=implementation,
            review=review,
            correction_cycles=correction_cycles,
            phase_provenance=phase_provenance,
            mutation_attribution=mutation,
            initial_git_baseline=run_state["initial_git_baseline"],
            security_scan_authority=run_state.get("security_scan_authority"),
            security_scan_authority_history=run_state.get("security_scan_authority_history", []),
        )
        _atomic_write_text(run_dir / "REPORT.md", report)
        return RunOutcome(
            run_dir=run_dir,
            verdict=review["verdict"],
            correction_cycles=correction_cycles,
            phase_provenance=tuple(phase_provenance),
            run_result=_run_result_record(run_dir, run_state),
        )
    except BaseException as exc:
        if run_state.get("status") != "blocked":
            if isinstance(exc, KeyboardInterrupt):
                run_state["status"] = "interrupted"
                if run_state.get("current_phase"):
                    run_state["provider_status"] = "unknown_after_interruption"
            else:
                run_state["status"] = "failed"
                if run_state.get("current_phase"):
                    run_state["provider_status"] = "failed"
            failure_record = {
                "failure_type": "DirtyRepository" if "Repository has uncommitted changes." in str(exc) else type(exc).__name__,
                "failure_class": str(getattr(exc, "failure_class", "")),
                "failed_at": datetime.now(timezone.utc).isoformat(),
            }
            failed_phase = next(
                (item for item in reversed(phase_provenance) if item.get("role") == run_state.get("last_phase")),
                None,
            )
            turn_provenance = failed_phase.get("app_server_turn_provenance") if isinstance(failed_phase, dict) else None
            if isinstance(turn_provenance, dict):
                failure_record["app_server_turn_provenance"] = turn_provenance
            run_state["failure"] = failure_record
            interrupted_phase = run_state.get("current_phase")
            if interrupted_phase:
                run_state["last_phase"] = interrupted_phase
                phase = next((item for item in reversed(phase_provenance) if item.get("role") == interrupted_phase), None)
                if phase is not None and phase.get("phase_state") in {"started", "running"}:
                    phase["phase_state"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else _phase_failure_state(exc)
                    phase["provider_status"] = "unknown_after_interruption" if isinstance(exc, KeyboardInterrupt) else "failed"
                    phase["completed_at"] = datetime.now(timezone.utc).isoformat()
                run_state["current_phase"] = None
                run_state["last_actor"] = run_state.get("current_actor") or run_state.get("last_actor")
                run_state["last_backend"] = run_state.get("current_backend") or run_state.get("last_backend")
                run_state["current_actor"] = None
                run_state["current_backend"] = None
            if run_state.get("status") == "blocked":
                mutation = run_state.get("mutation_attribution")
            else:
                try:
                    mutation = mutation_summary()
                    _safe_atomic_write_control_json(run_dir / "mutation-attribution.json", mutation)
                except Exception as attribution_error:
                    mutation = {"status": "unknown", "reason": _failure_reason(attribution_error)}
            run_state["mutation_attribution"] = mutation
            run_state["dual_agents_bootstrap_artifacts"] = canonical_bootstrap_artifacts(config.repository)
            try:
                _persist_run_state(config, canonical_root, run_dir, phase_provenance, run_state)
            except Exception:
                pass
            if interrupted_phase and isinstance(exc, KeyboardInterrupt):
                phase = next((item for item in reversed(phase_provenance) if item.get("role") == interrupted_phase), {})
                _progress_event(
                    progress,
                    phase=interrupted_phase,
                    actor=phase.get("actor_id", "unknown"),
                    backend=phase.get("backend", "unknown"),
                    state="interrupted",
                    failure_type="KeyboardInterrupt",
                )
        run_state["correction_cycles"] = correction_cycles
        _attach_run_result(exc, run_dir, run_state)
        raise
