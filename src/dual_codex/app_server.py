from __future__ import annotations

from collections import deque
import atexit
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
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
_SECURITY_RECOVERY_PLUGIN_ID = "codex-security@openai-curated-remote"
_SECURITY_RECOVERY_MCP_SERVER = "codex-security"
_SECURITY_RECOVERY_TOOL = "cancel_codex_security_scan"
_SECURITY_RECOVERY_NON_TOOL_ITEMS = frozenset({"agentMessage", "reasoning", "plan", "userMessage", "summary"})


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


def _thread_binding_signature(binding: Mapping[str, Any]) -> tuple[Any, ...] | None:
    """Compare repository roots without relying on ephemeral environment IDs."""

    cwd = binding.get("cwd")
    roots = binding.get("runtimeWorkspaceRoots")
    environments = binding.get("environments")
    if not isinstance(cwd, str) or not isinstance(roots, list) or not isinstance(environments, list):
        return None
    try:
        environment_signature = tuple(
            (
                path_identity_key(environment.get("cwd", "")),
                tuple(path_identity_key(root) for root in environment.get("runtimeWorkspaceRoots", [])),
            )
            for environment in environments
            if isinstance(environment, Mapping)
        )
        if len(environment_signature) != len(environments):
            return None
        return (
            path_identity_key(cwd),
            tuple(path_identity_key(root) for root in roots),
            environment_signature,
        )
    except (OSError, RuntimeError, ValueError, TypeError):
        return None


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


def _stable_identity(value: Mapping[str, Any]) -> str:
    """Hash a canonical, non-secret runtime identity payload."""

    encoded = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _command_runtime_identity(config: OrchestratorConfig, agent: AgentConfig, role: str) -> str:
    command = _app_server_command(config, agent=agent, role=role)
    prepared = _prepare_command([str(item) for item in command])
    executable = command[0]
    resolved = shutil.which(executable) or executable
    executable_path = Path(resolved).expanduser()
    try:
        stat = executable_path.stat()
        executable_file = {
            "path": str(executable_path.resolve(strict=False)),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }
    except OSError:
        executable_file = {"path": str(executable_path.resolve(strict=False)), "missing": True}
    return _stable_identity({"prepared_command": prepared, "executable": executable_file})


def _runtime_config_identity(
    agent: AgentConfig,
    config: OrchestratorConfig,
    repository: Path,
    role: str,
    require_workspace_ready: bool,
) -> str:
    """Identify every configured value retained by a persistent App Server."""

    timeout, timeout_source = _effective_turn_timeout(agent, config)
    return _stable_identity(
        {
            "account": agent.account_name,
            "codex_home": str(agent.codex_home.expanduser().resolve(strict=False)),
            "repository": str(repository.expanduser().resolve(strict=False)),
            "runs_dir": str(config.runs_dir.expanduser().resolve(strict=False)),
            "role": str(role or ""),
            "profile_config_identity": _profile_config_identity(agent),
            "sandbox": agent.sandbox,
            "network_access": bool(agent.network_access),
            "model": agent.model,
            "reasoning_effort": agent.reasoning_effort,
            "service_tier": agent.service_tier,
            "account_role_turn_timeout": getattr(agent, "app_server_turn_timeout", None),
            "global_turn_timeout": config.app_server_turn_timeout,
            "effective_turn_timeout": timeout,
            "timeout_source": timeout_source,
            "initialize_timeout": config.app_server_initialize_timeout,
            "thread_timeout": config.app_server_thread_timeout,
            "turn_start_timeout": config.app_server_turn_start_timeout,
            "command_identity": _command_runtime_identity(config, agent, role),
            "require_workspace_ready": bool(require_workspace_ready),
            "host_mode": {"os_name": os.name, "platform": sys.platform},
        }
    )


def _thread_mapping_identity(
    agent: AgentConfig,
    config: OrchestratorConfig,
    repository: Path,
    role: str,
    require_workspace_ready: bool,
) -> str:
    """Bind resumable thread state to the process configuration that owns it.

    App Server failures invalidate a thread mapping. Keeping mappings scoped to
    the full runtime identity also prevents an old in-flight turn from deleting
    or resuming a session after another dispatch has changed process settings.
    """

    return _runtime_config_identity(agent, config, repository, role, require_workspace_ready)


