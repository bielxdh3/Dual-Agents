from __future__ import annotations

"""Provider capability and request adapters.

The orchestration layer only needs a small, stable boundary here: a profile
describes provider capabilities and an adapter owns provider-specific request
details.  Credentials never live in a profile; API adapters resolve only an
environment-variable reference at request time.
"""

from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Protocol
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlunsplit

from .antigravity import antigravity_status
from .config import AccountConfig, AgentConfig, OrchestratorConfig
from .process import CommandResult, _prepare_command


class ProviderError(RuntimeError):
    """A safe, user-actionable provider error."""


@dataclass(frozen=True)
class ProviderCapabilities:
    provider: str
    provider_label: str
    adapter: str
    models: tuple[dict[str, Any], ...] = ()
    effort_levels: tuple[str, ...] = ()
    service_tiers: tuple[dict[str, str], ...] = ()
    model_list: bool = False
    reasoning: bool = False
    streaming: bool = False
    structured_output: bool = False
    conversation_resume: bool = False
    workspace_binding: bool = False
    tool_use: bool = False
    profile_isolation: bool = False
    isolation_note: str = ""
    credential_status: str = "unknown"
    runtime_status: str = "Unknown"
    error: str | None = None
    supported_roles: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "provider_label": self.provider_label,
            "adapter": self.adapter,
            "model_list": self.model_list,
            "reasoning": self.reasoning,
            "streaming": self.streaming,
            "structured_output": self.structured_output,
            "conversation_resume": self.conversation_resume,
            "workspace_binding": self.workspace_binding,
            "tool_use": self.tool_use,
            "profile_isolation": self.profile_isolation,
            "isolation_note": self.isolation_note,
            "credential_status": self.credential_status,
            "runtime_status": self.runtime_status,
            "error": self.error,
            "supported_roles": list(self.supported_roles),
            # Keep the dashboard's existing model-row contract.
            "effort_levels": list(self.effort_levels),
            "service_tiers": list(self.service_tiers),
        }

    def supports_role(self, role: str) -> bool:
        if self.runtime_status in {"Unavailable", "Unknown"}:
            return False
        return not self.supported_roles or role in self.supported_roles


class ProviderAdapter(Protocol):
    provider: str
    adapter: str

    def capabilities(self, config: OrchestratorConfig, account: AccountConfig) -> ProviderCapabilities:
        ...


_ANTIGRAVITY_MODES = ("low", "medium", "high", "thinking")
_ANTIGRAVITY_MODE_ORDER = {name: index for index, name in enumerate(_ANTIGRAVITY_MODES)}
_ANTIGRAVITY_DISPLAY_MODE = re.compile(r"^(?P<base>.+?)\s+\((?P<mode>Low|Medium|High|Thinking)\)$", re.IGNORECASE)


def _antigravity_mode_label(mode: str) -> str:
    return mode[:1].upper() + mode[1:].lower()


def _antigravity_mode_for_row(model_id: str, display: str) -> tuple[str, str, str]:
    """Return logical id, display name, and verified variant mode."""
    match = _ANTIGRAVITY_DISPLAY_MODE.fullmatch(display.strip())
    display_base = match.group("base").strip() if match else display.strip()
    display_mode = match.group("mode").lower() if match else ""
    slug_match = re.fullmatch(r"(?P<base>.+)-(?P<mode>low|medium|high|thinking)", model_id, re.IGNORECASE)
    slug_mode = slug_match.group("mode").lower() if slug_match else ""
    mode = display_mode or slug_mode
    base_id = slug_match.group("base") if slug_match and mode == slug_mode else model_id
    return base_id, display_base, mode


def provider_for_backend(backend: str) -> str:
    return {
        "app_server": "codex",
        "windows": "codex",
        "antigravity": "gemini",
        "api": "api",
        "claude_code": "anthropic",
    }.get(backend, backend or "unknown")


