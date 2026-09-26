from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import unittest
from unittest.mock import patch

from dual_codex.config import AgentConfig, OrchestratorConfig, load_config
from dual_codex.providers import (
    ProviderError,
    _parse_antigravity_models,
    api_adapter,
    provider_capabilities,
    resolve_antigravity_agent,
    supported_roles_for_backend,
)


class _CompletionHandler(BaseHTTPRequestHandler):
    payload: dict = {}
    authorization = ""

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        length = int(self.headers.get("Content-Length", "0"))
        self.__class__.payload = json.loads(self.rfile.read(length))
        self.__class__.authorization = self.headers.get("Authorization", "")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"choices":[{"message":{"content":"structured answer"}}]}')

    def log_message(self, *_args) -> None:
        return


class _ErrorHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        self.send_response(503)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"error":"upstream failure"}')

    def log_message(self, *_args) -> None:
        return


class ProviderTests(unittest.TestCase):
    _AGY_CATALOG = """Fetching available models...
gemini-3.8-flash-high\tGemini 3.8 Flash (High)
gemini-3.8-flash-medium\tGemini 3.8 Flash (Medium)
gemini-3.8-flash-low\tGemini 3.8 Flash (Low)
gemini-3.1-pro-high\tGemini 3.1 Pro (High)
gemini-3.1-pro-low\tGemini 3.1 Pro (Low)
claude-sonnet-4-6\tClaude Sonnet 4.6 (Thinking)
claude-opus-4-6-thinking\tClaude Opus 4.6 (Thinking)
gpt-oss-120b-medium\tGPT-OSS 120B (Medium)
other-family-high\tOther Family (High)
"""

    def _config(self, root: Path) -> OrchestratorConfig:
        return OrchestratorConfig(
            repository=root,
            runs_dir=root / "runs",
            max_correction_cycles=1,
            require_clean_git=False,
            codex_command="codex",
            accounts={},
            roles={},
            project_root=root,
            config_path=root / "config.toml",
        )

    def test_backend_role_matrix_matches_delegate_execution_modes(self) -> None:
        self.assertEqual(supported_roles_for_backend("antigravity"), ("executor",))
        self.assertIn("executor", supported_roles_for_backend("app_server"))
        self.assertNotIn("executor", supported_roles_for_backend("windows"))
        self.assertNotIn("executor", supported_roles_for_backend("claude_code"))

    def test_antigravity_catalog_normalizes_logical_models_and_exact_variants(self) -> None:
        rows = _parse_antigravity_models(self._AGY_CATALOG)
        self.assertEqual(
            [row["display_name"] for row in rows],
            [
                "Gemini 3.8 Flash",
                "Gemini 3.1 Pro",
                "Claude Sonnet 4.6",
                "Claude Opus 4.6",
                "GPT-OSS 120B",
                "Other Family",
            ],
        )
        flash = rows[0]
        self.assertEqual(flash["reasoning_efforts"], ["low", "medium", "high"])
        self.assertEqual(flash["runtime_variants"]["high"], "gemini-3.8-flash-high")
        self.assertEqual(rows[1]["reasoning_efforts"], ["low", "high"])
        self.assertEqual(rows[2]["fixed_mode"], "Thinking")
        self.assertEqual(rows[2]["reasoning_efforts"], [])
        self.assertEqual(rows[4]["fixed_mode"], "Medium")
        self.assertEqual(len(rows), 6)

    def test_antigravity_resolution_preserves_fixed_and_effort_runtime_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "config.toml"
            path.write_text(
                """[orchestrator]\nrepository = \".\"\nantigravity_command = \"agy\"\n\n[accounts.gemini]\nbackend = \"antigravity\"\ncodex_home = \"profiles/gemini\"\nmodel = \"gemini-3.8-flash\"\nreasoning_effort = \"high\"\n\n[roles]\nexecutor = \"gemini\"\n""",
                encoding="utf-8",
            )
            config = load_config(path)
            with patch("dual_codex.providers.subprocess.run") as run:
                run.return_value = type("Result", (), {"stdout": self._AGY_CATALOG, "stderr": "", "returncode": 0})()
                with patch("dual_codex.providers.antigravity_status", return_value="OK"):
                    resolved = resolve_antigravity_agent(config, config.executor)
            self.assertEqual(resolved.model, "gemini-3.8-flash")
            self.assertEqual(resolved.runtime_model, "gemini-3.8-flash-high")
            self.assertEqual(resolved.reasoning_effort, "high")

            fixed = config.accounts["gemini"]
            fixed = fixed.__class__(**{**fixed.__dict__, "model": "claude-sonnet-4-6", "reasoning_effort": "high", "runtime_model": ""})
            fixed_config = config.__class__(**{**config.__dict__, "accounts": {"gemini": fixed}})
            with patch("dual_codex.providers.subprocess.run") as run:
                run.return_value = type("Result", (), {"stdout": self._AGY_CATALOG, "stderr": "", "returncode": 0})()
                with patch("dual_codex.providers.antigravity_status", return_value="OK"):
                    resolved_fixed = resolve_antigravity_agent(fixed_config, fixed_config.executor)
            self.assertEqual(resolved_fixed.runtime_model, "claude-sonnet-4-6")
            self.assertEqual(resolved_fixed.fixed_mode, "Thinking")
            self.assertEqual(resolved_fixed.reasoning_effort, "")

    def test_antigravity_resolution_rejects_stale_variant_without_downgrade(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "config.toml"
            path.write_text(
                """[orchestrator]\nrepository = \".\"\n\n[accounts.gemini]\nbackend = \"antigravity\"\ncodex_home = \"profiles/gemini\"\nmodel = \"gemini-3.8-flash\"\nreasoning_effort = \"medium\"\n\n[roles]\nexecutor = \"gemini\"\n""",
                encoding="utf-8",
            )
            config = load_config(path)
            with patch("dual_codex.providers.subprocess.run") as run:
                run.return_value = type("Result", (), {"stdout": "gemini-3.8-flash-high\tGemini 3.8 Flash (High)\n", "stderr": "", "returncode": 0})()
                with patch("dual_codex.providers.antigravity_status", return_value="OK"):
                    with self.assertRaisesRegex(ProviderError, "stale|not supported"):
                        resolve_antigravity_agent(config, config.executor)

    def test_antigravity_resolution_rejects_changed_runtime_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "config.toml"
            path.write_text(
                """[orchestrator]\nrepository = \".\"\n\n[accounts.gemini]\nbackend = \"antigravity\"\ncodex_home = \"profiles/gemini\"\nmodel = \"gemini-3.8-flash\"\nruntime_model = \"gemini-3.8-flash-medium-old\"\nreasoning_effort = \"medium\"\n\n[roles]\nexecutor = \"gemini\"\n""",
                encoding="utf-8",
            )
            config = load_config(path)
            catalog = "gemini-3.8-flash-high\tGemini 3.8 Flash (High)\ngemini-3.8-flash-medium\tGemini 3.8 Flash (Medium)\n"
            with patch("dual_codex.providers.subprocess.run") as run:
                run.return_value = type("Result", (), {"stdout": catalog, "stderr": "", "returncode": 0})()
                with patch("dual_codex.providers.antigravity_status", return_value="OK"):
                    with self.assertRaisesRegex(ProviderError, "stale"):
                        resolve_antigravity_agent(config, config.executor)

    def test_antigravity_combined_slug_migrates_idempotently_on_load(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "config.toml"
            path.write_text(
                """[orchestrator]\nrepository = \".\"\n\n[accounts.gemini]\nbackend = \"antigravity\"\ncodex_home = \"profiles/gemini\"\nmodel = \"gemini-3.8-flash-high\"\nreasoning_effort = \"high\"\n\n[roles]\nexecutor = \"gemini\"\n""",
                encoding="utf-8",
            )
            first = load_config(path).accounts["gemini"]
            self.assertEqual(first.model, "gemini-3.8-flash")
            self.assertEqual(first.runtime_model, "gemini-3.8-flash-high")
            self.assertEqual(first.reasoning_effort, "high")
            normalized = path.read_text(encoding="utf-8").replace(
                'model = "gemini-3.8-flash-high"',
                'model = "gemini-3.8-flash"\nruntime_model = "gemini-3.8-flash-high"',
            )
            path.write_text(normalized, encoding="utf-8")
            second = load_config(path).accounts["gemini"]
            self.assertEqual(second, first)

    def test_api_profile_capabilities_are_explicit_and_secret_free(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "repo").mkdir()
            path = root / "config.toml"
            path.write_text(
                """[orchestrator]\nrepository = \"repo\"\n\n[accounts.api]\nbackend = \"api\"\nbase_url = \"https://api.example.test/v1\"\nauth_reference = \"env:TEST_PROVIDER_KEY\"\navailable_models = [\"model-a\"]\nsupported_reasoning_efforts = [\"low\", \"high\"]\nmodel = \"model-a\"\nreasoning_effort = \"high\"\n\n[roles]\nreviewer = \"api\"\n""",
                encoding="utf-8",
            )
            config = load_config(path)
            capabilities = provider_capabilities(config, config.accounts["api"])
            self.assertEqual(capabilities.provider, "api")
            self.assertEqual(capabilities.effort_levels, ("low", "high"))
            self.assertNotIn("architect", capabilities.supported_roles)
            self.assertIn("reviewer", capabilities.supported_roles)
            self.assertNotIn("TEST_PROVIDER_KEY", json.dumps(capabilities.as_dict()))

    def test_codex_and_gemini_profiles_coexist_with_role_scoped_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "config.toml"
            path.write_text(
                """[orchestrator]\nrepository = \".\"\n\n[accounts.codex_a]\nlabel = \"Codex Principal\"\nbackend = \"app_server\"\ncodex_home = \"profiles/codex-a\"\nmodel = \"model-a\"\nreasoning_effort = \"high\"\n\n[accounts.codex_b]\nlabel = \"Codex Secundario\"\nbackend = \"windows\"\ncodex_home = \"profiles/codex-b\"\nmodel = \"model-b\"\nreasoning_effort = \"medium\"\n\n[accounts.gemini_a]\nlabel = \"Gemini Pro\"\nbackend = \"antigravity\"\ncodex_home = \"profiles/gemini-a\"\nmodel = \"gemini-3.8-flash-high\"\nreasoning_effort = \"high\"\n\n[roles]\narchitect = \"codex_a\"\nexecutor = \"gemini_a\"\nreviewer = \"codex_b\"\n""",
                encoding="utf-8",
            )
            config = load_config(path)
            self.assertEqual(config.account_for_role("architect").name, "codex_a")
            self.assertEqual(config.account_for_role("executor").name, "gemini_a")
            self.assertEqual(config.account_for_role("reviewer").name, "codex_b")
            self.assertEqual(config.accounts["codex_a"].provider_type, "codex")
            self.assertEqual(config.accounts["gemini_a"].provider_type, "gemini")
            self.assertEqual(len({account.codex_home for account in config.accounts.values()}), 3)

    def test_api_adapter_maps_model_effort_and_env_secret(self) -> None:
        server = HTTPServer(("127.0.0.1", 0), _CompletionHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                old = os.environ.get("TEST_PROVIDER_KEY")
                os.environ["TEST_PROVIDER_KEY"] = "secret-value"
                try:
                    agent = AgentConfig(
                        codex_home=root / "profile",
                        model="model-a",
                        reasoning_effort="high",
                        sandbox="read-only",
                        account_name="api",
                        backend="api",
                        provider_type="api",
                        adapter_type="openai_compatible",
                        auth_mode="environment",
                        auth_reference="env:TEST_PROVIDER_KEY",
                        base_url=f"http://127.0.0.1:{server.server_port}/v1",
                        supported_reasoning_efforts=("high",),
                    )
                    output = root / "result.txt"
                    result = api_adapter().run(
                        agent=agent,
                        repository=root,
                        prompt="hello",
                        output_path=output,
                        config=self._config(root),
                    )
                    self.assertEqual(result.returncode, 0)
                    self.assertEqual(output.read_text(encoding="utf-8").strip(), "structured answer")
                    self.assertEqual(_CompletionHandler.payload["model"], "model-a")
                    self.assertEqual(_CompletionHandler.payload["reasoning_effort"], "high")
                    self.assertEqual(_CompletionHandler.authorization, "Bearer secret-value")
                    self.assertNotIn("secret-value", result.stderr)
                    default_agent = replace(agent, reasoning_effort="")
                    default_result = api_adapter().run(
                        agent=default_agent,
                        repository=root,
                        prompt="provider default",
                        output_path=root / "default.txt",
                        config=self._config(root),
                    )
                    self.assertEqual(default_result.returncode, 0)
                    self.assertNotIn("reasoning_effort", _CompletionHandler.payload)
                finally:
                    if old is None:
                        os.environ.pop("TEST_PROVIDER_KEY", None)
                    else:
                        os.environ["TEST_PROVIDER_KEY"] = old
        finally:
            server.shutdown()
            server.server_close()

    def test_api_adapter_rejects_plain_http_remote_and_raw_secret(self) -> None:
        with self.assertRaises(ProviderError):
            from dual_codex.providers import _api_url

            _api_url("http://example.test/v1")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "config.toml"
            path.write_text(
                """[orchestrator]\nrepository = \".\"\n\n[accounts.api]\nbackend = \"api\"\nauth_reference = \"plain-secret\"\n[roles]\narchitect = \"api\"\n""",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "env:VARIABLE"):
                load_config(path)

    def test_api_adapter_normalizes_provider_error_timeout_and_cancel(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            old = os.environ.get("TEST_PROVIDER_KEY")
            os.environ["TEST_PROVIDER_KEY"] = "secret-value"
            try:
                def make_agent(base_url: str) -> AgentConfig:
                    return AgentConfig(
                        codex_home=root / "profile",
                        model="model-a",
                        reasoning_effort="high",
                        sandbox="read-only",
                        account_name="api",
                        backend="api",
                        provider_type="api",
                        adapter_type="openai_compatible",
                        auth_mode="environment",
                        auth_reference="env:TEST_PROVIDER_KEY",
                        base_url=base_url,
                        supported_reasoning_efforts=("high",),
                    )

                server = HTTPServer(("127.0.0.1", 0), _ErrorHandler)
                threading.Thread(target=server.serve_forever, daemon=True).start()
                try:
                    error = api_adapter().run(
                        agent=make_agent(f"http://127.0.0.1:{server.server_port}/v1"),
                        repository=root,
                        prompt="hello",
                        output_path=root / "error.txt",
                        config=self._config(root),
                    )
                finally:
                    server.shutdown()
                    server.server_close()
                self.assertEqual(error.returncode, 503)
                self.assertEqual(error.metadata["provider_status"], "ERROR")
                self.assertNotIn("secret-value", error.stderr)

                with patch("dual_codex.providers.urllib.request.build_opener", side_effect=TimeoutError("timed out")):
                    timeout = api_adapter().run(
                        agent=make_agent("https://api.example.test/v1"),
                        repository=root,
                        prompt="hello",
                        output_path=root / "timeout.txt",
                        config=self._config(root),
                    )
                self.assertEqual(timeout.returncode, 1)
                self.assertEqual(timeout.metadata["provider_status"], "ERROR")
                self.assertIn("timed out", timeout.stderr)

                with patch("dual_codex.providers.urllib.request.build_opener", side_effect=KeyboardInterrupt):
                    canceled = api_adapter().run(
                        agent=make_agent("https://api.example.test/v1"),
                        repository=root,
                        prompt="hello",
                        output_path=root / "canceled.txt",
                        config=self._config(root),
                    )
                self.assertEqual(canceled.returncode, 130)
                self.assertEqual(canceled.metadata["provider_status"], "CANCELED")
            finally:
                if old is None:
                    os.environ.pop("TEST_PROVIDER_KEY", None)
                else:
                    os.environ["TEST_PROVIDER_KEY"] = old


if __name__ == "__main__":
    unittest.main()
