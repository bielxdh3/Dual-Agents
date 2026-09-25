from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .bootstrap import canonical_instructions_root
from .codex import _delegate_to_configured_actor
from .codex import configured_actor_provenance, run_codex_for_role
from .config import ConfigError, OrchestratorConfig
from .delegation import RepositoryLock
from .git import ensure_git_repository, status_and_diff, status_porcelain
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


def execute(config: OrchestratorConfig, task_file: Path) -> RunOutcome:
    lock = RepositoryLock(config.runs_dir, config.repository, "run-" + uuid4().hex)
    with lock:
        return _execute_locked(config, task_file)


def _execute_locked(config: OrchestratorConfig, task_file: Path) -> RunOutcome:
    task_file = task_file.expanduser().resolve()
    task = _read(task_file).strip()
    if not task:
        raise ValueError("Task file is empty")

    canonical_root = canonical_instructions_root()
    ensure_git_repository(config.repository)
    from .terminal import TerminalManager

    TerminalManager(config).reconcile_pending_task_artifact_cleanup(config.repository)
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
    result = delegate_to_configured_actor(
        config=config,
        role="architect",
        task=_prompt(config, "architect.txt", task=task),
        repository=config.repository,
        output_path=plan_path,
        schema_path=_schema(config, "architect-plan.schema.json"),
    )
    phase_provenance.append(dict(result.metadata))
    plan = load_json(plan_path)

    implementation_path = run_dir / "implementation.json"
    result = delegate_to_configured_actor(
        config=config,
        role="executor",
        task=_prompt(config, "executor.txt", task=task, plan=dump_json(plan)),
        repository=config.repository,
        output_path=implementation_path,
        schema_path=_schema(config, "implementation.schema.json"),
    )
    phase_provenance.append(dict(result.metadata))
    implementation = load_json(implementation_path)

    correction_cycles = 0
    while True:
        diff_text = status_and_diff(config.repository)
        (run_dir / f"diff-{correction_cycles}.md").write_text(diff_text, encoding="utf-8")
        review_path = run_dir / f"review-{correction_cycles}.json"
        result = delegate_to_configured_actor(
            config=config,
            role="reviewer",
            task=_prompt(
                config,
                "reviewer.txt",
                task=task,
                plan=dump_json(plan),
                implementation=dump_json(implementation),
                diff=diff_text,
            ),
            repository=config.repository,
            output_path=review_path,
            schema_path=_schema(config, "review.schema.json"),
        )
        phase_provenance.append(dict(result.metadata))
        review = load_json(review_path)
        if review["verdict"] == "approved":
            break
        if correction_cycles >= config.max_correction_cycles:
            break

        correction_cycles += 1
        implementation_path = run_dir / f"correction-{correction_cycles}.json"
        result = delegate_to_configured_actor(
            config=config,
            role="executor",
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
        phase_provenance.append(dict(result.metadata))
        implementation = load_json(implementation_path)

    try:
        orchestrator_metadata = configured_actor_provenance(
            agent=config.agent_for_role("orchestrator"),
            role="orchestrator",
            repository=config.repository,
            canonical_root=canonical_root,
        )
    except ConfigError as exc:
        # Older configs may omit the informational orchestrator assignment;
        # retain an explicit fail-closed status in the provenance artifact.
        orchestrator_metadata = {
            "phase": "orchestrator",
            "role": "orchestrator",
            "configured_actor": False,
            "routing_error": str(exc),
            "fallback_used": False,
        }
    provenance = {
        "schema_version": 1,
        "configured_actor_routing": phase_provenance,
        "orchestrator": orchestrator_metadata,
    }
    atomic_write_json(run_dir / "provenance.json", provenance)
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
