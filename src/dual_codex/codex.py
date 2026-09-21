from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from .bootstrap import configured_actor_prompt
from .config import AgentConfig, SUPPORTED_ROLES
from .paths import path_identity_key, same_path
from .process import CommandError, CommandResult, codex_environment, run_command


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
    configured_actor: bool = True,
) -> dict[str, Any]:
    """Build non-secret, control-plane-owned actor provenance."""

    transport = {
        "antigravity": "antigravity",
        "app_server": "app_server",
        "windows": "codex_terminal",
        "api": "api",
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
    }
    if canonical_root is not None:
        metadata.update(
            {
                "canonical_instructions_root": str(canonical_root),
                "canonical_bootstrap_required": True,
                "canonical_bootstrap_source": "machine-wide",
            }
        )
    return metadata


def _annotate_provider_result(
    result: CommandResult,
    agent: AgentConfig,
    role: str,
    *,
    repository: Path | None = None,
    canonical_root: Path | None = None,
    configured_actor: bool = True,
) -> CommandResult:
    result.metadata.update(
        configured_actor_provenance(
            agent=agent,
            role=role,
            repository=repository or Path("."),
            canonical_root=canonical_root,
            configured_actor=configured_actor,
        )
    )
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
    prepared_prompt, canonical_root = configured_actor_prompt(task, role=role)
    dispatch = runner
    result = dispatch(
        config=config,
        agent=agent,
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
    result.metadata.update(
        configured_actor_provenance(
            agent=agent,
            role=role,
            repository=repository,
            canonical_root=canonical_root,
        )
    )
    return result


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
        prompt, canonical_root = configured_actor_prompt(prompt, role=role)
    if agent is None:
        raise ValueError(f"Required role '{role}' is unassigned.")
    if role == "executor" and agent.backend != "antigravity":
        raise ValueError(
            "Dual Agents requires Antigravity/Gemini as the Executor backend; no fallback is permitted."
        )
    if agent.backend == "api":
        if role == "executor":
            raise ValueError(
                "API profiles are not enabled for the Executor role; Antigravity/Gemini remains the active Executor backend."
            )
        from .providers import api_adapter

        return _annotate_provider_result(api_adapter().run(
            agent=agent,
            repository=repository,
            prompt=prompt,
            output_path=output_path,
            config=config,
        ), agent, role, repository=repository, canonical_root=canonical_root, configured_actor=configured)
    if agent.backend == "antigravity":
        if role != "executor":
            raise ValueError("Antigravity backend is reserved for the Executor role.")
        from .antigravity import run_antigravity
        from .providers import resolve_antigravity_agent

        agent = resolve_antigravity_agent(config, agent)

        return _annotate_provider_result(run_antigravity(
            command=getattr(config, "antigravity_command", "agy"),
            agent=agent,
            repository=repository,
            prompt=prompt,
            output_path=output_path,
            schema_path=schema_path,
            config=config,
            progress=progress,
        ), agent, role, repository=repository, canonical_root=canonical_root, configured_actor=configured)
    if agent.backend == "app_server":
        from .terminal import session_id_for

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
            progress=progress,
        )
        if result.returncode != 0:
            raise CommandError(
                f"Codex {role} failed through the configured App Server backend: {result.stderr}"
            )
        return _annotate_provider_result(
            result,
            agent,
            role,
            repository=repository,
            canonical_root=canonical_root,
            configured_actor=configured,
        )
    if agent.backend != "windows":
        raise ValueError(f"Unsupported Codex backend '{agent.backend}'.")
    from .terminal import session_id_for

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
    if result.returncode != 0:
        raise CommandError(
            f"Codex {role} failed through the configured Windows terminal backend: {result.stderr}"
        )
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
    from .terminal import TerminalError, TerminalManager, TerminalSetupRequiredError

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
    manager = TerminalManager(config)
    try:
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
            add_dirs = (task_artifact_path.parent.resolve(),) if task_artifact_path is not None else ()
        ensure_kwargs = {
            "session_id": session_id,
            "agent": agent,
            "role": role or ("executor" if agent.sandbox == "workspace-write" else "architect"),
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
            turn_start = manager.send(session.session_id, prompt, lease_owner=lease_owner)
            result = manager.wait_for_turn(session.session_id, cursor=cursor, progress=progress)
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
    except TerminalError as exc:
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
