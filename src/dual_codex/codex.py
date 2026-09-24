from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from .bootstrap import (
    BOOTSTRAP_MARKER,
    bootstrap_artifact_dir,
    cleanup_canonical_bootstrap,
    configured_actor_prompt,
    create_canonical_bootstrap,
    select_required_skills,
)
from .config import AgentConfig, SUPPORTED_ROLES
from .paths import path_identity_key, same_path
from .process import CommandError, CommandResult, codex_environment, run_command


class ActorAvailabilityError(CommandError):
    """A configured actor could not be used for an availability-class reason."""

    def __init__(self, message: str, *, failure_class: str, actor: str, metadata: dict[str, Any] | None = None):
        super().__init__(message)
        self.failure_class = failure_class
        self.actor = actor
        self.metadata = dict(metadata or {})


_AVAILABILITY_MARKERS = {
    "quota": "quota_exhausted",
    "rate limit": "quota_exhausted",
    "rate_limit": "quota_exhausted",
    "temporarily unavailable": "provider_unavailable",
    "service unavailable": "provider_unavailable",
    "connection": "transport_unavailable",
    "timed out": "transport_unavailable",
    "timeout": "transport_unavailable",
    "authentication": "authentication_unavailable",
    "not logged in": "authentication_unavailable",
    "login status": "authentication_unavailable",
    "not_configured": "profile_readiness_unavailable",
    "not_ready": "profile_readiness_unavailable",
    "process unavailable": "process_unavailable",
    "unusable_runtime": "unusable_runtime",
}


def classify_actor_failure(result: CommandResult, *, backend: str = "") -> str | None:
    text = f"{result.stderr}\n{result.stdout}".casefold()
    if any(
        marker in text
        for marker in (
            "blocked by policy",
            "outside of the project",
            "outside the project",
            "outside project",
            "writing outside",
            "permission denied",
            "approval denied",
            "user denied",
            "security policy",
            "sandbox denied",
        )
    ):
        return None
    explicit = result.metadata.get("availability_failure_class")
    if isinstance(explicit, str) and explicit:
        return explicit
    terminal = str(result.metadata.get("antigravity_terminal_status", "")).upper()
    if terminal in {"FAILED", "CANCELED", "CANCELLED"}:
        return None
    if terminal in {"MALFORMED", "STARTUP_ERROR", "PREMATURE_CLOSE", "TIMEOUT"}:
        return "provider_runtime_unavailable"
    for marker, failure_class in _AVAILABILITY_MARKERS.items():
        if marker in text:
            return failure_class
    if backend in {"app_server", "windows", "antigravity"} and result.returncode != 0:
        return "provider_runtime_unavailable"
    return None


def _raise_dispatch_failure(result: CommandResult, *, role: str, agent: AgentConfig, message: str) -> None:
    if result.returncode == 0:
        return
    failure_class = classify_actor_failure(result, backend=agent.backend)
    if failure_class:
        raise ActorAvailabilityError(
            message,
            failure_class=failure_class,
            actor=agent.account_name,
            metadata=result.metadata,
        )
    raise CommandError(message)


def run_codex_app_server(**kwargs) -> CommandResult:
    from .app_server import run_codex_app_server as _run_codex_app_server

    return _run_codex_app_server(**kwargs)


def _report_from_message(message: str) -> dict | None:
    from .app_server import _normalise_report

    normalised = _normalise_report(message)
    candidates = [normalised.strip()]
    if normalised != message:
        candidates.append(message.strip())
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


def run_codex_exec(
    *,
    codex_command: str,
    agent: AgentConfig,
    repository: Path,
    prompt: str,
    output_path: Path,
    schema_path: Path,
    check: bool = True,
    progress: Callable[[str], None] | None = None,
) -> CommandResult:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        codex_command,
        "exec",
        "--cd",
        str(repository),
        "--sandbox",
        agent.sandbox,
        "-c",
        f'sandbox_mode="{agent.sandbox}"',
        "--output-last-message",
        str(output_path),
        "--output-schema",
        str(schema_path),
        "-c",
        f'model_reasoning_effort="{agent.reasoning_effort}"',
    ]
    if agent.sandbox == "workspace-write":
        command.extend(["--add-dir", str(repository)])
    if agent.model:
        command.extend(["--model", agent.model])
    command.append("-")
    return run_command(
        command,
        cwd=repository,
        env=codex_environment(agent),
        stdin=prompt,
        check=check,
        progress=progress,
    )


