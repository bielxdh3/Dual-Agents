from __future__ import annotations

"""Claude Code headless transport.

The host owns role selection, the canonical bootstrap, and workspace binding;
this module owns only the verified Claude Code process/protocol boundary.
"""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any, Callable

from .config import AgentConfig
from .process import CommandResult, _prepare_command


# This is the provider contract, not a scrape of the installed CLI help.  The
# Claude docs explicitly warn that `claude --help` is incomplete, so aliases
# such as `haiku` must remain available when the help window omits them.
_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
_CLAUDE_MODEL_CATALOG = (
    {
        "id": "sonnet",
        "family": "sonnet",
        "display_name": "Sonnet",
        "description": "Latest Sonnet model for daily coding tasks.",
        "reasoning_efforts": _EFFORT_LEVELS,
        "default_reasoning": "high",
    },
    {
        "id": "opus",
        "family": "opus",
        "display_name": "Opus",
        "description": "Latest Opus model for complex reasoning tasks.",
        "reasoning_efforts": _EFFORT_LEVELS,
        "default_reasoning": "high",
    },
    {
        "id": "haiku",
        "family": "haiku",
        "display_name": "Haiku",
        "description": "Fast and efficient Haiku model for simple tasks.",
        "reasoning_efforts": (),
        "default_reasoning": None,
        "access_caveat": "This model does not expose configurable effort.",
    },
    {
        "id": "fable",
        "family": "fable",
        "display_name": "Fable",
        "description": "Fable model for the hardest and longest-running tasks.",
        "reasoning_efforts": _EFFORT_LEVELS,
        "default_reasoning": "high",
        "access_caveat": "Requires Fable access; provider/account entitlement is checked at runtime.",
    },
    {
        "id": "best",
        "family": "best",
        "display_name": "Best",
        "description": "Fable when available, otherwise Opus.",
        "reasoning_efforts": _EFFORT_LEVELS,
        "default_reasoning": "high",
        "access_caveat": "Resolves to Fable when available, otherwise Opus; entitlement is checked at runtime.",
    },
    {
        "id": "sonnet[1m]",
        "family": "sonnet",
        "display_name": "Sonnet (1M context)",
        "description": "Sonnet with a one-million-token context window.",
        "reasoning_efforts": _EFFORT_LEVELS,
        "default_reasoning": "high",
    },
    {
        "id": "opus[1m]",
        "family": "opus",
        "display_name": "Opus (1M context)",
        "description": "Opus with a one-million-token context window.",
        "reasoning_efforts": _EFFORT_LEVELS,
        "default_reasoning": "high",
    },
    {
        "id": "opusplan",
        "family": "opusplan",
        "display_name": "Opus plan",
        "description": "Opus in plan mode, then Sonnet for execution.",
        "reasoning_efforts": _EFFORT_LEVELS,
        "default_reasoning": "high",
    },
)
_CLAUDE_MODEL_BY_ID = {row["id"]: row for row in _CLAUDE_MODEL_CATALOG}
_MODEL_ALIAS_HINTS = tuple(row["id"] for row in _CLAUDE_MODEL_CATALOG)
_SESSION_ID = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SECRET_MARKERS = re.compile(
    r"(?i)(authorization\s*:\s*bearer\s+|\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password)\b\s*[:=])[^\s,}]+"
)
_AUTH_PATH = re.compile(r"(?i)(?:[A-Za-z]:)?[^\r\n\s\"']*(?:\.credentials\.json|auth\.json)")
_SECRET_ENV_NAME = re.compile(
    r"(?i)(?:^|_)(?:api[_-]?key|key|token|secret|password|credential|cookie)(?:$|_)"
)


def _safe_error(value: Any, limit: int = 800) -> str:
    text = _AUTH_PATH.sub("[REDACTED_AUTH_PATH]", str(value or ""))
    text = _SECRET_MARKERS.sub("[REDACTED_SECRET]", text)
    return " ".join(text.replace("\r", " ").replace("\n", " ").split())[:limit]


