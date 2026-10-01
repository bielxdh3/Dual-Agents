from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import shutil
import signal
import subprocess
import threading
import time
from typing import Any, Callable, Iterable

from .config import AgentConfig


class CommandError(RuntimeError):
    def __init__(self, message: str, *, metadata: dict[str, Any] | None = None):
        super().__init__(message)
        self.metadata = dict(metadata or {})


DEFAULT_HOST_COMMAND_TIMEOUT = 30.0


@dataclass(frozen=True)
class CommandResult:
    command: list[str]
    returncode: int
    stdout: str
    stderr: str
    metadata: dict[str, Any] = field(default_factory=dict)


_CMD_CONTROL_CHARACTERS = frozenset("&|<>^()%!\r\n")


def _prepare_command(args: list[str]) -> list[str] | str:
    """Run npm .cmd/.bat shims through a constrained command processor."""
    if os.name != "nt" or not args:
        return args

    resolved = shutil.which(args[0])
    if resolved:
        args = [resolved, *args[1:]]

    if Path(args[0]).suffix.lower() in {".cmd", ".bat"}:
        if not resolved:
            raise ValueError("Windows command shim must resolve to an existing .cmd or .bat file")
        unsafe = next((arg for arg in args if any(char in arg for char in _CMD_CONTROL_CHARACTERS)), None)
        if unsafe is not None:
            raise ValueError("Windows command shim arguments cannot contain cmd.exe control characters")
        command_processor = os.environ.get(
            "COMSPEC",
            str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "cmd.exe"),
        )
        # .cmd/.bat files require cmd.exe; keep shell=False and expose only a
        # validated argv string to the minimum compatibility shim.
        command_line = subprocess.list2cmdline(args)
        return f'{subprocess.list2cmdline([command_processor])} /d /s /v:off /c "{command_line}"'

    return args


def run_command(
    command: Iterable[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    stdin: str | None = None,
    check: bool = True,
    timeout: float | None = DEFAULT_HOST_COMMAND_TIMEOUT,
    progress: Callable[[str], None] | None = None,
    progress_interval: float = 15.0,
) -> CommandResult:
    display_args = [str(part) for part in command]
    process_args = _prepare_command(display_args.copy())

    timed_out = False
    try:
        completed, timed_out = _run_with_progress(
            process_args,
            cwd=cwd,
            env=env,
            stdin=stdin,
            progress=progress,
            progress_interval=progress_interval,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        timed_out = True
        completed = subprocess.CompletedProcess(process_args, 124, "", "Command timed out.")
    metadata = {}
    if timed_out:
        timeout_notice = "Command timed out."
        stderr = completed.stderr or ""
        if timeout_notice.casefold() not in stderr.casefold():
            stderr = f"{stderr.rstrip()}\n{timeout_notice}".lstrip()
        metadata.update({
            "failure_class": "HOST_COMMAND_TIMEOUT",
            "availability_failure_class": "timeout",
        })
        completed = subprocess.CompletedProcess(
            completed.args,
            completed.returncode,
            completed.stdout,
            stderr,
        )
    result = CommandResult(
        display_args,
        completed.returncode,
        completed.stdout,
        completed.stderr,
        metadata,
    )
    if check and completed.returncode != 0:
        raise CommandError(
            f"Command failed ({completed.returncode}): {' '.join(display_args)}\n"
            f"STDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}",
            metadata=metadata,
        )
    return result


def _run_with_progress(
    process_args: list[str] | str,
    *,
    cwd: Path,
    env: dict[str, str] | None,
    stdin: str | None,
    progress: Callable[[str], None] | None,
    progress_interval: float,
    timeout: float | None,
) -> tuple[subprocess.CompletedProcess[str], bool]:
    process_kwargs: dict[str, Any] = {}
    windows_job = None
    if os.name == "nt":
        windows_job = _WindowsProcessJob()
        process_kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_SUSPENDED", 0x00000004)
        )
    else:
        process_kwargs["start_new_session"] = True
    process = None
    try:
        process = subprocess.Popen(
            process_args,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE if stdin is not None else None,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            **process_kwargs,
        )
        if windows_job is not None:
            windows_job.assign_and_resume(process)
    except BaseException:
        if windows_job is not None:
            if process is not None:
                _stop_suspended_process(process)
            windows_job.close(kill=True)
        raise
    stop = threading.Event()
    started = time.monotonic()

    def heartbeat() -> None:
        if progress is None:
            return
        while not stop.wait(progress_interval):
            progress(f"executor still running ({time.monotonic() - started:.1f}s elapsed)")

    watcher = None
    if progress is not None:
        watcher = threading.Thread(target=heartbeat, name="dual-codex-progress", daemon=True)
    watcher_started = False
    completed_normally = False
    try:
        if watcher is not None:
            watcher.start()
            watcher_started = True
        stdout, stderr = process.communicate(input=stdin, timeout=timeout)
        completed_normally = True
    except subprocess.TimeoutExpired as exc:
        _terminate_process_tree(process, windows_job)
        try:
            stdout, stderr = process.communicate(timeout=2.0)
        except subprocess.TimeoutExpired as drain_error:
            # A detached descendant can escape process-tree termination while
            # retaining inherited pipe handles. Never let output collection
            # turn the host deadline into an unbounded wait.
            stdout = _partial_output(drain_error.stdout or exc.stdout)
            stderr = _partial_output(drain_error.stderr or exc.stderr)
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    pass
        if not stdout and exc.stdout:
            stdout = _partial_output(exc.stdout)
        if not stderr and exc.stderr:
            stderr = _partial_output(exc.stderr)
        return subprocess.CompletedProcess(process.args, 124, stdout, stderr), True
    except KeyboardInterrupt:
        _terminate_process_tree(process, windows_job)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                pass
        raise
    finally:
        stop.set()
        try:
            if watcher_started and watcher is not None:
                watcher.join(timeout=1)
        finally:
            if windows_job is not None:
                windows_job.close(kill=not completed_normally)
    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr), False


