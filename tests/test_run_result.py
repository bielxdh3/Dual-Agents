from __future__ import annotations

from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from dual_codex.cli import main
from dual_codex.codex import ActorAvailabilityError
from dual_codex.config import AccountConfig, OrchestratorConfig
from dual_codex.process import CommandResult


def _fixture(root: Path, *, require_clean_git: bool = True) -> tuple[OrchestratorConfig, Path, Path, Path]:
    repository = root / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.name", "Run Result Test"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.email", "run-result@example.invalid"], cwd=repository, check=True)
    tracked = repository / "tracked.txt"
    tracked.write_text("committed\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=repository, check=True)

    instructions = root / "instructions"
    instructions.mkdir()
    (instructions / "AGENTS.md").write_text("Test host policy.\n", encoding="utf-8")
    accounts = {
        name: AccountConfig(
            name=name,
            label=name,
            codex_home=root / f"{name}-home",
            model="",
            reasoning_effort="high",
            backend="app_server",
        )
        for name in ("architect", "executor", "reviewer")
    }
    config = OrchestratorConfig(
        repository=repository,
        runs_dir=root / "runs",
        max_correction_cycles=0,
        require_clean_git=require_clean_git,
        codex_command="codex",
        accounts=accounts,
        roles={
            "orchestrator": "architect",
            "architect": "architect",
            "executor": "executor",
            "reviewer": "reviewer",
        },
        project_root=Path(__file__).resolve().parents[1],
        config_path=root / "config.toml",
    )
    task = root / "mission.md"
    task.write_text("PRIVATE_PROMPT_MUST_NOT_LEAK", encoding="utf-8")
    return config, repository, instructions, task


def _run_cli(config, repository: Path, instructions: Path, task: Path, actor, *, extra_patches=()):
    stdout = io.StringIO()
    stderr = io.StringIO()
    patches = [
        patch("dual_codex.cli.load_config", return_value=config),
        patch("dual_codex.orchestrator.canonical_instructions_root", return_value=instructions),
        patch("dual_codex.orchestrator.configured_actor_provenance", side_effect=lambda **kwargs: {
            "phase": kwargs["role"],
            "configured_actor": kwargs["agent"].account_name,
            "backend": kwargs["agent"].backend,
        }),
        patch("dual_codex.orchestrator.delegate_to_configured_actor", side_effect=actor),
        *extra_patches,
    ]
    with redirect_stdout(stdout), redirect_stderr(stderr), ExitStack() as stack:
        for context in patches:
            stack.enter_context(context)
        exit_code = main([
            "--config", str(config.config_path), "run", "--repository", str(repository), str(task)
        ])
    lines = [
        line.removeprefix("DUAL_CODEX_RUN_RESULT ")
        for line in stdout.getvalue().splitlines()
        if line.startswith("DUAL_CODEX_RUN_RESULT ")
    ]
    if len(lines) != 1:
        raise AssertionError(f"expected one structured run result, got {len(lines)}")
    return exit_code, json.loads(lines[0]), stdout.getvalue(), stderr.getvalue()


def _fake_actor(*, fail_role: str | None = None, interrupt: bool = False):
    def actor(**kwargs):
        role = kwargs["role"]
        account = kwargs["config"].agent_for_role(role).account_name
        if role == fail_role:
            if interrupt:
                raise KeyboardInterrupt()
            raise ActorAvailabilityError(
                f"Timed out waiting for simulated {role} turn.",
                failure_class="transport_unavailable",
                actor=account,
            )
        payloads = {
            "architect": {"summary": "plan", "steps": [], "acceptance_criteria": [], "risks": [], "files_to_inspect": [], "skills_loaded": []},
            "executor": {"summary": "implementation", "files_changed": [], "commands_run": [], "tests": [], "remaining_issues": []},
            "reviewer": {"verdict": "approved", "summary": "approved", "findings": []},
        }
        kwargs["output_path"].write_text(json.dumps(payloads[role]), encoding="utf-8")
        return CommandResult(
            ["fake"],
            0,
            "",
            "",
            {
                "phase": role,
                "role": role,
                "actor_id": account,
                "actual_actor": account,
                "provider": "codex",
                "backend": "app_server",
                "fallback_used": False,
            },
        )

    return actor


class StructuredRunResultTests(unittest.TestCase):
    def test_executor_failure_includes_safe_app_server_turn_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, repository, instructions, task = _fixture(root)
            turn_provenance = {
                "turn_timeout_seconds": 600.0,
                "timeout_source": "global_default",
                "turn_start_monotonic": 100.5,
                "turn_start_timestamp": "2026-01-01T00:00:00+00:00",
                "last_provider_event_timestamp": "2026-01-01T00:09:59+00:00",
                "termination_classification": "HOST_TURN_DEADLINE",
                "host_deadline_expired": True,
                "app_server_process_alive_at_failure": True,
                "app_server_process_exit_code": None,
                "thread_resumed": False,
                "thread_state": "fresh",
                "turn_id": "turn-safe-id",
                "last_safe_provider_notification_method": "item/started",
                "failure_reason": "Timed out waiting for turn/completed (turn-safe-id).",
            }

            def actor(**kwargs):
                role = kwargs["role"]
                account = kwargs["config"].agent_for_role(role).account_name
                if role == "executor":
                    raise ActorAvailabilityError(
                        "Timed out waiting for turn/completed (turn-safe-id).",
                        failure_class="HOST_TURN_DEADLINE",
                        actor=account,
                        metadata={"app_server_turn_provenance": turn_provenance},
                    )
                payloads = {
                    "architect": {"summary": "plan", "steps": [], "acceptance_criteria": [], "risks": [], "files_to_inspect": [], "skills_loaded": []},
                    "reviewer": {"verdict": "approved", "summary": "approved", "findings": []},
                }
                kwargs["output_path"].write_text(json.dumps(payloads[role]), encoding="utf-8")
                return CommandResult(["fake"], 0, "", "", {"phase": role, "role": role, "actor_id": account, "backend": "app_server"})

            exit_code, result, output, _ = _run_cli(config, repository, instructions, task, actor)
            run_dir = Path(result["run_directory"])
            run_state = json.loads((run_dir / "run_state.json").read_text(encoding="utf-8"))
            provenance = json.loads((run_dir / "provenance.json").read_text(encoding="utf-8"))

            self.assertEqual(exit_code, 1)
            self.assertEqual(result["failure_class"], "HOST_TURN_DEADLINE")
            self.assertEqual(result["app_server_turn_provenance"], turn_provenance)
            self.assertEqual(run_state["failure"]["app_server_turn_provenance"], turn_provenance)
            self.assertEqual(run_state["app_server_turn_provenance"], [turn_provenance])
            self.assertEqual(provenance["configured_actor_routing"][1]["app_server_turn_provenance"], turn_provenance)
            result_line = next(line for line in output.splitlines() if line.startswith("DUAL_CODEX_RUN_RESULT "))
            self.assertNotIn("PRIVATE_PROMPT_MUST_NOT_LEAK", result_line)
            self.assertNotIn("PRIVATE_REASONING", result_line)

    def test_architect_failure_reports_exact_run_and_partial_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, repository, instructions, task = _fixture(root)
            exit_code, result, output, _ = _run_cli(
                config, repository, instructions, task, _fake_actor(fail_role="architect")
            )

            run_dir = Path(result["run_directory"])
            self.assertEqual(exit_code, 1)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["last_phase"], "architect")
            self.assertEqual(result["last_actor"], "architect")
            self.assertEqual(result["last_backend"], "app_server")
            self.assertEqual(result["failure_type"], "ActorAvailabilityError")
            self.assertEqual(result["failure_class"], "transport_unavailable")
            self.assertTrue(run_dir.is_dir())
            self.assertTrue((run_dir / "initial_git_baseline.json").is_file())
            self.assertTrue((run_dir / "run_state.json").is_file())
            self.assertTrue((run_dir / "provenance.json").is_file())
            self.assertEqual(result["initial_git_baseline_path"], str(run_dir / "initial_git_baseline.json"))
            self.assertNotIn("PRIVATE_PROMPT_MUST_NOT_LEAK", next(
                line for line in output.splitlines() if line.startswith("DUAL_CODEX_RUN_RESULT ")
            ))

    def test_reviewer_failure_keeps_prior_phase_evidence_without_final_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, repository, instructions, task = _fixture(root)
            exit_code, result, _, _ = _run_cli(
                config, repository, instructions, task, _fake_actor(fail_role="reviewer")
            )

            run_dir = Path(result["run_directory"])
            self.assertEqual(exit_code, 1)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["last_phase"], "reviewer")
            self.assertEqual(result["last_actor"], "reviewer")
            self.assertEqual(result["failure_type"], "ActorAvailabilityError")
            self.assertEqual(result["failure_class"], "transport_unavailable")
            self.assertTrue((run_dir / "plan.json").is_file())
            self.assertTrue((run_dir / "implementation.json").is_file())
            self.assertTrue((run_dir / "initial_git_baseline.json").is_file())
            self.assertTrue((run_dir / "run_state.json").is_file())
            self.assertTrue((run_dir / "provenance.json").is_file())
            self.assertTrue((run_dir / "mutation-attribution.json").is_file())
            self.assertEqual(result["mutation_attribution_path"], str(run_dir / "mutation-attribution.json"))
            self.assertFalse((run_dir / "REPORT.md").exists())

    def test_dirty_repository_blocking_emits_evidence_and_keeps_nonzero_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, repository, instructions, task = _fixture(root, require_clean_git=True)
            (repository / "uncommitted.txt").write_text("dirty\n", encoding="utf-8")
            calls = []

            def unexpected_actor(**kwargs):
                calls.append(kwargs["role"])
                raise AssertionError("blocked run must not start an actor")

            exit_code, result, _, _ = _run_cli(config, repository, instructions, task, unexpected_actor)

            run_dir = Path(result["run_directory"])
            self.assertEqual(exit_code, 1)
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["failure_type"], "DirtyRepository")
            self.assertIsNone(result["last_phase"])
            self.assertEqual(calls, [])
            for name in ("initial_git_baseline.json", "run_state.json", "provenance.json", "mutation-attribution.json"):
                self.assertTrue((run_dir / name).is_file())
            state = json.loads((run_dir / "run_state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "blocked")

    def test_mutation_attribution_failure_is_reported_without_inventing_a_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, repository, instructions, task = _fixture(root, require_clean_git=False)
            failure = patch(
                "dual_codex.orchestrator.attribute_git_mutations",
                side_effect=PermissionError("simulated attribution denial"),
            )
            exit_code, result, _, _ = _run_cli(
                config,
                repository,
                instructions,
                task,
                _fake_actor(fail_role="architect"),
                extra_patches=(failure,),
            )

            run_dir = Path(result["run_directory"])
            self.assertEqual(exit_code, 1)
            self.assertEqual(result["mutation_attribution_status"], "unknown")
            self.assertEqual(result["mutation_attribution_path"], None)
            self.assertIn("PermissionError: simulated attribution denial", result["mutation_attribution_reason"])
            state = json.loads((run_dir / "run_state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["mutation_attribution"]["status"], "unknown")
            self.assertIn("simulated attribution denial", state["mutation_attribution"]["reason"])

    def test_interrupt_emits_interrupted_result_and_original_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, repository, instructions, task = _fixture(root)
            exit_code, result, _, _ = _run_cli(
                config, repository, instructions, task, _fake_actor(fail_role="architect", interrupt=True)
            )

            self.assertEqual(exit_code, 130)
            self.assertEqual(result["status"], "interrupted")
            self.assertEqual(result["failure_type"], "KeyboardInterrupt")
            self.assertEqual(result["last_phase"], "architect")
            self.assertTrue(Path(result["run_state_path"]).is_file())


if __name__ == "__main__":
    unittest.main()
