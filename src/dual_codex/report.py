from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from .paths import safe_atomic_write_text, safe_ensure_directory_tree, safe_read_bytes


EXECUTOR_REPORT_FIELDS = frozenset(
    {"summary", "files_changed", "commands_run", "tests", "remaining_issues"}
)
EXECUTOR_REPORT_OPTIONAL_FIELDS = frozenset({"memory_updates", "security_scan_provenance"})
EXECUTOR_REPORT_REQUIRED_WITHOUT_TELEMETRY = EXECUTOR_REPORT_FIELDS - {"commands_run"}
_EXTENDED_REPORT_FIELDS = frozenset(
    {
        "summary",
        "status",
        "starting_sha",
        "final_sha",
        "files_changed",
        "behavior_changed",
        "validations_run",
        "validations_not_run",
        "remaining_limitations",
        "next_plan_tree_item",
        "commands_run",
        "tests",
        "remaining_issues",
        "memory_updates",
        "security_scan_provenance",
        "push_result",
        "remote_result",
        "pr_result",
    }
)


def _string_list(value: Any) -> list[str] | None:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return None
    return list(value)


def _test_list(value: Any, *, status: str) -> list[dict[str, str]] | None:
    if not isinstance(value, list):
        return None
    result: list[dict[str, str]] = []
    for item in value:
        if isinstance(item, str):
            result.append({"command": item, "status": status, "details": "reported by Executor"})
            continue
        if not isinstance(item, Mapping) or set(item) != {"command", "status", "details"}:
            return None
        if not all(isinstance(item[field], str) for field in ("command", "status", "details")):
            return None
        if item["status"] not in {"passed", "failed", "not_run"}:
            return None
        result.append({field: item[field] for field in ("command", "status", "details")})
    return result


def _normalise_extended_report(value: Mapping[str, Any]) -> dict[str, Any] | None:
    """Adapt the known richer Executor result to the one canonical report shape."""

    if not set(value).issubset(_EXTENDED_REPORT_FIELDS):
        return None
    if not isinstance(value.get("status"), str) or not value["status"].strip():
        return None
    for field in (
        "starting_sha",
        "final_sha",
        "behavior_changed",
        "next_plan_tree_item",
        "summary",
        "push_result",
        "remote_result",
        "pr_result",
    ):
        if field in value and not isinstance(value[field], str):
            return None
    if not any(
        field in value
        for field in (
            "starting_sha",
            "final_sha",
            "behavior_changed",
            "validations_run",
            "validations_not_run",
            "remaining_limitations",
            "push_result",
            "remote_result",
            "pr_result",
        )
    ):
        return None
    files_changed = _string_list(value.get("files_changed"))
    commands_run = _string_list(value.get("commands_run", []))
    if files_changed is None or commands_run is None:
        return None
    validations_run = _test_list(value.get("validations_run", []), status="passed")
    validations_not_run = _test_list(value.get("validations_not_run", []), status="not_run")
    limitations = _string_list(value.get("remaining_limitations", []))
    remaining_issues = _string_list(value.get("remaining_issues", []))
    if validations_run is None or validations_not_run is None or limitations is None or remaining_issues is None:
        return None
    tests = _test_list(value.get("tests", []), status="passed")
    if tests is None:
        return None
    summary = value.get("summary")
    if summary is None:
        status = value["status"]
        behavior = value.get("behavior_changed", "")
        summary = f"{status}: {behavior}".rstrip(": ")
    if not isinstance(summary, str):
        return None
    remaining_issues.extend(limitations)
    for field in ("push_result", "remote_result", "pr_result"):
        result = value.get(field)
        if result is not None and not isinstance(result, str):
            return None
        if result and result.casefold() not in {"passed", "completed", "updated", "not attempted", "not checked", "not updated"}:
            remaining_issues.append(f"{field}: {result}")
    result = {
        "summary": summary,
        "files_changed": files_changed,
        "commands_run": commands_run,
        "tests": [*tests, *validations_run, *validations_not_run],
        "remaining_issues": remaining_issues,
    }
    if "memory_updates" in value:
        result["memory_updates"] = value["memory_updates"]
    if "security_scan_provenance" in value:
        result["security_scan_provenance"] = value["security_scan_provenance"]
    return result


