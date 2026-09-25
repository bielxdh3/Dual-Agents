from __future__ import annotations

import ctypes
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Callable, Mapping
from uuid import uuid4

from .bootstrap import canonical_instructions_root
from .codex import classify_actor_failure, configured_actor_provenance
from .codex import run_codex_exec as _run_codex_exec_legacy
from .codex import run_codex_app_server
from .codex import run_codex_terminal
from .antigravity import antigravity_status, run_antigravity
from .config import ConfigError, OrchestratorConfig
from .git import ensure_git_repository, head_revision, status_and_diff, status_porcelain
from .live_events import LiveEventJournal, repository_identity
from .paths import path_identity_key
from .process import CommandError, CommandResult
from .providers import provider_supports_role
from .registry import login_status
from .report import (
    EXECUTOR_REPORT_FIELDS,
    EXECUTOR_REPORT_OPTIONAL_FIELDS,
    atomic_write_json,
    dump_json,
    normalise_executor_report,
)


def run_codex_exec(**kwargs):
    """Select the configured structured backend while preserving the TUI seam."""
    requested_role = kwargs.get("role", "executor")
    if requested_role != "executor":
        raise DelegationError(
            "The executor delegation adapter cannot satisfy a configured non-executor role; "
            "no native or implicit fallback is permitted."
        )
    config = kwargs.get("config")
    agent = kwargs.get("agent")
    if agent is not None and agent.backend == "antigravity":
        if kwargs.get("reuse_existing"):
            raise DelegationError("Antigravity does not support native TUI reuse.")
        return run_antigravity(
            command=getattr(config, "antigravity_command", "agy"),
            agent=agent,
            repository=kwargs["repository"],
            prompt=kwargs["prompt"],
            output_path=kwargs["output_path"],
            schema_path=kwargs.get("schema_path"),
            config=config,
            conversation_id=kwargs.get("conversation_id", ""),
            task_artifact_path=kwargs.get("task_artifact_path"),
            task_sha256=kwargs.get("task_sha256", ""),
            progress=kwargs.get("progress"),
        )
    if kwargs.get("reuse_existing") and agent is not None and agent.backend != "windows":
        raise DelegationError("Strict reuse-existing requires a registered native Windows Executor TUI.")
    if agent is not None and agent.backend == "app_server":
        app_server_kwargs = dict(kwargs)
        app_server_kwargs.pop("schema_path", None)
        app_server_kwargs.pop("check", None)
        app_server_kwargs.pop("reuse_existing", None)
        app_server_kwargs.pop("conversation_id", None)
        from .terminal import session_id_for

        app_server_kwargs["session_id"] = session_id_for(
            agent.account_name,
            kwargs["repository"],
        )
        app_server_kwargs["request_id"] = kwargs.get("request_id", "")
        app_server_kwargs["run_id"] = kwargs.get("run_id", app_server_kwargs["session_id"])
        app_server_kwargs["role"] = kwargs.get("role", "executor")
        app_server_kwargs["require_workspace_ready"] = (
            kwargs.get("role", "executor") == "executor" and kwargs["agent"].sandbox == "workspace-write"
        )
        return run_codex_app_server(**app_server_kwargs)
    if agent is None or agent.backend != "windows":
        raise DelegationError(
            f"Unsupported Codex backend '{getattr(agent, 'backend', '')}'; no fallback is permitted."
        )
    command_name = Path(config.codex_command).stem.casefold() if config else "codex"
    if kwargs.get("reuse_existing") and not command_name.startswith("codex"):
        raise DelegationError("Strict reuse-existing refuses non-Codex or non-native fallback backends.")
    if config is not None and not command_name.startswith("codex"):
        return _run_codex_exec_legacy(
            codex_command=config.codex_command,
            agent=kwargs["agent"],
            repository=kwargs["repository"],
            prompt=kwargs["prompt"],
            output_path=kwargs["output_path"],
            schema_path=kwargs["schema_path"],
            check=False,
            progress=kwargs.get("progress"),
        )
    terminal_kwargs = dict(kwargs)
    terminal_kwargs.pop("schema_path", None)
    terminal_kwargs.pop("check", None)
    terminal_kwargs.pop("request_id", None)
    terminal_kwargs.pop("run_id", None)
    terminal_kwargs.pop("role", None)
    from .terminal import session_id_for

    terminal_kwargs["session_id"] = session_id_for(
        kwargs["agent"].account_name,
        kwargs["repository"],
    )
    # Native Executor delegation is always strict: never replace or fall back
    # from the already-registered TUI when an attempt-level probe fails.
    terminal_kwargs["reuse_existing"] = True
    return run_codex_terminal(**terminal_kwargs)


REQUEST_SCHEMA_VERSION = 1
RESULT_SCHEMA_VERSION = 1
PUBLICATION_ACTIONS = (
    "local_mutation",
    "local_commit",
    "normal_push",
    "create_branch",
    "branch_publication",
    "draft_pr_create",
    "draft_pr_update",
    "ready_for_review",
    "merge",
    "tag",
    "release",
    "deploy",
    "force_push",
    "destructive_remote",
    "issue_state",
    "repository_settings",
)
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SECRET = re.compile(
    r"""(?ix)(
        (?:authorization\s*:\s*bearer\s+)
        |(?:\b(?:token|api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)\b\"?\s*[:=]\s*)
    )(?:\"[^\"]*\"|'[^']*'|[^\s,}]+)"""
)
_AUTH_PATH = re.compile(r"(?i)(?:[A-Za-z]:)?[^\r\n\s\"']*auth\.json")
TASK_CONTROL_MESSAGE_MAX = 500


class DelegationError(RuntimeError):
    """Raised for a delegation that cannot be safely started."""


class InvalidRequestError(DelegationError):
    pass


@dataclass(frozen=True)
class MissionAuthorization:
    """Trusted, request-scoped capabilities; every action is denied by default."""

    allowed_actions: frozenset[str] = frozenset()

    def allows(self, action: str) -> bool:
        return action in self.allowed_actions

    def as_dict(self) -> dict[str, list[str]]:
        return {"allowed_actions": sorted(self.allowed_actions)}


@dataclass(frozen=True)
class DelegationRequest:
    schema_version: int
    request_id: str
    action: str
    repository: Path
    task: str
    constraints: tuple[str, ...]
    context_files: tuple[str, ...]
    review_findings: tuple[dict[str, Any], ...]
    max_correction_cycles: int
    authorization: MissionAuthorization
    parent_request_id: str | None = None
    antigravity_conversation_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "action": self.action,
            "repository": str(self.repository),
            "task": self.task,
            "constraints": list(self.constraints),
            "context_files": list(self.context_files),
            "review_findings": [dict(item) for item in self.review_findings],
            "max_correction_cycles": self.max_correction_cycles,
            "authorization": self.authorization.as_dict(),
            **({"parent_request_id": self.parent_request_id} if self.parent_request_id else {}),
            **({"antigravity_conversation_id": self.antigravity_conversation_id} if self.antigravity_conversation_id else {}),
        }


@dataclass(frozen=True)
class DelegationOutcome:
    status: str
    request_id: str
    result_file: Path
    run_directory: Path | None
    elapsed_seconds: float


def sanitize_text(value: str) -> str:
    value = _AUTH_PATH.sub("[REDACTED_AUTH_PATH]", str(value))
    return _SECRET.sub(lambda match: f"{match.group(0).split(':', 1)[0].split('=', 1)[0]}=[REDACTED]", value)


def sanitize_value(value: Any, *, key: str = "") -> Any:
    lowered = key.casefold()
    if any(
        marker in lowered
        for marker in ("token", "secret", "password", "credential", "api_key", "api-key", "authorization")
    ):
        return "[REDACTED]"
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, Mapping):
        return {str(name): sanitize_value(item, key=str(name)) for name, item in value.items()}
    if isinstance(value, list):
        return [sanitize_value(item) for item in value]
    return value


