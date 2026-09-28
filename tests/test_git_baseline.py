from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from dual_codex.git import (
    _git_metadata_snapshots,
    _git_object_inventory,
    _worktree_snapshot,
    attribute_git_mutations,
    capture_git_baseline,
)
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

    def test_scan_only_baseline_captures_git_configuration_refs_and_common_worktree_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            primary = self._repository(root)
            subprocess.run(["git", "config", "extensions.worktreeConfig", "true"], cwd=primary, check=True)
            worktree = root / "linked-worktree"
            subprocess.run(["git", "worktree", "add", "--detach", str(worktree), "HEAD"], cwd=primary, check=True)
            subprocess.run(["git", "config", "--worktree", "dual-agents.test", "before"], cwd=worktree, check=True)

            baseline = capture_git_baseline(worktree, include_ignored=True)
            metadata = baseline["git_metadata_snapshots"]
            branch = subprocess.run(
                ["git", "branch", "--show-current"], cwd=primary, check=True,
                text=True, stdout=subprocess.PIPE,
            ).stdout.strip()
            self.assertNotEqual(baseline["git_dir"], baseline["git_common_dir"])
            self.assertIn("git_common_dir/config", metadata)
            self.assertIn(f"git_common_dir/refs/heads/{branch}", metadata)
            self.assertIn("git_common_dir/objects/@inventory", metadata)
            self.assertIn("git_dir/config.worktree", metadata)
            self.assertTrue(baseline["complete"])

            subprocess.run(["git", "config", "--worktree", "dual-agents.test", "after"], cwd=worktree, check=True)
            mutation = attribute_git_mutations(worktree, baseline, include_ignored=True)
            self.assertTrue(mutation["repository_metadata_changed"])
            self.assertEqual(mutation["status"], "complete")

    def test_scan_only_attribution_detects_gitdir_path_change_with_matching_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            primary = self._repository(root)
            first = root / "first-worktree"
            second = root / "second-worktree"
            subprocess.run(["git", "worktree", "add", "--detach", str(first), "HEAD"], cwd=primary, check=True)
            subprocess.run(["git", "worktree", "add", "--detach", str(second), "HEAD"], cwd=primary, check=True)

            baseline = capture_git_baseline(first, include_ignored=True)
            self.assertTrue(baseline["complete"], baseline["git_metadata_snapshots"])
            original_gitdir = (first / ".git").read_text(encoding="utf-8").split(":", 1)[1].strip()
            replacement_gitdir = (second / ".git").read_text(encoding="utf-8").split(":", 1)[1].strip()
            self.assertNotEqual(original_gitdir, replacement_gitdir)

            def switched_gitdir(repository: Path):
                _git_dir, common_dir, snapshots = _git_metadata_snapshots(repository)
                return replacement_gitdir, common_dir, snapshots

            with patch("dual_codex.git._git_metadata_snapshots", side_effect=switched_gitdir):
                mutation = attribute_git_mutations(first, baseline, include_ignored=True)

            self.assertEqual(mutation["status"], "complete")
            self.assertTrue(mutation["repository_metadata_changed"])

    def test_object_inventory_detects_content_change_and_fails_closed_at_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self._repository(Path(temporary))
            objects = repository / ".git" / "objects"
            object_path = objects / "info" / "inventory-probe"
            object_path.parent.mkdir(exist_ok=True)
            object_path.write_bytes(b"first")
            before = _git_object_inventory(repository / ".git")
            original_stat = object_path.stat()
            object_path.write_bytes(b"other")
            os.utime(object_path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
            after = _git_object_inventory(repository / ".git")

            self.assertEqual(before["kind"], "inventory")
            self.assertEqual(after["kind"], "inventory")
            self.assertNotEqual(before["sha256"], after["sha256"])
            self.assertEqual(
                _git_object_inventory(repository / ".git", max_entries=0),
                {"kind": "unknown", "reason": "git_object_inventory_budget_exceeded"},
            )

    def test_custom_hooks_path_inside_gitdir_is_snapshotted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self._repository(Path(temporary))
            custom_hooks = repository / ".git" / "custom-hooks"
            custom_hooks.mkdir()
            hook = custom_hooks / "pre-commit"
            hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            subprocess.run(["git", "config", "--local", "core.hooksPath", ".git/custom-hooks"], cwd=repository, check=True)

            baseline = capture_git_baseline(repository, include_ignored=True)
            self.assertTrue(baseline["complete"], baseline["git_metadata_snapshots"])
            hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            mutation = attribute_git_mutations(repository, baseline, include_ignored=True)

            self.assertTrue(mutation["repository_metadata_changed"])
            self.assertEqual(mutation["status"], "complete")

    def test_scan_only_baseline_fails_closed_for_alternates_and_submodules(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self._repository(root)
            alternate_objects = root / "alternate-objects"
            alternate_objects.mkdir()
            alternates_file = repository / ".git" / "objects" / "info" / "alternates"
            alternates_file.write_text(str(alternate_objects), encoding="utf-8")

            alternate_baseline = capture_git_baseline(repository, include_ignored=True)
            self.assertFalse(alternate_baseline["complete"])
            self.assertIn(
                "external_git_alternate_object_store_not_inventoried",
                json.dumps(alternate_baseline["git_metadata_snapshots"]),
            )

        with tempfile.TemporaryDirectory() as temporary:
            repository = self._repository(Path(temporary))
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repository, check=True, text=True, stdout=subprocess.PIPE
            ).stdout.strip()
            subprocess.run(
                ["git", "update-index", "--add", "--cacheinfo", f"160000,{head},vendor/dependency"],
                cwd=repository,
                check=True,
            )
            submodule_baseline = capture_git_baseline(repository, include_ignored=True)
            self.assertFalse(submodule_baseline["complete"])
            self.assertEqual(
                submodule_baseline["git_metadata_snapshots"]["git_behavior/@submodules"]["reason"],
                "submodule_gitdirs_not_inventoried",
            )

        with tempfile.TemporaryDirectory() as temporary:
            repository = self._repository(Path(temporary))
            (repository / ".git" / "additional-config").write_text(
                "[core]\n\thooksPath = extra-hooks\n", encoding="utf-8"
            )
            subprocess.run(
                ["git", "config", "--local", "--add", "include.path", ".git/additional-config"],
                cwd=repository,
                check=True,
            )
            included_config_baseline = capture_git_baseline(repository, include_ignored=True)
            self.assertFalse(included_config_baseline["complete"])
            self.assertEqual(
                included_config_baseline["git_metadata_snapshots"]["git_behavior/@config_includes"]["reason"],
                "included_git_configuration_not_inventoried",
            )

    def test_split_index_shared_files_are_included_in_metadata_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = self._repository(Path(temporary))
            shared_index = repository / ".git" / "sharedindex.probe"
            shared_index.write_bytes(b"first")
            baseline = capture_git_baseline(repository, include_ignored=True)
            self.assertIn("git_dir/sharedindex.probe", baseline["git_metadata_snapshots"])
            shared_index.write_bytes(b"other")
            mutation = attribute_git_mutations(repository, baseline, include_ignored=True)
            self.assertTrue(mutation["repository_metadata_changed"])

    def test_worktree_snapshot_rejects_a_different_inode_opened_after_lstat(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self._repository(root)
            tracked = repository / "tracked.txt"
            replacement = root / "replacement.txt"
            replacement.write_bytes(tracked.read_bytes())
            original_open = os.open

            def swapped_open(path, flags, *args, **kwargs):
                if Path(path) == tracked:
                    return original_open(replacement, flags, *args, **kwargs)
                return original_open(path, flags, *args, **kwargs)

            with patch("dual_codex.git.os.open", side_effect=swapped_open):
                snapshot = _worktree_snapshot(repository, "tracked.txt")
            self.assertEqual(snapshot, {"kind": "unknown", "reason": "unsafe_opened_file"})


if __name__ == "__main__":
    unittest.main()