def _identity_digest(value: Path | None) -> str:
    if value is None:
        return ""
    return hashlib.sha256(path_identity_key(value).encode("utf-8")).hexdigest()[:16]


def _same_configured_actor(left: AgentConfig, right: AgentConfig) -> bool:
    """Compare trusted actor identity without relying on display labels."""

    if (
        left.account_name != right.account_name
        or left.backend != right.backend
        or left.provider_type != right.provider_type
        or left.adapter_type != right.adapter_type
        or left.auth_mode != right.auth_mode
        or left.auth_reference != right.auth_reference
    ):
        return False
    if not same_path(left.codex_home, right.codex_home):
        return False
    if left.state_root is not None or right.state_root is not None:
        if left.state_root is None or right.state_root is None or not same_path(left.state_root, right.state_root):
            return False
    return (
        left.model == right.model
        and left.runtime_model == right.runtime_model
        and left.reasoning_effort == right.reasoning_effort
        and left.fixed_mode == right.fixed_mode
    )


def configured_actor_provenance(
    *,
    agent: AgentConfig,
    role: str,
    repository: Path,
    canonical_root: Path | None = None,
    bootstrap=None,
    configured_actor: bool = True,
) -> dict[str, Any]:
    """Build non-secret, control-plane-owned actor provenance."""

    transport = {
        "antigravity": "antigravity",
        "app_server": "app_server",
        "windows": "codex_terminal",
        "api": "api",
        "claude_code": "claude_code",
    }.get(agent.backend, agent.backend or "unknown")
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
        "delegation_transport": transport,
        "repository": str(repository.expanduser().resolve()),
        "codex_home_identity": _identity_digest(agent.codex_home),
        "state_root_identity": _identity_digest(agent.state_root),
        "fallback_used": False,
        "primary_actor": agent.account_name,
        "actual_actor": agent.account_name,
        "fallback_enabled": False,
        "failed_actor": "",
        "fallback_actor": "",
        "fallback_reason": "",
        "fallback_failure_class": "",
        "auth_mode": agent.auth_mode,
        "session_id": "",
        "runtime_version": "",
    }
    if canonical_root is not None:
        metadata.update(
            {
                "canonical_instructions_root": str(canonical_root),
                "canonical_bootstrap_required": True,
                "canonical_bootstrap_source": "machine-wide",
            }
        )
    if bootstrap is not None:
        metadata.update(bootstrap.metadata())
    return metadata


def _annotate_provider_result(
    result: CommandResult,
    agent: AgentConfig,
    role: str,
    *,
    repository: Path | None = None,
    canonical_root: Path | None = None,
    bootstrap=None,
    configured_actor: bool = True,
) -> CommandResult:
    result.metadata.update(
        configured_actor_provenance(
            agent=agent,
            role=role,
            repository=repository or Path("."),
            canonical_root=canonical_root,
            bootstrap=bootstrap,
            configured_actor=configured_actor,
        )
    )
    if result.metadata.get("claude_session_id"):
        result.metadata["session_id"] = result.metadata["claude_session_id"]
    if result.metadata.get("claude_runtime_version"):
        result.metadata["runtime_version"] = result.metadata["claude_runtime_version"]
    return result