def provider_label(provider: str, backend: str = "") -> str:
    if provider == "codex":
        return "Codex"
    if provider == "gemini" or backend == "antigravity":
        return "Antigravity / Gemini"
    if provider == "api" or backend == "api":
        return "API / OpenAI-compatible"
    if provider == "anthropic" or backend == "claude_code":
        return "Anthropic Claude"
    return provider.replace("_", " ").title() or "Provider"


def provider_default_label(provider: str, backend: str = "") -> str:
    if provider == "codex":
        return "Inherit Codex default"
    if provider == "gemini" or backend == "antigravity":
        return "Inherit Antigravity default"
    if provider == "anthropic" or backend == "claude_code":
        return "Inherit Claude default"
    return "Provider default"


def _safe_error(value: Any, limit: int = 400) -> str:
    text = " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())
    text = re.sub(r"(?i)(authorization\s*:\s*bearer\s+)[^\s,}]+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*[^\s,}]+", r"\1=[REDACTED]", text)
    return text[:limit]


def _api_environment() -> dict[str, str]:
    env = os.environ.copy()
    for name in ("CODEX_HOME", "OPENAI_API_KEY", "CODEX_API_KEY", "AZURE_OPENAI_API_KEY"):
        # API profiles resolve their explicitly configured env reference below;
        # unrelated provider credentials must not leak into the adapter.
        env.pop(name, None)
    return env


def _api_url(base_url: str) -> str:
    raw = str(base_url or "").strip()
    parsed = urlsplit(raw)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise ProviderError("API base_url must use https:// (or http:// for loopback test servers).")
    if parsed.username or parsed.password:
        raise ProviderError("API base_url must not contain embedded credentials.")
    if parsed.scheme == "http" and parsed.hostname.casefold() not in {"127.0.0.1", "localhost", "::1"}:
        raise ProviderError("Plain HTTP API endpoints are limited to loopback hosts; use HTTPS remotely.")
    path = parsed.path.rstrip("/")
    if path.endswith("/chat/completions"):
        return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
    return urlunsplit((parsed.scheme, parsed.netloc, path + "/chat/completions", "", ""))


def _secret_from_reference(reference: str) -> tuple[str, str]:
    value = str(reference or "").strip()
    if not value.startswith("env:"):
        raise ProviderError("API credentials must use an env:VARIABLE secret reference.")
    variable = value[4:]
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", variable):
        raise ProviderError("API secret reference contains an invalid environment variable name.")
    secret = os.environ.get(variable, "")
    if not secret:
        raise ProviderError(f"API secret environment variable '{variable}' is not set.")
    return variable, secret


def _configured_model_rows(account: AccountConfig) -> list[dict[str, Any]]:
    ids = list(account.available_models)
    if account.model and account.model not in ids:
        ids.insert(0, account.model)
    return [
        {
            "id": model_id,
            "model": model_id,
            "display_name": model_id,
            "description": "Configured provider model",
            "is_default": index == 0,
            "hidden": False,
            "default_reasoning": account.reasoning_effort or None,
            "reasoning_efforts": list(account.supported_reasoning_efforts),
            "fixed_mode": account.fixed_mode,
            "runtime_model": account.runtime_model or model_id,
            "runtime_variants": {},
            "default_service_tier": None,
            "service_tiers": [],
        }
        for index, model_id in enumerate(ids)
    ]


