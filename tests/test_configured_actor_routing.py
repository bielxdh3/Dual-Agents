from __future__ import annotations

import hashlib
import json
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import subprocess
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

from dual_codex.bootstrap import BOOTSTRAP_MARKER
from dual_codex.cli import main as cli_main
from dual_codex.codex import ActorAvailabilityError, classify_actor_failure, delegate_to_configured_actor, run_codex_for_role
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
        for name in ("memory", "ponytail", "project-phase-review", "project-security-review"):
            skill = root / "skills" / name
            skill.mkdir()
            (skill / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
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

    def test_api_architect_is_rejected_before_bootstrap_or_provider_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = self._config(root)
            accounts = dict(base.accounts)
            accounts["api-architect"] = AccountConfig(
                name="api-architect",
                label="OpenAI-compatible API",
                codex_home=root / "api-home",
                model="model-a",
                reasoning_effort="high",
                backend="api",
                provider_type="api",
                adapter_type="openai_compatible",
                auth_mode="environment",
                auth_reference="env:TEST_API_KEY",
                base_url="https://api.example.test/v1",
            )
            config = replace(
                base,
                accounts=accounts,
                roles={**base.roles, "architect": "api-architect"},
            )

            with patch("dual_codex.codex.create_canonical_bootstrap") as create_bootstrap, patch(
                "dual_codex.providers.api_adapter"
            ) as api_adapter:
                with self.assertRaisesRegex(ValueError, "cannot serve the Architect role"):
                    delegate_to_configured_actor(
                        config=config,
                        role="architect",
                        task="Read the supplied task artifact and implement it.",
                        repository=config.repository,
                    )

            create_bootstrap.assert_not_called()
            api_adapter.assert_not_called()

    def test_restricted_claude_architect_is_rejected_before_bootstrap_or_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = self._config(root)
            accounts = dict(base.accounts)
            accounts["claude"] = AccountConfig(
                name="claude",
                label="Restricted Claude Code",
                codex_home=root / "claude-home",
                model="claude-sonnet",
                reasoning_effort="high",
                backend="claude_code",
                provider_type="anthropic",
                adapter_type="claude_code",
            )
            config = replace(
                base,
                accounts=accounts,
                roles={**base.roles, "architect": "claude"},
            )

            with patch("dual_codex.codex.create_canonical_bootstrap") as create_bootstrap, patch(
                "dual_codex.claude_code.run_claude_code"
            ) as run_claude:
                with self.assertRaisesRegex(ValueError, "Restricted Claude Code profiles cannot read"):
                    delegate_to_configured_actor(
                        config=config,
                        role="architect",
                        task="Read the supplied task artifact and implement it.",
                        repository=config.repository,
                    )

            create_bootstrap.assert_not_called()
            run_claude.assert_not_called()

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
                        "skills_loaded": ["ponytail"],
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
            architect_provenance = provenance["configured_actor_routing"][0]
            self.assertEqual(architect_provenance["canonical_bootstrap_selected_skills"], ["ponytail"])
            self.assertEqual(
                architect_provenance["canonical_bootstrap_skill_digests"]["ponytail"],
                hashlib.sha256(
                    (Path(self._instructions.name) / "skills" / "ponytail" / "SKILL.md").read_bytes()
                ).hexdigest(),
            )
            self.assertEqual(
                architect_provenance["canonical_bootstrap_skill_catalog"]["ponytail"],
                architect_provenance["canonical_bootstrap_skill_digests"]["ponytail"],
            )
            self.assertTrue(architect_provenance["canonical_bootstrap_skill_catalog_sha256"])
            self.assertIn("skills/ponytail/SKILL.md", architect_provenance["canonical_bootstrap_source_files"])

    def test_mission_dispatches_claude_roles_and_codex_executor_without_native_start(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = self._config(root)
            accounts = dict(base.accounts)
            accounts["claude"] = AccountConfig(
                name="claude",
                label="Claude",
                codex_home=root / "claude-home",
                model="claude-sonnet",
                reasoning_effort="high",
                backend="claude_code",
                provider_type="anthropic",
                adapter_type="claude_code",
            )
            accounts["codex-secundario"] = AccountConfig(
                name="codex-secundario",
                label="Codex Secundario",
                codex_home=root / "codex-secundario-home",
                model="gpt-5.6-luna",
                reasoning_effort="medium",
                backend="app_server",
                provider_type="codex",
                adapter_type="codex_cli",
            )
            config = replace(
                base,
                accounts=accounts,
                roles={
                    "orchestrator": "orchestrator",
                    "architect": "codex-secundario",
                    "reviewer": "claude",
                    "executor": "codex-secundario",
                },
            )
            config.repository.mkdir()
            subprocess.run(["git", "init", "--quiet"], cwd=config.repository, check=True)
            task = root / "task.md"
            task.write_text("exercise configured provider routing", encoding="utf-8")
            claude_roles: list[tuple[str, str, str]] = []
            codex_roles: list[tuple[str, str, str]] = []

            reports = {
                "architect": {
                    "summary": "plan",
                    "steps": [],
                    "acceptance_criteria": [],
                    "risks": [],
                    "files_to_inspect": [],
                    "skills_loaded": ["ponytail"],
                },
                "executor": {
                    "summary": "implementation",
                    "files_changed": [],
                    "commands_run": [],
                    "tests": [],
                    "remaining_issues": [],
                },
                "reviewer": {"verdict": "approved", "summary": "approved", "findings": []},
            }

            def fake_claude(**kwargs):
                role = kwargs["role"]
                agent = kwargs["agent"]
                claude_roles.append((role, agent.account_name, agent.backend))
                kwargs["output_path"].write_text(json.dumps(reports[role]), encoding="utf-8")
                return CommandResult(["claude"], 0, "", "", {"session_id": f"claude-{role}"})

            def fake_codex_app_server(**kwargs):
                role = kwargs["role"]
                agent = kwargs["agent"]
                codex_roles.append((role, agent.account_name, agent.backend))
                kwargs["output_path"].write_text(json.dumps(reports[role]), encoding="utf-8")
                return CommandResult(["codex", "app-server"], 0, "", "")

            with patch("dual_codex.claude_code.run_claude_code", side_effect=fake_claude) as claude, patch(
                "dual_codex.codex.run_codex_app_server", side_effect=fake_codex_app_server
            ) as codex, patch(
                "dual_codex.codex.run_codex_terminal", side_effect=AssertionError("unexpected native Codex dispatch")
            ) as terminal, patch(
                "dual_codex.terminal.TerminalManager.start",
                side_effect=AssertionError("Claude must not reach native terminal startup"),
            ) as native_start:
                outcome = execute(config, task)

            self.assertEqual(outcome.verdict, "approved")
            self.assertEqual(
                claude_roles,
                [("reviewer", "claude", "claude_code")],
            )
            self.assertEqual(claude.call_count, 1)
            self.assertEqual(
                codex_roles,
                [("architect", "codex-secundario", "app_server"), ("executor", "codex-secundario", "app_server")],
            )
            self.assertEqual(codex.call_count, 2)
            terminal.assert_not_called()
            native_start.assert_not_called()
            observed_provenance = [
                (item["role"], item["actor_id"], item["backend"], item["fallback_used"])
                for item in outcome.phase_provenance
            ]
            self.assertEqual(
                observed_provenance,
                [
                    ("architect", "codex-secundario", "app_server", False),
                    ("executor", "codex-secundario", "app_server", False),
                    ("reviewer", "claude", "claude_code", False),
                ],
            )

    def test_cli_run_bootstrap_routes_configured_topology_without_terminal_role_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = self._config(root)
            accounts = dict(base.accounts)
            accounts["biel3"] = AccountConfig(
                name="biel3",
                label="biel3",
                codex_home=root / "biel3-home",
                model="gpt-primary",
                reasoning_effort="high",
                backend="windows",
                provider_type="codex",
                adapter_type="codex_cli",
            )
            accounts["claude"] = AccountConfig(
                name="claude",
                label="Claude",
                codex_home=root / "claude-home",
                model="claude-sonnet",
                reasoning_effort="high",
                backend="claude_code",
                provider_type="anthropic",
                adapter_type="claude_code",
            )
            accounts["codex-secundario"] = AccountConfig(
                name="codex-secundario",
                label="Codex Secundario",
                codex_home=root / "codex-secundario-home",
                model="gpt-5.6-luna",
                reasoning_effort="medium",
                backend="app_server",
                provider_type="codex",
                adapter_type="codex_cli",
            )
            config = replace(
                base,
                accounts=accounts,
                roles={
                    "orchestrator": "biel3",
                    "architect": "biel3",
                    "reviewer": "claude",
                    "executor": "codex-secundario",
                },
            )
            config.repository.mkdir()
            subprocess.run(["git", "init", "--quiet"], cwd=config.repository, check=True)
            task = root / "task.md"
            task.write_text("exercise the configured mission entry point", encoding="utf-8")
            route_calls: list[tuple[str, str, str]] = []
            reports = {
                "architect": {
                    "summary": "plan",
                    "steps": [],
                    "acceptance_criteria": [],
                    "risks": [],
                    "files_to_inspect": [],
                    "skills_loaded": ["ponytail"],
                },
                "executor": {
                    "summary": "implementation",
                    "files_changed": [],
                    "commands_run": [],
                    "tests": [],
                    "remaining_issues": [],
                },
                "reviewer": {"verdict": "approved", "summary": "approved", "findings": []},
            }

            def fake_codex_terminal(**kwargs):
                role = kwargs["role"]
                agent = kwargs["agent"]
                if agent.backend != "windows":
                    raise AssertionError("Non-Windows configured roles must not use the native Codex adapter")
                route_calls.append((role, agent.account_name, agent.backend))
                kwargs["output_path"].write_text(json.dumps(reports[role]), encoding="utf-8")
                return CommandResult(["codex", "terminal"], 0, "", "")

            def fake_claude(**kwargs):
                role = kwargs["role"]
                agent = kwargs["agent"]
                route_calls.append((role, agent.account_name, agent.backend))
                kwargs["output_path"].write_text(json.dumps(reports[role]), encoding="utf-8")
                return CommandResult(["claude"], 0, "", "", {"claude_session_id": "claude-review"})

            def fake_app_server(**kwargs):
                role = kwargs["role"]
                agent = kwargs["agent"]
                route_calls.append((role, agent.account_name, agent.backend))
                kwargs["output_path"].write_text(json.dumps(reports[role]), encoding="utf-8")
                return CommandResult(["codex", "app-server"], 0, "", "")

            with patch("dual_codex.cli.load_config", return_value=config), patch(
                "dual_codex.codex.run_codex_terminal", side_effect=fake_codex_terminal
            ), patch("dual_codex.claude_code.run_claude_code", side_effect=fake_claude) as claude_adapter, patch(
                "dual_codex.codex.run_codex_app_server", side_effect=fake_app_server
            ) as app_server_adapter, patch("dual_codex.cli.TerminalManager") as terminal_manager, patch(
                "dual_codex.terminal.TerminalManager.start",
                side_effect=AssertionError("The configured Claude reviewer must never reach native terminal startup"),
            ) as native_terminal_start, patch(
                "dual_codex.terminal.TerminalManager.list",
                return_value=[
                    {"session_id": "biel4-stale", "account": "biel4", "role": "architect", "state": "exited"}
                ],
            ) as native_terminal_list, redirect_stdout(StringIO()):
                # A historical biel4 row must not be consulted as role configuration.
                terminal_manager.return_value.list.return_value = [
                    {"session_id": "biel4-stale", "account": "biel4", "role": "architect", "state": "exited"}
                ]
                terminal_manager.return_value.start.side_effect = AssertionError(
                    "The configured Claude reviewer must never reach native terminal startup"
                )
                result = cli_main(["--config", str(config.config_path), "run", str(task)])

            self.assertEqual(result, 0)
            self.assertEqual(
                route_calls,
                [
                    ("architect", "biel3", "windows"),
                    ("executor", "codex-secundario", "app_server"),
                    ("reviewer", "claude", "claude_code"),
                ],
            )
            claude_adapter.assert_called_once()
            app_server_adapter.assert_called_once()
            terminal_manager.return_value.start.assert_not_called()
            terminal_manager.return_value.list.assert_not_called()
            native_terminal_start.assert_not_called()
            native_terminal_list.assert_not_called()
            run_dirs = [path for path in config.runs_dir.iterdir() if path.is_dir()]
            self.assertEqual(len(run_dirs), 1)
            provenance = json.loads((run_dirs[0] / "provenance.json").read_text(encoding="utf-8"))
            self.assertEqual(
                [
                    (item["role"], item["actor_id"], item["backend"], item["actual_actor"], item["fallback_used"])
                    for item in provenance["configured_actor_routing"]
                ],
                [
                    ("architect", "biel3", "windows", "biel3", False),
                    ("executor", "codex-secundario", "app_server", "codex-secundario", False),
                    ("reviewer", "claude", "claude_code", "claude", False),
                ],
            )

    def test_unsupported_provider_role_pairs_fail_closed_before_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self._config(root)
            repository = root / "repository"
            repository.mkdir()
            antigravity_agent = replace(config.agent_for_role("executor"), backend="antigravity")
            api_agent = replace(config.agent_for_role("executor"), backend="api")

            with patch("dual_codex.antigravity.run_antigravity") as antigravity, patch(
                "dual_codex.providers.api_adapter"
            ) as api_adapter, patch("dual_codex.codex.run_codex_terminal") as terminal:
                with self.assertRaisesRegex(ValueError, "reserved for the Executor role"):
                    run_codex_for_role(
                        config=object(), agent=antigravity_agent, role="reviewer", repository=repository,
                        prompt="review", output_path=root / "review.json", schema_path=root / "schema.json",
                    )
                with self.assertRaisesRegex(ValueError, "do not provide the workspace-write Executor role"):
                    run_codex_for_role(
                        config=object(), agent=api_agent, role="executor", repository=repository,
                        prompt="implement", output_path=root / "implementation.json", schema_path=root / "schema.json",
                    )

            antigravity.assert_not_called()
            api_adapter.assert_not_called()
            terminal.assert_not_called()

    def test_native_terminal_start_rejects_claude_before_manager_start(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = self._config(root)
            accounts = dict(base.accounts)
            accounts["claude"] = AccountConfig(
                name="claude",
                label="Claude",
                codex_home=root / "claude-home",
                model="claude-sonnet",
                reasoning_effort="high",
                backend="claude_code",
                provider_type="anthropic",
                adapter_type="claude_code",
            )
            config = replace(
                base,
                accounts=accounts,
                roles={**base.roles, "reviewer": "claude"},
            )

            error = StringIO()
            with redirect_stderr(error), patch("dual_codex.cli.load_config", return_value=config), patch(
                "dual_codex.cli.TerminalManager"
            ) as terminal_manager:
                result = cli_main(
                    [
                        "--config",
                        str(root / "config.toml"),
                        "terminal",
                        "start",
                        "claude",
                        "--role",
                        "reviewer",
                    ]
                )

            self.assertEqual(result, 1)
            self.assertIn("requires the 'windows' backend", error.getvalue())
            terminal_manager.return_value.start.assert_not_called()

    def test_codex_app_server_can_be_primary_executor(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self._config(root, executor="secondary")
            repository = root / "repository"
            repository.mkdir()
            with patch("dual_codex.codex.run_codex_app_server", return_value=CommandResult(["codex"], 0, "", "")) as app_server:
                result = delegate_to_configured_actor(
                    config=config, role="executor", task="write", repository=repository,
                    output_path=root / "result.json", schema_path=root / "schema.json",
                )
            self.assertEqual(result.metadata["actual_actor"], "secondary")
            self.assertFalse(result.metadata["fallback_used"])
            self.assertEqual(app_server.call_args.kwargs["agent"].backend, "app_server")

    def test_configured_dispatch_preserves_provider_session_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self._config(root)
            repository = root / "repository"
            repository.mkdir()

            def fake_runner(**kwargs):
                kwargs["output_path"].write_text(json.dumps({"skills_loaded": ["ponytail"]}), encoding="utf-8")
                return CommandResult(
                    ["fake"],
                    0,
                    "",
                    "",
                    {"session_id": "provider-session", "runtime_version": "provider-runtime"},
                )

            with patch("dual_codex.codex.run_codex_for_role", side_effect=fake_runner):
                result = delegate_to_configured_actor(
                    config=config,
                    role="architect",
                    task="return a handshake",
                    repository=repository,
                    output_path=root / "result.json",
                    schema_path=root / "schema.json",
                )

            self.assertEqual(result.metadata["session_id"], "provider-session")
            self.assertEqual(result.metadata["runtime_version"], "provider-runtime")
            self.assertEqual(result.metadata["actual_actor"], "secondary")
            self.assertFalse(result.metadata["fallback_used"])

    def test_default_plan_schemas_keep_architect_skill_provenance_role_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self._config(root)
            repository = root / "repository"
            repository.mkdir()
            seen_schemas = {}

            def fake_runner(**kwargs):
                seen_schemas[kwargs["role"]] = kwargs["schema_path"]
                payload = {"skills_loaded": ["ponytail"]} if kwargs["role"] == "architect" else {}
                kwargs["output_path"].write_text(json.dumps(payload), encoding="utf-8")
                return CommandResult(["fake"], 0, "", "")

            with patch("dual_codex.codex.run_codex_for_role", side_effect=fake_runner):
                for role in ("architect", "orchestrator"):
                    delegate_to_configured_actor(
                        config=config,
                        role=role,
                        task="return a plan",
                        repository=repository,
                        output_path=root / f"{role}.json",
                    )

            schema_root = config.project_root / "schemas"
            self.assertEqual(seen_schemas["architect"], schema_root / "architect-plan.schema.json")
            self.assertEqual(seen_schemas["orchestrator"], schema_root / "plan.schema.json")

    def test_one_role_scoped_fallback_is_selected_and_provenanced(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = self._config(root)
            accounts = dict(base.accounts)
            accounts["secondary"] = replace(accounts["secondary"], fallback_roles=("executor",))
            config = replace(base, accounts=accounts, fallback_enabled=True)
            repository = root / "repository"
            repository.mkdir()

            def fake_runner(**kwargs):
                if kwargs["agent"].account_name == "executor-b":
                    raise ActorAvailabilityError("provider unavailable", failure_class="provider_unavailable", actor="executor-b")
                return CommandResult(["fake"], 0, "", "")

            with patch("dual_codex.codex.run_codex_for_role", side_effect=fake_runner):
                result = delegate_to_configured_actor(
                    config=config, role="executor", task="write", repository=repository,
                    output_path=root / "result.json", schema_path=root / "schema.json",
                )
            self.assertEqual(result.metadata["primary_actor"], "executor-b")
            self.assertEqual(result.metadata["actual_actor"], "secondary")
            self.assertTrue(result.metadata["fallback_used"])
            self.assertEqual(result.metadata["failed_actor"], "executor-b")
            self.assertEqual(result.metadata["fallback_failure_class"], "provider_unavailable")

    def test_fallback_candidate_order_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = self._config(root, executor="executor-d")
            accounts = dict(base.accounts)
            accounts["executor-b"] = replace(accounts["executor-b"], fallback_roles=("executor",))
            accounts["secondary"] = replace(accounts["secondary"], fallback_roles=("executor",))
            config = replace(base, accounts=accounts, fallback_enabled=True)
            repository = root / "repository"
            repository.mkdir()
            observed: list[str] = []

            def fake_runner(**kwargs):
                observed.append(kwargs["agent"].account_name)
                if kwargs["agent"].account_name == "executor-d":
                    raise ActorAvailabilityError(
                        "provider unavailable",
                        failure_class="provider_unavailable",
                        actor="executor-d",
                    )
                return CommandResult(["fake"], 0, "", "")

            with patch("dual_codex.providers.provider_supports_role", return_value=True), patch(
                "dual_codex.codex.run_codex_for_role", side_effect=fake_runner
            ):
                result = delegate_to_configured_actor(
                    config=config,
                    role="executor",
                    task="write",
                    repository=repository,
                    output_path=root / "result.json",
                    schema_path=root / "schema.json",
                )
            self.assertEqual(observed, ["executor-d", "executor-b"])
            self.assertEqual(result.metadata["actual_actor"], "executor-b")

    def test_fallback_candidate_capability_filter_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = self._config(root, executor="executor-d")
            accounts = dict(base.accounts)
            accounts["executor-b"] = replace(accounts["executor-b"], fallback_roles=("executor",))
            accounts["secondary"] = replace(accounts["secondary"], fallback_roles=("executor",))
            config = replace(base, accounts=accounts, fallback_enabled=True)
            repository = root / "repository"
            repository.mkdir()
            observed: list[str] = []

            def fake_runner(**kwargs):
                observed.append(kwargs["agent"].account_name)
                if kwargs["agent"].account_name == "executor-d":
                    raise ActorAvailabilityError(
                        "provider unavailable",
                        failure_class="provider_unavailable",
                        actor="executor-d",
                    )
                return CommandResult(["fake"], 0, "", "")

            def capability(config, account, role):
                del config, role
                return account.name != "executor-b"

            with patch("dual_codex.providers.provider_supports_role", side_effect=capability), patch(
                "dual_codex.codex.run_codex_for_role", side_effect=fake_runner
            ):
                result = delegate_to_configured_actor(
                    config=config,
                    role="executor",
                    task="write",
                    repository=repository,
                    output_path=root / "result.json",
                    schema_path=root / "schema.json",
                )
            self.assertEqual(observed, ["executor-d", "secondary"])
            self.assertEqual(result.metadata["actual_actor"], "secondary")

    def test_security_denial_never_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = self._config(root, executor="secondary")
            accounts = dict(base.accounts)
            accounts["executor-b"] = replace(accounts["executor-b"], fallback_roles=("executor",))
            config = replace(base, accounts=accounts, fallback_enabled=True)
            repository = root / "repository"
            repository.mkdir()
            denied = CommandResult(
                ["codex", "app-server"],
                1,
                "",
                "writing outside of the project is blocked by policy",
            )
            self.assertIsNone(classify_actor_failure(denied, backend="app_server"))
            with patch("dual_codex.codex.run_codex_app_server", return_value=denied) as app_server:
                with self.assertRaisesRegex(CommandError, "Codex executor failed"):
                    delegate_to_configured_actor(
                        config=config,
                        role="executor",
                        task="write",
                        repository=repository,
                        output_path=root / "result.json",
                        schema_path=root / "schema.json",
                    )
            self.assertEqual(app_server.call_count, 1)

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
                if kwargs["role"] == "architect":
                    kwargs["output_path"].write_text(
                        json.dumps({"skills_loaded": ["ponytail"]}), encoding="utf-8"
                    )
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
            observed: dict[str, object] = {}

            def fake_app_server(**kwargs):
                observed.update(kwargs)
                self.assertIn("BEGIN CANONICAL BOOTSTRAP SNAPSHOT", kwargs["prompt"])
                self.assertIn("inline AGENTS.md below is authoritative", kwargs["prompt"])
                self.assertNotIn("Read C:\\CodexGlobal", kwargs["prompt"])
                self.assertNotIn("Read the complete ephemeral bootstrap artifact", kwargs["prompt"])
                self.assertNotIn("Get-Content", kwargs["prompt"])
                self.assertIn("canonical_source_path:", kwargs["prompt"])
                self.assertIn("first read only the supplied task/architect artifact", kwargs["prompt"])
                self.assertIn("Do not ask the user to choose or identify skills", kwargs["prompt"])
                self.assertIn("No skills were preselected by the control plane", kwargs["prompt"])
                self.assertNotIn("# memory", kwargs["prompt"])
                self.assertNotIn("# project-security-review", kwargs["prompt"])
                kwargs["output_path"].write_text(
                    json.dumps({"skills_loaded": ["ponytail"]}), encoding="utf-8"
                )
                return expected

            with patch("dual_codex.codex.run_codex_app_server", side_effect=fake_app_server) as app_server:
                result = run_codex_for_role(
                    config=config,
                    role="architect",
                    repository=repository,
                    prompt="inspect",
                    output_path=root / "plan.json",
                    schema_path=root / "schema.json",
                )
            prompt = observed["prompt"]
            self.assertIn(BOOTSTRAP_MARKER, prompt)
            self.assertIn("AGENTS.md", prompt)
            self.assertIn("skills", prompt)
            self.assertTrue(result.metadata["configured_actor"])
            self.assertEqual(result.metadata["actor_id"], "secondary")
            self.assertEqual(result.metadata["provider"], "codex")
            self.assertEqual(result.metadata["backend"], "app_server")
            self.assertEqual(result.metadata["delegation_transport"], "app_server")
            self.assertTrue(result.metadata["canonical_bootstrap_required"])
            self.assertEqual(result.metadata["canonical_bootstrap_mechanism"], "ephemeral-run-artifact")
            self.assertTrue(result.metadata["canonical_bootstrap_source_sha256"])
            self.assertTrue(result.metadata["canonical_bootstrap_artifact_sha256"])
            self.assertEqual(
                result.metadata["canonical_bootstrap_delivery"],
                "trusted_inline",
            )
            self.assertEqual(
                result.metadata["canonical_bootstrap_selected_skills"],
                ["ponytail"],
            )
            self.assertNotIn(r"C:\CodexGlobal", result.command)
            self.assertFalse(Path(result.metadata["canonical_bootstrap_artifact"]).exists())
            app_server.assert_called_once()

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

    def test_reviewer_uses_the_same_ephemeral_canonical_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self._config(root)
            repository = root / "repository"
            repository.mkdir()
            observed: dict[str, object] = {}

            def fake_app_server(**kwargs):
                observed.update(kwargs)
                self.assertIn("authoritative canonical bootstrap", kwargs["prompt"])
                self.assertNotIn("Read C:\\CodexGlobal", kwargs["prompt"])
                return CommandResult(["codex", "app-server"], 0, "", "")

            with patch("dual_codex.codex.run_codex_app_server", side_effect=fake_app_server):
                result = run_codex_for_role(
                    config=config,
                    role="reviewer",
                    repository=repository,
                    prompt="review",
                    output_path=root / "review.json",
                    schema_path=root / "schema.json",
                )
            self.assertEqual(result.metadata["actor_id"], "secondary")
            self.assertEqual(result.metadata["canonical_bootstrap_mechanism"], "ephemeral-run-artifact")
            self.assertEqual(result.metadata["canonical_bootstrap_source"], "machine-wide")
            self.assertTrue(result.metadata["canonical_bootstrap_source_sha256"])
            self.assertEqual(result.metadata["canonical_bootstrap_delivery"], "trusted_inline")
            self.assertTrue(observed)

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
