from __future__ import annotations

"""Trusted bootstrap requirements for configured Dual Agents actors.

The files referenced here remain owned by the machine-wide Codex policy.  This
module only validates their presence and transports the requirement to a real
provider; it never copies policy files into a profile or accepts a model
supplied replacement path.
"""

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import tempfile
from typing import Iterable


CANONICAL_INSTRUCTIONS_ROOT = Path(r"C:\CodexGlobal")
BOOTSTRAP_MARKER = "[DUAL_CODEX_CANONICAL_BOOTSTRAP]"
_DEFAULT_SELECTED_SKILLS = (
    "memory",
    "ponytail",
    "project-phase-review",
    "project-security-review",
)


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
    artifact_source_sha256: str = ""
    artifact_source_files: tuple[tuple[str, str], ...] = ()

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
            "canonical_bootstrap_skill_catalog": dict(self.skill_catalog),
            "canonical_bootstrap_skill_catalog_sha256": self.skill_catalog_sha256,
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
        if not value or value.startswith("/") or ".." in value.split("/") or "/" in value:
            raise ValueError(f"Invalid canonical skill name: {name!r}")
        if value not in normalised:
            normalised.append(value)
    return tuple(sorted(normalised))


def select_required_skills(role: str, task: str = "") -> tuple[str, ...]:
    """Return skills the control plane can safely preselect for a phase.

    Architect skill selection is deferred to the actor because the applicable
    set depends on the task brief. Other configured phases retain their bounded
    transport set.
    """

    if role == "architect":
        return ()
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
        if not path.is_file():
            raise FileNotFoundError(f"Required canonical skill is unavailable: {path}")
        files.append((path.relative_to(root).as_posix(), path.read_bytes()))
    return files


