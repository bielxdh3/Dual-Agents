from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import re
import uuid
from typing import Any, Mapping

from .app_server import run_codex_security_cancel_turn
from .config import OrchestratorConfig
from .git import attribute_git_mutations, capture_git_baseline, git_top_level, head_revision
from .paths import path_identity_key, safe_ensure_directory_tree, same_path
from .report import atomic_write_json
from .security_scan import CodexSecurityProvider, SecurityScanError, _normal_path, _scan_status, _scope, stable_target_id


_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$", re.IGNORECASE)
_SAFE_FAILURE_CLASS = re.compile(r"^[A-Z0-9_]{1,128}$")
_TERMINAL_STATUSES = frozenset({"complete", "failed", "canceled"})


class SecurityRecoveryError(RuntimeError):
    def __init__(self, message: str, *, classification: str):
        super().__init__(message)
        self.classification = classification


def _safe_failure_class(value: Any, fallback: str = "UNKNOWN_ERROR") -> str:
    if isinstance(value, str) and _SAFE_FAILURE_CLASS.fullmatch(value):
        return value
    if fallback == "":
        return ""
    if isinstance(fallback, str) and _SAFE_FAILURE_CLASS.fullmatch(fallback):
        return fallback
    return "UNKNOWN_ERROR"


def _validate_uuid(value: str, field: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise SecurityRecoveryError(
            f"{field} must be one exact UUID.",
            classification="SECURITY_RECOVERY_AUTHORIZATION_INVALID",
        ) from exc
    if str(parsed) != value.casefold():
        raise SecurityRecoveryError(
            f"{field} must be one exact UUID.",
            classification="SECURITY_RECOVERY_AUTHORIZATION_INVALID",
        )
    return str(parsed)


def _validate_scope(scope: str) -> str:
    value = str(scope or "").replace("\\", "/").strip()
    if (
        not value
        or value.startswith("/")
        or re.match(r"^[A-Za-z]:", value)
        or any(part == ".." for part in value.split("/"))
    ):
        raise SecurityRecoveryError(
            "Scope must be one exact relative Security scan scope.",
            classification="SECURITY_RECOVERY_AUTHORIZATION_INVALID",
        )
    return _scope(value)


def _is_within(child: Path, parent: Path) -> bool:
    try:
        common = Path(os.path.commonpath((str(child.resolve(strict=False)), str(parent.resolve(strict=False)))))
    except (OSError, ValueError):
        return False
    return same_path(common, parent)


def _scan_summary(scan: Mapping[str, Any] | None, *, scan_id: str) -> dict[str, Any]:
    if not isinstance(scan, Mapping):
        return {"scan_id": scan_id, "status": "not_found"}
    progress = scan.get("progress")
    status = progress.get("status") if isinstance(progress, Mapping) else scan.get("status")
    return {
        "scan_id": str(scan.get("scanId", scan.get("scan_id", scan_id))),
        "target_path": str(scan.get("targetPath", scan.get("target_path", ""))),
        "target_id": str(scan.get("targetId", scan.get("target_id", ""))),
        "target_revision": str(scan.get("targetRevision", scan.get("target_revision", ""))),
        "scope": _scope(scan.get("scope", "")),
        "mode": str(scan.get("mode", "")),
        "status": str(status or "unknown").casefold(),
    }


def _mutation_summary(attribution: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "status": str(attribution.get("status", "unknown")),
        "head_changed": attribution.get("head_changed") is True,
        "branch_changed": attribution.get("branch_changed") is True,
        "staged_state_changed": attribution.get("staged_state_changed") is True,
        "repository_metadata_changed": attribution.get("repository_metadata_changed") is True,
        "touched_path_count": len(attribution.get("run_touched_paths", [])) if isinstance(attribution.get("run_touched_paths"), list) else 0,
        "created_path_count": len(attribution.get("run_created_paths", [])) if isinstance(attribution.get("run_created_paths"), list) else 0,
        "removed_path_count": len(attribution.get("run_removed_paths", [])) if isinstance(attribution.get("run_removed_paths"), list) else 0,
        "unknown_path_count": len(attribution.get("unknown_paths", [])) if isinstance(attribution.get("unknown_paths"), list) else 0,
    }


def _mutation_is_clean(attribution: Mapping[str, Any]) -> bool:
    return (
        attribution.get("status") == "complete"
        and attribution.get("head_changed") is False
        and attribution.get("branch_changed") is False
        and attribution.get("staged_state_changed") is False
        and attribution.get("repository_metadata_changed") is False
        and not attribution.get("run_touched_paths")
        and not attribution.get("run_created_paths")
        and not attribution.get("run_removed_paths")
        and not attribution.get("unknown_paths")
    )


def _thread_binding_matches_repository(binding: Any, repository: Path) -> bool:
    if not isinstance(binding, Mapping):
        return False
    roots = binding.get("runtimeWorkspaceRoots")
    environments = binding.get("environments")
    expected = _normal_path(str(repository))
    if (
        _normal_path(binding.get("cwd")) != expected
        or not isinstance(roots, list)
        or len(roots) != 1
        or _normal_path(roots[0]) != expected
        or not isinstance(environments, list)
        or not environments
    ):
        return False
    return all(
        isinstance(environment, Mapping)
        and _normal_path(environment.get("cwd")) == expected
        and isinstance(environment.get("runtimeWorkspaceRoots"), list)
        and len(environment["runtimeWorkspaceRoots"]) == 1
        and _normal_path(environment["runtimeWorkspaceRoots"][0]) == expected
        for environment in environments
    )


def _bounded_turn_timeout_provenance(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    for source, target in (
        ("termination_classification", "termination_classification"),
        ("timeout_source", "timeout_source"),
    ):
        item = value.get(source)
        if isinstance(item, str):
            result[target] = item[:128]
    timeout = value.get("turn_timeout_seconds")
    if isinstance(timeout, (int, float)) and not isinstance(timeout, bool):
        result["turn_timeout_seconds"] = timeout
    deadline_expired = value.get("host_deadline_expired")
    if isinstance(deadline_expired, bool):
        result["host_deadline_expired"] = deadline_expired
    process_alive = value.get("app_server_process_alive_at_failure")
    if isinstance(process_alive, bool) or process_alive is None:
        result["app_server_process_alive_at_failure"] = process_alive
    exit_code = value.get("app_server_process_exit_code")
    if isinstance(exit_code, int) and not isinstance(exit_code, bool):
        result["app_server_process_exit_code"] = exit_code
    return result


def _find_exact_scan(scans: list[Mapping[str, Any]], scan_id: str) -> list[Mapping[str, Any]]:
    return [
        scan
        for scan in scans
        if str(scan.get("scanId", scan.get("scan_id", ""))) == scan_id
    ]


def _scan_identity_matches(
    scan: Mapping[str, Any],
    *,
    scan_id: str,
    target: Path,
    target_id: str,
    revision: str,
    scope: str,
    mode: str,
) -> bool:
    return (
        str(scan.get("scanId", scan.get("scan_id", ""))) == scan_id
        and _normal_path(scan.get("targetPath", scan.get("target_path"))) == _normal_path(str(target))
        and str(scan.get("targetId", scan.get("target_id", ""))) == target_id
        and str(scan.get("targetRevision", scan.get("target_revision", ""))) == revision
        and _scope(scan.get("scope", "")) == scope
        and str(scan.get("mode", "")) == mode
    )


def _write_provenance(path: Path, record: dict[str, Any]) -> None:
    record["updated_at"] = datetime.now(timezone.utc).isoformat()
    atomic_write_json(path, record)


def cancel_security_scan(
    config: OrchestratorConfig,
    *,
    repository: str | Path,
    target_path: str | Path,
    scan_id: str,
    expected_revision: str,
    scope: str,
    mode: str,
    owner_thread_id: str | None = None,
    ownerless_admin: bool = False,
) -> dict[str, Any]:
    """Cancel exactly one explicitly authorized Codex Security scan.

    The owner-thread branch resumes one exact App Server thread and approves at
    most one exact destructive MCP call. The ownerless branch is restricted to
    Deep scans and uses the provider's app-only exact-ID cancellation tool.
    Both branches validate the profile ledger before and after and attribute
    every worktree and Git metadata change.
    """

    scan_id = _validate_uuid(scan_id, "scan ID")
    owner_thread = _validate_uuid(owner_thread_id, "owner thread ID") if owner_thread_id else None
    if owner_thread and ownerless_admin:
        raise SecurityRecoveryError(
            "Choose either the exact owner thread or ownerless app administration.",
            classification="SECURITY_RECOVERY_AUTHORIZATION_INVALID",
        )
    if not owner_thread and not ownerless_admin:
        raise SecurityRecoveryError(
            "Cancellation requires an exact owner thread or explicit ownerless app authorization.",
            classification="SECURITY_RECOVERY_AUTHORIZATION_INVALID",
        )
    if mode not in {"standard", "deep"}:
        raise SecurityRecoveryError(
            "Mode must be standard or deep.",
            classification="SECURITY_RECOVERY_AUTHORIZATION_INVALID",
        )
    if ownerless_admin and mode != "deep":
        raise SecurityRecoveryError(
            "Ownerless app administration is restricted to an exact Deep scan.",
            classification="SECURITY_RECOVERY_AUTHORIZATION_INVALID",
        )
    if not isinstance(expected_revision, str) or not _SHA.fullmatch(expected_revision):
        raise SecurityRecoveryError(
            "Expected revision must be one exact Git revision.",
            classification="SECURITY_RECOVERY_AUTHORIZATION_INVALID",
        )
    required_scope = _validate_scope(scope)
    executor = config.agent_for_role("executor")
    if executor.backend != "app_server" or executor.sandbox != "workspace-write":
        raise SecurityRecoveryError(
            "Security recovery requires the configured workspace-write App Server Executor profile.",
            classification="SECURITY_RECOVERY_EXECUTOR_PROFILE_UNSUPPORTED",
        )

    repository_argument = Path(repository).expanduser().resolve(strict=True)
    repository_root = git_top_level(repository_argument)
    if not same_path(repository_argument, repository_root):
        raise SecurityRecoveryError(
            "Repository must name the Git worktree root exactly.",
            classification="SECURITY_RECOVERY_REPOSITORY_BINDING_MISMATCH",
        )
    target = Path(target_path).expanduser().resolve(strict=True)
    if not target.is_dir() or not _is_within(target, repository_root):
        raise SecurityRecoveryError(
            "Security scan target must be an existing directory inside the authorized repository.",
            classification="SECURITY_RECOVERY_REPOSITORY_BINDING_MISMATCH",
        )

    operation_id = str(uuid.uuid4())
    provenance_directory = config.runs_dir.expanduser().resolve(strict=False) / "security-recovery" / operation_id
    if _is_within(provenance_directory, repository_root):
        raise SecurityRecoveryError(
            "Security recovery provenance must be stored outside the target repository.",
            classification="SECURITY_RECOVERY_PROVENANCE_PATH_UNSAFE",
        )
    safe_ensure_directory_tree(provenance_directory)
    provenance_path = provenance_directory / "provenance.json"

    record: dict[str, Any] = {
        "schema_version": 1,
        "operation_id": operation_id,
        "operation_type": "security_scan_cancel",
        "actor_profile": executor.account_name,
        "actor_backend": executor.backend,
        "repository_identity": {
            "path": str(repository_root),
            "head_before": head_revision(repository_root),
        },
        "authorized_scan_id": scan_id,
        "owner_thread_id": owner_thread or "",
        "target": {
            "path": str(target),
            "target_id": stable_target_id(target),
            "revision": expected_revision,
            "scope": required_scope,
            "mode": mode,
        },
        "approval": {
            "normal_policy": "never",
            "maintenance_policy": "on-request" if owner_thread else "not_applicable",
            "reset_policy": "never" if owner_thread else "not_applicable",
            "temporary_tool_policy": (
                {
                    "codex_apps_enabled": False,
                    "enabled_security_mcp_server": "codex-security",
                    "enabled_security_tool_names": ["cancel_codex_security_scan"],
                    "apps_enabled": False,
                    "browser_use_enabled": False,
                    "browser_use_external_enabled": False,
                    "browser_use_full_cdp_access_enabled": False,
                    "computer_use_enabled": False,
                    "image_generation_enabled": False,
                    "hooks_enabled": False,
                    "shell_tool_enabled": False,
                    "multi_agent_enabled": False,
                    "sleep_tool_enabled": False,
                    "web_search": "disabled",
                    "turn_sandbox": "readOnly",
                    "turn_network_access": False,
                }
                if owner_thread
                else {"provider_call": "exact_id_only", "turn_sandbox": "not_applicable"}
            ),
            "allowed_tool": (
                "codex-security.cancel_codex_security_scan"
                if owner_thread
                else "codex-security.cancel_codex_security_scan_from_app"
            ),
            "approval_granted": False,
        },
        "ledger_before": None,
        "ledger_after": None,
        "ledger_before_identity_matches": None,
        "ledger_after_identity_matches": None,
        "provider_outcome": None,
        "mutation_attribution": {"status": "pending"},
        "provenance_path": str(provenance_path),
        "tool_call_attempted": False,
        "cancellation_attempted": False,
        "scan_terminal": False,
        "success": False,
        "classification": "IN_PROGRESS",
    }

    baseline: dict[str, Any] | None = None
    provider: CodexSecurityProvider | None = None
    before_scan: Mapping[str, Any] | None = None
    preflight_classification = ""
    action_result: dict[str, Any] | None = None
    tool_result_acceptable = False
    already_terminal = False
    try:
        _write_provenance(provenance_path, record)
        baseline = capture_git_baseline(repository_root, include_ignored=True)
        if baseline.get("complete") is not True:
            preflight_classification = "SECURITY_RECOVERY_MUTATION_BASELINE_INCOMPLETE"
            record["preflight_failure_class"] = preflight_classification
            return record
        provider = CodexSecurityProvider(executor)
        scans = provider.list_target_scans(target)
        matches = _find_exact_scan(scans, scan_id)
        if len(matches) != 1:
            record["ledger_before"] = {
                "scan_id": scan_id,
                "match_count": len(matches),
                "status": "not_found" if not matches else "ambiguous",
            }
            preflight_classification = "SECURITY_RECOVERY_LEDGER_SCAN_MISSING_OR_AMBIGUOUS"
            return record
        before_scan = matches[0]
        before_summary = _scan_summary(before_scan, scan_id=scan_id)
        record["ledger_before"] = before_summary
        expected_target_id = stable_target_id(target)
        record["ledger_before_identity_matches"] = _scan_identity_matches(
            before_scan,
            scan_id=scan_id,
            target=target,
            target_id=expected_target_id,
            revision=expected_revision,
            scope=required_scope,
            mode=mode,
        )
        if not record["ledger_before_identity_matches"]:
            preflight_classification = "SECURITY_RECOVERY_LEDGER_IDENTITY_MISMATCH"
            return record
        before_status = _scan_status(before_scan)
        if before_status in _TERMINAL_STATUSES:
            already_terminal = True
            preflight_classification = "SECURITY_RECOVERY_ALREADY_TERMINAL"
            return record
        if before_status != "running":
            preflight_classification = "SECURITY_RECOVERY_LEDGER_STATE_UNSAFE"
            return record

        record["cancellation_attempted"] = True
        _write_provenance(provenance_path, record)
        if owner_thread:
            action_result = run_codex_security_cancel_turn(
                config=config,
                agent=executor,
                repository=repository_root,
                owner_thread_id=owner_thread,
                scan_id=scan_id,
            )
            record["owner_thread_execution"] = {
                "thread_id": str(action_result.get("thread_id", "")),
                "thread_resumed": action_result.get("thread_resumed") is True,
                "turn_id": str(action_result.get("turn_id", "")),
                "turn_state": str(action_result.get("turn_state", "unknown")),
                "failure_class": _safe_failure_class(action_result.get("failure_class", ""), ""),
                "resume_failure_class": _safe_failure_class(action_result.get("resume_failure_class", ""), ""),
                "approval_state_cleared": action_result.get("approval_state_cleared") is True,
                "approval_policy_reset": action_result.get("approval_policy_reset") is True,
                "approval_policy_reset_failure_class": _safe_failure_class(
                    action_result.get("approval_policy_reset_failure_class", ""), ""
                ),
                "tool_catalog": dict(action_result.get("tool_catalog", {}))
                if isinstance(action_result.get("tool_catalog"), Mapping)
                else {},
                "tool_catalog_validated": action_result.get("tool_catalog_validated") is True,
                "native_tool_policy": dict(action_result.get("native_tool_policy", {}))
                if isinstance(action_result.get("native_tool_policy"), Mapping)
                else {},
                "native_tool_policy_validated": action_result.get("native_tool_policy_validated") is True,
                "temporary_tool_policy_cleared": action_result.get("temporary_tool_policy_cleared") is True,
                "unexpected_item_seen": action_result.get("unexpected_item_seen") is True,
                "turn_cancel_requested": action_result.get("turn_cancel_requested") is True,
                "turn_cancel_confirmed": action_result.get("turn_cancel_confirmed") is True,
                "turn_timeout_provenance": _bounded_turn_timeout_provenance(action_result.get("turn_provenance")),
            }
            record["tool_call_attempted"] = action_result.get("tool_call_attempted") is True
            record["approval"]["approval_granted"] = action_result.get("approval_granted") is True
            record["approval"]["request_count"] = int(action_result.get("approval_request_count", 0))
            record["approval"]["denial_reason"] = str(action_result.get("approval_denial_reason", ""))
            record["provider_outcome"] = {
                "tool": "codex-security.cancel_codex_security_scan",
                "scan_id": scan_id,
                "tool_call_attempted": action_result.get("tool_call_attempted") is True,
                "tool_call_count": int(action_result.get("tool_call_count", 0)),
                "matching_tool_call_count": int(action_result.get("matching_tool_call_count", 0)),
                "result": dict(action_result["provider_tool_result"])
                if isinstance(action_result.get("provider_tool_result"), Mapping)
                else None,
            }
            binding = action_result.get("thread_binding")
            record["owner_thread_execution"]["cwd"] = str(binding.get("cwd", "")) if isinstance(binding, Mapping) else ""
            roots = binding.get("runtimeWorkspaceRoots", []) if isinstance(binding, Mapping) else []
            record["owner_thread_execution"]["runtime_workspace_roots"] = [str(root) for root in roots] if isinstance(roots, list) else []
            environments = binding.get("environments", []) if isinstance(binding, Mapping) else []
            record["owner_thread_execution"]["environments"] = [
                {
                    "cwd": str(environment.get("cwd", "")),
                    "runtime_workspace_roots": [str(root) for root in environment.get("runtimeWorkspaceRoots", [])]
                    if isinstance(environment.get("runtimeWorkspaceRoots"), list)
                    else [],
                }
                for environment in environments
                if isinstance(environment, Mapping)
            ] if isinstance(environments, list) else []
            record["owner_thread_execution"]["binding_validated"] = (
                action_result.get("thread_resumed") is True
                and str(action_result.get("thread_id", "")) == owner_thread
                and _thread_binding_matches_repository(binding, repository_root)
            )
            tool_result = action_result.get("provider_tool_result")
            tool_result_acceptable = (
                record["owner_thread_execution"]["thread_resumed"]
                and record["owner_thread_execution"]["binding_validated"]
                and record["owner_thread_execution"]["turn_state"] == "completed"
                and bool(record["owner_thread_execution"]["turn_id"])
                and record["owner_thread_execution"]["approval_state_cleared"]
                and record["owner_thread_execution"]["approval_policy_reset"]
                and record["owner_thread_execution"]["native_tool_policy_validated"]
                and record["owner_thread_execution"]["tool_catalog_validated"]
                and record["owner_thread_execution"]["temporary_tool_policy_cleared"]
                and record["approval"]["approval_granted"]
                and record["approval"]["request_count"] == 1
                and not record["approval"]["denial_reason"]
                and record["provider_outcome"]["tool_call_attempted"]
                and record["provider_outcome"]["tool_call_count"] == 1
                and record["provider_outcome"]["matching_tool_call_count"] == 1
                and isinstance(tool_result, Mapping)
                and tool_result.get("item_status") == "completed"
                and tool_result.get("result_returned") is True
                and tool_result.get("provider_error") is False
                and not record["owner_thread_execution"]["unexpected_item_seen"]
                and not action_result.get("failure_class")
            )
        else:
            action_result = provider.cancel_ownerless_deep_scan(scan_id)
            record["tool_call_attempted"] = action_result.get("tool_call_attempted") is True
            record["approval"]["approval_granted"] = False
            record["approval"]["approval_required"] = False
            record["provider_outcome"] = dict(action_result)
            if "failure_class" in record["provider_outcome"]:
                record["provider_outcome"]["failure_class"] = _safe_failure_class(
                    record["provider_outcome"].get("failure_class", ""), "UNKNOWN_ERROR"
                )
            tool_result_acceptable = (
                action_result.get("tool") == "cancel_codex_security_scan_from_app"
                and action_result.get("scan_id") == scan_id
                and action_result.get("result_returned") is True
                and action_result.get("provider_error") is False
                and action_result.get("result_scan_id_matches") is True
                and str(action_result.get("result_status", "")).casefold() in _TERMINAL_STATUSES
            )
        if not tool_result_acceptable:
            failure_class = (
                _safe_failure_class(action_result.get("failure_class", ""), "")
                if isinstance(action_result, Mapping)
                else ""
            )
            result_was_returned = (
                action_result.get("result_returned") is True and action_result.get("provider_error") is False
                if isinstance(action_result, Mapping)
                else False
            )
            preflight_classification = failure_class or (
                "SECURITY_RECOVERY_PROVIDER_RESULT_UNCONFIRMED"
                if result_was_returned
                else "SECURITY_RECOVERY_PROVIDER_TOOL_FAILED"
            )
    except SecurityRecoveryError as exc:
        preflight_classification = _safe_failure_class(exc.classification, "SECURITY_RECOVERY_ERROR")
        record["preflight_failure_class"] = preflight_classification
    except SecurityScanError as exc:
        preflight_classification = _safe_failure_class(exc.failure_class, "SECURITY_SCAN_ERROR")
        record["preflight_failure_class"] = preflight_classification
    except Exception as exc:
        preflight_classification = _safe_failure_class(
            getattr(exc, "failure_class", ""), _safe_failure_class(type(exc).__name__.upper())
        )
        record["preflight_failure_class"] = preflight_classification
    finally:
        if provider is not None and baseline is not None:
            try:
                after_scans = provider.list_target_scans(target)
                after_matches = _find_exact_scan(after_scans, scan_id)
                if len(after_matches) == 1:
                    record["ledger_after"] = _scan_summary(after_matches[0], scan_id=scan_id)
                    record["ledger_after_identity_matches"] = _scan_identity_matches(
                        after_matches[0],
                        scan_id=scan_id,
                        target=target,
                        target_id=stable_target_id(target),
                        revision=expected_revision,
                        scope=required_scope,
                        mode=mode,
                    )
                else:
                    record["ledger_after"] = {
                        "scan_id": scan_id,
                        "match_count": len(after_matches),
                        "status": "not_found" if not after_matches else "ambiguous",
                    }
                    record["ledger_after_identity_matches"] = False
            except Exception as exc:
                record["ledger_after"] = {
                    "scan_id": scan_id,
                    "status": "unavailable",
                    "failure_class": _safe_failure_class(
                        getattr(exc, "failure_class", ""), _safe_failure_class(type(exc).__name__.upper())
                    ),
                }
                record["ledger_after_identity_matches"] = False
        if baseline is not None:
            try:
                attribution = attribute_git_mutations(repository_root, baseline, include_ignored=True)
                record["mutation_attribution"] = _mutation_summary(attribution)
            except Exception as exc:
                attribution = None
                record["mutation_attribution"] = {
                    "status": "unknown",
                    "failure_class": _safe_failure_class(
                        getattr(exc, "failure_class", ""), _safe_failure_class(type(exc).__name__.upper())
                    ),
                }
        else:
            attribution = None
            record["mutation_attribution"] = {"status": "unknown", "failure_class": "baseline_not_captured"}

        ledger_after = record.get("ledger_after")
        after_status = str(ledger_after.get("status", "unknown")) if isinstance(ledger_after, Mapping) else "unknown"
        record["scan_terminal"] = after_status in _TERMINAL_STATUSES
        mutation_clean = attribution is not None and _mutation_is_clean(attribution)
        if already_terminal:
            record["success"] = bool(mutation_clean and record["scan_terminal"])
            if not mutation_clean:
                record["classification"] = "SECURITY_RECOVERY_MUTATION_ATTRIBUTION_FAILED"
            elif record["scan_terminal"] and record.get("ledger_after_identity_matches") is True:
                record["classification"] = "SECURITY_RECOVERY_ALREADY_TERMINAL"
            else:
                record["classification"] = "SECURITY_RECOVERY_LEDGER_STATE_UNCONFIRMED"
        elif not mutation_clean:
            record["success"] = False
            record["classification"] = "SECURITY_RECOVERY_MUTATION_ATTRIBUTION_FAILED"
        elif not record["cancellation_attempted"] and preflight_classification:
            record["success"] = False
            record["classification"] = preflight_classification
        elif record.get("ledger_after_identity_matches") is not True:
            record["success"] = False
            record["classification"] = "SECURITY_RECOVERY_LEDGER_IDENTITY_UNCONFIRMED"
        elif after_status not in _TERMINAL_STATUSES:
            record["success"] = False
            result = record.get("provider_outcome", {}).get("result") if isinstance(record.get("provider_outcome"), Mapping) else record.get("provider_outcome")
            if isinstance(result, Mapping) and result.get("provider_error_class") == "scan_not_found":
                record["classification"] = "SECURITY_RECOVERY_PROVIDER_CONTEXT_MISMATCH"
            else:
                record["classification"] = preflight_classification or "SECURITY_RECOVERY_SCAN_REMAINS_ACTIVE"
        elif not tool_result_acceptable:
            record["success"] = False
            record["classification"] = preflight_classification or "SECURITY_RECOVERY_PROVIDER_RESULT_UNCONFIRMED"
        elif preflight_classification:
            record["success"] = False
            record["classification"] = preflight_classification
        elif owner_thread:
            record["success"] = True
            record["classification"] = "SECURITY_RECOVERY_CANCELLED_BY_OWNER_THREAD"
        else:
            record["success"] = True
            record["classification"] = "SECURITY_RECOVERY_CANCELLED_BY_APP_ADMIN"
        try:
            _write_provenance(provenance_path, record)
        except Exception:
            record["success"] = False
            record["classification"] = "SECURITY_RECOVERY_PROVENANCE_WRITE_FAILED"
    return record
