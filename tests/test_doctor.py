from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dual_codex.config import AccountConfig, OrchestratorConfig
from dual_codex.doctor import _auth_check
from dual_codex.providers import ProviderCapabilities


class DoctorTests(unittest.TestCase):
    def test_claude_readiness_does_not_require_codex_profile_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            account = AccountConfig(
                name="claude",
                label="Claude",
                codex_home=root / "claude-state",
                model="sonnet",
                reasoning_effort="high",
                backend="claude_code",
                provider_type="anthropic",
                adapter_type="claude_code",
            )
            config = OrchestratorConfig(
                repository=root,
                runs_dir=root / "runs",
                max_correction_cycles=0,
                require_clean_git=False,
                codex_command="codex",
                accounts={"claude": account},
                roles={"architect": "claude"},
                project_root=root,
                config_path=root / "config.toml",
            )
            with patch("dual_codex.doctor.login_status", return_value="OK"):
                check = _auth_check(
                    "account claude",
                    account.codex_home,
                    backend=account.backend,
                    config=config,
                    account=account,
                )
            self.assertTrue(check.ok)
            self.assertIn("Claude Code authenticated", check.details)

    def test_dispatchable_requires_runtime_evidence(self) -> None:
        capabilities = ProviderCapabilities(
            provider="codex",
            provider_label="Codex",
            adapter="codex_cli",
            runtime_status="Configured",
            authenticated=True,
        )
        availability = capabilities.as_dict()["availability"]
        self.assertTrue(availability["configured"])
        self.assertTrue(availability["authenticated"])
        self.assertFalse(availability["runtime_initialized"])
        self.assertFalse(availability["thread_bound"])
        self.assertFalse(availability["dispatchable"])


if __name__ == "__main__":
    unittest.main()
