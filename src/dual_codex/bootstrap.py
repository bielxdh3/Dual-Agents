from __future__ import annotations

"""Trusted bootstrap requirements for configured Dual Agents actors.

Machine-wide policy remains global-owned; project skills are snapshotted from
the explicitly selected repository. This module never accepts a model-supplied
replacement path.
"""

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
import tempfile
from typing import Iterable


CANONICAL_INSTRUCTIONS_ROOT = Path(r"C:\CodexGlobal")
_DEFAULT_SELECTED_SKILLS = (
    "memory",
    "ponytail",
    "project-phase-review",
    "project-security-review",
)
_MANDATORY_ARCHITECT_SKILLS = _DEFAULT_SELECTED_SKILLS


@dataclass(frozen=True)
class SkillSource:
    identifier: str
    scope: str
    source_path: Path
    relative_path: str
    sha256: str

    def metadata(self) -> dict[str, str]:
        return {
            "identifier": self.identifier,
            "source_scope": self.scope,
            "source_path": str(self.source_path),
            "repository_relative_path": self.relative_path if self.scope == "project" else "",
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class CanonicalBootstrap:
    """A per-run, read-only transport snapshot of the canonical policy."""

    source_root: Path
    source_sha256: str
    artifact_path: Path | None = None
    artifact_sha256: str = ""
    mechanism: str = "canonical-source"
    selected_skills: tuple[str, ...] = ()
    source_files: tuple[tuple[str, str], ...] = ()
    skill_catalog: tuple[tuple[str, str], ...] = ()
    skill_catalog_sha256: str = ""
    skill_sources: tuple[SkillSource, ...] = ()
    combined_skill_catalog_sha256: str = ""
    project_root: Path | None = None
    artifact_source_sha256: str = ""
    artifact_source_files: tuple[tuple[str, str], ...] = ()
    host_loaded_skills: tuple[str, ...] = ()
    actor_selected_skills: tuple[str, ...] = ()

    def metadata(self) -> dict[str, object]:
        artifact_source_files = dict(self.artifact_source_files)
        source_files = dict(self.source_files)
        if self.artifact_path is None:
            delivery = "source-reference"
        elif any(
            artifact_source_files.get(relative) != digest
            for relative, digest in source_files.items()
        ):
            delivery = "mixed"
        else:
            delivery = "trusted_inline"
        skill_digests = {
            relative[len("skills/") : -len("/SKILL.md")]: digest
            for relative, digest in self.source_files
            if relative.startswith("skills/") and relative.endswith("/SKILL.md")
        }
        host_digests = {name: skill_digests[name] for name in self.host_loaded_skills if name in skill_digests}
        source_by_name = {entry.identifier.casefold(): entry for entry in self.skill_sources}
        actor_sources = [
            source_by_name[name.casefold()].metadata()
            for name in self.actor_selected_skills
            if name.casefold() in source_by_name
        ]
        host_sources = [
            source_by_name[name.casefold()].metadata()
            for name in self.host_loaded_skills
            if name.casefold() in source_by_name
        ]
        actor_digests = {item["identifier"]: item["sha256"] for item in actor_sources}
        project_skill_catalog = {
            entry.identifier: entry.sha256 for entry in self.skill_sources if entry.scope == "project"
        }
        return {
            "canonical_bootstrap_required": True,
            "canonical_bootstrap_source": "machine-wide",
            "canonical_bootstrap_source_path": str(self.source_root),
            "canonical_bootstrap_source_sha256": self.source_sha256,
            "canonical_bootstrap_mechanism": self.mechanism,
            "canonical_bootstrap_artifact": str(self.artifact_path or ""),
            "canonical_bootstrap_artifact_sha256": self.artifact_sha256,
            "canonical_bootstrap_artifact_source_sha256": self.artifact_source_sha256,
            "canonical_bootstrap_artifact_ephemeral": self.artifact_path is not None,
            "canonical_bootstrap_delivery": delivery,
            "canonical_bootstrap_selected_skills": list(self.selected_skills),
            "canonical_bootstrap_skill_digests": skill_digests,
            "canonical_bootstrap_host_loaded_skills": list(self.host_loaded_skills),
            "canonical_bootstrap_host_loaded_skill_digests": host_digests,
            "canonical_bootstrap_actor_selected_skills": list(self.actor_selected_skills),
            "canonical_bootstrap_actor_selected_skill_digests": actor_digests,
            "canonical_bootstrap_skill_catalog": dict(self.skill_catalog),
            "canonical_bootstrap_skill_catalog_sha256": self.skill_catalog_sha256,
            "canonical_bootstrap_project_root": str(self.project_root or ""),
            "canonical_bootstrap_project_skill_catalog": project_skill_catalog,
            "canonical_bootstrap_skill_catalog_combined_sha256": self.combined_skill_catalog_sha256,
            "canonical_bootstrap_skill_catalog_sources": [entry.metadata() for entry in self.skill_sources],
            "canonical_bootstrap_host_loaded_skill_sources": host_sources,
            "canonical_bootstrap_actor_selected_skill_sources": actor_sources,
            "canonical_bootstrap_source_files": {
                relative: digest for relative, digest in self.source_files
            },
            "canonical_bootstrap_artifact_source_files": artifact_source_files,
        }


def canonical_instructions_root(root: Path | None = None) -> Path:
    """Return the canonical policy root, failing closed when it is incomplete."""

    resolved = (root or CANONICAL_INSTRUCTIONS_ROOT).expanduser()
    agents = resolved / "AGENTS.md"
    skills = resolved / "skills"
    if not agents.is_file():
        raise FileNotFoundError(f"Canonical instruction file is unavailable: {agents}")
    if not skills.is_dir():
        raise FileNotFoundError(f"Canonical skill tree is unavailable: {skills}")
    return resolved


def _normalise_skill_names(selected_skills: Iterable[str] | None) -> tuple[str, ...]:
    names = _DEFAULT_SELECTED_SKILLS if selected_skills is None else tuple(selected_skills)
    normalised: list[str] = []
    for name in names:
        value = str(name).strip().replace("\\", "/")
        if not value or value in {".", ".."} or value.startswith("/") or ".." in value.split("/") or "/" in value:
            raise ValueError(f"Invalid skill identifier: {name!r}")
        if value not in normalised:
            normalised.append(value)
    return tuple(sorted(normalised))


def _canonical_skill_lookup(catalog: Iterable[tuple[str, str]]) -> dict[str, str]:
    by_casefold: dict[str, str] = {}
    for name, _digest in catalog:
        normalized = _normalise_skill_names((name,))
        if normalized != (name,):
            raise ValueError(f"Invalid skill directory name: {name!r}")
        key = name.casefold()
        existing = by_casefold.get(key)
        if existing is not None and existing != name:
            raise ValueError(
                "Skill catalog contains names that collide after case folding: "
                f"'{existing}' and '{name}'."
            )
        by_casefold[key] = name
    return by_casefold


def _resolve_architect_skill_names(
    reported_names: Iterable[str],
    catalog: Iterable[SkillSource],
) -> tuple[str, ...]:
    lookup: dict[str, str] = {}
    for entry in catalog:
        key = entry.identifier.casefold()
        existing = lookup.get(key)
        if existing is not None:
            raise ValueError(
                "Allowed skill catalogs contain names that collide after case folding: "
                f"'{existing}' and '{entry.identifier}'."
            )
        lookup[key] = entry.identifier
    resolved: list[str] = []
    seen: set[str] = set()
    for raw_name in reported_names:
        normalized = _normalise_skill_names((raw_name,))[0]
        canonical = lookup.get(normalized.casefold())
        if canonical is None:
            raise FileNotFoundError(
                "Required skill was not present in the pre-dispatch catalog snapshot: "
                + normalized
            )
        folded = canonical.casefold()
        if folded in seen:
            raise ValueError("Architect plan 'skills_loaded' contains duplicate skill names after case folding.")
        seen.add(folded)
        resolved.append(canonical)
    return tuple(sorted(resolved))


def select_required_skills(role: str, task: str = "") -> tuple[str, ...]:
    """Return skills the control plane can safely preselect for a phase.

    Architect task-specific skill selection is deferred to the actor because
    the applicable set depends on the task brief. Its mandatory baseline and
    other configured phases retain their bounded transport set.
    """

    if role == "architect":
        return _normalise_skill_names(_MANDATORY_ARCHITECT_SKILLS)
    selected = {"memory", "ponytail"}
    if role == "reviewer":
        selected.update({"project-phase-review", "project-security-review"})
    return _normalise_skill_names(selected)


def _canonical_files(
    root: Path,
    *,
    selected_skills: Iterable[str] | None = None,
) -> list[tuple[str, bytes]]:
    files = [("AGENTS.md", (root / "AGENTS.md").read_bytes())]
    for name in _normalise_skill_names(selected_skills):
        path = root / "skills" / name / "SKILL.md"
        content = _read_skill_file(path, root / "skills", name, "global")
        if content is None:
            raise FileNotFoundError(f"Required canonical skill is unavailable: {path}")
        files.append((path.relative_to(root).as_posix(), content))
    return files


def _is_reparse_point(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & reparse_attribute)


def _read_skill_file(path: Path, skill_root: Path, name: str, scope: str) -> bytes | None:
    """Read a direct regular skill file without following links or junctions."""

    skill_dir = path.parent
    if _is_reparse_point(skill_root) or _is_reparse_point(skill_dir) or _is_reparse_point(path):
        raise ValueError(f"{scope.title()} skill '{name}' uses a symlink or reparse point.")
    try:
        resolved_root = skill_root.resolve(strict=True)
        resolved_dir = skill_dir.resolve(strict=True)
        resolved_file = path.resolve(strict=True)
        info = path.lstat()
    except OSError:
        return None
    if resolved_dir.parent != resolved_root or resolved_file.parent != resolved_dir or not stat.S_ISREG(info.st_mode):
        raise ValueError(f"{scope.title()} skill '{name}' does not resolve under its expected skill root.")
    try:
        return path.read_bytes()
    except OSError:
        return None


def _canonical_skill_catalog(root: Path) -> tuple[tuple[str, str], ...]:
    """Snapshot direct skill names and hashes without selecting or delivering them."""

    entries: list[tuple[str, str]] = []
    skill_root = root / "skills"
    if _is_reparse_point(skill_root):
        raise ValueError("Canonical skill root uses a symlink or reparse point.")
    for directory in sorted(skill_root.iterdir(), key=lambda item: item.name.casefold()):
        if _is_reparse_point(directory):
            raise ValueError(f"Canonical skill directory '{directory.name}' uses a symlink or reparse point.")
        if not directory.is_dir():
            continue
        path = directory / "SKILL.md"
        name = path.parent.name
        normalized = _normalise_skill_names((name,))
        if normalized != (name,):
            raise ValueError(f"Invalid skill directory name: {name!r}")
        content = _read_skill_file(path, skill_root, name, "global")
        if content is None:
            continue
        entries.append((name, hashlib.sha256(content).hexdigest()))
    catalog = tuple(entries)
    _canonical_skill_lookup(catalog)
    return catalog


def _project_skill_catalog(repository: Path | None) -> tuple[SkillSource, ...]:
    if repository is None:
        return ()
    root = repository.expanduser().resolve(strict=True)
    agents_dir = root / ".agents"
    if not os.path.lexists(agents_dir):
        return ()
    if _is_reparse_point(agents_dir) or not agents_dir.is_dir():
        raise ValueError("Project .agents directory is not a safe directory under the target repository.")
    skill_root = agents_dir / "skills"
    if not os.path.lexists(skill_root):
        return ()
    if _is_reparse_point(skill_root) or not skill_root.is_dir():
        raise ValueError("Project skill root is not a safe directory under the target repository.")
    entries: list[SkillSource] = []
    for directory in sorted(skill_root.iterdir(), key=lambda item: item.name.casefold()):
        if _is_reparse_point(directory):
            raise ValueError(f"Project skill entry '{directory.name}' uses a symlink or reparse point.")
        if not directory.is_dir():
            continue
        name = directory.name
        if _normalise_skill_names((name,)) != (name,):
            raise ValueError(f"Invalid project skill directory name: {name!r}")
        path = directory / "SKILL.md"
        content = _read_skill_file(path, skill_root, name, "project")
        if content is None:
            raise ValueError(f"Malformed project skill '{name}': SKILL.md is missing or unreadable.")
        entries.append(
            SkillSource(
                identifier=name,
                scope="project",
                source_path=path,
                relative_path=f".agents/skills/{name}/SKILL.md",
                sha256=hashlib.sha256(content).hexdigest(),
            )
        )
    return tuple(entries)


def _catalog_sources(root: Path, catalog: Iterable[tuple[str, str]], project: Iterable[SkillSource]) -> tuple[SkillSource, ...]:
    global_sources = tuple(
        SkillSource(
            identifier=name,
            scope="global",
            source_path=root / "skills" / name / "SKILL.md",
            relative_path=f"skills/{name}/SKILL.md",
            sha256=digest,
        )
        for name, digest in catalog
    )
    sources = (*global_sources, *tuple(project))
    _resolve_architect_skill_names((entry.identifier for entry in sources), sources)
    return tuple(sorted(sources, key=lambda entry: (entry.scope, entry.identifier.casefold(), entry.identifier)))


def _combined_skill_catalog_sha256(sources: Iterable[SkillSource]) -> str:
    digest = hashlib.sha256()
    for entry in sources:
        digest.update(entry.scope.encode("utf-8"))
        digest.update(b"\0")
        digest.update(entry.identifier.encode("utf-8"))
        digest.update(b"\0")
        digest.update(entry.relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(entry.sha256))
    return digest.hexdigest()


def _read_catalog_source(entry: SkillSource, *, source_root: Path, project_root: Path | None) -> bytes:
    if entry.scope == "global":
        expected_root = source_root / "skills"
        expected_path = expected_root / entry.identifier / "SKILL.md"
    elif entry.scope == "project" and project_root is not None:
        agents_dir = project_root / ".agents"
        expected_root = agents_dir / "skills"
        expected_path = expected_root / entry.identifier / "SKILL.md"
        if _is_reparse_point(agents_dir) or _is_reparse_point(expected_root):
            raise RuntimeError(f"Project skill '{entry.identifier}' no longer resolves under its expected skill root.")
    else:
        raise RuntimeError(f"Skill '{entry.identifier}' has an invalid source scope.")
    if entry.source_path != expected_path or entry.relative_path != (
        f"skills/{entry.identifier}/SKILL.md" if entry.scope == "global" else f".agents/skills/{entry.identifier}/SKILL.md"
    ):
        raise RuntimeError(f"Skill '{entry.identifier}' no longer resolves under its expected skill root.")
    try:
        content = _read_skill_file(expected_path, expected_root, entry.identifier, entry.scope)
    except ValueError as exc:
        if entry.scope == "project":
            raise RuntimeError(f"Project skill '{entry.identifier}' no longer resolves under its expected skill root.") from exc
        raise RuntimeError(f"Canonical skill '{entry.identifier}' no longer resolves under its expected skill root.") from exc
    if content is None:
        raise FileNotFoundError(f"{entry.scope.title()} skill '{entry.identifier}' disappeared after dispatch.")
    if hashlib.sha256(content).hexdigest() != entry.sha256:
        scope = "Project" if entry.scope == "project" else "Canonical"
        raise RuntimeError(f"{scope} skill '{entry.identifier}' changed while the Architect mission was running.")
    return content


def _skill_catalog_sha256(catalog: Iterable[tuple[str, str]]) -> str:
    digest = hashlib.sha256()
    for name, file_digest in catalog:
        digest.update(f"skills/{name}/SKILL.md".encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(file_digest))
    return digest.hexdigest()


def _source_sha256(files: list[tuple[str, bytes]]) -> str:
    digest = hashlib.sha256()
    for relative, content in files:
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(content).digest())
    return digest.hexdigest()