class CodexAdapter:
    provider = "codex"
    adapter = "codex_cli"

    def capabilities(self, config: OrchestratorConfig, account: AccountConfig) -> ProviderCapabilities:
        del config
        models = _configured_model_rows(account)
        # The installed App Server catalog is authoritative when available.
        # Native TUI accounts have no safe model-list endpoint, so this is the
        # conservative effort set accepted by the current Codex integration.
        efforts = account.supported_reasoning_efforts or ("low", "medium", "high")
        return ProviderCapabilities(
            provider=self.provider,
            provider_label=provider_label(self.provider, account.backend),
            adapter=self.adapter,
            models=tuple(models),
            effort_levels=tuple(efforts),
            model_list=bool(account.available_models),
            reasoning=True,
            streaming=True,
            structured_output=True,
            conversation_resume=True,
            workspace_binding=True,
            tool_use=True,
            profile_isolation=True,
            isolation_note="Codex profile state is isolated by the account CODEX_HOME.",
            credential_status="provider-managed",
            runtime_status="Connected",
            supported_roles=("orchestrator", "architect", "reviewer", "executor"),
        )


def _parse_antigravity_models(output: str) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for line in output.splitlines():
        parts = line.strip().split("\t", 1)
        if len(parts) != 2 or not parts[0] or parts[0].lower().startswith("fetching "):
            continue
        model_id, display = parts
        base_id, display_base, mode = _antigravity_mode_for_row(model_id, display)
        key = (base_id, display_base.casefold())
        group = grouped.setdefault(
            key,
            {
                "id": base_id,
                "model": base_id,
                "display_name": display_base,
                "description": "Installed Antigravity model catalog",
                "is_default": not grouped,
                "hidden": False,
                "default_reasoning": None,
                "reasoning_efforts": [],
                "fixed_mode": "",
                "runtime_model": model_id,
                "runtime_variants": {},
                "variant_labels": {},
                "default_service_tier": None,
                "service_tiers": [],
            },
        )
        variant_key = mode or "default"
        group["runtime_variants"][variant_key] = model_id
        group["variant_labels"][variant_key] = _antigravity_mode_label(mode) if mode else display_base

    rows: list[dict[str, Any]] = []
    for group in grouped.values():
        variants = group["runtime_variants"]
        modes = [mode for mode in variants if mode != "default"]
        modes.sort(key=lambda value: _ANTIGRAVITY_MODE_ORDER.get(value, len(_ANTIGRAVITY_MODES)))
        if len(variants) > 1:
            group["reasoning_efforts"] = modes
            group["default_reasoning"] = "high" if "high" in modes else (modes[0] if modes else None)
        else:
            only_mode = next(iter(variants), "default")
            group["fixed_mode"] = group["variant_labels"].get(only_mode, "") if only_mode != "default" else ""
            group["runtime_model"] = variants.get(only_mode, group["runtime_model"])
        rows.append(group)
    return rows


def _antigravity_effort_levels(models: list[dict[str, Any]]) -> tuple[str, ...]:
    values = {effort for row in models for effort in row.get("reasoning_efforts", ())}
    return tuple(sorted(values, key=lambda value: _ANTIGRAVITY_MODE_ORDER.get(value, len(_ANTIGRAVITY_MODES))))


def _antigravity_row_for_selection(models: tuple[dict[str, Any], ...] | list[dict[str, Any]], selection: str) -> dict[str, Any] | None:
    if not selection:
        return None
    for row in models:
        if selection == row.get("id") or selection == row.get("runtime_model"):
            return row
        if selection in (row.get("runtime_variants") or {}).values():
            return row
    return None