def _app_server_command(
    config: OrchestratorConfig,
    *,
    agent: AgentConfig | None = None,
    role: str = "",
    security_recovery: bool = False,
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
    if security_recovery:
        # This process is dedicated to one maintenance turn. Replacing the
        # configured MCP/plugin maps gives the provider no other MCP tools,
        # even when the normal Codex Apps bridge exposes write operations.
        # The loopback URL is inert and satisfies config validation for the
        # app-hosted codex_apps server while it is disabled.
        command.extend(
            [
                "-c",
                "features.shell_tool=false",
                "-c",
                "features.hooks=false",
                "-c",
                "features.multi_agent=false",
                "-c",
                "features.apps=false",
                "-c",
                "features.browser_use=false",
                "-c",
                "features.browser_use_external=false",
                "-c",
                "features.browser_use_full_cdp_access=false",
                "-c",
                "features.computer_use=false",
                "-c",
                "features.image_generation=false",
                "-c",
                "features.sleep_tool=false",
                "-c",
                'web_search="disabled"',
                "-c",
                'mcp_servers={codex_apps={url="http://127.0.0.1:9",enabled=false}}',
                "-c",
                'plugins={"codex-security@openai-curated-remote"={enabled=true,mcp_servers={"codex-security"={enabled=true,enabled_tools=["cancel_codex_security_scan"]}}}}',
            ]
        )
    command.append("--stdio")
    return command


def _validate_security_cancel_tool_catalog(response: Mapping[str, Any]) -> dict[str, Any]:
    """Require the dedicated process to expose exactly one MCP tool."""

    if "error" in response:
        raise AppServerError(
            "App Server could not verify the Security recovery MCP tool catalog.",
            failure_class="SECURITY_RECOVERY_TOOL_CATALOG_UNAVAILABLE",
        )
    result = response.get("result")
    data = result.get("data") if isinstance(result, Mapping) else None
    cursor = result.get("nextCursor") if isinstance(result, Mapping) else None
    if not isinstance(data, list) or cursor not in (None, ""):
        raise AppServerError(
            "App Server returned an incomplete Security recovery MCP tool catalog.",
            failure_class="SECURITY_RECOVERY_TOOL_CATALOG_UNSAFE",
        )

    allowed = {(_SECURITY_RECOVERY_MCP_SERVER, _SECURITY_RECOVERY_TOOL)}
    observed: set[tuple[str, str]] = set()
    authorized_server_count = 0
    for server in data:
        if not isinstance(server, Mapping):
            raise AppServerError(
                "App Server returned an invalid Security recovery MCP server entry.",
                failure_class="SECURITY_RECOVERY_TOOL_CATALOG_UNSAFE",
            )
        server_name = server.get("name")
        plugin_id = server.get("pluginId")
        tools = server.get("tools")
        if not isinstance(server_name, str) or not isinstance(tools, Mapping):
            raise AppServerError(
                "App Server returned an incomplete Security recovery MCP server entry.",
                failure_class="SECURITY_RECOVERY_TOOL_CATALOG_UNSAFE",
            )
        if server_name == _SECURITY_RECOVERY_MCP_SERVER and plugin_id != _SECURITY_RECOVERY_PLUGIN_ID:
            raise AppServerError(
                "The Security recovery MCP server has an unexpected provider binding.",
                failure_class="SECURITY_RECOVERY_TOOL_CATALOG_UNSAFE",
            )
        if server_name == _SECURITY_RECOVERY_MCP_SERVER:
            authorized_server_count += 1
        if server_name == "codex_apps" and plugin_id is not None:
            raise AppServerError(
                "The disabled Codex Apps MCP server has an unexpected provider binding.",
                failure_class="SECURITY_RECOVERY_TOOL_CATALOG_UNSAFE",
            )
        for tool_name in tools:
            if not isinstance(tool_name, str):
                raise AppServerError(
                    "App Server returned an invalid Security recovery MCP tool name.",
                    failure_class="SECURITY_RECOVERY_TOOL_CATALOG_UNSAFE",
                )
            observed.add((server_name, tool_name))

    if observed != allowed or authorized_server_count != 1:
        raise AppServerError(
            "The dedicated App Server did not expose exactly the authorized Security cancellation tool.",
            failure_class="SECURITY_RECOVERY_TOOL_CATALOG_UNSAFE",
        )
    return {
        "server_count": len(data),
        "available_tool_count": len(observed),
        "exact_allowlist_verified": True,
    }


def _validate_security_recovery_native_tool_policy(response: Mapping[str, Any]) -> dict[str, Any]:
    """Require the disposable App Server's non-MCP tool surfaces to be off."""

    if "error" in response:
        raise AppServerError(
            "App Server could not verify the Security recovery native tool policy.",
            failure_class="SECURITY_RECOVERY_NATIVE_TOOL_POLICY_UNAVAILABLE",
        )
    result = response.get("result")
    effective = result.get("config") if isinstance(result, Mapping) else None
    features = effective.get("features") if isinstance(effective, Mapping) else None
    required_features = (
        "apps",
        "browser_use",
        "browser_use_external",
        "browser_use_full_cdp_access",
        "computer_use",
        "image_generation",
        "hooks",
        "multi_agent",
        "sleep_tool",
        "shell_tool",
    )
    if not isinstance(features, Mapping) or any(features.get(key) is not False for key in required_features):
        raise AppServerError(
            "App Server did not confirm that every unrelated native tool surface is disabled.",
            failure_class="SECURITY_RECOVERY_NATIVE_TOOL_POLICY_UNSAFE",
        )
    if effective.get("web_search") != "disabled":
        raise AppServerError(
            "App Server did not confirm that web search is disabled for Security recovery.",
            failure_class="SECURITY_RECOVERY_NATIVE_TOOL_POLICY_UNSAFE",
        )
    return {
        "verified": True,
        "disabled_features": list(required_features),
        "web_search": "disabled",
    }


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
        security_recovery_mode: bool = False,
    ) -> None:
        self.config = config
        self.agent = agent
        self.repository = repository.resolve()
        self.progress = progress
        self.role = str(role or "")
        self.require_workspace_ready = bool(require_workspace_ready)
        self.security_recovery_mode = bool(security_recovery_mode)
        self.runtime_config_identity = _runtime_config_identity(
            agent,
            config,
            self.repository,
            self.role,
            self.require_workspace_ready,
        )
        self.thread_mapping_identity = _thread_mapping_identity(
            agent,
            config,
            self.repository,
            self.role,
            self.require_workspace_ready,
        )
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
        self._active_security_cancel_approval: dict[str, Any] | None = None
        self.request_methods: list[str] = []
        command = _app_server_command(
            config,
            agent=agent,
            role=role,
            security_recovery=self.security_recovery_mode,
        )
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
        active_security_approval = getattr(self, "_active_security_cancel_approval", None)
        if method == "item/permissions/requestApproval" and active_security_approval is not None:
            context = active_security_approval
            context["approval_request_count"] = int(context.get("approval_request_count", 0)) + 1
            reason = self._security_cancel_approval_denial_reason(message, context)
            response: dict[str, Any] = {"jsonrpc": "2.0", "id": message.get("id")}
            if reason is None:
                context["approval_granted"] = True
                context["approved_item_id"] = str(message.get("params", {}).get("itemId", ""))
                response["result"] = {"permissions": {}, "scope": "turn", "strictAutoReview": True}
            else:
                context["approval_denial_reason"] = reason
                context["unexpected_item_seen"] = True
                context.setdefault("unexpected_item_types", []).append("invalid_security_cancel_approval")
                response["error"] = {
                    "code": -32000,
                    "message": "Security cancellation approval denied by Dual Codex.",
                }
            self._send(response)
            if reason is not None:
                self._request_security_recovery_turn_cancel()
            return
        if active_security_approval is not None:
            active_security_approval["unexpected_item_seen"] = True
            active_security_approval.setdefault("unexpected_item_types", []).append(method)
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
        if active_security_approval is not None:
            self._request_security_recovery_turn_cancel()

    def _request_security_recovery_turn_cancel(self) -> None:
        """Send one nonblocking cancel for the exact unexpected maintenance turn."""

        context = getattr(self, "_active_security_cancel_approval", None)
        if not isinstance(context, dict) or context.get("unexpected_item_seen") is not True:
            return
        if context.get("turn_cancel_requested") is True:
            return
        expected_thread_id = context.get("thread_id")
        active_thread_id = getattr(self, "_active_thread_id", "")
        active_turn = getattr(self, "_active_turn_provenance", None)
        turn_id = active_turn.get("turn_id") if isinstance(active_turn, Mapping) else None
        if (
            not isinstance(expected_thread_id, str)
            or not expected_thread_id
            or active_thread_id != expected_thread_id
            or not isinstance(turn_id, str)
            or not turn_id
        ):
            context["turn_cancel_deferred"] = True
            return
        self._next_id += 1
        request_id = self._next_id
        context["turn_cancel_requested"] = True
        context["turn_cancel_request_id"] = request_id
        context["turn_cancel_confirmed"] = False
        try:
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "turn/cancel",
                    "params": {"threadId": expected_thread_id, "turnId": turn_id},
                }
            )
        except Exception:
            context["turn_cancel_failure_class"] = "SECURITY_RECOVERY_CANCEL_SEND_FAILED"

    def _consume_security_recovery_turn_cancel_response(self, message: Mapping[str, Any]) -> bool:
        """Record the bounded response to the one maintenance turn/cancel request."""

        context = getattr(self, "_active_security_cancel_approval", None)
        if not isinstance(context, dict):
            return False
        request_id = context.get("turn_cancel_request_id")
        if request_id is None or message.get("id") != request_id or "method" in message:
            return False
        context["turn_cancel_confirmed"] = "error" not in message
        return True

    def _security_cancel_approval_denial_reason(
        self,
        message: Mapping[str, Any],
        context: Mapping[str, Any],
    ) -> str | None:
        """Match one exact MCP cancellation request against trusted host state."""

        if (
            context.get("operation") != "security_scan_cancel"
            or context.get("approval_policy") != "on-request"
            or context.get("binding_validated") is not True
            or context.get("tool_catalog_validated") is not True
            or context.get("approval_granted") is True
            or int(context.get("approval_request_count", 0)) != 1
        ):
            return "maintenance_context_invalid"
        params = message.get("params")
        if not isinstance(params, Mapping):
            return "request_shape_invalid"
        allowed_fields = {
            "cwd", "environmentId", "itemId", "permissions", "reason", "startedAtMs", "threadId", "turnId"
        }
        required_fields = {"cwd", "itemId", "permissions", "startedAtMs", "threadId", "turnId"}
        if not required_fields.issubset(params) or not set(params).issubset(allowed_fields):
            return "request_shape_invalid"
        if isinstance(params.get("startedAtMs"), bool) or not isinstance(params.get("startedAtMs"), int):
            return "request_shape_invalid"
        if params.get("reason") is not None and not isinstance(params.get("reason"), str):
            return "request_shape_invalid"
        if params.get("environmentId") is not None and not isinstance(params.get("environmentId"), str):
            return "request_shape_invalid"
        active_turn = self._active_turn_provenance
        expected_turn_id = active_turn.get("turn_id") if isinstance(active_turn, Mapping) else None
        expected_thread_id = str(context.get("thread_id", ""))
        if (
            not expected_thread_id
            or self._active_thread_id != expected_thread_id
            or self._active_thread_resumed is not True
            or params.get("threadId") != expected_thread_id
            or not isinstance(expected_turn_id, str)
            or not expected_turn_id
            or params.get("turnId") != expected_turn_id
        ):
            return "thread_or_turn_mismatch"
        expected_repository = str(context.get("repository", ""))
        requested_environment_id = params.get("environmentId")
        if requested_environment_id is not None:
            environment_ids = context.get("environment_ids")
            if (
                not isinstance(requested_environment_id, str)
                or not isinstance(environment_ids, list)
                or requested_environment_id not in environment_ids
            ):
                return "environment_binding_mismatch"
        requested_cwd = params.get("cwd")
        if not isinstance(requested_cwd, str) or not expected_repository:
            return "repository_binding_mismatch"
        try:
            if path_identity_key(requested_cwd) != path_identity_key(expected_repository):
                return "repository_binding_mismatch"
        except (OSError, RuntimeError, ValueError):
            return "repository_binding_mismatch"
        permissions = params.get("permissions")
        if not isinstance(permissions, Mapping) or not set(permissions).issubset({"fileSystem", "network"}):
            return "permission_profile_mismatch"
        if any(value not in (None, {}, []) for value in permissions.values()):
            return "permission_profile_mismatch"
        item_id = params.get("itemId")
        calls = context.get("mcp_tool_calls")
        if not isinstance(item_id, str) or not isinstance(calls, list) or len(calls) != 1:
            return "tool_call_count_mismatch"
        call = calls[0]
        if not isinstance(call, Mapping) or call.get("item_id") != item_id:
            return "tool_item_mismatch"
        if call.get("thread_id") != expected_thread_id or call.get("turn_id") != expected_turn_id:
            return "tool_item_binding_mismatch"
        if call.get("authorized") is not True:
            return "tool_or_scan_mismatch"
        if context.get("unexpected_item_seen") is True:
            return "unexpected_tool_seen"
        return None

    def _capture_security_cancel_event(self, method: str, params: Mapping[str, Any]) -> None:
        context = getattr(self, "_active_security_cancel_approval", None)
        if context is None:
            return
        if method not in {"item/started", "item/completed"}:
            return
        item = params.get("item")
        if not isinstance(item, Mapping):
            context["unexpected_item_seen"] = True
            context.setdefault("unexpected_item_types", []).append("missing_item_payload")
            return
        item_type = str(item.get("type", ""))
        item_id = str(item.get("id", ""))
        expected_thread_id = str(context.get("thread_id", ""))
        thread_id = params.get("threadId")
        turn_id = params.get("turnId")
        active_turn = self._active_turn_provenance
        expected_turn_id = active_turn.get("turn_id") if isinstance(active_turn, Mapping) else None
        if method == "item/started":
            if item_type == "mcpToolCall":
                context["mcp_tool_call_count"] = int(context.get("mcp_tool_call_count", 0)) + 1
                arguments = item.get("arguments")
                exact_arguments = (
                    isinstance(arguments, Mapping)
                    and set(arguments) == {"scanId"}
                    and arguments.get("scanId") == context.get("scan_id")
                )
                authorized = (
                    exact_arguments
                    and item.get("server") == "codex-security"
                    and item.get("tool") == "cancel_codex_security_scan"
                )
                if authorized:
                    context["matching_tool_call_count"] = int(context.get("matching_tool_call_count", 0)) + 1
                else:
                    context["unexpected_item_seen"] = True
                if thread_id != expected_thread_id or not isinstance(turn_id, str) or (
                    isinstance(expected_turn_id, str) and expected_turn_id and turn_id != expected_turn_id
                ):
                    context["unexpected_item_seen"] = True
                context.setdefault("mcp_tool_calls", []).append(
                    {
                        "item_id": item_id,
                        "thread_id": str(thread_id or ""),
                        "turn_id": str(turn_id or ""),
                        "authorized": bool(authorized),
                    }
                )
            elif item_type not in _SECURITY_RECOVERY_NON_TOOL_ITEMS:
                context["unexpected_item_seen"] = True
                context.setdefault("unexpected_item_types", []).append(item_type)
        elif item_id and any(
            isinstance(call, Mapping) and call.get("item_id") == item_id and call.get("authorized") is True
            for call in context.get("mcp_tool_calls", [])
        ):
            if thread_id != expected_thread_id or not isinstance(turn_id, str) or (
                isinstance(expected_turn_id, str) and expected_turn_id and turn_id != expected_turn_id
            ):
                context["unexpected_item_seen"] = True
                return
            result = item.get("result")
            has_result = isinstance(result, Mapping)
            provider_error = bool(item.get("error")) or (has_result and result.get("isError") is True)
            provider_error_class = ""
            if has_result:
                content = result.get("content")
                rendered = " ".join(
                    str(entry.get("text", ""))
                    for entry in content[:16]
                    if isinstance(entry, Mapping) and isinstance(entry.get("text"), str)
                ) if isinstance(content, list) else ""
                if re.search(r"(?i)\bscan\s+not\s+found\b", rendered):
                    provider_error_class = "scan_not_found"
                elif provider_error:
                    provider_error_class = "provider_error"
            status = str(item.get("status", ""))
            safe_statuses = {"inProgress", "completed", "failed", "declined", "interrupted"}
            context["provider_tool_result"] = {
                "item_status": status if status in safe_statuses else "unknown",
                "result_returned": has_result,
                "provider_error": bool(provider_error),
                "provider_error_class": provider_error_class,
            }
        elif item_type not in _SECURITY_RECOVERY_NON_TOOL_ITEMS:
            context["unexpected_item_seen"] = True
            context.setdefault("unexpected_item_types", []).append(item_type)

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
            if isinstance(method, str):
                self._capture_security_cancel_event(method, params)
            provider_turn = params.get("turn")
            event_turn_id = provider_turn.get("id") if isinstance(provider_turn, Mapping) else params.get("turnId")
            if active_turn is not None:
                if not active_turn.get("turn_id") and method == "turn/started" and isinstance(event_turn_id, str):
                    active_turn["turn_id"] = event_turn_id
                if isinstance(event_turn_id, str) and event_turn_id == active_turn.get("turn_id"):
                    active_turn["last_provider_event_timestamp"] = datetime.now(timezone.utc).isoformat()
                    if isinstance(method, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_./-]{0,127}", method):
                        active_turn["last_safe_provider_notification_method"] = method
            self._request_security_recovery_turn_cancel()
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
                if self._consume_security_recovery_turn_cancel_response(message):
                    continue
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

    def _resume_exact_security_thread_unlocked(
        self,
        thread_id: str,
        repository: Path,
        *,
        approval_policy: str,
    ) -> dict[str, Any]:
        """Resume one operator-specified thread without creating a replacement."""

        if not isinstance(thread_id, str) or not thread_id.strip():
            raise AppServerError(
                "Security recovery requires an exact owner thread ID.",
                failure_class="SECURITY_RECOVERY_OWNER_THREAD_INVALID",
            )
        if approval_policy not in {"never", "on-request"}:
            raise AppServerError(
                "Security recovery requested an unsupported approval policy.",
                failure_class="SECURITY_RECOVERY_APPROVAL_POLICY_INVALID",
            )
        repository = repository.resolve(strict=True)
        roots = _canonical_workspace_roots(repository)
        params: dict[str, Any] = {
            "cwd": str(repository),
            "runtimeWorkspaceRoots": roots,
            "sandbox": self.agent.sandbox,
            "approvalPolicy": approval_policy,
            "experimentalRawEvents": True,
        }
        if self.agent.model:
            params["model"] = self.agent.model
        if self.agent.service_tier:
            params["serviceTier"] = self.agent.service_tier
        response = self.request(
            "thread/resume",
            {"threadId": thread_id, **params},
            timeout=self.config.app_server_thread_timeout,
        )
        if "error" in response:
            if _is_stale_thread_error(response):
                raise AppServerError(
                    "The exact Security recovery owner thread is stale or not found.",
                    failure_class="SECURITY_RECOVERY_OWNER_THREAD_STALE",
                )
            raise AppServerError(
                "App Server rejected the exact Security recovery owner thread resume.",
                failure_class="SECURITY_RECOVERY_OWNER_THREAD_RESUME_REJECTED",
            )
        result = response.get("result")
        thread = result.get("thread") if isinstance(result, Mapping) else None
        returned_id = thread.get("id") if isinstance(thread, Mapping) else None
        if returned_id != thread_id:
            raise AppServerError(
                "App Server returned a different thread ID for the Security recovery owner thread.",
                failure_class="SECURITY_RECOVERY_OWNER_THREAD_BINDING_MISMATCH",
            )
        try:
            binding = _validate_thread_binding(response, repository)
        except AppServerError as exc:
            raise AppServerError(
                "App Server returned a mismatched Security recovery repository binding.",
                failure_class="SECURITY_RECOVERY_OWNER_THREAD_BINDING_MISMATCH",
            ) from exc
        expected_root = path_identity_key(repository)
        environments = binding.get("environments")
        if not isinstance(environments, list) or not environments:
            raise AppServerError(
                "App Server returned no Security recovery environment binding.",
                failure_class="SECURITY_RECOVERY_OWNER_THREAD_BINDING_MISMATCH",
            )
        for environment in environments:
            if not isinstance(environment, Mapping):
                raise AppServerError(
                    "App Server returned an invalid Security recovery environment binding.",
                    failure_class="SECURITY_RECOVERY_OWNER_THREAD_BINDING_MISMATCH",
                )
            environment_roots = environment.get("runtimeWorkspaceRoots")
            if (
                path_identity_key(environment.get("cwd", "")) != expected_root
                or not isinstance(environment_roots, list)
                or len(environment_roots) != 1
                or path_identity_key(environment_roots[0]) != expected_root
            ):
                raise AppServerError(
                    "App Server returned an unexpected Security recovery repository environment.",
                    failure_class="SECURITY_RECOVERY_OWNER_THREAD_BINDING_MISMATCH",
                )
        thread_cwd = thread.get("cwd") if isinstance(thread, Mapping) else None
        if isinstance(thread_cwd, str) and path_identity_key(thread_cwd) != expected_root:
            raise AppServerError(
                "App Server returned a different Security recovery thread cwd.",
                failure_class="SECURITY_RECOVERY_OWNER_THREAD_BINDING_MISMATCH",
            )
        self.last_thread_request = dict(params)
        self.last_thread_binding = dict(binding)
        self._active_thread_id = thread_id
        self._active_thread_resumed = True
        return dict(binding)

    def _verify_security_cancel_tool_catalog(self) -> dict[str, Any]:
        response = self.request(
            "mcpServerStatus/list",
            {"limit": 100},
            timeout=self.config.app_server_initialize_timeout,
        )
        return _validate_security_cancel_tool_catalog(response)

    def _verify_security_recovery_native_tool_policy(self, repository: Path) -> dict[str, Any]:
        response = self.request(
            "config/read",
            {"cwd": str(repository.resolve()), "includeLayers": False},
            timeout=self.config.app_server_initialize_timeout,
        )
        return _validate_security_recovery_native_tool_policy(response)

    def run_security_cancel_turn(
        self,
        repository: Path,
        owner_thread_id: str,
        scan_id: str,
        prompt: str,
    ) -> dict[str, Any]:
        """Run one bounded cancellation turn on an exact resumed owner thread."""

        with self._lock:
            previous_journal = self._event_journal
            previous_context = self._event_context
            previous_dispatch_provenance = getattr(self, "_active_dispatch_provenance", None)
            self._event_journal = None
            self._event_context = {}
            self._active_dispatch_provenance = {"maintenance_operation": "security_scan_cancel"}
            self.last_turn_provenance = {}
            outcome: dict[str, Any] = {
                "thread_id": owner_thread_id,
                "turn_id": "",
                "thread_resumed": False,
                "thread_binding": {},
                "turn_state": "not_started",
                "approval_policy": "on-request",
                "approval_granted": False,
                "approval_request_count": 0,
                "tool_call_attempted": False,
                "tool_call_count": 0,
                "tool_server": "codex-security",
                "tool_name": "cancel_codex_security_scan",
                "scan_id": scan_id,
                "tool_catalog": {},
                "tool_catalog_validated": False,
                "native_tool_policy": {},
                "native_tool_policy_validated": False,
                "provider_tool_result": None,
                "approval_state_cleared": True,
                "approval_policy_reset": False,
                "turn_cancel_requested": False,
                "turn_cancel_confirmed": False,
                "failure_class": "",
            }
            resumed = False
            turn_attempted = False
            context: dict[str, Any] | None = None
            try:
                if self.security_recovery_mode is not True:
                    outcome["failure_class"] = "SECURITY_RECOVERY_PROCESS_NOT_ISOLATED"
                    return outcome
                try:
                    native_tool_policy = self._verify_security_recovery_native_tool_policy(repository)
                except AppServerError as exc:
                    outcome["failure_class"] = exc.failure_class or "SECURITY_RECOVERY_NATIVE_TOOL_POLICY_UNSAFE"
                    return outcome
                outcome["native_tool_policy"] = native_tool_policy
                outcome["native_tool_policy_validated"] = True
                try:
                    catalog = self._verify_security_cancel_tool_catalog()
                except AppServerError as exc:
                    outcome["failure_class"] = exc.failure_class or "SECURITY_RECOVERY_TOOL_CATALOG_UNSAFE"
                    return outcome
                outcome["tool_catalog"] = {"before_resume": catalog}
                try:
                    binding = self._resume_exact_security_thread_unlocked(
                        owner_thread_id,
                        repository,
                        approval_policy="never",
                    )
                except AppServerError as exc:
                    outcome["failure_class"] = exc.failure_class or "SECURITY_RECOVERY_OWNER_THREAD_RESUME_FAILED"
                    outcome["resume_failure_class"] = outcome["failure_class"]
                    return outcome
                resumed = True
                outcome["thread_resumed"] = True
                outcome["thread_binding"] = binding
                try:
                    catalog = self._verify_security_cancel_tool_catalog()
                except AppServerError as exc:
                    outcome["failure_class"] = exc.failure_class or "SECURITY_RECOVERY_TOOL_CATALOG_UNSAFE"
                    return outcome
                outcome["tool_catalog"]["after_resume"] = catalog
                outcome["tool_catalog_validated"] = True
                context = {
                    "operation": "security_scan_cancel",
                    "approval_policy": "on-request",
                    "binding_validated": True,
                    "tool_catalog_validated": True,
                    "thread_id": owner_thread_id,
                    "repository": str(repository.resolve()),
                    "environment_ids": [
                        str(environment.get("environmentId", ""))
                        for environment in binding.get("environments", [])
                        if isinstance(environment, Mapping) and environment.get("environmentId")
                    ],
                    "scan_id": scan_id,
                    "approval_request_count": 0,
                    "approval_granted": False,
                    "mcp_tool_call_count": 0,
                    "matching_tool_call_count": 0,
                    "mcp_tool_calls": [],
                    "unexpected_item_seen": False,
                    "unexpected_item_types": [],
                    "provider_tool_result": None,
                    "approval_denial_reason": "",
                }
                self._active_security_cancel_approval = context
                turn_attempted = True
                try:
                    turn = self._turn_unlocked(
                        owner_thread_id,
                        prompt,
                        repository,
                        approval_policy="on-request",
                        sandbox_policy={"type": "readOnly", "networkAccess": False},
                        persist_thread_mapping=False,
                    )
                    outcome["turn_state"] = "completed"
                    outcome["turn_id"] = str(turn.get("turn_id", ""))
                    if (
                        context.get("unexpected_item_seen") is True
                        or int(context.get("mcp_tool_call_count", 0)) != 1
                        or int(context.get("matching_tool_call_count", 0)) != 1
                        or int(context.get("approval_request_count", 0)) != 1
                        or context.get("approval_granted") is not True
                    ):
                        outcome["turn_state"] = "failed"
                        outcome["failure_class"] = "SECURITY_RECOVERY_UNEXPECTED_TOOL_OR_APPROVAL_STATE"
                except Exception as exc:
                    outcome["turn_state"] = "failed"
                    failure = _finalize_turn_failure(self, exc)
                    active = self._active_turn_provenance
                    if isinstance(active, Mapping):
                        outcome["turn_id"] = str(active.get("turn_id") or "")
                    outcome["failure_class"] = (
                        str(getattr(exc, "failure_class", ""))
                        or str(getattr(exc, "termination_classification", ""))
                        or (str(failure.get("termination_classification", "")) if failure else "")
                        or "APP_SERVER_PROVIDER_ERROR"
                    )
                    outcome["turn_provenance"] = failure or {}
                    active_turn = self._active_turn_provenance
                    failed_turn_id = active_turn.get("turn_id") if isinstance(active_turn, Mapping) else None
                    if isinstance(failed_turn_id, str) and failed_turn_id:
                        if isinstance(context, dict) and context.get("turn_cancel_requested") is True:
                            outcome["turn_cancel_requested"] = True
                            outcome["turn_cancel_confirmed"] = context.get("turn_cancel_confirmed") is True
                        else:
                            outcome["turn_cancel_requested"] = True
                            try:
                                cancel_response = self.request(
                                    "turn/cancel",
                                    {"threadId": owner_thread_id, "turnId": failed_turn_id},
                                    timeout=self.config.app_server_thread_timeout,
                                )
                                outcome["turn_cancel_confirmed"] = "error" not in cancel_response
                            except Exception:
                                outcome["turn_cancel_confirmed"] = False
                finally:
                    if isinstance(self._active_turn_provenance, Mapping):
                        if not outcome.get("turn_provenance"):
                            self.last_turn_provenance = dict(self._active_turn_provenance)
                        self._active_turn_provenance = None
                    if isinstance(context, dict):
                        outcome["approval_granted"] = context.get("approval_granted") is True
                        outcome["approval_request_count"] = int(context.get("approval_request_count", 0))
                        outcome["approval_denial_reason"] = str(context.get("approval_denial_reason", ""))
                        outcome["tool_call_attempted"] = int(context.get("mcp_tool_call_count", 0)) > 0
                        outcome["tool_call_count"] = int(context.get("mcp_tool_call_count", 0))
                        outcome["matching_tool_call_count"] = int(context.get("matching_tool_call_count", 0))
                        outcome["unexpected_item_seen"] = context.get("unexpected_item_seen") is True
                        outcome["unexpected_item_types"] = list(context.get("unexpected_item_types", []))
                        if context.get("turn_cancel_requested") is True:
                            outcome["turn_cancel_requested"] = True
                            outcome["turn_cancel_confirmed"] = context.get("turn_cancel_confirmed") is True
                        cancel_failure_class = context.get("turn_cancel_failure_class")
                        if isinstance(cancel_failure_class, str) and cancel_failure_class:
                            outcome["turn_cancel_failure_class"] = cancel_failure_class
                        provider_result = context.get("provider_tool_result")
                        outcome["provider_tool_result"] = dict(provider_result) if isinstance(provider_result, Mapping) else None
                    self._active_security_cancel_approval = None
                    outcome["approval_state_cleared"] = self._active_security_cancel_approval is None
                    if turn_attempted:
                        try:
                            reset_binding = self._resume_exact_security_thread_unlocked(
                                owner_thread_id,
                                repository,
                                approval_policy="never",
                            )
                            if _thread_binding_signature(reset_binding) != _thread_binding_signature(
                                outcome.get("thread_binding", {})
                            ):
                                raise AppServerError(
                                    "The Security recovery owner thread binding changed while resetting approval policy.",
                                    failure_class="SECURITY_RECOVERY_OWNER_THREAD_BINDING_MISMATCH",
                                )
                            outcome["approval_policy_reset"] = True
                        except Exception as exc:
                            outcome["approval_policy_reset"] = False
                            outcome["approval_policy_reset_failure_class"] = (
                                str(getattr(exc, "failure_class", ""))
                                or str(getattr(exc, "termination_classification", ""))
                                or "SECURITY_RECOVERY_APPROVAL_POLICY_RESET_FAILED"
                            )
                            if not outcome.get("failure_class"):
                                outcome["failure_class"] = "SECURITY_RECOVERY_APPROVAL_POLICY_RESET_FAILED"
                if not outcome.get("turn_provenance"):
                    outcome["turn_provenance"] = dict(self.last_turn_provenance)
                return outcome
            finally:
                self._active_security_cancel_approval = None
                if not resumed:
                    outcome["approval_state_cleared"] = True
                self._event_journal = previous_journal
                self._event_context = previous_context
                self._active_dispatch_provenance = previous_dispatch_provenance

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
            require_workspace_ready=self.require_workspace_ready,
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

    def _turn_unlocked(
        self,
        thread_id: str,
        prompt: str,
        repository: Path,
        *,
        approval_policy: str | None = None,
        sandbox_policy: Mapping[str, Any] | None = None,
        persist_thread_mapping: bool = True,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": prompt}],
            "approvalPolicy": approval_policy or ("never" if self.agent.sandbox == "workspace-write" else "on-request"),
            "cwd": str(repository.resolve()),
        }
        if sandbox_policy is not None:
            params["sandboxPolicy"] = dict(sandbox_policy)
            params["runtimeWorkspaceRoots"] = _canonical_workspace_roots(repository)
        elif self.agent.sandbox == "workspace-write":
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
        dispatch_provenance = getattr(self, "_active_dispatch_provenance", None)
        if isinstance(dispatch_provenance, Mapping):
            self._active_turn_provenance.update(dispatch_provenance)
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
        self._request_security_recovery_turn_cancel()

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
                if self._consume_security_recovery_turn_cancel_response(message):
                    continue
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
        if persist_thread_mapping:
            _save_thread_mapping(
                self.config,
                self.agent,
                repository,
                thread_id,
                role=self.role,
                windows_sandbox=self.windows_sandbox,
                require_workspace_ready=bool(getattr(self, "require_workspace_ready", False)),
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
        dispatch_provenance: Mapping[str, Any] | None = None,
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
            previous_dispatch_provenance = getattr(self, "_active_dispatch_provenance", None)
            self.last_turn_provenance = {}
            self._event_journal = journal
            self._event_context = {str(key): str(value) for key, value in context.items()}
            self._active_dispatch_provenance = dict(dispatch_provenance or {})
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
                self._active_dispatch_provenance = previous_dispatch_provenance

    def close(self) -> None:
        lock = getattr(self, "_lock", None)
        if lock is None:
            self._close_unlocked()
            return
        with lock:
            self._close_unlocked()

    def _close_unlocked(self) -> None:
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
_PROCESSES: dict[tuple[str, str, str, str], _AppServerProcess] = {}
_PROCESS_SLOT_LOCKS: dict[tuple[str, str, str, str], threading.RLock] = {}


def _process_slot_lock(key: tuple[str, str, str, str]) -> threading.RLock:
    with _PROCESS_LOCK:
        return _PROCESS_SLOT_LOCKS.setdefault(key, threading.RLock())


def _process_key(
    agent: AgentConfig,
    config: OrchestratorConfig,
    repository: Path | None = None,
    role: str = "",
) -> tuple[str, str, str, str]:
    return (
        agent.account_name,
        str(agent.codex_home.expanduser().resolve()),
        str(repository.expanduser().resolve()) if repository is not None else "",
        str(role or ""),
    )


def _get_process(
    config: OrchestratorConfig,
    agent: AgentConfig,
    repository: Path,
    progress: Callable[[str], None] | None,
    role: str = "",
    require_workspace_ready: bool = False,
    dispatch_provenance: dict[str, Any] | None = None,
) -> _AppServerProcess:
    key = _process_key(agent, config, repository, role)
    requested_identity = _runtime_config_identity(agent, config, repository, role, require_workspace_ready)
    slot_lock = _process_slot_lock(key)
    with slot_lock:
        with _PROCESS_LOCK:
            process = _PROCESSES.get(key)
        alive = process is not None and process.process.poll() is None
        runtime_matches = bool(
            alive and getattr(process, "runtime_config_identity", None) == requested_identity
        )
        if runtime_matches:
            assert process is not None
            process.progress = progress
            if dispatch_provenance is not None:
                timeout, timeout_source = _effective_turn_timeout(process.agent, process.config)
                dispatch_provenance.update(
                    {
                        "turn_timeout_seconds": timeout,
                        "timeout_source": timeout_source,
                        "runtime_config_identity": process.runtime_config_identity,
                        "process_reuse_state": "reused_process",
                        "reused_process_runtime_identity_matched": True,
                    }
                )
            return process
        if process is not None:
            with _PROCESS_LOCK:
                if _PROCESSES.get(key) is process:
                    _PROCESSES.pop(key, None)
            process.close()
            if getattr(process, "runtime_config_identity", None) != requested_identity:
                _delete_thread_mapping(process.config, process.agent, Path(process.repository), role=process.role)
        process = _AppServerProcess(
            config=config,
            agent=agent,
            repository=repository,
            progress=progress,
            role=role,
            require_workspace_ready=require_workspace_ready,
        )
        with _PROCESS_LOCK:
            _PROCESSES[key] = process
        if dispatch_provenance is not None:
            timeout, timeout_source = _effective_turn_timeout(process.agent, process.config)
            dispatch_provenance.update(
                {
                    "turn_timeout_seconds": timeout,
                    "timeout_source": timeout_source,
                    "runtime_config_identity": process.runtime_config_identity,
                    "process_reuse_state": "new_process",
                    "reused_process_runtime_identity_matched": None,
                }
            )
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
    slot_lock = _process_slot_lock(key)
    with slot_lock:
        with _PROCESS_LOCK:
            current_process = _PROCESSES.get(key) is process
            if current_process:
                _PROCESSES.pop(key, None)
        if current_process and repository is not None:
            _delete_thread_mapping(process.config, process.agent, Path(repository), role=getattr(process, "role", ""))
        process.close()


atexit.register(_close_processes)


def _mapping_path(config: OrchestratorConfig, agent: AgentConfig, repository: Path, role: str = "") -> Path:
    key = f"{agent.account_name}|{agent.codex_home.resolve()}|{repository.resolve()}|{role or ''}".encode("utf-8")
    return config.runs_dir / "app-server-sessions" / (hashlib.sha256(key).hexdigest() + ".json")


def _load_thread_mapping(
    config: OrchestratorConfig,
    agent: AgentConfig,
    repository: Path,
    *,
    role: str = "",
    windows_sandbox: str = "",
    require_workspace_ready: bool = False,
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
        or value.get("thread_mapping_identity")
        != _thread_mapping_identity(agent, config, repository, role, require_workspace_ready)
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
    require_workspace_ready: bool = False,
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
            "thread_mapping_identity": _thread_mapping_identity(
                agent, config, repository, role, require_workspace_ready
            ),
            "thread_id": thread_id,
            "windows_sandbox": windows_sandbox if os.name == "nt" else "",
            "headless_raw_events": _HEADLESS_RAW_EVENTS_VERSION if os.name == "nt" else "",
        },
    )