def _artifact_text(root: Path, role: str, files: list[tuple[str, bytes]], source_sha256: str) -> str:
    lines = [
        "# Dual Codex canonical bootstrap transport",
        "",
        "This is an ephemeral, read-only transport snapshot. The canonical source remains machine-owned.",
        f"canonical_source_path: {root}",
        f"canonical_source_sha256: {source_sha256}",
        f"trusted_configured_phase_role: {role}",
        "trusted_role_dispatch_boundary: already inside this configured phase; follow its task and schema without recursively dispatching",
        "",
    ]
    for relative, content in files:
        lines.extend(
            [
                f"## {relative}",
                f"sha256: {hashlib.sha256(content).hexdigest()}",
                "",
                content.decode("utf-8"),
                "",
            ]
        )
    return "\n".join(lines)


def create_canonical_bootstrap(
    *,
    role: str,
    artifact_dir: Path | None = None,
    root: Path | None = None,
    repository: Path | None = None,
    selected_skills: Iterable[str] | None = None,
) -> CanonicalBootstrap:
    """Validate canonical policy and optionally materialize one ephemeral snapshot."""

    source_root = canonical_instructions_root(root)
    if role == "architect":
        requested_skills = () if selected_skills is None else tuple(selected_skills)
        selected = _normalise_skill_names((*_MANDATORY_ARCHITECT_SKILLS, *requested_skills))
    else:
        selected = _normalise_skill_names(selected_skills)
    skill_catalog = _canonical_skill_catalog(source_root) if role == "architect" else ()
    project_root = repository.expanduser().resolve(strict=True) if role == "architect" and repository is not None else None
    project_catalog = _project_skill_catalog(project_root) if role == "architect" else ()
    skill_sources = _catalog_sources(source_root, skill_catalog, project_catalog) if role == "architect" else ()
    skill_catalog_sha256 = _skill_catalog_sha256(skill_catalog)
    combined_skill_catalog_sha256 = _combined_skill_catalog_sha256(skill_sources)
    files = _canonical_files(source_root, selected_skills=selected)
    source_sha256 = _source_sha256(files)
    source_files = tuple(
        (relative, hashlib.sha256(content).hexdigest()) for relative, content in files
    )
    if artifact_dir is None:
        return CanonicalBootstrap(
            source_root=source_root,
            source_sha256=source_sha256,
            selected_skills=selected,
            source_files=source_files,
            skill_catalog=skill_catalog,
            skill_catalog_sha256=skill_catalog_sha256,
            skill_sources=skill_sources,
            combined_skill_catalog_sha256=combined_skill_catalog_sha256,
            project_root=project_root,
            host_loaded_skills=selected,
        )
    destination = artifact_dir.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    fd, raw_path = tempfile.mkstemp(prefix=f".canonical-bootstrap-{role}-", suffix=".md", dir=destination)
    path = Path(raw_path)
    try:
        content = _artifact_text(source_root, role, files, source_sha256).encode("utf-8")
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        return CanonicalBootstrap(
            source_root=source_root,
            source_sha256=source_sha256,
            artifact_path=path,
            artifact_sha256=hashlib.sha256(content).hexdigest(),
            mechanism="ephemeral-run-artifact",
            selected_skills=selected,
            source_files=source_files,
            skill_catalog=skill_catalog,
            skill_catalog_sha256=skill_catalog_sha256,
            skill_sources=skill_sources,
            combined_skill_catalog_sha256=combined_skill_catalog_sha256,
            project_root=project_root,
            artifact_source_sha256=source_sha256,
            artifact_source_files=source_files,
            host_loaded_skills=selected,
        )
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        path.unlink(missing_ok=True)
        raise


