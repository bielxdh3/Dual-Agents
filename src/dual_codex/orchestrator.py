from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading
from uuid import uuid4

from .bootstrap import (
    canonical_bootstrap_artifacts,
    canonical_bootstrap_control_paths,
    canonical_instructions_root,
    reconcile_orphan_canonical_bootstrap,
)
from .codex import _delegate_to_configured_actor
from .codex import configured_actor_provenance, run_codex_for_role
from .config import ConfigError, OrchestratorConfig
from .delegation import RepositoryLock
from .git import (
    attribute_git_mutations,
    capture_git_baseline,
    ensure_git_repository,
    git_top_level,
    status_and_diff,
)
from .paths import same_path
from .report import atomic_write_json, dump_json, load_json, render_markdown


@dataclass(frozen=True)
class RunOutcome:
    run_dir: Path
    verdict: str
    correction_cycles: int
    phase_provenance: tuple[dict, ...] = ()


def delegate_to_configured_actor(**kwargs):
    """Compatibility wrapper that keeps the registry as the source of truth.

    The runner is passed explicitly so deterministic tests can replace the
    provider boundary without changing role resolution or actor provenance.
    """

    return _delegate_to_configured_actor(**kwargs, runner=run_codex_for_role)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _prompt(config: OrchestratorConfig, name: str, **values: str) -> str:
    template = _read(config.project_root / "prompts" / name)
    return template.format(**values)


def _schema(config: OrchestratorConfig, name: str) -> Path:
    return config.project_root / "schemas" / name


def _reviewer_phase_context(config: OrchestratorConfig, phase_provenance: list[dict]) -> dict:
    fields = (
        "role",
        "primary_actor",
        "actual_actor",
        "provider",
        "backend",
        "fallback_enabled",
        "fallback_used",
        "repository",
    )
    reviewer = config.agent_for_role("reviewer")
    return {
        "completed_phases": [
            {field: item.get(field, "") for field in fields}
            for item in phase_provenance
            if item.get("role") in {"architect", "executor"}
        ],
        "current_phase": {
            "role": "reviewer",
            "configured_actor": reviewer.account_name,
            "provider": reviewer.provider_type,
            "backend": reviewer.backend,
            "actual_runtime_identity": "recorded by the control plane after this phase completes",
        },
    }


def _write_provenance(
    config: OrchestratorConfig,
    canonical_root: Path,
    run_dir: Path,
    phase_provenance: list[dict],
    run_state: dict | None = None,
) -> None:
    try:
        orchestrator_metadata = configured_actor_provenance(
            agent=config.agent_for_role("orchestrator"),
            role="orchestrator",
            repository=config.repository,
            canonical_root=canonical_root,
        )
    except ConfigError as exc:
        orchestrator_metadata = {
            "phase": "orchestrator",
            "role": "orchestrator",
            "configured_actor": False,
            "routing_error": str(exc),
            "fallback_used": False,
        }
    payload = {
        "schema_version": 2,
        "configured_actor_routing": phase_provenance,
        "orchestrator": orchestrator_metadata,
    }
    if run_state is not None:
        payload.update(
            {
                "run_id": run_state.get("run_id", ""),
                "repository": run_state.get("repository", ""),
                "status": run_state.get("status", "running"),
                "current_phase": run_state.get("current_phase"),
                "last_phase": run_state.get("last_phase"),
                "current_actor": run_state.get("current_actor"),
                "current_backend": run_state.get("current_backend"),
                "last_actor": run_state.get("last_actor"),
                "last_backend": run_state.get("last_backend"),
                "provider_status": run_state.get("provider_status", "unknown"),
                "last_progress_at": run_state.get("last_progress_at"),
                "initial_git_baseline": run_state.get("initial_git_baseline"),
                "mutation_attribution": run_state.get("mutation_attribution"),
                "dual_agents_bootstrap_artifacts": run_state.get("dual_agents_bootstrap_artifacts", []),
                "failure": run_state.get("failure"),
            }
        )
    atomic_write_json(run_dir / "provenance.json", payload)


