from __future__ import annotations

from collections import deque
import atexit
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from .config import AgentConfig, OrchestratorConfig
from .live_events import LiveEventJournal
from .paths import path_identity_key
from .process import CommandResult, _prepare_command, codex_environment, executor_npm_cache
from .report import (
    atomic_write_json,
    is_executor_report_shape,
    normalise_executor_report,
)


class AppServerError(RuntimeError):
    """Raised when the local Codex App Server cannot complete a safe request."""

    def __init__(
        self,
        message: str,
        *,
        failure_class: str = "",
        termination_classification: str = "",
    ):
        super().__init__(message)
        self.failure_class = failure_class
        self.termination_classification = termination_classification


def _effective_turn_timeout(agent: AgentConfig, config: OrchestratorConfig) -> tuple[float, str]:
    override = getattr(agent, "app_server_turn_timeout", None)
    if override is not None:
        return float(override), "account_role_override"
    return float(config.app_server_turn_timeout), "global_default"


_EVENT_PUBLICATION_QUEUE_SIZE = 64
_HEADLESS_RAW_EVENTS_VERSION = "responses-raw-v1"
_WINDOWS_SANDBOX_MODES = {"elevated", "unelevated"}


def _canonical_workspace_roots(*roots: Path) -> list[str]:
    """Return deduplicated, absolute runtime roots for one managed request."""

    result: list[str] = []
    seen: set[str] = set()
    for value in roots:
        root = value.expanduser().resolve(strict=False)
        key = path_identity_key(root)
        if key in seen:
            continue
        seen.add(key)
        result.append(str(root))
    return result


def _thread_binding(response: Mapping[str, Any]) -> dict[str, Any]:
    """Extract sanitized cwd/root identity from a thread response."""

    result = response.get("result")
    if not isinstance(result, Mapping):
        return {}
    thread = result.get("thread")
    thread = thread if isinstance(thread, Mapping) else {}
    environments = thread.get("environments")
    sanitized_environments: list[dict[str, Any]] = []
    if isinstance(environments, list):
        for environment in environments:
            if not isinstance(environment, Mapping):
                continue
            roots = environment.get("runtimeWorkspaceRoots")
            sanitized_environments.append(
                {
                    "environmentId": str(environment.get("environmentId", "")),
                    "cwd": str(environment.get("cwd", "")),
                    "runtimeWorkspaceRoots": [str(root) for root in roots] if isinstance(roots, list) else [],
                }
            )
    roots = result.get("runtimeWorkspaceRoots")
    return {
        "cwd": str(result.get("cwd", "")),
        "runtimeWorkspaceRoots": [str(root) for root in roots] if isinstance(roots, list) else [],
        "environments": sanitized_environments,
    }


def _validate_thread_binding(response: Mapping[str, Any], repository: Path) -> dict[str, Any]:
    """Fail closed unless the App Server confirms the managed repository binding."""

    expected = _canonical_workspace_roots(repository)
    binding = _thread_binding(response)
    returned_roots = binding.get("runtimeWorkspaceRoots")
    if not isinstance(returned_roots, list) or len(returned_roots) != len(expected):
        raise AppServerError("App Server did not return runtimeWorkspaceRoots for the managed repository.")
    if any(path_identity_key(actual) != path_identity_key(wanted) for actual, wanted in zip(returned_roots, expected)):
        raise AppServerError("App Server returned runtimeWorkspaceRoots for a different repository.")
    if path_identity_key(binding.get("cwd", "")) != path_identity_key(expected[0]):
        raise AppServerError("App Server returned a different thread cwd than the managed repository.")
    environments = binding.get("environments")
    if not isinstance(environments, list) or not environments:
        raise AppServerError("App Server did not return an environment binding for the managed repository.")
    matching = [
        environment
        for environment in environments
        if isinstance(environment, Mapping)
        and path_identity_key(environment.get("cwd", "")) == path_identity_key(expected[0])
        and [path_identity_key(root) for root in environment.get("runtimeWorkspaceRoots", [])] == [
            path_identity_key(expected[0])
        ]
    ]
    if not matching:
        raise AppServerError("App Server environment roots do not match the managed repository.")
    return binding


def _profile_config_identity(agent: AgentConfig) -> str:
    """Invalidate persistent App Server processes when profile config changes."""

    import hashlib

    path = agent.codex_home.expanduser() / "config.toml"
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "missing"


def _app_server_command(
    config: OrchestratorConfig,
    *,
    agent: AgentConfig | None = None,
    role: str = "",
) -> list[str]:
    """Build the non-interactive App Server command.

    The account-isolated CODEX_HOME owns the effective Windows sandbox
    configuration. Omitting --code-mode-host keeps the normal process-owned
    local CodeMode host; the isolated child environment prevents desktop
    bridge state from selecting a foreign host.
    """

    command = [config.codex_command, "app-server"]
    # `unelevated` is an emergency fallback with environment-level offline
    # controls. The Executor needs the supported Windows sandbox for Node's
    # worker and child-process validation; workspaceWrite still supplies the
    # exact filesystem/network boundary on each command and turn.
    if (
        agent is not None
        and role == "executor"
        and os.name == "nt"
        and agent.sandbox == "workspace-write"
    ):
        command.extend(["-c", 'windows.sandbox="elevated"'])
    command.append("--stdio")
    return command


def _report_object(message: str) -> dict[str, Any] | None:
    candidates = [message.strip()]
    if "```" in message:
        candidates.extend(
            part.strip()
            for part in message.split("```")
            if part.strip() and not part.strip().startswith("json")
        )
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            return value
    return None


def _normalise_report(message: str) -> str:
    """Keep App Server reports compatible with the existing delegation schema."""
    value = _report_object(message)
    if value is None:
        return message
    canonical = normalise_executor_report(value)
    if is_executor_report_shape(canonical):
        return json.dumps(canonical, ensure_ascii=False)
    tests = value.get("tests")
    if not (
        isinstance(tests, dict)
        or "status" in value
        or "commit_created" in value
        or "dependencies_added" in value
    ):
        return message
    if any(
        name in value and not isinstance(value[name], list)
        for name in ("files_changed", "commands_run", "remaining_issues")
    ):
        return message
    if (
        not isinstance(value.get("summary"), str)
        or not isinstance(value.get("files_changed"), list)
        or not isinstance(value.get("remaining_issues"), list)
    ):
        return message
    normalised_tests: list[dict[str, str]] = []
    if isinstance(tests, dict):
        command = str(tests.get("command", ""))
        result = str(tests.get("result", "")).casefold()
        status = "passed" if result == "passed" else "failed"
        details = f"result={result or 'unknown'}; exit_code={tests.get('exit_code', 'unknown')}; tests_run={tests.get('tests_run', 'unknown')}"
        normalised_tests.append({"command": command, "status": status, "details": details})
    elif isinstance(tests, list):
        for item in tests:
            if (
                not isinstance(item, dict)
                or set(item) != {"command", "status", "details"}
                or not all(isinstance(item[field], str) for field in ("command", "status", "details"))
            ):
                return message
            normalised_tests.append(dict(item))
    normalised = {
        "summary": value["summary"],
        "files_changed": value["files_changed"],
        "commands_run": value.get("commands_run", []),
        "tests": normalised_tests,
        "remaining_issues": value["remaining_issues"],
    }
    return json.dumps(normalised, ensure_ascii=False)


def _json_error(message: str) -> str:
    return message.replace("\r", " ").replace("\n", " ")[:500]


def _workspace_write_sandbox_policy(
    repository: Path,
    *,
    network_access: bool = False,
    npm_cache: Path | None = None,
) -> dict[str, Any]:
    root = repository.resolve()
    writable_roots = [str(root)]
    git_dir = root / ".git"
    if git_dir.exists():
        writable_roots.append(str(git_dir))
    if npm_cache is not None:
        writable_roots.append(str(npm_cache.resolve()))
    return {
        "type": "workspaceWrite",
        "networkAccess": bool(network_access),
        "writableRoots": writable_roots,
    }


def _error_message(response: dict[str, Any]) -> str:
    error = response.get("error")
    if isinstance(error, dict):
        return _json_error(str(error.get("message") or error))
    return _json_error(str(error or "App Server request failed."))