def _delegate_to_configured_actor(
    *,
    config,
    role: str,
    task: str,
    repository: Path,
    output_path: Path | None = None,
    schema_path: Path | None = None,
    request_id: str = "",
    run_id: str = "",
    progress: Callable[[str], None] | None = None,
    runner: Callable[..., CommandResult],
) -> CommandResult:
    """Resolve a role from the registry and dispatch only to that actor.

    ``runner`` is an internal seam used only by the orchestrator's deterministic
    provider boundary. It cannot change the resolved actor; the trusted
    ``AgentConfig`` and provenance are bound before the call.
    """

    if role not in SUPPORTED_ROLES:
        raise ValueError(f"Unsupported configured role '{role}'.")
    agent = config.agent_for_role(role)
    if output_path is None:
        output_path = config.runs_dir / f".configured-{role}.json"
    if schema_path is None:
        schema_name = {
            "architect": "plan.schema.json",
            "executor": "delegation-report.schema.json",
            "reviewer": "review.schema.json",
            "orchestrator": "plan.schema.json",
        }[role]
        schema_path = config.project_root / "schemas" / schema_name
    bootstrap = create_canonical_bootstrap(
        role=role,
        artifact_dir=bootstrap_artifact_dir(repository, output_path),
        selected_skills=select_required_skills(role, task),
    )
    canonical_root = bootstrap.source_root
    prepared_prompt, bootstrap = configured_actor_prompt(task, role=role, bootstrap=bootstrap)
    dispatch = runner
    primary_agent = agent
    actual_agent = agent
    fallback_used = False
    failed_actor = ""
    fallback_reason = ""
    fallback_failure_class = ""
    fallback_enabled = bool(getattr(config, "fallback_enabled", False))

    def invoke(dispatch_config, dispatch_agent):
        result = dispatch(
            config=dispatch_config,
            agent=dispatch_agent,
            role=role,
            repository=repository,
            prompt=prepared_prompt,
            output_path=output_path,
            schema_path=schema_path,
            request_id=request_id,
            run_id=run_id,
            progress=progress,
        )
        if not isinstance(result, CommandResult):
            raise TypeError("Configured actor dispatch must return CommandResult.")
        if result.returncode != 0:
            _raise_dispatch_failure(
                result,
                role=role,
                agent=dispatch_agent,
                message=f"Configured actor '{dispatch_agent.account_name}' failed for role '{role}': {result.stderr}",
            )
        return result

    try:
        try:
            result = invoke(config, primary_agent)
        except ActorAvailabilityError as exc:
            if not fallback_enabled:
                raise
            failed_actor = exc.actor
            fallback_reason = str(exc)
            fallback_failure_class = exc.failure_class
            # Account ids provide a stable deterministic ordering independent of
            # filesystem or mapping iteration order.
            from .providers import provider_supports_role

            candidates = []
            accounts = getattr(config, "accounts", {})
            for account_name in sorted(accounts):
                account = accounts[account_name]
                if account_name == primary_agent.account_name or not account.enabled:
                    continue
                if role not in getattr(account, "fallback_roles", ()):
                    continue
                if not provider_supports_role(config, account, role):
                    continue
                candidates.append(account_name)
            if not candidates:
                raise
            fallback_name = candidates[0]
            fallback_config = replace(config, roles={**config.roles, role: fallback_name})
            fallback_agent = fallback_config.agent_for_role(role)
            result = invoke(fallback_config, fallback_agent)
            actual_agent = fallback_agent
            fallback_used = True
        provenance = configured_actor_provenance(
            agent=actual_agent,
            role=role,
            repository=repository,
            canonical_root=canonical_root,
            bootstrap=bootstrap,
        )
        for key, value in provenance.items():
            result.metadata.setdefault(key, value)
        result.metadata.update({
            "primary_actor": primary_agent.account_name,
            "actual_actor": actual_agent.account_name,
            "fallback_enabled": fallback_enabled,
            "fallback_used": fallback_used,
            "failed_actor": failed_actor,
            "fallback_actor": actual_agent.account_name if fallback_used else "",
            "fallback_reason": fallback_reason,
            "fallback_failure_class": fallback_failure_class,
        })
        return result
    finally:
        cleanup_canonical_bootstrap(bootstrap)