def normalise_executor_report(value: Mapping[str, Any]) -> dict[str, Any]:
    """Canonicalise only the optional command telemetry omission.

    Semantic fields and present values are deliberately left untouched so the
    strict validator can reject malformed or incomplete reports afterwards.
    """

    normalised = dict(value)
    if (
        set(normalised).issubset(EXECUTOR_REPORT_FIELDS | EXECUTOR_REPORT_OPTIONAL_FIELDS)
        and EXECUTOR_REPORT_REQUIRED_WITHOUT_TELEMETRY.issubset(normalised)
        and "commands_run" not in normalised
    ):
        normalised["commands_run"] = []
    extended = _normalise_extended_report(normalised)
    if extended is not None:
        return extended
    return normalised


def is_executor_report_shape(value: Mapping[str, Any]) -> bool:
    """Return whether a value is a canonical report after safe normalisation."""

    keys = set(value)
    return (
        keys.issubset(EXECUTOR_REPORT_FIELDS | EXECUTOR_REPORT_OPTIONAL_FIELDS)
        and EXECUTOR_REPORT_FIELDS.issubset(keys)
    )


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(safe_read_bytes(path, max_bytes=64 * 1024 * 1024).decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return value


def dump_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Write a JSON object without leaving a partially written result."""
    path = Path(os.path.abspath(os.path.expanduser(os.fspath(path))))
    safe_ensure_directory_tree(path.parent)
    safe_atomic_write_text(path, dump_json(data) + "\n")


def render_markdown(
    *,
    task_file: Path,
    plan: dict[str, Any],
    implementation: dict[str, Any],
    review: dict[str, Any],
    correction_cycles: int,
    phase_provenance: list[dict[str, Any]] | None = None,
    mutation_attribution: Mapping[str, Any] | None = None,
    initial_git_baseline: Mapping[str, Any] | None = None,
    security_scan_authority: Mapping[str, Any] | None = None,
    security_scan_authority_history: list[Mapping[str, Any]] | None = None,
) -> str:
    lines = [
        "# Dual Codex Run Report",
        "",
        f"- Task: `{task_file}`",
        f"- Verdict: **{review['verdict']}**",
        f"- Correction cycles: **{correction_cycles}**",
        "",
        "## Architect plan",
        "",
        plan["summary"],
        "",
    ]
    for index, step in enumerate(plan["steps"], start=1):
        lines.append(f"{index}. {step}")
    lines.extend(["", "## Implementation", "", implementation["summary"], ""])
    if implementation["files_changed"]:
        lines.append("### Files changed")
        lines.extend(f"- `{item}`" for item in implementation["files_changed"])
        lines.append("")
    lines.extend(["## Review", "", review["summary"], ""])
    for finding in review["findings"]:
        lines.extend(
            [
                f"### {finding['severity'].upper()}: {finding['title']}",
                "",
                finding["details"],
                "",
            ]
        )
    if isinstance(security_scan_authority, Mapping):
        selected_id = str(security_scan_authority.get("selected_scan_id") or "")
        status = str(security_scan_authority.get("authority_state") or "missing")
        lines.extend(
            [
                "## Codex Security coverage",
                "",
                f"- Required mode and scope: `{security_scan_authority.get('required_mode', 'unknown')}` / `{security_scan_authority.get('required_scope', 'unknown')}`",
                f"- Authority: **{status}** / generation **{security_scan_authority.get('generation', 'unknown')}** / fresh for acceptance: **{str(status == 'completed_fresh').lower()}**",
                f"- Authoritative scan: `{selected_id or 'none'}` / mode `{security_scan_authority.get('selected_scan_mode', 'unknown')}` / scope `{security_scan_authority.get('selected_scan_scope', 'unknown')}`",
                f"- Target: `{security_scan_authority.get('target_path', 'unknown')}` / revision `{security_scan_authority.get('target_revision', 'unknown')}`",
                "",
            ]
        )
        history = security_scan_authority_history or []
        coverage_events = [
            event
            for event in history
            if isinstance(event, Mapping)
            and event.get("event") in {"run_owned", "completed_fresh", "completed_stale", "generation_authorized", "rescan_limit"}
        ]
        if coverage_events:
            lines.extend(["### Security generation history", ""])
            for event in coverage_events:
                event_name = str(event.get("event", "unknown"))
                generation = event.get("generation", "unknown")
                scan_id = str(event.get("selected_scan_id") or event.get("previous_scan_id") or "none")
                failure = f" / `{event.get('failure_class')}`" if event.get("failure_class") else ""
                lines.append(f"- Generation {generation}: **{event_name}** / scan `{scan_id}`{failure}")
            lines.append("")
    else:
        security_scan = implementation.get("security_scan_provenance")
        if isinstance(security_scan, Mapping):
            target = security_scan.get("target_identity")
            target_path = target.get("path", "unknown") if isinstance(target, Mapping) else "unknown"
            lines.extend(
                [
                    "## Executor-reported Codex Security evidence",
                    "",
                    "- Target: `{}`".format(target_path),
                    "- Scan: `{}` / mode `{}` / action `{}` / `{}` → `{}`".format(
                        security_scan.get("scan_id", "unknown"),
                        security_scan.get("scan_mode", security_scan.get("mode", "unknown")),
                        security_scan.get("action", "unknown"),
                        security_scan.get("initial_status", "unknown"),
                        security_scan.get("final_status", "unknown"),
                    ),
                    "- This is Executor-reported evidence; host authority is required to establish fresh coverage.",
                    "",
                ]
            )
    if phase_provenance:
        lines.extend(["## Configured actor routing", ""])
        for item in phase_provenance:
            lines.append(
                "- {phase}: actor `{actor}` / provider `{provider}` / backend `{backend}` "
                "/ transport `{transport}` / configured_actor=`{configured}`".format(
                    phase=item.get("phase", item.get("role", "unknown")),
                    actor=item.get("actor_id", item.get("profile_id", "unknown")),
                    provider=item.get("provider", "unknown"),
                    backend=item.get("backend", "unknown"),
                    transport=item.get("delegation_transport", "unknown"),
                    configured=str(bool(item.get("configured_actor", False))).lower(),
                )
            )
        lines.append("")
    if initial_git_baseline or mutation_attribution:
        lines.extend(["## Git mutation attribution", ""])
        if initial_git_baseline:
            lines.append(
                "- Initial baseline: `{path}` (HEAD `{head}`, SHA-256 `{sha256}`)".format(
                    path=initial_git_baseline.get("path", "initial_git_baseline.json"),
                    head=initial_git_baseline.get("head", "unknown"),
                    sha256=initial_git_baseline.get("sha256", "unknown"),
                )
            )
        if mutation_attribution:
            lines.append(f"- Attribution status: **{mutation_attribution.get('status', 'unknown')}**")
            for field, label in (
                ("unchanged_preexisting_paths", "Unchanged pre-existing paths"),
                ("run_touched_paths", "Changed further during run"),
                ("run_created_paths", "Created during run"),
                ("run_removed_paths", "Removed during run"),
                ("unknown_paths", "Unknown attribution"),
            ):
                paths = mutation_attribution.get(field, [])
                lines.append(f"- {label}: " + (", ".join(f"`{path}`" for path in paths) if paths else "none"))
            ephemeral = mutation_attribution.get("dual_agents_ephemeral_artifacts", {})
            if isinstance(ephemeral, Mapping):
                final_artifacts = ephemeral.get("final", [])
                lines.append(
                    "- Dual Agents bootstrap artifacts remaining: "
                    + (", ".join(f"`{path}`" for path in final_artifacts) if final_artifacts else "none")
                )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
