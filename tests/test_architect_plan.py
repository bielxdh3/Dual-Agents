from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from dual_codex.architect_plan import ArchitectPlanError, parse_architect_result
from dual_codex.bootstrap import create_canonical_bootstrap
from dual_codex.codex import _annotate_provider_result, _finalize_architect_output
from dual_codex.config import AgentConfig
from dual_codex.process import CommandResult


def _plan(*, skills: list[str] | None = None) -> dict[str, object]:
    return {
        "summary": "A focused plan",
        "steps": ["Inspect the requested files"],
        "acceptance_criteria": ["The requested behavior is covered"],
        "risks": [],
        "files_to_inspect": ["README.md"],
        "skills_loaded": skills or [],
    }


class ArchitectPlanTests(unittest.TestCase):
    def test_extracts_one_fenced_object_from_normal_assistant_output(self) -> None:
        raw = "Here is the plan:\n```json\n" + json.dumps(_plan()) + "\n```\n"
        self.assertEqual(parse_architect_result(raw), _plan())

    def test_rejects_conflicting_authoritative_plan_objects(self) -> None:
        first = json.dumps(_plan())
        second = json.dumps({**_plan(), "summary": "A different plan"})
        with self.assertRaisesRegex(ArchitectPlanError, "2 authoritative plan objects"):
            parse_architect_result(first + "\n" + second)

    def test_rejects_duplicate_json_fields(self) -> None:
        raw = '{"summary":"first","summary":"second"}'
        with self.assertRaisesRegex(ArchitectPlanError, "duplicate JSON field 'summary'"):
            parse_architect_result(raw)

    def test_validates_required_plan_fields_locally(self) -> None:
        incomplete = _plan()
        incomplete.pop("risks")
        with self.assertRaisesRegex(ArchitectPlanError, "missing required field.*risks"):
            parse_architect_result(json.dumps(incomplete))

    def test_invalid_completed_turn_is_preserved_and_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            canonical = root / "CodexGlobal"
            (canonical / "skills").mkdir(parents=True)
            (canonical / "AGENTS.md").write_text("# policy\n", encoding="utf-8")
            for name in ("memory", "ponytail", "project-phase-review", "project-security-review"):
                skill = canonical / "skills" / name
                skill.mkdir()
                (skill / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
            bootstrap = create_canonical_bootstrap(role="architect", root=canonical)
            raw = "Completed turn with malformed plan: {\"summary\":\"missing fields\"}"
            output = root / "plan.json"
            agent = AgentConfig(
                codex_home=root / "profile",
                model="model",
                reasoning_effort="high",
                sandbox="read-only",
                backend="app_server",
            )
            result = CommandResult(["codex", "app-server"], 0, raw, "")

            annotated = _annotate_provider_result(
                result,
                agent,
                "architect",
                repository=root,
                bootstrap=bootstrap,
                output_path=output,
            )

            self.assertEqual(annotated.returncode, 1)
            self.assertIn("Architect plan validation failed", annotated.stderr)
            self.assertIn("Completed output preserved", annotated.stderr)
            self.assertEqual(Path(str(output) + ".raw.txt").read_text(encoding="utf-8"), raw)

    def test_final_plan_rewrites_case_variant_to_canonical_catalog_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            canonical = root / "CodexGlobal"
            skills = canonical / "skills"
            skills.mkdir(parents=True)
            (canonical / "AGENTS.md").write_text("# policy\n", encoding="utf-8")
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
            bootstrap = create_canonical_bootstrap(role="architect", root=canonical)
            output = root / "plan.json"
            output.write_text(
                json.dumps(_plan(skills=["Task-Specific"])),
                encoding="utf-8",
            )

            finalized = _finalize_architect_output(bootstrap, output)
            plan = json.loads(output.read_text(encoding="utf-8"))
            metadata = finalized.metadata()
            self.assertEqual(plan["skills_loaded"], ["task-specific"])
            self.assertEqual(finalized.actor_selected_skills, ("task-specific",))
            self.assertEqual(
                metadata["canonical_bootstrap_actor_selected_skills"],
                ["task-specific"],
            )
            self.assertIn(
                "task-specific",
                metadata["canonical_bootstrap_actor_selected_skill_digests"],
            )


if __name__ == "__main__":
    unittest.main()
