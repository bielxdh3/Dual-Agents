from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .bootstrap import canonical_instructions_root
from .codex import _delegate_to_configured_actor
from .codex import configured_actor_provenance, run_codex_for_role
from .config import ConfigError, OrchestratorConfig
from .delegation import RepositoryLock
from .git import ensure_git_repository, git_top_level, status_and_diff, status_porcelain
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
    atomic_write_json(
        run_dir / "provenance.json",
        {
            "schema_version": 1,
            "configured_actor_routing": phase_provenance,
            "orchestrator": orchestrator_metadata,
        },
    )


def _dispatch_phase(
    *,
    config: OrchestratorConfig,
    canonical_root: Path,
    run_dir: Path,
    phase_provenance: list[dict],
    role: str,
    repository_trust_authorized: bool = False,
    **kwargs,
):
    try:
        result = delegate_to_configured_actor(
            config=config,
            role=role,
            repository_trust_authorized=repository_trust_authorized,
            **kwargs,
        )
    except Exception as exc:
        try:
            actor = config.agent_for_role(role)
            failure = configured_actor_provenance(
                agent=actor,
                role=role,
                repository=config.repository,
                canonical_root=canonical_root,
            )
        except ConfigError as config_error:
            failure = {
                "phase": role,
                "role": role,
                "configured_actor": False,
                "routing_error": str(config_error),
                "fallback_used": False,
            }
        failure.update(getattr(exc, "metadata", {}))
        failure.update(
            {
                "phase": role,
                "role": role,
                "dispatch_failed": True,
                "failure_type": type(exc).__name__,
            }
        )
        if getattr(exc, "failure_class", None):
            failure["failure_class"] = exc.failure_class
        phase_provenance.append(failure)
        _write_provenance(config, canonical_root, run_dir, phase_provenance)
        raise
    phase_provenance.append(dict(result.metadata))
    return result


def execute(
    config: OrchestratorConfig,
    task_file: Path,
    *,
    explicit_repository: bool = False,
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
    lock = RepositoryLock(config.runs_dir, config.repository, "run-" + uuid4().hex)
    with lock:
        return _execute_locked(config, task_file, repository_trust_authorized=explicit_repository)


def _execute_locked(
    config: OrchestratorConfig,
    task_file: Path,
    *,
    repository_trust_authorized: bool = False,
) -> RunOutcome:
    task_file = task_file.expanduser().resolve()
    task = _read(task_file).strip()
    if not task:
        raise ValueError("Task file is empty")

    canonical_root = canonical_instructions_root()
    ensure_git_repository(config.repository)
    from .terminal import reconcile_deferred_task_artifact_cleanup

    reconcile_deferred_task_artifact_cleanup(config, config.repository)
    if config.require_clean_git and status_porcelain(config.repository).strip():
        raise RuntimeError(
            "Repository has uncommitted changes. Commit/stash them or set "
            "require_clean_git = false explicitly."
        )

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = config.runs_dir / timestamp
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "task.md").write_text(task + "\n", encoding="utf-8")
    phase_provenance: list[dict] = []

    plan_path = run_dir / "plan.json"
    result = _dispatch_phase(
        config=config,
        canonical_root=canonical_root,
        run_dir=run_dir,
        phase_provenance=phase_provenance,
        role="architect",
        repository_trust_authorized=repository_trust_authorized,
        task=_prompt(config, "architect.txt", task=task),
        repository=config.repository,
        output_path=plan_path,
        schema_path=_schema(config, "architect-plan.schema.json"),
    )
    plan = load_json(plan_path)

    implementation_path = run_dir / "implementation.json"
    result = _dispatch_phase(
        config=config,
        canonical_root=canonical_root,
        run_dir=run_dir,
        phase_provenance=phase_provenance,
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
        result = _dispatch_phase(
            config=config,
            canonical_root=canonical_root,
            run_dir=run_dir,
            phase_provenance=phase_provenance,
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
        result = _dispatch_phase(
            config=config,
            canonical_root=canonical_root,
            run_dir=run_dir,
            phase_provenance=phase_provenance,
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

    _write_provenance(config, canonical_root, run_dir, phase_provenance)
    report = render_markdown(
        task_file=task_file,
        plan=plan,
        implementation=implementation,
        review=review,
        correction_cycles=correction_cycles,
        phase_provenance=phase_provenance,
    )
    (run_dir / "REPORT.md").write_text(report, encoding="utf-8")
    return RunOutcome(
        run_dir=run_dir,
        verdict=review["verdict"],
        correction_cycles=correction_cycles,
        phase_provenance=tuple(phase_provenance),
    )