def _windows_sandbox_setup_command(agent: AgentConfig) -> str:
    return (
        "codex sandbox setup --elevated --current-user --codex-home "
        f'"{agent.codex_home.expanduser().resolve()}"'
    )


def _raw_response_item_evidence(item: Any) -> dict[str, Any] | None:
    """Keep only the raw Responses items needed to reconcile tool output.

    ``experimentalRawEvents`` also emits prompts and encrypted reasoning. Those
    are deliberately not retained by the adapter; the App Server journal needs
    the tool call identity and output only.
    """

    if not isinstance(item, Mapping):
        return None
    item_type = str(item.get("type", ""))
    if item_type not in {"custom_tool_call", "custom_tool_call_output"}:
        return {
            "type": item_type,
            "id": _json_error(str(item.get("id", ""))),
        }
    evidence: dict[str, Any] = {
        "type": item_type,
        "id": _json_error(str(item.get("id", ""))),
        "call_id": _json_error(str(item.get("call_id", ""))),
    }
    for key in ("name", "namespace"):
        value = item.get(key)
        if isinstance(value, str) and value:
            evidence[key] = _json_error(value)
    if item_type == "custom_tool_call_output":
        output = item.get("output")
        if isinstance(output, list):
            safe_output: list[dict[str, str]] = []
            for content in output[:16]:
                if not isinstance(content, Mapping):
                    continue
                content_type = str(content.get("type", ""))
                text = content.get("text")
                if content_type == "input_text" and isinstance(text, str):
                    safe_output.append({"type": content_type, "text": _json_error(text)})
                elif content_type in {"input_image", "input_audio"}:
                    safe_output.append({"type": content_type, "text": "[REDACTED_MEDIA]"})
            evidence["output"] = safe_output
            rendered_output = " ".join(item["text"] for item in safe_output)
            if re.search(r"(?i)exit\s+code\s*:\s*0\b", rendered_output):
                evidence["success"] = True
            elif re.search(r"(?i)(?:script\s+failed|exit\s+code\s*:\s*[1-9]\d*|\"error\"\s*:)", rendered_output):
                evidence["success"] = False
    return evidence


def _tool_item_evidence(item: Any) -> dict[str, Any] | None:
    """Extract bounded, non-reasoning evidence for a completed tool item."""

    if not isinstance(item, Mapping):
        return None
    item_type = str(item.get("type", ""))
    if item_type not in {"commandExecution", "mcpToolCall", "dynamicToolCall"}:
        return None
    evidence: dict[str, Any] = {
        "type": item_type,
        "id": _json_error(str(item.get("id", ""))),
        "status": _json_error(str(item.get("status", ""))),
    }
    for key in ("cwd", "command", "server", "tool", "exitCode", "durationMs", "success"):
        value = item.get(key)
        if isinstance(value, (str, int, bool)):
            evidence[key] = _json_error(value) if isinstance(value, str) else value
    actions = item.get("commandActions")
    if isinstance(actions, list) and actions:
        first = actions[0]
        if isinstance(first, Mapping) and isinstance(first.get("command"), str):
            evidence["command"] = _json_error(first["command"])
    output = item.get("aggregatedOutput")
    if isinstance(output, str):
        evidence["aggregatedOutput"] = _sanitize_stderr(output)
    content_items = item.get("contentItems")
    if isinstance(content_items, list):
        evidence["contentItems"] = [
            {
                "type": str(content.get("type", "")),
                "text": _json_error(str(content.get("text", ""))),
            }
            for content in content_items[:16]
            if isinstance(content, Mapping) and content.get("type") in {"inputText", "input_text"}
        ]
    return evidence


_AUTH_PATH = re.compile(r"(?i)(?:[A-Za-z]:)?[^\r\n\s\"']*auth\.json")
_SECRET = re.compile(
    r"(?ix)(authorization\s*:\s*bearer\s+|\b(?:token|api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)\b\"?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,}]+)"
)


def _sanitize_stderr(value: str) -> str:
    value = _AUTH_PATH.sub("[REDACTED_AUTH_PATH]", str(value))
    return _SECRET.sub(
        lambda match: f"{match.group(0).split(':', 1)[0].split('=', 1)[0]}=[REDACTED]",
        value,
    )[-8000:]


def _provider_turn_failure_class(turn: Mapping[str, Any]) -> str:
    status = str(turn.get("status", "")).casefold()
    error = turn.get("error")
    details = [status]
    if isinstance(error, Mapping):
        details.extend(str(error.get(key, "")) for key in ("code", "type", "message"))
    elif isinstance(error, str):
        details.append(error)
    return "PROVIDER_TURN_TIMEOUT" if re.search(r"(?:timed?\s*out|timeout|deadline)", " ".join(details)) else "APP_SERVER_PROVIDER_ERROR"


_SAFE_PROVIDER_ERROR_TOKENS = {
    "provider_timeout",
    "model_timeout",
    "timeout",
    "deadline_exceeded",
    "rate_limit",
    "rate_limited",
    "overloaded",
    "server_error",
    "internal_error",
    "invalid_request",
    "authentication_error",
    "context_length_exceeded",
    "cancelled",
    "canceled",
    "failed",
    "error",
}


def _safe_turn_failure_reason(exc: BaseException, classification: str, exit_code: int | None) -> str:
    if classification == "APP_SERVER_PROCESS_EXIT":
        suffix = f" (exit code {exit_code})" if exit_code is not None else ""
        return f"App Server process exited unexpectedly{suffix}."
    if classification == "APP_SERVER_TRANSPORT_EOF":
        return "App Server stdout closed unexpectedly."
    if classification == "HOST_TURN_DEADLINE":
        return " ".join(_sanitize_stderr(str(exc)).split())[:500]
    if classification == "APP_SERVER_TRANSPORT_TIMEOUT":
        return " ".join(_sanitize_stderr(str(exc)).split())[:500]
    if classification == "APP_SERVER_ERROR_EVENT":
        return " ".join(_sanitize_stderr(str(exc)).split())[:500]
    if classification == "PROVIDER_TURN_TIMEOUT":
        return " ".join(_sanitize_stderr(str(exc)).split())[:500]
    if classification == "APP_SERVER_PROVIDER_ERROR":
        return " ".join(_sanitize_stderr(str(exc)).split())[:500]
    if classification == "APP_SERVER_TURN_START_ERROR":
        return " ".join(_sanitize_stderr(str(exc)).split())[:500]
    return f"{type(exc).__name__} ({classification})."


def _finalize_turn_failure(process: Any, exc: BaseException) -> dict[str, Any] | None:
    record = getattr(process, "last_turn_provenance", None)
    if not isinstance(record, Mapping) or not record:
        return None
    finalized = dict(record)
    if finalized.get("termination_classification") == "TURN_COMPLETED":
        return finalized
    process_object = getattr(process, "process", None)
    try:
        process_exit_code = process_object.poll() if process_object is not None else None
    except Exception:
        process_exit_code = None
    classification = str(finalized.get("termination_classification") or "")
    if not classification or classification == "IN_PROGRESS":
        classification = str(getattr(exc, "termination_classification", ""))
    if not classification:
        message = str(exc).casefold()
        if process_exit_code is not None or "process exited unexpectedly" in message:
            classification = "APP_SERVER_PROCESS_EXIT"
        elif "timed out waiting for app server" in message:
            classification = "APP_SERVER_TRANSPORT_TIMEOUT"
        elif "timed out waiting for turn/completed" in message:
            classification = "HOST_TURN_DEADLINE"
        else:
            classification = "APP_SERVER_PROVIDER_ERROR"
    finalized.update(
        {
            "termination_classification": classification,
            "host_deadline_expired": classification == "HOST_TURN_DEADLINE",
            "app_server_process_alive_at_failure": process_exit_code is None if process_object is not None else None,
            "failure_type": type(exc).__name__,
            "failure_reason": _safe_turn_failure_reason(exc, classification, process_exit_code),
        }
    )
    if process_exit_code is not None:
        finalized["app_server_process_exit_code"] = int(process_exit_code)
    return finalized


