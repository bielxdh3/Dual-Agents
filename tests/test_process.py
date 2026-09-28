from __future__ import annotations

import os
from pathlib import Path
import time
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

    def test_timeout_does_not_wait_for_child_that_keeps_output_pipes_open(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "child-finished"
            child_code = (
                "import time; from pathlib import Path; "
                f"time.sleep(1.2); Path({str(marker)!r}).write_text('finished')"
            )
            parent_code = (
                "import subprocess, sys, time; "
                f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
                "time.sleep(30)"
            )
            started = time.monotonic()
            result = run_command(
                [sys.executable, "-c", parent_code],
                cwd=Path.cwd(),
                check=False,
                timeout=0.15,
            )
            elapsed = time.monotonic() - started

            self.assertEqual(result.returncode, 124)
            self.assertLess(elapsed, 8.0)
            self.assertEqual(result.metadata["failure_class"], "HOST_COMMAND_TIMEOUT")
            time.sleep(1.4)
            self.assertFalse(marker.exists(), "timed-out child was left running with inherited output handles")

    def test_timeout_kills_child_when_root_exits_while_child_keeps_pipes_open(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "child-finished"
            child_code = (
                "import time; from pathlib import Path; "
                f"time.sleep(1.2); Path({str(marker)!r}).write_text('finished')"
            )
            parent_code = (
                "import subprocess, sys; "
                f"subprocess.Popen([sys.executable, '-c', {child_code!r}])"
            )
            started = time.monotonic()
            result = run_command(
                [sys.executable, "-c", parent_code],
                cwd=Path.cwd(),
                check=False,
                timeout=0.15,
            )
            elapsed = time.monotonic() - started

            self.assertEqual(result.returncode, 124)
            self.assertLess(elapsed, 8.0)
            self.assertEqual(result.metadata["failure_class"], "HOST_COMMAND_TIMEOUT")
            time.sleep(1.4)
            self.assertFalse(marker.exists(), "child survived after its root process exited")

    @unittest.skipUnless(os.name == "nt", "Windows Job Objects are platform-specific")
    def test_windows_job_assignment_failure_does_not_resume_child(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "child-started"
            command = [
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(marker)!r}).write_text('started')",
            ]
            with patch(
                "dual_codex.process._WindowsProcessJob.assign_and_resume",
                side_effect=OSError("simulated job assignment failure"),
            ):
                with self.assertRaisesRegex(OSError, "simulated job assignment failure"):
                    run_command(command, cwd=Path.cwd(), check=False, timeout=1)
            time.sleep(0.1)
            self.assertFalse(marker.exists(), "child ran without successful Job Object assignment")

    def test_legacy_exec_turn_has_a_finite_configurable_timeout(self) -> None:
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
                    timeout=1800,
                )
            self.assertEqual(runner.call_args.kwargs["timeout"], 1800)

    def test_command_timeout_is_reported_structurally_with_or_without_progress(self) -> None:
        for progress in (None, lambda _message: None):
            with self.subTest(progress=progress is not None):
                result = run_command(
                    [sys.executable, "-c", "import time; time.sleep(1)"],
                    cwd=Path.cwd(),
                    check=False,
                    timeout=0.05,
                    progress=progress,
                    progress_interval=0.01,
                )
                self.assertEqual(result.returncode, 124)
                self.assertEqual(result.metadata["failure_class"], "HOST_COMMAND_TIMEOUT")
                self.assertEqual(result.metadata["availability_failure_class"], "timeout")


if __name__ == "__main__":
    unittest.main()
