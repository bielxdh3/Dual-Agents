from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dual_codex.live_events import _open_journal_lock, _replace_journal_file


class WindowsJournalLockTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows-specific lock recovery")
    def test_transient_lock_open_failure_is_retried(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            lock_path = Path(temp) / "shared.jsonl.lock"
            real_open = Path.open
            failures = 0

            def flaky_open(path: Path, *args, **kwargs):
                nonlocal failures
                if path == lock_path and failures == 0:
                    failures += 1
                    raise PermissionError(13, "simulated transient Windows sharing violation")
                return real_open(path, *args, **kwargs)

            with patch.object(Path, "open", new=flaky_open):
                handle = _open_journal_lock(lock_path)
                handle.close()

            self.assertEqual(failures, 1)
            self.assertEqual(lock_path.read_bytes(), b"\0")

    @unittest.skipUnless(os.name == "nt", "Windows-specific journal replacement")
    def test_transient_replace_sharing_failure_is_retried(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            temporary = root / "snapshot.tmp"
            target = root / "shared.jsonl"
            temporary.write_bytes(b"new")
            target.write_bytes(b"old")
            real_replace = os.replace
            failures = 0

            def flaky_replace(source: Path, destination: Path) -> None:
                nonlocal failures
                if destination == target and failures == 0:
                    failures += 1
                    raise PermissionError(32, "simulated transient Windows sharing violation")
                real_replace(source, destination)

            with patch("dual_codex.live_events.os.replace", new=flaky_replace):
                _replace_journal_file(temporary, target)

            self.assertEqual(failures, 1)
            self.assertEqual(target.read_bytes(), b"new")

    @unittest.skipUnless(os.name == "nt", "Windows-specific journal replacement")
    def test_permanent_replace_failure_is_not_swallowed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            temporary = root / "snapshot.tmp"
            target = root / "shared.jsonl"
            temporary.write_bytes(b"new")
            target.write_bytes(b"old")
            with patch(
                "dual_codex.live_events.os.replace",
                side_effect=PermissionError(5, "simulated permanent access denial"),
            ):
                with self.assertRaises(PermissionError):
                    _replace_journal_file(temporary, target)


if __name__ == "__main__":
    unittest.main()