def _safe_diff(value: str) -> str:
    lines: list[str] = []
    redact_section = False
    for line in value.splitlines(keepends=True):
        if line.startswith("diff --git "):
            redact_section = any(
                marker in line.casefold()
                for marker in ("auth.json", ".env", "credentials", "secret")
            )
            if redact_section:
                lines.append("diff section redacted by Dual Agents\n")
                continue
        if not redact_section:
            lines.append(line)
    return sanitize_text("".join(lines))


def _required_string(raw: Mapping[str, Any], name: str) -> str:
    value = raw.get(name)
    if not isinstance(value, str) or not value.strip():
        raise InvalidRequestError(f"Request field '{name}' must be a non-empty string.")
    return value.strip()


def _string_list(raw: Mapping[str, Any], name: str) -> tuple[str, ...]:
    value = raw.get(name, [])
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise InvalidRequestError(f"Request field '{name}' must be an array of strings.")
    return tuple(item.strip() for item in value if item.strip())


def _request_id(raw: Mapping[str, Any]) -> str:
    value = _required_string(raw, "request_id")
    if not _REQUEST_ID.fullmatch(value):
        raise InvalidRequestError(
            "Request field 'request_id' must contain only letters, numbers, '.', '_' or '-'."
        )
    return value


def _authorization(raw: Mapping[str, Any]) -> MissionAuthorization:
    value = raw.get("authorization", {})
    if not isinstance(value, Mapping):
        raise InvalidRequestError("Request field 'authorization' must be an object.")
    unknown = sorted(set(value) - {"allowed_actions"})
    if unknown:
        raise InvalidRequestError(
            f"Unknown authorization field(s): {', '.join(unknown)}."
        )
    actions = value.get("allowed_actions", [])
    if not isinstance(actions, list) or any(not isinstance(item, str) or not item.strip() for item in actions):
        raise InvalidRequestError(
            "Authorization field 'allowed_actions' must be an array of non-empty strings."
        )
    normalised = [item.strip() for item in actions]
    if len(normalised) != len(set(normalised)):
        raise InvalidRequestError("Authorization 'allowed_actions' must not contain duplicates.")
    unknown_actions = sorted(set(normalised) - set(PUBLICATION_ACTIONS))
    if unknown_actions:
        raise InvalidRequestError(
            f"Unknown authorization action(s): {', '.join(unknown_actions)}."
        )
    return MissionAuthorization(frozenset(normalised))


def _repository(value: str, config: OrchestratorConfig) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else config.config_path.parent / path).resolve()


def parse_request(
    raw: Any,
    config: OrchestratorConfig,
    *,
    repository_override: str | None = None,
) -> DelegationRequest:
    if not isinstance(raw, dict):
        raise InvalidRequestError("Delegation request must be a JSON object.")
    allowed = {
        "schema_version",
        "request_id",
        "action",
        "repository",
        "task",
        "constraints",
        "context_files",
        "review_findings",
        "max_correction_cycles",
        "authorization",
        "parent_request_id",
        "antigravity_conversation_id",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise InvalidRequestError(f"Unknown request field(s): {', '.join(unknown)}.")
    version = raw.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool) or version != REQUEST_SCHEMA_VERSION:
        raise InvalidRequestError(
            f"Unsupported request schema_version {version!r}; expected {REQUEST_SCHEMA_VERSION}."
        )
    request_id = _request_id(raw)
    action = _required_string(raw, "action")
    if action not in {"implement", "correct"}:
        raise InvalidRequestError(
            f"Unknown delegation action '{action}'; supported actions: implement, correct."
        )
    repository_value = repository_override if repository_override is not None else raw.get("repository")
    if not isinstance(repository_value, str) or not repository_value.strip():
        raise InvalidRequestError(
            "A target repository is required in the request or through --repository."
        )
    task = _required_string(raw, "task")
    constraints = _string_list(raw, "constraints")
    context_files = _string_list(raw, "context_files")
    findings_raw = raw.get("review_findings", [])
    if not isinstance(findings_raw, list) or any(not isinstance(item, dict) for item in findings_raw):
        raise InvalidRequestError("Request field 'review_findings' must be an array of objects.")
    findings: list[dict[str, Any]] = []
    for finding in findings_raw:
        title = finding.get("title")
        details = finding.get("details")
        if not isinstance(title, str) or not title.strip() or not isinstance(details, str) or not details.strip():
            raise InvalidRequestError(
                "Each review finding must contain non-empty string fields 'title' and 'details'."
            )
        severity = finding.get("severity", "important")
        if severity not in {"blocking", "important", "optional"}:
            raise InvalidRequestError("Review finding severity must be blocking, important, or optional.")
        findings.append(dict(finding))
    authorization = _authorization(raw)
    max_cycles = raw.get("max_correction_cycles", config.max_correction_cycles)
    if isinstance(max_cycles, bool) or not isinstance(max_cycles, int) or max_cycles < 0:
        raise InvalidRequestError("Request field 'max_correction_cycles' must be a non-negative integer.")
    if max_cycles > config.max_correction_cycles:
        raise InvalidRequestError(
            "Request max_correction_cycles exceeds the configured maximum "
            f"({config.max_correction_cycles})."
        )
    parent_request_id = raw.get("parent_request_id")
    if parent_request_id is not None:
        if not isinstance(parent_request_id, str) or not _REQUEST_ID.fullmatch(parent_request_id.strip()):
            raise InvalidRequestError("Request field 'parent_request_id' is invalid.")
        parent_request_id = parent_request_id.strip()
    conversation_id = raw.get("antigravity_conversation_id", "")
    if not isinstance(conversation_id, str) or len(conversation_id) > 500 or any(
        char in conversation_id for char in "\r\n"
    ):
        raise InvalidRequestError(
            "Request field 'antigravity_conversation_id' must be a single-line string of at most 500 characters."
        )
    conversation_id = conversation_id.strip()
    if action == "correct":
        if not parent_request_id:
            raise InvalidRequestError("Correct requests must link to a parent_request_id.")
        if not findings:
            raise InvalidRequestError("Correct requests require actionable review_findings.")
    return DelegationRequest(
        schema_version=version,
        request_id=request_id,
        action=action,
        repository=_repository(repository_value.strip(), config),
        task=task,
        constraints=constraints,
        context_files=context_files,
        review_findings=tuple(findings),
        max_correction_cycles=max_cycles,
        authorization=authorization,
        parent_request_id=parent_request_id,
        antigravity_conversation_id=conversation_id,
    )


def load_request(
    config: OrchestratorConfig,
    *,
    request_file: Path | None = None,
    stdin_text: str | None = None,
    repository_override: str | None = None,
) -> DelegationRequest:
    if (request_file is None) == (stdin_text is None):
        raise InvalidRequestError("Provide exactly one of --request-file or --stdin.")
    try:
        text = request_file.read_text(encoding="utf-8-sig") if request_file else stdin_text or ""
        raw = json.loads(text)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InvalidRequestError(f"Could not read delegation request JSON: {exc}") from exc
    return parse_request(raw, config, repository_override=repository_override)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _windows_pid_alive(pid: int) -> bool:
    """Query a Windows PID without terminating or otherwise affecting it."""

    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    # PROCESS_QUERY_LIMITED_INFORMATION is sufficient and avoids requiring
    # broad process rights for a lock held by another user.
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if handle:
        kernel32.CloseHandle(handle)
        return True

    error = ctypes.get_last_error()
    if error == 5:  # ERROR_ACCESS_DENIED: the process exists but is protected.
        return True
    if error in {6, 87, 1168}:  # invalid handle/parameter or process not found.
        return False
    # Unknown query failures are treated as live to preserve the lock safety
    # property; stale locks can still be recovered explicitly.
    return True