def resolve_antigravity_agent(config: OrchestratorConfig, agent: AgentConfig) -> AgentConfig:
    """Resolve a logical model/effort pair to the exact agy runtime variant."""
    if agent.backend != "antigravity" or not agent.model:
        return agent
    account = config.accounts.get(agent.account_name)
    if account is None:
        return agent
    capabilities = provider_capabilities(config, account)
    row = _antigravity_row_for_selection(capabilities.models, agent.model)
    if row is None:
        raise ProviderError(f"Antigravity model '{agent.model}' is stale or unavailable in the installed catalog.")
    variants = row.get("runtime_variants") or {}
    fixed_mode = str(row.get("fixed_mode") or "")
    if fixed_mode:
        runtime_model = row.get("runtime_model") or next(iter(variants.values()), "")
        if agent.runtime_model and agent.runtime_model not in {agent.model, runtime_model}:
            raise ProviderError(f"Antigravity model '{agent.model}' is stale or unavailable in the installed catalog.")
        fixed_key = fixed_mode.casefold()
        requested = agent.reasoning_effort.casefold()
        if requested and requested != fixed_key and not (fixed_key == "thinking" and requested == "high"):
            raise ProviderError(f"Antigravity effort '{agent.reasoning_effort}' is not supported by model '{row['display_name']}'.")
        return replace(agent, model=row["id"], runtime_model=runtime_model, reasoning_effort="", fixed_mode=fixed_mode)
    effort = agent.reasoning_effort or row.get("default_reasoning") or ""
    runtime_model = variants.get(effort)
    if not runtime_model:
        raise ProviderError(f"Antigravity effort '{effort or 'provider default'}' is not supported by model '{row['display_name']}'.")
    if agent.runtime_model and agent.runtime_model not in {agent.model, runtime_model}:
        raise ProviderError(f"Antigravity model '{agent.model}' is stale or unavailable in the installed catalog.")
    return replace(agent, model=row["id"], runtime_model=runtime_model, reasoning_effort=effort, fixed_mode="")