def _canonical_skill_catalog(root: Path) -> tuple[tuple[str, str], ...]:
    """Snapshot direct skill names and hashes without selecting or delivering them."""

    entries: list[tuple[str, str]] = []
    for path in sorted((root / "skills").glob("*/SKILL.md")):
        name = path.parent.name
        normalized = _normalise_skill_names((name,))
        if normalized != (name,):
            raise ValueError(f"Invalid canonical skill directory name: {name!r}")
        entries.append((name, hashlib.sha256(path.read_bytes()).hexdigest()))
    return tuple(entries)


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
    selected_skills: Iterable[str] | None = None,
) -> CanonicalBootstrap:
    """Validate canonical policy and optionally materialize one ephemeral snapshot."""

    source_root = canonical_instructions_root(root)
    selected = _normalise_skill_names(
        () if selected_skills is None and role == "architect" else selected_skills
    )
    skill_catalog = _canonical_skill_catalog(source_root) if role == "architect" else ()
    skill_catalog_sha256 = _skill_catalog_sha256(skill_catalog)
    files = _canonical_files(source_root, selected_skills=selected)
    source_sha256 = _source_sha256(files)
    source_files = tuple(
        (relative, hashlib.sha256(content).hexdigest()) for relative, content in files
    )
    if artifact_dir is None:
        return CanonicalBootstrap(
            source_root,
            source_sha256,
            selected_skills=selected,
            source_files=source_files,
            skill_catalog=skill_catalog,
            skill_catalog_sha256=skill_catalog_sha256,
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
            source_root,
            source_sha256,
            path,
            hashlib.sha256(content).hexdigest(),
            "ephemeral-run-artifact",
            selected,
            source_files,
            skill_catalog,
            skill_catalog_sha256,
            source_sha256,
            source_files,
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
    """Verify the Architect's reported skill set against canonical files."""

    if not isinstance(selected_skills, list) or not selected_skills or any(
        not isinstance(name, str) for name in selected_skills
    ):
        raise ValueError("Architect plan must list the loaded canonical skills in 'skills_loaded'.")
    names = _normalise_skill_names(selected_skills)
    if len(names) != len(selected_skills):
        raise ValueError("Architect plan 'skills_loaded' must not contain duplicate skill names.")

    catalog = dict(bootstrap.skill_catalog)
    missing_from_catalog = [name for name in names if name not in catalog]
    if missing_from_catalog:
        raise FileNotFoundError(
            "Required canonical skills were not present in the pre-dispatch catalog snapshot: "
            + ", ".join(missing_from_catalog)
        )

    files = _canonical_files(bootstrap.source_root, selected_skills=names)
    digests = tuple(
        (relative, hashlib.sha256(content).hexdigest()) for relative, content in files
    )
    initial_agents_digest = dict(bootstrap.source_files).get("AGENTS.md")
    current_agents_digest = dict(digests).get("AGENTS.md")
    if initial_agents_digest and current_agents_digest != initial_agents_digest:
        raise RuntimeError("Canonical AGENTS.md changed while the Architect mission was running.")
    for name in names:
        loaded_digest = dict(digests).get(f"skills/{name}/SKILL.md")
        if loaded_digest != catalog[name]:
            raise RuntimeError(
                f"Canonical skill '{name}' changed while the Architect mission was running."
            )
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
        artifact_source_sha256=bootstrap.artifact_source_sha256,
        artifact_source_files=bootstrap.artifact_source_files,
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
) -> tuple[str, CanonicalBootstrap]:
    """Bind a phase prompt to the machine-wide bootstrap requirement.

    The marker makes the operation idempotent when a provider adapter and the
    control-plane seam both prepare the same prompt.  The role is generated by
    trusted control-plane code, never parsed from model output.
    """

    bootstrap = bootstrap or create_canonical_bootstrap(role=role)
    text = str(prompt)
    if BOOTSTRAP_MARKER in text:
        return text, bootstrap
    deferred_architect_skills = role == "architect" and not bootstrap.selected_skills
    artifact = bootstrap.artifact_path
    if artifact is None:
        transport = "The trusted host could not provide an inline canonical bootstrap artifact. Fail closed."
    else:
        try:
            snapshot = artifact.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise FileNotFoundError(f"Canonical bootstrap artifact is unavailable: {artifact}") from exc
        if deferred_architect_skills:
            source_description = (
                "The trusted Dual Agents control plane loaded the canonical AGENTS.md "
                f"directly from {bootstrap.source_root} and verified its SHA-256 value. "
                "The inline AGENTS.md below is authoritative and already loaded. Skills "
                "were intentionally left unselected because selection depends on the task. "
            )
            source_access = (
                "Do not reread AGENTS.md or this transport artifact. For this unattended "
                "Architect run, first read only the supplied task/architect artifact as "
                "read-only context; if none is referenced, use the TASK content in this "
                "prompt. This is the authorized read-only pre-read exception; inspect no "
                "other task or repository files and start no task work yet. "
                "Then select all applicable skills from the canonical catalog at "
                f"{bootstrap.source_root / 'skills'} and read each complete SKILL.md. Do not "
                "ask the user to choose or identify skills. If a required skill is unavailable, "
                "stop without asking for clarification. Do not inspect repository files, plan, "
                "edit, or run task commands until AGENTS.md and all selected skills are loaded. "
                "In the final plan, list every fully loaded skill directory name in the required "
                "top-level skills_loaded field so the control plane can verify its provenance. "
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
            "No skills were preselected by the control plane; this does not mean no skill "
            "applies. Select and load them yourself after reading the task context."
        )
    else:
        skill_status = (
            f"Selected canonical skills: {', '.join(bootstrap.selected_skills) or '(none)'}. "
            "Treat the injected AGENTS.md and selected SKILL.md content as already loaded "
            "canonical policy; do not attempt any bootstrap filesystem access."
        )
    prefix = (
        f"{BOOTSTRAP_MARKER}\n"
        f"{transport}\n"
        f"Canonical source path: {bootstrap.source_root}; source_sha256={bootstrap.source_sha256}. "
        f"{skill_status}\n"
        f"Trusted configured phase role: {role}\n\n"
    )
    return prefix + text, bootstrap