def _process_start_token(pid: int) -> str | None:
    """Return a process-creation token when the platform exposes one."""

    if pid <= 0:
        return None
    if os.name == "nt":
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x0400, False, pid)
        if not handle:
            return None
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel_time = wintypes.FILETIME()
        user_time = wintypes.FILETIME()
        try:
            if not kernel32.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_time), ctypes.byref(kernel_time), ctypes.byref(user_time)):
                return None
            value = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
            return str(value)
        finally:
            kernel32.CloseHandle(handle)
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").rsplit(") ", 1)[-1].split()
        return fields[19]
    except (OSError, UnicodeError, IndexError):
        return None


def _safe_process_start_token(pid: int) -> str | None:
    try:
        return _process_start_token(pid)
    except Exception:
        return None


class RepositoryLock:
    """A conservative, repository-scoped lock for local executor runs."""

    def __init__(self, runs_dir: Path, repository: Path, request_id: str, run_id: str = "") -> None:
        digest = hashlib.sha256(path_identity_key(repository).encode("utf-8")).hexdigest()[:24]
        self.path = runs_dir / ".locks" / f"{digest}.json"
        self.repository = repository
        self.request_id = request_id
        self.run_id = run_id
        self._held = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "request_id": self.request_id,
            "run_id": self.run_id,
            "repository_key": repository_identity(self.repository),
            "repository": str(self.repository),
            "pid": os.getpid(),
            "process_start": _safe_process_start_token(os.getpid()),
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        for _ in range(2):
            try:
                descriptor = os.open(
                    self.path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
                try:
                    os.write(descriptor, (json.dumps(payload) + "\n").encode("utf-8"))
                finally:
                    os.close(descriptor)
                self._held = True
                return
            except FileExistsError:
                try:
                    existing_text = self.path.read_text(encoding="utf-8")
                    existing = json.loads(existing_text)
                    pid = int(existing.get("pid", 0))
                except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                    raise DelegationError(
                        f"Repository lock exists and cannot be inspected: {self.path}"
                    ) from exc
                stored_start = existing.get("process_start")
                live_start = _safe_process_start_token(pid) if _pid_alive(pid) else None
                if _pid_alive(pid) and (
                    not isinstance(stored_start, str)
                    or not stored_start
                    or live_start is None
                    or live_start == stored_start
                ):
                    raise DelegationError(
                        "Repository is already delegated or in use; active request "
                        f"'{existing.get('request_id', 'unknown')}'."
                    )
                try:
                    if self.path.read_text(encoding="utf-8") != existing_text:
                        continue
                    self.path.unlink()
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    raise DelegationError(f"Could not recover stale repository lock: {self.path}") from exc
        raise DelegationError(f"Repository lock acquisition raced: {self.path}")

    def release(self) -> None:
        if not self._held:
            return
        try:
            existing = json.loads(self.path.read_text(encoding="utf-8"))
            current_start = _safe_process_start_token(os.getpid())
            stored_start = existing.get("process_start")
            same_process = (
                current_start is None
                or stored_start is None
                or stored_start == current_start
            )
            if (
                existing.get("request_id") == self.request_id
                and int(existing.get("pid", 0)) == os.getpid()
                and same_process
            ):
                self.path.unlink(missing_ok=True)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        self._held = False

    def __enter__(self) -> "RepositoryLock":
        self.acquire()
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.release()


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _publish_run_event(
    journal: LiveEventJournal | None,
    *,
    method: str,
    state: str,
    detail: Mapping[str, Any],
    thread_id: str = "",
    turn_id: str = "",
) -> None:
    if journal is None:
        return
    try:
        journal.append(
            kind="run",
            state=state,
            method=method,
            detail=dict(detail),
            thread_id=thread_id,
            turn_id=turn_id,
        )
    except Exception:
        # A telemetry failure must not change delegation or repository safety.
        pass


def _request_id_hint(raw_text: str) -> str:
    try:
        raw = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError):
        return "invalid-" + uuid4().hex[:12]
    value = raw.get("request_id") if isinstance(raw, dict) else None
    return value if isinstance(value, str) and _REQUEST_ID.fullmatch(value) else "invalid-" + uuid4().hex[:12]


def _result(
    *,
    request_id: str,
    status: str,
    started_at: str,
    finished_at: str,
    summary: str,
    repository: str = "",
    executor_account: str = "",
    executor_label: str = "",
    executor_sandbox: str = "",
    exit_code: int | None = None,
    parent_request_id: str | None = None,
    files_changed: list[str] | None = None,
    commands_run: list[str] | None = None,
    tests: list[Any] | None = None,
    remaining_issues: list[str] | None = None,
    memory_updates: list[dict[str, Any]] | None = None,
    git_status: str = "",
    diff_file: str = "",
    run_directory: str = "",
    executor_report_file: str = "",
    executor_report_actor: str = "",
    stderr_file: str = "",
    terminal_session_id: str = "",
    terminal_turn_start: str = "",
    app_server_thread_id: str = "",
    app_server_turn_id: str = "",
    app_server_process_id: str = "",
    executor_windows_sandbox: str = "",
    executor_windows_sandbox_readiness: str = "",
    executor_approval_policy: str = "",
    executor_role: str = "",
    executor_provider: str = "",
    executor_actor_id: str = "",
    executor_configured_actor: bool = False,
    executor_adapter: str = "",
    executor_backend: str = "",
    executor_model: str = "",
    executor_reasoning_effort: str = "",
    primary_actor: str = "",
    actual_actor: str = "",
    fallback_enabled: bool = False,
    fallback_used: bool = False,
    failed_actor: str = "",
    fallback_actor: str = "",
    fallback_reason: str = "",
    fallback_failure_class: str = "",
    executor_state_root_identity: str = "",
    canonical_instructions_root: str = "",
    canonical_bootstrap_required: bool = False,
    delegation_transport: str = "",
    antigravity_conversation_id: str = "",
    antigravity_terminal_status: str = "",
    task_transport: str = "",
    task_artifact: str = "",
    task_sha256: str = "",
    reuse_existing: bool = False,
    reuse_provenance: Mapping[str, Any] | None = None,
    error: str = "",
) -> dict[str, Any]:
    return sanitize_value(
        {
            "schema_version": RESULT_SCHEMA_VERSION,
            "request_id": request_id,
            "parent_request_id": parent_request_id,
            "status": status,
            "executor_account": executor_account,
            "executor_label": executor_label,
            "executor_sandbox": executor_sandbox,
            "exit_code": exit_code,
            "started_at": started_at,
            "finished_at": finished_at,
            "summary": summary,
            "repository": repository,
            "files_changed": files_changed or [],
            "commands_run": commands_run or [],
            "tests": tests or [],
            "remaining_issues": remaining_issues or [],
            "memory_updates": memory_updates or [],
            "git_status": git_status,
            "diff_file": diff_file,
            "run_directory": run_directory,
            "executor_report_file": executor_report_file,
            "executor_report_actor": executor_report_actor,
            "stderr_file": stderr_file,
            "terminal_session_id": terminal_session_id,
            "terminal_turn_start": terminal_turn_start,
            "app_server_thread_id": app_server_thread_id,
            "app_server_turn_id": app_server_turn_id,
            "app_server_process_id": app_server_process_id,
            "executor_windows_sandbox": executor_windows_sandbox,
            "executor_windows_sandbox_readiness": executor_windows_sandbox_readiness,
            "executor_approval_policy": executor_approval_policy,
            "executor_role": executor_role,
            "executor_provider": executor_provider,
            "executor_actor_id": executor_actor_id,
            "executor_configured_actor": executor_configured_actor,
            "executor_adapter": executor_adapter,
            "executor_backend": executor_backend,
            "executor_model": executor_model,
            "executor_reasoning_effort": executor_reasoning_effort,
            "primary_actor": primary_actor,
            "actual_actor": actual_actor,
            "fallback_enabled": fallback_enabled,
            "fallback_used": fallback_used,
            "failed_actor": failed_actor,
            "fallback_actor": fallback_actor,
            "fallback_reason": fallback_reason,
            "fallback_failure_class": fallback_failure_class,
            "executor_state_root_identity": executor_state_root_identity,
            "canonical_instructions_root": canonical_instructions_root,
            "canonical_bootstrap_required": canonical_bootstrap_required,
            "delegation_transport": delegation_transport,
            "antigravity_conversation_id": antigravity_conversation_id,
            "antigravity_terminal_status": antigravity_terminal_status,
            "task_transport": task_transport,
            "task_artifact": task_artifact,
            "task_sha256": task_sha256,
            "reuse_existing": reuse_existing,
            "reuse_provenance": dict(reuse_provenance or {"mode": "reuse_or_start"}),
            "error": error,
        }
    )


