from __future__ import annotations

"""Headless Antigravity/Gemini Executor transport.

The adapter intentionally owns only the process/protocol boundary.  Mission
authorization, repository locking, diff capture, and result classification stay
in :mod:`dual_codex.delegation`.
"""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
from typing import Any, Callable

from .config import AgentConfig
from .process import CommandResult, _prepare_command


_TERMINAL_STATUSES = {
    "SUCCESS",
    "ERROR",
    "CANCELED",
    "CANCELLED",
    "INTERRUPTED",
    "INVALID",
    "WAITING",
}
_SUCCESS_STATUS = "SUCCESS"
_HEARTBEAT_SECONDS = 15.0
_SHUTDOWN_TIMEOUT_SECONDS = 5.0
_CANONICAL_INSTRUCTIONS_ROOT = Path(r"C:\CodexGlobal")


@dataclass(frozen=True)
class _StreamItem:
    source: str
    line: str | None


def _environment() -> dict[str, str]:
    """Keep local Antigravity auth available without leaking Codex/API state."""

    env = os.environ.copy()
    for name in (
        "CODEX_HOME",
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "AZURE_OPENAI_API_KEY",
    ):
        env.pop(name, None)
    return env


def antigravity_status(command: str, *, cwd: Path | None = None) -> str:
    """Return a non-secret availability status for the configured executable."""

    resolved = shutil.which(command) or (str(Path(command)) if Path(command).exists() else "")
    if not resolved:
        return "NOT FOUND"
    try:
        result = subprocess.run(
            [resolved, "--version"],
            cwd=cwd,
            env=_environment(),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "UNAVAILABLE"
    return "OK" if result.returncode == 0 else "UNAVAILABLE"


def _canonical_instructions_root() -> Path:
    root = _CANONICAL_INSTRUCTIONS_ROOT
    if not (root / "AGENTS.md").is_file():
        raise FileNotFoundError(f"Canonical instruction file is unavailable: {root / 'AGENTS.md'}")
    if not (root / "skills").is_dir():
        raise FileNotFoundError(f"Canonical skill tree is unavailable: {root / 'skills'}")
    return root


def _event_message(prompt: str) -> str:
    return json.dumps(
        {
            "event": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": prompt}],
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def build_command(
    *,
    command: str,
    agent: AgentConfig,
    repository: Path,
    schema_path: Path | None = None,
    conversation_id: str = "",
    timeout_seconds: float | None = None,
    task_artifact_path: Path | None = None,
) -> list[str]:
    """Construct the verified machine-readable invocation.

    ``--new-project`` binds a fresh headless session to the canonical
    repository.  Antigravity's project resolution otherwise defaults
    independently of the process cwd, which can leave the orchestrator's
    project as the primary workspace.  ``accept-edits`` preserves the
    existing workspace-write role without using the dangerous permission
    bypass.  The canonical policy tree is added as a separate read scope so
    the Executor can satisfy the required global bootstrap without granting
    access to the orchestrator checkout.  Permission requests are never
    answered by this adapter; an unattended request therefore fails closed.
    """

    repository = repository.expanduser().resolve()
    result = [
        str(command),
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--mode",
        "accept-edits",
    ]
    if conversation_id:
        result.extend(["--conversation", conversation_id])
    else:
        result.append("--new-project")
    result.extend(["--add-dir", str(repository)])
    canonical_root = _canonical_instructions_root()
    if canonical_root != repository:
        result.extend(["--add-dir", str(canonical_root)])
    if task_artifact_path is not None:
        artifact = task_artifact_path.expanduser().resolve()
        if not artifact.is_file():
            raise ValueError(f"Task artifact does not exist: {artifact}")
        artifact_dir = artifact.parent
        if artifact_dir.name != "executor-task-artifacts":
            raise ValueError("Task artifact must be in the canonical executor-task-artifacts directory")
        if artifact_dir != repository:
            result.extend(["--add-dir", str(artifact_dir)])
    if schema_path is not None:
        result.extend(["--json-schema", str(schema_path.resolve())])
    if agent.model:
        result.extend(["--model", agent.model])
    if agent.reasoning_effort:
        result.extend(["--effort", agent.reasoning_effort])
    if timeout_seconds is not None and timeout_seconds > 0:
        result.extend(["--print-timeout", f"{timeout_seconds:g}s"])
    return result


def _reader(source: str, stream: Any, events: queue.Queue[_StreamItem]) -> None:
    try:
        for line in iter(stream.readline, ""):
            events.put(_StreamItem(source, line))
    finally:
        events.put(_StreamItem(source, None))


def _close_process(process: subprocess.Popen[str], *, force: bool = False) -> None:
    try:
        if process.stdin is not None:
            process.stdin.close()
    except (BrokenPipeError, OSError):
        pass
    if process.poll() is not None:
        return
    if not force:
        try:
            process.wait(timeout=_SHUTDOWN_TIMEOUT_SECONDS)
            return
        except (subprocess.TimeoutExpired, OSError):
            pass
    try:
        process.terminate()
        process.wait(timeout=_SHUTDOWN_TIMEOUT_SECONDS)
    except (subprocess.TimeoutExpired, OSError):
        try:
            process.kill()
            process.wait(timeout=_SHUTDOWN_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _timeout_seconds(config: Any | None) -> float:
    value = getattr(config, "antigravity_turn_timeout", None)
    if value is None:
        value = getattr(config, "app_server_turn_timeout", 600.0)
    try:
        return max(float(value), 1.0)
    except (TypeError, ValueError):
        return 600.0


def _response_text(response: Any) -> str | None:
    if isinstance(response, str):
        return response
    if isinstance(response, (dict, list)):
        return json.dumps(response, ensure_ascii=False)
    return None


def run_antigravity(
    *,
    command: str,
    agent: AgentConfig,
    repository: Path,
    prompt: str,
    output_path: Path,
    schema_path: Path | None = None,
    config: Any | None = None,
    conversation_id: str = "",
    task_artifact_path: Path | None = None,
    task_sha256: str = "",
    progress: Callable[[str], None] | None = None,
) -> CommandResult:
    """Run one streamed Antigravity turn and wait for its terminal result."""

    repository = repository.expanduser().resolve()
    timeout_seconds = _timeout_seconds(config)
    metadata: dict[str, Any] = {
        "executor_provider": "antigravity",
        "antigravity_protocol": "stream-json",
        "antigravity_conversation_id": "",
        "antigravity_terminal_status": "",
        "antigravity_event_count": 0,
        "antigravity_repository": str(repository),
        "antigravity_cwd": str(repository),
        "task_transport": "file" if task_artifact_path is not None else "inline",
        "task_artifact": str(task_artifact_path.expanduser().resolve()) if task_artifact_path is not None else "",
        "task_sha256": task_sha256,
        "reuse_existing": False,
    }
    if not repository.is_dir():
        metadata["antigravity_terminal_status"] = "WORKSPACE_UNAVAILABLE"
        return CommandResult(
            [str(command)],
            1,
            "",
            f"Antigravity workspace does not exist or is not a directory: {repository}",
            metadata,
        )
    if task_artifact_path is not None:
        artifact = task_artifact_path.expanduser().resolve()
        if not artifact.is_file():
            metadata["antigravity_terminal_status"] = "TASK_ARTIFACT_UNAVAILABLE"
            return CommandResult(
                [str(command)],
                1,
                "",
                f"Task artifact does not exist: {artifact}",
                metadata,
            )
    try:
        _canonical_instructions_root()
    except OSError as exc:
        metadata["antigravity_terminal_status"] = "INSTRUCTIONS_UNAVAILABLE"
        return CommandResult(
            [str(command)],
            1,
            "",
            str(exc),
            metadata,
        )
    display_command = build_command(
        command=command,
        agent=agent,
        repository=repository,
        schema_path=schema_path,
        conversation_id=conversation_id,
        timeout_seconds=timeout_seconds,
        task_artifact_path=task_artifact_path,
    )
    try:
        process_args = _prepare_command(display_command.copy())
        process = subprocess.Popen(
            process_args,
            cwd=repository,
            env=_environment(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
        )
    except (OSError, ValueError) as exc:
        metadata["antigravity_terminal_status"] = "STARTUP_ERROR"
        return CommandResult(display_command, 1, "", str(exc), metadata)

    events: queue.Queue[_StreamItem] = queue.Queue()
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    threads = [
        threading.Thread(
            target=_reader,
            args=("stdout", process.stdout, events),
            name="antigravity-stdout",
            daemon=True,
        ),
        threading.Thread(
            target=_reader,
            args=("stderr", process.stderr, events),
            name="antigravity-stderr",
            daemon=True,
        ),
    ]
    for thread in threads:
        thread.start()

    conversation = ""
    terminal_status = ""
    response: str | None = None
    response_source = ""
    protocol_error = ""
    stdout_closed = False
    started = time.monotonic()
    last_progress = started
    try:
        if process.stdin is None:
            raise OSError("Antigravity stdin was not opened.")
        process.stdin.write(_event_message(prompt) + "\n")
        process.stdin.flush()
        while not terminal_status:
            if time.monotonic() - started >= timeout_seconds:
                terminal_status = "TIMEOUT"
                protocol_error = f"Antigravity turn timed out after {timeout_seconds:g}s."
                break
            try:
                item = events.get(timeout=1.0)
            except queue.Empty:
                if progress is not None and time.monotonic() - last_progress >= _HEARTBEAT_SECONDS:
                    progress("Antigravity executor still running")
                    last_progress = time.monotonic()
                if process.poll() is not None and stdout_closed:
                    terminal_status = "PREMATURE_CLOSE"
                    protocol_error = "Antigravity closed stdout before a terminal result event."
                continue

            if item.source == "stderr":
                if item.line is not None:
                    stderr_lines.append(item.line)
                continue
            if item.line is None:
                stdout_closed = True
                if not terminal_status and process.poll() is not None:
                    terminal_status = "PREMATURE_CLOSE"
                    protocol_error = "Antigravity closed stdout before a terminal result event."
                continue

            stdout_lines.append(item.line)
            try:
                event = json.loads(item.line)
            except json.JSONDecodeError as exc:
                terminal_status = "MALFORMED"
                protocol_error = f"Antigravity emitted malformed NDJSON: {exc}."
                break
            if not isinstance(event, dict) or not isinstance(event.get("event"), str):
                terminal_status = "MALFORMED"
                protocol_error = "Antigravity emitted an event without a string 'event' field."
                break
            metadata["antigravity_event_count"] += 1
            event_name = event["event"]
            if event_name == "init":
                value = event.get("conversation_id")
                if isinstance(value, str):
                    conversation = value
                continue
            if event_name == "step_update":
                step = event.get("step_update")
                if isinstance(step, dict) and isinstance(step.get("conversation_id"), str):
                    conversation = step["conversation_id"]
                continue
            if event_name != "result":
                # Unknown notifications are retained in stdout but do not alter
                # the terminal state; the CLI may add metadata in future.
                continue

            result = event.get("result")
            if not isinstance(result, dict):
                terminal_status = "MALFORMED"
                protocol_error = "Antigravity result event did not contain an object result."
                break
            status = result.get("status")
            if not isinstance(status, str) or status.upper() not in _TERMINAL_STATUSES:
                terminal_status = "INVALID"
                protocol_error = f"Antigravity returned an unsupported terminal status: {status!r}."
                break
            terminal_status = status.upper()
            value = result.get("conversation_id")
            if isinstance(value, str):
                conversation = value
            structured_output = _response_text(result.get("structured_output"))
            if structured_output is not None:
                response = structured_output
                response_source = "structured_output"
            else:
                response = _response_text(result.get("response"))
                response_source = "response"
            if terminal_status == _SUCCESS_STATUS and response is None:
                terminal_status = "MALFORMED"
                protocol_error = "Antigravity SUCCESS result did not contain a text or object response."
                break
            if terminal_status != _SUCCESS_STATUS:
                error = result.get("error")
                if isinstance(error, str) and error.strip():
                    protocol_error = error.strip()
                elif not protocol_error:
                    protocol_error = f"Antigravity returned terminal status {terminal_status}."
    except KeyboardInterrupt:
        terminal_status = "CANCELED"
        protocol_error = "Antigravity turn interrupted by the user."
    except (BrokenPipeError, OSError) as exc:
        terminal_status = "STARTUP_ERROR"
        protocol_error = str(exc)
    finally:
        _close_process(process, force=terminal_status in {"TIMEOUT", "MALFORMED", "PREMATURE_CLOSE"})
        for thread in threads:
            thread.join(timeout=1.0)
        for stream in (process.stdout, process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass

    returncode = process.returncode
    if terminal_status == _SUCCESS_STATUS and returncode == 0 and response is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(response.rstrip() + "\n", encoding="utf-8", newline="\n")
        metadata["antigravity_conversation_id"] = conversation
        metadata["antigravity_terminal_status"] = terminal_status
        metadata["antigravity_result_source"] = response_source
        return CommandResult(
            display_command,
            0,
            "".join(stdout_lines),
            "".join(stderr_lines),
            metadata,
        )

    metadata["antigravity_conversation_id"] = conversation
    metadata["antigravity_terminal_status"] = terminal_status or "PREMATURE_CLOSE"
    if returncode is None or returncode == 0:
        returncode = 1
    stderr = "".join(stderr_lines)
    if protocol_error:
        stderr = f"{protocol_error}\n{stderr}" if stderr else protocol_error
    return CommandResult(display_command, returncode, "".join(stdout_lines), stderr, metadata)
