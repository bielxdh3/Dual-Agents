from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any
import tomllib


SUPPORTED_ROLES = ("orchestrator", "architect", "reviewer", "executor")
SUPPORTED_BACKENDS = ("app_server", "windows", "antigravity", "api", "claude_code")
SUPPORTED_PROVIDER_TYPES = ("codex", "gemini", "api", "anthropic")
SUPPORTED_ADAPTER_TYPES = ("codex_cli", "antigravity_cli", "openai_compatible", "claude_code")
SUPPORTED_AUTH_MODES = ("provider_native", "environment", "none")
_ACCOUNT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_ROLE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_SETTING_VALUE = re.compile(r"^[^\x00-\x1f\x7f]{0,200}$")
_ENV_REFERENCE = re.compile(r"^env:[A-Za-z_][A-Za-z0-9_]*$")
_ANTIGRAVITY_VARIANT = re.compile(r"^(?P<base>.+)-(?P<variant>low|medium|high|thinking)$", re.IGNORECASE)


class ConfigError(ValueError):
    """Raised when the registry configuration is invalid or incomplete."""


@dataclass(frozen=True)
class AccountConfig:
    name: str
    label: str
    codex_home: Path
    model: str
    reasoning_effort: str
    runtime_model: str = ""
    fixed_mode: str = ""
    backend: str = "windows"
    service_tier: str = ""
    network_access: bool = False
    provider_type: str = "codex"
    adapter_type: str = "codex_cli"
    auth_mode: str = "provider_native"
    auth_reference: str = ""
    state_root: Path | None = None
    base_url: str = ""
    available_models: tuple[str, ...] = ()
    supported_reasoning_efforts: tuple[str, ...] = ()
    enabled: bool = True
    fallback_roles: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentConfig:
    codex_home: Path
    model: str
    reasoning_effort: str
    sandbox: str
    runtime_model: str = ""
    fixed_mode: str = ""
    account_name: str = ""
    label: str = ""
    backend: str = "windows"
    service_tier: str = ""
    network_access: bool = False
    provider_type: str = "codex"
    adapter_type: str = "codex_cli"
    auth_mode: str = "provider_native"
    auth_reference: str = ""
    state_root: Path | None = None
    base_url: str = ""
    available_models: tuple[str, ...] = ()
    supported_reasoning_efforts: tuple[str, ...] = ()
    enabled: bool = True


@dataclass(frozen=True)
class OrchestratorConfig:
    repository: Path
    runs_dir: Path
    max_correction_cycles: int
    require_clean_git: bool
    codex_command: str
    accounts: dict[str, AccountConfig]
    roles: dict[str, str]
    project_root: Path
    config_path: Path
    legacy: bool = False
    antigravity_command: str = "agy"
    claude_command: str = "claude"
    node_command: str = "node"
    terminal_readiness_timeout: float = 60.0
    terminal_turn_start_timeout: float = 15.0
    app_server_initialize_timeout: float = 30.0
    app_server_thread_timeout: float = 30.0
    app_server_turn_start_timeout: float = 30.0
    app_server_turn_timeout: float = 600.0
    claude_turn_timeout: float = 600.0
    dashboard_telemetry_timeout: float = 5.0
    live_event_journal_max_records: int = 2000
    live_event_journal_max_record_bytes: int = 65536
    live_event_journal_max_detail_bytes: int = 16384
    fallback_enabled: bool = False

    @property
    def architect(self) -> AgentConfig:
        return self.agent_for_role("architect")

    @property
    def executor(self) -> AgentConfig:
        return self.agent_for_role("executor")

    def account_for_role(self, role: str) -> AccountConfig:
        if role == "reviewer" and role not in self.roles:
            role = "architect"
        account_name = self.roles.get(role)
        if not account_name:
            raise ConfigError(f"Required role '{role}' is unassigned.")
        account = self.accounts.get(account_name)
        if account is None:
            raise ConfigError(
                f"Role '{role}' refers to unknown account '{account_name}'."
            )
        if not account.enabled:
            raise ConfigError(f"Role '{role}' refers to disabled account '{account_name}'.")
        return account

    def agent_for_role(self, role: str) -> AgentConfig:
        account = self.account_for_role(role)
        sandbox = "workspace-write" if role == "executor" else "read-only"
        return AgentConfig(
            codex_home=account.codex_home,
            model=account.model,
            reasoning_effort=account.reasoning_effort,
            sandbox=sandbox,
            runtime_model=account.runtime_model,
            fixed_mode=account.fixed_mode,
            account_name=account.name,
            label=account.label,
            backend=account.backend,
            service_tier=account.service_tier,
            network_access=account.network_access,
            provider_type=account.provider_type,
            adapter_type=account.adapter_type,
            auth_mode=account.auth_mode,
            auth_reference=account.auth_reference,
            state_root=account.state_root,
            base_url=account.base_url,
            available_models=account.available_models,
            supported_reasoning_efforts=account.supported_reasoning_efforts,
            enabled=account.enabled,
        )


