"""Versioned, auditable handoff state shared by timetable engines.

State JSON is the audit source of truth. Excel files are materialized engine
inputs/exports; pickle files are never accepted from callers.
"""
from __future__ import annotations

from copy import deepcopy
from tempfile import TemporaryDirectory
from hashlib import sha256
import json
from pathlib import Path
import shutil
from typing import Any
from uuid import uuid4

from openpyxl import Workbook, load_workbook

try:
    from .repair_data import DAYS, Placement, load_dataset, room_metadata_audit
except ImportError:
    from repair_data import DAYS, Placement, load_dataset, room_metadata_audit


def file_hash(path: str | Path) -> str:
    digest = sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _save_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _read_ids(path: Path | None) -> list[str]:
    if not path or not path.is_file():
        return []
    book = load_workbook(path, read_only=True, data_only=True)
    try:
        rows = book.active.iter_rows(values_only=True)
        headers = [str(v or "").strip() for v in next(rows, ())]
        key = next((i for i, value in enumerate(headers) if value in ("jxbid", "JXBID", "教学班ID")), None)
        if key is None:
            return []
        return list(dict.fromkeys(str(row[key]).strip() for row in rows if key < len(row) and row[key] not in (None, "")))
    finally:
        book.close()


def _course_metadata(dataset) -> dict[str, dict[str, str]]:
    return {key: {"name": course.name, "campus": course.campus} for key, course in dataset.courses.items()}


def _placements(dataset) -> list[dict[str, Any]]:
    return [{"course_id": item.course_id, "weeks": sorted(item.weeks), "day": item.day,
             "periods": list(item.periods), "room_id": item.room_id}
            for item in sorted(dataset.placements, key=lambda x: (x.course_id, x.day, x.periods, x.room_id, sorted(x.weeks)))]