class AntigravityAdapter:
    provider = "gemini"
    adapter = "antigravity_cli"

    def capabilities(self, config: OrchestratorConfig, account: AccountConfig) -> ProviderCapabilities:
        command = config.antigravity_command
        try:
            result = subprocess.run(
                _prepare_command([command, "models"]),
                cwd=config.project_root,
                env=_api_environment(),
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=15,
                shell=False,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return ProviderCapabilities(
                provider=self.provider,
                provider_label=provider_label(self.provider, account.backend),
                adapter=self.adapter,
                effort_levels=("low", "medium", "high"),
                reasoning=True,
                profile_isolation=False,
                isolation_note="The installed agy 1.2.7 exposes no profile/state-root isolation flag.",
                credential_status="provider-managed",
                runtime_status="Unavailable",
                supported_roles=("executor",),
                error=_safe_error(exc),
            )
        models = _parse_antigravity_models(result.stdout)
        status = antigravity_status(command, cwd=config.project_root)
        error = _safe_error(result.stderr) if result.returncode else None
        effort_levels = _antigravity_effort_levels(models)
        return ProviderCapabilities(
            provider=self.provider,
            provider_label=provider_label(self.provider, account.backend),
            adapter=self.adapter,
            models=tuple(models),
            effort_levels=effort_levels,
            model_list=result.returncode == 0,
            reasoning=True,
            streaming=True,
            structured_output=True,
            workspace_binding=True,
            tool_use=True,
            profile_isolation=False,
            isolation_note="The installed agy runtime does not advertise isolated account state; saved Gemini profiles are metadata-only until such support exists.",
            credential_status="provider-managed",
            runtime_status="Connected" if status == "OK" and result.returncode == 0 else "Unavailable",
            error=error,
            supported_roles=("executor",),
        )


class OpenAICompatibleAdapter:
    provider = "api"
    adapter = "openai_compatible"

    def capabilities(self, config: OrchestratorConfig, account: AccountConfig) -> ProviderCapabilities:
        del config
        try:
            _api_url(account.base_url)
            reference = account.auth_reference
            variable = reference[4:] if reference.startswith("env:") else ""
            credential_status = "configured" if variable and os.environ.get(variable) else "missing"
            error = None
        except ProviderError as exc:
            credential_status = "invalid"
            error = str(exc)
        models = _configured_model_rows(account)
        efforts = tuple(account.supported_reasoning_efforts)
        return ProviderCapabilities(
            provider=self.provider,
            provider_label=provider_label(self.provider, account.backend),
            adapter=self.adapter,
            models=tuple(models),
            effort_levels=efforts,
            model_list=bool(account.available_models),
            reasoning=bool(efforts),
            streaming=False,
            structured_output=False,
            conversation_resume=False,
            workspace_binding=False,
            tool_use=False,
            profile_isolation=True,
            isolation_note="API credentials are referenced by environment variable; no key is persisted.",
            credential_status=credential_status,
            runtime_status="Configured" if error is None and credential_status == "configured" else "Unavailable",
            error=error,
            supported_roles=("orchestrator", "architect", "reviewer"),
        )

    def run(
        self,
        *,
        agent: AgentConfig,
        repository: Path,
        prompt: str,
        output_path: Path,
        config: OrchestratorConfig,
    ) -> CommandResult:
        del repository
        metadata = {
            "executor_provider": "api",
            "provider_adapter": self.adapter,
            "profile_id": agent.account_name,
            "model": agent.model,
            "reasoning_effort": agent.reasoning_effort or "provider-default",
        }
        try:
            url = _api_url(agent.base_url)
            _, secret = _secret_from_reference(agent.auth_reference)
            model = agent.model or (agent.available_models[0] if len(agent.available_models) == 1 else "")
            if not model:
                raise ProviderError("API profiles require an explicit model.")
            supported_efforts = tuple(agent.supported_reasoning_efforts)
            if agent.reasoning_effort and supported_efforts and agent.reasoning_effort not in supported_efforts:
                raise ProviderError("Selected reasoning effort is not supported by this API profile.")
            payload: dict[str, Any] = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
            }
            if agent.reasoning_effort and supported_efforts:
                payload["reasoning_effort"] = agent.reasoning_effort
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            request = urllib.request.Request(
                url,
                data=body,
                method="POST",
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {secret}",
                },
            )
            opener = urllib.request.build_opener(_NoRedirect())
            with opener.open(request, timeout=max(float(config.app_server_turn_timeout), 1.0)) as response:
                raw = response.read(4 * 1024 * 1024)
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise ProviderError("API response must be a JSON object.")
            choices = data.get("choices")
            message = choices[0].get("message") if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, list):
                content = "".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
            if not isinstance(content, str):
                raise ProviderError("API response did not contain choices[0].message.content.")
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(content.rstrip() + "\n", encoding="utf-8", newline="\n")
            return CommandResult([self.adapter, "POST", f"model={model}"], 0, content, "", metadata)
        except KeyboardInterrupt:
            metadata["provider_status"] = "CANCELED"
            return CommandResult([self.adapter, "POST"], 130, "", "API request canceled.", metadata)
        except urllib.error.HTTPError as exc:
            metadata["provider_status"] = "ERROR"
            try:
                exc.close()
            except OSError:
                pass
            return CommandResult([self.adapter, "POST"], exc.code, "", f"API provider returned HTTP {exc.code}.", metadata)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, ProviderError) as exc:
            metadata["provider_status"] = "ERROR"
            return CommandResult([self.adapter, "POST"], 1, "", _safe_error(exc), metadata)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def provider_capabilities(config: OrchestratorConfig, account: AccountConfig) -> ProviderCapabilities:
    if account.backend == "claude_code":
        from .claude_code import ClaudeCodeAdapter

        return ClaudeCodeAdapter().capabilities(config, account)
    if account.backend == "antigravity":
        return AntigravityAdapter().capabilities(config, account)
    if account.backend == "api":
        return OpenAICompatibleAdapter().capabilities(config, account)
    return CodexAdapter().capabilities(config, account)


def api_adapter() -> OpenAICompatibleAdapter:
    return OpenAICompatibleAdapter()


def provider_supports_role(config: OrchestratorConfig, account: AccountConfig, role: str) -> bool:
    """Return role support from the provider capability boundary."""

    if role == "executor" and account.backend == "api":
        return False
    if role in {"architect", "reviewer", "orchestrator"} and account.backend == "antigravity":
        return False
    try:
        return provider_capabilities(config, account).supports_role(role)
    except Exception:
        return False
