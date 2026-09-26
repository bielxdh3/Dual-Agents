from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from dual_codex.git import attribute_git_mutations, capture_git_baseline
from dual_codex.process import CommandError


@unittest.skipUnless(shutil.which("git"), "Git is required for mutation-baseline regressions")
class GitBaselineTests(unittest.TestCase):
    def _repository(self, root: Path) -> Path:
        repository = root / "repository"
        repository.mkdir()

        def git(*args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["git", *args], cwd=repository, check=True, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )

        git("init", "-q")
        git("config", "user.name", "Baseline Test")
        git("config", "user.email", "baseline@example.invalid")
        (repository / "tracked.txt").write_text("original\n", encoding="utf-8")
        git("add", "tracked.txt")
        git("commit", "-qm", "initial")
        return repository

    def test_clean_baseline_and_clean_final_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self._repository(Path(temporary))
            baseline = capture_git_baseline(repository)
            mutation = attribute_git_mutations(repository, baseline)

            self.assertEqual(baseline["repository"], str(repository.resolve()))
            self.assertTrue(baseline["head"])
            self.assertEqual(baseline["staged_status"], {})
            self.assertEqual(baseline["unstaged_status"], {})
            self.assertEqual(baseline["untracked_paths"], [])
            self.assertEqual(mutation["status"], "complete")
            self.assertFalse(mutation["head_changed"])
            self.assertEqual(mutation["run_touched_paths"], [])
            self.assertEqual(mutation["run_created_paths"], [])

    def test_preexisting_tracked_change_is_unchanged_or_attributed_when_changed_again(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self._repository(Path(temporary))
            tracked = repository / "tracked.txt"
            tracked.write_text("user edit\n", encoding="utf-8")
            baseline = capture_git_baseline(repository)
            unchanged = attribute_git_mutations(repository, baseline)
            self.assertEqual(unchanged["unchanged_preexisting_paths"], ["tracked.txt"])
            self.assertEqual(unchanged["run_touched_paths"], [])

            tracked.write_text("user edit plus run edit\n", encoding="utf-8")
            changed = attribute_git_mutations(repository, baseline)
            self.assertEqual(changed["run_touched_paths"], ["tracked.txt"])
            self.assertEqual(changed["unchanged_preexisting_paths"], [])

    def test_preexisting_untracked_is_distinguished_from_new_and_removed_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self._repository(Path(temporary))
            old_untracked = repository / "user.txt"
            old_untracked.write_text("user file\n", encoding="utf-8")
            baseline = capture_git_baseline(repository)
            unchanged = attribute_git_mutations(repository, baseline)
            self.assertEqual(baseline["untracked_paths"], ["user.txt"])
            self.assertEqual(unchanged["unchanged_preexisting_paths"], ["user.txt"])

            (repository / "created.txt").write_text("run file\n", encoding="utf-8")
            mutation = attribute_git_mutations(repository, baseline)
            self.assertEqual(mutation["run_created_paths"], ["created.txt"])
            self.assertEqual(mutation["unchanged_preexisting_paths"], ["user.txt"])

            old_untracked.unlink()
            mutation = attribute_git_mutations(repository, baseline)
            self.assertIn("user.txt", mutation["run_removed_paths"])

    def test_staged_state_and_head_changes_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self._repository(Path(temporary))
            tracked = repository / "tracked.txt"
            tracked.write_text("preexisting staged edit\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=repository, check=True)
            baseline = capture_git_baseline(repository)
            self.assertEqual(baseline["staged_status"], {"tracked.txt": "M"})

            tracked.write_text("changed and staged during run\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=repository, check=True)
            staged_mutation = attribute_git_mutations(repository, baseline)
            self.assertTrue(staged_mutation["staged_state_changed"])
            self.assertEqual(staged_mutation["run_touched_paths"], ["tracked.txt"])

            subprocess.run(["git", "commit", "-qm", "run commit"], cwd=repository, check=True)
            committed_mutation = attribute_git_mutations(repository, baseline)
            self.assertTrue(committed_mutation["head_changed"])
            self.assertTrue(committed_mutation["staged_state_changed"])

    def test_snapshot_exclusion_and_safe_git_filter_handling(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self._repository(root)
            baseline = capture_git_baseline(repository)
            (repository / "control").mkdir()
            (repository / "control" / "run.json").write_text("owned\n", encoding="utf-8")
            mutation = attribute_git_mutations(repository, baseline, excluded_paths=("control",))
            self.assertEqual(mutation["run_created_paths"], [])
            self.assertEqual(mutation["excluded_control_paths"], ["control"])

            marker = root / "filter-invoked"
            (repository / ".gitattributes").write_text("tracked.txt filter=hostile\n", encoding="utf-8")
            subprocess.run(["git", "add", ".gitattributes"], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-qm", "filter attribute"], cwd=repository, check=True)
            hostile = f"cmd /c echo invoked > {marker}"
            subprocess.run(["git", "config", "filter.hostile.clean", hostile], cwd=repository, check=True)
            subprocess.run(["git", "config", "filter.hostile.required", "true"], cwd=repository, check=True)
            with self.assertRaises(CommandError):
                capture_git_baseline(repository)
            self.assertFalse(marker.exists(), "repository-configured filter ran during baseline collection")


if __name__ == "__main__":
    unittest.main()