def validate_account_name(name: str) -> str:
    name = str(name).strip()
    if not _ACCOUNT_NAME.fullmatch(name):
        raise ConfigError(
            "Account names must start with a letter or number and contain "
            "only letters, numbers, '-' or '_'."
        )
    return name


def validate_role_name(role: str) -> str:
    role = str(role).strip()
    if not _ROLE_NAME.fullmatch(role):
        raise ConfigError(
            "Role names must start with a letter and contain only letters, "
            "numbers or '_'."
        )
    if role not in SUPPORTED_ROLES:
        raise ConfigError(
            f"Unknown role '{role}'. Supported roles: {', '.join(SUPPORTED_ROLES)}."
        )
    return role


def validate_setting_value(value: str, field: str) -> str:
    value = str(value).strip()
    if not _SETTING_VALUE.fullmatch(value):
        raise ConfigError(f"{field} must be a single line of at most 200 characters.")
    return value


def validate_auth_reference(value: str) -> str:
    value = validate_setting_value(value, "auth_reference")
    if value and not _ENV_REFERENCE.fullmatch(value):
        raise ConfigError("API credentials must use an env:VARIABLE reference.")
    return value


def normalize_antigravity_model_reference(
    model: str,
    reasoning_effort: str,
    runtime_model: str = "",
    fixed_mode: str = "",
) -> tuple[str, str, str, str]:
    """Migrate a combined agy model slug without guessing provider metadata."""
    selected = str(model or "").strip()
    runtime = str(runtime_model or "").strip() or selected
    effort = str(reasoning_effort or "").strip()
    fixed = str(fixed_mode or "").strip()
    match = _ANTIGRAVITY_VARIANT.fullmatch(selected) if selected else None
    if match:
        selected = match.group("base")
        runtime = runtime or str(model).strip()
        variant = match.group("variant").lower()
        if variant in {"low", "medium", "high"}:
            effort = variant
        if variant == "thinking":
            effort = ""
            fixed = fixed or "Thinking"
    return selected, effort, runtime, fixed


def _path(raw: Any, base: Path) -> Path:
    value = Path(str(raw)).expanduser()
    return value if value.is_absolute() else (base / value).resolve()