def delegate_to_configured_actor(
    *,
    config,
    role: str,
    task: str,
    repository: Path,
    output_path: Path | None = None,
    schema_path: Path | None = None,
    request_id: str = "",
    run_id: str = "",
    progress: Callable[[str], None] | None = None,
) -> CommandResult:
    """Resolve and dispatch a phase through its configured actor only."""

    return _delegate_to_configured_actor(
        config=config,
        role=role,
        task=task,
        repository=repository,
        output_path=output_path,
        schema_path=schema_path,
        request_id=request_id,
        run_id=run_id,
        progress=progress,
        runner=run_codex_for_role,
    )


def run_codex_for_role(
    *,
    config,
    agent: AgentConfig | None = None,
    role: str,
    repository: Path,
    prompt: str,
    output_path: Path,
    schema_path: Path,
    request_id: str = "",
    run_id: str = "",
    progress: Callable[[str], None] | None = None,
) -> CommandResult:
    """Dispatch orchestration through the configured account backend."""
    if role not in SUPPORTED_ROLES:
        raise ValueError(f"Unsupported configured role '{role}'.")
    configured = False
    canonical_root = None
    bootstrap = None
    configured_resolver = getattr(config, "agent_for_role", None)
    if callable(configured_resolver):
        configured_agent = configured_resolver(role)
        if agent is None:
            agent = configured_agent
        elif not _same_configured_actor(agent, configured_agent):
            raise ValueError(
                f"Role '{role}' must use its configured actor '{configured_agent.account_name}'; "
                "caller-supplied actor identity is not authoritative."
            )
        configured = True
        if BOOTSTRAP_MARKER not in prompt:
            bootstrap = create_canonical_bootstrap(
                role=role,
                artifact_dir=bootstrap_artifact_dir(repository, output_path),
                selected_skills=select_required_skills(role, prompt),
            )
            prompt, bootstrap = configured_actor_prompt(prompt, role=role, bootstrap=bootstrap)
        else:
            bootstrap = create_canonical_bootstrap(
                role=role,
                selected_skills=select_required_skills(role, prompt),
            )
        canonical_root = bootstrap.source_root
    if agent is None:
        raise ValueError(f"Required role '{role}' is unassigned.")
    if agent.backend == "api":
        if role == "executor":
            raise ValueError(
                "API profiles do not provide the workspace-write Executor role."
            )
        from .providers import api_adapter

        try:
            result = api_adapter().run(
                agent=agent,
                repository=repository,
                prompt=prompt,
                output_path=output_path,
                config=config,
            )
            _raise_dispatch_failure(result, role=role, agent=agent, message=f"{agent.provider_type} {role} dispatch failed: {result.stderr}")
            return _annotate_provider_result(result, agent, role, repository=repository, canonical_root=canonical_root,
                bootstrap=bootstrap, configured_actor=configured)
        finally:
            cleanup_canonical_bootstrap(bootstrap)
    if agent.backend == "claude_code":
        from .claude_code import run_claude_code

        try:
            result = run_claude_code(
                command=getattr(config, "claude_command", "claude"),
                agent=agent,
                role=role,
                repository=repository,
                prompt=prompt,
                output_path=output_path,
                schema_path=schema_path,
                config=config,
                progress=progress,
            )
            _raise_dispatch_failure(
                result,
                role=role,
                agent=agent,
                message=f"Claude {role} dispatch failed: {result.stderr}",
            )
            return _annotate_provider_result(
                result,
                agent,
                role,
                repository=repository,
                canonical_root=canonical_root,
                bootstrap=bootstrap,
                configured_actor=configured,
            )
        finally:
            cleanup_canonical_bootstrap(bootstrap)
    if agent.backend == "antigravity":
        if role != "executor":
            raise ValueError("Antigravity backend is reserved for the Executor role.")
        from .antigravity import run_antigravity
        from .providers import resolve_antigravity_agent

        agent = resolve_antigravity_agent(config, agent)

        try:
            result = run_antigravity(
                command=getattr(config, "antigravity_command", "agy"),
                agent=agent,
                repository=repository,
                prompt=prompt,
                output_path=output_path,
                schema_path=schema_path,
                config=config,
                progress=progress,
            )
            _raise_dispatch_failure(result, role=role, agent=agent, message=f"{agent.provider_type} {role} dispatch failed: {result.stderr}")
            return _annotate_provider_result(result, agent, role, repository=repository, canonical_root=canonical_root,
                bootstrap=bootstrap, configured_actor=configured)
        finally:
            cleanup_canonical_bootstrap(bootstrap)
    if agent.backend == "app_server":
        from .terminal import session_id_for

        try:
            result = run_codex_app_server(
                config=config,
                agent=agent,
                repository=repository,
                prompt=prompt,
                output_path=output_path,
                session_id=session_id_for(agent.account_name, repository),
                request_id=request_id,
                run_id=run_id,
                role=role,
                configured_actor=configured,
                require_workspace_ready=(role == "executor" and agent.sandbox == "workspace-write"),
                progress=progress,
            )
            _raise_dispatch_failure(result, role=role, agent=agent,
                message=f"Codex {role} failed through the configured App Server backend: {result.stderr}")
            return _annotate_provider_result(
                result,
                agent,
                role,
                repository=repository,
                canonical_root=canonical_root,
                bootstrap=bootstrap,
                configured_actor=configured,
            )
        finally:
            cleanup_canonical_bootstrap(bootstrap)
    if agent.backend != "windows":
        raise ValueError(f"Unsupported Codex backend '{agent.backend}'; no fallback is permitted.")
    if role == "executor" and not hasattr(config, "runs_dir"):
        raise ValueError("Codex Executor dispatch requires a complete configured profile; no fallback is permitted.")
    from .terminal import session_id_for

    try:
        result = run_codex_terminal(
            config=config,
            agent=agent,
            repository=repository,
            prompt=prompt,
            output_path=output_path,
            session_id=session_id_for(agent.account_name, repository),
            role=role,
            progress=progress,
        )
    finally:
        cleanup_canonical_bootstrap(bootstrap)
    _raise_dispatch_failure(result, role=role, agent=agent,
        message=f"Codex {role} failed through the configured Windows terminal backend: {result.stderr}")
    return _annotate_provider_result(
        result,
        agent,
        role,
        repository=repository,
        canonical_root=canonical_root,
        configured_actor=configured,
    )


