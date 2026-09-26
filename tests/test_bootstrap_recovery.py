from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from dual_codex.bootstrap import (
    cleanup_canonical_bootstrap,
    create_canonical_bootstrap,
    reconcile_orphan_canonical_bootstrap,
)
from dual_codex.config import AccountConfig, OrchestratorConfig
from dual_codex.delegation import RepositoryLock
from dual_codex.orchestrator import execute
from dual_codex.process import CommandResult
from dual_codex.report import atomic_write_json


def _commit_repo(repository: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.name", "Bootstrap Test"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.email", "bootstrap@example.invalid"], cwd=repository, check=True)
    (repository / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=repository, check=True)


class CanonicalBootstrapRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        _commit_repo(self.repository)
        self.instructions = self.root / "CodexGlobal"
        self.instructions.mkdir()
        (self.instructions / "AGENTS.md").write_text("Host policy.\n", encoding="utf-8")
        for skill in ("memory", "ponytail", "project-phase-review", "project-security-review"):
            skill_path = self.instructions / "skills" / skill / "SKILL.md"
            skill_path.parent.mkdir(parents=True, exist_ok=True)
            skill_path.write_text(f"# {skill}\n", encoding="utf-8")
        self.runs = self.root / "runs"
        self.lock = RepositoryLock(self.runs, self.repository, "current-request", "current-run")
        self.lock.acquire()
        self.addCleanup(self.lock.release)
        self.addCleanup(self.temporary.cleanup)

    def _artifact(self, run_id: str = "old-run"):
        return create_canonical_bootstrap(
            role="executor",
            artifact_dir=self.repository / ".dual_codex" / "bootstrap",
            root=self.instructions,
            artifact_repository=self.repository,
            run_id=run_id,
        )

    def _mark_owner_dead(self, owner_path: Path) -> None:
        owner = json.loads(owner_path.read_text(encoding="utf-8"))
        owner["pid"] = 2147483000
        owner["process_start"] = "dead-process"
        atomic_write_json(owner_path, owner)

    def test_normal_cleanup_removes_artifact_and_owner_metadata(self) -> None:
        bootstrap = self._artifact()
        artifact = bootstrap.artifact_path
        owner = bootstrap.artifact_owner_path
        self.assertTrue(artifact.is_file())
        self.assertTrue(owner.is_file())

        cleanup_canonical_bootstrap(bootstrap)

        self.assertFalse(artifact.exists())
        self.assertFalse(owner.exists())

    def test_proven_orphan_is_removed_idempotently_under_current_run_lock(self) -> None:
        bootstrap = self._artifact()
        self._mark_owner_dead(bootstrap.artifact_owner_path)

        result = reconcile_orphan_canonical_bootstrap(
            self.repository,
            repository_lock=self.lock,
            run_id="current-run",
        )
        again = reconcile_orphan_canonical_bootstrap(
            self.repository,
            repository_lock=self.lock,
            run_id="current-run",
        )

        self.assertEqual([item["name"] for item in result["removed"]], [bootstrap.artifact_path.name])
        self.assertEqual(again, {"removed": [], "retained": []})
        self.assertFalse(bootstrap.artifact_path.exists())
        self.assertFalse(bootstrap.artifact_owner_path.exists())

    def test_legacy_orphan_without_metadata_is_reaped_from_owned_name_under_lock(self) -> None:
        bootstrap = self._artifact()
        legacy = bootstrap.artifact_path
        bootstrap.artifact_owner_path.unlink()

        result = reconcile_orphan_canonical_bootstrap(
            self.repository,
            repository_lock=self.lock,
            run_id="current-run",
            configured_backends={"executor": "app_server"},
            canonical_root=self.instructions,
        )

        self.assertEqual(result["removed"], [{"name": legacy.name, "run_id": "legacy"}])
        self.assertFalse(legacy.exists())

    def test_legacy_file_with_generated_name_but_user_content_is_retained(self) -> None:
        bootstrap_dir = self.repository / ".dual_codex" / "bootstrap"
        bootstrap_dir.mkdir(parents=True)
        user_file = bootstrap_dir / ".canonical-bootstrap-executor-ABCDEFGH.md"
        user_file.write_text("user document\n", encoding="utf-8")

        result = reconcile_orphan_canonical_bootstrap(
            self.repository,
            repository_lock=self.lock,
            run_id="current-run",
            configured_backends={"executor": "app_server"},
            canonical_root=self.instructions,
        )

        self.assertEqual(result["removed"], [])
        self.assertTrue(user_file.exists())
        self.assertEqual(result["retained"][0]["reason"], "legacy_ownership_unproven")

    def test_legacy_claude_artifact_without_provider_identity_is_retained(self) -> None:
        bootstrap = create_canonical_bootstrap(
            role="executor",
            artifact_dir=self.repository / ".dual_codex" / "bootstrap",
            root=self.instructions,
            artifact_repository=self.repository,
            run_id="legacy-run",
        )
        bootstrap.artifact_owner_path.unlink()

        result = reconcile_orphan_canonical_bootstrap(
            self.repository,
            repository_lock=self.lock,
            run_id="current-run",
            configured_backends={"executor": "claude_code"},
            canonical_root=self.instructions,
        )

        self.assertEqual(result["removed"], [])
        self.assertTrue(bootstrap.artifact_path.exists())
        self.assertEqual(result["retained"][0]["reason"], "legacy_provider_ownership_ambiguous")

    def test_current_run_artifact_and_ambiguous_owner_are_preserved(self) -> None:
        current = self._artifact(run_id="current-run")
        active = self._artifact(run_id="another-live-run")
        ambiguous = self._artifact(run_id="prior-run")
        owner = json.loads(ambiguous.artifact_owner_path.read_text(encoding="utf-8"))
        owner["artifact_sha256"] = "0" * 64
        atomic_write_json(ambiguous.artifact_owner_path, owner)

        result = reconcile_orphan_canonical_bootstrap(
            self.repository,
            repository_lock=self.lock,
            run_id="current-run",
        )

        self.assertTrue(current.artifact_path.exists())
        self.assertTrue(active.artifact_path.exists())
        self.assertTrue(ambiguous.artifact_path.exists())
        reasons = {item["name"]: item["reason"] for item in result["retained"]}
        self.assertEqual(reasons[current.artifact_path.name], "current_run_artifact")
        self.assertEqual(reasons[active.artifact_path.name], "active_or_ambiguous_owner")
        self.assertEqual(reasons[ambiguous.artifact_path.name], "ambiguous_owner_metadata")

    def test_live_provider_process_keeps_dead_run_artifact(self) -> None:
        bootstrap = self._artifact(run_id="interrupted-run")
        owner = json.loads(bootstrap.artifact_owner_path.read_text(encoding="utf-8"))
        owner["backend"] = "app_server"
        owner["provider_state"] = "running"
        from dual_codex.delegation import _safe_process_start_token

        owner["provider_pid"] = os.getpid()
        owner["provider_process_start"] = _safe_process_start_token(owner["provider_pid"])
        owner["pid"] = 2147483000
        owner["process_start"] = "dead-process"
        atomic_write_json(bootstrap.artifact_owner_path, owner)

        result = reconcile_orphan_canonical_bootstrap(
            self.repository,
            repository_lock=self.lock,
            run_id="current-run",
        )

        self.assertTrue(bootstrap.artifact_path.exists())
        self.assertEqual(result["retained"][0]["reason"], "active_or_ambiguous_provider")

    def test_wrong_names_user_files_and_files_outside_exact_root_are_untouched(self) -> None:
        bootstrap_dir = self.repository / ".dual_codex" / "bootstrap"
        bootstrap_dir.mkdir(parents=True)
        user_file = bootstrap_dir / "notes.md"
        user_file.write_text("user content\n", encoding="utf-8")
        wrong_name = bootstrap_dir / ".canonical-bootstrap-executor-not-random.md"
        wrong_name.write_text("not a generated artifact\n", encoding="utf-8")
        outside_dir = self.repository / ".dual_codex" / "other"
        outside_dir.mkdir()
        outside = outside_dir / ".canonical-bootstrap-executor-ABCDEFGH.md"
        outside.write_text("outside exact root\n", encoding="utf-8")

        result = reconcile_orphan_canonical_bootstrap(
            self.repository,
            repository_lock=self.lock,
            run_id="current-run",
        )

        self.assertEqual(result["removed"], [])
        self.assertEqual(user_file.read_text(encoding="utf-8"), "user content\n")
        self.assertTrue(wrong_name.exists())
        self.assertTrue(outside.exists())

    def test_symlink_named_like_artifact_is_never_followed_or_deleted(self) -> None:
        bootstrap_dir = self.repository / ".dual_codex" / "bootstrap"
        bootstrap_dir.mkdir(parents=True)
        target = self.repository / "user-target.md"
        target.write_text("keep me\n", encoding="utf-8")
        link = bootstrap_dir / ".canonical-bootstrap-executor-ABCDEFGH.md"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")

        result = reconcile_orphan_canonical_bootstrap(
            self.repository,
            repository_lock=self.lock,
            run_id="current-run",
        )

        self.assertFalse(result["removed"])
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8"), "keep me\n")

    def test_symlink_bootstrap_directory_is_not_traversed(self) -> None:
        metadata_dir = self.repository / ".dual_codex"
        metadata_dir.mkdir()
        outside = self.root / "outside-bootstrap"
        outside.mkdir()
        artifact = outside / ".canonical-bootstrap-executor-ABCDEFGH.md"
        artifact.write_text("outside file\n", encoding="utf-8")
        try:
            (metadata_dir / "bootstrap").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"directory symlink creation unavailable: {exc}")

        result = reconcile_orphan_canonical_bootstrap(
            self.repository,
            repository_lock=self.lock,
            run_id="current-run",
        )

        self.assertEqual(result["removed"], [])
        self.assertEqual(result["retained"][0]["reason"], "unsafe_directory")
        self.assertEqual(artifact.read_text(encoding="utf-8"), "outside file\n")

    def test_reparse_point_file_is_retained(self) -> None:
        bootstrap_dir = self.repository / ".dual_codex" / "bootstrap"
        bootstrap_dir.mkdir(parents=True)
        artifact = bootstrap_dir / ".canonical-bootstrap-executor-ABCDEFGH.md"
        artifact.write_text("do not follow reparse points\n", encoding="utf-8")
        original_lstat = Path.lstat

        def lstat_with_reparse(path: Path):
            info = original_lstat(path)
            if path == artifact:
                return SimpleNamespace(
                    st_mode=info.st_mode,
                    st_file_attributes=0x400,
                    st_dev=info.st_dev,
                    st_ino=info.st_ino,
                    st_mtime_ns=info.st_mtime_ns,
                )
            return info

        with patch.object(Path, "lstat", lstat_with_reparse):
            result = reconcile_orphan_canonical_bootstrap(
                self.repository,
                repository_lock=self.lock,
                run_id="current-run",
            )

        self.assertEqual(result["removed"], [])
        self.assertTrue(artifact.exists())
        self.assertEqual(result["retained"][0]["reason"], "not_regular_file")

    def test_reparse_bootstrap_directory_is_not_traversed(self) -> None:
        metadata_dir = self.repository / ".dual_codex"
        metadata_dir.mkdir()
        bootstrap_dir = metadata_dir / "bootstrap"
        bootstrap_dir.mkdir()
        artifact = bootstrap_dir / ".canonical-bootstrap-executor-ABCDEFGH.md"
        artifact.write_text("keep outside target directory\n", encoding="utf-8")
        original_lstat = Path.lstat

        def lstat_with_reparse(path: Path):
            info = original_lstat(path)
            if path == bootstrap_dir:
                return SimpleNamespace(
                    st_mode=info.st_mode,
                    st_file_attributes=0x400,
                    st_dev=info.st_dev,
                    st_ino=info.st_ino,
                    st_mtime_ns=info.st_mtime_ns,
                )
            return info

        with patch.object(Path, "lstat", lstat_with_reparse):
            result = reconcile_orphan_canonical_bootstrap(
                self.repository,
                repository_lock=self.lock,
                run_id="current-run",
            )

        self.assertEqual(result["removed"], [])
        self.assertTrue(artifact.exists())
        self.assertEqual(result["retained"][0]["reason"], "unsafe_directory")

    def test_official_run_reaps_stale_bootstrap_before_cleanliness_and_baseline(self) -> None:
        stale = self._artifact()
        self._mark_owner_dead(stale.artifact_owner_path)
        accounts = {
            name: AccountConfig(
                name=name,
                label=name,
                codex_home=self.root / f"{name}-home",
                model="",
                reasoning_effort="high",
                backend="app_server",
            )
            for name in ("architect", "executor", "reviewer")
        }
        config = OrchestratorConfig(
            repository=self.repository,
            runs_dir=self.root / "official-runs",
            max_correction_cycles=0,
            require_clean_git=True,
            codex_command="codex",
            accounts=accounts,
            roles={"architect": "architect", "executor": "executor", "reviewer": "reviewer", "orchestrator": "architect"},
            project_root=Path.cwd(),
            config_path=self.root / "config.toml",
        )
        task = self.root / "brief.md"
        task.write_text("Cleanliness probe.", encoding="utf-8")
        calls: list[str] = []

        def fake_dispatch(**kwargs):
            role = kwargs["role"]
            calls.append(role)
            payload = (
                {"summary": "plan", "steps": [], "acceptance_criteria": [], "risks": [], "files_to_inspect": [], "skills_loaded": []}
                if role == "architect"
                else {"summary": "implementation", "files_changed": [], "commands_run": [], "tests": [], "remaining_issues": []}
                if role == "executor"
                else {"verdict": "approved", "summary": "approved", "findings": []}
            )
            kwargs["output_path"].write_text(json.dumps(payload), encoding="utf-8")
            return CommandResult(["fake"], 0, "", "", {"role": role, "backend": "app_server", "repository": str(self.repository)})

        with patch("dual_codex.bootstrap.CANONICAL_INSTRUCTIONS_ROOT", self.instructions), patch(
            "dual_codex.orchestrator.delegate_to_configured_actor", side_effect=fake_dispatch
        ):
            outcome = execute(config, task)

        baseline = json.loads((outcome.run_dir / "initial_git_baseline.json").read_text(encoding="utf-8"))
        state = json.loads((outcome.run_dir / "run_state.json").read_text(encoding="utf-8"))
        self.assertEqual(calls, ["architect", "executor", "reviewer"])
        self.assertFalse(stale.artifact_path.exists())
        self.assertEqual(baseline["status_entries"], [])
        self.assertEqual(state["bootstrap_cleanup"]["removed"][0]["name"], stale.artifact_path.name)


if __name__ == "__main__":
    unittest.main()