class _AppServerProcess:
    def __init__(
        self,
        *,
        config: OrchestratorConfig,
        agent: AgentConfig,
        repository: Path,
        progress: Callable[[str], None] | None,
        role: str = "",
        require_workspace_ready: bool = False,
    ) -> None:
        self.config = config
        self.agent = agent
        self.repository = repository.resolve()
        self.progress = progress
        self.role = str(role or "")
        self.require_workspace_ready = require_workspace_ready
        self._lock = threading.RLock()
        self._next_id = 0
        self._messages: queue.Queue[str | None] = queue.Queue()
        self._pending: deque[dict[str, Any]] = deque()
        self._stderr: deque[str] = deque(maxlen=80)
        self._events: deque[dict[str, Any]] = deque(maxlen=2000)
        self._event_journal: LiveEventJournal | None = None
        self._event_context: dict[str, str] = {}
        self._event_publications: queue.Queue[
            tuple[LiveEventJournal, str, dict[str, Any], dict[str, str]] | None
        ] = queue.Queue(maxsize=_EVENT_PUBLICATION_QUEUE_SIZE)
        self._event_publication_stop = threading.Event()
        self._closed = False
        self.windows_sandbox = ""
        self.windows_sandbox_readiness = "not_checked"
        self.executor_readiness: dict[str, bool | str] = {"status": "not_required"}
        self.initialize_params: dict[str, Any] = {
            "clientInfo": {
                "name": "dual-codex",
                "version": "1",
            },
            "capabilities": {"experimentalApi": True},
        }
        self.initialize_response: dict[str, Any] = {}
        self.last_thread_request: dict[str, Any] = {}
        self.last_thread_binding: dict[str, Any] = {}
        self._active_turn_provenance: dict[str, Any] | None = None
        self.last_turn_provenance: dict[str, Any] = {}
        self._active_thread_id = ""
        self._active_thread_resumed = False
        self.request_methods: list[str] = []
        command = _app_server_command(config, agent=agent, role=role)
        process_args = _prepare_command([str(item) for item in command])
        env = codex_environment(agent, isolate_desktop_bridge=True)
        if os.name == "nt" and require_workspace_ready and agent.sandbox == "workspace-write":
            try:
                executor_npm_cache(agent).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise AppServerError(
                    "EXECUTOR_WRITE_PATH_UNAVAILABLE: could not prepare the Executor's exact npm cache root.",
                    failure_class="EXECUTOR_WRITE_PATH_UNAVAILABLE",
                ) from exc
        self.process = subprocess.Popen(
            process_args,
            cwd=repository,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            shell=False,
        )
        threading.Thread(target=self._publish_events, name="dual-codex-event-journal", daemon=True).start()
        threading.Thread(target=self._read_stdout, name="dual-codex-app-server", daemon=True).start()
        threading.Thread(target=self._read_stderr, name="dual-codex-app-server-stderr", daemon=True).start()
        try:
            response = self.request(
                "initialize",
                self.initialize_params,
                timeout=config.app_server_initialize_timeout,
            )
            if "error" in response:
                raise AppServerError(f"App Server initialize failed: {_error_message(response)}")
            self.initialize_response = response.get("result", {}) if isinstance(response.get("result"), dict) else {}
            self.notify("initialized")
            self.windows_sandbox, self.windows_sandbox_readiness = self._verify_windows_sandbox(
                require_workspace_ready=require_workspace_ready
            )
        except Exception:
            self.close()
            raise

    @property
    def pid(self) -> int:
        return int(self.process.pid)

    @property
    def stderr_tail(self) -> str:
        return "".join(self._stderr)[-8000:]

    @property
    def events(self) -> list[dict[str, Any]]:
        return list(self._events)

    def _verify_windows_sandbox(self, *, require_workspace_ready: bool = False) -> tuple[str, str]:
        """Read the effective profile policy and gate elevated provisioning.

        ``config/read`` and ``windowsSandbox/readiness`` are App Server APIs;
        no local ACL probing or implicit sandbox downgrade is attempted.
        """

        if os.name != "nt":
            return "", "not_applicable"
        response = self.request(
            "config/read",
            {"cwd": str(self.repository), "includeLayers": False},
            timeout=self.config.app_server_initialize_timeout,
        )
        if "error" in response:
            raise AppServerError(
                "Unable to verify the effective Windows sandbox policy: "
                + _error_message(response)
            )
        result = response.get("result")
        effective = result.get("config") if isinstance(result, Mapping) else None
        windows = effective.get("windows") if isinstance(effective, Mapping) else None
        mode = windows.get("sandbox") if isinstance(windows, Mapping) else None
        if mode is None:
            # No explicit setting is intentionally left to Codex defaults and
            # recorded as such; the adapter never supplies a fallback value.
            if require_workspace_ready:
                raise AppServerError("WINDOWS_SANDBOX_NOT_CONFIGURED: workspace-write Codex Executor requires a configured Windows sandbox.")
            return "unspecified", "not_checked"
        if not isinstance(mode, str) or mode not in _WINDOWS_SANDBOX_MODES:
            raise AppServerError(f"Unsupported effective Windows sandbox policy: {mode!r}.")
        if mode != "elevated" and not require_workspace_ready:
            return mode, "not_required"
        readiness = self.request(
            "windowsSandbox/readiness",
            None,
            timeout=self.config.app_server_initialize_timeout,
        )
        if "error" in readiness:
            raise AppServerError(
                "Unable to verify elevated Windows sandbox provisioning: "
                + _error_message(readiness)
            )
        readiness_result = readiness.get("result")
        status = readiness_result.get("status") if isinstance(readiness_result, Mapping) else None
        if status != "ready":
            raise AppServerError(
                "WINDOWS_SANDBOX_NOT_READY: Windows sandbox is not ready (not provisioned) "
                f"(readiness={status or 'unknown'}). Run the official command "
                f"from an administrative terminal: {_windows_sandbox_setup_command(self.agent)}"
            )
        return mode, "ready"

    def _executor_command(
        self,
        command: list[str],
        *,
        timeout_ms: int = 8000,
        failure_class: str,
    ) -> dict[str, Any]:
        npm_cache = executor_npm_cache(self.agent)
        policy = _workspace_write_sandbox_policy(
            self.repository,
            network_access=self.agent.network_access,
            npm_cache=npm_cache,
        )
        try:
            response = self.request(
                "command/exec",
                {
                    "command": command,
                    "cwd": str(self.repository),
                    "timeoutMs": timeout_ms,
                    "sandboxPolicy": policy,
                },
                timeout=(timeout_ms / 1000) + 3,
            )
        except AppServerError as exc:
            raise AppServerError(
                f"{failure_class}: required Executor readiness command did not complete.",
                failure_class=failure_class,
            ) from exc
        if "error" in response:
            raise AppServerError(
                f"{failure_class}: App Server sandbox rejected a required Executor readiness command.",
                failure_class=failure_class,
            )
        result = response.get("result")
        if not isinstance(result, Mapping):
            raise AppServerError(
                f"{failure_class}: App Server omitted the Executor readiness command result.",
                failure_class=failure_class,
            )
        return dict(result)

    def check_executor_capabilities(self) -> dict[str, bool | str]:
        """Run cheap, bounded checks for the configured workspaceWrite Executor."""

        if os.name != "nt" or self.agent.sandbox != "workspace-write" or self.role != "executor":
            self.executor_readiness = {"status": "not_applicable"}
            return dict(self.executor_readiness)
        if self.windows_sandbox != "elevated" or self.windows_sandbox_readiness != "ready":
            raise AppServerError(
                "EXECUTOR_SANDBOX_UNAVAILABLE: the Executor requires the provisioned elevated Windows sandbox.",
                failure_class="EXECUTOR_SANDBOX_UNAVAILABLE",
            )

        trivial = self._executor_command(
            ["cmd.exe", "/d", "/c", "exit", "0"],
            failure_class="EXECUTOR_SUBPROCESS_UNAVAILABLE",
        )
        if trivial.get("exitCode") != 0:
            raise AppServerError(
                "EXECUTOR_SUBPROCESS_UNAVAILABLE: the Windows sandbox could not launch a trivial child process.",
                failure_class="EXECUTOR_SUBPROCESS_UNAVAILABLE",
            )

        git = self._executor_command(["git", "--version"], failure_class="EXECUTOR_GIT_UNAVAILABLE")
        if git.get("exitCode") != 0:
            raise AppServerError(
                "EXECUTOR_GIT_UNAVAILABLE: Git could not launch inside the Executor sandbox.",
                failure_class="EXECUTOR_GIT_UNAVAILABLE",
            )

        node = self._executor_command(["node", "--version"], failure_class="EXECUTOR_NODE_UNAVAILABLE")
        if node.get("exitCode") != 0:
            raise AppServerError(
                "EXECUTOR_NODE_UNAVAILABLE: Node could not launch inside the Executor sandbox.",
                failure_class="EXECUTOR_NODE_UNAVAILABLE",
            )

        node_checks = r'''const fs=require("node:fs");const os=require("node:os");const path=require("node:path");const cp=require("node:child_process");const {Worker}=require("node:worker_threads");(async()=>{const out={child:false,worker:false,temp_write:false,cache_write:false};try{const c=cp.spawnSync(process.execPath,["-e","process.exit(0)"],{stdio:"ignore",timeout:5000,windowsHide:true});out.child=!c.error&&c.status===0}catch{}try{await new Promise((resolve,reject)=>{const w=new Worker("const {parentPort}=require('node:worker_threads');parentPort.postMessage('ready');parentPort.close()",{eval:true});const t=setTimeout(()=>reject(Error('timeout')),4000);w.once('message',()=>{clearTimeout(t);resolve()});w.once('error',e=>{clearTimeout(t);reject(e)})}) ;out.worker=true}catch{}for(const [key,root] of [["temp_write",os.tmpdir()],["cache_write",process.env.NPM_CONFIG_CACHE||""]]){try{if(!root)continue;const file=path.join(root,".dual-codex-readiness-"+process.pid+"-"+Date.now());fs.writeFileSync(file,"ok",{flag:"wx"});fs.unlinkSync(file);out[key]=true}catch{}}process.stdout.write(JSON.stringify(out))})().catch(()=>process.exit(90));'''
        worker = self._executor_command(
            ["node", "-e", node_checks],
            timeout_ms=12000,
            failure_class="EXECUTOR_NODE_WORKER_UNAVAILABLE",
        )
        try:
            worker_result = json.loads(str(worker.get("stdout", "")))
        except (TypeError, json.JSONDecodeError):
            worker_result = {}
        if worker.get("exitCode") != 0 or not isinstance(worker_result, Mapping) or not worker_result.get("child"):
            raise AppServerError(
                "EXECUTOR_SUBPROCESS_UNAVAILABLE: Node could not launch a child process inside the sandbox.",
                failure_class="EXECUTOR_SUBPROCESS_UNAVAILABLE",
            )
        if not worker_result.get("worker"):
            raise AppServerError(
                "EXECUTOR_NODE_WORKER_UNAVAILABLE: Node worker_threads could not start inside the sandbox.",
                failure_class="EXECUTOR_NODE_WORKER_UNAVAILABLE",
            )
        if not worker_result.get("temp_write") or not worker_result.get("cache_write"):
            raise AppServerError(
                "EXECUTOR_WRITE_PATH_UNAVAILABLE: TEMP or the exact npm cache root is not writable.",
                failure_class="EXECUTOR_WRITE_PATH_UNAVAILABLE",
            )

        readiness: dict[str, bool | str] = {
            "status": "ready",
            "child_process": True,
            "git": True,
            "node_process": True,
            "node_child_process": True,
            "node_worker": True,
            "temp_write": True,
            "npm_cache_write": True,
            "network_enabled": bool(self.agent.network_access),
        }
        if self.agent.network_access:
            network = r'''const https=require("node:https");const checks={registry:false,prisma_host:false};function get(url,key){return new Promise(resolve=>{const req=https.get(url,{timeout:5000},res=>{res.resume();checks[key]=res.statusCode<500;resolve()});req.on("timeout",()=>req.destroy(Error("timeout")));req.on("error",()=>resolve())})}Promise.all([get("https://registry.npmjs.org/-/ping","registry"),get("https://binaries.prisma.sh/","prisma_host")]).then(()=>{process.stdout.write(JSON.stringify(checks));process.exit(checks.registry&&checks.prisma_host?0:2)})'''
            probe = self._executor_command(
                ["node", "-e", network],
                timeout_ms=11000,
                failure_class="EXECUTOR_NETWORK_UNAVAILABLE",
            )
            try:
                network_result = json.loads(str(probe.get("stdout", "")))
            except (TypeError, json.JSONDecodeError):
                network_result = {}
            if probe.get("exitCode") != 0 or not isinstance(network_result, Mapping) or not network_result.get("registry") or not network_result.get("prisma_host"):
                raise AppServerError(
                    "EXECUTOR_NETWORK_UNAVAILABLE: networkAccess is enabled but npm or Prisma HTTPS is unreachable.",
                    failure_class="EXECUTOR_NETWORK_UNAVAILABLE",
                )
            readiness["network_registry"] = True
            readiness["network_prisma"] = True
        self.executor_readiness = readiness
        return dict(readiness)

    def set_event_context(self, journal: LiveEventJournal | None, **context: str) -> None:
        with self._lock:
            self._event_journal = journal
            self._event_context = {str(key): str(value) for key, value in context.items()}

    def request_without_event_journal(
        self,
        method: str,
        params: dict[str, Any] | None,
        *,
        timeout: float,
    ) -> dict[str, Any]:
        """Run dashboard telemetry without inheriting an Executor event context."""

        with self._lock:
            previous_journal = self._event_journal
            previous_context = self._event_context
            self._event_journal = None
            self._event_context = {}
            try:
                return self.request(method, params, timeout=timeout)
            finally:
                self._event_journal = previous_journal
                self._event_context = previous_context

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self._messages.put(line)
        self._messages.put(None)

    def _read_stderr(self) -> None:
        assert self.process.stderr is not None
        for line in self.process.stderr:
            self._stderr.append(line)

    def _publish_events(self) -> None:
        stop = getattr(self, "_event_publication_stop", None)
        while True:
            try:
                publication = self._event_publications.get(timeout=0.1)
            except queue.Empty:
                if stop is not None and stop.is_set():
                    return
                continue
            if publication is None:
                self._event_publications.task_done()
                return
            journal, method, params, context = publication
            try:
                journal.append_notification(method, params, **context)
            except Exception:
                # Journal contention/failure must not affect protocol handling.
                pass
            finally:
                self._event_publications.task_done()

    def _send(self, message: dict[str, Any]) -> None:
        if self._closed or self.process.poll() is not None:
            raise AppServerError("App Server process is not running.")
        assert self.process.stdin is not None
        try:
            self.process.stdin.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise AppServerError("App Server stdin closed unexpectedly.") from exc

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)

    def _respond_to_server_request(self, message: dict[str, Any]) -> None:
        method = str(message.get("method", ""))
        # Never grant an approval or permission implicitly. Normal workspace-write
        # turns use approvalPolicy=never; an unexpected request is a hard denial.
        if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
            result: Any = {"decision": "decline"}
        elif method == "item/tool/call":
            # Dynamic tools are client-owned. Returning a protocol-shaped
            # failure is important: an error response leaves the call without
            # a custom-tool output and the upstream turn retries until timeout.
            # Built-in commandExecution remains available to headless turns.
            result = {
                "success": False,
                "contentItems": [
                    {
                        "type": "inputText",
                        "text": (
                            "Dynamic tools are unavailable in the headless Dual Codex "
                            "App Server; use built-in command execution."
                        ),
                    }
                ],
            }
        elif method == "item/tool/requestUserInput":
            result = {"answers": {}}
        elif method == "mcpServer/elicitation/request":
            result = {"action": "decline"}
        elif method == "currentTime/read":
            result = {"currentTimeAt": int(time.time())}
        elif method == "item/permissions/requestApproval":
            result = {"permissions": {}, "scope": "turn", "strictAutoReview": False}
        elif method in {"applyPatchApproval", "execCommandApproval"}:
            result = {"decision": {"denied": {"rejection": "Dual Codex does not auto-approve requests."}}}
        else:
            result = None
        response: dict[str, Any] = {"jsonrpc": "2.0", "id": message.get("id")}
        if result is None:
            response["error"] = {"code": -32000, "message": "Unsupported server request; denied by Dual Codex."}
        else:
            response["result"] = result
        self._send(response)

    def _next_message(self, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AppServerError("Timed out waiting for App Server JSON-RPC data.")
            try:
                line = self._messages.get(timeout=remaining)
            except queue.Empty as exc:
                raise AppServerError("Timed out waiting for App Server JSON-RPC data.") from exc
            if line is None:
                stderr = _sanitize_stderr(self.stderr_tail)
                detail = f": {stderr}" if stderr else "."
                process = getattr(self, "process", None)
                try:
                    exit_code = process.poll() if process is not None else None
                except Exception:
                    exit_code = None
                classification = "APP_SERVER_PROCESS_EXIT" if exit_code is not None else "APP_SERVER_TRANSPORT_EOF"
                description = "App Server process exited unexpectedly" if exit_code is not None else "App Server stdout closed unexpectedly"
                raise AppServerError(
                    f"{description}{detail}",
                    failure_class=classification,
                    termination_classification=classification,
                )
            try:
                message = json.loads(line)
            except (TypeError, ValueError) as exc:
                raise AppServerError(
                    "App Server emitted invalid JSON-RPC data.",
                    failure_class="APP_SERVER_PROTOCOL_ERROR",
                    termination_classification="APP_SERVER_PROTOCOL_ERROR",
                ) from exc
            if not isinstance(message, dict):
                raise AppServerError(
                    "App Server emitted a non-object JSON-RPC message.",
                    failure_class="APP_SERVER_PROTOCOL_ERROR",
                    termination_classification="APP_SERVER_PROTOCOL_ERROR",
                )
            if "method" in message and "id" in message:
                # Preserve client-owned dynamic-tool calls in the same
                # provenance stream as notifications before replying.
                self._record_notification(message)
                self._respond_to_server_request(message)
                continue
            return message

    def _record_notification(self, message: dict[str, Any]) -> None:
        if "method" in message:
            active_turn = getattr(self, "_active_turn_provenance", None)
            method = message.get("method")
            params = message.get("params")
            params = params if isinstance(params, Mapping) else {}
            provider_turn = params.get("turn")
            event_turn_id = provider_turn.get("id") if isinstance(provider_turn, Mapping) else params.get("turnId")
            if active_turn is not None:
                if not active_turn.get("turn_id") and method == "turn/started" and isinstance(event_turn_id, str):
                    active_turn["turn_id"] = event_turn_id
                if isinstance(event_turn_id, str) and event_turn_id == active_turn.get("turn_id"):
                    active_turn["last_provider_event_timestamp"] = datetime.now(timezone.utc).isoformat()
                    if isinstance(method, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_./-]{0,127}", method):
                        active_turn["last_safe_provider_notification_method"] = method
            event_message = message
            if message.get("method") == "rawResponseItem/completed":
                if isinstance(params, dict):
                    safe_params: dict[str, Any] = {
                        key: params[key]
                        for key in ("threadId", "turnId")
                        if key in params
                    }
                    safe_item = _raw_response_item_evidence(params.get("item"))
                    if safe_item is not None:
                        safe_params["item"] = safe_item
                    event_message = {**message, "params": safe_params}
            self._events.append(event_message)
            journal = getattr(self, "_event_journal", None)
            publications = getattr(self, "_event_publications", None)
            if journal is not None and publications is not None:
                try:
                    publications.put_nowait(
                        (
                            journal,
                            str(event_message.get("method", "")),
                            event_message.get("params") if isinstance(event_message.get("params"), dict) else {},
                            dict(getattr(self, "_event_context", {})),
                        )
                    )
                except queue.Full:
                    # A bounded queue prevents observability from backpressuring RPC.
                    pass
                except Exception:
                    # Observability must not change the executor's protocol behavior.
                    pass

    def request(self, method: str, params: dict[str, Any] | None, *, timeout: float) -> dict[str, Any]:
        with self._lock:
            request_methods = getattr(self, "request_methods", None)
            if request_methods is not None:
                request_methods.append(method)
            self._next_id += 1
            request_id = self._next_id
            message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
            if params is not None:
                message["params"] = params
            else:
                message["params"] = None
            self._send(message)
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AppServerError(
                        f"Timed out waiting for App Server response to {method}.",
                        failure_class="APP_SERVER_TRANSPORT_TIMEOUT",
                        termination_classification="APP_SERVER_TRANSPORT_TIMEOUT",
                    )
                try:
                    message = self._next_message(min(1.0, remaining))
                except AppServerError as exc:
                    if str(exc).startswith("Timed out waiting for App Server JSON-RPC data"):
                        continue
                    raise
                if message.get("id") == request_id:
                    return message
                self._record_notification(message)
                self._pending.append(message)

    def _take_pending(self) -> dict[str, Any] | None:
        if self._pending:
            return self._pending.popleft()
        return None

    def _read_event(self, timeout: float) -> dict[str, Any]:
        pending = self._take_pending()
        if pending is not None:
            return pending
        return self._next_message(timeout)

    def _thread_id_for_unlocked(self, repository: Path) -> tuple[str, bool]:
        repository = repository.resolve(strict=False)
        runtime_workspace_roots = _canonical_workspace_roots(repository)
        params: dict[str, Any] = {
            "cwd": str(repository),
            "runtimeWorkspaceRoots": runtime_workspace_roots,
            "sandbox": self.agent.sandbox,
            "approvalPolicy": "never" if self.agent.sandbox == "workspace-write" else "on-request",
            # The raw Responses stream is the App Server equivalent of the
            # native executor's custom_tool_call_output records. It is scoped
            # to the headless thread and does not alter the TUI/backend path.
            "experimentalRawEvents": True,
        }
        if self.agent.model:
            params["model"] = self.agent.model
        if self.agent.service_tier:
            params["serviceTier"] = self.agent.service_tier
        stored = _load_thread_mapping(
            self.config,
            self.agent,
            repository,
            role=self.role,
            windows_sandbox=self.windows_sandbox,
        )
        if stored:
            response = self.request(
                "thread/resume",
                {"threadId": stored, **params},
                timeout=self.config.app_server_thread_timeout,
            )
            if "error" not in response:
                self.last_thread_request = dict(params)
                self.last_thread_binding = _validate_thread_binding(response, repository)
                return stored, True
            if not _is_stale_thread_error(response):
                raise AppServerError(f"App Server thread/resume failed: {_error_message(response)}")
        self.last_thread_request = dict(params)
        response = self.request("thread/start", params, timeout=self.config.app_server_thread_timeout)
        if "error" in response:
            raise AppServerError(f"App Server thread/start failed: {_error_message(response)}")
        self.last_thread_binding = _validate_thread_binding(response, repository)
        thread = response.get("result", {}).get("thread", {})
        thread_id = thread.get("id") if isinstance(thread, dict) else None
        if not isinstance(thread_id, str) or not thread_id:
            raise AppServerError("App Server thread/start returned no thread ID.")
        return thread_id, False

    def thread_id_for(self, repository: Path) -> tuple[str, bool]:
        with self._lock:
            return self._thread_id_for_unlocked(repository)

    def _turn_unlocked(self, thread_id: str, prompt: str, repository: Path) -> dict[str, Any]:
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": prompt}],
            "approvalPolicy": "never" if self.agent.sandbox == "workspace-write" else "on-request",
            "cwd": str(repository.resolve()),
        }
        if self.agent.sandbox == "workspace-write":
            params["sandboxPolicy"] = _workspace_write_sandbox_policy(
                repository,
                network_access=self.agent.network_access,
                npm_cache=executor_npm_cache(self.agent),
            )
        if self.agent.model:
            params["model"] = self.agent.model
        if self.agent.reasoning_effort:
            params["effort"] = self.agent.reasoning_effort
        if self.agent.service_tier:
            params["serviceTier"] = self.agent.service_tier
        turn_timeout, timeout_source = _effective_turn_timeout(self.agent, self.config)
        self._active_turn_provenance = {
            "turn_timeout_seconds": turn_timeout,
            "timeout_source": timeout_source,
            "turn_start_monotonic": None,
            "turn_start_timestamp": None,
            "last_provider_event_timestamp": None,
            "termination_classification": "IN_PROGRESS",
            "host_deadline_expired": False,
            "app_server_process_alive_at_failure": None,
            "app_server_process_exit_code": None,
            "thread_resumed": bool(getattr(self, "_active_thread_resumed", False)),
            "thread_state": "resumed" if getattr(self, "_active_thread_resumed", False) else "fresh",
            "thread_id": thread_id,
            "turn_id": None,
            "process_id": getattr(getattr(self, "process", None), "pid", None),
            "last_safe_provider_notification_method": None,
        }
        self.last_turn_provenance = self._active_turn_provenance
        response = self.request("turn/start", params, timeout=self.config.app_server_turn_start_timeout)
        if "error" in response:
            self._active_turn_provenance["termination_classification"] = "APP_SERVER_TURN_START_ERROR"
            raise AppServerError(
                f"App Server turn/start failed: {_error_message(response)}",
                failure_class="APP_SERVER_TURN_START_ERROR",
                termination_classification="APP_SERVER_TURN_START_ERROR",
            )
        turn = response.get("result", {}).get("turn", {})
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        if not isinstance(turn_id, str) or not turn_id:
            self._active_turn_provenance["termination_classification"] = "APP_SERVER_PROTOCOL_ERROR"
            raise AppServerError(
                "App Server turn/start returned no turn ID.",
                failure_class="APP_SERVER_PROTOCOL_ERROR",
                termination_classification="APP_SERVER_PROTOCOL_ERROR",
            )
        self._active_turn_provenance["turn_id"] = turn_id

        started = False
        completed: dict[str, Any] | None = None
        tool_items: dict[tuple[str, str], dict[str, Any]] = {}
        raw_custom_calls: dict[str, dict[str, Any]] = {}
        raw_custom_outputs: dict[str, dict[str, Any]] = {}

        def observe_item(item: Any) -> None:
            evidence = _tool_item_evidence(item)
            if evidence is not None:
                key = (str(evidence.get("type", "")), str(evidence.get("id", "")))
                tool_items[key] = evidence

        def observe_raw(item: Any) -> None:
            evidence = _raw_response_item_evidence(item)
            if evidence is None:
                return
            item_type = evidence.get("type")
            call_id = str(evidence.get("call_id", ""))
            if item_type == "custom_tool_call" and call_id:
                raw_custom_calls[call_id] = evidence
            elif item_type == "custom_tool_call_output" and call_id:
                raw_custom_outputs[call_id] = evidence

        turn_start_monotonic = time.monotonic()
        self._active_turn_provenance["turn_start_monotonic"] = turn_start_monotonic
        self._active_turn_provenance["turn_start_timestamp"] = datetime.now(timezone.utc).isoformat()
        deadline = turn_start_monotonic + turn_timeout
        last_progress = time.monotonic()
        while completed is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._active_turn_provenance["termination_classification"] = "HOST_TURN_DEADLINE"
                self._active_turn_provenance["host_deadline_expired"] = True
                raise AppServerError(
                    f"Timed out waiting for turn/completed ({turn_id}).",
                    failure_class="HOST_TURN_DEADLINE",
                    termination_classification="HOST_TURN_DEADLINE",
                )
            try:
                message = self._read_event(min(1.0, remaining))
            except AppServerError as exc:
                if str(exc).startswith("Timed out waiting for App Server JSON-RPC data"):
                    continue
                raise
            if "id" in message and "method" not in message:
                self._pending.append(message)
                continue
            self._record_notification(message)
            method = message.get("method")
            event_params = message.get("params") or {}
            if isinstance(event_params, Mapping):
                observe_item(event_params.get("item"))
                if method == "rawResponseItem/completed":
                    observe_raw(event_params.get("item"))
            event_turn = event_params.get("turn") if isinstance(event_params, dict) else None
            event_turn_id = event_turn.get("id") if isinstance(event_turn, dict) else None
            if method == "turn/started" and event_turn_id == turn_id:
                started = True
            elif method == "turn/completed" and event_turn_id == turn_id:
                completed = event_turn
            elif method == "error":
                self._active_turn_provenance["termination_classification"] = "APP_SERVER_ERROR_EVENT"
                raise AppServerError(
                    "App Server emitted an error notification.",
                    failure_class="APP_SERVER_ERROR_EVENT",
                    termination_classification="APP_SERVER_ERROR_EVENT",
                )
            if self.progress and time.monotonic() - last_progress >= 15:
                self.progress(f"app-server turn {turn_id} still running")
                last_progress = time.monotonic()
        if not started:
            self._active_turn_provenance["termination_classification"] = "APP_SERVER_PROTOCOL_ERROR"
            raise AppServerError(
                f"App Server completed turn {turn_id} without turn/started.",
                failure_class="APP_SERVER_PROTOCOL_ERROR",
                termination_classification="APP_SERVER_PROTOCOL_ERROR",
            )
        if completed.get("status") != "completed":
            failure_class = _provider_turn_failure_class(completed)
            self._active_turn_provenance["termination_classification"] = failure_class
            error = completed.get("error")
            if isinstance(error, Mapping):
                for field in ("code", "type"):
                    value = error.get(field)
                    if isinstance(value, int):
                        self._active_turn_provenance[f"provider_error_{field}"] = str(value)
                    elif isinstance(value, str):
                        safe_value = _sanitize_stderr(value).strip().casefold()
                        if safe_value in _SAFE_PROVIDER_ERROR_TOKENS:
                            self._active_turn_provenance[f"provider_error_{field}"] = safe_value
                        elif value.strip():
                            self._active_turn_provenance[f"provider_error_{field}_present"] = True
            reason_parts = [f"App Server turn {turn_id} ended with status {completed.get('status')!r}."]
            provider_code = self._active_turn_provenance.get("provider_error_code")
            provider_type = self._active_turn_provenance.get("provider_error_type")
            if provider_code or provider_type:
                details = ", ".join(
                    f"{key}={value}"
                    for key, value in (("code", provider_code), ("type", provider_type))
                    if value
                )
                reason_parts.append(f"Provider error {details}.")
            raise AppServerError(
                " ".join(reason_parts),
                failure_class=failure_class,
                termination_classification=failure_class,
            )
        items = completed.get("items") or []
        for item in items:
            observe_item(item)
        messages = [item.get("text", "") for item in items if isinstance(item, dict) and item.get("type") == "agentMessage"]
        assistant = str(messages[-1]) if messages else ""
        missing_custom_outputs = sorted(set(raw_custom_calls) - set(raw_custom_outputs))
        if missing_custom_outputs:
            self._active_turn_provenance["termination_classification"] = "APP_SERVER_PROTOCOL_ERROR"
            raise AppServerError(
                "App Server completed turn with missing custom-tool output for call ids: "
                + ", ".join(missing_custom_outputs),
                failure_class="APP_SERVER_PROTOCOL_ERROR",
                termination_classification="APP_SERVER_PROTOCOL_ERROR",
            )
        self._active_turn_provenance["termination_classification"] = "TURN_COMPLETED"
        # A fresh thread has no durable rollout until its first turn is
        # accepted. Persist only materialized threads so a later invocation
        # cannot resume the unmaterialized id returned by thread/start.
        _save_thread_mapping(
            self.config,
            self.agent,
            repository,
            thread_id,
            role=self.role,
            windows_sandbox=self.windows_sandbox,
        )
        return {
            "thread_id": thread_id,
            "turn_id": turn_id,
            "request_order": list(self.request_methods),
            "thread_request": dict(self.last_thread_request),
            "thread_binding": dict(self.last_thread_binding),
            "assistant": assistant,
            "event_count": len(self._events),
            "turn_started": started,
            "turn_status": completed.get("status"),
            "turn_provenance": dict(self._active_turn_provenance),
            "tool_executions": list(tool_items.values()),
            "custom_tool_outputs": list(raw_custom_outputs.values()),
        }

    def turn(self, thread_id: str, prompt: str, repository: Path) -> dict[str, Any]:
        with self._lock:
            self.last_turn_provenance = {}
            self._active_thread_id = thread_id
            self._active_thread_resumed = False
            try:
                return self._turn_unlocked(thread_id, prompt, repository)
            finally:
                if self._active_turn_provenance is not None:
                    self.last_turn_provenance = dict(self._active_turn_provenance)
                    self._active_turn_provenance = None

    def run_turn_with_context(
        self,
        repository: Path,
        prompt: str,
        *,
        journal: LiveEventJournal | None,
        **context: str,
    ) -> dict[str, Any]:
        """Serialize provenance context with thread/resume and the turn.

        A persistent App Server process can serve more than one orchestrator
        request. Setting the journal outside this lock allowed concurrent
        requests to attach one turn's notifications to another run.
        """

        with self._lock:
            previous_journal = self._event_journal
            previous_context = self._event_context
            self.last_turn_provenance = {}
            self._event_journal = journal
            self._event_context = {str(key): str(value) for key, value in context.items()}
            try:
                thread_id, resumed = self._thread_id_for_unlocked(repository)
                self._active_thread_id = thread_id
                self._active_thread_resumed = resumed
                try:
                    turn = self._turn_unlocked(thread_id, prompt, repository)
                finally:
                    if self._active_turn_provenance is not None:
                        self.last_turn_provenance = dict(self._active_turn_provenance)
                        self._active_turn_provenance = None
                turn["thread_id"] = thread_id
                turn["thread_resumed"] = resumed
                return turn
            finally:
                self._event_journal = previous_journal
                self._event_context = previous_context

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        stop = getattr(self, "_event_publication_stop", None)
        # All protocol notifications for a completed turn have already been
        # queued. Drain them before terminating the child so the final tool
        # output cannot be lost from the reconciliable journal.
        try:
            self._event_publications.join()
        except (AttributeError, RuntimeError):
            pass
        if stop is not None:
            stop.set()
        try:
            self._event_publications.put_nowait(None)
        except (queue.Full, AttributeError):
            pass
        try:
            self._event_publications.join()
        except (AttributeError, RuntimeError):
            pass
        try:
            if self.process.stdin:
                self.process.stdin.close()
        except OSError:
            pass
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)