def _resolve_command(command: str) -> str:
    resolved = shutil.which(command)
    if resolved:
        return resolved
    candidate = Path(command).expanduser()
    return str(candidate) if candidate.exists() else ""


def _run_capture(command: list[str], *, cwd: Path, env: dict[str, str] | None = None, timeout: float = 15.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _prepare_command(command),
        cwd=cwd,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        shell=False,
        check=False,
    )


def _help_text(command: str, *, cwd: Path) -> tuple[str, str | None]:
    resolved = _resolve_command(command)
    if not resolved:
        return "", "Claude Code CLI was not found. Install it from the official Anthropic distribution."
    try:
        result = _run_capture([resolved, "--help"], cwd=cwd)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "", _safe_error(exc)
    text = f"{result.stdout}\n{result.stderr}"
    if result.returncode != 0 or not text.strip():
        return "", _safe_error(text or f"Claude Code --help exited with {result.returncode}.")
    return text, None


def _runtime_version(command: str, *, cwd: Path) -> str:
    resolved = _resolve_command(command)
    if not resolved:
        return ""
    try:
        result = _run_capture([resolved, "--version"], cwd=cwd)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    value = (result.stdout or result.stderr).strip().splitlines()
    return _safe_error(value[0], 200) if value else ""


def _flag(help_text: str, name: str) -> bool:
    return bool(re.search(rf"(?<![A-Za-z0-9_-]){re.escape(name)}(?![A-Za-z0-9_-])", help_text))


def _parse_choices(help_text: str, flag: str, values: tuple[str, ...]) -> tuple[str, ...]:
    marker = re.search(rf"{re.escape(flag)}\b", help_text, re.IGNORECASE)
    if not marker:
        return ()
    window = help_text[marker.end() : marker.end() + 400]
    match = re.search(r"\(([^)]*)\)|\[([^]]*)\]", window, re.IGNORECASE | re.DOTALL)
    if not match:
        return ()
    haystack = ",".join(part or "" for part in match.groups()).casefold()
    return tuple(value for value in values if value.casefold() in haystack)


def _model_capability(model: str) -> dict[str, Any] | None:
    """Return the verified capability family for an alias or known full ID."""

    value = str(model or "").strip()
    if not value:
        return None
    row = _CLAUDE_MODEL_BY_ID.get(value)
    if row is not None:
        return row
    # Full Anthropic model IDs are valid Claude Code model values.  Only map
    # IDs whose family is unambiguous; arbitrary aliases stay unsupported.
    lowered = value.casefold()
    family = next((name for name in ("fable", "opus", "sonnet", "haiku") if f"claude-{name}-" in lowered), None)
    if family is None:
        return None
    return next(row for row in _CLAUDE_MODEL_CATALOG if row["family"] == family)


