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
    env = dict(os.environ if kwargs.get("env") is None else kwargs["env"])
    # Prevent a status read at either edge of scan-only attribution from
    # taking an optional index lock and rewriting cache-only index fields.
    env["GIT_OPTIONAL_LOCKS"] = "0"
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
        run_kwargs = dict(kwargs)
        run_kwargs["env"] = env
        return runner(safe_args, cwd=cwd, **run_kwargs)


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


def _stat_identity(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_mode,
        getattr(info, "st_file_attributes", 0),
    )


def _directory_identity(info: os.stat_result) -> tuple[int, int, int, int]:
    """Identify a directory without treating read-only access-time/mtime noise as content."""

    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        getattr(info, "st_file_attributes", 0),
    )


def _worktree_snapshot(
    repository: Path,
    relative_path: str,
    *,
    max_file_bytes: int | None = None,
) -> dict[str, object]:
    """Hash raw worktree bytes without following symlinks or running Git filters."""

    parts = relative_path.split("/")
    if not relative_path or relative_path.startswith("/") or any(part in {"", ".", ".."} for part in parts):
        return {"kind": "unknown", "reason": "invalid_repository_relative_path"}
    path = repository
    parent_snapshots: list[tuple[Path, os.stat_result]] = []
    try:
        root_info = path.lstat()
        if stat.S_ISLNK(root_info.st_mode) or _is_reparse_point(root_info) or not stat.S_ISDIR(root_info.st_mode):
            return {"kind": "unknown", "reason": "unsafe_repository_root"}
        for part in parts[:-1]:
            path = path / part
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or _is_reparse_point(info) or not stat.S_ISDIR(info.st_mode):
                return {"kind": "unknown", "reason": "unsafe_parent_path"}
            parent_snapshots.append((path, info))
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
    if max_file_bytes is not None and before.st_size > max_file_bytes:
        return {"kind": "unknown", "reason": "file_exceeds_metadata_limit"}

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or _is_reparse_point(opened)
                or stat.S_ISLNK(opened.st_mode)
                or _stat_identity(opened) != _stat_identity(before)
            ):
                return {"kind": "unknown", "reason": "unsafe_opened_file"}
            digest = hashlib.sha256()
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
            after = path.lstat()
            parents_unchanged = all(
                _stat_identity(parent.lstat()) == _stat_identity(parent_before)
                for parent, parent_before in parent_snapshots
            )
            root_after = repository.lstat()
            if (
                _stat_identity(before) != _stat_identity(after)
                or not parents_unchanged
                or _stat_identity(root_info) != _stat_identity(root_after)
                or _stat_identity(opened) != _stat_identity(os.fstat(handle.fileno()))
            ):
                return {"kind": "unknown", "reason": "path_changed_during_snapshot"}
            return {"kind": "file", "mode": stat.S_IMODE(opened.st_mode), "sha256": digest.hexdigest()}
    except OSError:
        return {"kind": "unknown", "reason": "file_read_failed"}