def _partial_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _stop_suspended_process(process: subprocess.Popen) -> None:
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=1.0)
    except (OSError, subprocess.TimeoutExpired):
        pass


class _WindowsProcessJob:
    """Contain a Windows process tree before its initial thread can run."""

    _KILL_ON_JOB_CLOSE = 0x00002000
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimitInformation),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        self._extended_limit_information = ExtendedLimitInformation
        kernel32 = self._kernel32
        kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = (
            wintypes.HANDLE,
            wintypes.INT,
            ctypes.c_void_p,
            wintypes.DWORD,
        )
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL

        self._handle = kernel32.CreateJobObjectW(None, None)
        if not self._handle:
            self._raise_last_error("CreateJobObjectW failed")
        self._closed = False
        information = self._extended_limit_information()
        information.BasicLimitInformation.LimitFlags = self._KILL_ON_JOB_CLOSE
        if not self._set_information(information):
            error = ctypes.get_last_error()
            self.close(kill=True)
            raise OSError(error, "SetInformationJobObject failed")

    def _set_information(self, information: Any) -> bool:
        return bool(
            self._kernel32.SetInformationJobObject(
                self._handle,
                self._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                self._ctypes.byref(information),
                self._ctypes.sizeof(information),
            )
        )

    def _raise_last_error(self, message: str) -> None:
        error = self._ctypes.get_last_error()
        raise OSError(error, message)

    def assign_and_resume(self, process: subprocess.Popen) -> None:
        process_handle = self._wintypes.HANDLE(int(process._handle))
        if not self._kernel32.AssignProcessToJobObject(self._handle, process_handle):
            self._raise_last_error("AssignProcessToJobObject failed")
        self._resume_initial_thread(process.pid)

    def _resume_initial_thread(self, process_id: int) -> None:
        ctypes = self._ctypes
        wintypes = self._wintypes

        class ThreadEntry32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ThreadID", wintypes.DWORD),
                ("th32OwnerProcessID", wintypes.DWORD),
                ("tpBasePri", wintypes.LONG),
                ("tpDeltaPri", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
            ]

        kernel32 = self._kernel32
        kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Thread32First.argtypes = (wintypes.HANDLE, ctypes.POINTER(ThreadEntry32))
        kernel32.Thread32First.restype = wintypes.BOOL
        kernel32.Thread32Next.argtypes = (wintypes.HANDLE, ctypes.POINTER(ThreadEntry32))
        kernel32.Thread32Next.restype = wintypes.BOOL
        kernel32.OpenThread.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenThread.restype = wintypes.HANDLE
        kernel32.ResumeThread.argtypes = (wintypes.HANDLE,)
        kernel32.ResumeThread.restype = wintypes.DWORD

        snapshot = kernel32.CreateToolhelp32Snapshot(0x00000004, 0)
        if not snapshot or int(snapshot) == ctypes.c_void_p(-1).value:
            self._raise_last_error("CreateToolhelp32Snapshot failed")
        try:
            entry = ThreadEntry32()
            entry.dwSize = ctypes.sizeof(entry)
            found_thread = False
            if kernel32.Thread32First(snapshot, ctypes.byref(entry)):
                while True:
                    if entry.th32OwnerProcessID == process_id:
                        thread = kernel32.OpenThread(0x0002, False, entry.th32ThreadID)
                        if not thread:
                            self._raise_last_error("OpenThread failed")
                        try:
                            previous_suspend_count = kernel32.ResumeThread(thread)
                            if previous_suspend_count != 1:
                                self._raise_last_error("Could not resume the suspended process thread")
                        finally:
                            kernel32.CloseHandle(thread)
                        found_thread = True
                        break
                    entry.dwSize = ctypes.sizeof(entry)
                    if not kernel32.Thread32Next(snapshot, ctypes.byref(entry)):
                        break
            if not found_thread:
                self._raise_last_error("The suspended process thread could not be located")
        finally:
            kernel32.CloseHandle(snapshot)

    def close(self, *, kill: bool) -> None:
        if self._closed:
            return
        if kill:
            self._kernel32.TerminateJobObject(self._handle, 124)
        else:
            information = self._extended_limit_information()
            information.BasicLimitInformation.LimitFlags = 0
            if not self._set_information(information):
                # Closing with KILL_ON_JOB_CLOSE is safer than leaving a child
                # process behind if this best-effort compatibility step fails.
                self._kernel32.TerminateJobObject(self._handle, 124)
        self._kernel32.CloseHandle(self._handle)
        self._closed = True


