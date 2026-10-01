from __future__ import annotations

"""Run-scoped, exact-repository trust for configured Codex profiles."""

import json
import os
from pathlib import Path
import stat
import tempfile
import tomllib

from .git import git_top_level
from .paths import same_path
from .process import CommandError


class RepositoryTrustError(CommandError):
    """Raised when safe project trust cannot be provisioned."""


def _is_reparse_point(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & attribute)


def _project_key_is_absolute(value: str) -> bool:
    try:
        return Path(value).is_absolute()
    except (OSError, ValueError):
        return False


def _validate_trust_target(path: Path) -> None:
    if path == Path(path.anchor):
        raise RepositoryTrustError("Cannot automatically trust a drive or filesystem root.")
    try:
        user_profile = Path.home().resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise RepositoryTrustError("Cannot verify whether the target is the user profile directory.") from exc
    if same_path(path, user_profile):
        raise RepositoryTrustError("Cannot automatically trust the user profile directory.")


def _parse_table_header(line: str) -> tuple[tuple[str, ...], bool] | None:
    candidate = line.strip()
    if not candidate.startswith("["):
        return None
    array_table = candidate.startswith("[[")
    try:
        value = tomllib.loads(line)
    except tomllib.TOMLDecodeError:
        return None
    path: list[str] = []
    node: object = value
    while isinstance(node, dict) and len(node) == 1:
        name, child = next(iter(node.items()))
        path.append(name)
        node = child
        if node == {}:
            break
        if array_table and isinstance(node, list):
            break
    return (tuple(path), array_table) if path else None


def _multiline_quote_state(line: str, state: str) -> str:
    index = 0
    quote = state
    while index < len(line):
        if quote:
            if quote in {'"""', "'''"}:
                if quote == '"""' and line[index] == "\\":
                    index += 2
                    continue
                if line.startswith(quote, index):
                    index += 3
                    quote = ""
                    continue
                index += 1
                continue
            if quote == '"' and line[index] == "\\":
                index += 2
                continue
            if line[index] == quote:
                quote = ""
            index += 1
            continue
        if line[index] == "#":
            break
        if line.startswith('"""', index):
            quote = '"""'
            index += 3
        elif line.startswith("'''", index):
            quote = "'''"
            index += 3
        elif line[index] in {'"', "'"}:
            quote = line[index]
            index += 1
        else:
            index += 1
    return quote if quote in {'"""', "'''"} else ""


def _table_headers(text: str) -> list[tuple[int, tuple[str, ...], bool]]:
    result: list[tuple[int, tuple[str, ...], bool]] = []
    multiline = ""
    for index, line in enumerate(text.splitlines()):
        if not multiline:
            header = _parse_table_header(line)
            if header is not None:
                path, array_table = header
                result.append((index, path, array_table))
        multiline = _multiline_quote_state(line, multiline)
    return result


def _quoted_toml_key(value: str) -> str:
    if any(character in value for character in "\r\n\x00"):
        raise RepositoryTrustError("The resolved repository path cannot be represented as a TOML project key.")
    if "'" not in value:
        return f"'{value}'"
    return json.dumps(value, ensure_ascii=False)


def _section_insert(text: str, index: int, content: str, newline: str) -> str:
    lines = text.splitlines(keepends=True)
    if index > 0 and not lines[index - 1].endswith(("\n", "\r")):
        lines[index - 1] += newline
    return "".join((*lines[:index], content, *lines[index:]))


def _edit_project_trust(text: str, repository: Path, projects: dict[str, object]) -> str:
    matches: list[tuple[str, dict[str, object]]] = []
    for key, value in projects.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            raise RepositoryTrustError("Codex project trust configuration is malformed.")
        if "trust_level" in value and value["trust_level"] not in {"trusted", "untrusted"}:
            raise RepositoryTrustError(f"Codex project trust entry '{key}' has an invalid trust_level.")
        if not _project_key_is_absolute(key):
            raise RepositoryTrustError("Codex project trust configuration contains a non-absolute project path.")
        try:
            if same_path(key, repository):
                matches.append((key, value))
        except (OSError, ValueError):
            raise RepositoryTrustError("Codex project trust configuration contains an invalid project path.")
    if len(matches) > 1:
        raise RepositoryTrustError("Codex project trust configuration has ambiguous entries for the target repository.")

    if matches:
        key, value = matches[0]
        trust = value.get("trust_level")
        if trust == "trusted":
            return text
        if trust == "untrusted":
            raise RepositoryTrustError(
                f"Codex profile explicitly marks '{repository}' untrusted. Dual Agents will not override it; "
                "review that actor profile's config.toml trust entry before retrying."
            )

    newline = "\r\n" if "\r\n" in text else "\n"
    headers = _table_headers(text)
    quoted = _quoted_toml_key(str(repository))
    project_path = ("projects", matches[0][0]) if matches else ()
    if matches:
        exact_headers = [
            item for item in headers if item[1] == project_path and not item[2]
        ]
        if exact_headers:
            if len(exact_headers) != 1:
                raise RepositoryTrustError("Codex project trust configuration has duplicate target tables.")
            start_index = exact_headers[0][0]
            end_index = next(
                (item[0] for item in headers if item[0] > start_index),
                len(text.splitlines()),
            )
            return _section_insert(text, end_index, f'trust_level = "trusted"{newline}', newline)
        descendants = [
            item for item in headers if len(item[1]) > 2 and item[1][:2] == project_path
        ]
        if descendants:
            line_index = min(item[0] for item in descendants)
            section = f"[projects.{_quoted_toml_key(matches[0][0])}]{newline}trust_level = \"trusted\"{newline}"
            return _section_insert(text, line_index, section, newline)
        raise RepositoryTrustError(
            "Codex target project entry uses a nonstandard inline or dotted TOML form; "
            "convert it to a [projects.<path>] table before retrying."
        )

    addition = f"[projects.{quoted}]{newline}trust_level = \"trusted\"{newline}"
    if not text:
        return addition
    if not text.endswith(("\n", "\r")):
        text += newline
    if not text.endswith(newline + newline):
        text += newline
    return text + addition


