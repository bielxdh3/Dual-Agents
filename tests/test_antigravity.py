from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from dual_codex.antigravity import antigravity_status, build_command, run_antigravity
from dual_codex.config import AgentConfig


def _agent() -> AgentConfig:
    return AgentConfig(
        codex_home=Path("C:/CodexProfiles/executor"),
        model="gemini-3.8-flash-high",
        reasoning_effort="high",
        sandbox="workspace-write",
        account_name="executor",
        label="Antigravity/Gemini Executor",
        backend="antigravity",
    )


def _mock_command(root: Path) -> Path:
    script = root / "mock_agy.py"
    script.write_text(
        "\n".join(
            [
                "import json, sys",
                "args = sys.argv[1:]",
                "if args == ['--version']:",
                "    print('agy 1.2.5')",
                "    raise SystemExit(0)",
                "line = sys.stdin.readline()",
                "message = json.loads(line)",
                "text = message['message']['content'][0]['text']",
                "if 'malformed' in text:",
                "    print('{not-json', flush=True)",
                "    raise SystemExit(0)",
                "if 'error' in text:",
                "    error_report = {'summary':'should not be accepted','files_changed':['hello.txt'],'commands_run':[],'tests':[],'remaining_issues':[]}",
                "    error_response = json.dumps(error_report) + json.dumps({'toolAction':'Finishing task','toolSummary':'Submit task completion report'})",
                "    print(json.dumps({'event':'result','result':{'status':'ERROR','error':'blocked by mock','response':error_response,'structured_output':error_report}}), flush=True)",
                "    raise SystemExit(2)",
                "if 'cancel' in text:",
                "    print(json.dumps({'event':'result','result':{'status':'CANCELED','error':'cancelled by mock'}}), flush=True)",
                "    raise SystemExit(1)",
                "if 'structured' in text:",
                "    structured_report = {'summary':'structured complete','files_changed':['hello.txt'],'commands_run':[],'tests':[{'command':'view_file hello.txt','status':'passed','details':'FINAL'}],'remaining_issues':[],'memory_updates':[]}",
                "    response = json.dumps(structured_report) + json.dumps({'commands_run':[],'files_changed':['hello.txt'],'memory_updates':[],'remaining_issues':[],'summary':'structured complete','tests':[],'toolAction':'Finishing task','toolSummary':'Submit task completion report'})",
                "    print(json.dumps({'event':'result','result':{'status':'SUCCESS','conversation_id':'mock-conversation','response':response,'structured_output':structured_report}}), flush=True)",
                "    raise SystemExit(0)",
                "print(json.dumps({'event':'init','conversation_id':'mock-conversation'}), flush=True)",
                "print(json.dumps({'event':'step_update','step_update':{'conversation_id':'mock-conversation'}}), flush=True)",
                "report = {'summary':'mock complete','files_changed':[],'commands_run':[],'tests':[],'remaining_issues':[]}",
                "print(json.dumps({'event':'result','result':{'status':'SUCCESS','conversation_id':'mock-conversation','response':json.dumps(report)}}), flush=True)",
                "raise SystemExit(0)",
                "",
            ]
        ),
        encoding="utf-8",
    )
    wrapper = root / "mock_agy.cmd"
    wrapper.write_text(f'@echo off\n"{sys.executable}" "{script}" %*\n', encoding="utf-8")
    return wrapper


def _workspace_probe_command(root: Path) -> Path:
    script = root / "mock workspace probe.py"
    script.write_text(
        "\n".join(
            [
                "import json, pathlib, sys",
                "args = sys.argv[1:]",
                "if args == ['--version']:",
                "    print('agy 1.2.5')",
                "    raise SystemExit(0)",
                "sys.stdin.readline()",
                "pathlib.Path('cwd-probe.txt').write_text(str(pathlib.Path.cwd()), encoding='utf-8')",
                "print(json.dumps({'event':'result','result':{'status':'SUCCESS','conversation_id':'probe-conversation','response':json.dumps({'summary':'probe','files_changed':['cwd-probe.txt'],'commands_run':[],'tests':[],'remaining_issues':[]})}}), flush=True)",
                "raise SystemExit(0)",
                "",
            ]
        ),
        encoding="utf-8",
    )
    wrapper = root / "mock workspace probe.cmd"
    wrapper.write_text(f'@echo off\n"{sys.executable}" "{script}" %*\n', encoding="utf-8")
    return wrapper


class AntigravityTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self._instruction_temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._instruction_temp.cleanup)
        self.instruction_root = Path(self._instruction_temp.name)
        (self.instruction_root / "AGENTS.md").write_text(
            "# Synthetic test instructions\n",
            encoding="utf-8",
        )
        (self.instruction_root / "skills").mkdir()
        self._instruction_patch = patch(
            "dual_codex.antigravity._CANONICAL_INSTRUCTIONS_ROOT",
            self.instruction_root,
        )
        self._instruction_patch.start()
        self.addCleanup(self._instruction_patch.stop)

    def test_command_uses_stream_protocol_without_permission_bypass(self) -> None:
        command = build_command(
            command="agy",
            agent=_agent(),
            repository=Path("C:/workspace/project"),
            schema_path=Path("C:/workspace/schema.json"),
            timeout_seconds=30,
        )
        self.assertIn("--input-format", command)
        self.assertIn("stream-json", command)
        self.assertIn("--output-format", command)
        self.assertIn("--mode", command)
        self.assertIn("accept-edits", command)
        self.assertIn("--new-project", command)
        self.assertIn("--add-dir", command)
        self.assertIn(str(self.instruction_root), command)
        self.assertNotIn(r"C:\CodexGlobal", command)
        self.assertIn("--json-schema", command)
        self.assertNotIn("--dangerously-skip-permissions", command)

    def test_followup_reuses_conversation_without_rebinding_project(self) -> None:
        command = build_command(
            command="agy",
            agent=_agent(),
            repository=Path("C:/workspace/project"),
            conversation_id="conversation-1",
        )
        self.assertIn("--conversation", command)
        self.assertNotIn("--new-project", command)

    def test_status_and_success_parse_headless_ndjson(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "repository"
            repository.mkdir()
            command = _mock_command(root)
            self.assertEqual(antigravity_status(str(command), cwd=repository), "OK")
            output = root / "report.json"
            result = run_antigravity(
                command=str(command),
                agent=_agent(),
                repository=repository,
                prompt="implement",
                output_path=output,
                config=SimpleNamespace(antigravity_turn_timeout=5),
            )
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.metadata["executor_provider"], "antigravity")
            self.assertEqual(result.metadata["antigravity_terminal_status"], "SUCCESS")
            self.assertEqual(result.metadata["antigravity_conversation_id"], "mock-conversation")
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["summary"], "mock complete")

    def test_structured_output_is_authoritative_over_auxiliary_response_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "repository"
            repository.mkdir()
            command = _mock_command(root)
            output = root / "report.json"
            result = run_antigravity(
                command=str(command),
                agent=_agent(),
                repository=repository,
                prompt="structured",
                output_path=output,
                config=SimpleNamespace(antigravity_turn_timeout=5),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.metadata["antigravity_result_source"], "structured_output")
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["summary"], "structured complete")

    def test_explicit_workspace_reaches_child_cwd_and_artifact_access(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "smoke workspace with spaces"
            repository.mkdir()
            artifact_dir = root / "runtime" / "executor-task-artifacts"
            artifact_dir.mkdir(parents=True)
            artifact = artifact_dir / "task.md"
            artifact.write_text("task\n", encoding="utf-8")
            command = _workspace_probe_command(root)
            result = run_antigravity(
                command=str(command),
                agent=_agent(),
                repository=repository,
                prompt="probe",
                output_path=root / "report.json",
                task_artifact_path=artifact,
                config=SimpleNamespace(antigravity_turn_timeout=5),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.metadata["antigravity_repository"], str(repository.resolve()))
            self.assertEqual(result.metadata["antigravity_cwd"], str(repository.resolve()))
            self.assertEqual((repository / "cwd-probe.txt").read_text(encoding="utf-8"), str(repository.resolve()))
            self.assertFalse((root / "cwd-probe.txt").exists())
            self.assertEqual(
                result.command[result.command.index("--add-dir") + 1],
                str(repository.resolve()),
            )
            self.assertIn("--new-project", result.command)
            self.assertIn(str(artifact_dir.resolve()), result.command)
            self.assertNotIn(str(root.resolve()), result.command)
            self.assertIn(str(self.instruction_root), result.command)
            self.assertNotIn(r"C:\CodexGlobal", result.command)

    def test_missing_workspace_fails_before_child_launch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            missing = root / "missing workspace"
            command = _mock_command(root)
            with patch("dual_codex.antigravity.subprocess.Popen") as popen:
                result = run_antigravity(
                    command=str(command),
                    agent=_agent(),
                    repository=missing,
                    prompt="probe",
                    output_path=root / "report.json",
                    config=SimpleNamespace(antigravity_turn_timeout=5),
                )

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.metadata["antigravity_terminal_status"], "WORKSPACE_UNAVAILABLE")
            self.assertIn("does not exist", result.stderr)
            popen.assert_not_called()

    def test_malformed_and_error_terminal_events_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "repository"
            repository.mkdir()
            command = _mock_command(root)
            malformed = run_antigravity(
                command=str(command),
                agent=_agent(),
                repository=repository,
                prompt="malformed",
                output_path=root / "malformed.json",
                config=SimpleNamespace(antigravity_turn_timeout=5),
            )
            self.assertNotEqual(malformed.returncode, 0)
            self.assertEqual(malformed.metadata["antigravity_terminal_status"], "MALFORMED")
            failed = run_antigravity(
                command=str(command),
                agent=_agent(),
                repository=repository,
                prompt="error",
                output_path=root / "error.json",
                config=SimpleNamespace(antigravity_turn_timeout=5),
            )
            self.assertNotEqual(failed.returncode, 0)
            self.assertEqual(failed.metadata["antigravity_terminal_status"], "ERROR")
            self.assertIn("blocked by mock", failed.stderr)
            self.assertFalse((root / "error.json").exists())
            canceled = run_antigravity(
                command=str(command),
                agent=_agent(),
                repository=repository,
                prompt="cancel",
                output_path=root / "cancel.json",
                config=SimpleNamespace(antigravity_turn_timeout=5),
            )
            self.assertNotEqual(canceled.returncode, 0)
            self.assertEqual(canceled.metadata["antigravity_terminal_status"], "CANCELED")

    def test_missing_canonical_policy_blocks_launch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "repository"
            repository.mkdir()
            with patch("dual_codex.antigravity._CANONICAL_INSTRUCTIONS_ROOT", root / "missing"):
                result = run_antigravity(
                    command="agy",
                    agent=_agent(),
                    repository=repository,
                    prompt="implement",
                    output_path=root / "report.json",
                    config=SimpleNamespace(antigravity_turn_timeout=5),
                )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.metadata["antigravity_terminal_status"], "INSTRUCTIONS_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