def _delete_thread_mapping(
    config: OrchestratorConfig,
    agent: AgentConfig,
    repository: Path,
    *,
    role: str = "",
) -> None:
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
    dispatch_started: Callable[[Mapping[str, Any]], None] | None = None,
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
            dispatch_provenance=metadata,
        )
        dispatch_record = {
            field: metadata.get(field)
            for field in (
                "turn_timeout_seconds",
                "timeout_source",
                "runtime_config_identity",
                "process_reuse_state",
                "reused_process_runtime_identity_matched",
            )
        }
        if dispatch_started is not None:
            try:
                dispatch_started(dispatch_record)
            except BaseException:
                _discard_process(process)
                raise
        metadata["app_server_dispatch_provenance"] = dispatch_record
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
                dispatch_provenance=dispatch_record,
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


def run_codex_security_cancel_turn(
    *,
    config: OrchestratorConfig,
    agent: AgentConfig,
    repository: Path,
    owner_thread_id: str,
    scan_id: str,
) -> dict[str, Any]:
    """Resume an exact owner thread and request one exact Codex Security cancellation."""

    if agent.backend != "app_server" or agent.sandbox != "workspace-write":
        raise AppServerError(
            "Security recovery requires the configured workspace-write App Server Executor profile.",
            failure_class="SECURITY_RECOVERY_EXECUTOR_PROFILE_UNSUPPORTED",
        )
    prompt = (
        f"Cancel only Codex Security scan {scan_id} using the official Codex Security cancellation tool available "
        "to this owner thread. Do not cancel, start, or modify any other scan. Do not make repository edits or use "
        "any other tool. After the tool call, report only the exact result."
    )
    process: _AppServerProcess | None = None
    result: dict[str, Any] = {
        "thread_id": owner_thread_id,
        "turn_state": "not_started",
        "failure_class": "SECURITY_RECOVERY_APP_SERVER_NO_RESULT",
    }
    try:
        process = _AppServerProcess(
            config=config,
            agent=agent,
            repository=repository,
            progress=None,
            role="executor",
            require_workspace_ready=True,
            security_recovery_mode=True,
        )
        run = getattr(process, "run_security_cancel_turn", None)
        if not callable(run):
            raise AppServerError(
                "The configured App Server adapter does not support exact-thread Security recovery.",
                failure_class="SECURITY_RECOVERY_ADAPTER_UNSUPPORTED",
            )
        result = run(repository, owner_thread_id, scan_id, prompt)
        if not isinstance(result, dict):
            result = {
                "thread_id": owner_thread_id,
                "turn_state": "failed",
                "failure_class": "SECURITY_RECOVERY_APP_SERVER_INVALID_RESULT",
            }
    except Exception as exc:
        failure_class = (
            str(getattr(exc, "failure_class", ""))
            or str(getattr(exc, "termination_classification", ""))
            or type(exc).__name__.upper()
        )
        if not re.fullmatch(r"[A-Z0-9_]{1,128}", failure_class):
            failure_class = type(exc).__name__.upper()
        result = {
            "thread_id": owner_thread_id,
            "turn_state": "failed",
            "failure_class": failure_class,
        }
    finally:
        close_error = False
        if process is not None:
            try:
                process.close()
            except Exception:
                close_error = True
        try:
            closed = process is None or bool(process.process.poll() is not None)
        except Exception:
            closed = False
        result["temporary_tool_policy_cleared"] = bool(closed and not close_error)
        if not result["temporary_tool_policy_cleared"]:
            result["failure_class"] = "SECURITY_RECOVERY_TOOL_POLICY_CLEAR_FAILED"
    return result


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
