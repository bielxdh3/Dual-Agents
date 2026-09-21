from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dual_codex import bootstrap


def _write_fixture(root: Path) -> None:
    (root / "AGENTS.md").write_text("# isolated canonical policy\n", encoding="utf-8")
    skills = root / "skills"
    skills.mkdir()
    for name in ("memory", "ponytail", "project-phase-review", "project-security-review"):
        skill = skills / name
        skill.mkdir()
        (skill / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")


class CanonicalBootstrapTests(unittest.TestCase):
    def test_default_root_remains_strict_machine_wide_location(self) -> None:
        self.assertEqual(bootstrap.CANONICAL_INSTRUCTIONS_ROOT, Path(r"C:\CodexGlobal"))

        with tempfile.TemporaryDirectory() as temp:
            missing = Path(temp) / "missing-canonical-policy"
            with patch.object(bootstrap, "CANONICAL_INSTRUCTIONS_ROOT", missing):
                with self.assertRaisesRegex(FileNotFoundError, "Canonical instruction file"):
                    bootstrap.canonical_instructions_root()

    def test_explicit_fixture_override_preserves_trusted_source_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "CodexGlobal"
            root.mkdir()
            _write_fixture(root)
            artifact_dir = Path(temp) / "run-artifacts"
            source_before = (root / "AGENTS.md").read_bytes()

            snapshot = bootstrap.create_canonical_bootstrap(
                role="architect",
                root=root,
                artifact_dir=artifact_dir,
            )
            metadata = snapshot.metadata()

            self.assertEqual(snapshot.source_root, root)
            self.assertEqual(metadata["canonical_bootstrap_source_path"], str(root))
            self.assertTrue(metadata["canonical_bootstrap_source_sha256"])
            self.assertTrue(metadata["canonical_bootstrap_artifact_sha256"])
            self.assertEqual(metadata["canonical_bootstrap_delivery"], "trusted_inline")
            self.assertEqual(metadata["canonical_bootstrap_mechanism"], "ephemeral-run-artifact")
            self.assertTrue(snapshot.artifact_path)
            self.assertTrue(snapshot.artifact_path.is_relative_to(artifact_dir.resolve()))
            self.assertIn(str(root), snapshot.artifact_path.read_text(encoding="utf-8"))
            self.assertEqual((root / "AGENTS.md").read_bytes(), source_before)

            bootstrap.cleanup_canonical_bootstrap(snapshot)
            self.assertFalse(snapshot.artifact_path.exists())


if __name__ == "__main__":
    unittest.main()