def finalize_architect_bootstrap(
    bootstrap: CanonicalBootstrap,
    selected_skills: object,
) -> CanonicalBootstrap:
    """Verify the Architect's reported identifiers against the pre-dispatch catalogs."""

    if not isinstance(selected_skills, list) or any(
        not isinstance(name, str) for name in selected_skills
    ):
        raise ValueError("Architect plan must list actor-selected skill identifiers in 'skills_loaded'.")
    source_by_name = {entry.identifier.casefold(): entry for entry in bootstrap.skill_sources}
    baseline = _resolve_architect_skill_names(
        bootstrap.host_loaded_skills or _MANDATORY_ARCHITECT_SKILLS,
        bootstrap.skill_sources,
    )
    reported = _resolve_architect_skill_names(selected_skills, bootstrap.skill_sources)
    baseline_names = {name.casefold() for name in baseline}
    actor_selected = tuple(name for name in reported if name.casefold() not in baseline_names)
    names = tuple(sorted((*baseline, *actor_selected)))
    selected_sources = [source_by_name[name.casefold()] for name in names]
    files = [("AGENTS.md", (bootstrap.source_root / "AGENTS.md").read_bytes())]
    for entry in selected_sources:
        content = _read_catalog_source(
            entry,
            source_root=bootstrap.source_root,
            project_root=bootstrap.project_root,
        )
        if entry.scope == "global":
            files.append((entry.relative_path, content))
    digests = tuple(
        (relative, hashlib.sha256(content).hexdigest()) for relative, content in files
    )
    initial_agents_digest = dict(bootstrap.source_files).get("AGENTS.md")
    current_agents_digest = dict(digests).get("AGENTS.md")
    if initial_agents_digest and current_agents_digest != initial_agents_digest:
        raise RuntimeError("Canonical AGENTS.md changed while the Architect mission was running.")
    return CanonicalBootstrap(
        source_root=bootstrap.source_root,
        source_sha256=_source_sha256(files),
        artifact_path=bootstrap.artifact_path,
        artifact_sha256=bootstrap.artifact_sha256,
        mechanism=bootstrap.mechanism,
        selected_skills=names,
        source_files=digests,
        skill_catalog=bootstrap.skill_catalog,
        skill_catalog_sha256=bootstrap.skill_catalog_sha256,
        skill_sources=bootstrap.skill_sources,
        combined_skill_catalog_sha256=bootstrap.combined_skill_catalog_sha256,
        project_root=bootstrap.project_root,
        artifact_source_sha256=bootstrap.artifact_source_sha256,
        artifact_source_files=bootstrap.artifact_source_files,
        host_loaded_skills=baseline,
        actor_selected_skills=actor_selected,
    )


