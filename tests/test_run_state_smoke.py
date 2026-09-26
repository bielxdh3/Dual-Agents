from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from dual_codex.bootstrap import create_canonical_bootstrap
from dual_codex.cli import main
from dual_codex.config import AccountConfig, OrchestratorConfig
from dual_codex.process import CommandResult
from dual_codex.report import atomic_write_json


class OfficialRunStateSmokeTests(unittest.TestCase):
    def test_cli_run_progress_failure_recovery_and_mutation_attribution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "disposable-repository"
            repository.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.name", "Smoke Test"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.email", "smoke@example.invalid"], cwd=repository, check=True)
            tracked = repository / "tracked.txt"
            tracked.write_text("committed\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-qm", "initial"], cwd=repository, check=True)
            tracked.write_text("pre-existing dirty file\n", encoding="utf-8")

            bootstrap_dir = repository / ".dual_codex" / "bootstrap"
            bootstrap_dir.mkdir(parents=True)
            user_file = bootstrap_dir / "notes.md"
            user_file.write_text("user-owned bootstrap directory note\n", encoding="utf-8")
            wrong_name = bootstrap_dir / ".canonical-bootstrap-executor-not-random.md"
            wrong_name.write_text("not a generated artifact\n", encoding="utf-8")
            outside_dir = repository / ".dual_codex" / "other"
            outside_dir.mkdir()
            outside_artifact = outside_dir / ".canonical-bootstrap-executor-ABCDEFGH.md"
            outside_artifact.write_text("outside exact bootstrap root\n", encoding="utf-8")
            unrelated = repository / "unrelated-user-file.txt"
            unrelated.write_text("keep this file\n", encoding="utf-8")

            instructions = root / "CodexGlobal"
            instructions.mkdir()
            (instructions / "AGENTS.md").write_text("Host policy.\n", encoding="utf-8")
            for skill in ("memory", "ponytail", "project-phase-review", "project-security-review"):
                skill_path = instructions / "skills" / skill / "SKILL.md"
                skill_path.parent.mkdir(parents=True, exist_ok=True)
                skill_path.write_text(f"# {skill}\n", encoding="utf-8")

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
                require_clean_git=False,
                codex_command="codex",
                accounts=accounts,
                roles={"architect": "architect", "executor": "executor", "reviewer": "reviewer", "orchestrator": "architect"},
                project_root=Path.cwd(),
                config_path=root / "config.toml",
            )
            task = root / "mission.md"
            task.write_text("Bounded fake provider mission.", encoding="utf-8")
            mode = {"fail_executor": True}

            def fake_provider(**kwargs):
                role = kwargs["role"]
                if role == "architect":
                    payload = {"summary": "plan", "steps": [], "acceptance_criteria": [], "risks": [], "files_to_inspect": [], "skills_loaded": []}
                elif role == "executor":
                    if mode["fail_executor"]:
                        (repository / "created-by-run.txt").write_text("run-created file\n", encoding="utf-8")
                        kwargs["progress"]("app-server turn smoke-turn still running")
                        raise TimeoutError("simulated provider interruption")
                    payload = {"summary": "implementation", "files_changed": [], "commands_run": [], "tests": [], "remaining_issues": []}
                else:
                    payload = {"verdict": "approved", "summary": "approved", "findings": []}
                kwargs["output_path"].write_text(json.dumps(payload), encoding="utf-8")
                agent = kwargs["agent"]
                return CommandResult(
                    ["fake"], 0, "", "", {
                        "phase": role,
                        "role": role,
                        "actor_id": agent.account_name,
                        "actual_actor": agent.account_name,
                        "provider": agent.provider_type,
                        "backend": agent.backend,
                        "repository": str(repository.resolve()),
                        "fallback_used": False,
                    },
                )

            argv = [
                "--config", str(config.config_path), "run", "--repository", str(repository), str(task),
            ]
            output = io.StringIO()
            error = io.StringIO()
            with patch("dual_codex.cli.load_config", return_value=config), patch(
                "dual_codex.bootstrap.CANONICAL_INSTRUCTIONS_ROOT", instructions
            ), patch("dual_codex.repo_trust.provision_repository_trust", return_value=False), patch(
                "dual_codex.orchestrator.run_codex_for_role", side_effect=fake_provider
            ), redirect_stdout(output), redirect_stderr(error):
                first_exit = main(argv)

            self.assertEqual(first_exit, 1)
            self.assertIn('"phase":"executor","actor":"executor","backend":"app_server","state":"running"', output.getvalue())
            first_run = next(path for path in config.runs_dir.iterdir() if (path / "run_state.json").is_file())
            first_baseline = json.loads((first_run / "initial_git_baseline.json").read_text(encoding="utf-8"))
            first_mutation = json.loads((first_run / "mutation-attribution.json").read_text(encoding="utf-8"))
            self.assertIn("tracked.txt", [entry["path"] for entry in first_baseline["status_entries"]])
            preexisting_paths = sorted(entry["path"] for entry in first_baseline["status_entries"])
            self.assertEqual(first_mutation["unchanged_preexisting_paths"], preexisting_paths)
            self.assertEqual(first_mutation["run_created_paths"], ["created-by-run.txt"])

            old_state = json.loads((first_run / "run_state.json").read_text(encoding="utf-8"))
            old_state.update(
                {
                    "status": "running",
                    "current_phase": "executor",
                    "current_actor": "executor",
                    "current_backend": "app_server",
                    "provider_status": "alive",
                    "failure": None,
                }
            )
            atomic_write_json(first_run / "run_state.json", old_state)
            old_provenance = json.loads((first_run / "provenance.json").read_text(encoding="utf-8"))
            old_provenance.update({"status": "running", "current_phase": "executor", "provider_status": "alive"})
            old_provenance["configured_actor_routing"][-1].update({"phase_state": "running", "provider_status": "alive"})
            atomic_write_json(first_run / "provenance.json", old_provenance)

            stale = create_canonical_bootstrap(
                role="executor",
                artifact_dir=bootstrap_dir,
                root=instructions,
                artifact_repository=repository,
                run_id=old_state["run_id"],
            )
            stale_owner = json.loads(stale.artifact_owner_path.read_text(encoding="utf-8"))
            stale_owner["pid"] = 2147483000
            stale_owner["process_start"] = "dead-process"
            atomic_write_json(stale.artifact_owner_path, stale_owner)

            mode["fail_executor"] = False
            output.seek(0)
            output.truncate(0)
            with patch("dual_codex.cli.load_config", return_value=config), patch(
                "dual_codex.bootstrap.CANONICAL_INSTRUCTIONS_ROOT", instructions
            ), patch("dual_codex.repo_trust.provision_repository_trust", return_value=False), patch(
                "dual_codex.orchestrator.run_codex_for_role", side_effect=fake_provider
            ), redirect_stdout(output), redirect_stderr(error):
                second_exit = main(argv)

            self.assertEqual(second_exit, 0)
            self.assertFalse(stale.artifact_path.exists())
            self.assertFalse(stale.artifact_owner_path.exists())
            self.assertEqual(user_file.read_text(encoding="utf-8"), "user-owned bootstrap directory note\n")
            self.assertTrue(wrong_name.exists())
            self.assertTrue(outside_artifact.exists())
            self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep this file\n")

            recovered = json.loads((first_run / "run_state.json").read_text(encoding="utf-8"))
            recovered_mutation = json.loads((first_run / "mutation-attribution.json").read_text(encoding="utf-8"))
            self.assertEqual(recovered["status"], "interrupted")
            self.assertEqual(recovered_mutation["unchanged_preexisting_paths"], preexisting_paths)
            self.assertEqual(recovered_mutation["run_created_paths"], ["created-by-run.txt"])


if __name__ == "__main__":
    unittest.main()