def _write_text(path: Path, value: str) -> None:
    path.write_text(sanitize_text(value), encoding="utf-8", newline="\n")


def _files_from_status(status: str) -> list[str]:
    files: list[str] = []
    for line in status.splitlines():
        if len(line) >= 4:
            files.append(line[3:].strip())
    return files


def _run_id(config: OrchestratorConfig, request: DelegationRequest) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = f"{stamp}-{request.request_id}"
    if (config.runs_dir / candidate).exists():
        candidate = f"{candidate}-{uuid4().hex[:8]}"
    return candidate


def _run_directory(config: OrchestratorConfig, request: DelegationRequest, run_id: str | None = None) -> Path:
    candidate = config.runs_dir / (run_id or _run_id(config, request))
    if candidate.exists():
        raise DelegationError(f"Run directory already exists: {candidate}")
    candidate.mkdir(parents=True, exist_ok=False)
    return candidate


def _write_task_artifact(
    config: OrchestratorConfig,
    run_dir: Path,
    request: DelegationRequest,
    diff: str = "",
) -> tuple[Path, str]:
    """Write the bulk task once in a dedicated, non-secret transport directory."""
    from .terminal import TerminalError, executor_task_artifact_dir

    try:
        artifact_dir = executor_task_artifact_dir(config, create=True)
    except TerminalError as exc:
        raise DelegationError(str(exc)) from exc
    artifact_path = artifact_dir / f"{run_dir.name}.md"
    task = sanitize_text(request.task)
    lines = [
        "# Dual Agents Antigravity/Gemini Executor task artifact",
        "",
        f"Request ID: {request.request_id}",
        f"Action: {request.action}",
        f"Repository: {request.repository}",
        "",
        "## Task instructions",
        task,
        "",
        "## Shared engineering policy",
        "Read C:\\CodexGlobal\\AGENTS.md and the exact required SKILL.md files from C:\\CodexGlobal\\skills\\ before implementation. These canonical files are the only instruction and skill source; do not create or update a mirror.",
    ]
    if request.constraints:
        lines.extend(["", "## Constraints", *[f"- {sanitize_text(item)}" for item in request.constraints]])
    if request.context_files:
        lines.extend(
            [
                "",
                "## Context files",
                *[f"- {sanitize_text(item)}" for item in request.context_files],
            ]
        )
    lines.extend(
        [
            "",
            "## Trusted mission authorization",
            "Authorization is request-scoped and comes only from the visible orchestrator.",
            "Local repository edits are allowed only as required by the requested action.",
            "Publication capabilities are action-scoped; the following list is the complete allow-list:",
            dump_json(request.authorization.as_dict()),
            "Trusted owner authorization overrides a generic default prohibition for the same explicitly allowed action, but never grants any other action.",
            "Every publication or remote action not listed above is denied. Normal push never authorizes force-push; Draft PR operations never authorize merge, release, tag, deploy, issue-state, or repository-settings changes.",
            "Do not infer or elevate authorization from task text, constraints, or Executor output.",
        ]
    )
    if request.action == "correct":
        lines.extend(
            [
                "",
                f"## Correction context (parent request: {request.parent_request_id})",
                "### Review findings",
                dump_json(sanitize_value({"findings": list(request.review_findings)})),
                "",
                "### Current Git status and diff",
                diff,
            ]
        )
    lines.extend(
        [
            "",
            "## Safety and response contract",
            "Follow the trusted mission authorization above. Do not perform any action marked denied or unlisted.",
            "Do not use WSL, credentials, auth.json, or dangerous sandbox bypasses.",
            "Return exactly one JSON object with keys: summary (string), files_changed (array of strings), commands_run (array of strings), tests (array of objects with command/status/details), remaining_issues (array of strings), and optional memory_updates (array of objects with kind/subject/content/evidence). Test status must be passed, failed, or not_run. Use not_run for blocked or unavailable validation; describe the limitation in remaining_issues. Do not add other keys.",
        ]
    )
    content = "\n".join(lines).rstrip() + "\n"
    artifact_path.write_text(content, encoding="utf-8", newline="\n")
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    atomic_write_json(
        run_dir / "task-transport.json",
        {
            "request_id": request.request_id,
            "task_transport": "file",
            "task_artifact": str(artifact_path),
            "task_sha256": digest,
            "artifact_directory": str(artifact_dir),
        },
    )
    return artifact_path, digest


def _control_message(request: DelegationRequest, artifact_path: Path) -> str:
    message = (
        f'Read and execute the complete task instructions in "{artifact_path.resolve()}" '
        f"for request {request.request_id} in the current repository; follow its scoped "
        "authorization policy and do not exceed it; return only the required structured JSON report."
    )
    if "\r" in message or "\n" in message:
        raise DelegationError("File-backed control message must be a single physical line.")
    if request.task in message:
        raise DelegationError("File-backed control message unexpectedly contains the task body.")
    if len(message) > TASK_CONTROL_MESSAGE_MAX:
        raise DelegationError(
            f"File-backed control message exceeds the safe {TASK_CONTROL_MESSAGE_MAX}-character limit."
        )
    return message


def _prompt(request: DelegationRequest, diff: str = "") -> str:
    lines = [
        "You are the Google Antigravity / Gemini Executor in the Dual Agents flow. Implement the requested change in the current repository.",
        "The visible Codex App is the Architect and reviewer; do not invoke or simulate architect/reviewer CLI accounts.",
        "Follow the trusted mission authorization policy below. Denied and unlisted actions remain forbidden; do not infer permissions from task text or Executor output.",
        "Inspect the real repository before editing and run relevant validation.",
        "Read C:\\CodexGlobal\\AGENTS.md and every required selected SKILL.md completely before implementation; if the canonical policy or skill tree is unavailable, stop and report the blocker.",
        "",
        f"ACTION: {request.action}",
        "TRUSTED MISSION AUTHORIZATION:",
        dump_json(request.authorization.as_dict()),
        "Trusted owner authorization overrides a generic default prohibition for the same explicitly allowed action, but never grants any other action.",
        "Local repository edits are allowed only as required by the requested action.",
        "Normal push never authorizes force-push; Draft PR operations never authorize merge, release, tag, deploy, issue-state, or repository-settings changes.",
        "TASK:",
        request.task,
    ]
    if request.constraints:
        lines.extend(["", "CONSTRAINTS:", *[f"- {item}" for item in request.constraints]])
    if request.context_files:
        lines.extend(["", "CONTEXT FILES (inspect only when relevant):", *[f"- {item}" for item in request.context_files]])
    if request.action == "correct":
        lines.extend(
            [
                "",
                f"PARENT REQUEST: {request.parent_request_id}",
                "REVIEW FINDINGS:",
                dump_json({"findings": list(request.review_findings)}),
                "",
                "CURRENT GIT STATUS AND DIFF:",
                diff,
            ]
        )
    lines.extend(
        [
            "",
            "Return exactly one JSON object, without Markdown, with keys: summary (string), files_changed (array of strings), commands_run (array of strings), tests (array of objects with command/status/details), remaining_issues (array of strings), and optional memory_updates (array of objects with kind/subject/content/evidence). Test status must be passed, failed, or not_run. Use not_run for blocked or unavailable validation; describe the limitation in remaining_issues. Do not write canonical Obsidian/LVault memory.",
        ]
    )
    return "\n".join(lines)


