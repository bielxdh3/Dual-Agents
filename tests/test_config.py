from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from dual_codex.config import ConfigError, load_config


class ConfigTests(unittest.TestCase):
    def test_loads_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "config.toml").write_text(
                """
[orchestrator]
repository = "repo"
runs_dir = "runs"
max_correction_cycles = 2
require_clean_git = true
codex_command = "codex"
live_event_journal_max_records = 9
live_event_journal_max_record_bytes = 2048
live_event_journal_max_detail_bytes = 512

[architect]
codex_home = "profiles/a"
model = ""
reasoning_effort = "high"
sandbox = "read-only"

[executor]
codex_home = "profiles/b"
model = ""
reasoning_effort = "medium"
sandbox = "workspace-write"
network_access = true
""".strip(),
                encoding="utf-8",
            )
            config = load_config(root / "config.toml")
            self.assertEqual(config.repository, (root / "repo").resolve())
            self.assertEqual(config.runs_dir, (root / "runs").resolve())
            self.assertEqual(config.max_correction_cycles, 2)
            self.assertEqual(config.architect.sandbox, "read-only")
            self.assertEqual(config.executor.sandbox, "workspace-write")
            self.assertTrue(config.executor.network_access)
            self.assertEqual(config.live_event_journal_max_records, 9)
            self.assertEqual(config.live_event_journal_max_record_bytes, 2048)
            self.assertEqual(config.live_event_journal_max_detail_bytes, 512)

    def test_rejects_unknown_backend(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "config.toml").write_text(
                """
[orchestrator]
repository = "repo"

[accounts.executor]
codex_home = "profile"
backend = "unsupported"

[roles]
executor = "executor"
""".strip(),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigError, "unsupported backend"):
                load_config(root / "config.toml")

    def test_rejects_non_boolean_network_access(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "config.toml").write_text(
                """
[orchestrator]
repository = "repo"

[accounts.executor]
codex_home = "profile"
network_access = "true"

[roles]
executor = "executor"
""".strip(),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigError, "network_access must be a boolean"):
                load_config(root / "config.toml")

    def test_app_server_turn_timeout_is_scoped_to_account_and_role(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "config.toml").write_text(
                """
[orchestrator]
repository = "repo"
app_server_turn_timeout = 600

[accounts.architect]
codex_home = "profiles/architect"
backend = "app_server"

[accounts.codex-secundario]
codex_home = "profiles/executor"
backend = "app_server"

[accounts.codex-secundario.app_server_turn_timeouts]
executor = 3600

[roles]
orchestrator = "architect"
architect = "architect"
executor = "codex-secundario"
""".strip(),
                encoding="utf-8",
            )

            config = load_config(root / "config.toml")

            self.assertEqual(config.executor.app_server_turn_timeout, 3600)
            self.assertIsNone(config.architect.app_server_turn_timeout)
            self.assertEqual(config.app_server_turn_timeout, 600)
            self.assertEqual(config.app_server_turn_start_timeout, 30)
            self.assertEqual(config.app_server_initialize_timeout, 30)
            self.assertEqual(config.app_server_thread_timeout, 30)

    def test_rejects_invalid_account_role_turn_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "config.toml").write_text(
                """
[orchestrator]
repository = "repo"

[accounts.executor]
codex_home = "profile"
backend = "app_server"

[accounts.executor.app_server_turn_timeouts]
executor = 0

[roles]
executor = "executor"
""".strip(),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigError, "must be positive and finite"):
                load_config(root / "config.toml")


if __name__ == "__main__":
    unittest.main()
