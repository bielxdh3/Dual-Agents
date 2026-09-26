from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path
import re
import stat
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


def _porcelain_entries(raw: str) -> list[dict[str, str]]:
    fields = raw.split("\0")
    entries: list[dict[str, str]] = []
    index = 0
    while index < len(fields):
        record = fields[index]
        index += 1
        if not record:
            continue
        if len(record) < 4 or record[2] != " ":
            raise RuntimeError("Could not safely parse Git's NUL-delimited status output.")
        entry = {"index": record[0], "worktree": record[1], "path": record[3:]}
        if "R" in record[:2] or "C" in record[:2]:
            if index >= len(fields) or not fields[index]:
                raise RuntimeError("Could not safely parse a Git rename/copy status entry.")
            entry["source_path"] = fields[index]
            index += 1
        entries.append(entry)
    if any("\ufffd" in entry["path"] for entry in entries):
        raise RuntimeError("Git status contains a path that is not valid UTF-8.")
    return entries


def _index_entries(repository: Path) -> dict[str, list[dict[str, str]]]:
    result = run_git(["git", "ls-files", "--stage", "-z"], cwd=repository)
    entries: dict[str, list[dict[str, str]]] = {}
    for record in result.stdout.split("\0"):
        if not record:
            continue
        metadata, separator, path = record.partition("\t")
        fields = metadata.split()
        if not separator or len(fields) != 3 or "\ufffd" in path:
            raise RuntimeError("Could not safely parse Git's index entries.")
        mode, object_id, stage = fields
        entries.setdefault(path, []).append({"mode": mode, "object_id": object_id, "stage": stage})
    return entries