def provision_repository_trust(codex_home: Path, repository: Path) -> bool:
    """Trust exactly one Git worktree in the explicitly configured Codex profile.

    Returns True when config.toml changed and False when the target was already trusted.
    """

    try:
        target = Path(repository).expanduser().resolve(strict=True)
        _validate_trust_target(target)
        root = git_top_level(target)
    except (OSError, RuntimeError, ValueError) as exc:
        raise RepositoryTrustError(f"Cannot provision Codex trust: target is not a valid Git repository: {repository}") from exc
    if not same_path(root, target):
        raise RepositoryTrustError(
            f"Cannot provision Codex trust: '{target}' is not the Git top-level '{root}'."
        )
    _validate_trust_target(root)

    try:
        profile = Path(codex_home).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise RepositoryTrustError("Cannot provision Codex trust: configured actor CODEX_HOME is unavailable.") from exc
    if not profile.is_dir():
        raise RepositoryTrustError("Cannot provision Codex trust: configured actor CODEX_HOME is not a directory.")
    config_path = profile / "config.toml"
    if _is_reparse_point(config_path):
        raise RepositoryTrustError("Cannot provision Codex trust: actor config.toml is a symlink or reparse point.")
    if config_path.exists() and not config_path.is_file():
        raise RepositoryTrustError("Cannot provision Codex trust: actor config.toml is not a regular file.")
    try:
        original_bytes = config_path.read_bytes() if config_path.exists() else b""
        has_bom = original_bytes.startswith(b"\xef\xbb\xbf")
        original_text = original_bytes.decode("utf-8-sig")
        config = tomllib.loads(original_text) if original_text.strip() else {}
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise RepositoryTrustError(f"Cannot provision Codex trust: actor config.toml is malformed: {config_path}") from exc

    projects = config.get("projects", {})
    if not isinstance(projects, dict):
        raise RepositoryTrustError("Cannot provision Codex trust: the [projects] configuration is malformed.")
    updated_text = _edit_project_trust(original_text, root, projects)
    if updated_text == original_text:
        return False
    try:
        updated_config = tomllib.loads(updated_text)
    except tomllib.TOMLDecodeError as exc:
        raise RepositoryTrustError("Cannot provision Codex trust: safe TOML update could not be verified.") from exc
    updated_projects = updated_config.get("projects", {})
    trusted = [
        value
        for key, value in updated_projects.items()
        if isinstance(value, dict) and _project_key_is_absolute(key) and same_path(key, root)
    ]
    if len(trusted) != 1 or trusted[0].get("trust_level") != "trusted":
        raise RepositoryTrustError("Cannot provision Codex trust: updated project entry did not verify as trusted.")

    encoded = ("\ufeff" if has_bom else "") + updated_text
    output_bytes = encoded.encode("utf-8")
    mode = stat.S_IMODE(config_path.stat().st_mode) if config_path.exists() else 0o600
    fd, temporary_name = tempfile.mkstemp(prefix=".config.toml-", suffix=".tmp", dir=profile)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(output_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, mode)
        if _is_reparse_point(config_path):
            raise RepositoryTrustError("Cannot provision Codex trust: actor config.toml changed to a reparse point.")
        current_bytes = config_path.read_bytes() if config_path.exists() else b""
        if current_bytes != original_bytes:
            raise RepositoryTrustError("Cannot provision Codex trust: actor config.toml changed during the update.")
        os.replace(temporary_path, config_path)
    except (OSError, RepositoryTrustError) as exc:
        temporary_path.unlink(missing_ok=True)
        if isinstance(exc, RepositoryTrustError):
            raise
        raise RepositoryTrustError(f"Cannot atomically update actor config.toml: {config_path}") from exc
    return True
