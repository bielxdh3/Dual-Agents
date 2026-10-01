from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dual_codex.cli import main
from dual_codex.config import AccountConfig, OrchestratorConfig


class CliRunRepositoryTests(unittest.TestCase):
    def _config(self, root: Path) -> OrchestratorConfig:
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
        return OrchestratorConfig(
            repository=root / "configured-repository",
            runs_dir=root / "runs",
            max_correction_cycles=0,
            require_clean_git=True,
            codex_command="codex",
            accounts=accounts,
            roles={
                "architect": "architect",
                "executor": "executor",
                "reviewer": "reviewer",
            },
            project_root=Path.cwd(),
            config_path=root / "config.toml",
        )

    def test_run_uses_explicit_external_repository_and_keeps_configured_roles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "external-target"
            target.mkdir()
            task = root / "mission.md"
            task.write_text("A normal multi-role mission without a launcher phrase.", encoding="utf-8")
            config = self._config(root)
            observed = {}

            def capture_run(actual_config, actual_task, **kwargs):
                observed["config"] = actual_config
                observed["task"] = actual_task
                observed.update(kwargs)
                return type("Outcome", (), {"run_dir": root / "runs" / "run", "verdict": "approved", "correction_cycles": 0})()

            with patch("dual_codex.cli.load_config", return_value=config), patch(
                "dual_codex.cli.execute", side_effect=capture_run
            ):
                result = main(
                    [
                        "--config",
                        str(config.config_path),
                        "run",
                        "--repository",
                        str(target),
                        str(task),
                    ]
                )

            self.assertEqual(result, 0)
            self.assertEqual(observed["config"].repository, target.resolve())
            self.assertEqual(observed["config"].roles, config.roles)
            self.assertEqual(observed["task"], task)
            self.assertTrue(observed["explicit_repository"])
            self.assertTrue(callable(observed["progress"]))
            with patch("builtins.print") as printer:
                observed["progress"]("DUAL_CODEX_PROGRESS {\"phase\":\"executor\",\"state\":\"running\"}")
            printer.assert_called_once_with(
                'DUAL_CODEX_PROGRESS {"phase":"executor","state":"running"}',
                flush=True,
            )

    def test_run_keeps_configured_repository_when_no_override_is_given(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._config(root)
            task = root / "mission.md"
            task.write_text("Mission", encoding="utf-8")
            observed = {}

            def capture_run(actual_config, actual_task, **kwargs):
                observed["config"] = actual_config
                observed.update(kwargs)
                return type("Outcome", (), {"run_dir": root / "runs" / "run", "verdict": "approved", "correction_cycles": 0})()

            with patch("dual_codex.cli.load_config", return_value=config), patch(
                "dual_codex.cli.execute", side_effect=capture_run
            ):
                result = main(["--config", str(config.config_path), "run", str(task)])

            self.assertEqual(result, 0)
            self.assertEqual(observed["config"].repository, config.repository)
            self.assertFalse(observed["explicit_repository"])


if __name__ == "__main__":
    unittest.main()
