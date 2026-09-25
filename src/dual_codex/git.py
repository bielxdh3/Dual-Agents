from __future__ import annotations

from pathlib import Path
import re
import tempfile
from typing import Callable, Iterable

from .process import CommandError, CommandResult, DEFAULT_HOST_COMMAND_TIMEOUT, run_command


def run_git(
    command: Iterable[str],
    *,
    cwd: Path,
    runner: Callable[..., CommandResult] = run_command,
    **kwargs,
) -> CommandResult:
    """Run trusted host Git without repository hooks, fsmonitor, or filters."""

    args = [str(part) for part in command]
    if not args or Path(args[0]).name.casefold() not in {"git", "git.exe"}:
        raise ValueError("run_git requires a Git command.")
    timeout = kwargs.get("timeout", DEFAULT_HOST_COMMAND_TIMEOUT)
    env = kwargs.get("env")
    # Git's content filters can launch repository-configured processes during
    # status/diff. Read their names with the safe config subcommand, then
    # override every executable filter entry for the actual host operation.
    with tempfile.TemporaryDirectory(prefix="dual-codex-no-git-hooks-") as hooks_dir:
        safe_prefix = [
            args[0],
            "-c",
            f"core.hooksPath={hooks_dir}",
            "-c",
            "core.fsmonitor=",
            "-c",
            "diff.external=",
        ]
        probe = run_command(
            # Query all active config scopes: worktree-specific filter
            # commands live outside .git/config when extensions.worktreeConfig
            # is enabled, and --local would miss them.
            [*safe_prefix, "config", "--name-only", "--get-regexp", r"^filter\."],
            cwd=cwd,
            env=env,
            check=False,
            timeout=timeout,
        )
        if probe.returncode not in {0, 1} and "not in a git directory" not in probe.stderr.casefold():
            message = "Could not safely inspect repository Git filter configuration."
            if probe.returncode == 124:
                message = "Timed out while inspecting repository Git filter configuration."
            failed = CommandResult(args, probe.returncode or 1, "", message)
            if kwargs.get("check", True):
                raise CommandError(message)
            return failed
        filter_names = sorted(
            {
                key.strip().rsplit(".", 1)[0]
                for key in probe.stdout.splitlines()
                if key.strip().casefold().endswith((".process", ".clean", ".smudge", ".required"))
            }
        )
        if any(not re.fullmatch(r"filter\.[A-Za-z0-9_.-]+", name) for name in filter_names):
            message = "Could not safely disable repository Git filter configuration."
            failed = CommandResult(args, 1, "", message)
            if kwargs.get("check", True):
                raise CommandError(message)
            return failed
        safe_args = [*safe_prefix]
        for name in filter_names:
            for field, value in (("process", ""), ("clean", ""), ("smudge", ""), ("required", "false")):
                safe_args.extend(("-c", f"{name}.{field}={value}"))
        safe_args.extend(args[1:])
        return runner(safe_args, cwd=cwd, **kwargs)


def ensure_git_repository(repository: Path) -> None:
    result = run_git(["git", "rev-parse", "--is-inside-work-tree"], cwd=repository)
    if result.stdout.strip() != "true":
        raise RuntimeError(f"Not a Git work tree: {repository}")


def status_porcelain(repository: Path) -> str:
    return run_git(["git", "status", "--porcelain=v1"], cwd=repository).stdout


def head_revision(repository: Path) -> str:
    return run_git(["git", "rev-parse", "HEAD"], cwd=repository).stdout.strip()


def status_and_diff(repository: Path) -> str:
    status = run_git(["git", "status", "--short"], cwd=repository).stdout
    unstaged = run_git(["git", "diff", "--no-ext-diff", "--no-textconv"], cwd=repository).stdout
    staged = run_git(["git", "diff", "--cached", "--no-ext-diff", "--no-textconv"], cwd=repository).stdout
    return (
        "## git status --short\n"
        f"{status or '(clean)'}\n"
        "## git diff\n"
        f"{unstaged or '(no unstaged diff)'}\n"
        "## git diff --cached\n"
        f"{staged or '(no staged diff)'}\n"
    )
