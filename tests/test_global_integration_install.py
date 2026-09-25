from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = REPOSITORY_ROOT / "scripts" / "install-dual-agents-integration.ps1"
GLOBAL_AGENTS_FIXTURE = REPOSITORY_ROOT / "tests" / "fixtures" / "codex-global-AGENTS.md"
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


@unittest.skipUnless(POWERSHELL, "PowerShell is required")
class GlobalIntegrationInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.global_root = root / "codex-global"
        self.global_root.mkdir()
        shutil.copyfile(GLOBAL_AGENTS_FIXTURE, self.global_root / "AGENTS.md")
        self.config = root / "dual-agents.toml"
        self.config.write_text("# test config path; contents are not loaded by the installer\n", encoding="utf-8")

    def _run_installer(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                POWERSHELL,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(INSTALLER),
                "-GlobalRoot",
                str(self.global_root),
                "-ConfigPath",
                str(self.config),
                *arguments,
            ],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def _managed_hashes(self) -> dict[str, str]:
        paths = [
            self.global_root / "AGENTS.md",
            self.global_root / "AGENTS.md.pre-dual-agents.bak",
            self.global_root / "dual-agents-integration.json",
            self.global_root / "skills" / "dual-agents" / "SKILL.md",
            self.global_root / "skills" / "dual-agents" / "SKILL.md.dual-agents-managed",
            self.global_root / "bin" / "dual-codex.ps1",
            self.global_root / "bin" / "dual-codex.ps1.dual-agents-managed",
        ]
        return {
            str(path.relative_to(self.global_root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths
            if path.is_file()
        }

    def test_install_verify_and_repeat_are_idempotent_and_repo_owned(self) -> None:
        first = self._run_installer()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        before = self._managed_hashes()

        second = self._run_installer()
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(before, self._managed_hashes())

        verify = self._run_installer("-Verify")
        self.assertEqual(verify.returncode, 0, verify.stdout + verify.stderr)

        manifest = json.loads((self.global_root / "dual-agents-integration.json").read_text(encoding="utf-8"))
        self.assertEqual(Path(manifest["repository_root"]), REPOSITORY_ROOT)
        self.assertEqual(Path(manifest["config_path"]), self.config.resolve())
        installed_skill = (self.global_root / "skills" / "dual-agents" / "SKILL.md").read_text(encoding="utf-8")
        self.assertEqual(installed_skill, (REPOSITORY_ROOT / "skills" / "dual-agents" / "SKILL.md").read_text(encoding="utf-8"))
        global_policy = (self.global_root / "AGENTS.md").read_text(encoding="utf-8")
        self.assertEqual(global_policy.count("DUAL_AGENTS_GLOBAL_ARCHITECTURE_BEGIN"), 1)
        self.assertIn("provider capability matrix", global_policy)
        self.assertIn("only to the user-facing entrypoint before its first", global_policy)
        self.assertIn("do not invoke the entrypoint recursively", global_policy)
        self.assertNotIn("only active `executor` backend", global_policy)

    def test_verify_fails_closed_on_modified_installed_skill(self) -> None:
        installed_skill = self.global_root / "skills" / "dual-agents" / "SKILL.md"
        installed_skill.parent.mkdir(parents=True)
        installed_skill.write_text("unowned prior file\n", encoding="utf-8")
        refused = self._run_installer()
        self.assertNotEqual(refused.returncode, 0)

        installed_skill.unlink()
        installed_skill.parent.rmdir()
        installed = self._run_installer()
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        installed_skill.write_text(installed_skill.read_text(encoding="utf-8") + "local drift\n", encoding="utf-8")
        drift = self._run_installer("-Verify")
        self.assertNotEqual(drift.returncode, 0)


if __name__ == "__main__":
    unittest.main()
