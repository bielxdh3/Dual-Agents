from __future__ import annotations

from pathlib import Path
import re
import tempfile
from typing import Callable, Iterable

from .process import CommandError, CommandResult, DEFAULT_HOST_COMMAND_TIMEOUT, run_command

_GIT_GLOBAL_FLAGS = {
    "-p",
    "-P",
    "--paginate",
    "--no-pager",
    "--no-replace-objects",
    "--no-optional-locks",
    "--no-lazy-fetch",
    "--bare",
    "-v",
}
_ALLOWED_TRUSTED_CONFIG = {
    "credential.helper": {"", "manager"},
    "http.extraheader": {""},
    "http.sslbackend": {"openssl"},
    "http.sslverify": {"true"},
}


def _command_offset(git_args: list[str]) -> int:
    index = 0
    while index < len(git_args):
        argument = git_args[index]
        if argument in _GIT_GLOBAL_FLAGS:
            index += 1
            continue
        if argument.startswith("-"):
            if argument == "-c" and index + 1 < len(git_args):
                key, separator, value = git_args[index + 1].partition("=")
                allowed_values = _ALLOWED_TRUSTED_CONFIG.get(key.casefold())
                if not separator or allowed_values is None or value.casefold() not in allowed_values:
                    raise ValueError("run_git does not accept untrusted Git config overrides.")
                index += 2
                continue
            raise ValueError("run_git does not accept Git global config or repository overrides.")
        return index
    return len(git_args)


def run_git(
    command: Iterable[str],
    *,
    cwd: Path,
    runner: Callable[..., CommandResult] = run_command,
    **kwargs,
) -> CommandResult:
    """Run trusted host Git without repository hooks or fsmonitor."""

    args = [str(part) for part in command]
    if not args or Path(args[0]).name.casefold() not in {"git", "git.exe"}:
        raise ValueError("run_git requires a Git command.")
    timeout = kwargs.get("timeout", DEFAULT_HOST_COMMAND_TIMEOUT)
    env = kwargs.get("env")
    # Git's content filters can launch repository-configured processes during
    # status/diff. Disabling a filter changes Git's clean/smudge comparison
    # semantics, so first reject commands where a filter applies to a tracked
    # path. Unfiltered paths remain safe to inspect with filter commands
    # disabled below.
    with tempfile.TemporaryDirectory(prefix="dual-codex-no-git-hooks-") as hooks_dir:
        safe_prefix = [
            args[0],
            "-c",
            f"core.hooksPath={hooks_dir}",
            "-c",
            "core.fsmonitor=",
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
        git_args = args[1:]
        command_offset = _command_offset(git_args)
        command_name = git_args[command_offset] if command_offset < len(git_args) else ""
        filter_paths: list[str] = []
        if command_name in {"status", "diff"}:
            tracked = run_command(
                [*safe_prefix, "ls-files", "-z"],
                cwd=cwd,
                env=env,
                check=False,
                timeout=timeout,
            )
            if tracked.returncode != 0:
                no_repository = "not a git repository" in tracked.stderr.casefold()
                no_repository = no_repository or "not in a git directory" in tracked.stderr.casefold()
                if not no_repository:
                    message = "Could not safely inspect tracked paths for Git content filters."
                    if tracked.returncode == 124:
                        message = "Timed out while inspecting tracked paths for Git content filters."
                    failed = CommandResult(args, tracked.returncode or 1, "", message)
                    if kwargs.get("check", True):
                        raise CommandError(message)
                    return failed
            else:
                filter_paths.extend(path for path in tracked.stdout.split("\0") if path)
        elif command_name == "hash-object":
            for index, part in enumerate(git_args):
                if part.startswith("--path="):
                    filter_paths.append(part.partition("=")[2])
                elif part == "--path" and index + 1 < len(git_args):
                    filter_paths.append(git_args[index + 1])
        if filter_paths:
            if any("\ufffd" in path for path in filter_paths):
                message = "Cannot safely inspect Git content filters because a tracked path is not valid UTF-8."
                failed = CommandResult(args, 1, "", message)
                if kwargs.get("check", True):
                    raise CommandError(message)
                return failed
            attributes = run_command(
                [*safe_prefix, "check-attr", "-z", "--stdin", "filter"],
                cwd=cwd,
                env=env,
                stdin="".join(f"{path}\0" for path in filter_paths),
                check=False,
                timeout=timeout,
            )
            if attributes.returncode != 0:
                message = "Could not safely inspect Git content-filter attributes."
                if attributes.returncode == 124:
                    message = "Timed out while inspecting Git content-filter attributes."
                failed = CommandResult(args, attributes.returncode or 1, "", message)
                if kwargs.get("check", True):
                    raise CommandError(message)
                return failed
            fields = attributes.stdout.split("\0")
            if fields and fields[-1] == "":
                fields.pop()
            if len(fields) % 3:
                message = "Could not safely parse Git content-filter attributes."
                failed = CommandResult(args, 1, "", message)
                if kwargs.get("check", True):
                    raise CommandError(message)
                return failed
            if any(fields[index + 2] not in {"", "unspecified", "unset"} for index in range(0, len(fields), 3)):
                message = (
                    "Trusted host Git cannot inspect paths that use Git content filters without executing "
                    "repository-configured commands. Remove the filter attribute or use an unfiltered checkout."
                )
                failed = CommandResult(args, 1, "", message)
                if kwargs.get("check", True):
                    raise CommandError(message)
                return failed
        safe_args = [args[0], *git_args[:command_offset], *safe_prefix[1:]]
        for name in filter_names:
            for field, value in (("process", ""), ("clean", ""), ("smudge", ""), ("required", "false")):
                safe_args.extend(("-c", f"{name}.{field}={value}"))
        if command_name == "diff":
            safe_args.extend(git_args[command_offset : command_offset + 1])
            safe_args.extend(("--no-ext-diff", "--no-textconv"))
            safe_args.extend(git_args[command_offset + 1 :])
        else:
            safe_args.extend(git_args[command_offset:])
        return runner(safe_args, cwd=cwd, **kwargs)


def ensure_git_repository(repository: Path) -> None:
    result = run_git(["git", "rev-parse", "--is-inside-work-tree"], cwd=repository)
    if result.stdout.strip() != "true":
        raise RuntimeError(f"Not a Git work tree: {repository}")


def git_top_level(repository: Path) -> Path:
    """Resolve the actual worktree root without trusting repository metadata."""

    path = Path(repository).expanduser().resolve(strict=True)
    result = run_git(["git", "rev-parse", "--show-toplevel"], cwd=path, check=False)
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(f"Could not resolve Git top-level for repository: {path}")
    try:
        return Path(result.stdout.strip()).expanduser().resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(f"Git top-level is unavailable for repository: {path}") from exc


def status_porcelain(repository: Path) -> str:
    return run_git(["git", "status", "--porcelain=v1"], cwd=repository).stdout


def head_revision(repository: Path) -> str:
    return run_git(["git", "rev-parse", "HEAD"], cwd=repository).stdout.strip()


def status_and_diff(repository: Path) -> str:
    status = status_porcelain(repository)
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
