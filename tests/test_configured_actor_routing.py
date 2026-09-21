from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from dual_codex.bootstrap import BOOTSTRAP_MARKER
from dual_codex.codex import delegate_to_configured_actor, run_codex_for_role
from dual_codex.config import AccountConfig, ConfigError, OrchestratorConfig
from dual_codex.delegation import DelegationError, run_codex_exec as delegation_adapter
from dual_codex.orchestrator import execute
from dual_codex.process import CommandError, CommandResult


class ConfiguredActorRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._instructions = tempfile.TemporaryDirectory()
        self.addCleanup(self._instructions.cleanup)
        root = Path(self._instructions.name)
        (root / "AGENTS.md").write_text("# canonical test policy\n", encoding="utf-8")
        (root / "skills").mkdir()
        self._bootstrap_patch = patch("dual_codex.bootstrap.CANONICAL_INSTRUCTIONS_ROOT", root)
        self._bootstrap_patch.start()
        self.addCleanup(self._bootstrap_patch.stop)

    def _config(self, root: Path, *, executor: str = "executor-b") -> OrchestratorConfig:
        accounts = {
            "orchestrator": AccountConfig(
                name="orchestrator",
                label="Primary Codex",
                codex_home=root / "orchestrator-home",
                model="gpt-primary",
                reasoning_effort="high",
                backend="windows",
            ),
            "executor-b": AccountConfig(
                name="executor-b",
                label="Antigravity / Gemini",
                codex_home=root / "executor-b-home",
                model="Gemini 3.8 Flash",
                reasoning_effort="medium",
                backend="antigravity",
                provider_type="gemini",
                adapter_type="antigravity_cli",
            ),
            "executor-d": AccountConfig(
                name="executor-d",
                label="Replacement Antigravity / Gemini",
                codex_home=root / "executor-d-home",
                model="Gemini 3.8 Flash",
                reasoning_effort="medium",
                backend="antigravity",
                provider_type="gemini",
                adapter_type="antigravity_cli",
            ),
            "secondary": AccountConfig(
                name="secondary",
                label="Codex Secondary",
                codex_home=root / "secondary-home",
                model="gpt-5.6-luna",
                reasoning_effort="max",
                backend="app_server",
                provider_type="codex",
                adapter_type="codex_cli",
            ),
        }
        return OrchestratorConfig(
            repository=root / "repository",
            runs_dir=root / "runs",
            max_correction_cycles=0,
            require_clean_git=True,
            codex_command="codex",
            accounts=accounts,
            roles={
                "orchestrator": "orchestrator",
                "architect": "secondary",
                "reviewer": "secondary",
                "executor": executor,
            },
            project_root=Path(__file__).parents[1],
            config_path=root / "config.toml",
        )

    def test_full_topology_uses_configured_provider_backend_and_zero_native_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self._config(root)
            config.repository.mkdir()
            subprocess.run(["git", "init", "--quiet"], cwd=config.repository, check=True)
            task = root / "task.md"
            task.write_text("route the configured actors", encoding="utf-8")
            seen: list[tuple[str, str, str, str]] = []

            def fake_runner(**kwargs):
                role = kwargs["role"]
                agent = kwargs["agent"]
                seen.append((role, agent.account_name, agent.provider_type, agent.backend))
                if role == "architect":
                    payload = {
                        "summary": "plan",
                        "steps": [],
                        "acceptance_criteria": [],
                        "risks": [],
                        "files_to_inspect": [],
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
                kwargs["output_path"].write_text(json.dumps(payload), encoding="utf-8")
                return CommandResult([agent.backend], 0, "", "")

            with patch("dual_codex.orchestrator.run_codex_for_role", side_effect=fake_runner), patch(
                "dual_codex.codex.run_codex_exec"
            ) as native_spawn:
                outcome = execute(config, task)

            self.assertEqual(
                seen,
                [
                    ("architect", "secondary", "codex", "app_server"),
                    ("executor", "executor-b", "gemini", "antigravity"),
                    ("reviewer", "secondary", "codex", "app_server"),
                ],
            )
            native_spawn.assert_not_called()
            self.assertEqual(
                [(item["role"], item["actor_id"], item["provider"], item["backend"]) for item in outcome.phase_provenance],
                [
                    ("architect", "secondary", "codex", "app_server"),
                    ("executor", "executor-b", "gemini", "antigravity"),
                    ("reviewer", "secondary", "codex", "app_server"),
                ],
            )
            provenance = json.loads((outcome.run_dir / "provenance.json").read_text(encoding="utf-8"))
            self.assertTrue(all(item["configured_actor"] for item in provenance["configured_actor_routing"]))

    def test_role_reassignment_is_consumed_on_next_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self._config(root)
            repository = root / "repository"
            repository.mkdir()
            observed: list[str] = []

            def fake_runner(**kwargs):
                observed.append(kwargs["agent"].account_name)
                return CommandResult(["fake"], 0, "", "")

            with patch("dual_codex.codex.run_codex_for_role", side_effect=fake_runner):
                delegate_to_configured_actor(
                    config=config,
                    role="executor",
                    task="first",
                    repository=repository,
                    output_path=root / "first.json",
                    schema_path=root / "schema.json",
                )
                config.roles["executor"] = "executor-d"
                delegate_to_configured_actor(
                    config=config,
                    role="executor",
                    task="second",
                    repository=repository,
                    output_path=root / "second.json",
                    schema_path=root / "schema.json",
                )
            self.assertEqual(observed, ["executor-b", "executor-d"])

    def test_configured_actor_contract_allows_minimal_task_repository_call(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self._config(root)
            repository = root / "repository"
            repository.mkdir()
            captured: dict[str, object] = {}

            def fake_runner(**kwargs):
                captured.update(kwargs)
                return CommandResult(["fake"], 0, "", "")

            with patch("dual_codex.codex.run_codex_for_role", side_effect=fake_runner):
                result = delegate_to_configured_actor(
                    config=config,
                    role="executor",
                    task="minimal contract",
                    repository=repository,
                )
            self.assertEqual(captured["agent"].account_name, "executor-b")
            self.assertTrue(result.metadata["configured_actor"])

    def test_same_configured_actor_can_serve_architect_and_reviewer(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self._config(root)
            repository = root / "repository"
            repository.mkdir()
            observed: list[tuple[str, str, str]] = []

            def fake_runner(**kwargs):
                observed.append((kwargs["role"], kwargs["agent"].account_name, kwargs["agent"].backend))
                return CommandResult(["fake"], 0, "", "")

            with patch("dual_codex.codex.run_codex_for_role", side_effect=fake_runner):
                for role in ("architect", "reviewer"):
                    delegate_to_configured_actor(
                        config=config,
                        role=role,
                        task=role,
                        repository=repository,
                        output_path=root / f"{role}.json",
                        schema_path=root / "schema.json",
                    )
            self.assertEqual(observed, [("architect", "secondary", "app_server"), ("reviewer", "secondary", "app_server")])

    def test_unavailable_configured_architect_and_reviewer_fail_without_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self._config(root)
            repository = root / "repository"
            repository.mkdir()
            unavailable = CommandResult(["codex", "app-server"], 1, "", "secondary unavailable")
            with patch("dual_codex.codex.run_codex_app_server", return_value=unavailable), patch(
                "dual_codex.codex.run_codex_exec"
            ) as native:
                for role in ("architect", "reviewer"):
                    with self.assertRaisesRegex(CommandError, f"Codex {role} failed"):
                        run_codex_for_role(
                            config=config,
                            role=role,
                            repository=repository,
                            prompt=role,
                            output_path=root / f"{role}.json",
                            schema_path=root / "schema.json",
                        )
                native.assert_not_called()

    def test_unassigned_executor_is_explicitly_unavailable_without_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self._config(root)
            config.roles.pop("executor")
            with patch("dual_codex.codex.run_codex_exec") as native:
                with self.assertRaisesRegex(ConfigError, "Required role 'executor' is unassigned"):
                    delegate_to_configured_actor(
                        config=config,
                        role="executor",
                        task="blocked",
                        repository=root,
                        output_path=root / "result.json",
                        schema_path=root / "schema.json",
                    )
                native.assert_not_called()

    def test_executor_adapter_cannot_satisfy_architect_or_reviewer(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self._config(root)
            repository = root / "repository"
            repository.mkdir()
            with patch("dual_codex.delegation.run_antigravity") as agy, patch(
                "dual_codex.delegation.run_codex_terminal"
            ) as terminal:
                for role in ("architect", "reviewer"):
                    with self.assertRaisesRegex(DelegationError, "non-executor role"):
                        delegation_adapter(
                            config=config,
                            agent=config.agent_for_role("executor"),
                            role=role,
                            repository=repository,
                            prompt=role,
                            output_path=root / f"{role}.json",
                            schema_path=root / "schema.json",
                        )
                agy.assert_not_called()
                terminal.assert_not_called()

    def test_canonical_bootstrap_is_bound_to_codex_secondary_transport(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self._config(root)
            repository = root / "repository"
            repository.mkdir()
            expected = CommandResult(["codex", "app-server"], 0, "", "")
            with patch("dual_codex.codex.run_codex_app_server", return_value=expected) as app_server:
                result = run_codex_for_role(
                    config=config,
                    role="architect",
                    repository=repository,
                    prompt="inspect",
                    output_path=root / "plan.json",
                    schema_path=root / "schema.json",
                )
            prompt = app_server.call_args.kwargs["prompt"]
            self.assertIn(BOOTSTRAP_MARKER, prompt)
            self.assertIn("AGENTS.md", prompt)
            self.assertIn("skills", prompt)
            self.assertTrue(result.metadata["configured_actor"])
            self.assertEqual(result.metadata["actor_id"], "secondary")
            self.assertEqual(result.metadata["provider"], "codex")
            self.assertEqual(result.metadata["backend"], "app_server")
            self.assertEqual(result.metadata["delegation_transport"], "app_server")
            self.assertTrue(result.metadata["canonical_bootstrap_required"])

    def test_missing_canonical_bootstrap_fails_closed_before_transport(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self._config(root)
            repository = root / "repository"
            repository.mkdir()
            missing = root / "missing-policy"
            with patch("dual_codex.bootstrap.CANONICAL_INSTRUCTIONS_ROOT", missing), patch(
                "dual_codex.codex.run_codex_app_server"
            ) as app_server:
                with self.assertRaisesRegex(FileNotFoundError, "Canonical instruction file"):
                    delegate_to_configured_actor(
                        config=config,
                        role="reviewer",
                        task="inspect",
                        repository=repository,
                        output_path=root / "review.json",
                        schema_path=root / "schema.json",
                    )
                app_server.assert_not_called()

    def test_caller_cannot_replace_registry_actor_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self._config(root)
            repository = root / "repository"
            repository.mkdir()
            wrong_actor = config.agent_for_role("executor")
            config.roles["executor"] = "executor-d"
            with self.assertRaisesRegex(ValueError, "configured actor 'executor-d'"):
                run_codex_for_role(
                    config=config,
                    agent=wrong_actor,
                    role="executor",
                    repository=repository,
                    prompt="do not reroute",
                    output_path=root / "result.json",
                    schema_path=root / "schema.json",
                )


if __name__ == "__main__":
    unittest.main()
