from __future__ import annotations

import json
from typing import Any


_PLAN_FIELDS = frozenset(
    {
        "summary",
        "steps",
        "acceptance_criteria",
        "risks",
        "files_to_inspect",
        "skills_loaded",
    }
)
_PLAN_ARRAY_FIELDS = ("steps", "acceptance_criteria", "risks", "files_to_inspect")


class ArchitectPlanError(ValueError):
    """A completed Architect turn did not produce one valid plan."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ArchitectPlanError(f"Architect output contains duplicate JSON field '{key}'.")
        value[key] = item
    return value


def _json_objects(text: str) -> list[dict[str, Any]]:
    decoder = json.JSONDecoder(object_pairs_hook=_unique_object)
    objects: list[dict[str, Any]] = []
    cursor = 0
    while cursor < len(text):
        start = text.find("{", cursor)
        if start < 0:
            break
        try:
            value, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            cursor = start + 1
            continue
        if isinstance(value, dict):
            objects.append(value)
        cursor = max(end, start + 1)
    return objects


def validate_architect_plan(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ArchitectPlanError("Architect plan must be a JSON object.")
    unknown = sorted(set(value) - _PLAN_FIELDS)
    if unknown:
        raise ArchitectPlanError("Architect plan has unsupported field(s): " + ", ".join(unknown) + ".")
    missing = sorted(_PLAN_FIELDS - set(value))
    if missing:
        raise ArchitectPlanError("Architect plan is missing required field(s): " + ", ".join(missing) + ".")
    if not isinstance(value["summary"], str) or not value["summary"].strip():
        raise ArchitectPlanError("Architect plan field 'summary' must be a non-empty string.")
    for name in _PLAN_ARRAY_FIELDS:
        items = value[name]
        if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
            raise ArchitectPlanError(f"Architect plan field '{name}' must be an array of strings.")
    skills = value["skills_loaded"]
    if not isinstance(skills, list) or any(not isinstance(name, str) or not name.strip() for name in skills):
        raise ArchitectPlanError("Architect plan field 'skills_loaded' must be an array of skill names.")
    if len(skills) != len(set(skills)):
        raise ArchitectPlanError("Architect plan field 'skills_loaded' contains duplicate skill names.")
    return dict(value)


def parse_architect_result(text: str) -> dict[str, Any]:
    """Extract and locally validate one authoritative plan from assistant text."""

    if not isinstance(text, str) or not text.strip():
        raise ArchitectPlanError("Architect completed without returning plan text.")
    objects = _json_objects(text)
    candidates = [value for value in objects if set(value) & _PLAN_FIELDS]
    if not candidates:
        raise ArchitectPlanError("Architect output contains no JSON Architect plan object.")
    if len(candidates) != 1:
        raise ArchitectPlanError(
            f"Architect output contains {len(candidates)} authoritative plan objects; exactly one is required."
        )
    return validate_architect_plan(candidates[0])
