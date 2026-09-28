from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from dual_codex.app_server import (
    AppServerError,
    _PROCESSES,
    _canonical_workspace_roots,
    _app_server_command,
    _finalize_turn_failure,
    _load_thread_mapping,
    _mapping_path,
    _normalise_report,
    _provider_turn_failure_class,
    _process_key,
    _get_process,
    _save_thread_mapping,
    _sanitize_stderr,
    _workspace_write_sandbox_policy,
    app_server_call,
    run_codex_app_server,
)
from dual_codex.codex import _report_from_message
from dual_codex.config import AgentConfig, OrchestratorConfig
from dual_codex.live_events import read_journal
from dual_codex.paths import same_path
from dual_codex.process import codex_environment, executor_npm_cache


class _FakeStdout:
    def __init__(self) -> None:
        self.lines: queue.Queue[str | None] = queue.Queue()

    def __iter__(self):
        while True:
            line = self.lines.get()
            if line is None:
                return
            yield line


class _FakeStdin:
    def __init__(self, process: "_FakeProcess") -> None:
        self.process = process

    def write(self, value: str) -> None:
        for line in value.splitlines():
            self.process.handle(json.loads(line))

    def flush(self) -> None:
        return None

    def close(self) -> None:
        return None


class _FakeProcess:
    next_pid = 4100

    def __init__(self, *args, **kwargs) -> None:
        self.pid = _FakeProcess.next_pid
        _FakeProcess.next_pid += 1
        self.stdout = _FakeStdout()
        self.stderr = _FakeStdout()
        self.stdin = _FakeStdin(self)
        self.returncode = None
        self.prompts: list[str] = []
        self.initialize_params: list[dict] = []
        self.turn_params: list[dict] = []
        self.thread_params: list[dict] = []
        self.request_order: list[str] = []
        self.thread_id = "thread-probe"
        self.turn_number = 0
        self.command_exec_params: list[dict] = []
        self.start_error = False
        self.resume_error = False
        self.response_roots_override: list[str] | None = None
        self.windows_sandbox = None
        self.windows_sandbox_readiness = "ready"

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def terminate(self):
        self.returncode = 0
        self.stdout.lines.put(None)

    def kill(self):
        self.terminate()

    def _emit(self, message: dict) -> None:
        self.stdout.lines.put(json.dumps(message) + "\n")

    def handle(self, message: dict) -> None:
        method = message.get("method")
        request_id = message.get("id")
        if request_id is not None:
            self.request_order.append(method)
        if method == "initialize":
            self.initialize_params.append(message["params"])
            self._emit(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "codexHome": "C:/CodexProfiles/executor",
                        "platformFamily": "windows",
                        "platformOs": "windows",
                        "userAgent": "Codex Desktop/test",
                    },
                }
            )
        elif method == "thread/start":
            self.thread_params.append(message["params"])
            repository = message["params"]["cwd"]
            if self.start_error:
                self._emit({"jsonrpc": "2.0", "id": request_id, "error": {"message": "thread start failed"}})
                return
            roots = self.response_roots_override or [repository]
            self._emit(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "thread": {
                            "id": self.thread_id,
                            "environments": [
                                {
                                    "environmentId": "local",
                                    "cwd": repository,
                                    "runtimeWorkspaceRoots": roots,
                                }
                            ],
                        },
                        "cwd": repository,
                        "runtimeWorkspaceRoots": roots,
                    },
                }
            )
        elif method == "thread/resume":
            repository = message["params"]["cwd"]
            if self.resume_error:
                self._emit(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"message": "no rollout found for thread id"},
                    }
                )
                return
            roots = self.response_roots_override or [repository]
            self._emit(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "thread": {
                            "id": self.thread_id,
                            "environments": [
                                {
                                    "environmentId": "local",
                                    "cwd": repository,
                                    "runtimeWorkspaceRoots": roots,
                                }
                            ],
                        },
                        "cwd": repository,
                        "runtimeWorkspaceRoots": roots,
                    },
                }
            )
        elif method == "turn/start":
            self.turn_number += 1
            turn_id = f"turn-{self.turn_number}"
            text = message["params"]["input"][0]["text"]
            self.prompts.append(text)
            self.turn_params.append(message["params"])
            report = {
                "summary": "probe",
                "files_changed": ["probe.txt"],
                "commands_run": [],
                "tests": [],
                "remaining_issues": [],
            }
            self._emit({"jsonrpc": "2.0", "id": request_id, "result": {"turn": {"id": turn_id}}})
            self._emit({"jsonrpc": "2.0", "method": "turn/started", "params": {"threadId": self.thread_id, "turn": {"id": turn_id}}})
            self._emit({"jsonrpc": "2.0", "method": "turn/completed", "params": {"threadId": self.thread_id, "turn": {"id": turn_id, "status": "completed", "items": [{"type": "agentMessage", "text": json.dumps(report)}]}}})
        elif method == "config/read":
            windows = None if self.windows_sandbox is None else {"sandbox": self.windows_sandbox}
            self._emit({"jsonrpc": "2.0", "id": request_id, "result": {"config": {"windows": windows}, "origins": {}}})
        elif method == "windowsSandbox/readiness":
            self._emit({"jsonrpc": "2.0", "id": request_id, "result": {"status": self.windows_sandbox_readiness}})
        elif method == "command/exec":
            params = message["params"]
            self.command_exec_params.append(params)
            argv = params.get("command", [])
            if len(argv) >= 2 and argv[0] == "node" and argv[1] == "-e":
                stdout = (
                    json.dumps({"registry": True, "prisma_host": True})
                    if "checks={registry:false,prisma_host:false}" in argv[-1]
                    else json.dumps({"child": True, "worker": True, "temp_write": True, "cache_write": True})
                )
            else:
                stdout = "ready"
            self._emit({
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"exitCode": 0, "stdout": stdout, "stderr": ""},
            })


def _config(root: Path) -> OrchestratorConfig:
    return OrchestratorConfig(
        repository=root / "repo",
        runs_dir=root / "runs",
        max_correction_cycles=1,
        require_clean_git=True,
        codex_command="codex",
        accounts={},
        roles={},
        project_root=root,
        config_path=root / "config.toml",
        app_server_turn_timeout=5,
    )