_PROCESS_LOCK = threading.RLock()
_PROCESSES: dict[tuple[str, str, str, str, str, str, str, str], _AppServerProcess] = {}


def _process_key(
    agent: AgentConfig,
    config: OrchestratorConfig,
    repository: Path | None = None,
    role: str = "",
) -> tuple[str, str, str, str, str, str, str, str]:
    return (
        agent.account_name,
        str(agent.codex_home.expanduser().resolve()),
        str(Path(config.codex_command).resolve()),
        str(bool(agent.network_access)),
        str(repository.expanduser().resolve()) if repository is not None else "",
        _profile_config_identity(agent),
        agent.sandbox,
        str(role or ""),
    )


def _get_process(
    config: OrchestratorConfig,
    agent: AgentConfig,
    repository: Path,
    progress: Callable[[str], None] | None,
    role: str = "",
    require_workspace_ready: bool = False,
) -> _AppServerProcess:
    key = _process_key(agent, config, repository, role)
    with _PROCESS_LOCK:
        process = _PROCESSES.get(key)
        if process is not None and process.process.poll() is None:
            process.progress = progress
            return process
        if process is not None:
            process.close()
        process = _AppServerProcess(
            config=config,
            agent=agent,
            repository=repository,
            progress=progress,
            role=role,
            require_workspace_ready=require_workspace_ready,
        )
        _PROCESSES[key] = process
        return process