def _account(name: str, raw: dict[str, Any], base: Path) -> AccountConfig:
    backend = str(raw.get("backend", "windows")).strip() or "windows"
    if "codex_home" not in raw and backend != "api":
        raise ConfigError(f"Account '{name}' is missing codex_home.")
    if backend not in SUPPORTED_BACKENDS:
        raise ConfigError(
            f"Account '{name}' has unsupported backend '{backend}'. "
            f"Supported backends: {', '.join(SUPPORTED_BACKENDS)}."
        )
    network_access = raw.get("network_access", False)
    if not isinstance(network_access, bool):
        raise ConfigError(f"Account '{name}' network_access must be a boolean.")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError(f"Account '{name}' enabled must be a boolean.")
    raw_fallback_roles = raw.get("fallback_roles", [])
    if isinstance(raw_fallback_roles, str):
        raw_fallback_roles = [item.strip() for item in raw_fallback_roles.split(",") if item.strip()]
    if not isinstance(raw_fallback_roles, list) or any(not isinstance(item, str) for item in raw_fallback_roles):
        raise ConfigError(f"Account '{name}' fallback_roles must be a list of role names.")
    fallback_roles: list[str] = []
    for item in raw_fallback_roles:
        role = validate_role_name(item)
        if role not in fallback_roles:
            fallback_roles.append(role)
    default_provider = (
        "gemini" if backend == "antigravity"
        else "api" if backend == "api"
        else "anthropic" if backend == "claude_code"
        else "codex"
    )
    default_adapter = (
        "antigravity_cli" if backend == "antigravity"
        else "openai_compatible" if backend == "api"
        else "claude_code" if backend == "claude_code"
        else "codex_cli"
    )
    provider_type = str(raw.get("provider_type", default_provider)).strip() or default_provider
    adapter_type = str(raw.get("adapter_type", default_adapter)).strip() or default_adapter
    auth_mode = str(raw.get("auth_mode", "environment" if backend == "api" else "provider_native")).strip() or "provider_native"
    if provider_type not in SUPPORTED_PROVIDER_TYPES:
        raise ConfigError(f"Account '{name}' has unsupported provider_type '{provider_type}'.")
    if adapter_type not in SUPPORTED_ADAPTER_TYPES:
        raise ConfigError(f"Account '{name}' has unsupported adapter_type '{adapter_type}'.")
    if auth_mode not in SUPPORTED_AUTH_MODES:
        raise ConfigError(f"Account '{name}' has unsupported auth_mode '{auth_mode}'.")
    raw_models = raw.get("available_models", raw.get("models", []))
    if isinstance(raw_models, str):
        raw_models = [item.strip() for item in raw_models.split(",") if item.strip()]
    if not isinstance(raw_models, list) or any(not isinstance(item, str) for item in raw_models):
        raise ConfigError(f"Account '{name}' available_models must be a list of strings.")
    available_models = tuple(validate_setting_value(item, "available_models") for item in raw_models if item.strip())
    raw_efforts = raw.get("supported_reasoning_efforts", raw.get("effort_levels", []))
    if isinstance(raw_efforts, str):
        raw_efforts = [item.strip() for item in raw_efforts.split(",") if item.strip()]
    if not isinstance(raw_efforts, list) or any(not isinstance(item, str) for item in raw_efforts):
        raise ConfigError(f"Account '{name}' supported_reasoning_efforts must be a list of strings.")
    supported_reasoning_efforts = tuple(validate_setting_value(item, "supported_reasoning_efforts") for item in raw_efforts if item.strip())
    state_root = raw.get("state_root")
    state_path = _path(state_root, base) if state_root else None
    auth_reference = (
        validate_auth_reference(raw.get("auth_reference", ""))
        if backend == "api" or (backend == "claude_code" and str(raw.get("auth_mode", "provider_native")).strip() == "environment")
        else validate_setting_value(raw.get("auth_reference", ""), "auth_reference")
    )
    model = validate_setting_value(raw.get("model", ""), "model")
    has_reasoning_setting = "reasoning_effort" in raw
    reasoning_effort = validate_setting_value(raw.get("reasoning_effort", "" if backend in {"api", "claude_code"} else "high"), "reasoning_effort")
    runtime_model = validate_setting_value(raw.get("runtime_model", ""), "runtime_model")
    fixed_mode = validate_setting_value(raw.get("fixed_mode", ""), "fixed_mode")
    if backend == "antigravity":
        model, reasoning_effort, runtime_model, fixed_mode = normalize_antigravity_model_reference(
            model, reasoning_effort, runtime_model, fixed_mode
        )
    return AccountConfig(
        name=validate_account_name(name),
        label=str(raw.get("label", "")).strip(),
        codex_home=_path(raw.get("codex_home", raw.get("state_root", f".dual-codex-profiles/{name}")), base),
        model=model,
        reasoning_effort=reasoning_effort or ("" if backend in {"api", "claude_code"} or (backend == "antigravity" and (fixed_mode or has_reasoning_setting)) else "high"),
        runtime_model=runtime_model,
        fixed_mode=fixed_mode,
        backend=backend,
        service_tier=validate_setting_value(raw.get("service_tier", ""), "service_tier"),
        network_access=network_access,
        provider_type=provider_type,
        adapter_type=adapter_type,
        auth_mode=auth_mode,
        auth_reference=auth_reference,
        state_root=state_path or (_path(raw.get("codex_home"), base) if backend == "claude_code" and raw.get("codex_home") else None),
        base_url=validate_setting_value(raw.get("base_url", ""), "base_url"),
        available_models=available_models,
        supported_reasoning_efforts=supported_reasoning_efforts,
        enabled=enabled,
        fallback_roles=tuple(fallback_roles),
    )


def load_raw_config(path: Path) -> dict[str, Any]:
    path = path.expanduser().resolve()
    data = path.read_bytes()
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return tomllib.loads(data.decode("utf-8"))


def is_legacy_raw(raw: dict[str, Any]) -> bool:
    return "accounts" not in raw and ("architect" in raw or "executor" in raw)


