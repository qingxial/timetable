"""Reviewed, declarative extensions to the bounded timetable repair model.

Rule packs are data, never executable code.  Each rule handler is deliberately
small and testable; extending the vocabulary requires code review and tests.
Campus is deliberately absent from the vocabulary: a rule pack cannot permit a
cross-campus placement because :func:`room_rejections` retains that hard check.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
import math
from pathlib import Path
from typing import Any, Callable

try:
    from .repair_data import Dataset
except ImportError:
    from repair_data import Dataset


class RulePackError(ValueError):
    """A declarative rule pack is malformed or does not fit the input data."""


def _read_json(path: str | Path) -> dict[str, Any]:
    def duplicate_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise RulePackError(f"Duplicate JSON field: {key}")
            result[key] = value
        return result

    def invalid_constant(value):
        raise RulePackError(f"Non-finite JSON number: {value}")

    def finite_float(value):
        result = float(value)
        if not math.isfinite(result):
            invalid_constant(value)
        return result

    try:
        with Path(path).open(encoding="utf-8") as stream:
            value = json.load(stream, object_pairs_hook=duplicate_keys,
                              parse_constant=invalid_constant, parse_float=finite_float)
    except OSError as exc:
        raise RulePackError(f"Cannot read rule pack: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RulePackError(f"Invalid rule-pack JSON: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise RulePackError("Rule pack root must be an object")
    return value


def _exact_object(value, required, location):
    if not isinstance(value, dict):
        raise RulePackError(f"{location} must be an object")
    unknown = set(value) - set(required)
    missing = set(required) - set(value)
    if missing:
        raise RulePackError(f"{location} missing fields: {sorted(missing)}")
    if unknown:
        raise RulePackError(f"{location} unknown fields: {sorted(unknown)}")


def load_rule_pack(path: str | Path) -> dict[str, Any]:
    """Read a schema-version-one pack without interpreting it against courses."""
    pack = _read_json(path)
    _exact_object(pack, ("schema_version", "name", "rules"), "rule pack")
    if pack["schema_version"] != 1 or type(pack["schema_version"]) is not int:
        raise RulePackError("Only rule-pack schema_version 1 is supported")
    if not isinstance(pack["name"], str) or not pack["name"].strip():
        raise RulePackError("rule pack name must be a non-empty string")
    if not isinstance(pack["rules"], list):
        raise RulePackError("rule pack rules must be an array")
    ids = set()
    normalized = []
    for index, rule in enumerate(pack["rules"]):
        location = f"rules[{index}]"
        _exact_object(rule, ("id", "kind", "course_ids", "substitute_room_types", "evidence"), location)
        if rule["kind"] != "room_type_substitution":
            raise RulePackError(f"{location}.kind is unsupported: {rule['kind']!r}")
        if not isinstance(rule["id"], str) or not rule["id"].strip() or rule["id"] in ids:
            raise RulePackError(f"{location}.id must be a unique non-empty string")
        ids.add(rule["id"])
        for key in ("course_ids", "substitute_room_types"):
            value = rule[key]
            if (not isinstance(value, list) or not value or any(not isinstance(x, str) or not x.strip() for x in value)
                    or len(set(value)) != len(value)):
                raise RulePackError(f"{location}.{key} must be a non-empty unique string array")
        if not isinstance(rule["evidence"], str) or not rule["evidence"].strip():
            raise RulePackError(f"{location}.evidence must be a non-empty decision basis")
        normalized.append({key: rule[key] for key in ("id", "kind", "course_ids", "substitute_room_types", "evidence")})
    return {"schema_version": 1, "name": pack["name"].strip(), "rules": normalized}


def _apply_room_type_substitution(dataset: Dataset, rule: dict[str, Any]) -> None:
    for course_id in rule["course_ids"]:
        course = dataset.courses.get(course_id)
        if course is None:
            raise RulePackError(f"Rule {rule['id']!r} references unknown course {course_id!r}")
        if not course.room_type:
            raise RulePackError(f"Rule {rule['id']!r} cannot substitute an empty source room type for {course_id!r}")
        substitutes = frozenset(rule["substitute_room_types"])
        if course.room_type in substitutes:
            raise RulePackError(f"Rule {rule['id']!r} repeats source room type for {course_id!r}")
        allowed = (course.allowed_room_types or frozenset({course.room_type})) | substitutes
        dataset.courses[course_id] = replace(course, allowed_room_types=allowed)


RULE_HANDLERS: dict[str, Callable[[Dataset, dict[str, Any]], None]] = {
    "room_type_substitution": _apply_room_type_substitution,
}


def apply_rule_pack(dataset: Dataset, pack: dict[str, Any]) -> tuple[Dataset, dict[str, Any]]:
    """Return a copied dataset and an auditable summary of applied rules."""
    result = deepcopy(dataset)
    applied = []
    for rule in pack["rules"]:
        RULE_HANDLERS[rule["kind"]](result, rule)
        applied.append({**rule, "campus_constraint": "same_campus_only"})
    return result, {"name": pack["name"], "schema_version": pack["schema_version"],
                    "rules": applied,
                    "interpretation": "Rules only expand accepted room types for named courses. Campus, capacity, occupancy, teachers, classes, hours and time constraints remain enforced; cross-campus placement is never allowed."}