def _close_processes() -> None:
    with _PROCESS_LOCK:
        processes = list(_PROCESSES.values())
        _PROCESSES.clear()
    for process in processes:
        process.close()


def _discard_process(process: _AppServerProcess) -> None:
    repository = getattr(process, "repository", None)
    key = _process_key(process.agent, process.config, repository, getattr(process, "role", ""))
    with _PROCESS_LOCK:
        if _PROCESSES.get(key) is process:
            _PROCESSES.pop(key, None)
    if repository is not None:
        _delete_thread_mapping(process.config, process.agent, Path(repository), role=getattr(process, "role", ""))
    process.close()


atexit.register(_close_processes)


def _mapping_path(config: OrchestratorConfig, agent: AgentConfig, repository: Path, role: str = "") -> Path:
    import hashlib

    key = f"{agent.account_name}|{agent.codex_home.resolve()}|{repository.resolve()}|{role or ''}".encode("utf-8")
    return config.runs_dir / "app-server-sessions" / (hashlib.sha256(key).hexdigest() + ".json")


def _load_thread_mapping(
    config: OrchestratorConfig,
    agent: AgentConfig,
    repository: Path,
    *,
    role: str = "",
    windows_sandbox: str = "",
) -> str | None:
    path = _mapping_path(config, agent, repository, role)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    expected_windows_sandbox = windows_sandbox if os.name == "nt" else ""
    if (
        value.get("repository") != str(repository.resolve())
        or value.get("account") != agent.account_name
        or value.get("codex_home") != str(agent.codex_home.resolve())
        or value.get("role", "") != str(role or "")
        or value.get("windows_sandbox") != expected_windows_sandbox
        or value.get("headless_raw_events") != (_HEADLESS_RAW_EVENTS_VERSION if os.name == "nt" else "")
    ):
        _delete_thread_mapping(config, agent, repository, role=role)
        return None
    thread_id = value.get("thread_id")
    return thread_id if isinstance(thread_id, str) and thread_id else None