def load_config(path: Path) -> OrchestratorConfig:
    path = path.expanduser().resolve()
    raw = load_raw_config(path)
    base = path.parent

    try:
        orch = raw["orchestrator"]
        if not isinstance(orch, dict):
            raise TypeError
    except (KeyError, TypeError) as exc:
        raise ConfigError("Missing [orchestrator] configuration.") from exc

    repository = _path(orch["repository"], base)
    runs_dir = _path(orch.get("runs_dir", "runs"), base)
    fallback_enabled = orch.get("fallback_enabled", False)
    if not isinstance(fallback_enabled, bool):
        raise ConfigError("fallback_enabled must be a boolean.")

    legacy = is_legacy_raw(raw)
    accounts: dict[str, AccountConfig] = {}
    if legacy:
        if "architect" not in raw or "executor" not in raw:
            raise ConfigError("Legacy config must contain both [architect] and [executor].")
        for name in ("architect", "executor"):
            value = raw[name]
            if not isinstance(value, dict):
                raise ConfigError(f"Legacy [{name}] section is invalid.")
            accounts[name] = _account(name, value, base)
        roles = {
            "orchestrator": "architect",
            "architect": "architect",
            "reviewer": "architect",
            "executor": "executor",
        }
    else:
        raw_accounts = raw.get("accounts", {})
        if not isinstance(raw_accounts, dict):
            raise ConfigError("[accounts] must contain account tables.")
        for name, value in raw_accounts.items():
            if not isinstance(value, dict):
                raise ConfigError(f"Account '{name}' is invalid.")
            account = _account(str(name), value, base)
            if account.name in accounts:
                raise ConfigError(f"Duplicate account '{account.name}'.")
            accounts[account.name] = account

        raw_roles = raw.get("roles", {})
        if not isinstance(raw_roles, dict):
            raise ConfigError("[roles] must contain role assignments.")
        roles = {
            str(role): str(account).strip()
            for role, account in raw_roles.items()
            if str(account).strip()
        }

    terminal_readiness_timeout = float(orch.get("terminal_readiness_timeout", 60.0))
    if terminal_readiness_timeout <= 0:
        raise ConfigError("terminal_readiness_timeout must be positive.")
    terminal_turn_start_timeout = float(orch.get("terminal_turn_start_timeout", 15.0))
    if terminal_turn_start_timeout <= 0:
        raise ConfigError("terminal_turn_start_timeout must be positive.")
    app_server_initialize_timeout = float(orch.get("app_server_initialize_timeout", 30.0))
    app_server_thread_timeout = float(orch.get("app_server_thread_timeout", 30.0))
    app_server_turn_start_timeout = float(orch.get("app_server_turn_start_timeout", 30.0))
    app_server_turn_timeout = float(orch.get("app_server_turn_timeout", 600.0))
    claude_turn_timeout = float(orch.get("claude_turn_timeout", app_server_turn_timeout))
    dashboard_telemetry_timeout = float(orch.get("dashboard_telemetry_timeout", 5.0))
    live_event_journal_max_records = int(orch.get("live_event_journal_max_records", 2000))
    live_event_journal_max_record_bytes = int(orch.get("live_event_journal_max_record_bytes", 65536))
    live_event_journal_max_detail_bytes = int(orch.get("live_event_journal_max_detail_bytes", 16384))
    if any(
        value <= 0
        for value in (
            live_event_journal_max_records,
            live_event_journal_max_record_bytes,
            live_event_journal_max_detail_bytes,
        )
    ):
        raise ConfigError("Live event journal limits must be positive.")
    if any(
        value <= 0
        for value in (
            app_server_initialize_timeout,
            app_server_thread_timeout,
            app_server_turn_start_timeout,
            app_server_turn_timeout,
            claude_turn_timeout,
            dashboard_telemetry_timeout,
        )
    ):
        raise ConfigError("App Server timeouts must be positive.")

    return OrchestratorConfig(
        repository=repository,
        runs_dir=runs_dir,
        max_correction_cycles=int(orch.get("max_correction_cycles", 1)),
        require_clean_git=bool(orch.get("require_clean_git", True)),
        codex_command=str(orch.get("codex_command", "codex")),
        accounts=accounts,
        roles=roles,
        project_root=Path(__file__).resolve().parents[2],
        config_path=path,
        legacy=legacy,
        antigravity_command=(
            validate_setting_value(orch.get("antigravity_command", "agy"), "antigravity_command")
            or "agy"
        ),
        claude_command=(
            validate_setting_value(orch.get("claude_command", "claude"), "claude_command")
            or "claude"
        ),
        node_command=str(orch.get("node_command", "node")).strip() or "node",
        terminal_readiness_timeout=terminal_readiness_timeout,
        terminal_turn_start_timeout=terminal_turn_start_timeout,
        app_server_initialize_timeout=app_server_initialize_timeout,
        app_server_thread_timeout=app_server_thread_timeout,
        app_server_turn_start_timeout=app_server_turn_start_timeout,
        app_server_turn_timeout=app_server_turn_timeout,
        claude_turn_timeout=claude_turn_timeout,
        dashboard_telemetry_timeout=dashboard_telemetry_timeout,
        live_event_journal_max_records=live_event_journal_max_records,
        live_event_journal_max_record_bytes=live_event_journal_max_record_bytes,
        live_event_journal_max_detail_bytes=live_event_journal_max_detail_bytes,
        fallback_enabled=fallback_enabled,
    )
