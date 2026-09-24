from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dual_codex import bootstrap


ARCHITECT_BASELINE = (
    "memory",
    "ponytail",
    "project-phase-review",
    "project-security-review",
)


def _write_fixture(root: Path) -> None:
    (root / "AGENTS.md").write_text("# isolated canonical policy\n", encoding="utf-8")
    skills = root / "skills"
    skills.mkdir()
    for name in (
        "memory",
        "ponytail",
        "project-phase-review",
        "project-security-review",
        "task-specific",
    ):
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
                selected_skills=(),
            )
            metadata = snapshot.metadata()

            self.assertEqual(snapshot.source_root, root)
            self.assertEqual(metadata["canonical_bootstrap_source_path"], str(root))
            self.assertTrue(metadata["canonical_bootstrap_source_sha256"])
            self.assertTrue(metadata["canonical_bootstrap_artifact_sha256"])
            self.assertEqual(metadata["canonical_bootstrap_delivery"], "trusted_inline")
            self.assertEqual(metadata["canonical_bootstrap_mechanism"], "ephemeral-run-artifact")
            self.assertEqual(metadata["canonical_bootstrap_selected_skills"], list(ARCHITECT_BASELINE))
            self.assertEqual(
                bootstrap.select_required_skills("architect", "Requires task-specific skill."),
                ARCHITECT_BASELINE,
            )
            self.assertEqual(
                metadata["canonical_bootstrap_skill_catalog"]["task-specific"],
                hashlib.sha256((root / "skills" / "task-specific" / "SKILL.md").read_bytes()).hexdigest(),
            )
            self.assertTrue(metadata["canonical_bootstrap_skill_catalog_sha256"])
            self.assertTrue(snapshot.artifact_path)
            self.assertTrue(snapshot.artifact_path.is_relative_to(artifact_dir.resolve()))
            self.assertIn(str(root), snapshot.artifact_path.read_text(encoding="utf-8"))
            self.assertEqual((root / "AGENTS.md").read_bytes(), source_before)

            bootstrap.cleanup_canonical_bootstrap(snapshot)
            self.assertFalse(snapshot.artifact_path.exists())

    def test_architect_skill_provenance_requires_and_hashes_canonical_skills(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "CodexGlobal"
            root.mkdir()
            _write_fixture(root)
            snapshot = bootstrap.create_canonical_bootstrap(role="architect", root=root)

            with self.assertRaisesRegex(ValueError, "skills_loaded"):
                bootstrap.finalize_architect_bootstrap(snapshot, [])
            with self.assertRaisesRegex(ValueError, "mandatory baseline skills"):
                bootstrap.finalize_architect_bootstrap(snapshot, ["task-specific"])
            with self.assertRaisesRegex(FileNotFoundError, "Required canonical skill"):
                bootstrap.finalize_architect_bootstrap(snapshot, [*ARCHITECT_BASELINE, "missing"])

            finalized = bootstrap.finalize_architect_bootstrap(
                snapshot, [*ARCHITECT_BASELINE, "task-specific"]
            )
            metadata = finalized.metadata()
            skill_digest = hashlib.sha256(
                (root / "skills" / "task-specific" / "SKILL.md").read_bytes()
            ).hexdigest()
            self.assertEqual(finalized.selected_skills, (*ARCHITECT_BASELINE, "task-specific"))
            self.assertNotEqual(finalized.source_sha256, snapshot.source_sha256)
            self.assertEqual(
                metadata["canonical_bootstrap_skill_digests"]["task-specific"],
                skill_digest,
            )
            self.assertEqual(
                metadata["canonical_bootstrap_source_files"]["skills/task-specific/SKILL.md"],
                skill_digest,
            )

    def test_architect_mixed_delivery_records_inline_artifact_and_skill_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "CodexGlobal"
            root.mkdir()
            _write_fixture(root)
            snapshot = bootstrap.create_canonical_bootstrap(
                role="architect",
                root=root,
                artifact_dir=Path(temp) / "run-artifacts",
            )

            finalized = bootstrap.finalize_architect_bootstrap(
                snapshot, [*ARCHITECT_BASELINE, "task-specific"]
            )
            metadata = finalized.metadata()

            self.assertEqual(metadata["canonical_bootstrap_delivery"], "mixed")
            self.assertEqual(
                metadata["canonical_bootstrap_artifact_source_files"],
                {
                    "AGENTS.md": metadata["canonical_bootstrap_source_files"]["AGENTS.md"],
                    **{
                        f"skills/{name}/SKILL.md": metadata["canonical_bootstrap_source_files"][f"skills/{name}/SKILL.md"]
                        for name in ARCHITECT_BASELINE
                    },
                },
            )
            self.assertIn("skills/task-specific/SKILL.md", metadata["canonical_bootstrap_source_files"])
            self.assertNotIn(
                "skills/task-specific/SKILL.md",
                metadata["canonical_bootstrap_artifact_source_files"],
            )
            self.assertEqual(
                metadata["canonical_bootstrap_artifact_source_sha256"],
                snapshot.source_sha256,
            )
            self.assertIn(
                f"canonical_source_sha256: {metadata['canonical_bootstrap_artifact_source_sha256']}",
                snapshot.artifact_path.read_text(encoding="utf-8"),
            )

    def test_architect_skill_change_after_dispatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "CodexGlobal"
            root.mkdir()
            _write_fixture(root)
            snapshot = bootstrap.create_canonical_bootstrap(role="architect", root=root)
            skill = root / "skills" / "task-specific" / "SKILL.md"
            skill.write_text("# changed after dispatch\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "changed while the Architect mission was running"):
                bootstrap.finalize_architect_bootstrap(
                    snapshot, [*ARCHITECT_BASELINE, "task-specific"]
                )

    def test_unattended_architect_reads_task_before_loading_its_skill(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "CodexGlobal"
            root.mkdir()
            _write_fixture(root)
            task_artifact = Path(temp) / "architect task.md"
            task_artifact.write_text(
                "Implement the task-specific change. Required skill: task-specific.",
                encoding="utf-8",
            )
            bootstrap_artifact_dir = Path(temp) / "run-artifacts"
            snapshot = bootstrap.create_canonical_bootstrap(
                role="architect",
                root=root,
                artifact_dir=bootstrap_artifact_dir,
            )
            prompt, _ = bootstrap.configured_actor_prompt(
                f"Task artifact: {task_artifact}",
                role="architect",
                bootstrap=snapshot,
            )

            self.assertEqual(snapshot.selected_skills, ARCHITECT_BASELINE)
            self.assertIn(f"Task artifact: {task_artifact}", prompt)
            self.assertIn("already loaded", prompt)
            self.assertIn("first read only the supplied task/architect artifact", prompt)
            self.assertIn("authorized read-only pre-read exception", prompt)
            self.assertIn("select all additional applicable skills from the canonical catalog", prompt)
            self.assertIn("read each complete SKILL.md", prompt)
            self.assertIn("Do not ask the user to choose or identify skills", prompt)
            self.assertIn("until AGENTS.md and all selected skills are loaded", prompt)
            snapshot_text = snapshot.artifact_path.read_text(encoding="utf-8")
            for name in ARCHITECT_BASELINE:
                self.assertIn(f"## skills/{name}/SKILL.md", snapshot_text)
            self.assertNotIn("## skills/task-specific/SKILL.md", snapshot_text)

            # Simulate the unattended Architect's permitted bootstrap sequence:
            # read only the supplied brief, select from its contents, load the
            # matching canonical skill, then proceed without a user prompt.
            events: list[str] = ["mandatory-baseline-loaded", "task-artifact-supplied"]
            brief = task_artifact.read_text(encoding="utf-8")
            events.append("task-artifact-read")
            selected = "task-specific" if "Required skill: task-specific" in brief else ""
            self.assertTrue(selected)
            events.append(f"skill-selected:{selected}")
            skill_text = (root / "skills" / selected / "SKILL.md").read_text(encoding="utf-8")
            self.assertIn("# task-specific", skill_text)
            events.append(f"skill-loaded:{selected}")
            finalized = bootstrap.finalize_architect_bootstrap(
                snapshot, [*ARCHITECT_BASELINE, selected]
            )
            self.assertEqual(finalized.selected_skills, (*ARCHITECT_BASELINE, selected))
            events.append("mission-proceeded")
            self.assertEqual(
                events,
                [
                    "mandatory-baseline-loaded",
                    "task-artifact-supplied",
                    "task-artifact-read",
                    "skill-selected:task-specific",
                    "skill-loaded:task-specific",
                    "mission-proceeded",
                ],
            )


if __name__ == "__main__":
    unittest.main()
