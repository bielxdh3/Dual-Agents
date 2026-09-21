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

    def metadata(self) -> dict[str, object]:
        return {
            "canonical_bootstrap_required": True,
            "canonical_bootstrap_source": "machine-wide",
            "canonical_bootstrap_source_path": str(self.source_root),
            "canonical_bootstrap_source_sha256": self.source_sha256,
            "canonical_bootstrap_mechanism": self.mechanism,
            "canonical_bootstrap_artifact": str(self.artifact_path or ""),
            "canonical_bootstrap_artifact_sha256": self.artifact_sha256,
            "canonical_bootstrap_artifact_ephemeral": self.artifact_path is not None,
            "canonical_bootstrap_delivery": "trusted_inline" if self.artifact_path is not None else "source-reference",
            "canonical_bootstrap_selected_skills": list(self.selected_skills),
            "canonical_bootstrap_source_files": {
                relative: digest for relative, digest in self.source_files
            },
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
    """Return the bounded skill set selected for a configured Codex phase.

    The global policy makes memory and Ponytail mandatory for coding work. The
    architect/reviewer phases additionally require phase and security review;
    no unrelated skill tree content is transported.
    """

    selected = {"memory", "ponytail"}
    if role in {"architect", "reviewer"}:
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
    selected = _normalise_skill_names(selected_skills)
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
        )
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        path.unlink(missing_ok=True)
        raise


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
    artifact = bootstrap.artifact_path
    if artifact is None:
        transport = "The trusted host could not provide an inline canonical bootstrap artifact. Fail closed."
    else:
        try:
            snapshot = artifact.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise FileNotFoundError(f"Canonical bootstrap artifact is unavailable: {artifact}") from exc
        transport = (
            "The trusted Dual Agents control plane loaded the canonical instruction sources "
            f"directly from {bootstrap.source_root} and verified their SHA-256 values. "
            "For this turn, the complete inline block below is the authoritative canonical "
            "bootstrap. Do not issue filesystem or shell commands to re-read or rediscover "
            "the source paths or this transport artifact. Apply the injected content exactly. "
            "If the block is missing or invalid, stop fail-closed. Begin inline bootstrap:\n"
            f"--- BEGIN CANONICAL BOOTSTRAP SNAPSHOT ---\n{snapshot}\n"
            "--- END CANONICAL BOOTSTRAP SNAPSHOT ---"
        )
    prefix = (
        f"{BOOTSTRAP_MARKER}\n"
        f"{transport}\n"
        f"Canonical source path: {bootstrap.source_root}; source_sha256={bootstrap.source_sha256}. "
        f"Selected canonical skills: {', '.join(bootstrap.selected_skills) or '(none)'}. "
        "Treat the injected AGENTS.md and selected SKILL.md content as already loaded "
        "canonical policy; do not attempt any bootstrap filesystem access.\n"
        f"Trusted configured phase role: {role}\n\n"
    )
    return prefix + text, bootstrap