def _worktree_snapshot(repository: Path, relative_path: str) -> dict[str, object]:
    """Hash raw worktree bytes without following symlinks or running Git filters."""

    parts = relative_path.split("/")
    if not relative_path or relative_path.startswith("/") or any(part in {"", ".", ".."} for part in parts):
        return {"kind": "unknown", "reason": "invalid_repository_relative_path"}
    path = repository
    try:
        for part in parts[:-1]:
            path = path / part
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or _is_reparse_point(info) or not stat.S_ISDIR(info.st_mode):
                return {"kind": "unknown", "reason": "unsafe_parent_path"}
        path = path / parts[-1]
        before = path.lstat()
    except FileNotFoundError:
        return {"kind": "missing"}
    except OSError:
        return {"kind": "unknown", "reason": "lstat_failed"}

    if stat.S_ISLNK(before.st_mode):
        try:
            target = os.readlink(path)
            after = path.lstat()
            if (before.st_dev, before.st_ino, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_mtime_ns):
                return {"kind": "unknown", "reason": "path_changed_during_snapshot"}
            digest = hashlib.sha256(os.fsencode(target)).hexdigest()
            return {"kind": "symlink", "mode": stat.S_IMODE(before.st_mode), "sha256": digest}
        except OSError:
            return {"kind": "unknown", "reason": "symlink_read_failed"}
    if _is_reparse_point(before):
        return {"kind": "unknown", "reason": "reparse_point"}
    if stat.S_ISDIR(before.st_mode):
        return {"kind": "directory", "mode": stat.S_IMODE(before.st_mode)}
    if not stat.S_ISREG(before.st_mode):
        return {"kind": "unknown", "reason": "unsupported_file_type"}

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or _is_reparse_point(opened):
                return {"kind": "unknown", "reason": "unsafe_opened_file"}
            digest = hashlib.sha256()
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
            after = path.lstat()
            if (
                (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            ):
                return {"kind": "unknown", "reason": "path_changed_during_snapshot"}
            return {"kind": "file", "mode": stat.S_IMODE(after.st_mode), "sha256": digest.hexdigest()}
    except OSError:
        return {"kind": "unknown", "reason": "file_read_failed"}


def _is_reparse_point(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def capture_git_baseline(repository: Path) -> dict[str, object]:
    """Capture a hook/filter-safe, hash-only Git/worktree baseline."""

    repository = git_top_level(repository)
    status = run_git(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=repository,
    ).stdout
    status_entries = _porcelain_entries(status)
    index_raw = run_git(["git", "ls-files", "--stage", "-z"], cwd=repository).stdout
    index = _index_entries(repository)
    tracked_paths = sorted(index)
    untracked_paths = sorted(entry["path"] for entry in status_entries if entry["index"] == "?" and entry["worktree"] == "?")
    paths_to_hash = sorted(set(tracked_paths).union(untracked_paths))
    snapshots = {path: _worktree_snapshot(repository, path) for path in paths_to_hash}
    staged_paths = {
        entry["path"]
        for entry in status_entries
        if entry["index"] not in {" ", "?"}
    }
    branch = run_git(["git", "symbolic-ref", "--short", "-q", "HEAD"], cwd=repository, check=False)
    if branch.returncode not in {0, 1}:
        raise RuntimeError("Could not safely determine the current Git branch state.")
    head_result = run_git(["git", "rev-parse", "--verify", "HEAD"], cwd=repository, check=False)
    if head_result.returncode == 0:
        head = head_result.stdout.strip()
    elif branch.returncode == 0 and (
        "unknown revision" in head_result.stderr.casefold()
        or "needed a single revision" in head_result.stderr.casefold()
    ):
        head = ""
    else:
        raise RuntimeError("Could not safely determine the current Git HEAD.")
    return {
        "schema_version": 1,
        "repository": str(repository),
        "git_dir": run_git(["git", "rev-parse", "--absolute-git-dir"], cwd=repository).stdout.strip(),
        "head": head,
        "branch": branch.stdout.strip() if branch.returncode == 0 else "",
        "detached": branch.returncode == 1,
        "status_entries": status_entries,
        "staged_status": {entry["path"]: entry["index"] for entry in status_entries if entry["index"] not in {" ", "?"}},
        "unstaged_status": {entry["path"]: entry["worktree"] for entry in status_entries if entry["worktree"] not in {" ", "?"}},
        "untracked_paths": untracked_paths,
        "index_fingerprint": hashlib.sha256(index_raw.encode("utf-8")).hexdigest(),
        "staged_index_entries": {path: index[path] for path in sorted(staged_paths) if path in index},
        "worktree_snapshots": snapshots,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "complete": all(snapshot.get("kind") != "unknown" for snapshot in snapshots.values()),
    }


def attribute_git_mutations(
    repository: Path,
    baseline: dict[str, object],
    *,
    excluded_paths: Iterable[str] = (),
) -> dict[str, object]:
    """Compare final status and raw file hashes with a captured run baseline."""

    repository = git_top_level(repository)
    if str(baseline.get("repository", "")) != str(repository):
        raise RuntimeError("Git baseline belongs to a different repository root.")
    final = capture_git_baseline(repository)
    excluded = tuple(path.replace("\\", "/").strip("/") for path in excluded_paths if path)

    def is_excluded(path: str) -> bool:
        return any(path == prefix or path.startswith(prefix + "/") for prefix in excluded)

    old_entries = {entry["path"]: entry for entry in baseline.get("status_entries", []) if isinstance(entry, dict)}
    new_entries = {entry["path"]: entry for entry in final.get("status_entries", []) if isinstance(entry, dict)}
    old_snapshots = baseline.get("worktree_snapshots", {})
    new_snapshots = final.get("worktree_snapshots", {})
    old_index = baseline.get("staged_index_entries", {})
    new_index = final.get("staged_index_entries", {})
    old_head = str(baseline.get("head", ""))
    new_head = str(final.get("head", ""))
    user_paths = {
        path for path in set(old_snapshots) | set(new_snapshots) | set(old_entries) | set(new_entries)
        if not is_excluded(path)
    }
    unchanged: list[str] = []
    touched: list[str] = []
    created: list[str] = []
    removed: list[str] = []
    unknown: list[str] = []
    for path in sorted(user_paths):
        old_exists = path in old_snapshots
        new_exists = path in new_snapshots
        before = old_snapshots.get(path, {}) if isinstance(old_snapshots, dict) else {}
        after = new_snapshots.get(path, {}) if isinstance(new_snapshots, dict) else {}
        old_status = old_entries.get(path)
        new_status = new_entries.get(path)
        old_staged = old_index.get(path) if isinstance(old_index, dict) else None
        new_staged = new_index.get(path) if isinstance(new_index, dict) else None
        if old_exists and not new_exists or (isinstance(after, dict) and after.get("kind") == "missing"):
            removed.append(path)
            continue
        if not old_exists and new_exists:
            created.append(path)
            continue
        if before.get("kind") == "unknown" or after.get("kind") == "unknown":
            unknown.append(path)
            continue
        if old_status == new_status and before == after and old_staged == new_staged:
            if old_status is not None:
                unchanged.append(path)
            continue
        if old_status is None and new_status is not None:
            created.append(path)
        elif old_status is not None and new_status is None:
            touched.append(path)
        elif old_status is not None or new_status is not None or before != after or old_staged != new_staged:
            touched.append(path)

    return {
        "schema_version": 1,
        "status": "complete" if baseline.get("complete") and final.get("complete") and not unknown else "unknown",
        "repository": str(repository),
        "initial_head": old_head,
        "final_head": new_head,
        "head_changed": old_head != new_head,
        "initial_branch": baseline.get("branch", ""),
        "final_branch": final.get("branch", ""),
        "branch_changed": baseline.get("branch", "") != final.get("branch", "") or baseline.get("detached") != final.get("detached"),
        "staged_state_changed": baseline.get("index_fingerprint") != final.get("index_fingerprint"),
        "unchanged_preexisting_paths": unchanged,
        "run_touched_paths": touched,
        "run_created_paths": created,
        "run_removed_paths": removed,
        "unknown_paths": unknown,
        "excluded_control_paths": list(excluded),
        "observed_at": final.get("captured_at", ""),
    }


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