_REPORT_FIELDS = EXECUTOR_REPORT_FIELDS
_REPORT_TEST_STATUSES = {"passed", "failed", "not_run"}
_AUXILIARY_PROTOCOL_KEYS = frozenset({"toolAction", "toolSummary"})
_REPORT_SHAPE_KEYS = frozenset(
    {
        *EXECUTOR_REPORT_FIELDS,
        *EXECUTOR_REPORT_OPTIONAL_FIELDS,
        "status",
        "starting_sha",
        "final_sha",
        "behavior_changed",
        "validations_run",
        "validations_not_run",
        "remaining_limitations",
        "next_plan_tree_item",
        "push_result",
        "remote_result",
        "pr_result",
    }
)


def _validate_executor_report(value: Mapping[str, Any]) -> str:
    missing = sorted(_REPORT_FIELDS - set(value))
    extra = sorted(set(value) - (_REPORT_FIELDS | EXECUTOR_REPORT_OPTIONAL_FIELDS))
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing fields: {', '.join(missing)}")
        if extra:
            details.append(f"unknown fields: {', '.join(extra)}")
        return "; ".join(details)
    if not isinstance(value["summary"], str):
        return "summary must be a string"
    for field in ("files_changed", "commands_run", "remaining_issues"):
        items = value[field]
        if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
            return f"{field} must be an array of strings"
    tests = value["tests"]
    if not isinstance(tests, list):
        return "tests must be an array"
    for index, test in enumerate(tests):
        if not isinstance(test, Mapping):
            return f"tests[{index}] must be an object"
        if set(test) != {"command", "status", "details"}:
            return f"tests[{index}] must contain only command, status and details"
        if not all(isinstance(test[field], str) for field in ("command", "status", "details")):
            return f"tests[{index}] fields must be strings"
        if test["status"] not in _REPORT_TEST_STATUSES:
            return f"tests[{index}] has unsupported status '{test['status']}'"
    if "memory_updates" in value:
        updates = value["memory_updates"]
        if not isinstance(updates, list):
            return "memory_updates must be an array"
        allowed_kinds = {"decision", "architecture", "workflow", "constraint", "discovery"}
        for index, update in enumerate(updates):
            if not isinstance(update, Mapping) or set(update) != {"kind", "subject", "content", "evidence"}:
                return (
                    f"memory_updates[{index}] must contain only kind, subject, content and evidence"
                )
            if not all(isinstance(update[field], str) and update[field].strip() for field in update):
                return f"memory_updates[{index}] fields must be non-empty strings"
            if update["kind"] not in allowed_kinds:
                return f"memory_updates[{index}] has unsupported kind '{update['kind']}'"
    return ""


def _looks_like_executor_report(value: Mapping[str, Any]) -> bool:
    """Distinguish report-shaped JSON from unrelated protocol metadata."""

    return bool(set(value) & _REPORT_SHAPE_KEYS)


def _decode_concatenated_json(raw_text: str) -> tuple[list[Any], str]:
    """Decode JSON values embedded in a stream response.

    Antigravity can repeat the terminal report in its response text and may
    append a non-report tool metadata object.  Decode each complete value so
    equivalent reports can be deduplicated without accepting malformed
    report-shaped JSON.  Human text around the values is intentionally ignored.
    """

    decoder = json.JSONDecoder()
    values: list[Any] = []
    cursor = 0
    while cursor < len(raw_text):
        match = re.search(r"[\[{]", raw_text[cursor:])
        if match is None:
            break
        start = cursor + match.start()
        try:
            value, end = decoder.raw_decode(raw_text, start)
        except json.JSONDecodeError as exc:
            # A JSON-looking object after a valid value is a real protocol
            # candidate; fail closed instead of silently dropping it.  Braces
            # used as ordinary prose (for example, ``{placeholder}``) are not.
            tail = raw_text[start + 1 :].lstrip()
            looks_json = raw_text[start] == "{" and (not tail or tail[0] in '\"}')
            looks_json = looks_json or (
                raw_text[start] == "["
                and (not tail or tail[0] in '\"{[]-0123456789ntf')
            )
            if values and looks_json:
                return values, f"{exc}"
            cursor = start + 1
            continue
        values.append(value)
        cursor = end
    return values, ""


def _read_report(path: Path) -> tuple[dict[str, Any] | None, str]:
    if not path.exists():
        return None, "Executor did not produce a structured report."
    try:
        raw_text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        return None, f"Executor report could not be read: {exc}"
    parse_error = ""
    try:
        raw = json.loads(raw_text)
        values: list[Any] = [raw]
    except (UnicodeError, json.JSONDecodeError) as exc:
        values, parse_error = _decode_concatenated_json(raw_text)
        if parse_error:
            _write_text(path.with_suffix(".invalid.log"), raw_text)
            return None, f"Executor report is not valid JSON: {parse_error}"
        if not values:
            _write_text(path.with_suffix(".invalid.log"), raw_text)
            return None, f"Executor report is not valid JSON: {exc}"

    if len(values) == 1 and not isinstance(values[0], dict):
        return None, "Executor report must be a JSON object."

    reports: list[dict[str, Any]] = []
    schema_errors: list[str] = []
    unclassified_values = False
    for value in values:
        if not isinstance(value, dict):
            # Arrays and scalars cannot be authoritative executor reports.
            unclassified_values = True
            continue
        sanitized_value = sanitize_value(value)
        value_keys = set(sanitized_value)
        auxiliary_keys = value_keys & _AUXILIARY_PROTOCOL_KEYS
        candidate_value = {
            key: item
            for key, item in sanitized_value.items()
            if key not in _AUXILIARY_PROTOCOL_KEYS
        }
        sanitized = normalise_executor_report(candidate_value)
        validation_error = _validate_executor_report(sanitized)
        if not validation_error:
            reports.append(sanitized)
        elif _looks_like_executor_report(sanitized):
            schema_errors.append(validation_error)
        elif not (auxiliary_keys and value_keys.issubset(_AUXILIARY_PROTOCOL_KEYS)):
            # Unknown structured JSON is not silently accepted beside a valid
            # report. Only the narrow, known tool metadata shape is ignorable.
            unclassified_values = True

    if schema_errors:
        _write_text(path.with_suffix(".invalid.log"), raw_text)
        return None, f"Executor report schema validation failed: {schema_errors[0]}."
    if unclassified_values:
        _write_text(path.with_suffix(".invalid.log"), raw_text)
        return None, "Executor report contains unclassified structured JSON."
    if not reports:
        _write_text(path.with_suffix(".invalid.log"), raw_text)
        return None, "Executor report did not contain a valid structured report."

    canonical = reports[0]
    if any(report != canonical for report in reports[1:]):
        _write_text(path.with_suffix(".invalid.log"), raw_text)
        return None, "Executor report contains conflicting structured JSON results."
    atomic_write_json(path, canonical)
    return canonical, ""


