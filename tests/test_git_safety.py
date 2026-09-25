from __future__ import annotations

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from dual_codex.git import ensure_git_repository, head_revision, status_and_diff
from dual_codex.git import run_git
from dual_codex.process import CommandError, CommandResult, run_command
from dual_codex.publication import _run


@unittest.skipUnless(shutil.which("git"), "Git is required for repository safety regressions")
class GitSafetyTests(unittest.TestCase):
    def test_host_inspection_and_publication_disable_repo_fsmonitor_and_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "repository"
            repository.mkdir()
            remote = root / "remote.git"

            def git(*args: str, cwd: Path = repository) -> None:
                subprocess.run(["git", *args], cwd=cwd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            hostile_git_env = {
                name: value
                for name, value in os.environ.items()
                if not name.startswith("GIT_CONFIG_") and name != "GIT_CONFIG_PARAMETERS"
            }

            git("init", "-q")
            git("config", "user.name", "Safety Test")
            git("config", "user.email", "safety@example.invalid")
            (repository / "tracked.txt").write_text("tracked\n", encoding="utf-8")
            git("add", "tracked.txt")
            git("commit", "-qm", "initial")
            git("init", "--bare", "-q", str(remote), cwd=root)
            git("remote", "add", "origin", str(remote))

            fsmonitor_marker = repository / "fsmonitor-invoked"
            hook_marker = repository / "pre-push-invoked"
            hooks = repository / "repository-hooks"
            hooks.mkdir()
            pre_push = hooks / "pre-push"
            pre_push.write_text(
                "#!/bin/sh\nprintf x >> 'pre-push-invoked'\nexit 0\n",
                encoding="utf-8",
            )
            pre_push.chmod(0o755)
            git("config", "core.hooksPath", "repository-hooks")

            # Exercise each repository-controlled command before checking the
            # trusted wrapper's equivalent host operation.
            git("config", "core.fsmonitor", "sh -c 'printf x >> fsmonitor-invoked'")
            subprocess.run(
                ["git", "status", "--short"], cwd=repository, check=False,
                env=hostile_git_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            self.assertTrue(fsmonitor_marker.exists(), "security fixture did not exercise repository fsmonitor")
            fsmonitor_marker.unlink()
            raw_push = subprocess.run(
                ["git", "push", "origin", "HEAD:refs/heads/fixture"], cwd=repository,
                env=hostile_git_env, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            self.assertEqual(raw_push.returncode, 0, raw_push.stderr.decode(errors="replace"))
            self.assertTrue(hook_marker.exists(), "security fixture did not exercise repository pre-push hook")
            hook_marker.unlink()

            # Remove only the test host's injected Git config so success must
            # come from run_git's per-command protection, not ambient config.
            with patch.dict(os.environ, hostile_git_env, clear=True):
                (repository / "tracked.txt").write_text("modified\n", encoding="utf-8")
                captured_diff = status_and_diff(repository)
                self.assertIn("+modified", captured_diff)
                ensure_git_repository(repository)
                self.assertTrue(head_revision(repository))
                self.assertFalse(fsmonitor_marker.exists(), "repository fsmonitor command executed during host inspection")

                pushed = _run(
                    run_command,
                    ["git", "push", "origin", "HEAD:refs/heads/main"],
                    cwd=repository,
                )
                self.assertEqual(pushed.returncode, 0, pushed.stderr)
                self.assertFalse(hook_marker.exists(), "repository pre-push hook executed during trusted publication")
                self.assertFalse(fsmonitor_marker.exists(), "repository fsmonitor command executed during publication")

    def test_host_diff_does_not_run_repository_external_diff_or_textconv(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "repository"
            repository.mkdir()

            def git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    ["git", *args], cwd=repository, check=check, text=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )

            git("init", "-q")
            git("config", "user.name", "Safety Test")
            git("config", "user.email", "safety@example.invalid")
            tracked_path = repository / "tracked.txt"
            tracked_path.write_text("original\n", encoding="utf-8")
            (repository / ".gitattributes").write_text("tracked.txt diff=hostile\n", encoding="utf-8")
            git("add", ".")
            git("commit", "-qm", "initial")
            tracked_path.write_text("modified\n", encoding="utf-8")

            external_marker = root / "external-diff-invoked"
            textconv_marker = root / "textconv-invoked"
            external_script = root / "external_diff.py"
            textconv_script = root / "textconv.py"
            external_script.write_text(
                "from pathlib import Path\n"
                f"Path({str(external_marker)!r}).write_text('called', encoding='utf-8')\n",
                encoding="utf-8",
            )
            textconv_script.write_text(
                "from pathlib import Path\nimport sys\n"
                f"Path({str(textconv_marker)!r}).write_text('called', encoding='utf-8')\n"
                "sys.stdout.write(Path(sys.argv[-1]).read_text(encoding='utf-8'))\n",
                encoding="utf-8",
            )
            external_command = f"{shlex.quote(sys.executable)} {shlex.quote(str(external_script))}"
            textconv_command = f"{shlex.quote(sys.executable)} {shlex.quote(str(textconv_script))}"
            git("config", "diff.external", external_command)
            git("config", "diff.hostile.textconv", textconv_command)

            raw_external = git("diff", check=False)
            self.assertEqual(raw_external.returncode, 0, raw_external.stderr)
            self.assertTrue(external_marker.exists(), "fixture did not exercise the configured external diff")
            external_marker.unlink()

            git("config", "--unset", "diff.external")
            raw_textconv = git("diff", check=False)
            self.assertEqual(raw_textconv.returncode, 0, raw_textconv.stderr)
            self.assertTrue(textconv_marker.exists(), "fixture did not exercise the configured textconv")
            textconv_marker.unlink()

            git("config", "diff.external", external_command)
            safe_diff = run_git(["git", "diff"], cwd=repository)
            self.assertIn("+modified", safe_diff.stdout)
            self.assertFalse(external_marker.exists(), "repository external diff executed during trusted inspection")
            self.assertFalse(textconv_marker.exists(), "repository textconv executed during trusted inspection")

    def test_host_inspection_disables_repository_filter_process_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repository = Path(temp) / "filter-repository"
            repository.mkdir()

            def git(*args: str) -> None:
                subprocess.run(
                    ["git", *args], cwd=repository, check=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )

            git("init", "-q")
            git("config", "user.name", "Safety Test")
            git("config", "user.email", "safety@example.invalid")
            (repository / "tracked.txt").write_text("tracked\n", encoding="utf-8")
            (repository / ".gitattributes").write_text("tracked.txt filter=hostile\n", encoding="utf-8")
            git("add", ".")
            git("commit", "-qm", "initial")
            marker = repository / "filter-process-invoked"
            git("config", "filter.hostile.process", "sh -c 'printf x >> filter-process-invoked'")
            git("config", "filter.hostile.required", "true")
            (repository / "tracked.txt").write_text("changed\n", encoding="utf-8")

            # Verify the hostile process is live in this isolated fixture.
            raw_status = subprocess.run(
                ["git", "status", "--short"], cwd=repository, check=False,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            self.assertTrue(
                marker.exists(),
                f"security fixture did not exercise the Git process filter: {raw_status.stderr.decode(errors='replace')}",
            )
            marker.unlink()

            with self.assertRaisesRegex(
                CommandError,
                "Trusted host Git cannot inspect paths that use Git content filters",
            ):
                status_and_diff(repository)
            ensure_git_repository(repository)
            self.assertTrue(head_revision(repository))
            safe_hash = run_git(
                ["git", "hash-object", "--path=tracked.txt", "tracked.txt"],
                cwd=repository,
                check=False,
            )
            self.assertEqual(safe_hash.returncode, 1)
            self.assertIn(
                "Trusted host Git cannot inspect paths that use Git content filters",
                safe_hash.stderr,
            )
            self.assertFalse(marker.exists(), "repository Git filter process executed during host inspection")

    def test_host_inspection_disables_worktree_filter_process_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "main"
            worktree = root / "worktree"
            repository.mkdir()

            def git(*args: str, cwd: Path) -> None:
                subprocess.run(
                    ["git", *args], cwd=cwd, check=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )

            git("init", "-q", cwd=repository)
            git("config", "user.name", "Safety Test", cwd=repository)
            git("config", "user.email", "safety@example.invalid", cwd=repository)
            git("config", "extensions.worktreeConfig", "true", cwd=repository)
            (repository / "tracked.txt").write_text("original\n", encoding="utf-8")
            (repository / ".gitattributes").write_text("tracked.txt filter=hostile\n", encoding="utf-8")
            git("add", ".", cwd=repository)
            git("commit", "-qm", "initial", cwd=repository)
            git("worktree", "add", "-q", "-b", "safety-worktree", str(worktree), cwd=repository)

            marker = worktree / "filter-process-invoked"
            command = f"sh -c 'printf x >> {marker.as_posix()}'"
            git("config", "--worktree", "filter.hostile.process", command, cwd=worktree)
            git("config", "--worktree", "filter.hostile.required", "true", cwd=worktree)
            (worktree / "tracked.txt").write_text("changed\n", encoding="utf-8")

            with self.assertRaisesRegex(
                CommandError,
                "Trusted host Git cannot inspect paths that use Git content filters",
            ):
                status_and_diff(worktree)
            self.assertFalse(marker.exists(), "worktree Git filter process executed during host inspection")

    def test_clean_filter_semantics_fail_closed_instead_of_reporting_a_false_dirty_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repository = Path(temp) / "filtered-repository"
            repository.mkdir()

            def git(*args: str) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    ["git", *args], cwd=repository, check=True, text=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )

            git("init", "-q")
            git("config", "user.name", "Safety Test")
            git("config", "user.email", "safety@example.invalid")
            tracked_path = repository / "tracked.txt"
            tracked_path.write_text("original\n", encoding="utf-8")
            (repository / ".gitattributes").write_text("tracked.txt filter=canonical\n", encoding="utf-8")
            git("add", ".")
            git("commit", "-qm", "initial")

            clean_script = "import sys; sys.stdout.write(sys.stdin.read().replace('worktree', 'original'))"
            git("config", "filter.canonical.clean", f"{shlex.quote(sys.executable)} -c {shlex.quote(clean_script)}")
            previous_mtime_ns = tracked_path.stat().st_mtime_ns
            tracked_path.write_text("worktree\n", encoding="utf-8")
            next_mtime = previous_mtime_ns + 3_000_000_000
            os.utime(tracked_path, ns=(next_mtime, next_mtime))
            filtered_hash = git("hash-object", "--path=tracked.txt", "tracked.txt").stdout.strip()
            index_hash = git("rev-parse", "HEAD:tracked.txt").stdout.strip()
            self.assertEqual(
                filtered_hash,
                index_hash,
                "fixture clean filter should map worktree bytes to the index blob",
            )
            ordinary_status = git("status", "--porcelain")
            self.assertEqual(ordinary_status.stdout, "", "fixture should be clean under the configured clean filter")

            safe_status = run_git(["git", "status", "--porcelain"], cwd=repository, check=False)
            self.assertEqual(safe_status.returncode, 1)
            self.assertIn("Trusted host Git cannot inspect paths that use Git content filters", safe_status.stderr)

    def test_unrepresentable_filter_names_fail_closed_before_host_git_runs(self) -> None:
        runner = Mock()
        with patch(
            "dual_codex.git.run_command",
            return_value=CommandResult([], 0, "filter.hostile=name.process\n", ""),
        ):
            result = run_git(
                ["git", "status", "--short"],
                cwd=Path.cwd(),
                runner=runner,
                check=False,
            )
        self.assertEqual(result.returncode, 1)
        self.assertIn("Could not safely disable", result.stderr)
        runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