def _state_hash(state):
    return sha256(json.dumps({k: v for k, v in state.items() if k != 'state_hash'},
                             ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class StateStore:
    """Append-only state versions, each bound to immutable source snapshots."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.datasets = self.root / "datasets"
        self.states = self.root / "states"
        self.datasets.mkdir(parents=True, exist_ok=True)
        self.states.mkdir(parents=True, exist_ok=True)

    def state_path(self, state_id: str) -> Path:
        if not state_id or Path(state_id).name != state_id:
            raise ValueError("invalid state_id")
        path = self.states / state_id / "state.json"
        if not path.is_file():
            raise ValueError(f"unknown timetable state: {state_id}")
        return path

    def load(self, state_id: str) -> dict[str, Any]:
        state = json.loads(self.state_path(state_id).read_text(encoding="utf-8"))
        if state.get('state_hash') and state['state_hash'] != _state_hash(state):
            raise ValueError('state JSON hash mismatch')
        dataset_dir = self.datasets / state["dataset_id"]
        for key, name in state["source_files"].items():
            if Path(name).name != name or file_hash(dataset_dir / name) != state["source_hashes"].get(key):
                raise ValueError(f"state source snapshot hash mismatch: {key}")
        for key, name in state.get("artifacts", {}).items():
            expected = state.get("artifact_hashes", {}).get(key)
            if expected and (Path(name).name != name or file_hash(self.states / state_id / name) != expected):
                raise ValueError(f"state artifact hash mismatch: {key}")
        return state

    def _state_dir(self, state_id: str) -> Path:
        self.state_path(state_id)
        return self.states / state_id

    def dataset_files(self, state: dict[str, Any]) -> dict[str, Path]:
        base = self.datasets / state["dataset_id"]
        return {key: base / name for key, name in state["source_files"].items()}

    def create_baseline(self, source_paths: dict[str, str | Path], *,
                        operation=None, strict=False, validation=None) -> dict[str, Any]:
        required = {"courses", "rooms", "schedule"}
        if not required.issubset(source_paths):
            raise ValueError("baseline needs courses, rooms, and schedule workbooks")
        paths = {key: Path(value).resolve() for key, value in source_paths.items() if value}
        hashes = {key: file_hash(path) for key, path in sorted(paths.items())}
        dataset_id = sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()[:24]
        dataset_dir = self.datasets / dataset_id
        dataset_dir.mkdir(parents=True, exist_ok=True)
        source_files = {}
        for key, path in paths.items():
            suffix = path.suffix or ".xlsx"
            name = f"{key}{suffix}"
            target = dataset_dir / name
            if not target.exists():
                shutil.copy2(path, target)
            source_files[key] = name
        dataset = load_dataset(dataset_dir / source_files["courses"], dataset_dir / source_files["rooms"],
                               dataset_dir / source_files["schedule"],
                               dataset_dir / source_files["classes"] if "classes" in source_files else None)
        rows = _placements(dataset)
        remaining = _read_ids(dataset_dir / source_files['failed']) if 'failed' in source_files else []
        operation = deepcopy(operation or {'engine': 'input', 'tool': 'create_baseline', 'parameters': {}})
        policy = self.resolve_policy(None, operation.get('parameters', {}), dataset)
        checked = self._validate(dataset, dataset_dir, source_files)
        if not checked['valid']:
            raise ValueError('baseline rejected: existing schedule contains cross-campus placements')
        checked['mode'] = 'imported_baseline'
        if strict:
            try:
                from .timetable_acceptance import validate_transition
            except ImportError:
                from timetable_acceptance import validate_transition
            checked.update(validate_transition(dataset, [], rows,
                {r['course_id'] for r in rows} | set(remaining), remaining, policy, weeks=policy['weeks'], full=True))
        if validation:
            checked['engine_validation'] = validation
        state = {'schema_version': 1, 'state_id': uuid4().hex, 'parent_state_id': None,
                 'dataset_id': dataset_id, 'source_files': source_files, 'source_hashes': hashes,
                 'placements': rows, 'remaining_course_ids': remaining, 'operation': operation,
                 'effective_policy': policy, 'validation': checked,
                 'artifacts': {'schedule': 'schedule.xlsx', 'remaining': 'remaining.xlsx'}}
        return self._publish(state, dataset)

    def resolve_policy(self, parent=None, overrides=None, dataset=None):
        policy = {'days': list(range(5)), 'periods': list(range(1, 9)),
                  'time_scope': 'strict', 'allowed_changes': [], 'weeks': 20}
        if parent:
            if parent.get('effective_policy'):
                policy.update(deepcopy(parent['effective_policy']))
            else:
                ancestor = self.load(parent['parent_state_id']) if parent.get('parent_state_id') else None
                policy = self.resolve_policy(ancestor, parent.get('operation', {}).get('parameters', {}), dataset)
        elif dataset:
            policy['weeks'] = max([20] + [max(c.weeks) for c in dataset.courses.values() if c.weeks])
        overrides = dict(overrides or {})
        if isinstance(overrides.get('config'), dict):
            overrides = overrides['config']
        policy.update(deepcopy(overrides))
        # Movement/target grants and acceptance waivers belong to one operation and are never inherited.
        for key, default in {'movable_ids': [], 'locked_ids': [], 'target_ids': [], 'max_moved_courses': 0,
                             'check_class_conflicts': True, 'class_conflict_exemption_evidence': '',
                             'preference_as_soft': False, 'relax_course_unavailable': False,
                             'reviewed_soft_time_ids': [], 'time_relaxation_levels': {},
                             'auto_concentrated_teaching': False, 'course_block_limits': {}}.items():
            policy[key] = deepcopy(overrides.get(key, default))
        from scripts.class_conflict_policy import enabled, acceptance_policy
        if not enabled():policy.update(acceptance_policy())
        return policy

    def _publish(self, state, dataset, legacy_cache=None):
        """Publish a complete version atomically; failures expose no partial state."""
        staging = self.root / 'staging'
        staging.mkdir(exist_ok=True)
        with TemporaryDirectory(prefix='publish-', dir=staging) as temporary:
            directory = Path(temporary) / state['state_id']; directory.mkdir()
            self._write_schedule(directory / 'schedule.xlsx', state['placements'])
            self._write_remaining(directory / 'remaining.xlsx', state['remaining_course_ids'], dataset)
            self._write_export(state, dataset, directory / 'schedule-export.xlsx')
            state['artifacts']['export'] = 'schedule-export.xlsx'
            if legacy_cache:
                shutil.copy2(legacy_cache, directory / 'legacy-cache.pkl')
                state['artifacts']['legacy_cache'] = 'legacy-cache.pkl'
            state['artifact_hashes'] = {key: file_hash(directory / name) for key, name in state['artifacts'].items()}
            state['state_hash'] = _state_hash(state)
            _save_json(directory / 'state.json', state)
            directory.rename(self.states / state['state_id'])
        return state

    def _validate(self, dataset, dataset_dir: Path, source_files: dict[str, str]) -> dict[str, Any]:
        campus_errors = []
        for placement in dataset.placements:
            course = dataset.courses.get(placement.course_id)
            room = dataset.rooms.get(placement.room_id)
            if course and room and course.campus and room.campus and course.campus != room.campus:
                campus_errors.append({"course_id": placement.course_id, "course_campus": course.campus,
                                      "room_id": placement.room_id, "room_campus": room.campus})
        return {"valid": not campus_errors, "cross_campus_violations": campus_errors,
                "dataset_issues": dataset.issues, "room_metadata_audit": room_metadata_audit(dataset),
                "placement_count": len(dataset.placements),
                "source_files_rehashed": {key: file_hash(dataset_dir / name) for key, name in source_files.items()}}

    def _write_remaining(self, path: Path, ids: list[str], dataset) -> None:
        book = Workbook(); sheet = book.active
        sheet.append(["教学班ID", "课程名称"])
        for identifier in ids:
            sheet.append([identifier, dataset.courses.get(identifier).name if identifier in dataset.courses else ""])
        book.save(path); book.close()

    def _write_schedule(self, path: Path, placements: list[dict[str, Any]]) -> None:
        book = Workbook(); sheet = book.active
        sheet.append(["教学班ID", "教室代码", "上课周次", "星期", "节次"])
        for row in placements:
            weeks = sorted(set(int(x) for x in row["weeks"]))
            periods = sorted(set(int(x) for x in row["periods"]))
            day = int(row["day"])
            day_text = f"周{'一二三四五六日'[day]}" if day in range(7) else ""
            period_text = f"第{periods[0]}-{periods[-1]}节" if periods else ""
            # Keep uncertain baseline rows as explicit malformed records. The
            # repair engine freezes their known resources conservatively.
            room_id = row["room_id"] or "(共占)"
            sheet.append([row["course_id"], room_id, ",".join(map(str, weeks)), day_text, period_text])
        book.save(path); book.close()

    def export_excel(self, state_id: str, output_path: str | Path | None = None) -> Path:
        state = self.load(state_id)
        existing = self.states / state_id / state['artifacts'].get('export', 'schedule-export.xlsx')
        if existing.is_file():
            if output_path is not None and Path(output_path).resolve() != existing:
                shutil.copy2(existing, output_path)
                return Path(output_path)
            return existing
        files = self.dataset_files(state)
        dataset = load_dataset(files["courses"], files["rooms"],
                               self._state_dir(state_id) / state["artifacts"]["schedule"],
                               files.get("classes"))
        target = Path(output_path) if output_path else self._state_dir(state_id) / "schedule-export.xlsx"
        self._write_export(state, dataset, target)
        return target

    def _write_export(self, state, dataset, target):
        book = Workbook(); sheet = book.active
        sheet.append(["教学班ID", "课程名称", "教室", "教室代码", "校区", "上课周次", "星期", "节次"])
        for row in state["placements"]:
            course = dataset.courses.get(row["course_id"])
            room = dataset.rooms.get(row["room_id"])
            weeks = sorted(row["weeks"]); periods = sorted(row["periods"])
            week_text = ",".join(map(str, weeks))
            period_text = f"第{periods[0]}-{periods[-1]}节" if periods else ""
            sheet.append([row["course_id"], course.name if course else "", room.name if room else "",
                          row["room_id"] or "(共占)", room.campus if room else "", week_text,
                          f"周{'一二三四五六日'[row['day']]}" if 0 <= row["day"] <= 6 else "", period_text])
        book.save(target); book.close()

    def commit(self, parent_state_id: str, placements: list[dict[str, Any]], remaining_ids: list[str],
               operation: dict[str, Any], validation: dict[str, Any] | None = None,
               legacy_cache: str | Path | None = None) -> dict[str, Any]:
        parent = self.load(parent_state_id)
        files = self.dataset_files(parent)
        try:
            from .timetable_acceptance import effective_dataset, validate_transition
        except ImportError:
            from timetable_acceptance import effective_dataset, validate_transition
        parent_schedule = self.states / parent_state_id / parent['artifacts']['schedule']
        dataset = load_dataset(files['courses'], files['rooms'], parent_schedule, files.get('classes'))
        operation = deepcopy(operation)
        inherited=parent.get('operation',{}).get('planning_assumptions')
        if inherited and inherited.get('conditional_only'):operation['planning_assumptions']=deepcopy(inherited)
        prior_history = parent.get('operation', {}).get('correction_history', parent.get('operation', {}).get('corrections', []))
        history = deepcopy(prior_history)
        for item in operation.get('correction_history', []) + operation.get('corrections', []):
            if item not in history:
                if item['course_id'] in {r['course_id'] for r in parent['placements']} and item['operation'] != 'quarantine_incomplete_segments':
                    raise ValueError('cannot change metadata of an already assigned course')
                history.append(item)
        operation['correction_history'] = history
        rule_pack = operation.get('rule_pack_spec', parent.get('operation', {}).get('rule_pack_spec'))
        if rule_pack is not None:
            operation['rule_pack_spec'] = rule_pack
        dataset = effective_dataset(dataset, history, rule_pack)
        policy = self.resolve_policy(parent, operation.get('parameters', {}), dataset)
        before_rows = parent['placements']
        quarantined = {item['course_id'] for item in history if item['operation'] == 'quarantine_incomplete_segments'}
        if quarantined:
            # The audited correction removes only surplus malformed records after
            # proving the retained rows already satisfy every declared hour/week.
            before_rows = [row for row in before_rows if not (row['course_id'] in quarantined and
                           (not row['weeks'] or not row['periods'] or row['day'] not in range(7)))]
        checked = validate_transition(dataset, before_rows, placements,
            parent['remaining_course_ids'], remaining_ids, policy, weeks=policy['weeks'])
        if validation:
            checked['engine_validation'] = validation
        state = {'schema_version': 1, 'state_id': uuid4().hex, 'parent_state_id': parent_state_id,
                 'dataset_id': parent['dataset_id'], 'source_files': parent['source_files'],
                 'source_hashes': parent['source_hashes'], 'placements': deepcopy(placements),
                 'remaining_course_ids': sorted(set(remaining_ids)), 'operation': operation,
                 'effective_policy': policy, 'validation': checked,
                 'artifacts': {'schedule': 'schedule.xlsx', 'remaining': 'remaining.xlsx'}}
        return self._publish(state, dataset, legacy_cache)