def _report_list(report: Mapping[str, Any], name: str) -> list[Any]:
    value = report.get(name, [])
    return list(value) if isinstance(value, list) else []


_EXECUTOR_FAILURE_MARKERS = (
    "blocked",
    "rejected",
    "not applied",
    "could not apply",
    "could not be applied",
    "unable to apply",
    "failed to apply",
    "permission denied by sandbox",
    "no required changes",
    "no changes required",
    "nothing to change",
    "already complete",
    "already implemented",
)
_READ_ONLY_SUCCESS_MARKERS = (
    "dual_codex_skill_attestation_ok",
    "dual_codex_read_only_handshake_ok",
    "read-only probe completed",
)


def _read_only_validation_succeeded(report: Mapping[str, Any]) -> bool:
    """Accept only an explicit, no-mutation validation attestation."""
    if _report_list(report, "files_changed"):
        return False
    evidence = [
        str(report.get("summary", "")),
        *[str(item) for item in _report_list(report, "remaining_issues")],
    ]
    for test in _report_list(report, "tests"):
        if isinstance(test, Mapping):
            evidence.append(str(test.get("details", "")))
    text = "\n".join(evidence).casefold()
    return any(marker in text for marker in _READ_ONLY_SUCCESS_MARKERS)


def _app_server_tool_attestation_error(
    report: Mapping[str, Any] | None,
    command_result: CommandResult,
) -> str:
    """Reject an App Server report that claims commands without wire evidence.

    The assistant report is not proof that a tool ran. The App Server adapter
    records completed command items separately; when that metadata is present,
    require at least one successful item before accepting a report that claims
    command/test execution. Older non-App-Server test doubles may omit the
    metadata and retain their existing behavior.
    """

    if report is None or "app_server_tool_executions" not in command_result.metadata:
        return ""
    claimed_commands = _report_list(report, "commands_run")
    claimed_tests = [
        item
        for item in _report_list(report, "tests")
        if isinstance(item, Mapping) and str(item.get("command", "")).strip()
    ]
    if not claimed_commands and not claimed_tests:
        return ""
    executions = command_result.metadata.get("app_server_tool_executions")
    if not isinstance(executions, list):
        executions = []
    successful = any(
        isinstance(item, Mapping)
        and str(item.get("status", "")).casefold() == "completed"
        and (
            item.get("exitCode") == 0
            or item.get("success") is True
        )
        for item in executions
    )
    if successful:
        custom_outputs = command_result.metadata.get("app_server_custom_tool_outputs")
        if isinstance(custom_outputs, list) and any(
            isinstance(item, Mapping) and item.get("success") is False
            for item in custom_outputs
        ):
            return "App Server returned a failed custom-tool output; the report cannot be accepted as a successful execution."
        return ""
    return (
        "App Server executor report claims command/test execution, but the App Server "
        "notification stream contains no completed successful tool execution."
    )


def _classify_executor_result(
    *,
    report: Mapping[str, Any] | None,
    report_error: str,
    changed: list[str],
    command_result: CommandResult,
    repository_unchanged: bool = False,
) -> tuple[str, str, str]:
    """Classify execution by its repository effect and report semantics."""
    if command_result.returncode != 0:
        return (
            "failed",
            "Codex executor failed; repository modifications, if any, were preserved.",
            f"Executor exited with code {command_result.returncode}.",
        )
    if report is None:
        return (
            "failed",
            "Codex executor completed without a valid structured report.",
            report_error,
        )

    summary = str(report.get("summary", "Executor completed."))
    remaining_issues = _report_list(report, "remaining_issues")
    tests = _report_list(report, "tests")
    semantic_parts = [summary, *[str(item) for item in remaining_issues]]
    semantic_text = "\n".join(semantic_parts).casefold()
    if any(marker in semantic_text for marker in _EXECUTOR_FAILURE_MARKERS):
        return (
            "failed",
            summary,
            "Executor report indicates that the requested change was not applied.",
        )

    for test in tests:
        if not isinstance(test, Mapping):
            return "failed", summary, "Executor report contains an invalid test entry."
        test_status = str(test.get("status", "")).casefold()
        if test_status in {"failed", "not_run"}:
            return "failed", summary, f"Executor reported a test with status '{test_status}'."
        if test_status != "passed":
            return "failed", summary, f"Executor reported an unsupported test status '{test_status}'."

    if repository_unchanged and not report_error and _read_only_validation_succeeded(report):
        return "completed", summary, report_error

    if not changed:
        return (
            "failed",
            summary,
            "Executor produced a valid report but did not apply any repository changes.",
        )

    return "completed", summary, report_error


def _capture_git(repository: Path, run_dir: Path) -> tuple[str, str, list[str]]:
    status = status_porcelain(repository)
    diff = status_and_diff(repository)
    diff_path = run_dir / "git-diff.md"
    _write_text(diff_path, _safe_diff(diff))
    return status, str(diff_path), _files_from_status(status)


def _emit(output: Callable[[str], None], started: float, message: str) -> None:
    rendered = f"{message} (elapsed {time.monotonic() - started:.1f}s)"
    if output is print:
        print(rendered, flush=True)
    else:
        output(rendered)


def _failed_outcome(
    *,
    result_file: Path,
    request_id: str,
    started_at: str,
    started: float,
    status: str,
    summary: str,
    error: str,
    repository: str = "",
    executor_account: str = "",
    executor_label: str = "",
    executor_sandbox: str = "",
    exit_code: int | None = None,
    parent_request_id: str | None = None,
    reuse_existing: bool = False,
) -> DelegationOutcome:
    finished_at = _timestamp()
    atomic_write_json(
        result_file,
        _result(
            request_id=request_id,
            status=status,
            started_at=started_at,
            finished_at=finished_at,
            summary=summary,
            repository=repository,
            executor_account=executor_account,
            executor_label=executor_label,
            executor_sandbox=executor_sandbox,
            primary_actor=executor_account,
            actual_actor=executor_account,
            exit_code=exit_code,
            parent_request_id=parent_request_id,
            reuse_existing=reuse_existing,
            error=error,
        ),
    )
    return DelegationOutcome(status, request_id, result_file, None, time.monotonic() - started)