class AppServerTests(unittest.TestCase):
    def tearDown(self) -> None:
        for process in list(_PROCESSES.values()):
            process.close()
        _PROCESSES.clear()

    def _turn_process(
        self,
        repository: Path,
        *,
        timeout: float,
        progress: list[str],
        agent_timeout: float | None = None,
    ):
        from dual_codex.app_server import _AppServerProcess

        process = object.__new__(_AppServerProcess)
        process.agent = SimpleNamespace(
            sandbox="read-only",
            model="",
            reasoning_effort="",
            service_tier="",
            network_access=False,
            app_server_turn_timeout=agent_timeout,
        )
        process.config = SimpleNamespace(app_server_turn_start_timeout=5, app_server_turn_timeout=timeout)
        process.repository = repository
        process.progress = progress.append
        process.role = "executor"
        process.windows_sandbox = ""
        process._events = []
        process.request_methods = []
        process.last_thread_request = {}
        process.last_thread_binding = {}
        process._active_turn_provenance = None
        process.last_turn_provenance = {}
        process._active_thread_id = "thread-1"
        process._active_thread_resumed = False
        process.request = Mock(return_value={"result": {"turn": {"id": "turn-1"}}})
        process._record_notification = Mock()
        process._read_event = Mock(
            side_effect=[
                {"jsonrpc": "2.0", "method": "turn/started", "params": {"turn": {"id": "turn-1"}}},
                {
                    "jsonrpc": "2.0",
                    "method": "turn/completed",
                    "params": {"turn": {"id": "turn-1", "status": "completed", "items": [{"type": "agentMessage", "text": "PRIVATE_REASONING"}]}},
                },
            ]
        )
        return process

    def test_app_server_heartbeat_uses_existing_progress_callback_without_content(self) -> None:
        from dual_codex.app_server import _save_thread_mapping

        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            events: list[str] = []
            process = self._turn_process(repository, timeout=30, progress=events)
            clock = iter([0.0, 0.0, 1.0, 16.0, 16.0, 16.0])
            with patch("dual_codex.app_server.time.monotonic", side_effect=lambda: next(clock, 16.0)), patch(
                "dual_codex.app_server._save_thread_mapping"
            ) as save_mapping:
                result = process._turn_unlocked("thread-1", "PRIVATE_PROMPT", repository)

            self.assertEqual(result["turn_status"], "completed")
            self.assertEqual(events, ["app-server turn turn-1 still running"])
            self.assertNotIn("PRIVATE_PROMPT", "\n".join(events))
            self.assertNotIn("PRIVATE_REASONING", "\n".join(events))
            save_mapping.assert_called_once()

    def test_app_server_turn_timeout_remains_hard_and_does_not_wait_past_deadline(self) -> None:
        from dual_codex.app_server import _AppServerProcess

        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            events: list[str] = []
            process = self._turn_process(repository, timeout=5, progress=events)
            clock = iter([0.0, 0.0, 6.0])
            with patch("dual_codex.app_server.time.monotonic", side_effect=lambda: next(clock)):
                with self.assertRaisesRegex(AppServerError, "Timed out waiting for turn/completed") as raised:
                    process._turn_unlocked("thread-1", "private", repository)
            process._read_event.assert_not_called()
            self.assertEqual(events, [])
            self.assertEqual(raised.exception.failure_class, "HOST_TURN_DEADLINE")
            self.assertEqual(process.last_turn_provenance["termination_classification"], "HOST_TURN_DEADLINE")
            self.assertTrue(process.last_turn_provenance["host_deadline_expired"])

    def test_app_server_turn_provenance_records_effective_timeout_source_and_thread_state(self) -> None:
        from dual_codex.app_server import _save_thread_mapping

        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            process = self._turn_process(repository, timeout=600, agent_timeout=3600, progress=[])
            process._active_thread_resumed = True
            with patch("dual_codex.app_server.time.monotonic", return_value=0.0), patch(
                "dual_codex.app_server._save_thread_mapping"
            ):
                result = process._turn_unlocked("thread-1", "PRIVATE_PROMPT", repository)

            provenance = result["turn_provenance"]
            self.assertEqual(provenance["turn_timeout_seconds"], 3600)
            self.assertEqual(provenance["timeout_source"], "account_role_override")
            self.assertEqual(provenance["thread_state"], "resumed")
            self.assertTrue(provenance["thread_resumed"])
            self.assertEqual(provenance["turn_id"], "turn-1")
            self.assertEqual(provenance["termination_classification"], "TURN_COMPLETED")
            self.assertNotIn("PRIVATE_PROMPT", json.dumps(provenance))
            self.assertNotIn("PRIVATE_REASONING", json.dumps(provenance))

    def test_app_server_turn_provenance_records_global_default_for_fresh_thread(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            process = self._turn_process(repository, timeout=600, progress=[])
            with patch("dual_codex.app_server.time.monotonic", return_value=0.0), patch(
                "dual_codex.app_server._save_thread_mapping"
            ):
                result = process._turn_unlocked("thread-1", "PRIVATE_PROMPT", repository)

            provenance = result["turn_provenance"]
            self.assertEqual(provenance["turn_timeout_seconds"], 600)
            self.assertEqual(provenance["timeout_source"], "global_default")
            self.assertEqual(provenance["thread_state"], "fresh")
            self.assertFalse(provenance["thread_resumed"])
            self.assertIsInstance(provenance["turn_start_monotonic"], float)
            self.assertIsInstance(provenance["turn_start_timestamp"], str)

    def test_app_server_turn_provenance_records_only_safe_provider_event_metadata(self) -> None:
        from dual_codex.app_server import _AppServerProcess

        process = object.__new__(_AppServerProcess)
        process._active_turn_provenance = {"turn_id": "turn-1"}
        process._events = []
        message = {
            "jsonrpc": "2.0",
            "method": "item/started",
            "params": {"turnId": "turn-1", "item": {"type": "agentMessage", "text": "PRIVATE_PROMPT"}},
        }
        process._record_notification(message)
        provenance = process._active_turn_provenance

        self.assertEqual(provenance["last_safe_provider_notification_method"], "item/started")
        self.assertIsInstance(provenance["last_provider_event_timestamp"], str)
        self.assertNotIn("PRIVATE_PROMPT", json.dumps(provenance))

    def test_app_server_failure_provenance_distinguishes_process_exit(self) -> None:
        process = SimpleNamespace(
            last_turn_provenance={
                "termination_classification": "IN_PROGRESS",
                "host_deadline_expired": False,
                "thread_resumed": False,
            },
            process=SimpleNamespace(poll=lambda: 17),
        )
        error = AppServerError(
            "App Server process exited unexpectedly.",
            failure_class="APP_SERVER_PROCESS_EXIT",
            termination_classification="APP_SERVER_PROCESS_EXIT",
        )
        provenance = _finalize_turn_failure(process, error)
        self.assertEqual(provenance["termination_classification"], "APP_SERVER_PROCESS_EXIT")
        self.assertFalse(provenance["host_deadline_expired"])
        self.assertFalse(provenance["app_server_process_alive_at_failure"])
        self.assertEqual(provenance["app_server_process_exit_code"], 17)
        self.assertIn("exit code 17", provenance["failure_reason"])

    def test_app_server_failure_provenance_preserves_provider_error_before_deadline(self) -> None:
        process = SimpleNamespace(
            last_turn_provenance={
                "termination_classification": "APP_SERVER_ERROR_EVENT",
                "host_deadline_expired": False,
                "thread_resumed": False,
            },
            process=SimpleNamespace(poll=lambda: None),
        )
        error = AppServerError(
            "App Server emitted an error notification.",
            failure_class="APP_SERVER_ERROR_EVENT",
            termination_classification="APP_SERVER_ERROR_EVENT",
        )
        provenance = _finalize_turn_failure(process, error)
        self.assertEqual(provenance["termination_classification"], "APP_SERVER_ERROR_EVENT")
        self.assertFalse(provenance["host_deadline_expired"])
        self.assertTrue(provenance["app_server_process_alive_at_failure"])
        self.assertEqual(provenance["failure_reason"], str(error))
        self.assertNotIn("provider_runtime_unavailable", json.dumps(provenance))

    def test_app_server_provider_timeout_is_distinct_from_host_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            process = self._turn_process(repository, timeout=600, progress=[])
            process.process = SimpleNamespace(poll=lambda: None)
            process._read_event = Mock(
                side_effect=[
                    {"jsonrpc": "2.0", "method": "turn/started", "params": {"turn": {"id": "turn-1"}}},
                    {
                        "jsonrpc": "2.0",
                        "method": "turn/completed",
                        "params": {
                            "turn": {
                                "id": "turn-1",
                                "status": "failed",
                                "error": {
                                    "code": "provider_timeout",
                                    "type": "model_timeout",
                                    "message": "PRIVATE_PROMPT PRIVATE_REASONING private provider payload",
                                    "payload": {"secret": "private provider payload"},
                                },
                            }
                        },
                    },
                ]
            )
            with patch("dual_codex.app_server._save_thread_mapping"):
                with self.assertRaises(AppServerError) as raised:
                    process._turn_unlocked("thread-1", "PRIVATE_PROMPT", repository)
            self.assertEqual(raised.exception.failure_class, "PROVIDER_TURN_TIMEOUT")
            provenance = _finalize_turn_failure(process, raised.exception)

        self.assertEqual(provenance["termination_classification"], "PROVIDER_TURN_TIMEOUT")
        self.assertFalse(provenance["host_deadline_expired"])
        self.assertEqual(provenance["provider_error_code"], "provider_timeout")
        self.assertEqual(provenance["provider_error_type"], "model_timeout")
        self.assertEqual(
            provenance["failure_reason"],
            "App Server turn turn-1 ended with status 'failed'. Provider error code=provider_timeout, type=model_timeout.",
        )
        self.assertNotIn("private provider payload", json.dumps(provenance))
        self.assertNotIn("PRIVATE_PROMPT", json.dumps(provenance))
        self.assertNotIn("PRIVATE_REASONING", json.dumps(provenance))

    def test_failure_provenance_keeps_host_timeout_cause_without_prompt_or_secret(self) -> None:
        message = "Timed out waiting for turn/completed (turn-1)."
        process = SimpleNamespace(
            last_turn_provenance={
                "termination_classification": "HOST_TURN_DEADLINE",
                "host_deadline_expired": True,
                "turn_id": "turn-1",
            },
            process=SimpleNamespace(poll=lambda: None),
        )
        provenance = _finalize_turn_failure(
            process,
            AppServerError(message, failure_class="HOST_TURN_DEADLINE", termination_classification="HOST_TURN_DEADLINE"),
        )
        self.assertEqual(provenance["failure_reason"], message)
        serialized = json.dumps(provenance)
        self.assertNotIn("PRIVATE_PROMPT", serialized)
        self.assertNotIn("PRIVATE_REASONING", serialized)
        self.assertNotIn("secret-value", serialized)

    def test_app_server_account_role_timeout_overrides_only_turn_completion(self) -> None:
        from dual_codex.app_server import _save_thread_mapping

        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            process = self._turn_process(repository, timeout=5, agent_timeout=3600, progress=[])
            clock = iter([0.0, 0.0, 6.0, 6.0, 7.0, 7.0])
            with patch("dual_codex.app_server.time.monotonic", side_effect=lambda: next(clock)), patch(
                "dual_codex.app_server._save_thread_mapping"
            ):
                result = process._turn_unlocked("thread-1", "private", repository)

            self.assertEqual(result["turn_status"], "completed")
            self.assertEqual(process.config.app_server_turn_start_timeout, 5)
            self.assertEqual(process.config.app_server_turn_timeout, 5)

    def test_app_server_heartbeat_does_not_extend_hard_turn_deadline(self) -> None:
        from dual_codex.app_server import _AppServerProcess

        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            events: list[str] = []
            process = self._turn_process(repository, timeout=16, progress=events)
            clock = iter([0.0, 0.0, 15.0, 15.0, 17.0])
            with patch("dual_codex.app_server.time.monotonic", side_effect=lambda: next(clock, 17.0)):
                with self.assertRaisesRegex(AppServerError, "Timed out waiting for turn/completed"):
                    process._turn_unlocked("thread-1", "private", repository)

            process._read_event.assert_called_once()
            self.assertEqual(events, ["app-server turn turn-1 still running"])

    def test_structured_turns_reuse_thread_and_clear_api_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "repo"
            repository.mkdir()
            (repository / ".git").mkdir()
            config = _config(root)
            agent = AgentConfig(
                codex_home=root / "profile",
                model="gpt-5.6-luna",
                reasoning_effort="high",
                sandbox="workspace-write",
                account_name="biel4",
                label="executor",
                backend="app_server",
            )
            fake_processes: list[_FakeProcess] = []
            with patch.dict("os.environ", {"LOCALAPPDATA": str(root / "localappdata")}):
                expected_cache = executor_npm_cache(agent)

            def create(*args, **kwargs):
                fake = _FakeProcess(*args, **kwargs)
                fake_processes.append(fake)
                self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
                self.assertNotIn("CODEX_API_KEY", kwargs["env"])
                for key in (
                    "CODEX_INTERNAL_ORIGINATOR_OVERRIDE",
                    "CODEX_MCP_NODE_PATH",
                    "CODEX_APP_TOOLS_PIPE_PATH",
                    "CODEX_PERMISSION_PROFILE",
                    "CODEX_SESSION_ID",
                    "CODEX_THREAD_ID",
                    "CODEX_SHELL",
                    "CODEX_CI",
                ):
                    self.assertNotIn(key, kwargs["env"])
                self.assertEqual(kwargs["env"]["CODEX_HOME"], str(agent.codex_home))
                self.assertIn("PATH", kwargs["env"])
                self.assertTrue(any(key.casefold() == "systemroot" for key in kwargs["env"]))
                return fake

            long_prompt = "x" * 2201
            with patch.dict(
                "os.environ",
                {
                    "OPENAI_API_KEY": "secret",
                    "CODEX_API_KEY": "secret",
                    "LOCALAPPDATA": str(root / "localappdata"),
                    "CODEX_INTERNAL_ORIGINATOR_OVERRIDE": "desktop",
                    "CODEX_MCP_NODE_PATH": "desktop-node",
                    "CODEX_APP_TOOLS_PIPE_PATH": "desktop-pipe",
                    "CODEX_PERMISSION_PROFILE": "desktop-profile",
                    "CODEX_SESSION_ID": "desktop-session",
                    "CODEX_THREAD_ID": "desktop-thread",
                    "CODEX_SHELL": "desktop-shell",
                    "CODEX_CI": "1",
                },
            ), patch(
                "dual_codex.app_server.subprocess.Popen", side_effect=create
            ):
                first = run_codex_app_server(
                    config=config,
                    agent=agent,
                    repository=repository,
                    prompt="short",
                    output_path=root / "first.json",
                    session_id="biel4-session",
                    request_id="request-1",
                    run_id="run-1",
                )
                second = run_codex_app_server(
                    config=config,
                    agent=agent,
                    repository=repository,
                    prompt=long_prompt,
                    output_path=root / "second.json",
                    session_id="biel4-session",
                    request_id="request-1",
                    run_id="run-1",
                )
            for process in list(_PROCESSES.values()):
                process.close()
            _PROCESSES.clear()
            time.sleep(0.05)

            self.assertEqual(first.returncode, 0)
            self.assertEqual(second.returncode, 0)
            self.assertEqual(
                first.command,
                ["codex", "app-server", "-c", 'windows.sandbox="elevated"', "--stdio"],
            )
            self.assertEqual(first.metadata["app_server_thread_id"], "thread-probe")
            self.assertEqual(second.metadata["app_server_thread_id"], "thread-probe")
            self.assertEqual(first.metadata["app_server_dispatch_provenance"]["turn_timeout_seconds"], 5)
            self.assertEqual(first.metadata["app_server_dispatch_provenance"]["timeout_source"], "global_default")
            self.assertEqual(first.metadata["app_server_dispatch_provenance"]["process_reuse_state"], "new_process")
            self.assertEqual(second.metadata["app_server_dispatch_provenance"]["turn_timeout_seconds"], 5)
            self.assertEqual(second.metadata["app_server_dispatch_provenance"]["process_reuse_state"], "reused_process")
            self.assertTrue(second.metadata["app_server_dispatch_provenance"]["reused_process_runtime_identity_matched"])
            self.assertEqual(first.metadata["app_server_turn_id"], "turn-1")
            self.assertEqual(second.metadata["app_server_turn_id"], "turn-2")
            self.assertEqual(second.metadata["task_transport"], "app_server")
            self.assertEqual(len(fake_processes), 1)
            self.assertEqual(fake_processes[0].prompts, ["short", long_prompt])
            lifecycle_requests = [
                method
                for method in fake_processes[0].request_order
                if method in {"initialize", "thread/start", "thread/resume", "turn/start"}
            ]
            self.assertEqual(
                lifecycle_requests,
                ["initialize", "thread/start", "turn/start", "thread/resume", "turn/start"],
            )
            self.assertEqual(fake_processes[0].turn_params[0]["threadId"], "thread-probe")
            initialize = fake_processes[0].initialize_params[0]
            self.assertTrue(initialize["capabilities"]["experimentalApi"])
            thread = fake_processes[0].thread_params[0]
            self.assertEqual(thread["cwd"], str(repository.resolve()))
            self.assertEqual(thread["runtimeWorkspaceRoots"], [str(repository.resolve())])
            self.assertEqual(first.metadata["app_server_thread_cwd"], str(repository.resolve()))
            self.assertEqual(first.metadata["app_server_runtime_workspace_roots"], [str(repository.resolve())])
            self.assertEqual(
                first.metadata["app_server_environment_roots"][0]["runtimeWorkspaceRoots"],
                [str(repository.resolve())],
            )
            self.assertEqual(thread["sandbox"], "workspace-write")
            self.assertEqual(thread["approvalPolicy"], "never")
            self.assertEqual(thread["model"], "gpt-5.6-luna")
            self.assertNotIn("tool_mode", thread)
            self.assertNotIn("use_responses_lite", thread)
            policy = fake_processes[0].turn_params[0]["sandboxPolicy"]
            self.assertEqual(policy["type"], "workspaceWrite")
            self.assertFalse(policy["networkAccess"])
            expected_roots = [repository, repository / ".git", expected_cache]
            self.assertEqual(len(policy["writableRoots"]), len(expected_roots))
            for actual, expected in zip(policy["writableRoots"], expected_roots):
                self.assertTrue(same_path(actual, expected), (actual, expected))
            self.assertEqual(fake_processes[0].turn_params[0]["effort"], "high")
            self.assertEqual(fake_processes[0].turn_params[0]["model"], "gpt-5.6-luna")
            self.assertNotIn("tool_mode", fake_processes[0].turn_params[0])
            self.assertNotIn("use_responses_lite", fake_processes[0].turn_params[0])
            self.assertTrue(fake_processes[0].thread_params[0].get("experimentalRawEvents"))
            self.assertTrue(same_path(fake_processes[0].turn_params[0]["cwd"], repository))
            self.assertNotIn("runtimeWorkspaceRoots", fake_processes[0].turn_params[0])
            journal_path = Path(first.metadata["live_event_journal"])
            deadline = time.monotonic() + 1
            journal_events = read_journal(journal_path)
            while len(journal_events) < 4 and time.monotonic() < deadline:
                time.sleep(0.01)
                journal_events = read_journal(journal_path)
            self.assertGreaterEqual(len(journal_events), 4)
            self.assertEqual(journal_events[0].method, "turn/started")
            self.assertIn("turn/completed", [event.method for event in journal_events])
            self.assertTrue(all(event.request_id == "request-1" for event in journal_events))
            self.assertTrue(all(event.run_id == "run-1" for event in journal_events))
            self.assertTrue(all(event.thread_id == "thread-probe" for event in journal_events))
            self.assertTrue(all(event.turn_id in {"turn-1", "turn-2"} for event in journal_events))

    def test_runtime_workspace_roots_are_canonical_and_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            self.assertEqual(_canonical_workspace_roots(root, root / "."), [str(root)])

    def test_fresh_thread_is_not_persisted_until_first_turn_materializes_rollout(self) -> None:
        from dual_codex.app_server import _AppServerProcess

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "repo"
            repository.mkdir()
            config = _config(root)
            agent = AgentConfig(
                codex_home=root / "profile",
                model="gpt-5.6-luna",
                reasoning_effort="max",
                sandbox="workspace-write",
                account_name="executor",
                backend="app_server",
            )
            fake = _FakeProcess()
            with patch("dual_codex.app_server.subprocess.Popen", return_value=fake):
                process = _AppServerProcess(config=config, agent=agent, repository=repository, progress=None)
                try:
                    thread_id, resumed = process.thread_id_for(repository)
                    self.assertEqual(thread_id, "thread-probe")
                    self.assertFalse(resumed)
                    self.assertFalse(_mapping_path(config, agent, repository).exists())
                    self.assertEqual(
                        [method for method in fake.request_order if method in {"initialize", "thread/start", "thread/resume", "turn/start"}],
                        ["initialize", "thread/start"],
                    )
                finally:
                    process.close()

    def test_fresh_root_binding_mismatch_stops_before_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "repo"
            repository.mkdir()
            agent = AgentConfig(
                codex_home=root / "profile",
                model="gpt-5.6-luna",
                reasoning_effort="max",
                sandbox="workspace-write",
                account_name="executor",
                backend="app_server",
            )
            fake = _FakeProcess()
            fake.response_roots_override = [str(root / "foreign")]
            with patch("dual_codex.app_server.subprocess.Popen", return_value=fake):
                result = run_codex_app_server(
                    config=_config(root),
                    agent=agent,
                    repository=repository,
                    prompt="probe",
                    output_path=root / "result.json",
                    session_id="root-mismatch",
                )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("different repository", result.stderr)
            self.assertEqual(fake.turn_params, [])

    def test_thread_start_failure_stops_before_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "repo"
            repository.mkdir()
            agent = AgentConfig(
                codex_home=root / "profile",
                model="gpt-5.6-luna",
                reasoning_effort="max",
                sandbox="workspace-write",
                account_name="executor",
                backend="app_server",
            )
            fake = _FakeProcess()
            fake.start_error = True
            with patch("dual_codex.app_server.subprocess.Popen", return_value=fake):
                result = run_codex_app_server(
                    config=_config(root),
                    agent=agent,
                    repository=repository,
                    prompt="probe",
                    output_path=root / "result.json",
                    session_id="start-failure",
                )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("thread/start failed", result.stderr)
            self.assertEqual(fake.turn_params, [])

    def test_existing_unmaterialized_thread_fails_closed_without_new_thread(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "repo"
            repository.mkdir()
            config = _config(root)
            agent = AgentConfig(
                codex_home=root / "profile",
                model="gpt-5.6-luna",
                reasoning_effort="max",
                sandbox="workspace-write",
                account_name="executor",
                backend="app_server",
            )
            _save_thread_mapping(config, agent, repository, "unmaterialized-thread", role="executor", windows_sandbox="unspecified")
            fake = _FakeProcess()
            fake.resume_error = True
            with patch("dual_codex.app_server.subprocess.Popen", return_value=fake):
                result = run_codex_app_server(
                    config=config,
                    agent=agent,
                    repository=repository,
                    prompt="probe",
                    output_path=root / "result.json",
                    session_id="unmaterialized-resume",
                )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("no rollout found", result.stderr)
            self.assertEqual(fake.turn_params, [])
            self.assertNotIn("thread/start", fake.request_order)
            self.assertFalse(_mapping_path(config, agent, repository, "executor").exists())

    def test_network_access_is_explicit_and_fail_closed(self) -> None:
        repository = Path("C:/repo")
        disabled = _workspace_write_sandbox_policy(repository)
        enabled = _workspace_write_sandbox_policy(repository, network_access=True)
        self.assertFalse(disabled["networkAccess"])
        self.assertTrue(enabled["networkAccess"])

    def test_security_recovery_command_is_separate_and_has_exact_mcp_allowlist(self) -> None:
        config = _config(Path("C:/dual-codex-test"))
        agent = AgentConfig(
            codex_home=Path("C:/dual-codex-test/profile"),
            model="",
            reasoning_effort="high",
            sandbox="workspace-write",
            account_name="codex-secundario",
            backend="app_server",
        )
        normal = _app_server_command(config, agent=agent, role="executor")
        maintenance = _app_server_command(
            config,
            agent=agent,
            role="executor",
            security_recovery=True,
        )
        self.assertNotIn("mcp_servers=", " ".join(normal))
        self.assertNotIn("plugins=", " ".join(normal))
        self.assertNotIn("features.shell_tool=false", normal)
        self.assertNotIn("features.hooks=false", normal)
        self.assertNotIn("features.multi_agent=false", normal)
        self.assertNotIn('web_search="disabled"', normal)
        self.assertNotIn("features.browser_use=false", normal)
        self.assertNotIn("features.computer_use=false", normal)
        self.assertNotIn("features.apps=false", normal)
        self.assertIn("features.shell_tool=false", maintenance)
        self.assertIn("features.hooks=false", maintenance)
        self.assertIn("features.multi_agent=false", maintenance)
        self.assertIn('web_search="disabled"', maintenance)
        self.assertIn("features.apps=false", maintenance)
        self.assertIn("features.browser_use=false", maintenance)
        self.assertIn("features.browser_use_external=false", maintenance)
        self.assertIn("features.browser_use_full_cdp_access=false", maintenance)
        self.assertIn("features.computer_use=false", maintenance)
        self.assertIn("features.image_generation=false", maintenance)
        self.assertIn("features.sleep_tool=false", maintenance)
        self.assertIn("mcp_servers={codex_apps={url=\"http://127.0.0.1:9\",enabled=false}}", maintenance)
        self.assertIn(
            'plugins={"codex-security@openai-curated-remote"={enabled=true,mcp_servers={"codex-security"={enabled=true,enabled_tools=["cancel_codex_security_scan"]}}}}',
            maintenance,
        )

    @unittest.skipUnless(os.name == "nt", "Windows Executor sandbox policy")
    def test_elevated_sandbox_override_is_limited_to_workspace_write_executor(self) -> None:
        root = Path("C:/dual-codex-test")
        config = _config(root)
        executor = AgentConfig(
            codex_home=root / "executor-profile",
            model="",
            reasoning_effort="high",
            sandbox="workspace-write",
            account_name="executor",
            backend="app_server",
        )
        architect_command = _app_server_command(config, agent=executor, role="architect")
        reviewer_command = _app_server_command(config, agent=executor, role="reviewer")
        executor_command = _app_server_command(config, agent=executor, role="executor")
        self.assertNotIn("windows.sandbox", " ".join(architect_command))
        self.assertNotIn("windows.sandbox", " ".join(reviewer_command))
        self.assertIn('windows.sandbox="elevated"', executor_command)
        read_only = AgentConfig(
            codex_home=root / "read-only-profile",
            model="",
            reasoning_effort="high",
            sandbox="read-only",
            account_name="executor",
            backend="app_server",
        )
        self.assertNotIn("windows.sandbox", " ".join(_app_server_command(config, agent=read_only, role="executor")))

    @unittest.skipUnless(os.name == "nt", "Windows Executor readiness gate")
    def test_executor_readiness_runs_before_turn_with_bounded_workspace_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "repo"
            repository.mkdir()
            (repository / ".git").mkdir()
            agent = AgentConfig(
                codex_home=root / "profile",
                model="",
                reasoning_effort="high",
                sandbox="workspace-write",
                account_name="codex-secundario",
                backend="app_server",
                network_access=False,
            )
            fake = _FakeProcess()
            fake.windows_sandbox = "elevated"
            with patch.dict("os.environ", {"LOCALAPPDATA": str(root / "localappdata")}):
                expected_cache = executor_npm_cache(agent)
                with patch("dual_codex.app_server.subprocess.Popen", return_value=fake):
                    result = run_codex_app_server(
                        config=_config(root),
                        agent=agent,
                        repository=repository,
                        prompt="probe",
                        output_path=root / "result.json",
                        session_id="executor-readiness",
                        role="executor",
                        require_workspace_ready=True,
                    )
            for process in list(_PROCESSES.values()):
                process.close()
            _PROCESSES.clear()
            time.sleep(0.05)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(fake.request_order.index("command/exec") < fake.request_order.index("turn/start"), True)
            self.assertEqual(len(fake.command_exec_params), 4)
            expected_roots = [repository.resolve(), (repository / ".git").resolve(), expected_cache.resolve()]
            for params in fake.command_exec_params:
                self.assertNotIn("outputBytesCap", params)
                self.assertEqual(params["timeoutMs"], 8000 if params["command"][0] != "node" or len(params["command"]) == 2 else 12000)
                policy = params["sandboxPolicy"]
                self.assertEqual(policy["type"], "workspaceWrite")
                self.assertFalse(policy["networkAccess"])
                self.assertEqual(len(policy["writableRoots"]), len(expected_roots))
                for actual, expected in zip(policy["writableRoots"], expected_roots):
                    self.assertTrue(same_path(actual, expected), (actual, expected))
            readiness = result.metadata["app_server_executor_readiness"]
            self.assertEqual(readiness["status"], "ready")
            self.assertTrue(readiness["child_process"])
            self.assertTrue(readiness["git"])
            self.assertTrue(readiness["node_child_process"])
            self.assertTrue(readiness["node_worker"])
            self.assertTrue(readiness["temp_write"])
            self.assertTrue(readiness["npm_cache_write"])
            self.assertFalse(readiness["network_enabled"])
            self.assertFalse(set(readiness) & {"token", "handoffClaimToken", "secret", "credential"})
            self.assertFalse(result.metadata["fallback_used"])
            self.assertEqual(result.metadata["actor_id"], "codex-secundario")
            self.assertIn("windows.sandbox=\"elevated\"", result.command)

    @unittest.skipUnless(os.name == "nt", "Windows Executor readiness gate")
    def test_executor_readiness_classifies_child_git_worker_and_network_failures(self) -> None:
        from dual_codex.app_server import _AppServerProcess

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "repo"
            repository.mkdir()
            (repository / ".git").mkdir()
            agent = AgentConfig(
                codex_home=root / "profile",
                model="",
                reasoning_effort="high",
                sandbox="workspace-write",
                account_name="executor",
                backend="app_server",
                network_access=True,
            )

            def build_process(responses: list[dict]):
                process = object.__new__(_AppServerProcess)
                process.agent = agent
                process.repository = repository
                process.role = "executor"
                process.windows_sandbox = "elevated"
                process.windows_sandbox_readiness = "ready"
                process.executor_readiness = {"status": "not_checked"}
                process.config = SimpleNamespace(app_server_initialize_timeout=2)
                process.request = Mock(side_effect=responses)
                return process

            success = {"result": {"exitCode": 0, "stdout": "ready", "stderr": ""}}
            worker_success = {"result": {"exitCode": 0, "stdout": json.dumps({"child": True, "worker": True, "temp_write": True, "cache_write": True}), "stderr": ""}}
            cases = [
                ([{"error": {"message": "blocked"}}], "EXECUTOR_SUBPROCESS_UNAVAILABLE"),
                ([success, {"result": {"exitCode": 1, "stdout": "", "stderr": "git"}}], "EXECUTOR_GIT_UNAVAILABLE"),
                ([success, success, success, {"result": {"exitCode": 0, "stdout": json.dumps({"child": True, "worker": False, "temp_write": True, "cache_write": True}), "stderr": ""}}], "EXECUTOR_NODE_WORKER_UNAVAILABLE"),
                ([success, success, success, worker_success, {"result": {"exitCode": 2, "stdout": json.dumps({"registry": False, "prisma_host": True}), "stderr": ""}}], "EXECUTOR_NETWORK_UNAVAILABLE"),
            ]
            with patch.dict("os.environ", {"LOCALAPPDATA": str(root / "localappdata")}):
                for responses, expected in cases:
                    with self.subTest(failure=expected):
                        process = build_process(responses)
                        with self.assertRaises(AppServerError) as raised:
                            process.check_executor_capabilities()
                        self.assertEqual(raised.exception.failure_class, expected)

    def test_headless_app_server_does_not_override_profile_windows_sandbox(self) -> None:
        command = _app_server_command(_config(Path("C:/dual-codex-test")))
        self.assertEqual(
            command,
            ["codex", "app-server", "--stdio"],
        )
        self.assertNotIn("--disable", command)
        self.assertNotIn("code_mode_host", command)
        self.assertNotIn("--code-mode-host", command)
        self.assertNotIn("-c", command)
        self.assertNotIn("windows.sandbox", command)

    def test_default_codex_environment_keeps_desktop_bridge_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            agent = AgentConfig(
                codex_home=Path(temp) / "profile",
                model="",
                reasoning_effort="high",
                sandbox="workspace-write",
                account_name="interactive",
            )
            with patch.dict(
                "os.environ",
                {"CODEX_INTERNAL_ORIGINATOR_OVERRIDE": "desktop"},
                clear=False,
            ):
                environment = codex_environment(agent)
            self.assertEqual(environment["CODEX_INTERNAL_ORIGINATOR_OVERRIDE"], "desktop")

    def test_explicit_elevated_profile_is_preserved_and_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "repo"
            repository.mkdir()
            (repository / ".git").mkdir()
            agent = AgentConfig(
                codex_home=root / "profile",
                model="",
                reasoning_effort="high",
                sandbox="workspace-write",
                account_name="biel4",
                backend="app_server",
            )
            fake = _FakeProcess()
            fake.windows_sandbox = "elevated"
            with patch("dual_codex.app_server.subprocess.Popen", return_value=fake):
                result = run_codex_app_server(
                    config=_config(root),
                    agent=agent,
                    repository=repository,
                    prompt="probe",
                    output_path=root / "result.json",
                    session_id="elevated-session",
                )
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.metadata["app_server_windows_sandbox"], "elevated")
            self.assertEqual(result.metadata["app_server_windows_sandbox_readiness"], "ready")
            self.assertEqual(result.metadata["app_server_role"], "executor")
            self.assertEqual(result.metadata["app_server_approval_policy"], "never")
            self.assertEqual(result.metadata["app_server_sandbox_policy"], "workspace-write")
            self.assertNotIn("danger-full-access", result.command)
            for process in list(_PROCESSES.values()):
                process.close()
            _PROCESSES.clear()

    def test_missing_windows_sandbox_setting_is_explicitly_unmodified(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "repo"
            repository.mkdir()
            agent = AgentConfig(
                codex_home=root / "profile",
                model="",
                reasoning_effort="high",
                sandbox="workspace-write",
                account_name="biel4",
                backend="app_server",
            )
            fake = _FakeProcess()
            with patch("dual_codex.app_server.subprocess.Popen", return_value=fake):
                result = run_codex_app_server(
                    config=_config(root),
                    agent=agent,
                    repository=repository,
                    prompt="probe",
                    output_path=root / "result.json",
                    session_id="default-session",
                )
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.metadata["app_server_windows_sandbox"], "unspecified")
            self.assertEqual(result.metadata["app_server_windows_sandbox_readiness"], "not_checked")
            for process in list(_PROCESSES.values()):
                process.close()
            _PROCESSES.clear()

    def test_unprovisioned_elevated_sandbox_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "repo"
            repository.mkdir()
            agent = AgentConfig(
                codex_home=root / "profile",
                model="",
                reasoning_effort="high",
                sandbox="workspace-write",
                account_name="biel4",
                backend="app_server",
            )
            fake = _FakeProcess()
            fake.windows_sandbox = "elevated"
            fake.windows_sandbox_readiness = "notConfigured"
            with patch("dual_codex.app_server.subprocess.Popen", return_value=fake):
                result = run_codex_app_server(
                    config=_config(root),
                    agent=agent,
                    repository=repository,
                    prompt="probe",
                    output_path=root / "result.json",
                    session_id="blocked-session",
                )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("not provisioned", result.stderr)
            self.assertIn("codex sandbox setup --elevated --current-user", result.stderr)
            self.assertNotIn("unelevated", result.stderr)
            self.assertNotIn("danger-full-access", result.stderr)

    def test_mapping_is_invalidated_when_windows_sandbox_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "repo"
            repository.mkdir()
            config = _config(root)
            agent = AgentConfig(
                codex_home=root / "profile",
                model="",
                reasoning_effort="high",
                sandbox="workspace-write",
                account_name="biel4",
                backend="app_server",
            )
            _save_thread_mapping(config, agent, repository, "old-thread", windows_sandbox="elevated")
            self.assertIsNone(
                _load_thread_mapping(config, agent, repository, windows_sandbox="unelevated")
            )
            self.assertFalse(_mapping_path(config, agent, repository).exists())

    def test_thread_mapping_identity_tracks_effective_runtime_configuration(self) -> None:
        from dataclasses import replace

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "repo"
            repository.mkdir()
            config = _config(root)
            agent = AgentConfig(
                codex_home=root / "profile",
                model="gpt-test",
                reasoning_effort="high",
                sandbox="workspace-write",
                account_name="executor",
                backend="app_server",
                network_access=False,
            )
            _save_thread_mapping(config, agent, repository, "thread-1", role="executor")
            self.assertIsNone(
                _load_thread_mapping(
                    config,
                    replace(agent, app_server_turn_timeout=3600),
                    repository,
                    role="executor",
                )
            )
            _save_thread_mapping(config, agent, repository, "thread-1", role="executor")
            self.assertIsNone(
                _load_thread_mapping(config, replace(agent, network_access=True), repository, role="executor")
            )
            _save_thread_mapping(config, agent, repository, "thread-2", role="executor")
            self.assertIsNone(
                _load_thread_mapping(config, replace(agent, sandbox="read-only"), repository, role="executor")
            )
            _save_thread_mapping(config, agent, repository, "thread-3", role="executor")
            agent.codex_home.mkdir(parents=True)
            (agent.codex_home / "config.toml").write_text("[features]\nnew = true\n", encoding="utf-8")
            self.assertIsNone(_load_thread_mapping(config, agent, repository, role="executor"))
            _save_thread_mapping(
                config, agent, repository, "thread-4", role="executor", require_workspace_ready=True
            )
            self.assertIsNone(
                _load_thread_mapping(
                    config, agent, repository, role="executor", require_workspace_ready=False
                )
            )

    def test_process_key_is_scoped_to_repository(self) -> None:
        config = _config(Path("C:/dual-codex-test"))
        agent = AgentConfig(
            codex_home=Path("C:/profile"),
            model="",
            reasoning_effort="high",
            sandbox="workspace-write",
            account_name="executor",
            backend="app_server",
        )
        self.assertNotEqual(
            _process_key(agent, config, Path("C:/repo-a")),
            _process_key(agent, config, Path("C:/repo-b")),
        )

    def test_persistent_process_reuse_requires_exact_runtime_identity(self) -> None:
        from dataclasses import replace

        from dual_codex.app_server import _runtime_config_identity, _thread_mapping_identity

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repo"
            repository.mkdir()
            config = _config(root)
            agent = AgentConfig(
                codex_home=root / "profile",
                model="gpt-test",
                reasoning_effort="high",
                sandbox="workspace-write",
                account_name="executor",
                backend="app_server",
                network_access=True,
                service_tier="priority",
                app_server_turn_timeout=600,
            )
            created = []

            class FakePersistentProcess:
                def __init__(self, **kwargs):
                    self.agent = kwargs["agent"]
                    self.config = kwargs["config"]
                    self.repository = repository
                    self.role = kwargs["role"]
                    self.require_workspace_ready = kwargs["require_workspace_ready"]
                    self.runtime_config_identity = _runtime_config_identity(
                        self.agent, self.config, repository, self.role, self.require_workspace_ready
                    )
                    self.thread_mapping_identity = _thread_mapping_identity(
                        self.agent, self.config, repository, self.role, self.require_workspace_ready
                    )
                    self.process = SimpleNamespace(poll=lambda: None)
                    self.progress = kwargs["progress"]
                    self.pid = len(created) + 5000
                    self.close = Mock()
                    created.append(self)

            with patch("dual_codex.app_server._AppServerProcess", FakePersistentProcess):
                dispatch = {}
                first = _get_process(config, agent, repository, None, role="executor", dispatch_provenance=dispatch)
                self.assertEqual(dispatch["turn_timeout_seconds"], 600)
                self.assertEqual(dispatch["timeout_source"], "account_role_override")
                self.assertEqual(dispatch["process_reuse_state"], "new_process")
                self.assertIsNone(dispatch["reused_process_runtime_identity_matched"])

                same_identity = {}
                reused = _get_process(config, replace(agent, label="metadata-only"), repository, None, role="executor", dispatch_provenance=same_identity)
                self.assertIs(reused, first)
                self.assertEqual(same_identity["process_reuse_state"], "reused_process")
                self.assertTrue(same_identity["reused_process_runtime_identity_matched"])
                self.assertEqual(same_identity["runtime_config_identity"], first.runtime_config_identity)
                self.assertEqual(len(created), 1)

                changed_values = [
                    replace(agent, app_server_turn_timeout=3600),
                    replace(agent, model="gpt-test-next"),
                    replace(agent, reasoning_effort="medium"),
                    replace(agent, service_tier="flex"),
                    replace(agent, network_access=False),
                    replace(agent, sandbox="read-only"),
                ]
                current = first
                for changed in changed_values:
                    next_process = _get_process(config, changed, repository, None, role="executor")
                    self.assertIsNot(next_process, current)
                    current.close.assert_called_once_with()
                    self.assertNotEqual(
                        next_process.runtime_config_identity,
                        current.runtime_config_identity,
                    )
                    current = next_process

                global_timeout_change = _get_process(
                    replace(config, app_server_turn_timeout=9), current.agent, repository, None, role="executor"
                )
                self.assertIsNot(global_timeout_change, current)
                self.assertNotEqual(global_timeout_change.runtime_config_identity, current.runtime_config_identity)

                secret_agent = replace(global_timeout_change.agent, label="secret-value", auth_reference="secret-value")
                self.assertEqual(
                    _runtime_config_identity(secret_agent, config, repository, "executor", False),
                    _runtime_config_identity(global_timeout_change.agent, config, repository, "executor", False),
                )
                serialized = json.dumps(
                    {
                        "identity": global_timeout_change.runtime_config_identity,
                        "dispatch": same_identity,
                    }
                )
                self.assertNotIn("secret-value", serialized)

    def test_dead_matching_process_preserves_compatible_thread_mapping(self) -> None:
        from dual_codex.app_server import _load_thread_mapping

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repo"
            repository.mkdir()
            config = _config(root)
            agent = AgentConfig(
                codex_home=root / "profile",
                model="gpt-test",
                reasoning_effort="high",
                sandbox="workspace-write",
                account_name="executor",
                backend="app_server",
                app_server_turn_timeout=600,
            )
            created = []

            class FakePersistentProcess:
                def __init__(self, **kwargs):
                    from dual_codex.app_server import _runtime_config_identity, _thread_mapping_identity

                    self.agent = kwargs["agent"]
                    self.config = kwargs["config"]
                    self.repository = repository
                    self.role = kwargs["role"]
                    self.require_workspace_ready = kwargs["require_workspace_ready"]
                    self.runtime_config_identity = _runtime_config_identity(
                        self.agent, self.config, repository, self.role, self.require_workspace_ready
                    )
                    self.thread_mapping_identity = _thread_mapping_identity(
                        self.agent, self.config, repository, self.role, self.require_workspace_ready
                    )
                    self.process = SimpleNamespace(poll=lambda: None)
                    self.close = Mock()
                    created.append(self)

            with patch("dual_codex.app_server._AppServerProcess", FakePersistentProcess):
                first = _get_process(config, agent, repository, None, role="executor")
                _save_thread_mapping(config, agent, repository, "valid-session-thread", role="executor")
                first.process.poll = lambda: 1
                replacement = _get_process(config, agent, repository, None, role="executor")

            self.assertEqual(len(created), 2)
            self.assertIsNot(replacement, first)
            first.close.assert_called_once_with()
            self.assertEqual(
                _load_thread_mapping(config, agent, repository, role="executor"),
                "valid-session-thread",
            )

    def test_process_replacement_does_not_hold_global_lock_while_closing_other_turn(self) -> None:
        from concurrent.futures import ThreadPoolExecutor
        from dataclasses import replace

        from dual_codex.app_server import _runtime_config_identity, _thread_mapping_identity

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repo"
            unrelated_repository = root / "other-repo"
            repository.mkdir()
            unrelated_repository.mkdir()
            config = _config(root)
            agent = AgentConfig(
                codex_home=root / "profile",
                model="gpt-test",
                reasoning_effort="high",
                sandbox="workspace-write",
                account_name="executor",
                backend="app_server",
                network_access=True,
                app_server_turn_timeout=600,
            )

            class FakePersistentProcess:
                def __init__(self, **kwargs):
                    self.agent = kwargs["agent"]
                    self.config = kwargs["config"]
                    self.repository = kwargs["repository"]
                    self.role = kwargs["role"]
                    self.require_workspace_ready = kwargs["require_workspace_ready"]
                    self.runtime_config_identity = _runtime_config_identity(
                        self.agent, self.config, self.repository, self.role, self.require_workspace_ready
                    )
                    self.thread_mapping_identity = _thread_mapping_identity(
                        self.agent, self.config, self.repository, self.role, self.require_workspace_ready
                    )
                    self.process = SimpleNamespace(poll=lambda: None)
                    self.progress = kwargs["progress"]
                    self.pid = 6000
                    self.close = Mock()

            close_entered = threading.Event()
            release_close = threading.Event()

            with patch("dual_codex.app_server._AppServerProcess", FakePersistentProcess):
                old = _get_process(config, agent, repository, None, role="executor")

                def slow_close():
                    close_entered.set()
                    release_close.wait(timeout=5)

                old.close = slow_close
                with ThreadPoolExecutor(max_workers=2) as pool:
                    replacement = pool.submit(
                        _get_process,
                        config,
                        replace(agent, app_server_turn_timeout=3600),
                        repository,
                        None,
                        "executor",
                    )
                    self.assertTrue(close_entered.wait(timeout=1))
                    unrelated = pool.submit(
                        _get_process,
                        config,
                        agent,
                        unrelated_repository,
                        None,
                        "executor",
                    )
                    try:
                        unrelated_process = unrelated.result(timeout=1)
                    finally:
                        release_close.set()
                    replacement_process = replacement.result(timeout=2)

                self.assertIsNot(unrelated_process, old)
                self.assertIsNot(replacement_process, old)
                self.assertEqual(replacement_process.agent.app_server_turn_timeout, 3600)

    def test_timeout_change_recreates_live_process_and_dispatch_provenance_uses_3600(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repo"
            repository.mkdir()
            agent = AgentConfig(
                codex_home=root / "profile",
                model="gpt-test",
                reasoning_effort="high",
                sandbox="read-only",
                account_name="executor",
                backend="app_server",
                app_server_turn_timeout=600,
            )
            config = _config(root)
            processes = []

            def create_process(*args, **kwargs):
                process = _FakeProcess(*args, **kwargs)
                processes.append(process)
                return process

            with patch("dual_codex.app_server.subprocess.Popen", side_effect=create_process):
                first_dispatch = []
                first = run_codex_app_server(
                    config=config,
                    agent=agent,
                    repository=repository,
                    prompt="first",
                    output_path=root / "first.json",
                    session_id="session",
                    dispatch_started=first_dispatch.append,
                )
                second_dispatch = []
                second = run_codex_app_server(
                    config=config,
                    agent=AgentConfig(
                        **{**agent.__dict__, "app_server_turn_timeout": 3600}
                    ),
                    repository=repository,
                    prompt="second",
                    output_path=root / "second.json",
                    session_id="session",
                    dispatch_started=second_dispatch.append,
                )

            for process in list(_PROCESSES.values()):
                process.close()
            _PROCESSES.clear()
            time.sleep(0.05)

            self.assertEqual(first.returncode, 0)
            self.assertEqual(second.returncode, 0)
            self.assertEqual(len(processes), 2)
            self.assertEqual(first_dispatch[0]["turn_timeout_seconds"], 600)
            self.assertEqual(second_dispatch[0]["turn_timeout_seconds"], 3600)
            self.assertEqual(second_dispatch[0]["timeout_source"], "account_role_override")
            self.assertEqual(second_dispatch[0]["process_reuse_state"], "new_process")
            self.assertIsNone(second_dispatch[0]["reused_process_runtime_identity_matched"])
            self.assertEqual(second.metadata["app_server_turn_provenance"]["turn_timeout_seconds"], 3600)
            self.assertEqual(second.metadata["app_server_turn_provenance"]["runtime_config_identity"], second_dispatch[0]["runtime_config_identity"])
            self.assertEqual(second.metadata["app_server_turn_provenance"]["process_reuse_state"], "new_process")

    def test_thread_mapping_is_scoped_to_profile_repository_and_role(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "repo"
            repository.mkdir()
            config = _config(root)
            agent = AgentConfig(
                codex_home=root / "profile",
                model="",
                reasoning_effort="high",
                sandbox="read-only",
                account_name="secondary",
                backend="app_server",
            )
            _save_thread_mapping(config, agent, repository, "executor-thread", role="executor")
            self.assertEqual(
                _load_thread_mapping(config, agent, repository, role="executor"),
                "executor-thread",
            )
            self.assertIsNone(_load_thread_mapping(config, agent, repository, role="architect"))
            self.assertNotEqual(
                _mapping_path(config, agent, repository, "executor"),
                _mapping_path(config, agent, repository, "architect"),
            )

    def test_network_enabled_executor_turn_receives_scoped_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "repo"
            repository.mkdir()
            (repository / ".git").mkdir()
            agent = AgentConfig(
                codex_home=root / "profile",
                model="",
                reasoning_effort="high",
                sandbox="workspace-write",
                account_name="biel4",
                backend="app_server",
                network_access=True,
            )
            fake = _FakeProcess()
            with patch.dict("os.environ", {"LOCALAPPDATA": str(root / "localappdata")}):
                expected_cache = executor_npm_cache(agent)
                with patch("dual_codex.app_server.subprocess.Popen", return_value=fake):
                    result = run_codex_app_server(
                        config=_config(root),
                        agent=agent,
                        repository=repository,
                        prompt="network probe",
                        output_path=root / "result.json",
                        session_id="network-session",
                    )
            self.assertEqual(result.returncode, 0)
            policy = fake.turn_params[0]["sandboxPolicy"]
            self.assertTrue(policy["networkAccess"])
            expected_roots = [repository, repository / ".git", expected_cache]
            self.assertEqual(len(policy["writableRoots"]), len(expected_roots))
            for actual, expected in zip(policy["writableRoots"], expected_roots):
                self.assertTrue(same_path(actual, expected), (actual, expected))
            for process in list(_PROCESSES.values()):
                process.close()
            _PROCESSES.clear()
            time.sleep(0.1)

    def test_server_requests_are_denied_without_escalation(self) -> None:
        # The real probe used approvalPolicy=never and emitted no requests. This
        # unit seam is covered by the implementation's explicit decline branch.
        from dual_codex.app_server import _AppServerProcess

        process = object.__new__(_AppServerProcess)
        sent: list[dict] = []
        process._send = sent.append
        process._respond_to_server_request({"id": 7, "method": "item/commandExecution/requestApproval"})
        self.assertEqual(sent[0]["result"], {"decision": "decline"})

    def test_dynamic_tool_request_returns_structured_custom_output(self) -> None:
        from dual_codex.app_server import _AppServerProcess

        process = object.__new__(_AppServerProcess)
        sent: list[dict] = []
        process._send = sent.append
        process._respond_to_server_request(
            {
                "id": 8,
                "method": "item/tool/call",
                "params": {"tool": "probe", "callId": "call-1"},
            }
        )
        self.assertEqual(sent[0]["result"]["success"], False)
        self.assertEqual(sent[0]["result"]["contentItems"][0]["type"], "inputText")
        self.assertIn("headless Dual Codex App Server", sent[0]["result"]["contentItems"][0]["text"])

    def test_raw_custom_tool_output_is_bounded_to_reconciliation_fields(self) -> None:
        from dual_codex.app_server import _raw_response_item_evidence

        evidence = _raw_response_item_evidence(
            {
                "type": "custom_tool_call_output",
                "id": "ctco-1",
                "call_id": "call-1",
                "name": "exec",
                "input": "do not retain this input",
                "encrypted_content": "do not retain reasoning",
                "output": [
                    {"type": "input_text", "text": "Exit code: 0\\nOutput: ok"},
                ],
            }
        )
        self.assertEqual(evidence["type"], "custom_tool_call_output")
        self.assertEqual(evidence["call_id"], "call-1")
        self.assertEqual(evidence["output"][0]["text"], r"Exit code: 0\nOutput: ok")
        self.assertTrue(evidence["success"])
        self.assertNotIn("input", evidence)
        self.assertNotIn("encrypted_content", evidence)

    def test_event_journal_failure_cannot_change_notification_handling(self) -> None:
        from collections import deque
        from dual_codex.app_server import _AppServerProcess

        process = object.__new__(_AppServerProcess)
        process._events = deque()
        process._event_journal = Mock()
        process._event_journal.append_notification.side_effect = RuntimeError("journal unavailable")
        process._event_context = {}
        process._record_notification({"jsonrpc": "2.0", "method": "future/notice", "params": {}})
        self.assertEqual(process.events[0]["method"], "future/notice")

    def test_event_journal_contention_cannot_block_notification_handling(self) -> None:
        from collections import deque
        from dual_codex.app_server import _AppServerProcess

        process = object.__new__(_AppServerProcess)
        process._events = deque()
        process._event_context = {}
        process._event_publications = queue.Queue(maxsize=1)
        process._event_journal = Mock()
        blocked = threading.Event()
        release = threading.Event()

        def append_notification(*args, **kwargs):
            blocked.set()
            release.wait(2)

        process._event_journal.append_notification.side_effect = append_notification
        publisher = threading.Thread(target=process._publish_events, daemon=True)
        publisher.start()
        first = queued = dropped = None

        try:
            first_done = threading.Event()
            first = threading.Thread(
                target=lambda: (process._record_notification({"method": "blocked/notice"}), first_done.set()),
                daemon=True,
            )
            first.start()
            self.assertTrue(blocked.wait(1))
            self.assertTrue(first_done.wait(0.5))

            queued_done = threading.Event()
            queued = threading.Thread(
                target=lambda: (process._record_notification({"method": "queued/notice"}), queued_done.set()),
                daemon=True,
            )
            queued.start()
            self.assertTrue(queued_done.wait(0.5))

            dropped_done = threading.Event()
            dropped = threading.Thread(
                target=lambda: (process._record_notification({"method": "dropped/notice"}), dropped_done.set()),
                daemon=True,
            )
            dropped.start()
            self.assertTrue(dropped_done.wait(0.5))
            self.assertEqual([event["method"] for event in process.events], [
                "blocked/notice", "queued/notice", "dropped/notice",
            ])
        finally:
            release.set()
            if first is not None:
                first.join(1)
            if queued is not None:
                queued.join(1)
            if dropped is not None:
                dropped.join(1)
            deadline = time.monotonic() + 1
            while True:
                try:
                    process._event_publications.put_nowait(None)
                    break
                except queue.Full:
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(0.01)
            publisher.join(1)

    def test_dashboard_rpc_clears_executor_event_context(self) -> None:
        root = Path(".").resolve()
        config = _config(root)
        agent = AgentConfig(
            codex_home=root / "profile",
            model="",
            reasoning_effort="high",
            sandbox="workspace-write",
            account_name="executor",
            backend="app_server",
        )
        process = Mock()
        process.request.return_value = {"id": 1, "result": {"ok": True}}
        with patch("dual_codex.app_server._get_process", return_value=process):
            self.assertEqual(
                app_server_call(
                    config=config,
                    agent=agent,
                    repository=root,
                    method="account/read",
                ),
                {"ok": True},
            )
        process.set_event_context.assert_called_once_with(None)

    def test_dashboard_request_restores_executor_context_after_suppression(self) -> None:
        from dual_codex.app_server import _AppServerProcess

        process = object.__new__(_AppServerProcess)
        process._lock = threading.RLock()
        process._event_journal = "executor-journal"
        process._event_context = {"run_id": "run-1"}
        process.request = Mock(return_value={"id": 1, "result": {}})
        self.assertEqual(
            process.request_without_event_journal("account/read", {}, timeout=1),
            {"id": 1, "result": {}},
        )
        self.assertEqual(process._event_journal, "executor-journal")
        self.assertEqual(process._event_context, {"run_id": "run-1"})

    def test_request_tolerates_an_intermediate_queue_timeout(self) -> None:
        from dual_codex.app_server import _AppServerProcess

        process = object.__new__(_AppServerProcess)
        process._lock = threading.RLock()
        process._next_id = 0
        process._send = Mock()
        process._next_message = Mock(
            side_effect=[
                AppServerError("Timed out waiting for App Server JSON-RPC data."),
                {"jsonrpc": "2.0", "id": 1, "result": {}},
            ]
        )
        response = process.request("thread/start", {}, timeout=1)
        self.assertEqual(response["id"], 1)
        self.assertEqual(process._next_message.call_count, 2)

    def test_process_exit_preserves_sanitized_stderr_diagnostic(self) -> None:
        from collections import deque
        from dual_codex.app_server import _AppServerProcess

        process = object.__new__(_AppServerProcess)
        process._messages = queue.Queue()
        process._messages.put(None)
        process._stderr = deque(['state=auth.json token="secret-value"'])
        process.process = SimpleNamespace(poll=lambda: 17)

        with self.assertRaisesRegex(AppServerError, "process exited unexpectedly") as raised:
            process._next_message(0.1)

        self.assertIn("[REDACTED_AUTH_PATH]", str(raised.exception))
        self.assertNotIn("secret-value", str(raised.exception))
        self.assertEqual(raised.exception.termination_classification, "APP_SERVER_PROCESS_EXIT")

    def test_stdout_eof_while_app_server_is_alive_is_transport_failure(self) -> None:
        from collections import deque
        from dual_codex.app_server import _AppServerProcess

        process = object.__new__(_AppServerProcess)
        process._messages = queue.Queue()
        process._messages.put(None)
        process._stderr = deque()
        process.process = SimpleNamespace(poll=lambda: None)

        with self.assertRaises(AppServerError) as raised:
            process._next_message(0.1)

        self.assertEqual(raised.exception.termination_classification, "APP_SERVER_TRANSPORT_EOF")

    def test_report_normalisation_keeps_existing_delegation_shape(self) -> None:
        value = json.loads(
            _normalise_report(
                json.dumps(
                    {
                        "summary": "Executor completed.",
                        "request_id": "x",
                        "status": "completed",
                        "files_changed": ["src/tiny_math/core.py"],
                        "tests": {"command": "python -m unittest", "result": "passed", "exit_code": 0, "tests_run": 4},
                        "remaining_issues": [],
                        "commit_created": False,
                    }
                )
            )
        )
        self.assertEqual(set(value), {"summary", "files_changed", "commands_run", "tests", "remaining_issues"})
        self.assertEqual(value["tests"][0]["status"], "passed")

    def test_report_normalisation_defaults_only_missing_command_telemetry(self) -> None:
        payload = {
            "summary": "Read-only probe completed.",
            "files_changed": [],
            "tests": [],
            "remaining_issues": [],
        }
        app_server = json.loads(_normalise_report(json.dumps(payload)))
        native_tui = _report_from_message(json.dumps(payload))
        self.assertEqual(app_server["commands_run"], [])
        self.assertEqual(native_tui, app_server)

    def test_report_normalisation_does_not_repair_invalid_or_missing_semantics(self) -> None:
        invalid_commands = {
            "summary": "done",
            "files_changed": [],
            "commands_run": "none",
            "tests": [],
            "remaining_issues": [],
        }
        missing_summary = {
            "files_changed": [],
            "commands_run": [],
            "tests": [],
            "remaining_issues": [],
        }
        self.assertEqual(json.loads(_normalise_report(json.dumps(invalid_commands))), invalid_commands)
        self.assertEqual(json.loads(_normalise_report(json.dumps(missing_summary))), missing_summary)

    def test_app_server_stderr_is_sanitized_before_result_return(self) -> None:
        raw = 'auth=C:/Users/USER/.codex/auth.json token="secret-value"'
        sanitized = _sanitize_stderr(raw)
        self.assertNotIn("auth.json", sanitized)
        self.assertNotIn("secret-value", sanitized)
        self.assertIn("[REDACTED_AUTH_PATH]", sanitized)
        self.assertIn("[REDACTED]", sanitized)

    def test_failed_turn_discards_persistent_process_without_replay(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "repo"
            repository.mkdir()
            config = _config(root)
            agent = AgentConfig(
                codex_home=root / "profile",
                model="",
                reasoning_effort="high",
                sandbox="workspace-write",
                account_name="executor",
                backend="app_server",
            )
            process = object.__new__(type("Process", (), {}))
            process.agent = agent
            process.config = config
            process.repository = repository
            process.thread_id_for = Mock(side_effect=AppServerError("turn timed out"))
            process.close = Mock()
            _save_thread_mapping(config, agent, repository, "stale-thread")
            key = _process_key(agent, config, repository)
            _PROCESSES[key] = process
            with patch("dual_codex.app_server._get_process", return_value=process):
                result = run_codex_app_server(
                    config=config,
                    agent=agent,
                    repository=repository,
                    prompt="unsafe to replay",
                    output_path=root / "result.json",
                    session_id="session",
                )
            self.assertEqual(result.returncode, 1)
            self.assertNotIn(key, _PROCESSES)
            self.assertFalse(_mapping_path(config, agent, repository).exists())
            process.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