def _save_thread_mapping(
    config: OrchestratorConfig,
    agent: AgentConfig,
    repository: Path,
    thread_id: str,
    *,
    role: str = "",
    windows_sandbox: str = "",
) -> None:
    path = _mapping_path(config, agent, repository, role)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        path,
        {
            "account": agent.account_name,
            "codex_home": str(agent.codex_home.resolve()),
            "repository": str(repository.resolve()),
            "role": str(role or ""),
            "thread_id": thread_id,
            "windows_sandbox": windows_sandbox if os.name == "nt" else "",
            "headless_raw_events": _HEADLESS_RAW_EVENTS_VERSION if os.name == "nt" else "",
        },
    )


def _delete_thread_mapping(config: OrchestratorConfig, agent: AgentConfig, repository: Path, *, role: str = "") -> None:
    """Forget a thread that may contain an unresolved client tool call."""

    path = _mapping_path(config, agent, repository, role)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        # A stale mapping must never prevent a fresh App Server process from
        # starting; the next successful turn overwrites it atomically.
        pass


def _is_stale_thread_error(response: dict[str, Any]) -> bool:
    message = _error_message(response).casefold()
    return any(marker in message for marker in ("not found", "unknown thread", "no such thread", "does not exist"))


def run_codex_app_server(
    *,
    config: OrchestratorConfig,
    agent: AgentConfig,
    repository: Path,
    prompt: str,
    output_path: Path,
    session_id: str,
    task_artifact_path: Path | None = None,
    task_sha256: str = "",
    request_id: str = "",
    run_id: str = "",
    role: str = "executor",
    configured_actor: bool = False,
    canonical_bootstrap=None,
    require_workspace_ready: bool = False,
    progress: Callable[[str], None] | None = None,
    process_started: Callable[[int], None] | None = None,
) -> CommandResult:
    command = _app_server_command(config, agent=agent, role=role)
    metadata: dict[str, Any] = {
        "phase": role,
        "role": role,
        "actor_id": agent.account_name,
        "profile_id": agent.account_name,
        "configured_actor": bool(configured_actor),
        "provider": agent.provider_type,
        "adapter": agent.adapter_type,
        "backend": agent.backend,
        "model": agent.model,
        "runtime_model": agent.runtime_model or agent.model,
        "reasoning_effort": agent.reasoning_effort or "provider-default",
        "delegation_transport": "app_server",
        "fallback_used": False,
        "canonical_bootstrap_required": canonical_bootstrap is not None,
        "canonical_instructions_root": str(canonical_bootstrap.source_root) if canonical_bootstrap is not None else "",
        "canonical_bootstrap_source": "machine-wide" if canonical_bootstrap is not None else "",
        "app_server_session_id": session_id,
        "task_transport": "app_server",
        "task_artifact": str(task_artifact_path.resolve()) if task_artifact_path else "",
        "task_sha256": task_sha256,
        "app_server_backend": agent.backend,
        "app_server_account": agent.account_name,
        "app_server_role": role,
        "app_server_repository": str(repository.resolve()),
        "app_server_thread_cwd": str(repository.resolve()),
        "app_server_runtime_workspace_roots": _canonical_workspace_roots(repository),
        "app_server_environment_roots": [],
        "app_server_thread_binding": {},
        "app_server_experimental_api": True,
        "app_server_windows_sandbox": "",
        "app_server_windows_sandbox_readiness": "not_checked",
        "app_server_executor_readiness": {"status": "not_checked"},
        "app_server_sandbox_policy": agent.sandbox,
        "app_server_approval_policy": "never" if agent.sandbox == "workspace-write" else "on-request",
        "app_server_raw_events": os.name == "nt",
        "app_server_tui": False,
        "app_server_fallback": False,
        "app_server_tool_executions": [],
        "app_server_custom_tool_outputs": [],
    }
    journal: LiveEventJournal | None = None
    try:
        journal = LiveEventJournal(
            config.runs_dir,
            account=agent.account_name,
            role=role,
            repository=repository,
            run_id=run_id or session_id,
            request_id=request_id,
            max_records=config.live_event_journal_max_records,
            max_record_bytes=config.live_event_journal_max_record_bytes,
            max_detail_bytes=config.live_event_journal_max_detail_bytes,
        )
        metadata["live_event_journal"] = str(journal.path)
    except Exception:
        # A telemetry path/configuration failure must not block the Executor.
        journal = None
    process: _AppServerProcess | None = None
    try:
        process = _get_process(
            config,
            agent,
            repository,
            progress,
            role=role,
            require_workspace_ready=require_workspace_ready,
        )
        if require_workspace_ready and role == "executor":
            check_capabilities = getattr(process, "check_executor_capabilities", None)
            if not callable(check_capabilities):
                raise AppServerError(
                    "EXECUTOR_READINESS_UNAVAILABLE: App Server adapter has no capability readiness check.",
                    failure_class="EXECUTOR_READINESS_UNAVAILABLE",
                )
            metadata["app_server_executor_readiness"] = check_capabilities()
        if process_started:
            try:
                process_started(process.pid)
            except BaseException:
                _discard_process(process)
                raise
        run_with_context = getattr(process, "run_turn_with_context", None)
        if callable(run_with_context):
            turn = run_with_context(
                repository,
                prompt,
                journal=journal,
                run_id=run_id or session_id,
                request_id=request_id,
                account=agent.account_name,
                role=role,
            )
            thread_id = str(turn["thread_id"])
            resumed = bool(turn.get("thread_resumed", False))
        else:
            # Compatibility seam for older test doubles; production uses the
            # serialized method above so provenance cannot cross requests.
            set_context = getattr(process, "set_event_context", None)
            if callable(set_context):
                if journal is not None:
                    set_context(
                        journal,
                        run_id=run_id or session_id,
                        request_id=request_id,
                        account=agent.account_name,
                        role=role,
                    )
                else:
                    set_context(None)
            thread_id, resumed = process.thread_id_for(repository)
            turn = process.turn(thread_id, prompt, repository)
        assistant = turn["assistant"]
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(_normalise_report(assistant), encoding="utf-8")
        metadata.update(
            {
                "app_server_windows_sandbox": getattr(process, "windows_sandbox", ""),
                "app_server_windows_sandbox_readiness": getattr(process, "windows_sandbox_readiness", "not_checked"),
                "app_server_executor_readiness": getattr(
                    process, "executor_readiness", metadata["app_server_executor_readiness"]
                ),
                "app_server_thread_id": thread_id,
                "app_server_turn_id": turn["turn_id"],
                "app_server_thread_resumed": str(resumed).lower(),
                "app_server_process_id": str(process.pid),
                "app_server_turn_provenance": dict(
                    getattr(process, "last_turn_provenance", {}) or turn.get("turn_provenance", {})
                ),
                "app_server_event_count": str(turn["event_count"]),
                "app_server_request_order": turn.get("request_order", []),
                "app_server_tool_executions": turn.get("tool_executions", []),
                "app_server_custom_tool_outputs": turn.get("custom_tool_outputs", []),
                "app_server_thread_request": turn.get("thread_request", {}),
                "app_server_thread_binding": turn.get("thread_binding", {}),
                "app_server_thread_cwd": turn.get("thread_binding", {}).get("cwd", str(repository.resolve())),
                "app_server_runtime_workspace_roots": turn.get("thread_binding", {}).get(
                    "runtimeWorkspaceRoots", _canonical_workspace_roots(repository)
                ),
                "app_server_environment_roots": turn.get("thread_binding", {}).get("environments", []),
            }
        )
        return CommandResult(command, 0, assistant, _sanitize_stderr(process.stderr_tail), metadata)
    except (AppServerError, OSError, ValueError) as exc:
        failure_reason = _sanitize_stderr(str(exc))
        if process is not None:
            turn_provenance = _finalize_turn_failure(process, exc)
            if turn_provenance is not None:
                metadata["app_server_turn_provenance"] = turn_provenance
                if turn_provenance.get("termination_classification") != "TURN_COMPLETED":
                    metadata["availability_failure_class"] = turn_provenance["termination_classification"]
                    failure_reason = turn_provenance["failure_reason"]
        if process is not None:
            _discard_process(process)
        text = str(exc).casefold()
        failure_class = str(metadata.get("availability_failure_class") or getattr(exc, "failure_class", ""))
        if failure_class:
            metadata["availability_failure_class"] = failure_class
        elif "not_configured" in text:
            metadata["availability_failure_class"] = "profile_readiness_unavailable"
        elif "not_ready" in text or "readiness=" in text:
            metadata["availability_failure_class"] = "profile_readiness_unavailable"
        elif isinstance(exc, OSError):
            metadata["availability_failure_class"] = "process_unavailable"
        elif "authentication" in text or "login" in text:
            metadata["availability_failure_class"] = "authentication_unavailable"
        else:
            metadata["availability_failure_class"] = "provider_runtime_unavailable"
        return CommandResult(command, 1, "", failure_reason, metadata)