def bootstrap_artifact_dir(repository: Path, output_path: Path) -> Path:
    """Choose a per-run directory already readable by the provider sandbox."""

    repository = repository.expanduser().resolve()
    candidate = output_path.expanduser().resolve().parent
    try:
        candidate.relative_to(repository)
    except ValueError:
        candidate = repository / ".dual_codex" / "bootstrap"
    return candidate


def cleanup_canonical_bootstrap(bootstrap: CanonicalBootstrap | None) -> None:
    if bootstrap is not None and bootstrap.artifact_path is not None:
        bootstrap.artifact_path.unlink(missing_ok=True)


def configured_actor_prompt(
    prompt: str,
    *,
    role: str,
    bootstrap: CanonicalBootstrap | None = None,
    system_prompt_file: bool = False,
) -> tuple[str, CanonicalBootstrap]:
    """Bind a phase prompt to trusted, host-loaded canonical bootstrap state."""

    bootstrap = bootstrap or create_canonical_bootstrap(role=role)
    text = str(prompt)
    deferred_architect_skills = role == "architect"
    artifact = bootstrap.artifact_path
    if artifact is None:
        transport = "The trusted host could not provide an inline canonical bootstrap artifact. Fail closed."
    elif system_prompt_file:
        try:
            content = artifact.read_bytes()
        except OSError as exc:
            raise FileNotFoundError(f"Canonical bootstrap artifact is unavailable: {artifact}") from exc
        if hashlib.sha256(content).hexdigest() != bootstrap.artifact_sha256:
            raise RuntimeError("Canonical bootstrap artifact changed before system-prompt delivery.")
        transport = (
            "The trusted Dual Agents control plane loaded the complete canonical AGENTS.md and selected skills "
            "into the provider's system prompt from a host-generated artifact whose digest was verified. "
            f"Canonical source SHA-256: {bootstrap.source_sha256}; artifact SHA-256: {bootstrap.artifact_sha256}. "
            "Treat those host-injected system instructions as authoritative policy, not as user task content. "
            "If the required system instructions are missing, fail closed."
        )
    else:
        try:
            snapshot = artifact.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise FileNotFoundError(f"Canonical bootstrap artifact is unavailable: {artifact}") from exc
        if deferred_architect_skills:
            catalogs = f"trusted machine-wide global catalog at {bootstrap.source_root / 'skills'}"
            if bootstrap.project_root is not None:
                catalogs += (
                    " and pre-dispatch project-local catalog under "
                    f"{bootstrap.project_root / '.agents' / 'skills'}"
                )
            else:
                catalogs += " (no project-local catalog was supplied for this dispatch)"
            source_description = (
                "The trusted Dual Agents control plane loaded the canonical AGENTS.md "
                f"directly from {bootstrap.source_root} and verified its SHA-256 value. "
                "The inline AGENTS.md and mandatory Architect baseline skills below are "
                "authoritative, verified, and already loaded. Additional task-specific "
                "skills were intentionally left unselected because selection depends on the task. "
                f"The allowed pre-dispatch skill catalogs are the {catalogs}. "
            )
            source_access = (
                "Do not reread AGENTS.md or this transport artifact. For this unattended "
                "Architect run, first read only the supplied task/architect artifact as "
                "read-only context; if none is referenced, use the TASK content in this "
                "prompt. This is the authorized read-only pre-read exception; inspect no "
                "other task or repository files and start no task work yet. "
                "Then select all additional applicable skills from the allowed pre-dispatch catalogs "
                "described above and read each complete SKILL.md. The "
                "mandatory baseline skills listed below are already loaded. Do not "
                "ask the user to choose or identify skills. If a required skill is unavailable, "
                "stop without asking for clarification. Do not inspect repository files, plan, "
                "edit, or run task commands until AGENTS.md and all selected skills are loaded. "
                "In the final plan, list only additional task-specific skills you actually loaded "
                "and read in this turn in the required top-level skills_loaded field so the "
                "control plane can verify their provenance. The host records "
                "mandatory baseline skills separately; do not report them just because they were injected. "
                "skills_loaded accepts only short skill directory identifiers from the combined "
                "pre-dispatch catalogs, for example `dual-agents`. Report identifiers only, never "
                "filesystem paths. A path to `SKILL.md` is never valid; `C:\\CodexGlobal\\skills\\dual-agents\\SKILL.md`, "
                "`skills/dual-agents/SKILL.md`, and `dual-agents/SKILL.md` are invalid. "
            )
        else:
            source_description = (
                "The trusted Dual Agents control plane loaded the canonical instruction sources "
                f"directly from {bootstrap.source_root} and verified their SHA-256 values. "
                "The complete inline block below is the authoritative canonical bootstrap. "
            )
            source_access = (
                "Do not issue filesystem or shell commands to re-read or rediscover the source "
                "paths or this transport artifact. Apply the injected content exactly. "
            )
        transport = (
            f"{source_description}{source_access}If the block is missing or invalid, fail closed. "
            "Begin inline bootstrap:\n"
            f"--- BEGIN CANONICAL BOOTSTRAP SNAPSHOT ---\n{snapshot}\n"
            "--- END CANONICAL BOOTSTRAP SNAPSHOT ---"
        )
    if deferred_architect_skills:
        skill_status = (
            f"Mandatory Architect baseline skills already loaded: {', '.join(bootstrap.selected_skills)}. "
            "Select and load any additional task-specific skills yourself after reading the task context."
        )
    else:
        skill_status = (
            f"Selected canonical skills: {', '.join(bootstrap.selected_skills) or '(none)'}. "
            "Treat the injected AGENTS.md and selected SKILL.md content as already loaded "
            "canonical policy; do not attempt any bootstrap filesystem access."
        )
    prefix = (
        f"{transport}\n"
        f"Canonical source path: {bootstrap.source_root}; source_sha256={bootstrap.source_sha256}. "
        f"{skill_status}\n"
        f"Trusted configured phase role: {role}\n"
        "This turn is already inside that role's control-plane dispatch. Follow its bounded task and exact output schema; do not invoke the global Dual Agents entrypoint recursively. The natural mission router is for the outer user-facing turn before dispatch.\n\n"
    )
    return prefix + text, bootstrap