def _terminate_process_tree(
    process: subprocess.Popen,
    windows_job: _WindowsProcessJob | None = None,
) -> None:
    if os.name == "nt" and windows_job is not None:
        windows_job.close(kill=True)
    elif os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        except OSError:
            pass
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass


_DESKTOP_BRIDGE_ENV_KEYS = (
    "CODEX_INTERNAL_ORIGINATOR_OVERRIDE",
    "CODEX_MCP_NODE_PATH",
    "CODEX_APP_TOOLS_PIPE_PATH",
    "CODEX_PERMISSION_PROFILE",
    "CODEX_SESSION_ID",
    "CODEX_THREAD_ID",
    "CODEX_SHELL",
    "CODEX_CI",
)


def codex_environment(
    agent: AgentConfig,
    *,
    isolate_desktop_bridge: bool = False,
) -> dict[str, str]:
    env = os.environ.copy()
    env["CODEX_HOME"] = str(agent.codex_home)
    # The Codex child creates this scoped cache when it needs it.  Environment
    # construction must remain read-only: the Architect may be read-only and
    # a managed host may deny writes to the account profile's parent.
    env["NPM_CONFIG_CACHE"] = str(executor_npm_cache(agent))
    for name in ("OPENAI_API_KEY", "CODEX_API_KEY", "AZURE_OPENAI_API_KEY"):
        env.pop(name, None)
    if isolate_desktop_bridge:
        for name in _DESKTOP_BRIDGE_ENV_KEYS:
            env.pop(name, None)
    return env


def executor_npm_cache(agent: AgentConfig) -> Path:
    """Return a per-CODEX_HOME cache outside the target repository."""

    local_app_data = Path(os.environ.get("LOCALAPPDATA", "")).expanduser()
    if not local_app_data.is_absolute():
        local_app_data = Path.home() / "AppData" / "Local"
    identity = hashlib.sha256(
        str(agent.codex_home.expanduser().resolve()).encode("utf-8")
    ).hexdigest()[:16]
    return (local_app_data.resolve() / "DualCodex" / "npm-cache" / identity).resolve()
