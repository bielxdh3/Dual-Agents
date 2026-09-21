from __future__ import annotations

import json
from pathlib import Path
import queue
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from dual_codex.app_server import (
    AppServerError,
    _PROCESSES,
    _canonical_workspace_roots,
    _app_server_command,
    _load_thread_mapping,
    _mapping_path,
    _normalise_report,
    _process_key,
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

            self.assertEqual(first.returncode, 0)
            self.assertEqual(second.returncode, 0)
            self.assertEqual(
                first.command,
                ["codex", "app-server", "--stdio"],
            )
            self.assertEqual(first.metadata["app_server_thread_id"], "thread-probe")
            self.assertEqual(second.metadata["app_server_thread_id"], "thread-probe")
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
            _save_thread_mapping(config, agent, repository, "unmaterialized-thread", windows_sandbox="unspecified")
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
            self.assertFalse(_mapping_path(config, agent, repository).exists())

    def test_network_access_is_explicit_and_fail_closed(self) -> None:
        repository = Path("C:/repo")
        disabled = _workspace_write_sandbox_policy(repository)
        enabled = _workspace_write_sandbox_policy(repository, network_access=True)
        self.assertFalse(disabled["networkAccess"])
        self.assertTrue(enabled["networkAccess"])

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

        with self.assertRaisesRegex(AppServerError, "process exited unexpectedly") as raised:
            process._next_message(0.1)

        self.assertIn("[REDACTED_AUTH_PATH]", str(raised.exception))
        self.assertNotIn("secret-value", str(raised.exception))

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
