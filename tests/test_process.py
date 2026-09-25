from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from dual_codex.codex import run_codex_exec
from dual_codex.config import AgentConfig
from dual_codex.process import CommandError, CommandResult, run_command


class ProcessTimeoutTests(unittest.TestCase):
    def test_short_host_command_times_out_with_a_nonzero_result(self) -> None:
        result = run_command(
            [sys.executable, "-c", "import time; time.sleep(1)"],
            cwd=Path.cwd(),
            check=False,
            timeout=0.05,
        )
        self.assertEqual(result.returncode, 124)
        self.assertIn("timed out", result.stderr.casefold())

    def test_long_model_turn_is_not_given_a_host_probe_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            agent = AgentConfig(
                codex_home=root / "profile",
                model="gpt-6-sol",
                reasoning_effort="high",
                sandbox="read-only",
            )
            completed = CommandResult(["codex"], 0, "{}", "")
            with patch("dual_codex.codex.run_command", return_value=completed) as runner:
                run_codex_exec(
                    codex_command="codex",
                    agent=agent,
                    repository=root,
                    prompt="complete a long model turn",
                    output_path=root / "out.json",
                    schema_path=root / "schema.json",
                )
            self.assertIsNone(runner.call_args.kwargs["timeout"])


if __name__ == "__main__":
    unittest.main()