def run_codex_terminal(
    *,
    config,
    agent: AgentConfig,
    repository: Path,
    prompt: str,
    output_path: Path,
    session_id: str,
    role: str = "",
    task_artifact_path: Path | None = None,
    task_sha256: str = "",
    reuse_existing: bool = False,
    progress: Callable[[str], None] | None = None,
) -> CommandResult:
    from .terminal import (
        TERMINAL_INLINE_MESSAGE_MAX,
        TerminalError,
        TerminalManager,
        TerminalSetupRequiredError,
        executor_task_artifact_dir,
    )

    resolved_role = role or ("executor" if agent.sandbox == "workspace-write" else "architect")
    temporary_task_artifact_path: Path | None = None
    temporary_task_artifact_content = ""
    task_input_attempted = False
    task_turn_completed = False
    transport = "file" if task_artifact_path is not None else "inline"
    artifact = str(task_artifact_path.resolve()) if task_artifact_path is not None else ""
    metadata = {
        "terminal_session_id": session_id,
        "task_transport": transport,
        "task_artifact": artifact,
        "task_sha256": task_sha256,
        "reuse_existing": reuse_existing,
    }
    if task_artifact_path is not None and not task_artifact_path.is_file():
        return CommandResult(
            ["codex", "--no-alt-screen", "--sandbox", agent.sandbox],
            1,
            "",
            f"Task artifact does not exist: {task_artifact_path}",
            metadata,
        )
    try:
        manager = TerminalManager(config)
        if task_artifact_path is None and len(prompt) > TERMINAL_INLINE_MESSAGE_MAX:
            temporary_task_artifact_content = f"# Dual Codex {resolved_role} instructions\n\n{prompt.rstrip()}\n"
            if agent.sandbox == "read-only":
                # Read-only Codex rejects extra roots; stage the prompt under cwd and remove it after the turn.
                repository_root = Path(repository).expanduser().resolve()
                if not repository_root.is_dir():
                    raise TerminalError(f"Task repository does not exist: {repository_root}")
                for _attempt in range(3):
                    candidate = repository_root / f".dual-codex-task-{uuid4().hex}.md"
                    try:
                        with candidate.open("x", encoding="utf-8", newline="\n") as stream:
                            stream.write(temporary_task_artifact_content)
                        task_artifact_path = candidate
                        break
                    except FileExistsError:
                        continue
                if task_artifact_path is None:
                    raise TerminalError("Could not allocate a unique read-only task artifact.")
                temporary_task_artifact_path = task_artifact_path
            else:
                if resolved_role != "executor":
                    raise TerminalError("Only the configured Executor may use writable terminal task transport.")
                artifact_dir = executor_task_artifact_dir(config, create=True)
                task_artifact_path = artifact_dir / f"{resolved_role}-{uuid4().hex}.md"
                task_artifact_path.write_text(temporary_task_artifact_content, encoding="utf-8", newline="\n")
            content = temporary_task_artifact_content
            task_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
            prompt = (
                f'Read the complete {resolved_role} instructions from "{task_artifact_path}" '
                "and return the requested result."
            )
            transport = "file"
            artifact = str(task_artifact_path.resolve())
            metadata.update(
                {
                    "task_transport": transport,
                    "task_artifact": artifact,
                    "task_sha256": task_sha256,
                }
            )
        if task_artifact_path is not None and not task_artifact_path.is_file():
            return CommandResult(
                ["codex", "--no-alt-screen", "--sandbox", agent.sandbox],
                1,
                "",
                f"Task artifact does not exist: {task_artifact_path}",
                metadata,
            )
        if reuse_existing:
            try:
                manager._load(session_id)
                current = manager.status(session_id)
            except TerminalError as exc:
                return CommandResult(
                    ["codex", "--no-alt-screen", "--sandbox", agent.sandbox],
                    1,
                    "",
                    f"Strict reuse requires an existing terminal session: {exc}",
                    metadata,
                )
            if current.get("state") != "running" or current.get("alive") is False:
                return CommandResult(
                    ["codex", "--no-alt-screen", "--sandbox", agent.sandbox],
                    1,
                    "",
                    f"Strict reuse requires a running terminal session; observed state '{current.get('state', 'unknown')}'.",
                    metadata,
                )
            # The pre-opened TUI may not have been launched with the task-artifact
            # directory. Reuse it without requesting a new add-dir or spawning a
            # replacement; the short control message still points at the immutable
            # artifact captured by the orchestrator.
            add_dirs = ()
        else:
            artifact_parent = task_artifact_path.parent.resolve() if task_artifact_path is not None else None
            add_dirs = (
                (artifact_parent,)
                if artifact_parent is not None and not same_path(artifact_parent, repository)
                else ()
            )
        ensure_kwargs = {
            "session_id": session_id,
            "agent": agent,
            "role": resolved_role,
            "repository": repository,
            "approval_policy": "never" if agent.sandbox == "workspace-write" else "on-request",
            "add_dirs": add_dirs,
            "reuse_existing": reuse_existing,
        }
        if not reuse_existing:
            ensure_kwargs["visible"] = agent.sandbox == "workspace-write"
        session = manager.ensure(
            **ensure_kwargs,
        )
        cursor = manager.turn_cursor(session.session_id)
        lease_owner = manager.begin_automation_turn(session.session_id)
        try:
            task_input_attempted = True
            turn_start = manager.send(session.session_id, prompt, lease_owner=lease_owner)
            result = manager.wait_for_turn(session.session_id, cursor=cursor, progress=progress)
            task_turn_completed = True
        finally:
            manager.release_input_lease(session.session_id, lease_owner)
        assistant = result.get("assistant", "")
        report = _report_from_message(assistant)
        status_snapshot = manager.status(session.session_id)
        readiness = getattr(manager, "_last_readiness_diagnostics", {})
        if not isinstance(readiness, dict):
            readiness = {}
        terminal_pid = int(status_snapshot.get("pid") or session.pid)
        host_pid = int(status_snapshot.get("host_pid") or session.pid)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if temporary_task_artifact_path is not None:
            archived_task_artifact = output_path.with_name(f"{output_path.stem}-{uuid4().hex}.task.md")
            archived_task_artifact.write_text(temporary_task_artifact_content, encoding="utf-8", newline="\n")
            artifact = str(archived_task_artifact.resolve())
            metadata.update(
                {
                    "task_transport": "file",
                    "task_artifact": artifact,
                    "task_sha256": task_sha256,
                }
            )
            turn_start.update(
                {
                    "task_transport": "file",
                    "task_artifact": artifact,
                    "task_sha256": task_sha256,
                }
            )
        output_path.write_text(
            json.dumps(report, ensure_ascii=False) if report is not None else assistant,
            encoding="utf-8",
        )
        return CommandResult(
            ["codex", "--no-alt-screen", "--sandbox", agent.sandbox],
            0,
            assistant,
            "",
            {
                **metadata,
                "terminal_session_id": session.session_id,
                "codex_session_id": result.get("session_id", ""),
                "terminal_turn_start": json.dumps(turn_start, ensure_ascii=True),
                "reuse_existing": reuse_existing,
                "reuse_provenance": {
                    "mode": "strict_existing" if reuse_existing else "reuse_or_start",
                    "terminal_session_id": session.session_id,
                    "session_record": getattr(session, "session_file", ""),
                    "session_host_pid": session.pid,
                    "session_process_epoch": getattr(session, "process_epoch", ""),
                    "session_process_start_identity": getattr(session, "process_start_identity", ""),
                    "terminal_pid": terminal_pid,
                    "host_pid": host_pid,
                    "account": session.account,
                    "account_label": getattr(session, "label", ""),
                    "role": session.role,
                    "repository_identity": getattr(session, "repository_identity", "") or str(session.repository),
                    "codex_home_identity": getattr(session, "codex_home_identity", "") or str(session.codex_home),
                    "codex_session_id": result.get("session_id", ""),
                    "repository": str(session.repository),
                    "codex_home": str(session.codex_home),
                    "pid": session.pid,
                    "host_pid": host_pid,
                    "process_started_at": getattr(session, "process_started_at", 0.0),
                    "pipe": getattr(session, "pipe", ""),
                    "viewer_attached": bool(
                        isinstance(status_snapshot.get("viewer"), dict)
                        and status_snapshot["viewer"].get("attached") is True
                    ),
                    "viewer_pid": int(status_snapshot.get("viewer_pid") or getattr(session, "viewer_pid", 0) or 0),
                    "viewer_epoch": str(
                        status_snapshot.get("viewer_epoch")
                        or getattr(session, "viewer_epoch", "")
                        or ""
                    ),
                    "target_model": str(readiness.get("target_model") or "unknown"),
                    "target_reasoning": str(readiness.get("target_reasoning") or "unknown"),
                    "model_provenance": str(readiness.get("model_provenance") or "unavailable"),
                    "reasoning_provenance": str(
                        readiness.get("reasoning_provenance") or "unavailable"
                    ),
                },
            },
        )
    except (TerminalError, OSError) as exc:
        metadata["terminal_error_type"] = type(exc).__name__
        if isinstance(exc, TerminalSetupRequiredError):
            metadata["terminal_setup_required"] = True
        return CommandResult(
            ["codex", "--no-alt-screen", "--sandbox", agent.sandbox],
            1,
            "",
            str(exc),
            metadata,
        )
    finally:
        if temporary_task_artifact_path is not None:
            if task_input_attempted and not task_turn_completed:
                metadata["task_artifact_cleanup_pending"] = str(temporary_task_artifact_path)
            else:
                try:
                    temporary_task_artifact_path.unlink(missing_ok=True)
                except OSError as exc:
                    metadata["task_artifact_cleanup_error"] = str(exc)
