from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dual_codex.claude_code import (
    ClaudeCodeAdapter,
    build_command,
    capability_snapshot,
    claude_environment,
    claude_status,
    run_claude_code,
)
from dual_codex.codex import ActorAvailabilityError, classify_actor_failure, delegate_to_configured_actor, run_codex_for_role
from dual_codex.config import AccountConfig, AgentConfig, OrchestratorConfig, load_config
from dual_codex.process import CommandError, CommandResult
from dual_codex.providers import provider_capabilities, provider_supports_role


HELP = """
--print --output-format --json-schema --permission-mode --permission-prompts
--tools --resume --safe-mode --restricted --model <model> opus sonnet haiku fable
--effort <level>
  Effort level for the current session
  (low, medium, high, xhigh, max)
"""


class ClaudeCodeTests(unittest.TestCase):
    def _config(self, root: Path) -> OrchestratorConfig:
        return OrchestratorConfig(
            repository=root,
            runs_dir=root / "runs",
            max_correction_cycles=1,
            require_clean_git=False,
            codex_command="codex",
            claude_command="claude",
            accounts={},
            roles={},
            project_root=root,
            config_path=root / "config.toml",
            claude_turn_timeout=5,
        )

    def _agent(self, root: Path, *, role: str = "reviewer", model: str = "sonnet", auth_mode: str = "provider_native") -> AgentConfig:
        return AgentConfig(
            codex_home=root / "claude-state",
            state_root=root / "claude-state",
            model=model,
            reasoning_effort="high",
            sandbox="workspace-write" if role == "executor" else "read-only",
            account_name="claude",
            backend="claude_code",
            provider_type="anthropic",
            adapter_type="claude_code",
            auth_mode=auth_mode,
            auth_reference="env:TEST_ANTHROPIC_KEY" if auth_mode == "environment" else "",
            supported_reasoning_efforts=("low", "medium", "high", "max"),
        )

    def _snapshot(self, agent: AgentConfig, role: str | None = None) -> dict:
        return {
            "available": True,
            "error": None,
            "help": HELP,
            "roles": ("architect", "reviewer", "executor"),
            "efforts": ("low", "medium", "high", "max"),
            "models": [{"id": name} for name in ("sonnet", "opus", "haiku", "fable")],
            "runtime_version": "2.1.211 (Claude Code)",
        }

    def test_profile_persistence_and_secret_free_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "repo").mkdir()
            path = root / "config.toml"
            path.write_text(
                """[orchestrator]\nrepository = \"repo\"\nclaude_command = \"claude\"\n\n[accounts.claude]\nlabel = \"Claude\"\nbackend = \"claude_code\"\ncodex_home = \"profiles/claude\"\nprovider_type = \"anthropic\"\nadapter_type = \"claude_code\"\nauth_mode = \"environment\"\nauth_reference = \"env:TEST_ANTHROPIC_KEY\"\nmodel = \"sonnet\"\nreasoning_effort = \"high\"\n\n[roles]\narchitect = \"claude\"\nreviewer = \"claude\"\nexecutor = \"claude\"\n""",
                encoding="utf-8",
            )
            config = load_config(path)
            account = config.accounts["claude"]
            self.assertEqual(account.provider_type, "anthropic")
            self.assertEqual(account.adapter_type, "claude_code")
            self.assertEqual(config.roles["executor"], "claude")
            serialized = path.read_text(encoding="utf-8")
            self.assertIn("env:TEST_ANTHROPIC_KEY", serialized)
            self.assertNotIn("secret-value", serialized)
            self.assertNotIn("credentials.json", serialized)

    def test_environment_isolation_does_not_copy_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            agent = self._agent(root, auth_mode="environment")
            with patch.dict(
                "os.environ",
                {"TEST_ANTHROPIC_KEY": "secret-value", "UNRELATED_TOKEN": "other-secret", "CLAUDE_CODE_SIMPLE": "1", "claude_code_simple": "1"},
                clear=False,
            ):
                env = claude_environment(agent, repo)
            self.assertEqual(env["CLAUDE_CONFIG_DIR"], str((root / "claude-state").resolve()))
            self.assertEqual(env["ANTHROPIC_API_KEY"], "secret-value")
            self.assertNotIn("TEST_ANTHROPIC_KEY", env)
            self.assertNotIn("UNRELATED_TOKEN", env)
            self.assertNotIn("CLAUDE_CODE_SIMPLE", env)
            self.assertNotIn("claude_code_simple", env)
            self.assertNotIn("secret-value", json.dumps({"profile": agent.account_name, "auth_mode": agent.auth_mode}))

    def test_managed_launch_uses_safe_mode_not_bare_and_keeps_oauth_auth(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            command = build_command(
                command="claude",
                agent=self._agent(root),
                role="reviewer",
                prompt="inspect",
                schema='{"type":"object"}',
                help_text=HELP,
            )
            self.assertIn("--safe-mode", command)
            self.assertIn("--restricted", command)
            self.assertNotIn("--bare", command)
            self.assertNotIn("CLAUDE_CODE_SIMPLE=1", command)
            self.assertEqual(command[command.index("--tools") + 1], "Read,Glob,Grep")
            self.assertEqual(command[command.index("--permission-mode") + 1], "plan")
            self.assertEqual(command[command.index("--permission-prompts") + 1], "none")

    def test_capability_detection_requires_safe_mode_and_restricted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            agent = self._agent(root)
            for flag in ("--safe-mode", "--restricted"):
                help_text = HELP.replace(flag, "")
                with patch("dual_codex.claude_code._help_text", return_value=(help_text, None)):
                    snapshot = capability_snapshot("claude", cwd=root, account=agent)
                self.assertEqual(snapshot["roles"], ())
                self.assertIn(flag, snapshot["error"])

    def test_capability_detection_does_not_accept_similar_flag_names(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            agent = self._agent(root)
            help_text = HELP.replace("--safe-mode", "--safe-mode-preview")
            with patch("dual_codex.claude_code._help_text", return_value=(help_text, None)):
                snapshot = capability_snapshot("claude", cwd=root, account=agent)
            self.assertEqual(snapshot["roles"], ())
            self.assertIn("--safe-mode", snapshot["error"])

    def test_capabilities_and_roles_are_runtime_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            agent = self._agent(root)
            with patch("dual_codex.claude_code._help_text", return_value=(HELP, None)), patch(
                "dual_codex.claude_code._runtime_version", return_value="2.1.211 (Claude Code)"
            ):
                snapshot = capability_snapshot("claude", cwd=root, account=agent)
            self.assertEqual(snapshot["roles"], ("architect", "reviewer", "executor"))
            self.assertEqual(snapshot["efforts"], ("low", "medium", "high", "max"))
            self.assertNotIn("dangerously-skip-permissions", snapshot["help"])

    def test_model_capabilities_are_discovered_from_installed_help(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            agent = self._agent(root)
            help_text = HELP.replace("haiku", "")
            with patch("dual_codex.claude_code._help_text", return_value=(help_text, None)), patch(
                "dual_codex.claude_code._runtime_version", return_value="2.1.268"
            ):
                snapshot = capability_snapshot("claude", cwd=root, account=agent)
            self.assertEqual([row["id"] for row in snapshot["models"]], ["sonnet", "opus", "fable"])
            self.assertNotIn("haiku", {row["id"] for row in snapshot["models"]})

    def test_effort_choices_are_parsed_from_multiline_cli_help(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            agent = self._agent(root)
            help_text = HELP.replace(
                "--effort <level> (low, medium, high, max)",
                "--effort <level>\n  Effort level for the current session\n  (low, medium, high, xhigh, max)",
            )
            with patch("dual_codex.claude_code._help_text", return_value=(help_text, None)), patch(
                "dual_codex.claude_code._runtime_version", return_value="2.1.268"
            ):
                snapshot = capability_snapshot("claude", cwd=root, account=agent)
            self.assertEqual(snapshot["efforts"], ("low", "medium", "high", "max"))

    def test_native_windows_executor_is_file_edit_only_without_os_sandbox(self) -> None:
        if os.name != "nt":
            self.skipTest("native Windows sandbox classification only applies on Windows")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            agent = self._agent(root, role="executor")
            with patch("dual_codex.claude_code._help_text", return_value=(HELP, None)):
                snapshot = capability_snapshot("claude", cwd=root, account=agent, role="executor")
            self.assertIn("executor", snapshot["roles"])
            self.assertIsNone(snapshot["error"])

    def test_executor_allowlist_has_no_command_or_network_tools(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            command = build_command(
                command="claude",
                agent=self._agent(root, role="executor"),
                role="executor",
                prompt="edit",
                schema="{}",
                help_text=HELP,
            )
            tools = command[command.index("--tools") + 1].split(",")
            self.assertEqual(tools, ["Edit", "Write", "Read", "Glob", "Grep"])
            self.assertNotIn("Bash", tools)
            self.assertNotIn("PowerShell", tools)
            self.assertNotIn("WebFetch", tools)
            self.assertNotIn("MCP", tools)

    def test_executor_capability_fails_without_restricted_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            agent = self._agent(root, role="executor")
            help_text = HELP.replace("--restricted", "")
            with patch("dual_codex.claude_code._help_text", return_value=(help_text, None)), patch(
                "dual_codex.claude_code._runtime_version", return_value="2.1.211"
            ):
                snapshot = capability_snapshot("claude", cwd=root, account=agent, role="executor")
            self.assertIn("--restricted", snapshot["error"])

    def test_read_roles_fail_closed_without_unattended_permission_control(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            agent = self._agent(root)
            help_text = HELP.replace("--permission-prompts", "")
            with patch("dual_codex.claude_code._help_text", return_value=(help_text, None)):
                snapshot = capability_snapshot("claude", cwd=root, account=agent)
            self.assertEqual(snapshot["roles"], ())
            self.assertIn("--permission-prompts", snapshot["error"])

    def test_environment_auth_status_uses_child_mapping_without_serializing_secret(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            agent = self._agent(root, auth_mode="environment")
            with patch.dict("os.environ", {"TEST_ANTHROPIC_KEY": "secret-value"}, clear=False), patch(
                "dual_codex.claude_code.claude_environment", return_value={"ANTHROPIC_API_KEY": "secret-value"}
            ) as child_env, patch("dual_codex.claude_code._resolve_command", return_value="claude"):
                self.assertEqual(claude_status("claude", cwd=repo, account=agent), "OK")
            child_env.assert_called_once()
            self.assertNotIn("TEST_ANTHROPIC_KEY", json.dumps({"profile": agent.account_name, "auth_mode": agent.auth_mode}))

    def test_doctor_not_signed_in_wording_is_typed_as_auth_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            completed = type("Completed", (), {"returncode": 0, "stdout": "Not signed in to claude.ai", "stderr": ""})()
            with patch("dual_codex.claude_code._resolve_command", return_value="claude"), patch(
                "dual_codex.claude_code._run_capture", return_value=completed
            ):
                self.assertEqual(claude_status("claude", cwd=repo, account=self._agent(root)), "NOT LOGGED IN")

    def test_auth_status_json_marks_authenticated_profile_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            completed = type("Completed", (), {"returncode": 0, "stdout": '{"loggedIn": true, "authMethod": "claude.ai"}', "stderr": ""})()
            with patch("dual_codex.claude_code._resolve_command", return_value="claude"), patch(
                "dual_codex.claude_code._run_capture", return_value=completed
            ) as capture:
                self.assertEqual(claude_status("claude", cwd=repo, account=self._agent(root)), "OK")
            self.assertEqual(capture.call_args.args[0][1:4], ["auth", "status", "--json"])

    def test_provider_native_auth_missing_fails_before_model_call(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            schema = root / "schema.json"
            schema.write_text('{"type":"object"}', encoding="utf-8")
            agent = self._agent(root)
            config = self._config(root)
            with patch("dual_codex.claude_code.capability_snapshot", return_value=self._snapshot(agent)), patch(
                "dual_codex.claude_code.claude_status", return_value="NOT LOGGED IN"
            ), patch("dual_codex.claude_code.subprocess.run") as run:
                result = run_claude_code(
                    command="claude",
                    agent=agent,
                    role="reviewer",
                    repository=repo,
                    prompt="inspect",
                    output_path=root / "out.json",
                    schema_path=schema,
                    config=config,
                )
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.metadata["availability_failure_class"], "authentication_unavailable")
            run.assert_not_called()

    def test_profile_state_root_cannot_be_repository_or_global_policy_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            same = self._agent(repo)
            with self.assertRaises(ValueError):
                claude_environment(same, repo)
            state = root / "claude-state"
            state.mkdir()
            nested = state / "nested-repo"
            nested.mkdir()
            with self.assertRaises(ValueError):
                claude_environment(self._agent(root), nested)

    def test_protected_workspaces_fail_before_capability_or_model_call(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / "claude-state"
            state.mkdir()
            schema = root / "schema.json"
            schema.write_text('{"type":"object"}', encoding="utf-8")
            agent = self._agent(root)
            config = self._config(root)
            with patch("dual_codex.claude_code.capability_snapshot") as snapshot:
                result = run_claude_code(
                    command="claude",
                    agent=agent,
                    role="reviewer",
                    repository=state,
                    prompt="inspect",
                    output_path=root / "out.json",
                    schema_path=schema,
                    config=config,
                )
            self.assertEqual(result.returncode, 1)
            self.assertTrue(result.metadata["capability_failure"])
            snapshot.assert_not_called()

    def test_build_command_binds_role_permissions_and_never_bypasses_safety(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            reviewer = self._agent(root, role="reviewer")
            command = build_command(
                command="claude",
                agent=reviewer,
                role="reviewer",
                prompt="inspect",
                schema="{}",
                help_text=HELP,
            )
            self.assertIn("--permission-mode", command)
            self.assertEqual(command[command.index("--permission-mode") + 1], "plan")
            self.assertEqual(command[command.index("--tools") + 1], "Read,Glob,Grep")
            self.assertNotIn("--dangerously-skip-permissions", command)
            executor = self._agent(root, role="executor")
            executor_command = build_command(command="claude", agent=executor, role="executor", prompt="edit", schema="{}", help_text=HELP)
            self.assertEqual(executor_command[executor_command.index("--permission-mode") + 1], "acceptEdits")
            self.assertIn("--restricted", executor_command)

    def test_structured_result_captures_session_and_resumes_exact_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            schema = root / "schema.json"
            schema.write_text('{"type":"object"}', encoding="utf-8")
            config = self._config(root)
            agent = self._agent(root)
            payloads = [
                {"subtype": "success", "is_error": False, "session_id": "session-a", "result": "ok", "structured_output": {"ok": True}},
                {"subtype": "success", "is_error": False, "session_id": "session-b", "result": "ok", "structured_output": {"ok": True}},
            ]
            with patch("dual_codex.claude_code.capability_snapshot", side_effect=lambda *args, **kwargs: self._snapshot(agent)), patch(
                "dual_codex.claude_code.claude_environment", return_value={}
            ), patch("dual_codex.claude_code.claude_status", return_value="OK"), patch("dual_codex.claude_code.subprocess.run") as run:
                run.side_effect = [
                    type("Completed", (), {"returncode": 0, "stdout": json.dumps(payloads[0]), "stderr": ""})(),
                    type("Completed", (), {"returncode": 0, "stdout": json.dumps(payloads[1]), "stderr": ""})(),
                ]
                first = run_claude_code(command="claude", agent=agent, role="reviewer", repository=repo, prompt="inspect", output_path=root / "out.json", schema_path=schema, config=config)
                second = run_claude_code(command="claude", agent=agent, role="reviewer", repository=repo, prompt="continue", output_path=root / "out2.json", schema_path=schema, config=config)
            self.assertEqual(first.returncode, 0)
            self.assertEqual(first.metadata["claude_session_id"], "session-a")
            self.assertEqual(json.loads((root / "out.json").read_text(encoding="utf-8")), {"ok": True})
            self.assertEqual(second.returncode, 0)
            second_command = run.call_args_list[1].args[0]
            self.assertEqual(run.call_args_list[0].kwargs["cwd"], repo)
            self.assertNotIn("secret-value", json.dumps(first.metadata))
            self.assertIn("--resume", second_command)
            self.assertEqual(second_command[second_command.index("--resume") + 1], "session-a")
            self.assertNotIn("--continue", second_command)

    def test_blank_or_malformed_success_is_unusable_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            schema = root / "schema.json"
            schema.write_text('{"type":"object"}', encoding="utf-8")
            config = self._config(root)
            agent = self._agent(root)
            with patch("dual_codex.claude_code.capability_snapshot", return_value=self._snapshot(agent)), patch(
                "dual_codex.claude_code.claude_environment", return_value={}
            ), patch("dual_codex.claude_code.claude_status", return_value="OK"), patch("dual_codex.claude_code.subprocess.run") as run:
                run.return_value = type("Completed", (), {"returncode": 0, "stdout": "{}", "stderr": ""})()
                result = run_claude_code(command="claude", agent=agent, role="reviewer", repository=repo, prompt="inspect", output_path=root / "out.json", schema_path=schema, config=config)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.metadata["availability_failure_class"], "unusable_runtime")
            self.assertEqual(classify_actor_failure(result, backend="claude_code"), "unusable_runtime")

    def test_typed_auth_error_is_fallback_eligible_but_semantic_failure_is_not(self) -> None:
        auth = CommandResult(["claude"], 1, "", "Login expired", {"availability_failure_class": "authentication_unavailable"})
        semantic = CommandResult(["claude"], 1, "", "Tests failed", {})
        self.assertEqual(classify_actor_failure(auth, backend="claude_code"), "authentication_unavailable")
        self.assertIsNone(classify_actor_failure(semantic, backend="claude_code"))

    def test_provider_capabilities_and_role_support_fail_closed_when_cli_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self._config(root)
            agent = self._agent(root)
            account = type("Account", (), {**agent.__dict__, "name": "claude", "available_models": (), "supported_reasoning_efforts": ()})()
            config = OrchestratorConfig(**{**config.__dict__, "accounts": {"claude": account}})
            with patch("dual_codex.claude_code._help_text", return_value=("", "missing")):
                capabilities = provider_capabilities(config, account)
            self.assertEqual(capabilities.runtime_status, "Unavailable")
            self.assertFalse(provider_supports_role(config, account, "executor"))

    def test_dashboard_exposes_claude_profile_and_auth_controls(self) -> None:
        from dual_codex.dashboard import HTML, SCRIPT

        self.assertIn('value="claude_code">Anthropic Claude', HTML)
        self.assertIn("data-profile-auth-mode", HTML)
        self.assertIn("auth_mode:", SCRIPT)
        self.assertIn("body.backend==='claude_code'", SCRIPT)
        self.assertIn("backend?.value!=='claude_code'", SCRIPT)
        self.assertIn("body.auth_reference", SCRIPT)

    def test_dispatch_uses_configured_claude_actor_and_preserves_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            schema = root / "schema.json"
            schema.write_text('{"type":"object"}', encoding="utf-8")
            (root / "AGENTS.md").write_text("policy", encoding="utf-8")
            for skill in ("memory", "ponytail", "project-phase-review", "project-security-review"):
                skill_dir = root / "skills" / skill
                skill_dir.mkdir(parents=True)
                (skill_dir / "SKILL.md").write_text(skill, encoding="utf-8")
            agent = self._agent(root, role="reviewer")
            config = OrchestratorConfig(**{**self._config(root).__dict__, "accounts": {"claude": type("Account", (), {**agent.__dict__, "name": "claude", "label": "Claude", "enabled": True, "fallback_roles": ()})()}, "roles": {"reviewer": "claude"}})
            with patch("dual_codex.claude_code.capability_snapshot", return_value=self._snapshot(agent)), patch(
                "dual_codex.claude_code.claude_environment", return_value={}
            ), patch("dual_codex.claude_code.claude_status", return_value="OK"), patch("dual_codex.claude_code.subprocess.run") as run, patch("dual_codex.bootstrap.CANONICAL_INSTRUCTIONS_ROOT", root):
                run.return_value = type("Completed", (), {"returncode": 0, "stdout": json.dumps({"session_id": "session-c", "structured_output": {"ok": True}}), "stderr": ""})()
                result = run_codex_for_role(config=config, role="reviewer", repository=repo, prompt="inspect", output_path=root / "out.json", schema_path=schema)
            self.assertEqual(result.metadata["provider"], "anthropic")
            self.assertEqual(result.metadata["adapter"], "claude_code")
            self.assertEqual(result.metadata["session_id"], "session-c")
            self.assertEqual(result.metadata["role"], "reviewer")
            self.assertTrue(result.metadata["claude_safe_mode"])
            self.assertTrue(result.metadata["claude_restricted"])
            self.assertEqual(result.metadata["claude_tools"], "Read,Glob,Grep")
            launch = run.call_args.args[0]
            self.assertIn("BEGIN CANONICAL BOOTSTRAP SNAPSHOT", launch[-1])
            self.assertNotIn("--bare", launch)

    def test_claude_is_role_scoped_fallback_without_provider_substitution(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            (root / "AGENTS.md").write_text("policy", encoding="utf-8")
            for skill in ("memory", "ponytail", "project-phase-review", "project-security-review"):
                skill_dir = root / "skills" / skill
                skill_dir.mkdir(parents=True)
                (skill_dir / "SKILL.md").write_text(skill, encoding="utf-8")
            primary = AccountConfig(
                name="claude",
                label="Claude",
                codex_home=root / "claude-state",
                state_root=root / "claude-state",
                model="sonnet",
                reasoning_effort="high",
                backend="claude_code",
                provider_type="anthropic",
                adapter_type="claude_code",
            )
            fallback = AccountConfig(
                name="codex",
                label="Codex",
                codex_home=root / "codex-state",
                model="",
                reasoning_effort="high",
                backend="windows",
                provider_type="codex",
                adapter_type="codex_cli",
                fallback_roles=("reviewer",),
            )
            config = OrchestratorConfig(
                **{
                    **self._config(root).__dict__,
                    "accounts": {"claude": primary, "codex": fallback},
                    "roles": {"reviewer": "claude"},
                    "fallback_enabled": True,
                }
            )

            def fake_runner(**kwargs):
                if kwargs["agent"].account_name == "claude":
                    raise ActorAvailabilityError("login expired", failure_class="authentication_unavailable", actor="claude")
                return CommandResult(["codex"], 0, "", "")

            with patch("dual_codex.codex.run_codex_for_role", side_effect=fake_runner), patch(
                "dual_codex.bootstrap.CANONICAL_INSTRUCTIONS_ROOT", root
            ):
                result = delegate_to_configured_actor(
                    config=config,
                    role="reviewer",
                    task="inspect",
                    repository=repo,
                    output_path=root / "out.json",
                    schema_path=root / "schema.json",
                )
            self.assertTrue(result.metadata["fallback_used"])
            self.assertEqual(result.metadata["primary_actor"], "claude")
            self.assertEqual(result.metadata["actual_actor"], "codex")
            self.assertEqual(result.metadata["fallback_failure_class"], "authentication_unavailable")


if __name__ == "__main__":
    unittest.main()