def app_server_call(
    *,
    config: OrchestratorConfig,
    agent: AgentConfig,
    repository: Path,
    method: str,
    params: dict[str, Any] | None = None,
    timeout: float | None = None,
    role: str = "",
) -> dict[str, Any]:
    """Make one bounded, structured read against the account-isolated server.

    The response is deliberately kept as JSON data; callers must select safe
    fields before returning it from an HTTP endpoint.
    """
    process: _AppServerProcess | None = None
    try:
        process = _get_process(config, agent, repository, None, role=role)
        telemetry_call = getattr(type(process), "request_without_event_journal", None)
        if callable(telemetry_call):
            response = process.request_without_event_journal(
                method,
                params,
                timeout=timeout or config.dashboard_telemetry_timeout,
            )
        else:
            set_context = getattr(process, "set_event_context", None)
            if callable(set_context):
                set_context(None)
            response = process.request(
                method,
                params,
                timeout=timeout or config.dashboard_telemetry_timeout,
            )
        if "error" in response:
            return {"error": {"message": _error_message(response)}}
        result = response.get("result")
        return result if isinstance(result, dict) else {}
    except (AppServerError, OSError, ValueError) as exc:
        if process is not None:
            _discard_process(process)
        return {"error": {"message": _json_error(str(exc))}}


def app_server_events(
    *,
    config: OrchestratorConfig,
    agent: AgentConfig,
    repository: Path,
    role: str = "",
) -> list[dict[str, Any]]:
    """Return the in-memory notification tail for a healthy account process."""
    key = _process_key(agent, config, repository, role)
    with _PROCESS_LOCK:
        process = _PROCESSES.get(key)
        if process is None or process.process.poll() is not None:
            return []
        return process.events


def close_app_server_processes() -> None:
    """Stop dashboard-owned App Server children during a clean shutdown."""
    _close_processes()
