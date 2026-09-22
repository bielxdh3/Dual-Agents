from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
from typing import Callable, Iterable, Mapping

from .config import (
    AccountConfig,
    ConfigError,
    OrchestratorConfig,
    SUPPORTED_BACKENDS,
    SUPPORTED_ROLES,
    _account,
    is_legacy_raw,
    load_raw_config,
    validate_account_name,
    validate_auth_reference,
    validate_setting_value,
    validate_role_name,
)
from .antigravity import antigravity_status
from .claude_code import claude_status
from .process import codex_environment, run_command


InputFn = Callable[[str], str]
OutputFn = Callable[[str], None]
_SECTION = re.compile(r"^\s*\[([^]]+)\]\s*(?:#.*)?$")


def abbreviate_path(path: Path) -> str:
    value = str(path)
    home = str(Path.home())
    if any(value.casefold().startswith(home.casefold() + separator) for separator in ("\\", "/")):
        tail = value[len(home) :].lstrip("\\/").splitlines()[0]
        parts = re.split(r"[\\/]", tail)
        value = "~\\...\\" + "\\".join(parts[-2:]) if parts else "~"
    else:
        parts = re.split(r"[\\/]", value)
        if len(parts) > 3:
            value = "...\\" + "\\".join(parts[-2:])
    if len(value) > 72:
        return "..." + value[-69:]
    return value


def roles_for_account(roles: Mapping[str, str], account_name: str) -> list[str]:
    order = {role: index for index, role in enumerate(SUPPORTED_ROLES)}
    return sorted(
        [role for role, account in roles.items() if account == account_name],
        key=lambda role: (order.get(role, len(order)), role),
    )