_APP_SERVER_PROGRESS = re.compile(r"^app-server turn [A-Za-z0-9._:-]{1,100} still running$")


def _progress_event(progress, **values) -> None:
    if progress is None:
        return
    try:
        progress("DUAL_CODEX_PROGRESS " + json.dumps(values, ensure_ascii=True, separators=(",", ":")))
    except (BrokenPipeError, OSError):
        # Progress is observational. A closed console must not change provider
        # dispatch or its configured hard timeout.
        return


def _safe_backend_detail(value: str) -> str | None:
    detail = str(value).strip()
    if _APP_SERVER_PROGRESS.fullmatch(detail):
        return detail
    if detail in {
        "Claude Code turn started",
        "Antigravity executor still running",
    }:
        return detail
    return None


def _atomic_write_text(path: Path, value: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid4().hex}")
    try:
        temporary.write_text(value, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _persist_run_state(
    config: OrchestratorConfig,
    canonical_root: Path,
    run_dir: Path,
    phase_provenance: list[dict],
    run_state: dict,
    *,
    required: bool = False,
) -> None:
    run_state["updated_at"] = datetime.now(timezone.utc).isoformat()
    try:
        atomic_write_json(run_dir / "run_state.json", run_state)
        _write_provenance(config, canonical_root, run_dir, phase_provenance, run_state)
        return
    except OSError as exc:
        run_state["evidence_write_error"] = type(exc).__name__
        if required:
            raise


def _phase_failure_state(exc: BaseException) -> str:
    failure_class = str(getattr(exc, "failure_class", ""))
    if (
        isinstance(exc, TimeoutError)
        or "timeout" in type(exc).__name__.casefold()
        or "timeout" in failure_class.casefold()
        or "timed out" in str(exc).casefold()
    ):
        return "timeout"
    return "failed"


def _run_owner_process_start() -> str | None:
    try:
        from .delegation import _safe_process_start_token

        return _safe_process_start_token(os.getpid())
    except Exception:
        return None


def _relative_control_path(path: Path, repository: Path) -> str | None:
    try:
        return path.resolve(strict=False).relative_to(repository).as_posix()
    except (OSError, ValueError):
        return None


def _recover_interrupted_runs(
    config: OrchestratorConfig,
    repository: Path,
    *,
    current_run_id: str,
    bootstrap_cleanup: dict[str, list[dict[str, str]]],
) -> None:
    """Finalize prior run records left running after their owner process ended."""

    runs_dir = config.runs_dir.expanduser().resolve()
    if not runs_dir.is_dir():
        return
    try:
        from .delegation import _pid_alive, _safe_process_start_token
    except Exception:
        _pid_alive = lambda _pid: True
        _safe_process_start_token = lambda _pid: None

    for item in runs_dir.iterdir():
        try:
            directory_info = item.lstat()
            if not stat.S_ISDIR(directory_info.st_mode) or stat.S_ISLNK(directory_info.st_mode):
                continue
            state_path = item / "run_state.json"
            state_info = state_path.lstat()
            if not stat.S_ISREG(state_info.st_mode) or stat.S_ISLNK(state_info.st_mode):
                continue
            run_state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
            continue
        if (
            not isinstance(run_state, dict)
            or run_state.get("status") != "running"
            or run_state.get("repository") != str(repository)
            or run_state.get("run_id") == current_run_id
        ):
            continue
        try:
            owner_pid = int(run_state.get("pid", 0))
        except (ValueError, TypeError):
            owner_pid = 0
        stored_start = run_state.get("process_start")
        if owner_pid == os.getpid():
            owner_active = False
        else:
            try:
                owner_alive = _pid_alive(owner_pid)
                live_start = _safe_process_start_token(owner_pid) if owner_alive else None
                owner_active = owner_alive and (
                    not isinstance(stored_start, str)
                    or not stored_start
                    or live_start is None
                    or live_start == stored_start
                )
            except Exception:
                owner_active = True
        if owner_active:
            continue

        baseline_path = item / "initial_git_baseline.json"
        try:
            baseline_info = baseline_path.lstat()
            if not stat.S_ISREG(baseline_info.st_mode) or stat.S_ISLNK(baseline_info.st_mode):
                raise OSError("unsafe baseline file")
            baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
            run_id = str(run_state.get("run_id", ""))
            if not isinstance(baseline, dict) or baseline.get("repository") != str(repository):
                raise ValueError("baseline repository mismatch")
            excluded = _relative_control_path(item, repository)
            excluded_paths = [excluded] if excluded else []
            excluded_paths.extend(canonical_bootstrap_control_paths(repository))
            mutation = attribute_git_mutations(
                repository,
                baseline,
                excluded_paths=excluded_paths,
            )
            removed_artifacts = [
                {"name": record["name"], "state": "reconciled_orphan"}
                for record in bootstrap_cleanup.get("removed", [])
                if record.get("run_id") == run_id or record.get("run_id") == "legacy"
            ]
            run_state["mutation_attribution"] = mutation
            run_state["dual_agents_bootstrap_artifacts"] = [
                *removed_artifacts,
                *[
                    {"name": name, "state": "retained_ambiguous"}
                    for name in canonical_bootstrap_artifacts(repository)
                ],
            ]
            run_state["status"] = "interrupted"
            run_state["provider_status"] = "unknown_after_interruption"
            run_state["failure"] = {
                "failure_type": "ProcessInterrupted",
                "observed_at": datetime.now(timezone.utc).isoformat(),
            }
            mutation_path = item / "mutation-attribution.json"
            atomic_write_json(mutation_path, mutation)
        except Exception as exc:
            run_state["status"] = "interrupted"
            run_state["provider_status"] = "unknown_after_interruption"
            run_state["mutation_attribution"] = {
                "status": "unknown",
                "reason": type(exc).__name__,
            }
            run_state["failure"] = {
                "failure_type": "ProcessInterrupted",
                "evidence_error": type(exc).__name__,
                "observed_at": datetime.now(timezone.utc).isoformat(),
            }
        interrupted_phase = run_state.get("current_phase")
        run_state["last_phase"] = interrupted_phase or run_state.get("last_phase")
        if interrupted_phase:
            run_state["last_actor"] = run_state.get("current_actor") or run_state.get("last_actor")
            run_state["last_backend"] = run_state.get("current_backend") or run_state.get("last_backend")
        run_state["current_phase"] = None
        run_state["current_actor"] = None
        run_state["current_backend"] = None
        run_state["finalized_by_run_id"] = current_run_id
        try:
            atomic_write_json(state_path, run_state)
            provenance_path = item / "provenance.json"
            if provenance_path.is_file() and not provenance_path.is_symlink():
                provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
                if isinstance(provenance, dict):
                    phases = provenance.get("configured_actor_routing")
                    if isinstance(phases, list) and phases and isinstance(phases[-1], dict):
                        if phases[-1].get("phase_state") in {"started", "running"}:
                            phases[-1]["phase_state"] = "interrupted"
                            phases[-1]["provider_status"] = "unknown_after_interruption"
                    provenance.update(
                        {
                            "status": "interrupted",
                            "current_phase": None,
                            "last_phase": run_state.get("last_phase"),
                            "current_actor": None,
                            "current_backend": None,
                            "last_actor": run_state.get("last_actor"),
                            "last_backend": run_state.get("last_backend"),
                            "provider_status": "unknown_after_interruption",
                            "mutation_attribution": run_state.get("mutation_attribution"),
                            "dual_agents_bootstrap_artifacts": run_state.get("dual_agents_bootstrap_artifacts", []),
                            "failure": run_state.get("failure"),
                        }
                    )
                    atomic_write_json(provenance_path, provenance)
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass


def _dispatch_phase(
    *,
    config: OrchestratorConfig,
    canonical_root: Path,
    run_dir: Path,
    phase_provenance: list[dict],
    run_state: dict,
    state_lock,
    progress=None,
    role: str,
    repository_trust_authorized: bool = False,
    **kwargs,
):
    started_at = datetime.now(timezone.utc).isoformat()
    actor = None
    entry: dict = {
        "phase": role,
        "role": role,
        "phase_state": "started",
        "started_at": started_at,
        "provider_status": "starting",
        "last_progress_at": None,
    }
    try:
        actor = config.agent_for_role(role)
        entry.update(
            {
                "configured_actor": actor.account_name,
                "actor_id": actor.account_name,
                "provider": actor.provider_type,
                "backend": actor.backend,
                "fallback_enabled": bool(getattr(config, "fallback_enabled", False)),
                "fallback_used": False,
                "repository": str(config.repository),
            }
        )
        try:
            entry.update(
                configured_actor_provenance(
                    agent=actor,
                    role=role,
                    repository=config.repository,
                    canonical_root=canonical_root,
                )
            )
        except Exception:
            pass
        with state_lock:
            phase_provenance.append(entry)
            run_state["current_phase"] = role
            run_state["last_phase"] = role
            run_state["current_actor"] = actor.account_name
            run_state["current_backend"] = actor.backend
            run_state["last_actor"] = actor.account_name
            run_state["last_backend"] = actor.backend
            run_state["provider_status"] = "starting"
            _persist_run_state(config, canonical_root, run_dir, phase_provenance, run_state, required=True)
        _progress_event(
            progress,
            phase=role,
            actor=actor.account_name,
            backend=actor.backend,
            state="started",
        )

        def backend_progress(detail: str) -> None:
            safe_detail = _safe_backend_detail(detail)
            timestamp = datetime.now(timezone.utc).isoformat()
            with state_lock:
                entry["phase_state"] = "running"
                entry["provider_status"] = "alive"
                entry["last_progress_at"] = timestamp
                run_state["provider_status"] = "alive"
                run_state["last_progress_at"] = timestamp
                _persist_run_state(config, canonical_root, run_dir, phase_provenance, run_state)
            event = {
                "phase": role,
                "actor": actor.account_name,
                "backend": actor.backend,
                "state": "running",
            }
            if safe_detail is not None:
                event["detail"] = safe_detail
            _progress_event(progress, **event)

        result = delegate_to_configured_actor(
            config=config,
            role=role,
            repository_trust_authorized=repository_trust_authorized,
            run_id=run_state["run_id"],
            progress=backend_progress,
            **kwargs,
        )
    except Exception as exc:
        if entry not in phase_provenance:
            phase_provenance.append(entry)
        failure_state = _phase_failure_state(exc)
        entry.update(getattr(exc, "metadata", {}))
        entry.update(
            {
                "phase_state": failure_state,
                "provider_status": "failed",
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "dispatch_failed": True,
                "failure_type": type(exc).__name__,
            }
        )
        if actor is None:
            entry["configured_actor"] = False
            entry["routing_error"] = type(exc).__name__
        if getattr(exc, "failure_class", None):
            entry["failure_class"] = str(exc.failure_class)
        with state_lock:
            run_state["current_phase"] = None
            run_state["current_actor"] = None
            run_state["current_backend"] = None
            run_state["last_phase"] = role
            if actor is not None:
                run_state["last_actor"] = actor.account_name
                run_state["last_backend"] = actor.backend
            run_state["provider_status"] = "failed"
            _persist_run_state(config, canonical_root, run_dir, phase_provenance, run_state)
        _progress_event(
            progress,
            phase=role,
            actor=actor.account_name if actor is not None else "unknown",
            backend=actor.backend if actor is not None else "unknown",
            state=failure_state,
            failure_type=type(exc).__name__,
        )
        raise
    entry.update(dict(result.metadata))
    completed_at = datetime.now(timezone.utc).isoformat()
    entry.update(
        {
            "phase": role,
            "role": role,
            "phase_state": "completed",
            "provider_status": "completed",
            "completed_at": completed_at,
            "dispatch_failed": False,
        }
    )
    with state_lock:
        run_state["current_phase"] = None
        run_state["current_actor"] = None
        run_state["current_backend"] = None
        run_state["last_phase"] = role
        run_state["last_actor"] = actor.account_name
        run_state["last_backend"] = actor.backend
        run_state["provider_status"] = "completed"
        _persist_run_state(config, canonical_root, run_dir, phase_provenance, run_state)
    _progress_event(
        progress,
        phase=role,
        actor=actor.account_name,
        backend=actor.backend,
        state="completed",
    )
    return result


def execute(
    config: OrchestratorConfig,
    task_file: Path,
    *,
    explicit_repository: bool = False,
    progress=None,
) -> RunOutcome:
    try:
        target_root = git_top_level(config.repository)
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError(f"run requires a valid Git repository root: {config.repository}") from exc
    if not same_path(target_root, config.repository):
        raise RuntimeError(
            f"run repository must be the exact Git top-level '{target_root}', not '{config.repository}'."
        )
    config = replace(config, repository=target_root)
    run_id = uuid4().hex
    lock = RepositoryLock(config.runs_dir, config.repository, "run-" + run_id, run_id)
    with lock:
        return _execute_locked(
            config,
            task_file,
            repository_trust_authorized=explicit_repository,
            repository_lock=lock,
            run_id=run_id,
            progress=progress,
        )


def _execute_locked(
    config: OrchestratorConfig,
    task_file: Path,
    *,
    repository_trust_authorized: bool = False,
    repository_lock: RepositoryLock,
    run_id: str,
    progress=None,
) -> RunOutcome:
    task_file = task_file.expanduser().resolve()
    task = _read(task_file).strip()
    if not task:
        raise ValueError("Task file is empty")

    canonical_root = canonical_instructions_root()
    ensure_git_repository(config.repository)
    from .terminal import reconcile_deferred_task_artifact_cleanup

    reconcile_deferred_task_artifact_cleanup(config, config.repository)
    configured_backends = {
        role: config.accounts[account_name].backend
        for role, account_name in config.roles.items()
        if account_name in config.accounts
    }
    bootstrap_cleanup = reconcile_orphan_canonical_bootstrap(
        config.repository,
        repository_lock=repository_lock,
        run_id=run_id,
        configured_backends=configured_backends,
        canonical_root=canonical_root,
    )
    _recover_interrupted_runs(
        config,
        config.repository,
        current_run_id=run_id,
        bootstrap_cleanup=bootstrap_cleanup,
    )
    baseline = capture_git_baseline(config.repository)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = config.runs_dir / f"{timestamp}-{run_id[:8]}"
    run_dir.mkdir(parents=True, exist_ok=False)
    _atomic_write_text(run_dir / "task.md", task + "\n")
    baseline_path = run_dir / "initial_git_baseline.json"
    atomic_write_json(baseline_path, baseline)
    phase_provenance: list[dict] = []
    state_lock = threading.RLock()
    baseline_sha256 = hashlib.sha256(baseline_path.read_bytes()).hexdigest()
    initial_bootstrap_artifacts = canonical_bootstrap_artifacts(config.repository)
    initial_bootstrap_control_paths = canonical_bootstrap_control_paths(config.repository)
    run_state: dict = {
        "schema_version": 1,
        "run_id": run_id,
        "repository": str(config.repository),
        "pid": os.getpid(),
        "process_start": _run_owner_process_start(),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "status": "running",
        "current_phase": None,
        "last_phase": None,
        "current_actor": None,
        "current_backend": None,
        "last_actor": None,
        "last_backend": None,
        "provider_status": "not_started",
        "last_progress_at": None,
        "task_sha256": hashlib.sha256(task.encode("utf-8")).hexdigest(),
        "initial_git_baseline": {
            "path": baseline_path.name,
            "sha256": baseline_sha256,
            "head": baseline.get("head"),
            "branch": baseline.get("branch"),
            "detached": baseline.get("detached"),
            "captured_at": baseline.get("captured_at"),
        },
        "mutation_attribution": {"status": "pending", "path": "mutation-attribution.json"},
        "bootstrap_cleanup": bootstrap_cleanup,
        "dual_agents_bootstrap_artifacts": initial_bootstrap_artifacts,
        "verdict": None,
        "failure": None,
    }
    _persist_run_state(config, canonical_root, run_dir, phase_provenance, run_state, required=True)

    def mutation_summary() -> dict:
        final_bootstrap_control_paths = canonical_bootstrap_control_paths(config.repository)
        excluded = []
        run_relative = _relative_control_path(run_dir, config.repository)
        if run_relative:
            excluded.append(run_relative)
        excluded.extend(set(initial_bootstrap_control_paths) | set(final_bootstrap_control_paths))
        result = attribute_git_mutations(config.repository, baseline, excluded_paths=excluded)
        result["dual_agents_ephemeral_artifacts"] = {
            "initial": initial_bootstrap_control_paths,
            "final": final_bootstrap_control_paths,
            "reconciled_before_baseline": bootstrap_cleanup.get("removed", []),
        }
        return result

    try:
        if config.require_clean_git and baseline.get("status_entries"):
            run_state["status"] = "blocked"
            run_state["failure"] = {"failure_type": "DirtyRepository"}
            run_state["mutation_attribution"] = mutation_summary()
            atomic_write_json(run_dir / "mutation-attribution.json", run_state["mutation_attribution"])
            _persist_run_state(config, canonical_root, run_dir, phase_provenance, run_state)
            _progress_event(progress, phase="run", actor="", backend="", state="failed", failure_type="DirtyRepository")
            raise RuntimeError(
                "Repository has uncommitted changes. Commit/stash them or set "
                "require_clean_git = false explicitly."
            )

        plan_path = run_dir / "plan.json"
        _dispatch_phase(
            config=config,
            canonical_root=canonical_root,
            run_dir=run_dir,
            phase_provenance=phase_provenance,
            run_state=run_state,
            state_lock=state_lock,
            progress=progress,
            role="architect",
            repository_trust_authorized=repository_trust_authorized,
            task=_prompt(config, "architect.txt", task=task),
            repository=config.repository,
            output_path=plan_path,
            schema_path=_schema(config, "architect-plan.schema.json"),
        )
        plan = load_json(plan_path)

        implementation_path = run_dir / "implementation.json"
        _dispatch_phase(
            config=config,
            canonical_root=canonical_root,
            run_dir=run_dir,
            phase_provenance=phase_provenance,
            run_state=run_state,
            state_lock=state_lock,
            progress=progress,
            role="executor",
            repository_trust_authorized=repository_trust_authorized,
            task=_prompt(config, "executor.txt", task=task, plan=dump_json(plan)),
            repository=config.repository,
            output_path=implementation_path,
            schema_path=_schema(config, "implementation.schema.json"),
        )
        implementation = load_json(implementation_path)

        correction_cycles = 0
        while True:
            diff_text = status_and_diff(config.repository)
            (run_dir / f"diff-{correction_cycles}.md").write_text(diff_text, encoding="utf-8")
            review_path = run_dir / f"review-{correction_cycles}.json"
            _dispatch_phase(
                config=config,
                canonical_root=canonical_root,
                run_dir=run_dir,
                phase_provenance=phase_provenance,
                run_state=run_state,
                state_lock=state_lock,
                progress=progress,
                role="reviewer",
                repository_trust_authorized=repository_trust_authorized,
                task=_prompt(
                    config,
                    "reviewer.txt",
                    task=task,
                    plan=dump_json(plan),
                    implementation=dump_json(implementation),
                    diff=diff_text,
                    phase_provenance=dump_json(_reviewer_phase_context(config, phase_provenance)),
                ),
                repository=config.repository,
                output_path=review_path,
                schema_path=_schema(config, "review.schema.json"),
            )
            review = load_json(review_path)
            if review["verdict"] == "approved":
                break
            if correction_cycles >= config.max_correction_cycles:
                break

            correction_cycles += 1
            implementation_path = run_dir / f"correction-{correction_cycles}.json"
            _dispatch_phase(
                config=config,
                canonical_root=canonical_root,
                run_dir=run_dir,
                phase_provenance=phase_provenance,
                run_state=run_state,
                state_lock=state_lock,
                progress=progress,
                role="executor",
                repository_trust_authorized=repository_trust_authorized,
                task=_prompt(
                    config,
                    "correction.txt",
                    task=task,
                    plan=dump_json(plan),
                    review=dump_json(review),
                ),
                repository=config.repository,
                output_path=implementation_path,
                schema_path=_schema(config, "implementation.schema.json"),
            )
            implementation = load_json(implementation_path)

        mutation = mutation_summary()
        atomic_write_json(run_dir / "mutation-attribution.json", mutation)
        run_state["mutation_attribution"] = mutation
        run_state["status"] = "completed"
        run_state["provider_status"] = "completed"
        run_state["current_phase"] = None
        run_state["verdict"] = review["verdict"]
        run_state["completed_at"] = datetime.now(timezone.utc).isoformat()
        _persist_run_state(config, canonical_root, run_dir, phase_provenance, run_state)
        report = render_markdown(
            task_file=task_file,
            plan=plan,
            implementation=implementation,
            review=review,
            correction_cycles=correction_cycles,
            phase_provenance=phase_provenance,
            mutation_attribution=mutation,
            initial_git_baseline=run_state["initial_git_baseline"],
        )
        _atomic_write_text(run_dir / "REPORT.md", report)
        return RunOutcome(
            run_dir=run_dir,
            verdict=review["verdict"],
            correction_cycles=correction_cycles,
            phase_provenance=tuple(phase_provenance),
        )
    except BaseException as exc:
        if run_state.get("status") not in {"blocked", "completed"}:
            if isinstance(exc, KeyboardInterrupt):
                run_state["status"] = "interrupted"
                if run_state.get("current_phase"):
                    run_state["provider_status"] = "unknown_after_interruption"
            else:
                run_state["status"] = "failed"
                if run_state.get("current_phase"):
                    run_state["provider_status"] = "failed"
            run_state["failure"] = {
                "failure_type": "DirtyRepository" if "Repository has uncommitted changes." in str(exc) else type(exc).__name__,
                "failure_class": str(getattr(exc, "failure_class", "")),
                "failed_at": datetime.now(timezone.utc).isoformat(),
            }
            interrupted_phase = run_state.get("current_phase")
            if interrupted_phase:
                run_state["last_phase"] = interrupted_phase
                phase = next((item for item in reversed(phase_provenance) if item.get("role") == interrupted_phase), None)
                if phase is not None and phase.get("phase_state") in {"started", "running"}:
                    phase["phase_state"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else _phase_failure_state(exc)
                    phase["provider_status"] = "unknown_after_interruption" if isinstance(exc, KeyboardInterrupt) else "failed"
                    phase["completed_at"] = datetime.now(timezone.utc).isoformat()
                run_state["current_phase"] = None
                run_state["last_actor"] = run_state.get("current_actor") or run_state.get("last_actor")
                run_state["last_backend"] = run_state.get("current_backend") or run_state.get("last_backend")
                run_state["current_actor"] = None
                run_state["current_backend"] = None
            if run_state.get("status") == "blocked":
                mutation = run_state.get("mutation_attribution")
            else:
                try:
                    mutation = mutation_summary()
                    atomic_write_json(run_dir / "mutation-attribution.json", mutation)
                except Exception as attribution_error:
                    mutation = {"status": "unknown", "reason": type(attribution_error).__name__}
            run_state["mutation_attribution"] = mutation
            run_state["dual_agents_bootstrap_artifacts"] = canonical_bootstrap_artifacts(config.repository)
            try:
                _persist_run_state(config, canonical_root, run_dir, phase_provenance, run_state)
            except Exception:
                pass
            if interrupted_phase and isinstance(exc, KeyboardInterrupt):
                phase = next((item for item in reversed(phase_provenance) if item.get("role") == interrupted_phase), {})
                _progress_event(
                    progress,
                    phase=interrupted_phase,
                    actor=phase.get("actor_id", "unknown"),
                    backend=phase.get("backend", "unknown"),
                    state="interrupted",
                    failure_type="KeyboardInterrupt",
                )
        raise