def delegate(
    config: OrchestratorConfig,
    *,
    result_file: Path,
    request_file: Path | None = None,
    stdin_text: str | None = None,
    repository_override: str | None = None,
    allow_dirty: bool = False,
    reuse_existing: bool = False,
    output: Callable[[str], None] = print,
) -> DelegationOutcome:
    result_file = result_file.expanduser().resolve()
    started = time.monotonic()
    started_at = _timestamp()
    request_text = ""
    if request_file is not None:
        try:
            request_text = request_file.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            return _failed_outcome(
                result_file=result_file,
                request_id="invalid-" + uuid4().hex[:12],
                started_at=started_at,
                started=started,
                status="invalid_request",
                summary="Delegation request could not be read.",
                error=str(exc),
            )
    elif stdin_text is not None:
        request_text = stdin_text
    try:
        request = load_request(
            config,
            request_file=None if stdin_text is not None else request_file,
            stdin_text=stdin_text,
            repository_override=repository_override,
        )
    except Exception as exc:
        return _failed_outcome(
            result_file=result_file,
            request_id=_request_id_hint(request_text),
            started_at=started_at,
            started=started,
            status="invalid_request",
            summary="Delegation request validation failed.",
            error=sanitize_text(str(exc)),
        )

    request_id = request.request_id
    executor_account = ""
    executor_label = ""
    executor_sandbox = ""
    run_journal: LiveEventJournal | None = None
    run_id = ""
    try:
        _emit(output, started, "[1/5] Validating request")
        try:
            agent = config.agent_for_role("executor")
        except ConfigError as exc:
            return _failed_outcome(
                result_file=result_file,
                request_id=request_id,
                started_at=started_at,
                started=started,
                status="executor_unavailable",
                summary="The executor role is unassigned.",
                error=str(exc),
                repository=str(request.repository),
                parent_request_id=request.parent_request_id,
                reuse_existing=reuse_existing,
            )
        executor_account = agent.account_name
        executor_label = agent.label
        executor_sandbox = agent.sandbox
        _emit(output, started, f"[2/5] Resolving executor account: {executor_account}")
        from .providers import supported_roles_for_backend

        if "executor" not in supported_roles_for_backend(agent.backend):
            return _failed_outcome(
                result_file=result_file,
                request_id=request_id,
                started_at=started_at,
                started=started,
                status="executor_unavailable",
                summary="Configured Executor backend is not eligible for workspace-write dispatch.",
                error=(
                    f"Configured Executor backend '{agent.backend}' is not eligible for Executor dispatch."
                ),
                executor_account=executor_account,
                executor_label=executor_label,
                executor_sandbox=executor_sandbox,
                parent_request_id=request.parent_request_id,
                repository=str(request.repository),
                reuse_existing=reuse_existing,
            )
        antigravity_command = getattr(config, "antigravity_command", "agy")
        if agent.backend == "antigravity" and antigravity_status(antigravity_command, cwd=config.project_root) != "OK":
            return _failed_outcome(
                result_file=result_file,
                request_id=request_id,
                started_at=started_at,
                started=started,
                status="executor_unavailable",
                summary="The Antigravity/Gemini Executor is unavailable or cannot be reached.",
                error=(
                    "Antigravity executable is unavailable or failed its version probe: "
                    f"{antigravity_command}"
                ),
                executor_account=executor_account,
                executor_label=executor_label,
                executor_sandbox=executor_sandbox,
                parent_request_id=request.parent_request_id,
                repository=str(request.repository),
                reuse_existing=reuse_existing,
            )
        output(f"Target repository: {request.repository}")
        run_id = _run_id(config, request)
        with RepositoryLock(config.runs_dir, request.repository, request.request_id, run_id):
            ensure_git_repository(request.repository)
            from .terminal import reconcile_deferred_task_artifact_cleanup

            reconcile_deferred_task_artifact_cleanup(config, request.repository)
            initial_git_status = status_porcelain(request.repository)
            dirty = bool(initial_git_status.strip())
            if dirty:
                if config.require_clean_git and not allow_dirty:
                    raise DelegationError(
                        "Target repository has uncommitted changes. Commit/stash them or use "
                        "--allow-dirty / require_clean_git = false explicitly."
                    )
                output("[warning] Target repository is dirty; continuing by explicit policy.")
            run_dir = _run_directory(config, request, run_id)
            try:
                run_journal = LiveEventJournal(
                    config.runs_dir,
                    account=executor_account,
                    role="executor",
                    repository=request.repository,
                    run_id=run_id,
                    request_id=request.request_id,
                    max_records=config.live_event_journal_max_records,
                    max_record_bytes=config.live_event_journal_max_record_bytes,
                    max_detail_bytes=config.live_event_journal_max_detail_bytes,
                )
            except (OSError, ValueError):
                # Observability is best-effort; delegation and its safety gates remain authoritative.
                run_journal = None
            _publish_run_event(
                run_journal,
                method="run/started",
                state="started",
                detail={"request_id": request.request_id, "started_at": started_at},
            )
            atomic_write_json(run_dir / "request.json", sanitize_value(request.as_dict()))
            initial_diff = _safe_diff(status_and_diff(request.repository)) if request.action == "correct" else ""
            initial_head = head_revision(request.repository)
            task_artifact, task_sha256 = _write_task_artifact(config, run_dir, request, initial_diff)
            control_message = _control_message(request, task_artifact)
            executor_prompt = control_message
            if agent.backend == "app_server":
                executor_prompt = task_artifact.read_text(encoding="utf-8")
            output(f"Run directory: {run_dir}")
            _emit(
                output,
                started,
                f"[3/5] Starting executor: {executor_account} (sandbox={executor_sandbox})",
            )
            def _run_executor_once(selected_config, selected_agent, attempt: int) -> tuple[CommandResult, Path]:
                attempt_report = run_dir / f"executor-report-attempt-{attempt}-{selected_agent.account_name}.json"
                attempt_report.unlink(missing_ok=True)
                try:
                    result = run_codex_exec(
                        config=selected_config,
                        agent=selected_agent,
                        repository=request.repository,
                        prompt=executor_prompt,
                        output_path=attempt_report,
                        schema_path=config.project_root / "schemas" / "delegation-report.schema.json",
                        check=False,
                        task_artifact_path=task_artifact,
                        task_sha256=task_sha256,
                        request_id=request.request_id,
                        run_id=run_dir.name,
                        role="executor",
                        conversation_id=request.antigravity_conversation_id,
                        progress=lambda message: _emit(output, started, f"[3/5] {message}"),
                        reuse_existing=reuse_existing,
                    )
                except (OSError, CommandError) as exc:
                    result = CommandResult(
                        [selected_agent.backend],
                        1,
                        "",
                        sanitize_text(str(exc)),
                        {"availability_failure_class": "process_unavailable" if isinstance(exc, OSError) else "provider_runtime_unavailable"},
                    )
                return result, attempt_report

            command_result, report_path = _run_executor_once(config, agent, 1)
            report_actor = agent.account_name
            primary_actor = agent.account_name
            fallback_enabled = bool(getattr(config, "fallback_enabled", False))
            fallback_used = False
            failed_actor = ""
            fallback_reason = ""
            fallback_failure_class = ""
            failure_class = classify_actor_failure(command_result, backend=agent.backend)
            if command_result.returncode != 0 and failure_class and fallback_enabled:
                candidates = [
                    account_name for account_name in sorted(config.accounts)
                    if account_name != primary_actor
                    and config.accounts[account_name].enabled
                    and "executor" in config.accounts[account_name].fallback_roles
                    and provider_supports_role(config, config.accounts[account_name], "executor")
                ]
                if candidates:
                    failed_actor = primary_actor
                    fallback_reason = command_result.stderr[:500]
                    fallback_failure_class = failure_class
                    fallback_name = candidates[0]
                    fallback_config = replace(config, roles={**config.roles, "executor": fallback_name})
                    fallback_agent = fallback_config.agent_for_role("executor")
                    command_result, report_path = _run_executor_once(fallback_config, fallback_agent, 2)
                    report_actor = fallback_agent.account_name
                    fallback_used = True
                    agent = fallback_agent
                    executor_account = agent.account_name
                    executor_label = agent.label
                    executor_sandbox = agent.sandbox
            command_result.metadata.update({
                "primary_actor": primary_actor,
                "actual_actor": agent.account_name,
                "fallback_enabled": fallback_enabled,
                "fallback_used": fallback_used,
                "failed_actor": failed_actor,
                "fallback_actor": agent.account_name if fallback_used else "",
                "fallback_reason": fallback_reason,
                "fallback_failure_class": fallback_failure_class,
            })
            try:
                canonical_root = canonical_instructions_root()
            except OSError:
                canonical_root = None
            command_result.metadata.update(
                configured_actor_provenance(
                    agent=agent,
                    role="executor",
                    repository=request.repository,
                    canonical_root=canonical_root,
                )
            )
            command_result.metadata.update({
                "primary_actor": primary_actor,
                "actual_actor": agent.account_name,
                "fallback_enabled": fallback_enabled,
                "fallback_used": fallback_used,
                "failed_actor": failed_actor,
                "fallback_actor": agent.account_name if fallback_used else "",
                "fallback_reason": fallback_reason,
                "fallback_failure_class": fallback_failure_class,
            })
            stdout_path = run_dir / "executor.stdout.log"
            stderr_path = run_dir / "executor.stderr.log"
            _write_text(stdout_path, command_result.stdout)
            _write_text(stderr_path, command_result.stderr)
            report, report_error = _read_report(report_path)
            accepted_report_actor = report_actor if report is not None else ""
            _emit(output, started, "[4/5] Capturing diff and validation results")
            try:
                git_status, diff_file, changed = _capture_git(request.repository, run_dir)
                head_changed = bool(initial_head and head_revision(request.repository) != initial_head)
            except Exception as exc:
                git_status, diff_file, changed = "unavailable", "", []
                head_changed = False
                report_error = f"{report_error} Git capture failed: {exc}".strip()
            status, summary, error = _classify_executor_result(
                report=report,
                report_error=report_error,
                changed=changed,
                command_result=command_result,
                repository_unchanged=git_status == initial_git_status,
            )
            if status == "completed" and agent.backend == "app_server":
                tool_attestation_error = _app_server_tool_attestation_error(report, command_result)
                if tool_attestation_error:
                    status = "failed"
                    error = tool_attestation_error
            if head_changed:
                status = "failed"
                error = (
                    f"{error} Executor changed Git HEAD; no rollback was attempted."
                ).strip()
            remaining_issues = _report_list(report or {}, "remaining_issues")
            if head_changed:
                remaining_issues.append("Git HEAD changed during delegation; inspect the commit manually.")
            finished_at = _timestamp()
            result = _result(
                request_id=request.request_id,
                status=status,
                started_at=started_at,
                finished_at=finished_at,
                summary=summary,
                repository=str(request.repository),
                executor_account=executor_account,
                executor_label=executor_label,
                executor_sandbox=executor_sandbox,
                exit_code=command_result.returncode,
                parent_request_id=request.parent_request_id,
                files_changed=_report_list(report or {}, "files_changed") or changed,
                commands_run=_report_list(report or {}, "commands_run"),
                tests=_report_list(report or {}, "tests"),
                remaining_issues=remaining_issues,
                memory_updates=_report_list(report or {}, "memory_updates"),
                git_status=git_status,
                diff_file=diff_file,
                run_directory=str(run_dir),
                executor_report_file=str(report_path) if report_path.exists() else "",
                executor_report_actor=accepted_report_actor,
                stderr_file=str(stderr_path),
                terminal_session_id=command_result.metadata.get("terminal_session_id", ""),
                terminal_turn_start=command_result.metadata.get("terminal_turn_start", ""),
                app_server_thread_id=command_result.metadata.get("app_server_thread_id", ""),
                app_server_turn_id=command_result.metadata.get("app_server_turn_id", ""),
                app_server_process_id=command_result.metadata.get("app_server_process_id", ""),
                executor_windows_sandbox=command_result.metadata.get("app_server_windows_sandbox", ""),
                executor_windows_sandbox_readiness=command_result.metadata.get(
                    "app_server_windows_sandbox_readiness", ""
                ),
                executor_approval_policy=command_result.metadata.get("app_server_approval_policy", ""),
                executor_role=command_result.metadata.get(
                    "app_server_role",
                    "executor"
                    if command_result.metadata.get("executor_provider") == "antigravity"
                    else "",
                ),
                executor_provider=command_result.metadata.get("executor_provider", "")
                or command_result.metadata.get("provider", "")
                or ("antigravity" if agent.backend == "antigravity" else ""),
                executor_actor_id=command_result.metadata.get("actor_id", executor_account),
                executor_configured_actor=bool(command_result.metadata.get("configured_actor", False)),
                executor_adapter=command_result.metadata.get("adapter", ""),
                executor_backend=command_result.metadata.get("backend", agent.backend),
                executor_model=command_result.metadata.get("model", agent.model),
                executor_reasoning_effort=command_result.metadata.get("reasoning_effort", agent.reasoning_effort),
                primary_actor=command_result.metadata.get("primary_actor", executor_account),
                actual_actor=command_result.metadata.get("actual_actor", executor_account),
                fallback_enabled=bool(command_result.metadata.get("fallback_enabled", False)),
                fallback_used=bool(command_result.metadata.get("fallback_used", False)),
                failed_actor=command_result.metadata.get("failed_actor", ""),
                fallback_actor=command_result.metadata.get("fallback_actor", ""),
                fallback_reason=command_result.metadata.get("fallback_reason", ""),
                fallback_failure_class=command_result.metadata.get("fallback_failure_class", ""),
                executor_state_root_identity=command_result.metadata.get("state_root_identity", ""),
                canonical_instructions_root=command_result.metadata.get("canonical_instructions_root", ""),
                canonical_bootstrap_required=bool(
                    command_result.metadata.get("canonical_bootstrap_required", False)
                ),
                delegation_transport=command_result.metadata.get("delegation_transport", "antigravity"),
                antigravity_conversation_id=command_result.metadata.get(
                    "antigravity_conversation_id", ""
                ),
                antigravity_terminal_status=command_result.metadata.get(
                    "antigravity_terminal_status", ""
                ),
                task_transport=command_result.metadata.get("task_transport", "file"),
                task_artifact=command_result.metadata.get("task_artifact", str(task_artifact)),
                task_sha256=command_result.metadata.get("task_sha256", task_sha256),
                reuse_existing=reuse_existing,
                reuse_provenance=command_result.metadata.get("reuse_provenance", {}),
                error=error,
            )
            terminal_state = "completed" if status == "completed" else "cancelled" if status == "cancelled" else "failed"
            _publish_run_event(
                run_journal,
                method=f"run/{terminal_state}",
                state=terminal_state,
                detail={
                    "request_id": request.request_id,
                    "status": status,
                    "started_at": started_at,
                    "ended_at": finished_at,
                    "reason": error or summary,
                },
                thread_id=command_result.metadata.get("app_server_thread_id", ""),
                turn_id=command_result.metadata.get("app_server_turn_id", ""),
            )
            _emit(output, started, "[5/5] Writing result")
            atomic_write_json(result_file, result)
            return DelegationOutcome(status, request_id, result_file, run_dir, time.monotonic() - started)
    except KeyboardInterrupt:
        finished_at = _timestamp()
        _publish_run_event(
            run_journal,
            method="run/cancelled",
            state="cancelled",
            detail={"request_id": request_id, "started_at": started_at, "ended_at": finished_at, "reason": "Interrupted by user."},
        )
        return _failed_outcome(
            result_file=result_file,
            request_id=request_id,
            started_at=started_at,
            started=started,
            status="cancelled",
            summary="Delegation cancelled; repository modifications, if any, were preserved.",
            error="Interrupted by user.",
            executor_account=executor_account,
            executor_label=executor_label,
            executor_sandbox=executor_sandbox,
            parent_request_id=request.parent_request_id,
            repository=str(request.repository),
            reuse_existing=reuse_existing,
        )
    except (ConfigError, DelegationError, OSError, ValueError) as exc:
        finished_at = _timestamp()
        _publish_run_event(
            run_journal,
            method="run/failed",
            state="failed",
            detail={"request_id": request_id, "started_at": started_at, "ended_at": finished_at, "reason": sanitize_text(str(exc))},
        )
        return _failed_outcome(
            result_file=result_file,
            request_id=request_id,
            started_at=started_at,
            started=started,
            status="failed",
            summary="Delegation did not start or did not finish safely.",
            error=sanitize_text(str(exc)),
            executor_account=executor_account,
            executor_label=executor_label,
            executor_sandbox=executor_sandbox,
            parent_request_id=request.parent_request_id,
            repository=str(request.repository),
            reuse_existing=reuse_existing,
        )