def _toml_string(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _toml_key(value: str) -> str:
    return value if re.fullmatch(r"[A-Za-z0-9_-]+", value) else _toml_string(value)


def _managed_section(header: str) -> bool:
    section = header.strip()
    return (
        section in {"accounts", "roles", "architect", "executor"}
        or section.startswith("accounts.")
    )


def _without_managed_sections(text: str) -> str:
    lines = text.splitlines(keepends=True)
    kept: list[str] = []
    skipping = False
    for line in lines:
        match = _SECTION.match(line.rstrip("\r\n"))
        if match:
            skipping = _managed_section(match.group(1))
        if not skipping:
            kept.append(line)
    return "".join(kept).rstrip()


def _registry_block(accounts: Mapping[str, AccountConfig], roles: Mapping[str, str]) -> str:
    lines: list[str] = []
    for name in sorted(accounts):
        account = accounts[name]
        lines.extend(
            [
                f"[accounts.{_toml_string(name)}]",
                f"label = {_toml_string(account.label)}",
                f"codex_home = {_toml_string(str(account.codex_home))}",
                f"model = {_toml_string(account.model)}",
                f"reasoning_effort = {_toml_string(account.reasoning_effort)}",
                f"runtime_model = {_toml_string(account.runtime_model)}",
                f"fixed_mode = {_toml_string(account.fixed_mode)}",
                f"backend = {_toml_string(account.backend)}",
                f"service_tier = {_toml_string(account.service_tier)}",
                f"network_access = {'true' if account.network_access else 'false'}",
                f"provider_type = {_toml_string(account.provider_type)}",
                f"adapter_type = {_toml_string(account.adapter_type)}",
                f"auth_mode = {_toml_string(account.auth_mode)}",
                f"auth_reference = {_toml_string(account.auth_reference)}",
                f"state_root = {_toml_string(str(account.state_root))}" if account.state_root else "",
                f"base_url = {_toml_string(account.base_url)}",
                f"available_models = {json.dumps(list(account.available_models), ensure_ascii=False)}",
                f"supported_reasoning_efforts = {json.dumps(list(account.supported_reasoning_efforts), ensure_ascii=False)}",
                f"enabled = {'true' if account.enabled else 'false'}",
                f"fallback_roles = {json.dumps(list(account.fallback_roles), ensure_ascii=False)}",
                "",
            ]
        )
    lines.append("[roles]")
    for role in sorted(roles):
        account_name = roles[role]
        if account_name:
            lines.append(f"{_toml_key(role)} = {_toml_string(account_name)}")
    return "\n".join(lines).rstrip() + "\n"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        temporary.write_text(text, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_registry_config(
    path: Path,
    accounts: Mapping[str, AccountConfig],
    roles: Mapping[str, str],
) -> None:
    """Update registry sections while retaining unrelated configuration text."""
    original = path.read_bytes().decode("utf-8-sig")
    preserved = _without_managed_sections(original)
    prefix = f"{preserved}\n\n" if preserved else ""
    _atomic_write(path, prefix + _registry_block(accounts, roles))


def set_fallback_enabled(config: OrchestratorConfig, enabled: bool) -> bool:
    """Persist the global, explicit fallback switch without rewriting other settings."""
    if config.legacy:
        raise ConfigError("Run 'dual-codex migrate-config' before changing fallback settings.")
    if not isinstance(enabled, bool):
        raise ConfigError("fallback_enabled must be a boolean.")
    original = config.config_path.read_bytes().decode("utf-8-sig")
    lines = original.splitlines()
    section_start = next((i for i, line in enumerate(lines) if _SECTION.match(line) and _SECTION.match(line).group(1).strip() == "orchestrator"), None)
    if section_start is None:
        raise ConfigError("Missing [orchestrator] configuration.")
    section_end = len(lines)
    for i in range(section_start + 1, len(lines)):
        match = _SECTION.match(lines[i])
        if match:
            section_end = i
            break
    replacement = f"fallback_enabled = {'true' if enabled else 'false'}"
    for i in range(section_start + 1, section_end):
        if re.match(r"^\s*fallback_enabled\s*=", lines[i]):
            lines[i] = replacement
            break
    else:
        lines.insert(section_start + 1, replacement)
    _atomic_write(config.config_path, "\n".join(lines).rstrip() + "\n")
    return enabled


def _profile_config_text(path: Path) -> str:
    if path.exists():
        text = path.read_bytes().decode("utf-8-sig")
    else:
        text = ""
    lines = text.splitlines()
    setting = 'cli_auth_credentials_store = "file"'
    for index, line in enumerate(lines):
        if re.match(r"^\s*cli_auth_credentials_store\s*=", line):
            lines[index] = setting
            break
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(setting)
    return "\n".join(lines).rstrip() + "\n"


def ensure_codex_profile(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    _atomic_write(home / "config.toml", _profile_config_text(home / "config.toml"))


def _agent_for_status(account: AccountConfig):
    from .config import AgentConfig

    return AgentConfig(
        codex_home=account.codex_home,
        model=account.model,
        reasoning_effort=account.reasoning_effort,
        runtime_model=account.runtime_model,
        fixed_mode=account.fixed_mode,
        sandbox="read-only",
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


def login_status(config: OrchestratorConfig, account: AccountConfig) -> str:
    """Check login status without reading or displaying authentication data."""
    if not account.enabled:
        return "DISABLED"
    if account.backend == "antigravity":
        return antigravity_status(config.antigravity_command, cwd=config.project_root)
    if account.backend == "claude_code":
        return claude_status(getattr(config, "claude_command", "claude"), cwd=config.project_root, account=account)
    if account.backend == "api":
        reference = account.auth_reference.strip()
        if reference.startswith("env:"):
            name = reference[4:]
            return "OK" if name and os.environ.get(name) else "NOT CONFIGURED"
        return "NOT CONFIGURED"
    if shutil.which(config.codex_command) is None and not Path(config.codex_command).exists():
        return "UNKNOWN"
    try:
        result = run_command(
            [config.codex_command, "login", "status"],
            cwd=config.project_root,
            env=codex_environment(_agent_for_status(account)),
            check=False,
        )
    except OSError:
        return "UNKNOWN"
    return "OK" if result.returncode == 0 else "NOT LOGGED IN"


def _verify_login(config: OrchestratorConfig, account: AccountConfig) -> None:
    status = login_status(config, account)
    if status != "OK":
        raise RuntimeError(f"Codex login status failed for account '{account.name}'.")


def _run_login(config: OrchestratorConfig, account: AccountConfig) -> None:
    if account.backend == "antigravity":
        raise RuntimeError(
            "Antigravity authentication is managed by the installed agy CLI; "
            "use its interactive login flow instead of Codex login."
        )
    try:
        run_command(
            [config.codex_command, "login"],
            cwd=config.project_root,
            env=codex_environment(_agent_for_status(account)),
        )
    except Exception as exc:
        raise RuntimeError(f"Codex login failed for account '{account.name}'.") from exc
    _verify_login(config, account)


def _resolve_home(config: OrchestratorConfig, value: str | None, name: str) -> Path:
    if value is None:
        home = Path.home() / "CodexProfiles" / name
    else:
        raw = str(value).strip()
        if not raw or any(ord(char) < 32 for char in raw):
            raise ConfigError("CODEX_HOME must be a non-empty safe path.")
        home = Path(raw).expanduser()
        if not home.is_absolute():
            home = config.config_path.parent / home
    home = home.resolve()
    protected = {
        config.config_path.parent.resolve(),
        config.repository.resolve(),
        Path.home().resolve(),
    }
    if home in protected:
        raise ConfigError("CODEX_HOME must not point at the repository, config directory, or user home.")
    for existing_name, existing in config.accounts.items():
        if existing.codex_home.resolve() == home:
            raise ConfigError(
                f"CODEX_HOME is already assigned to account '{existing_name}'."
            )
    return home


def add_account(
    config: OrchestratorConfig,
    name: str,
    *,
    label: str = "",
    codex_home: str | None = None,
    model: str = "",
    reasoning_effort: str = "",
    runtime_model: str = "",
    fixed_mode: str = "",
    backend: str = "windows",
    provider_type: str | None = None,
    adapter_type: str | None = None,
    auth_mode: str | None = None,
    auth_reference: str = "",
    base_url: str = "",
    available_models: Iterable[str] = (),
    supported_reasoning_efforts: Iterable[str] = (),
    roles: list[str] | None = None,
    fallback_roles: list[str] | None = None,
    enabled: bool = True,
    authenticate: bool = True,
    output: OutputFn = print,
) -> AccountConfig:
    if config.legacy:
        raise ConfigError("Run 'dual-codex migrate-config' before adding accounts.")
    name = validate_account_name(name)
    if name in config.accounts:
        raise ConfigError(f"Account '{name}' is already registered.")
    requested_roles = [validate_role_name(role) for role in (roles or [])]
    if backend not in SUPPORTED_BACKENDS:
        raise ConfigError("backend must be one of: " + ", ".join(SUPPORTED_BACKENDS) + ".")
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
    provider_type = (provider_type or default_provider).strip()
    adapter_type = (adapter_type or default_adapter).strip()
    auth_mode = (auth_mode or ("environment" if backend == "api" else "provider_native")).strip()
    if provider_type not in {"codex", "gemini", "api", "anthropic"} or adapter_type not in {"codex_cli", "antigravity_cli", "openai_compatible", "claude_code"}:
        raise ConfigError("Unsupported provider or adapter type.")
    if backend == "api" or (backend == "claude_code" and auth_mode == "environment"):
        auth_reference = validate_auth_reference(auth_reference)
    if not isinstance(enabled, bool):
        raise ConfigError("enabled must be a boolean.")
    home = _resolve_home(config, codex_home, name)
    if (home / "auth.json").exists():
        raise ConfigError(
            f"An unregistered auth file already exists for '{name}'. "
            "Use account login only after registering it safely."
        )

    account = AccountConfig(
        name=name,
        label=validate_setting_value(label, "label"),
        codex_home=home,
        model=validate_setting_value(model, "model"),
        reasoning_effort=(
            validate_setting_value(reasoning_effort, "reasoning_effort")
            or ("" if backend == "api" else "high")
        ),
        runtime_model=validate_setting_value(runtime_model, "runtime_model"),
        fixed_mode=validate_setting_value(fixed_mode, "fixed_mode"),
        backend=backend,
        service_tier="",
        network_access=False,
        provider_type=provider_type,
        adapter_type=adapter_type,
        auth_mode=auth_mode,
        auth_reference=auth_reference if backend == "api" or (backend == "claude_code" and auth_mode == "environment") else validate_setting_value(auth_reference, "auth_reference"),
        base_url=validate_setting_value(base_url, "base_url"),
        available_models=tuple(validate_setting_value(value, "available_models") for value in available_models),
        supported_reasoning_efforts=tuple(validate_setting_value(value, "supported_reasoning_efforts") for value in supported_reasoning_efforts),
        enabled=enabled,
        fallback_roles=tuple(dict.fromkeys(validate_role_name(role) for role in (fallback_roles or []))),
    )
    output(f"Account: {account.name}")
    output(f"Label: {account.label or '(none)'}")
    if backend in {"windows", "app_server"}:
        output(
            f"Preparing CODEX_HOME: {abbreviate_path(account.codex_home)}"
            if not authenticate
            else f"Authenticating with CODEX_HOME: {abbreviate_path(account.codex_home)}"
        )
        ensure_codex_profile(account.codex_home)
    else:
        output(f"Registering {provider_type} profile without copying provider credentials.")
    if backend in {"windows", "app_server"} and authenticate:
        _run_login(config, account)

    accounts = dict(config.accounts)
    accounts[name] = account
    assignments = dict(config.roles)
    for role in requested_roles:
        assignments[role] = name
    write_registry_config(config.config_path, accounts, assignments)
    return account


def login_account(
    config: OrchestratorConfig,
    name: str,
    *,
    assume_yes: bool = False,
    input_fn: InputFn = input,
    output: OutputFn = print,
) -> None:
    if config.legacy:
        raise ConfigError("Run 'dual-codex migrate-config' before using account login.")
    account = config.accounts.get(validate_account_name(name))
    if account is None:
        raise ConfigError(f"Unknown account '{name}'.")
    if (account.codex_home / "auth.json").exists() and not assume_yes:
        answer = input_fn(
            f"Replace the existing login for account '{account.name}' "
            f"({account.label or 'no label'})? [y/N] "
        ).strip().lower()
        if answer not in {"y", "yes"}:
            raise RuntimeError("Login cancelled.")
    output(f"Account: {account.name}")
    output(f"Label: {account.label or '(none)'}")
    output(f"CODEX_HOME: {abbreviate_path(account.codex_home)}")
    _run_login(config, account)


def logout_account(config: OrchestratorConfig, name: str) -> str:
    """Remove provider-native Codex credentials from exactly one profile."""
    if config.legacy:
        raise ConfigError("Run 'dual-codex migrate-config' before using account logout.")
    account = config.accounts.get(validate_account_name(name))
    if account is None:
        raise ConfigError(f"Unknown account '{name}'.")
    if account.backend not in {"windows", "app_server"}:
        raise ConfigError("Logout is only supported for Codex CLI profiles.")
    result = run_command(
        [config.codex_command, "logout"],
        cwd=config.project_root,
        env=codex_environment(_agent_for_status(account)),
        check=False,
    )
    return "OK" if result.returncode == 0 else "FAILED"


def rename_account(config: OrchestratorConfig, old_name: str, new_name: str) -> None:
    if config.legacy:
        raise ConfigError("Run 'dual-codex migrate-config' before renaming accounts.")
    old_name = validate_account_name(old_name)
    new_name = validate_account_name(new_name)
    if old_name not in config.accounts:
        raise ConfigError(f"Unknown account '{old_name}'.")
    if new_name in config.accounts:
        raise ConfigError(f"Account '{new_name}' is already registered.")
    accounts = dict(config.accounts)
    old = accounts.pop(old_name)
    accounts[new_name] = replace(old, name=new_name)
    roles = {
        role: new_name if account == old_name else account
        for role, account in config.roles.items()
    }
    write_registry_config(config.config_path, accounts, roles)


def label_account(config: OrchestratorConfig, name: str, label: str) -> None:
    if config.legacy:
        raise ConfigError("Run 'dual-codex migrate-config' before changing labels.")
    name = validate_account_name(name)
    account = config.accounts.get(name)
    if account is None:
        raise ConfigError(f"Unknown account '{name}'.")
    accounts = dict(config.accounts)
    accounts[name] = replace(account, label=validate_setting_value(label, "label"))
    write_registry_config(config.config_path, accounts, config.roles)


def remove_account(
    config: OrchestratorConfig,
    name: str,
    *,
    delete_profile: bool = False,
    confirm_delete: bool = False,
    input_fn: InputFn = input,
) -> None:
    if config.legacy:
        raise ConfigError("Run 'dual-codex migrate-config' before removing accounts.")
    name = validate_account_name(name)
    account = config.accounts.get(name)
    if account is None:
        raise ConfigError(f"Unknown account '{name}'.")
    assigned = roles_for_account(config.roles, name)
    if assigned:
        raise ConfigError(
            f"Cannot remove account '{name}'; roles still assigned: {', '.join(assigned)}."
        )
    if delete_profile:
        if account.codex_home.resolve() in {Path.home().resolve(), config.config_path.parent.resolve()}:
            raise ConfigError("Refusing to delete a broad profile or configuration directory.")
        if not confirm_delete:
            answer = input_fn(
                f"Type DELETE to remove the profile directory for '{name}': "
            ).strip()
            if answer != "DELETE":
                raise RuntimeError("Profile deletion cancelled.")
    accounts = dict(config.accounts)
    accounts.pop(name)
    write_registry_config(config.config_path, accounts, config.roles)
    if delete_profile and account.codex_home.exists():
        shutil.rmtree(account.codex_home)


def update_account_settings(
    config: OrchestratorConfig,
    name: str,
    *,
    model: str | None = None,
    reasoning_effort: str | None = None,
    runtime_model: str | None = None,
    fixed_mode: str | None = None,
    service_tier: str | None = None,
    backend: str | None = None,
    provider_type: str | None = None,
    adapter_type: str | None = None,
    auth_mode: str | None = None,
    auth_reference: str | None = None,
    state_root: str | None = None,
    base_url: str | None = None,
    available_models: tuple[str, ...] | list[str] | None = None,
    supported_reasoning_efforts: tuple[str, ...] | list[str] | None = None,
    enabled: bool | None = None,
    fallback_roles: tuple[str, ...] | list[str] | None = None,
) -> AccountConfig:
    """Persist validated future-turn settings without touching profile credentials."""
    if config.legacy:
        raise ConfigError("Run 'dual-codex migrate-config' before changing account settings.")
    name = validate_account_name(name)
    account = config.accounts.get(name)
    if account is None:
        raise ConfigError(f"Unknown account '{name}'.")
    new_backend = account.backend if backend is None else str(backend).strip()
    if new_backend not in SUPPORTED_BACKENDS:
        raise ConfigError(
            "backend must be one of: " + ", ".join(SUPPORTED_BACKENDS) + "."
        )
    new_auth_reference = account.auth_reference if auth_reference is None else validate_setting_value(auth_reference, "auth_reference")
    new_auth_mode = account.auth_mode if auth_mode is None else str(auth_mode).strip()
    if new_auth_mode not in {"provider_native", "environment", "none"}:
        raise ConfigError("auth_mode must be one of: provider_native, environment, none.")
    if new_backend == "api" or (new_backend == "claude_code" and new_auth_mode == "environment"):
        new_auth_reference = validate_auth_reference(new_auth_reference)
    new_model = account.model if model is None else validate_setting_value(model, "model")
    new_fixed_mode = account.fixed_mode if fixed_mode is None else validate_setting_value(fixed_mode, "fixed_mode")
    if reasoning_effort is None:
        new_reasoning_effort = account.reasoning_effort
    else:
        requested_effort = validate_setting_value(reasoning_effort, "reasoning_effort")
        if requested_effort:
            new_reasoning_effort = requested_effort
        elif new_backend == "api" or (new_backend == "antigravity" and (new_fixed_mode or not new_model)):
            new_reasoning_effort = ""
        else:
            new_reasoning_effort = "high"
    if fallback_roles is None:
        new_fallback_roles = account.fallback_roles
    else:
        new_fallback_roles = tuple(dict.fromkeys(validate_role_name(role) for role in fallback_roles))
    updated = replace(
        account,
        model=new_model,
        reasoning_effort=new_reasoning_effort,
        runtime_model=(account.runtime_model if runtime_model is None else validate_setting_value(runtime_model, "runtime_model")),
        fixed_mode=new_fixed_mode,
        backend=new_backend,
        service_tier=(account.service_tier if service_tier is None else validate_setting_value(service_tier, "service_tier")),
        provider_type=account.provider_type if provider_type is None else str(provider_type).strip(),
        adapter_type=account.adapter_type if adapter_type is None else str(adapter_type).strip(),
        auth_mode=new_auth_mode,
        auth_reference=new_auth_reference,
        state_root=account.state_root if state_root is None else Path(state_root).expanduser().resolve(),
        base_url=account.base_url if base_url is None else validate_setting_value(base_url, "base_url"),
        available_models=account.available_models if available_models is None else tuple(validate_setting_value(value, "available_models") for value in available_models),
        supported_reasoning_efforts=account.supported_reasoning_efforts if supported_reasoning_efforts is None else tuple(validate_setting_value(value, "supported_reasoning_efforts") for value in supported_reasoning_efforts),
        enabled=account.enabled if enabled is None else bool(enabled),
        fallback_roles=new_fallback_roles,
    )
    accounts = dict(config.accounts)
    accounts[name] = updated
    write_registry_config(config.config_path, accounts, config.roles)
    return updated


def set_account_enabled(config: OrchestratorConfig, name: str, enabled: bool) -> AccountConfig:
    """Enable or disable a profile without touching provider-native state."""
    if config.legacy:
        raise ConfigError("Run 'dual-codex migrate-config' before changing accounts.")
    name = validate_account_name(name)
    account = config.accounts.get(name)
    if account is None:
        raise ConfigError(f"Unknown account '{name}'.")
    updated = update_account_settings(config, name, enabled=bool(enabled))
    return updated


def assign_role(config: OrchestratorConfig, role: str, account_name: str) -> tuple[str | None, str]:
    if config.legacy:
        raise ConfigError("Run 'dual-codex migrate-config' before changing roles.")
    role = validate_role_name(role)
    account_name = validate_account_name(account_name)
    if account_name not in config.accounts:
        raise ConfigError(f"Unknown account '{account_name}'.")
    roles = dict(config.roles)
    previous = roles.get(role)
    roles[role] = account_name
    write_registry_config(config.config_path, config.accounts, roles)
    return previous, account_name


def _validated_role_map(
    accounts: Mapping[str, AccountConfig],
    roles: Mapping[str, str],
) -> dict[str, str]:
    validated: dict[str, str] = {}
    for raw_role, raw_account in roles.items():
        role = validate_role_name(raw_role)
        if role in validated:
            raise ConfigError(f"Duplicate role '{role}'.")
        account = validate_account_name(raw_account)
        if account not in accounts:
            raise ConfigError(f"Role '{role}' refers to unknown account '{account}'.")
        validated[role] = account
    return validated


def set_roles_for_account(
    config: OrchestratorConfig,
    account_name: str,
    roles: Iterable[str],
) -> dict[str, str]:
    """Atomically replace one account's complete role set."""
    if config.legacy:
        raise ConfigError("Run 'dual-codex migrate-config' before changing roles.")
    account_name = validate_account_name(account_name)
    if account_name not in config.accounts:
        raise ConfigError(f"Unknown account '{account_name}'.")
    if isinstance(roles, (str, bytes)):
        raise ConfigError("roles must be a list of role names.")

    requested: list[str] = []
    for raw_role in roles:
        if not isinstance(raw_role, str):
            raise ConfigError("roles must contain only strings.")
        role = validate_role_name(raw_role)
        if role in requested:
            raise ConfigError(f"Duplicate role '{role}'.")
        requested.append(role)

    current = _validated_role_map(config.accounts, config.roles)
    resulting = {role: owner for role, owner in current.items() if owner != account_name}
    resulting.update({role: account_name for role in requested})
    resulting = _validated_role_map(config.accounts, resulting)
    write_registry_config(config.config_path, config.accounts, resulting)
    return resulting


def unassign_role(config: OrchestratorConfig, role: str) -> str | None:
    if config.legacy:
        raise ConfigError("Run 'dual-codex migrate-config' before changing roles.")
    role = validate_role_name(role)
    roles = dict(config.roles)
    previous = roles.pop(role, None)
    write_registry_config(config.config_path, config.accounts, roles)
    return previous


def swap_roles(config: OrchestratorConfig, first: str, second: str) -> tuple[str | None, str | None]:
    if config.legacy:
        raise ConfigError("Run 'dual-codex migrate-config' before changing roles.")
    first = validate_role_name(first)
    second = validate_role_name(second)
    roles = dict(config.roles)
    previous = (roles.get(first), roles.get(second))
    roles[first], roles[second] = roles.get(second, ""), roles.get(first, "")
    roles = {role: account for role, account in roles.items() if account}
    write_registry_config(config.config_path, config.accounts, roles)
    return previous


@dataclass(frozen=True)
class MigrationResult:
    changed: bool
    backup_path: Path | None


def migrate_legacy_config(
    path: Path,
    *,
    architect_name: str | None = None,
    executor_name: str | None = None,
    architect_label: str | None = None,
    executor_label: str | None = None,
    dry_run: bool = False,
    input_fn: InputFn = input,
    output: OutputFn = print,
    now: datetime | None = None,
) -> MigrationResult:
    path = path.expanduser().resolve()
    raw = load_raw_config(path)
    if not is_legacy_raw(raw):
        if "accounts" in raw and not ("architect" in raw or "executor" in raw):
            output("Configuration already uses the account registry; nothing to migrate.")
            return MigrationResult(changed=False, backup_path=None)
        raise ConfigError(
            "Configuration is neither a complete legacy format nor a clean registry format. "
            "Remove the partial sections manually and rerun migration."
        )
    if "architect" not in raw or "executor" not in raw:
        raise ConfigError("Legacy config must contain both [architect] and [executor].")

    base = path.parent
    legacy_architect = _account("architect", raw["architect"], base)
    legacy_executor = _account("executor", raw["executor"], base)

    architect_name = validate_account_name(
        architect_name if architect_name is not None else input_fn("Stable name for legacy Architect account: ")
    )
    executor_name = validate_account_name(
        executor_name if executor_name is not None else input_fn("Stable name for legacy Executor account: ")
    )
    if architect_name == executor_name:
        raise ConfigError("Legacy Architect and Executor accounts must have different names.")
    architect_label = (
        architect_label
        if architect_label is not None
        else input_fn("Friendly label for legacy Architect account (optional): ")
    ).strip()
    executor_label = (
        executor_label
        if executor_label is not None
        else input_fn("Friendly label for legacy Executor account (optional): ")
    ).strip()

    accounts = {
        architect_name: AccountConfig(
            name=architect_name,
            label=architect_label,
            codex_home=legacy_architect.codex_home,
            model=legacy_architect.model,
            reasoning_effort=legacy_architect.reasoning_effort,
            runtime_model=legacy_architect.runtime_model,
            fixed_mode=legacy_architect.fixed_mode,
            backend=legacy_architect.backend,
            service_tier=legacy_architect.service_tier,
            network_access=legacy_architect.network_access,
            provider_type=legacy_architect.provider_type,
            adapter_type=legacy_architect.adapter_type,
            auth_mode=legacy_architect.auth_mode,
            auth_reference=legacy_architect.auth_reference,
            state_root=legacy_architect.state_root,
            base_url=legacy_architect.base_url,
            available_models=legacy_architect.available_models,
            supported_reasoning_efforts=legacy_architect.supported_reasoning_efforts,
            enabled=legacy_architect.enabled,
        ),
        executor_name: AccountConfig(
            name=executor_name,
            label=executor_label,
            codex_home=legacy_executor.codex_home,
            model=legacy_executor.model,
            reasoning_effort=legacy_executor.reasoning_effort,
            runtime_model=legacy_executor.runtime_model,
            fixed_mode=legacy_executor.fixed_mode,
            backend=legacy_executor.backend,
            service_tier=legacy_executor.service_tier,
            network_access=legacy_executor.network_access,
            provider_type=legacy_executor.provider_type,
            adapter_type=legacy_executor.adapter_type,
            auth_mode=legacy_executor.auth_mode,
            auth_reference=legacy_executor.auth_reference,
            state_root=legacy_executor.state_root,
            base_url=legacy_executor.base_url,
            available_models=legacy_executor.available_models,
            supported_reasoning_efforts=legacy_executor.supported_reasoning_efforts,
            enabled=legacy_executor.enabled,
        ),
    }
    roles = {
        "orchestrator": architect_name,
        "architect": architect_name,
        "reviewer": architect_name,
        "executor": executor_name,
    }
    output("Migration preview:")
    output(f"  architect account: {architect_name} ({architect_label or 'no label'})")
    output(f"  executor account: {executor_name} ({executor_label or 'no label'})")
    output(f"  architect CODEX_HOME: {abbreviate_path(legacy_architect.codex_home)}")
    output(f"  executor CODEX_HOME: {abbreviate_path(legacy_executor.codex_home)}")
    output("  roles: orchestrator/architect/reviewer -> architect account; executor -> executor account")
    if dry_run:
        output("Dry run: configuration was not changed and no backup was created.")
        return MigrationResult(changed=False, backup_path=None)

    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.bak-{stamp}")
    suffix = 1
    while backup.exists():
        backup = path.with_name(f"{path.name}.bak-{stamp}-{suffix}")
        suffix += 1
    shutil.copy2(path, backup)
    write_registry_config(path, accounts, roles)
    output(f"Migration complete. Backup: {backup.name}")
    return MigrationResult(changed=True, backup_path=backup)
