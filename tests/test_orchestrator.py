from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest
from dataclasses import replace
import json
from unittest.mock import patch

from dual_codex.codex import ActorResultError
from dual_codex.config import AccountConfig, AgentConfig, OrchestratorConfig
from dual_codex.delegation import DelegationError, RepositoryLock
from dual_codex.orchestrator import execute
from dual_codex.process import CommandError, CommandResult
from dual_codex.security_scan import PLUGIN_ID, SecurityScanError, arbitrate_security_scans, stable_target_id


class OrchestratorTests(unittest.TestCase):
    def setUp(self) -> None:
        # Hosted Windows runners do not provide the developer machine's
        # machine-wide policy tree.  Keep production resolution strict while
        # giving this orchestration test an explicit, isolated policy fixture.
        self._instructions = tempfile.TemporaryDirectory()
        self.addCleanup(self._instructions.cleanup)
        instruction_root = Path(self._instructions.name)
        (instruction_root / "AGENTS.md").write_text(
            "# canonical test policy\n",
            encoding="utf-8",
        )
        skills_root = instruction_root / "skills"
        skills_root.mkdir()
        for name in ("memory", "ponytail", "project-phase-review", "project-security-review"):
            skill_root = skills_root / name
            skill_root.mkdir()
            (skill_root / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
        self._bootstrap_patch = patch(
            "dual_codex.bootstrap.CANONICAL_INSTRUCTIONS_ROOT",
            instruction_root,
        )
        self._bootstrap_patch.start()
        self.addCleanup(self._bootstrap_patch.stop)

    def test_run_and_delegate_share_repository_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "disposable-mission"
            repository.mkdir()
            subprocess.run(["git", "init", "--quiet"], cwd=repository, check=True)
            account = AccountConfig(
                name="biel3",
                label="Architect",
                codex_home=root / "architect-home",
                model="",
                reasoning_effort="high",
                backend="windows",
            )
            config = OrchestratorConfig(
                repository=repository,
                runs_dir=root / "runs",
                max_correction_cycles=0,
                require_clean_git=False,
                codex_command="codex",
                accounts={"biel3": account},
                roles={"architect": "biel3"},
                project_root=Path.cwd(),
                config_path=root / "config.toml",
            )
            task = root / "brief.md"
            task.write_text("This must never dispatch while delegate owns the lock.", encoding="utf-8")
            delegate_lock = RepositoryLock(config.runs_dir, repository, "active-delegate")
            delegate_lock.acquire()
            try:
                with self.assertRaisesRegex(DelegationError, "already delegated or in use"):
                    execute(config, task)
            finally:
                delegate_lock.release()

    def test_mission_dispatches_architect_and_app_server_executor_by_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "disposable-mission"
            repository.mkdir()
            subprocess.run(["git", "init", "--quiet"], cwd=repository, check=True)
            accounts = {
                "biel3": AccountConfig(
                    name="biel3",
                    label="Architect",
                    codex_home=root / "architect-home",
                    model="",
                    reasoning_effort="high",
                    backend="windows",
                ),
                "biel4": AccountConfig(
                    name="biel4",
                    label="Executor",
                    codex_home=root / "executor-home",
                    model="",
                    reasoning_effort="high",
                    backend="app_server",
                    network_access=True,
                ),
            }
            config = OrchestratorConfig(
                repository=repository,
                runs_dir=root / "runs",
                max_correction_cycles=0,
                require_clean_git=True,
                codex_command="codex",
                accounts=accounts,
                roles={
                    "architect": "biel3",
                    "executor": "biel4",
                    "reviewer": "biel3",
                },
                project_root=Path.cwd(),
                config_path=root / "config.toml",
            )
            task = root / "brief.md"
            task.write_text("Read this harmless mission brief.", encoding="utf-8")
            seen: list[tuple[str, str, str]] = []

            def fake_runner(**kwargs):
                role = kwargs["role"]
                agent: AgentConfig = kwargs["agent"]
                seen.append((role, agent.account_name, agent.backend))
                if role == "executor":
                    kwargs["dispatch_started"](
                        {
                            "turn_timeout_seconds": 3600,
                            "timeout_source": "account_role_override",
                            "runtime_config_identity": "a" * 64,
                            "process_reuse_state": "new_process",
                            "reused_process_runtime_identity_matched": None,
                        }
                    )
                self.assertIn("harmless mission brief", kwargs["prompt"])
                if role == "reviewer":
                    self.assertIn("CONTROL-PLANE VERIFIED PHASE PROVENANCE", kwargs["prompt"])
                    self.assertIn('"actual_actor": "biel3"', kwargs["prompt"])
                    self.assertIn('"actual_actor": "biel4"', kwargs["prompt"])
                    self.assertIn('"configured_actor": "biel3"', kwargs["prompt"])
                if role == "architect":
                    payload = {
                        "summary": "plan",
                        "steps": [],
                        "acceptance_criteria": [],
                        "risks": [],
                        "files_to_inspect": [],
                        "skills_loaded": [],
                    }
                elif role == "executor":
                    payload = {
                        "summary": "implementation",
                        "files_changed": [],
                        "commands_run": [],
                        "tests": [],
                        "remaining_issues": [],
                    }
                else:
                    payload = {"verdict": "approved", "summary": "approved", "findings": []}
                import json

                kwargs["output_path"].write_text(json.dumps(payload), encoding="utf-8")
                return CommandResult(
                    ["codex"],
                    0,
                    "",
                    "",
                    {
                        "role": role,
                        "primary_actor": agent.account_name,
                        "actual_actor": agent.account_name,
                        "provider": agent.provider_type,
                        "backend": agent.backend,
                        "fallback_enabled": False,
                        "fallback_used": False,
                        "repository": str(repository.resolve()),
                    },
                )

            with patch("dual_codex.orchestrator.run_codex_for_role", side_effect=fake_runner):
                outcome = execute(config, task)

            state = json.loads((outcome.run_dir / "run_state.json").read_text(encoding="utf-8"))
            provenance = json.loads((outcome.run_dir / "provenance.json").read_text(encoding="utf-8"))
            dispatch = state["app_server_dispatch_provenance"][0]
            self.assertEqual(dispatch["turn_timeout_seconds"], 3600)
            self.assertEqual(dispatch["timeout_source"], "account_role_override")
            self.assertEqual(dispatch["process_reuse_state"], "new_process")
            executor_phase = next(
                item for item in provenance["configured_actor_routing"] if item.get("role") == "executor"
            )
            self.assertEqual(
                executor_phase["app_server_dispatch_provenance"],
                {key: dispatch[key] for key in (
                    "turn_timeout_seconds",
                    "timeout_source",
                    "runtime_config_identity",
                    "process_reuse_state",
                    "reused_process_runtime_identity_matched",
                )},
            )

            self.assertEqual(
                seen,
                [
                    ("architect", "biel3", "windows"),
                    ("executor", "biel4", "app_server"),
                    ("reviewer", "biel3", "windows"),
                ],
            )

    def test_failed_reviewer_dispatch_persists_prior_phase_and_failure_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "disposable-mission"
            repository.mkdir()
            subprocess.run(["git", "init", "--quiet"], cwd=repository, check=True)
            accounts = {
                "architect": AccountConfig(
                    name="architect",
                    label="Architect",
                    codex_home=root / "architect-home",
                    model="",
                    reasoning_effort="high",
                    backend="windows",
                ),
                "executor": AccountConfig(
                    name="executor",
                    label="Executor",
                    codex_home=root / "executor-home",
                    model="",
                    reasoning_effort="high",
                    backend="app_server",
                ),
                "reviewer": AccountConfig(
                    name="reviewer",
                    label="Reviewer",
                    codex_home=root / "reviewer-home",
                    model="sonnet",
                    reasoning_effort="high",
                    backend="claude_code",
                    provider_type="anthropic",
                    adapter_type="claude_code",
                ),
            }
            config = OrchestratorConfig(
                repository=repository,
                runs_dir=root / "runs",
                max_correction_cycles=0,
                require_clean_git=True,
                codex_command="codex",
                accounts=accounts,
                roles={
                    "architect": "architect",
                    "executor": "executor",
                    "reviewer": "reviewer",
                    "orchestrator": "architect",
                },
                project_root=Path.cwd(),
                config_path=root / "config.toml",
            )
            task = root / "brief.md"
            task.write_text("Make a small documentation edit.", encoding="utf-8")

            def fail_reviewer(**kwargs):
                payload = (
                    {
                        "summary": "plan",
                        "steps": [],
                        "acceptance_criteria": [],
                        "risks": [],
                        "files_to_inspect": [],
                        "skills_loaded": [],
                    }
                    if kwargs["role"] == "architect"
                    else {
                        "summary": "implemented",
                        "files_changed": [],
                        "commands_run": [],
                        "tests": [],
                        "remaining_issues": [],
                    }
                )
                if kwargs["role"] == "reviewer":
                    raise CommandError("Configured reviewer runtime unavailable")
                kwargs["output_path"].write_text(json.dumps(payload), encoding="utf-8")
                agent = config.agent_for_role(kwargs["role"])
                return CommandResult(
                    ["fake"],
                    0,
                    "",
                    "",
                    {
                        "phase": kwargs["role"],
                        "role": kwargs["role"],
                        "actor_id": agent.account_name,
                        "actual_actor": agent.account_name,
                        "provider": agent.provider_type,
                        "backend": agent.backend,
                        "repository": str(repository.resolve()),
                        "fallback_used": False,
                    },
                )

            with patch("dual_codex.orchestrator.delegate_to_configured_actor", side_effect=fail_reviewer):
                with self.assertRaisesRegex(CommandError, "runtime unavailable"):
                    execute(config, task)

            run_dirs = [
                path
                for path in config.runs_dir.iterdir()
                if (path / "provenance.json").is_file()
            ]
            self.assertEqual(len(run_dirs), 1)
            provenance = json.loads((run_dirs[0] / "provenance.json").read_text(encoding="utf-8"))
            phases = provenance["configured_actor_routing"]
            self.assertEqual([phase["role"] for phase in phases], ["architect", "executor", "reviewer"])
            failed = phases[-1]
            self.assertTrue(failed["dispatch_failed"])
            self.assertEqual(failed["failure_type"], "CommandError")
            self.assertEqual(failed["actor_id"], "reviewer")
            self.assertEqual(failed["actual_actor"], "reviewer")
            self.assertEqual(failed["provider"], "anthropic")
            self.assertEqual(failed["backend"], "claude_code")
            self.assertEqual(failed["repository"], str(repository.resolve()))

    def test_invalid_architect_plan_persists_actual_actor_provenance_before_failing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "disposable-mission"
            repository.mkdir()
            subprocess.run(["git", "init", "--quiet"], cwd=repository, check=True)
            architect = AccountConfig(
                name="biel3",
                label="Architect",
                codex_home=root / "architect-home",
                model="",
                reasoning_effort="high",
                backend="windows",
            )
            fallback = AccountConfig(
                name="fallback",
                label="Fallback",
                codex_home=root / "fallback-home",
                model="",
                reasoning_effort="high",
                backend="app_server",
                fallback_roles=("architect",),
            )
            config = OrchestratorConfig(
                repository=repository,
                runs_dir=root / "runs",
                max_correction_cycles=0,
                require_clean_git=True,
                codex_command="codex",
                accounts={"biel3": architect, "fallback": fallback},
                roles={"architect": "biel3", "orchestrator": "biel3"},
                project_root=Path.cwd(),
                config_path=root / "config.toml",
                fallback_enabled=True,
            )
            task = root / "brief.md"
            task.write_text("Plan a small UI change.", encoding="utf-8")
            invalid_plan = CommandResult(
                ["codex"], 0, '{"summary":"Completed turn with an invalid plan."}', ""
            )

            with patch("dual_codex.providers.provider_supports_role", return_value=True), patch(
                "dual_codex.codex.run_codex_terminal", return_value=invalid_plan
            ) as primary, patch("dual_codex.codex.run_codex_app_server") as fallback_dispatch:
                with self.assertRaises(ActorResultError):
                    execute(config, task)

            primary.assert_called_once()
            fallback_dispatch.assert_not_called()
            run_dirs = [
                path
                for path in config.runs_dir.iterdir()
                if (path / "provenance.json").is_file()
            ]
            self.assertEqual(len(run_dirs), 1)
            provenance = json.loads((run_dirs[0] / "provenance.json").read_text(encoding="utf-8"))
            architect_provenance = provenance["configured_actor_routing"][0]
            self.assertEqual(architect_provenance["actor_id"], "biel3")
            self.assertEqual(architect_provenance["actual_actor"], "biel3")
            self.assertTrue(architect_provenance["fallback_enabled"])
            self.assertFalse(architect_provenance["fallback_used"])
            self.assertIn("missing required field", architect_provenance["architect_result_validation_error"])
            self.assertTrue(architect_provenance["canonical_bootstrap_source_sha256"])

    def test_invalid_fallback_architect_plan_preserves_primary_failure_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "disposable-mission"
            repository.mkdir()
            subprocess.run(["git", "init", "--quiet"], cwd=repository, check=True)
            architect = AccountConfig(
                name="biel3",
                label="Architect",
                codex_home=root / "architect-home",
                model="",
                reasoning_effort="high",
                backend="windows",
            )
            fallback = AccountConfig(
                name="fallback",
                label="Fallback",
                codex_home=root / "fallback-home",
                model="",
                reasoning_effort="high",
                backend="app_server",
                fallback_roles=("architect",),
            )
            third = replace(fallback, name="third", label="Third", codex_home=root / "third-home")
            config = OrchestratorConfig(
                repository=repository,
                runs_dir=root / "runs",
                max_correction_cycles=0,
                require_clean_git=True,
                codex_command="codex",
                accounts={"biel3": architect, "fallback": fallback, "third": third},
                roles={"architect": "biel3", "orchestrator": "biel3"},
                project_root=Path.cwd(),
                config_path=root / "config.toml",
                fallback_enabled=True,
            )
            task = root / "brief.md"
            task.write_text("Plan a small UI change.", encoding="utf-8")
            primary_unavailable = CommandResult(
                ["codex"],
                1,
                "",
                "primary provider unavailable",
                {"availability_failure_class": "provider_unavailable"},
            )
            invalid_plan = CommandResult(
                ["codex", "app-server"],
                0,
                '{"summary":"Fallback completed with an invalid plan."}',
                "",
            )
            fallback_actors: list[str] = []

            def run_fallback(**kwargs):
                fallback_actors.append(kwargs["agent"].account_name)
                return invalid_plan

            with patch("dual_codex.providers.provider_supports_role", return_value=True), patch(
                "dual_codex.codex.run_codex_terminal", return_value=primary_unavailable
            ) as primary, patch(
                "dual_codex.codex.run_codex_app_server", side_effect=run_fallback
            ) as fallback_dispatch:
                with self.assertRaises(ActorResultError) as raised:
                    execute(config, task)

            primary.assert_called_once()
            fallback_dispatch.assert_called_once()
            self.assertEqual(fallback_actors, ["fallback"])
            error = raised.exception
            self.assertEqual(error.actor, "fallback")
            self.assertIn("Architect plan validation failed", str(error))
            self.assertEqual(error.metadata["primary_actor"], "biel3")
            self.assertEqual(error.metadata["actual_actor"], "fallback")
            self.assertTrue(error.metadata["fallback_enabled"])
            self.assertTrue(error.metadata["fallback_used"])
            self.assertEqual(error.metadata["failed_actor"], "biel3")
            self.assertEqual(error.metadata["fallback_actor"], "fallback")
            self.assertEqual(error.metadata["fallback_failure_class"], "provider_unavailable")
            self.assertEqual(
                error.metadata["fallback_reason"],
                "Codex architect failed through the configured Windows terminal backend: primary provider unavailable",
            )
            self.assertIn("missing required field", error.metadata["architect_result_validation_error"])
            self.assertEqual(error.metadata["actor_id"], "fallback")
            self.assertTrue(error.metadata["canonical_bootstrap_required"])
            self.assertTrue(error.metadata["canonical_bootstrap_source_sha256"])

            run_dirs = [
                path
                for path in config.runs_dir.iterdir()
                if (path / "provenance.json").is_file()
            ]
            self.assertEqual(len(run_dirs), 1)
            provenance = json.loads((run_dirs[0] / "provenance.json").read_text(encoding="utf-8"))
            architect_provenance = provenance["configured_actor_routing"][0]
            for key in (
                "primary_actor",
                "actual_actor",
                "fallback_enabled",
                "fallback_used",
                "failed_actor",
                "fallback_actor",
                "fallback_failure_class",
                "fallback_reason",
                "architect_result_validation_error",
                "canonical_bootstrap_required",
                "canonical_bootstrap_source_sha256",
            ):
                self.assertEqual(architect_provenance[key], error.metadata[key], key)

    def test_run_progress_propagates_safe_heartbeats_without_creating_corrections(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "repository"
            repository.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.name", "Run Test"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.email", "run@example.invalid"], cwd=repository, check=True)
            tracked = repository / "tracked.txt"
            tracked.write_text("committed\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-qm", "initial"], cwd=repository, check=True)
            tracked.write_text("pre-existing user edit\n", encoding="utf-8")
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
                max_correction_cycles=1,
                require_clean_git=False,
                codex_command="codex",
                accounts=accounts,
                roles={"architect": "architect", "executor": "executor", "reviewer": "reviewer", "orchestrator": "architect"},
                project_root=Path.cwd(),
                config_path=root / "config.toml",
            )
            task = root / "brief.md"
            task.write_text("A bounded fake mission.", encoding="utf-8")
            events: list[str] = []

            def fake_runner(**kwargs):
                role = kwargs["role"]
                run_dir = kwargs["output_path"].parent
                self.assertTrue((run_dir / "task.md").is_file())
                self.assertTrue((run_dir / "initial_git_baseline.json").is_file())
                self.assertEqual(json.loads((run_dir / "run_state.json").read_text(encoding="utf-8"))["status"], "running")
                if role == "architect":
                    self.assertTrue(
                        any(
                            (event := json.loads(item.removeprefix("DUAL_CODEX_PROGRESS "))).get("phase") == "architect"
                            and event.get("state") == "started"
                            for item in events
                        )
                    )
                    payload = {"summary": "plan", "steps": [], "acceptance_criteria": [], "risks": [], "files_to_inspect": [], "skills_loaded": []}
                elif role == "executor":
                    (repository / "created-by-run.txt").write_text("run output\n", encoding="utf-8")
                    kwargs["progress"]("app-server turn turn-safe-1 still running")
                    kwargs["progress"]("app-server turn turn-safe-1 still running")
                    kwargs["progress"]("PRIVATE_PROMPT and model reasoning must not be printed")
                    payload = {"summary": "implemented", "files_changed": ["created-by-run.txt"], "commands_run": [], "tests": [], "remaining_issues": []}
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

            with patch("dual_codex.orchestrator.run_codex_for_role", side_effect=fake_runner):
                outcome = execute(config, task, progress=events.append)

            decoded = [json.loads(event.removeprefix("DUAL_CODEX_PROGRESS ")) for event in events]
            executor_running = [event for event in decoded if event.get("phase") == "executor" and event.get("state") == "running"]
            self.assertEqual(len(executor_running), 3)
            self.assertTrue(all(event.get("detail") == "app-server turn turn-safe-1 still running" for event in executor_running[:2]))
            self.assertNotIn("detail", executor_running[2])
            self.assertNotIn("PRIVATE_PROMPT", "\n".join(events))
            self.assertNotIn("reasoning", "\n".join(events))
            self.assertTrue(any(event.get("phase") == "architect" and event.get("state") == "started" for event in decoded))
            self.assertTrue(any(event.get("phase") == "executor" and event.get("state") == "completed" for event in decoded))
            self.assertTrue(any(event.get("phase") == "reviewer" and event.get("state") == "completed" for event in decoded))
            self.assertEqual(outcome.correction_cycles, 0)
            self.assertEqual(outcome.verdict, "approved")
            state = json.loads((outcome.run_dir / "run_state.json").read_text(encoding="utf-8"))
            mutation = json.loads((outcome.run_dir / "mutation-attribution.json").read_text(encoding="utf-8"))
            self.assertTrue(state["last_progress_at"])
            self.assertEqual(mutation["unchanged_preexisting_paths"], ["tracked.txt"])
            self.assertEqual(mutation["run_created_paths"], ["created-by-run.txt"])

    def test_phase_timeout_is_visible_and_baseline_survives_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "repository"
            repository.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.name", "Run Test"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.email", "run@example.invalid"], cwd=repository, check=True)
            tracked = repository / "tracked.txt"
            tracked.write_text("committed\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-qm", "initial"], cwd=repository, check=True)
            tracked.write_text("pre-existing user edit\n", encoding="utf-8")
            account = AccountConfig(
                name="actor",
                label="Actor",
                codex_home=root / "actor-home",
                model="",
                reasoning_effort="high",
                backend="app_server",
            )
            config = OrchestratorConfig(
                repository=repository,
                runs_dir=root / "runs",
                max_correction_cycles=0,
                require_clean_git=False,
                codex_command="codex",
                accounts={"actor": account},
                roles={"architect": "actor", "executor": "actor", "reviewer": "actor", "orchestrator": "actor"},
                project_root=Path.cwd(),
                config_path=root / "config.toml",
            )
            task = root / "brief.md"
            task.write_text("Timeout fixture.", encoding="utf-8")
            events: list[str] = []

            def timeout_runner(**kwargs):
                kwargs["progress"]("app-server turn turn-timeout still running")
                raise TimeoutError("simulated provider timeout")

            with patch("dual_codex.orchestrator.run_codex_for_role", side_effect=timeout_runner):
                with self.assertRaisesRegex(TimeoutError, "simulated provider timeout"):
                    execute(config, task, progress=events.append)

            run_dir = next(path for path in config.runs_dir.iterdir() if (path / "run_state.json").is_file())
            baseline = json.loads((run_dir / "initial_git_baseline.json").read_text(encoding="utf-8"))
            state = json.loads((run_dir / "run_state.json").read_text(encoding="utf-8"))
            provenance = json.loads((run_dir / "provenance.json").read_text(encoding="utf-8"))
            mutation = json.loads((run_dir / "mutation-attribution.json").read_text(encoding="utf-8"))
            decoded = [json.loads(event.removeprefix("DUAL_CODEX_PROGRESS ")) for event in events]
            self.assertTrue(baseline["head"])
            self.assertEqual(state["status"], "failed")
            self.assertEqual(state["initial_git_baseline"]["path"], "initial_git_baseline.json")
            self.assertEqual(provenance["configured_actor_routing"][-1]["phase_state"], "timeout")
            self.assertTrue(any(event.get("state") == "timeout" for event in decoded))
            self.assertEqual(mutation["unchanged_preexisting_paths"], ["tracked.txt"])
            self.assertEqual(mutation["run_touched_paths"], [])

    def test_security_scan_conflict_keeps_git_mutation_attribution_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "disposable-security-conflict"
            repository.mkdir()
            subprocess.run(["git", "init", "--quiet"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.name", "Run Test"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.email", "run@example.invalid"], cwd=repository, check=True)
            (repository / "tracked.txt").write_text("baseline\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-qm", "initial"], cwd=repository, check=True)
            revision = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repository, check=True, capture_output=True, text=True
            ).stdout.strip()
            actor = AccountConfig(
                name="actor",
                label="Actor",
                codex_home=root / "actor-home",
                model="",
                reasoning_effort="high",
                backend="app_server",
            )
            config = OrchestratorConfig(
                repository=repository,
                runs_dir=root / "runs",
                max_correction_cycles=0,
                require_clean_git=True,
                codex_command="codex",
                accounts={"actor": actor},
                roles={"architect": "actor", "executor": "actor", "reviewer": "actor", "orchestrator": "actor"},
                project_root=Path.cwd(),
                config_path=root / "config.toml",
            )
            task = root / "brief.md"
            task.write_text("Run a Codex Security standard scan for this repository.", encoding="utf-8")
            active_scans = [
                {
                    "scanId": scan_id,
                    "mode": mode,
                    "progress": {"status": "running"},
                    "targetPath": str(repository.resolve()),
                    "targetId": stable_target_id(repository),
                    "targetRevision": revision,
                    "scope": ".",
                }
                for scan_id, mode in (("deep-active", "deep"), ("standard-active", "standard"))
            ]
            decision = arbitrate_security_scans(
                active_scans,
                plugin_id=PLUGIN_ID,
                plugin_version="0.1.31",
                target_path=repository,
                target_revision=revision,
            )
            self.assertEqual(decision.action, "conflict")
            provider = type("Provider", (), {"arbitrate": lambda *_args, **_kwargs: decision})()
            seen_roles: list[str] = []

            def fake_runner(**kwargs):
                role = kwargs["role"]
                seen_roles.append(role)
                payload = {
                    "summary": "plan",
                    "steps": [],
                    "acceptance_criteria": [],
                    "risks": [],
                    "files_to_inspect": [],
                    "skills_loaded": [],
                }
                kwargs["output_path"].write_text(json.dumps(payload), encoding="utf-8")
                return CommandResult(
                    ["fake"],
                    0,
                    "",
                    "",
                    {
                        "phase": role,
                        "role": role,
                        "actor_id": "actor",
                        "actual_actor": "actor",
                        "provider": "codex",
                        "backend": "app_server",
                        "repository": str(repository.resolve()),
                        "fallback_used": False,
                    },
                )

            with patch("dual_codex.orchestrator.CodexSecurityProvider", return_value=provider), patch(
                "dual_codex.orchestrator.run_codex_for_role", side_effect=fake_runner
            ):
                with self.assertRaises(SecurityScanError) as raised:
                    execute(config, task)

            self.assertEqual(raised.exception.failure_class, "SECURITY_SCAN_CONFLICT")
            self.assertIn("deep-active", str(raised.exception))
            self.assertIn("standard-active", str(raised.exception))
            self.assertEqual(seen_roles, [])
            run_result = raised.exception.dual_codex_run_result
            self.assertEqual(run_result["failure_class"], "SECURITY_SCAN_CONFLICT")
            self.assertEqual(run_result["mutation_attribution_status"], "complete")
            run_state = json.loads(Path(run_result["run_state_path"]).read_text(encoding="utf-8"))
            mutation = json.loads(Path(run_result["mutation_attribution_path"]).read_text(encoding="utf-8"))
            self.assertEqual(run_state["failure"]["failure_class"], "SECURITY_SCAN_CONFLICT")
            self.assertEqual(run_state["mission_security_requirement"]["source"], "original_task")
            self.assertEqual(run_state["mutation_attribution"]["status"], "complete")
            self.assertEqual(mutation["status"], "complete")
            self.assertNotIn("UNKNOWN_MUTATION_STATE", json.dumps(run_state))

    def test_stale_security_scan_is_rescanned_before_reviewer_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "disposable-security-success"
            repository.mkdir()
            subprocess.run(["git", "init", "--quiet"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.name", "Run Test"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.email", "run@example.invalid"], cwd=repository, check=True)
            (repository / "tracked.txt").write_text("baseline\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-qm", "initial"], cwd=repository, check=True)
            revision = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repository, check=True, capture_output=True, text=True
            ).stdout.strip()
            actor = AccountConfig(
                name="actor",
                label="Actor",
                codex_home=root / "actor-home",
                model="",
                reasoning_effort="high",
                backend="app_server",
            )
            config = OrchestratorConfig(
                repository=repository,
                runs_dir=root / "runs",
                max_correction_cycles=2,
                require_clean_git=True,
                codex_command="codex",
                accounts={"actor": actor},
                roles={"architect": "actor", "executor": "actor", "reviewer": "actor", "orchestrator": "actor"},
                project_root=Path.cwd(),
                config_path=root / "config.toml",
            )
            task = root / "brief.md"
            task.write_text("Run a Codex Security standard scan for this repository.", encoding="utf-8")
            scan_id = "disposable-scan-1"
            target_id = stable_target_id(repository)
            completed_scan = {
                "scanId": scan_id,
                "mode": "standard",
                "progress": {"status": "complete"},
                "targetPath": str(repository.resolve()),
                "targetId": target_id,
                "targetRevision": revision,
                "scope": ".",
                "targetSnapshotDigest": "completed-target-snapshot",
                "currentSnapshotDigest": "workspace-mutated-after-scan",
            }
            replacement_scan = {
                **completed_scan,
                "scanId": "disposable-scan-2",
                "targetSnapshotDigest": "fresh-target-snapshot",
                "currentSnapshotDigest": "fresh-target-snapshot",
            }
            final_replacement_scan = {
                **completed_scan,
                "scanId": "disposable-scan-3",
                "targetSnapshotDigest": "final-fresh-target-snapshot",
                "currentSnapshotDigest": "final-fresh-target-snapshot",
            }

            class FakeSecurityProvider:
                plugin_id = PLUGIN_ID
                plugin_version = "0.1.31"

                def __init__(self):
                    self.scans = []
                    self.arbitrations = []

                def arbitrate(self, **kwargs):
                    self.arbitrations.append(dict(kwargs))
                    return arbitrate_security_scans(
                        self.scans,
                        plugin_id=self.plugin_id,
                        plugin_version=self.plugin_version,
                        target_path=kwargs["repository"],
                        target_revision=kwargs["target_revision"],
                        required_mode=kwargs["required_mode"],
                        required_scope=kwargs["required_scope"],
                        allow_completed_reuse=kwargs["allow_completed_reuse"],
                    )

                def list_target_scans(self, _repository):
                    return list(self.scans)

            provider = FakeSecurityProvider()
            seen_roles: list[str] = []
            scan_only_calls = 0
            reviewer_calls = 0

            def fake_runner(**kwargs):
                nonlocal scan_only_calls, reviewer_calls
                role = kwargs["role"]
                seen_roles.append(role)
                if role == "architect":
                    self.assertIn("HOST SECURITY GATE", kwargs["prompt"])
                    self.assertIn('"required_mode":"standard"', kwargs["prompt"])
                    self.assertIn('"host_decision":"start"', kwargs["prompt"])
                    self.assertIn('"revision":"' + revision + '"', kwargs["prompt"])
                    self.assertIn("must not start, resume, cancel, await operationally", kwargs["prompt"])
                    self.assertNotIn("security_scan_provenance", kwargs["prompt"])
                    self.assertNotIn("start exactly one scan", kwargs["prompt"])
                    payload = {
                        "summary": "plan",
                        "steps": ["Do not run the Codex Security scan."],
                        "acceptance_criteria": [],
                        "risks": [],
                        "files_to_inspect": [],
                        "skills_loaded": [],
                    }
                elif role == "executor":
                    self.assertIn('"decision":"start"', kwargs["prompt"])
                    if "scan-only continuation" in kwargs["prompt"]:
                        scan_only_calls += 1
                        self.assertNotIn("Implement the task", kwargs["prompt"])
                        if scan_only_calls == 1:
                            provider.scans = [completed_scan, replacement_scan]
                            current_scan_id = "disposable-scan-2"
                            summary = "fresh scan-only continuation completed"
                        else:
                            provider.scans = [completed_scan, replacement_scan, final_replacement_scan]
                            current_scan_id = "disposable-scan-3"
                            summary = "final fresh scan-only continuation completed"
                    else:
                        self.assertIn("supersedes any conflicting Architect plan", kwargs["prompt"])
                        self.assertIn("Do not run the Codex Security scan", kwargs["prompt"])
                        provider.scans = [completed_scan]
                        current_scan_id = scan_id
                        summary = "initial scan completed stale"
                    payload = {
                        "summary": summary,
                        "files_changed": [],
                        "commands_run": [],
                        "tests": [],
                        "remaining_issues": [],
                        "security_scan_provenance": {
                            "plugin_id": PLUGIN_ID,
                            "plugin_version": "0.1.31",
                            "target_identity": {
                                "path": str(repository.resolve()),
                                "target_id": target_id,
                                "revision": revision,
                                "scope": ".",
                            },
                            "scan_id": current_scan_id,
                            "mode": "standard",
                            "initial_status": "running",
                            "action": "started",
                            "final_status": "complete",
                        },
                    }
                else:
                    reviewer_calls += 1
                    prompt_parts = kwargs["prompt"].split("CONTROL-PLANE VERIFIED PHASE PROVENANCE:\n", 1)
                    reviewer_context = json.loads(prompt_parts[1].split("\n\nGIT STATUS AND DIFF:", 1)[0])
                    security_gate = reviewer_context["host_security_gate"]
                    self.assertTrue(security_gate["fresh_for_acceptance"])
                    self.assertEqual(security_gate["authority_state"], "completed_fresh")
                    self.assertEqual(security_gate["selected_scan"]["scan_id"], "disposable-scan-2" if reviewer_calls == 1 else "disposable-scan-3")
                    self.assertIn(
                        {"event": "completed_stale", "generation": 1, "selected_scan_id": scan_id, "failure_class": "SECURITY_SCAN_SNAPSHOT_STALE"},
                        security_gate["coverage_history"],
                    )
                    if reviewer_calls == 2:
                        self.assertIn(
                            {"event": "completed_stale", "generation": 2, "selected_scan_id": "disposable-scan-2", "failure_class": "SECURITY_SCAN_SNAPSHOT_STALE"},
                            security_gate["coverage_history"],
                        )
                    state_at_review = json.loads(
                        (kwargs["output_path"].parent / "run_state.json").read_text(encoding="utf-8")
                    )
                    authority_at_review = state_at_review["security_scan_authority"]
                    self.assertEqual(authority_at_review["authority_state"], "completed_fresh")
                    expected_generation = 2 if reviewer_calls == 1 else 3
                    expected_scan_id = f"disposable-scan-{expected_generation}"
                    self.assertEqual(authority_at_review["generation"], expected_generation)
                    self.assertEqual(authority_at_review["selected_scan_id"], expected_scan_id)
                    payload = {"verdict": "approved", "summary": "approved", "findings": []}
                    if reviewer_calls == 1:
                        # Simulate the provider observing a repository mutation during Reviewer work.
                        replacement_scan["currentSnapshotDigest"] = "workspace-changed-after-review"
                kwargs["output_path"].write_text(json.dumps(payload), encoding="utf-8")
                return CommandResult(
                    ["fake"],
                    0,
                    "",
                    "",
                    {
                        "phase": role,
                        "role": role,
                        "actor_id": "actor",
                        "actual_actor": "actor",
                        "provider": "codex",
                        "backend": "app_server",
                        "repository": str(repository.resolve()),
                        "fallback_used": False,
                    },
                )

            with patch("dual_codex.orchestrator.CodexSecurityProvider", return_value=provider), patch(
                "dual_codex.orchestrator.run_codex_for_role", side_effect=fake_runner
            ):
                outcome = execute(config, task)

            run_state = json.loads((outcome.run_dir / "run_state.json").read_text(encoding="utf-8"))
            provenance = json.loads((outcome.run_dir / "provenance.json").read_text(encoding="utf-8"))
            self.assertEqual(
                seen_roles,
                ["architect", "executor", "executor", "reviewer", "executor", "reviewer"],
            )
            self.assertEqual(reviewer_calls, 2)
            self.assertEqual([item["required_mode"] for item in provider.arbitrations], ["standard"])
            self.assertEqual([item["required_scope"] for item in provider.arbitrations], ["."])
            self.assertEqual(run_state["security_scan_arbitrations"][0]["decision"], "start")
            self.assertEqual(run_state["security_scan_authority"]["selected_scan_id"], "disposable-scan-3")
            self.assertEqual(run_state["security_scan_authority"]["generation"], 3)
            self.assertEqual(
                run_state["security_scan_authority"]["completed_validation"]["validated_at_checkpoint"],
                "after_reviewer_0_1",
            )
            saved_scan = run_state["security_scan_provenance"][0]
            self.assertEqual(saved_scan["scan_id"], scan_id)
            self.assertEqual(saved_scan["scan_mode"], "standard")
            self.assertEqual(saved_scan["initial_status"], "running")
            self.assertEqual(saved_scan["action"], "started")
            self.assertEqual(saved_scan["final_status"], "complete")
            self.assertEqual(
                [item["scan_id"] for item in run_state["security_scan_provenance"]],
                [scan_id, "disposable-scan-2", "disposable-scan-3"],
            )
            self.assertEqual(
                [item["scan_id"] for item in provenance["security_scan_provenance"]],
                [scan_id, "disposable-scan-2", "disposable-scan-3"],
            )
            executor_provenance = [item for item in provenance["configured_actor_routing"] if item["role"] == "executor"]
            self.assertEqual(
                [item["security_scan_provenance"]["scan_id"] for item in executor_provenance],
                [scan_id, "disposable-scan-2", "disposable-scan-3"],
            )
            report = (outcome.run_dir / "REPORT.md").read_text(encoding="utf-8")
            self.assertIn("## Codex Security coverage", report)
            self.assertIn("Authority: **completed_fresh** / generation **3** / fresh for acceptance: **true**", report)
            self.assertIn("Authoritative scan: `disposable-scan-3`", report)
            self.assertIn("Generation 1: **completed_stale** / scan `disposable-scan-1`", report)
            self.assertIn("Generation 2: **completed_stale** / scan `disposable-scan-2`", report)
            self.assertIn("Generation 3: **completed_fresh** / scan `disposable-scan-3`", report)
            self.assertNotIn("Executor-reported Codex Security evidence", report)
            self.assertNotIn("handoffClaimToken", json.dumps(provenance))


if __name__ == "__main__":
    unittest.main()