def _verified_models(account: Any, help_text: str, *, effort_levels: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    del help_text  # `--help` is intentionally not the model catalog source.
    supported_efforts = tuple(effort_levels or _EFFORT_LEVELS)
    configured = [str(value).strip() for value in getattr(account, "available_models", ()) if str(value).strip()]
    configured_model = str(getattr(account, "model", "") or "").strip()
    configured_runtime_model = str(getattr(account, "runtime_model", "") or "").strip()
    values = list(_MODEL_ALIAS_HINTS)
    for value in (*configured, configured_model, configured_runtime_model):
        if value and value not in values and _model_capability(value) is not None:
            values.append(value)
    rows = []
    for index, value in enumerate(values):
        contract = _model_capability(value)
        if contract is None:
            continue
        efforts = tuple(item for item in contract["reasoning_efforts"] if item in supported_efforts)
        row = {
            "id": value,
            "model": value,
            "display_name": contract["display_name"] if value == contract["id"] else value,
            "description": contract["description"],
            "access_caveat": contract.get("access_caveat", ""),
            "selectable": True,
            "is_default": index == 0,
            "hidden": False,
            "default_reasoning": contract["default_reasoning"] if efforts else None,
            "reasoning_efforts": list(efforts),
            "fixed_mode": "",
            "runtime_model": value,
            "runtime_variants": {},
            "default_service_tier": None,
            "service_tiers": [],
        }
        rows.append(row)
    return rows


def _state_root(agent: AgentConfig, repository: Path) -> Path:
    root = (agent.state_root or agent.codex_home).expanduser().resolve()
    repo = repository.expanduser().resolve()
    if root == repo or root.is_relative_to(repo) or repo.is_relative_to(root):
        raise ValueError("Claude state root must not be the selected repository or a child of it.")
    if root == Path.home().resolve() or root == Path(r"C:\CodexGlobal").resolve():
        raise ValueError("Claude state root must be an isolated profile directory.")
    return root


def claude_environment(agent: AgentConfig, repository: Path) -> dict[str, str]:
    """Build a child environment without copying or exposing credentials."""

    auth_variable = ""
    if agent.auth_mode == "environment":
        reference = str(agent.auth_reference or "").strip()
        if not reference.startswith("env:") or not _ENV_NAME.fullmatch(reference[4:]):
            raise ValueError("Claude environment authentication requires an env:VARIABLE reference.")
        auth_variable = reference[4:]
    auth_value = os.environ.get(auth_variable, "") if auth_variable else ""
    if auth_variable and not auth_value:
        raise RuntimeError(f"Claude authentication environment variable '{auth_variable}' is not set.")
    env = os.environ.copy()
    scrub_names = {
        "CODEX_HOME",
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        # Do not inherit the CLI's legacy bare-mode switch.  Bare mode skips
        # Claude.ai/keychain authentication; managed calls use --safe-mode so
        # customizations are disabled while provider-owned auth still works.
        "CLAUDE_CODE_SIMPLE",
        "ANTHROPIC_PROFILE",
    }
    for name in list(env):
        if name.upper() in scrub_names or _SECRET_ENV_NAME.search(name):
            env.pop(name, None)
    env["CLAUDE_CONFIG_DIR"] = str(_state_root(agent, repository))
    if auth_value:
        env["ANTHROPIC_API_KEY"] = auth_value
    return env


def claude_status(command: str, *, cwd: Path, account: Any | None = None) -> str:
    """Return a sanitized runtime/auth status without starting a model turn."""

    resolved = _resolve_command(command)
    if not resolved:
        return "NOT FOUND"
    env = None
    if account is not None and getattr(account, "auth_mode", "provider_native") == "environment":
        reference = str(getattr(account, "auth_reference", ""))
        if not reference.startswith("env:") or not os.environ.get(reference[4:]):
            return "NOT CONFIGURED"
        try:
            claude_environment(account, cwd)
        except (OSError, RuntimeError, ValueError):
            return "NOT CONFIGURED"
        # An environment-backed profile has an explicit, user-owned secret
        # reference.  Doctor may only report claude.ai subscription state and
        # cannot validate that API key without spending a model turn.
        return "OK"
    elif account is not None:
        try:
            env = claude_environment(account, cwd)
        except (OSError, RuntimeError, ValueError):
            return "UNKNOWN"
    # `doctor` is installation-oriented and may omit authentication state even
    # for a valid Claude.ai session.  Use the documented machine-readable auth
    # command first, then retain the doctor fallback for older CLIs.
    try:
        auth_result = _run_capture([resolved, "auth", "status", "--json"], cwd=cwd, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return "UNKNOWN"
    try:
        auth_payload = json.loads(auth_result.stdout or "")
    except (TypeError, ValueError):
        auth_payload = None
    if isinstance(auth_payload, dict) and isinstance(auth_payload.get("loggedIn"), bool):
        return "OK" if auth_payload["loggedIn"] else "NOT LOGGED IN"
    try:
        result = _run_capture([resolved, "doctor"], cwd=cwd, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return "UNKNOWN"
    text = f"{result.stdout}\n{result.stderr}".casefold()
    if any(marker in text for marker in ("not logged in", "not signed in", "login required", "authentication required", "login expired", "please log in", "no usable credentials", "no api key")):
        return "NOT LOGGED IN"
    if result.returncode != 0:
        return "UNKNOWN"
    if any(marker in text for marker in ("authenticated", "logged in", "login:", "profile:")):
        return "OK"
    return "UNKNOWN"


def capability_snapshot(command: str, *, cwd: Path, account: Any, role: str | None = None) -> dict[str, Any]:
    help_text, error = _help_text(command, cwd=cwd)
    if error:
        return {"available": False, "error": error, "help": "", "roles": ()}
    required = (
        "--print",
        "--output-format",
        "--json-schema",
        "--permission-mode",
        "--permission-prompts",
        "--tools",
        "--resume",
        "--safe-mode",
        "--restricted",
    )
    missing = [name for name in required if not _flag(help_text, name)]
    read_roles = not missing
    if not read_roles:
        return {
            "available": True,
            "error": (
                "Claude Code cannot safely enforce unattended read-only permissions with the installed CLI."
                if not missing
                else f"Claude Code is missing required headless flags: {', '.join(missing)}."
            ),
            "help": help_text,
            "roles": (),
        }
    # Native Windows has no Claude OS command sandbox.  `--restricted` still
    # provides a bounded built-in file-tool surface, so Executor is exposed
    # only as file-edit-only; command-capable execution remains unavailable.
    roles = ("architect", "reviewer", "executor") if read_roles else ()
    if role and role not in roles:
        return {
            "available": True,
            "error": f"Claude Code cannot safely support the '{role}' role with the installed CLI.",
            "help": help_text,
            "roles": roles,
        }
    # Keep the installed flag as a capability sanity check, but use the
    # documented Claude effort contract for model rows.  Account-level effort
    # metadata is not a Claude model catalog and may be empty or stale.
    efforts = _parse_choices(help_text, "--effort", _EFFORT_LEVELS) or _EFFORT_LEVELS
    return {
        "available": True,
        "error": None if not missing else f"Claude Code is missing required headless flags: {', '.join(missing)}.",
        "help": help_text,
        "roles": roles,
        "efforts": efforts,
        "models": _verified_models(account, help_text, effort_levels=efforts),
        "runtime_version": _runtime_version(command, cwd=cwd),
    }


def build_command(
    *,
    command: str,
    agent: AgentConfig,
    role: str,
    prompt: str,
    schema: str,
    help_text: str,
    session_id: str = "",
) -> list[str]:
    """Construct a bounded, non-interactive Claude Code invocation."""

    runtime_model = getattr(agent, "runtime_model", "") or agent.model
    contract = _model_capability(runtime_model)
    if runtime_model and contract is None:
        raise ValueError(f"Selected Claude model '{runtime_model}' is not in the verified Claude Code catalog.")
    if agent.reasoning_effort:
        allowed = tuple(contract["reasoning_efforts"]) if contract is not None else _parse_choices(help_text, "--effort", _EFFORT_LEVELS)
        if agent.reasoning_effort not in allowed:
            raise ValueError(
                f"Claude effort '{agent.reasoning_effort}' is not supported by model '{runtime_model or 'provider default'}'."
            )

    permission_mode = "acceptEdits" if role == "executor" else "plan"
    # On native Windows Executor is deliberately file-edit-only.  Restricted
    # mode removes command/code tools; never name Bash or another shell here.
    tools = "Edit,Write,Read,Glob,Grep" if role == "executor" else "Read,Glob,Grep"
    result = [
        str(command),
        # --bare disables Claude.ai/keychain authentication.  --safe-mode is
        # the supported isolation mode: customizations stay disabled while
        # authentication, model selection, tools, and permissions work.
        "--safe-mode",
        "--print",
        "--output-format",
        "json",
        "--json-schema",
        schema,
        "--permission-mode",
        permission_mode,
        "--permission-prompts",
        "none",
        "--tools",
        tools,
        "--max-turns",
        "20",
    ]
    result.insert(2, "--restricted")
    if runtime_model:
        result.extend(["--model", runtime_model])
    if agent.reasoning_effort:
        result.extend(["--effort", agent.reasoning_effort])
    if session_id:
        result.extend(["--resume", session_id])
    result.append(prompt)
    return result


def _session_path(config: Any) -> Path:
    return Path(config.runs_dir).expanduser().resolve() / ".claude-sessions.json"


def _session_key(agent: AgentConfig, repository: Path, role: str) -> str:
    raw = json.dumps(
        {"profile": agent.account_name, "repository": str(repository.resolve()), "role": role},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _load_session(config: Any, agent: AgentConfig, repository: Path, role: str) -> str:
    path = _session_path(config)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return ""
    entry = payload.get(_session_key(agent, repository, role)) if isinstance(payload, dict) else None
    value = entry.get("session_id") if isinstance(entry, dict) else None
    return str(value) if isinstance(value, str) and _SESSION_ID.fullmatch(value) else ""


def _save_session(config: Any, agent: AgentConfig, repository: Path, role: str, session_id: str) -> None:
    if not _SESSION_ID.fullmatch(session_id):
        return
    path = _session_path(config)
    try:
        payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    key = _session_key(agent, repository, role)
    payload[key] = {
        "profile_id": agent.account_name,
        "repository": str(repository.resolve()),
        "role": role,
        "session_id": session_id,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _failure_class(text: str) -> str | None:
    lowered = text.casefold()
    for markers, value in (
        (("login expired", "not logged in", "authentication", "unauthorized", "401", "403"), "authentication_unavailable"),
        (("environment variable", "not configured", "credential"), "authentication_unavailable"),
        (("rate limit", "rate_limit", "quota", "billing"), "quota_exhausted"),
        (("model not found", "model unavailable", "unknown model"), "model_unavailable"),
        (("overloaded", "service unavailable", "temporarily unavailable", "server error"), "provider_unavailable"),
        (("timed out", "timeout", "no response"), "timeout"),
        (("connection", "network", "econn", "transport"), "transport_unavailable"),
    ):
        if any(marker in lowered for marker in markers):
            return value
    return None


def _result_payload(stdout: str) -> dict[str, Any] | None:
    candidates = [stdout.strip(), *[line.strip() for line in stdout.splitlines()[::-1] if line.strip()]]
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            return value
    return None


def _claude_schema(schema_path: Path) -> str:
    """Return the schema contract in the dialect accepted by Claude Code.

    Claude Code validates the contract itself and does not accept the
    repository's Draft 2020-12 ``$schema`` identifier.  Removing only that
    dialect marker preserves every validation keyword and keeps structured
    output enforcement intact.
    """

    text = schema_path.read_text(encoding="utf-8")
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return text
    if isinstance(value, dict):
        value = dict(value)
        value.pop("$schema", None)
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return text


def run_claude_code(
    *,
    command: str,
    agent: AgentConfig,
    role: str,
    repository: Path,
    prompt: str,
    output_path: Path,
    schema_path: Path,
    config: Any,
    progress: Callable[[str], None] | None = None,
) -> CommandResult:
    repository = repository.expanduser().resolve()
    managed_tools = "Edit,Write,Read,Glob,Grep" if role == "executor" else "Read,Glob,Grep"
    metadata: dict[str, Any] = {
        "executor_provider": "anthropic",
        "provider_adapter": "claude_code",
        "provider": "anthropic",
        "adapter": "claude_code",
        "profile_id": agent.account_name,
        "model": agent.model,
        "reasoning_effort": agent.reasoning_effort or "provider-default",
        "auth_mode": agent.auth_mode,
        "claude_repository": str(repository),
        "claude_cwd": str(repository),
        "claude_permission_mode": "acceptEdits" if role == "executor" else "plan",
        "claude_safe_mode": True,
        "claude_restricted": True,
        "claude_tools": managed_tools,
        "claude_denied_tools": "Bash,PowerShell,shell,command,code,WebFetch,network,MCP",
        "claude_executor_capability": "file-edit-only" if role == "executor" else "read-only",
        "claude_transport": "json",
        "claude_session_id": "",
        "claude_runtime_version": "",
    }
    if not repository.is_dir():
        metadata["availability_failure_class"] = "workspace_unavailable"
        return CommandResult([command], 1, "", f"Claude workspace does not exist: {repository}", metadata)
    protected_workspaces = {
        Path.home().resolve(),
        Path(r"C:\CodexGlobal").resolve(),
        (agent.state_root or agent.codex_home).expanduser().resolve(),
    }
    if repository in protected_workspaces:
        metadata["capability_failure"] = True
        return CommandResult(
            [command],
            1,
            "",
            "Claude workspace is a protected host or profile-state directory.",
            metadata,
        )
    if not schema_path.is_file():
        metadata["capability_failure"] = True
        return CommandResult([command], 1, "", f"Claude schema does not exist: {schema_path}", metadata)
    snapshot = capability_snapshot(command, cwd=repository, account=agent, role=role)
    metadata["claude_runtime_version"] = snapshot.get("runtime_version", "")
    if not snapshot.get("available"):
        metadata["availability_failure_class"] = "process_unavailable"
        return CommandResult([command, "--help"], 127, "", _safe_error(snapshot.get("error")), metadata)
    if snapshot.get("error"):
        metadata["capability_failure"] = True
        return CommandResult([command, "--help"], 2, "", _safe_error(snapshot["error"]), metadata)
    auth_status = claude_status(command, cwd=repository, account=agent)
    metadata["claude_auth_status"] = auth_status
    if auth_status == "NOT FOUND":
        metadata["availability_failure_class"] = "process_unavailable"
        return CommandResult([command, "auth", "status"], 127, "", "Claude Code CLI was not found.", metadata)
    if auth_status in {"NOT CONFIGURED", "NOT LOGGED IN"}:
        metadata["availability_failure_class"] = "authentication_unavailable"
        return CommandResult([command, "auth", "status"], 1, "", "Claude Code authentication is not configured for this profile.", metadata)
    if auth_status != "OK":
        metadata["capability_failure"] = True
        return CommandResult([command, "auth", "status"], 2, "", "Claude Code authentication status could not be verified safely.", metadata)
    model_rows = tuple(row for row in snapshot.get("models", ()) if isinstance(row, dict))
    models = {row.get("id") for row in model_rows}
    selected_model = getattr(agent, "runtime_model", "") or agent.model
    if selected_model and selected_model not in models:
        metadata["availability_failure_class"] = "model_unavailable"
        return CommandResult([command, "--model", selected_model], 1, "", "Selected Claude model is not verified by this adapter.", metadata)
    selected_row = next((row for row in model_rows if row.get("id") == selected_model), None)
    allowed_efforts = (
        tuple(selected_row.get("reasoning_efforts", ()))
        if selected_row is not None and "reasoning_efforts" in selected_row
        else tuple(snapshot.get("efforts", ()))
    )
    if agent.reasoning_effort and agent.reasoning_effort not in allowed_efforts:
        metadata["capability_failure"] = True
        return CommandResult(
            [command, "--effort", agent.reasoning_effort],
            2,
            "",
            "Selected Claude effort is not supported by the configured model.",
            metadata,
        )
    try:
        env = claude_environment(agent, repository)
        session_id = _load_session(config, agent, repository, role)
        schema = _claude_schema(schema_path)
        command_argv = build_command(
            command=command,
            agent=agent,
            role=role,
            prompt=prompt,
            schema=schema,
            help_text=str(snapshot.get("help", "")),
            session_id=session_id,
        )
    except (OSError, UnicodeError, RuntimeError, ValueError) as exc:
        text = _safe_error(exc)
        failure = _failure_class(text)
        if failure:
            metadata["availability_failure_class"] = failure
        else:
            metadata["capability_failure"] = True
        return CommandResult([command], 1, "", text, metadata)
    if progress:
        progress("Claude Code turn started")
    timeout = max(float(getattr(config, "claude_turn_timeout", getattr(config, "app_server_turn_timeout", 600.0))), 1.0)
    try:
        completed = subprocess.run(
            _prepare_command(command_argv),
            cwd=repository,
            env=env,
            input=None,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            shell=False,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        metadata["availability_failure_class"] = "timeout"
        return CommandResult(command_argv, 124, "", "Claude Code turn timed out.", metadata)
    except OSError as exc:
        metadata["availability_failure_class"] = "process_unavailable"
        return CommandResult(command_argv, 127, "", _safe_error(exc), metadata)
    stderr = _safe_error(completed.stderr)
    payload = _result_payload(completed.stdout)
    if completed.returncode != 0:
        text = _safe_error((payload or {}).get("result", "") if isinstance(payload, dict) else stderr)
        text = text or stderr or f"Claude Code exited with {completed.returncode}."
        failure = _failure_class(text)
        if failure:
            metadata["availability_failure_class"] = failure
        return CommandResult(command_argv, completed.returncode, "", text, metadata)
    if not isinstance(payload, dict):
        metadata["availability_failure_class"] = "unusable_runtime"
        return CommandResult(command_argv, 1, "", "Claude Code returned malformed JSON output.", metadata)
    session_value = payload.get("session_id")
    if not isinstance(session_value, str) or not _SESSION_ID.fullmatch(session_value):
        metadata["availability_failure_class"] = "unusable_runtime"
        return CommandResult(command_argv, 1, "", "Claude Code returned no usable session ID.", metadata)
    metadata["claude_session_id"] = session_value
    metadata["claude_subtype"] = str(payload.get("subtype", ""))
    metadata["claude_is_error"] = bool(payload.get("is_error"))
    if payload.get("is_error"):
        text = _safe_error(payload.get("result") or payload.get("error") or "Claude Code reported an error.")
        failure = _failure_class(text)
        if failure:
            metadata["availability_failure_class"] = failure
        return CommandResult(command_argv, 1, "", text, metadata)
    structured = payload.get("structured_output")
    if not isinstance(structured, (dict, list)):
        metadata["availability_failure_class"] = "unusable_runtime"
        return CommandResult(command_argv, 1, "", "Claude Code returned no usable structured result.", metadata)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(structured, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    _save_session(config, agent, repository, role, session_value)
    return CommandResult(command_argv, 0, str(payload.get("result") or ""), stderr, metadata)


class ClaudeCodeAdapter:
    provider = "anthropic"
    adapter = "claude_code"

    def capabilities(self, config: Any, account: Any) -> Any:
        from .providers import ProviderCapabilities

        snapshot = capability_snapshot(config.claude_command, cwd=config.project_root, account=account)
        if not snapshot.get("available"):
            return ProviderCapabilities(
                provider=self.provider,
                provider_label="Anthropic Claude",
                adapter=self.adapter,
                credential_status="unknown",
                runtime_status="Unavailable",
                isolation_note="Claude Code credentials remain provider-owned; no credential files are copied.",
                profile_isolation=True,
                error=_safe_error(snapshot.get("error")),
            )
        roles = tuple(snapshot.get("roles", ()))
        efforts = tuple(snapshot.get("efforts", ()))
        auth_status = claude_status(config.claude_command, cwd=config.project_root, account=account)
        credential_status = "configured" if auth_status == "OK" else "missing" if auth_status in {"NOT CONFIGURED", "NOT LOGGED IN"} else "unknown"
        runtime_status = "Authenticated" if not snapshot.get("error") and auth_status == "OK" else "Unavailable"
        return ProviderCapabilities(
            provider=self.provider,
            provider_label="Anthropic Claude",
            adapter=self.adapter,
            models=tuple(snapshot.get("models", ())),
            effort_levels=efforts,
            model_list=True,
            reasoning=bool(efforts),
            streaming=True,
            structured_output=True,
            conversation_resume=True,
            workspace_binding=True,
            tool_use=True,
            profile_isolation=True,
            isolation_note=(
                "Claude Code state and credentials are isolated with CLAUDE_CONFIG_DIR; "
                "Dual Agents never copies credential material. Executor is file-edit-only; "
                "command-running requires an unavailable native OS sandbox."
            ),
            credential_status=credential_status,
            runtime_status=runtime_status,
            authenticated=auth_status == "OK",
            error=_safe_error(snapshot.get("error")) if snapshot.get("error") else None,
            supported_roles=roles,
        )


def claude_adapter() -> ClaudeCodeAdapter:
    return ClaudeCodeAdapter()