def _is_reparse_point(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _ignored_tree_snapshots(repository: Path, ignored_paths: Iterable[str]) -> dict[str, dict[str, object]]:
    """Snapshot ignored entries, recursively enumerating directories without following links."""

    snapshots: dict[str, dict[str, object]] = {}
    enumerated: set[str] = set()
    pending = [path.rstrip("/") for path in ignored_paths]
    while pending:
        relative_path = pending.pop()
        if relative_path in enumerated:
            continue
        enumerated.add(relative_path)
        snapshot = snapshots.get(relative_path)
        if snapshot is None:
            snapshot = _worktree_snapshot(repository, relative_path)
            snapshots[relative_path] = snapshot
        if snapshot.get("kind") != "directory":
            continue

        directory = repository / Path(*relative_path.split("/"))
        try:
            before = directory.lstat()
            if stat.S_ISLNK(before.st_mode) or _is_reparse_point(before) or not stat.S_ISDIR(before.st_mode):
                snapshots[relative_path] = {"kind": "unknown", "reason": "unsafe_ignored_directory"}
                continue
            with os.scandir(directory) as entries:
                children = list(entries)
            after = directory.lstat()
            if (
                stat.S_ISLNK(after.st_mode)
                or _is_reparse_point(after)
                or not stat.S_ISDIR(after.st_mode)
                or (before.st_dev, before.st_ino, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_mtime_ns)
            ):
                snapshots[relative_path] = {"kind": "unknown", "reason": "directory_changed_during_snapshot"}
                continue
        except OSError:
            snapshots[relative_path] = {"kind": "unknown", "reason": "directory_enumeration_failed"}
            continue

        for entry in children:
            child_path = f"{relative_path}/{entry.name}"
            child_snapshot = _worktree_snapshot(repository, child_path)
            snapshots[child_path] = child_snapshot
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                snapshots[child_path] = {"kind": "unknown", "reason": "directory_entry_stat_failed"}
                continue
            if (
                stat.S_ISDIR(info.st_mode)
                and not stat.S_ISLNK(info.st_mode)
                and not _is_reparse_point(info)
            ):
                pending.append(child_path)
    return snapshots


_GIT_METADATA_FILES = (
    "HEAD",
    "ORIG_HEAD",
    "FETCH_HEAD",
    "MERGE_HEAD",
    "MERGE_MSG",
    "MERGE_MODE",
    "MERGE_AUTOSTASH",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    "REBASE_HEAD",
    "AUTO_MERGE",
    "SQUASH_MSG",
    "BISECT_LOG",
    "BISECT_NAMES",
    "BISECT_START",
    "BISECT_EXPECTED_REV",
    "BISECT_TERMS",
    "COMMIT_EDITMSG",
    "index.lock",
    "config.lock",
    "packed-refs.lock",
    "HEAD.lock",
    "shallow.lock",
    "FETCH_HEAD.lock",
    "index",
    "config",
    "config.worktree",
    "packed-refs",
    "shallow",
    "commondir",
    "gitdir",
    "info/exclude",
    "info/attributes",
    "info/sparse-checkout",
    "info/grafts",
    "objects/info/alternates",
    "objects/info/http-alternates",
    "objects/info/packs",
)
_GIT_METADATA_TREES = (
    "refs",
    "hooks",
    "reftable",
    "worktrees",
    "logs",
    "sequencer",
    "rebase-apply",
    "rebase-merge",
    "bisect",
    "rr-cache",
)


def _git_object_inventory(
    root: Path,
    *,
    max_entries: int = 250_000,
    max_bytes: int = 1024 * 1024 * 1024,
) -> dict[str, object]:
    """Hash a bounded inventory and contents of Git's object store."""

    objects = root / "objects"
    digest = hashlib.sha256()
    pending = [Path("objects")]
    visited = 0
    total_bytes = 0
    try:
        root_info = objects.lstat()
        if stat.S_ISLNK(root_info.st_mode) or _is_reparse_point(root_info) or not stat.S_ISDIR(root_info.st_mode):
            return {"kind": "unknown", "reason": "unsafe_git_object_directory"}
    except OSError:
        return {"kind": "unknown", "reason": "git_object_directory_unavailable"}

    while pending:
        relative_dir = pending.pop()
        directory = root / relative_dir
        try:
            before = directory.lstat()
            if stat.S_ISLNK(before.st_mode) or _is_reparse_point(before) or not stat.S_ISDIR(before.st_mode):
                return {"kind": "unknown", "reason": "unsafe_git_object_subdirectory"}
            with os.scandir(directory) as entries:
                children = sorted(list(entries), key=lambda entry: entry.name)
            for entry in children:
                visited += 1
                if visited > max_entries:
                    return {"kind": "unknown", "reason": "git_object_inventory_budget_exceeded"}
                relative = relative_dir / entry.name
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    return {"kind": "unknown", "reason": "git_object_entry_unavailable"}
                if stat.S_ISLNK(info.st_mode) or _is_reparse_point(info):
                    return {"kind": "unknown", "reason": "git_object_reparse_point"}
                if stat.S_ISDIR(info.st_mode):
                    kind = "directory"
                    pending.append(relative)
                elif stat.S_ISREG(info.st_mode):
                    kind = "file"
                    total_bytes += max(info.st_size, 0)
                    if total_bytes > max_bytes:
                        return {"kind": "unknown", "reason": "git_object_byte_budget_exceeded"}
                else:
                    return {"kind": "unknown", "reason": "unsupported_git_object_entry"}
                identity = _stat_identity(info)
                digest.update(os.fsencode(relative.as_posix()))
                digest.update(b"\0")
                digest.update(kind.encode("ascii"))
                digest.update(b"\0")
                digest.update(",".join(str(field) for field in identity).encode("ascii"))
                if kind == "file":
                    snapshot = _worktree_snapshot(root, relative.as_posix(), max_file_bytes=max_bytes)
                    if snapshot.get("kind") != "file":
                        return {"kind": "unknown", "reason": "git_object_content_unavailable"}
                    digest.update(b"\0")
                    digest.update(str(snapshot["sha256"]).encode("ascii"))
                digest.update(b"\n")
            after = directory.lstat()
            if _stat_identity(before) != _stat_identity(after):
                return {"kind": "unknown", "reason": "git_object_directory_changed"}
        except OSError:
            return {"kind": "unknown", "reason": "git_object_inventory_failed"}
    return {"kind": "inventory", "entries": visited, "sha256": digest.hexdigest()}


def _git_metadata_snapshots(repository: Path) -> tuple[str, str, dict[str, dict[str, object]]]:
    """Capture bounded Git configuration, hook, ref and worktree metadata."""

    git_dir = Path(run_git(["git", "rev-parse", "--absolute-git-dir"], cwd=repository).stdout.strip())
    common_raw = run_git(["git", "rev-parse", "--git-common-dir"], cwd=repository).stdout.strip()
    common_path = Path(common_raw)
    if not common_path.is_absolute():
        common_path = repository / common_path
    common_dir = Path(os.path.abspath(common_path))
    git_dir = Path(os.path.abspath(git_dir))
    roots: list[tuple[str, Path]] = [("git_dir", git_dir)]
    if os.path.normcase(str(common_dir)) != os.path.normcase(str(git_dir)):
        roots.append(("git_common_dir", common_dir))

    snapshots: dict[str, dict[str, object]] = {}
    hooks_config = run_command(
        ["git", "config", "--path", "--null", "--get-all", "core.hooksPath"],
        cwd=repository,
        check=False,
    )
    if hooks_config.returncode not in {0, 1}:
        raise RuntimeError("Could not safely inspect the configured Git hooks path.")
    configured_hooks_paths = sorted(
        {
            os.path.abspath(value if Path(value).is_absolute() else repository / value)
            for value in hooks_config.stdout.split("\0")
            if value
        }
    )
    snapshots["git_behavior/@hooks_paths"] = {
        "kind": "paths",
        "paths": configured_hooks_paths,
    }
    include_config = run_command(
        ["git", "config", "--no-includes", "--null", "--get-regexp", r"^include"],
        cwd=repository,
        check=False,
    )
    if include_config.returncode == 0:
        snapshots["git_behavior/@config_includes"] = {
            "kind": "unknown",
            "reason": "included_git_configuration_not_inventoried",
        }
    elif include_config.returncode != 1:
        snapshots["git_behavior/@config_includes"] = {
            "kind": "unknown",
            "reason": "git_configuration_include_inspection_failed",
        }

    extra_tree_roots: dict[str, set[str]] = {label: set() for label, _root in roots}
    for hook_path_text in configured_hooks_paths:
        hook_path = Path(hook_path_text)
        for label, root in roots:
            try:
                relative = hook_path.relative_to(root)
            except ValueError:
                continue
            if not relative.parts:
                snapshots[f"{label}/@hooks_path"] = {
                    "kind": "unknown",
                    "reason": "hooks_path_is_git_directory",
                }
                continue
            extra_tree_roots[label].add(relative.as_posix())

    for label, root in roots:
        try:
            root_info = root.lstat()
            safe_root = stat.S_ISDIR(root_info.st_mode) and not stat.S_ISLNK(root_info.st_mode) and not _is_reparse_point(root_info)
        except OSError:
            safe_root = False
        if not safe_root:
            snapshots[f"{label}/@root"] = {"kind": "unknown", "reason": "unsafe_git_directory"}
            continue
        snapshots[f"{label}/@root"] = {
            "kind": "directory",
            "identity": list(_directory_identity(root_info)),
        }
        try:
            with os.scandir(root) as entries:
                root_names = sorted(entry.name for entry in entries)
            shared_index_names = [name for name in root_names if name.startswith("sharedindex.")]
            snapshots[f"{label}/@entries"] = {
                "kind": "names",
                "names": root_names,
            }
        except OSError:
            snapshots[f"{label}/@entries"] = {
                "kind": "unknown",
                "reason": "git_directory_enumeration_failed",
            }
            snapshots[f"{label}/@sharedindex_names"] = {
                "kind": "unknown",
                "reason": "shared_index_enumeration_failed",
            }
            shared_index_names = []
        else:
            snapshots[f"{label}/@sharedindex_names"] = {
                "kind": "names",
                "names": shared_index_names,
            }
        for shared_index_name in shared_index_names:
            snapshots[f"{label}/{shared_index_name}"] = _worktree_snapshot(
                root,
                shared_index_name,
                max_file_bytes=64 * 1024 * 1024,
            )
        for relative in _GIT_METADATA_FILES:
            snapshots[f"{label}/{relative}"] = _worktree_snapshot(
                root,
                relative,
                max_file_bytes=64 * 1024 * 1024 if relative == "index" else 1024 * 1024,
            )
        for relative_root in sorted(set(_GIT_METADATA_TREES) | extra_tree_roots[label]):
            tree_key = f"{label}/{relative_root}"
            initial = _worktree_snapshot(root, relative_root)
            snapshots[tree_key] = initial
            if initial.get("kind") != "directory":
                continue
            pending = [(relative_root, 0)]
            visited = 0
            total_file_bytes = 0
            total_index_bytes = 0
            while pending:
                current, depth = pending.pop()
                if depth >= 8 or visited >= 4096:
                    snapshots[tree_key] = {"kind": "unknown", "reason": "metadata_tree_budget_exceeded"}
                    break
                directory = root / Path(*current.split("/"))
                try:
                    before = directory.lstat()
                    if stat.S_ISLNK(before.st_mode) or _is_reparse_point(before) or not stat.S_ISDIR(before.st_mode):
                        raise OSError("unsafe Git metadata directory")
                    with os.scandir(directory) as entries:
                        children = sorted(list(entries), key=lambda entry: entry.name)
                    after = directory.lstat()
                    if _stat_identity(before) != _stat_identity(after):
                        raise OSError("Git metadata directory changed")
                except OSError:
                    snapshots[tree_key] = {"kind": "unknown", "reason": "metadata_tree_unavailable"}
                    break
                for entry in children:
                    visited += 1
                    if visited > 4096:
                        snapshots[tree_key] = {"kind": "unknown", "reason": "metadata_tree_budget_exceeded"}
                        break
                    relative = f"{current}/{entry.name}"
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        info = None
                    if info is not None and stat.S_ISREG(info.st_mode):
                        if relative.endswith("/index"):
                            total_index_bytes += max(info.st_size, 0)
                            over_budget = info.st_size > 64 * 1024 * 1024 or total_index_bytes > 128 * 1024 * 1024
                        else:
                            total_file_bytes += max(info.st_size, 0)
                            over_budget = info.st_size > 1024 * 1024 or total_file_bytes > 16 * 1024 * 1024
                        if over_budget:
                            snapshots[tree_key] = {"kind": "unknown", "reason": "metadata_tree_byte_budget_exceeded"}
                            break
                    child = _worktree_snapshot(root, relative, max_file_bytes=1024 * 1024)
                    snapshots[f"{label}/{relative}"] = child
                    if child.get("kind") == "directory":
                        pending.append((relative, depth + 1))
                if snapshots[tree_key].get("kind") == "unknown":
                    break
        # Linked worktree gitdirs keep per-worktree state here, while Git's
        # object database lives in the shared common directory. Snapshot it
        # once at its actual location instead of marking every linked
        # worktree baseline incomplete for a deliberately absent objects/.
        if os.path.normcase(os.path.abspath(root)) == os.path.normcase(os.path.abspath(common_dir)):
            inventory_key = f"{label}/objects/@inventory"
            snapshots[inventory_key] = _git_object_inventory(root)
            if any(
                os.environ.get(name)
                for name in (
                    "GIT_OBJECT_DIRECTORY",
                    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                    "GIT_INDEX_FILE",
                )
            ):
                snapshots[inventory_key] = {
                    "kind": "unknown",
                    "reason": "git_object_or_index_environment_override",
                }
            alternates = snapshots.get(f"{label}/objects/info/alternates", {})
            if alternates.get("kind") != "missing":
                snapshots[inventory_key] = {
                    "kind": "unknown",
                    "reason": "external_git_alternate_object_store_not_inventoried",
                }
    return str(git_dir), str(common_dir), snapshots


def capture_git_baseline(repository: Path, *, include_ignored: bool = False) -> dict[str, object]:
    """Capture a hook/filter-safe, hash-only Git/worktree baseline."""

    repository = git_top_level(repository)
    status_args = ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"]
    if include_ignored:
        # matching lists files inside ignored directories as well, so a scan-only
        # turn cannot hide newly written source by adding an ignore rule.
        status_args.append("--ignored=matching")
    status = run_git(
        status_args,
        cwd=repository,
    ).stdout
    status_entries = _porcelain_entries(status)
    index_raw = run_git(["git", "ls-files", "--stage", "-z"], cwd=repository).stdout
    index = _index_entries(repository)
    tracked_paths = sorted(index)
    untracked_paths = sorted(entry["path"] for entry in status_entries if entry["index"] == "?" and entry["worktree"] == "?")
    ignored_paths = sorted(
        entry["path"]
        for entry in status_entries
        if entry["index"] == "!" and entry["worktree"] == "!"
    ) if include_ignored else []
    paths_to_hash = sorted(set(tracked_paths).union(untracked_paths))
    snapshots = {path: _worktree_snapshot(repository, path) for path in paths_to_hash}
    if include_ignored:
        snapshots.update(_ignored_tree_snapshots(repository, ignored_paths))
        # A scan-only Executor could write through a pre-existing repository
        # symlink to data that the repository snapshot does not cover. Fail
        # closed instead of treating link-text stability as target stability.
        for path, snapshot in tuple(snapshots.items()):
            if snapshot.get("kind") == "symlink":
                snapshots[path] = {"kind": "unknown", "reason": "symlink_target_unverified"}
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
    if include_ignored:
        git_dir, git_common_dir, git_metadata_snapshots = _git_metadata_snapshots(repository)
        if any(
            entry.get("mode") == "160000"
            for entries in index.values()
            for entry in entries
        ):
            git_metadata_snapshots["git_behavior/@submodules"] = {
                "kind": "unknown",
                "reason": "submodule_gitdirs_not_inventoried",
            }
        for path, snapshot in tuple(git_metadata_snapshots.items()):
            if snapshot.get("kind") == "symlink":
                git_metadata_snapshots[path] = {"kind": "unknown", "reason": "symlink_target_unverified"}
    else:
        git_dir = run_git(["git", "rev-parse", "--absolute-git-dir"], cwd=repository).stdout.strip()
        git_common_dir = run_git(["git", "rev-parse", "--git-common-dir"], cwd=repository).stdout.strip()
        git_metadata_snapshots = {}
    return {
        "schema_version": 1,
        "repository": str(repository),
        "git_dir": git_dir,
        "git_common_dir": git_common_dir,
        "head": head,
        "branch": branch.stdout.strip() if branch.returncode == 0 else "",
        "detached": branch.returncode == 1,
        "status_entries": status_entries,
        "staged_status": {entry["path"]: entry["index"] for entry in status_entries if entry["index"] not in {" ", "?"}},
        "unstaged_status": {entry["path"]: entry["worktree"] for entry in status_entries if entry["worktree"] not in {" ", "?"}},
        "untracked_paths": untracked_paths,
        "ignored_paths": ignored_paths,
        "include_ignored": include_ignored,
        "git_metadata_snapshots": git_metadata_snapshots,
        "index_fingerprint": hashlib.sha256(index_raw.encode("utf-8")).hexdigest(),
        "staged_index_entries": {path: index[path] for path in sorted(staged_paths) if path in index},
        "worktree_snapshots": snapshots,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "complete": (
            all(snapshot.get("kind") != "unknown" for snapshot in snapshots.values())
            and all(snapshot.get("kind") != "unknown" for snapshot in git_metadata_snapshots.values())
        ),
    }


def attribute_git_mutations(
    repository: Path,
    baseline: dict[str, object],
    *,
    excluded_paths: Iterable[str] = (),
    exact_excluded_paths: Iterable[str] = (),
    include_ignored: bool = False,
) -> dict[str, object]:
    """Compare final status and raw file hashes with a captured run baseline.

    ``excluded_paths`` names recursive control roots; ``exact_excluded_paths``
    exempts only the named files so descendants remain attributable.
    """

    repository = git_top_level(repository)
    if str(baseline.get("repository", "")) != str(repository):
        raise RuntimeError("Git baseline belongs to a different repository root.")
    if include_ignored and baseline.get("include_ignored") is not True:
        raise RuntimeError("Mutation attribution requires a baseline that included ignored files.")
    final = capture_git_baseline(repository, include_ignored=include_ignored)
    excluded = tuple(path.replace("\\", "/").strip("/") for path in excluded_paths if path)
    exact_excluded = tuple(path.replace("\\", "/").strip("/") for path in exact_excluded_paths if path)

    def is_excluded(path: str) -> bool:
        return path in exact_excluded or any(
            path == prefix or path.startswith(prefix + "/") for prefix in excluded
        )

    old_entries = {entry["path"]: entry for entry in baseline.get("status_entries", []) if isinstance(entry, dict)}
    new_entries = {entry["path"]: entry for entry in final.get("status_entries", []) if isinstance(entry, dict)}
    old_snapshots = baseline.get("worktree_snapshots", {})
    new_snapshots = final.get("worktree_snapshots", {})
    old_index = baseline.get("staged_index_entries", {})
    new_index = final.get("staged_index_entries", {})
    old_metadata = baseline.get("git_metadata_snapshots", {})
    new_metadata = final.get("git_metadata_snapshots", {})
    git_directory_changed = (
        baseline.get("git_dir") != final.get("git_dir")
        or baseline.get("git_common_dir") != final.get("git_common_dir")
    )
    metadata_changed = old_metadata != new_metadata or git_directory_changed
    metadata_unknown = any(
        isinstance(snapshot, dict) and snapshot.get("kind") == "unknown"
        for snapshot in (*old_metadata.values(), *new_metadata.values())
    ) if isinstance(old_metadata, dict) and isinstance(new_metadata, dict) else True
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
        "status": "complete" if baseline.get("complete") and final.get("complete") and not unknown and not metadata_unknown else "unknown",
        "repository": str(repository),
        "initial_head": old_head,
        "final_head": new_head,
        "head_changed": old_head != new_head,
        "initial_branch": baseline.get("branch", ""),
        "final_branch": final.get("branch", ""),
        "branch_changed": baseline.get("branch", "") != final.get("branch", "") or baseline.get("detached") != final.get("detached"),
        "staged_state_changed": baseline.get("index_fingerprint") != final.get("index_fingerprint"),
        "repository_metadata_changed": metadata_changed,
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
