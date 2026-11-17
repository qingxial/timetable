"""Run the original rescheduler against one explicit versioned state.

The legacy script runs in a disposable directory with materialized workbooks.
It never receives or loads a caller-provided pickle; its generated pickle is
retained only as an internal cache attached to the resulting state version.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

from openpyxl import Workbook

try:
    from .repair_data import load_dataset
except ImportError:
    from repair_data import load_dataset


class LegacyAdapterError(ValueError):
    pass


_WORKER = r'''
import json, sys
from pathlib import Path
stage = Path(sys.argv[1]).resolve()
base = stage / "智能排课基础数据" / "提取的基础数据表_converted"
results = stage / "排课结果"
import reschedule_by_adjusting as legacy
import utils_for_reschedule as utils
policy = json.loads(Path(sys.argv[2]).read_text())
# Workbooks already match the reference templates. Do not rebuild them from
# dataclass fields: that loses Chinese headers, column order and cell formats.
import Basic_Data as basic
from scripts.repair_data import _read_table, merge_room_rows
original_rooms = basic.load_classrooms
def unique_rooms(*args, **kwargs):
    loaded=original_rooms(*args, **kwargs)
    rows=_read_table(Path(args[0]),True)
    canonical={str(row['JASDM']):row for _,row in merge_room_rows(rows)}
    result=[];seen=set()
    for room in loaded:
        rid=str(room.JASDM).strip()
        if rid in seen:continue
        room.JASDM=rid;room.JASMC=canonical[rid]['JASMC']
        result.append(room);seen.add(rid)
    return result
for module in (basic,utils,legacy):module.load_classrooms=unique_rooms
for module in (utils, legacy):
    module.num_weeks = policy["weeks"]
    module.num_days = 7
    module.num_periods = 11
    module.root_path = str(base)
    module.course_excel = str(base / "课程表.xlsx")
    module.classroom_excel = str(base / "教室表.xlsx")
    module.teacher_excel = str(base / "教师表.xlsx")
    module.banji_excel = str(base / "班级表.xlsx")
    module.results_dir = str(results)
    module.failed_courses_file = str(results / "排课失败课程_全部.xlsx")
    module.scheduled_courses_file = str(results / "排课结果_全部.xlsx")
    module.scheduling_timetables_file = str(results / "DO_NOT_LOAD_CLIENT_PICKLE.pkl")
    module.reschedule_timetables_file = str(results / "reschedule_timetables.pkl")
results.mkdir(parents=True, exist_ok=True)
# 搜索阶段就只在授权时间范围内找新时段；全周 7 天 × 11 节时不限制，保持原脚本行为。
domain = {(d, p) for d in policy.get("days", range(7)) for p in policy.get("periods", range(1, 12))}
legacy.TIME_DOMAIN = None if domain >= {(d, p) for d in range(7) for p in range(1, 12)} else domain
from scripts.timetable_progress import emit
progress_dir = policy.get('progress_dir')
original_failed = legacy.load_failed_courses
def traced_failed():
    frame = original_failed()
    if frame is None: return frame
    original_rows = frame.iterrows
    total = len(set(str(v) for v in frame['教学班ID']))
    completed = set()
    emit(progress_dir,'legacy','原调课引擎开始逐课处理；百分比为处理进度',completed=0,total=total)
    def traced_rows():
        for index, row in original_rows():
            cid = str(row['教学班ID'])
            emit(progress_dir,'legacy',f'{cid}：开始调课尝试',completed=len(completed),total=total,course_id=cid)
            yield index, row
            completed.add(cid)
            attempt = legacy.course_attempt_result(cid)
            label = {'success':'本课尝试成功','failed':'本课尝试失败'}.get(attempt,'本课未进入调课尝试')
            emit(progress_dir,'legacy',f'{cid}：{label}，结果待整体验收',completed=len(completed),total=total,course_id=cid,outcome='attempted')
    frame.iterrows = traced_rows
    return frame
legacy.load_failed_courses = traced_failed
legacy.reschedule(None)  # force reconstruction from this version's Excel state
'''


def _cells(rows: list[dict[str, Any]]) -> dict[str, set[tuple[str, int, int, int, str]]]:
    result: dict[str, set[tuple[str, int, int, int, str]]] = {}
    for row in rows:
        course_id = row["course_id"]
        result.setdefault(course_id, set()).update(
            (course_id, int(week), int(row["day"]), int(period), str(row["room_id"]))
            for week in row["weeks"] for period in row["periods"])
    return result


def run_legacy_reschedule(store, state_id: str, config: dict[str, Any] | None = None,
                          authorization: dict[str, Any] | None = None, *,
                          artifact_dir: str | Path | None = None) -> dict[str, Any]:
    state = store.load(state_id)
    if not state.get("remaining_course_ids"):
        raise LegacyAdapterError("current state has no remaining courses for legacy rescheduling")
    operation = state.get("operation", {})
    if any(str(issue).startswith("baseline:row") for issue in state.get("validation", {}).get("dataset_issues", [])):
        raise LegacyAdapterError("legacy engine cannot safely reconstruct uncertain baseline rows; use the versioned repair engine")
    config = store.resolve_policy(state, config or {})
    allowed_days = set(config.get("days", range(5)))
    allowed_periods = set(config.get("periods", range(1, 9)))
    movable_ids = set(config.get("movable_ids", []))
    max_moved = int(config.get("max_moved_courses", 0))
    relevant_ids = set(state.get("remaining_course_ids", [])) | movable_ids
    inherited_decisions = operation.get("correction_history", []) + operation.get("corrections", [])
    active_corrections = [item for item in inherited_decisions if item.get("course_id") in relevant_ids]
    rule_pack = operation.get("rule_pack_spec") or {}
    active_rules = [rule for rule in rule_pack.get("rules", [])
                    if relevant_ids.intersection(rule.get("course_ids", []))]
    if active_corrections or active_rules:
        raise LegacyAdapterError("legacy engine cannot apply active data corrections or room rules for the remaining/movable courses; use the versioned repair engine")
    source = store.dataset_files(state)
    state_dir = store.states / state_id
    with TemporaryDirectory(prefix="timetable-legacy-") as temporary:
        stage = Path(temporary)
        base = stage / "智能排课基础数据" / "提取的基础数据表_converted"
        result_dir = stage / "排课结果"
        base.mkdir(parents=True)
        result_dir.mkdir(parents=True)
        aliases = {"courses": "课程表.xlsx", "rooms": "教室表.xlsx", "teachers": "教师表.xlsx", "classes": "班级表.xlsx"}
        for key, name in aliases.items():
            if key in source:
                shutil.copy2(source[key], base / name)
        if "teachers" not in source or "classes" not in source:
            raise LegacyAdapterError("legacy engine requires teacher and class workbooks in the baseline sources")
        try:
            from .engine_input_contract import prepare_engine_inputs
        except ImportError:
            from engine_input_contract import prepare_engine_inputs
        contract_directory=Path(artifact_dir)/'engine-inputs' if artifact_dir is not None else stage/'engine-inputs'
        prepared,format_audit=prepare_engine_inputs(source,contract_directory,Path(__file__).resolve().parents[1])
        for key,name in aliases.items():
            if key in prepared:shutil.copy2(prepared[key],base/name)
        schedule_input = state_dir / state["artifacts"]["schedule"]
        shutil.copy2(schedule_input, result_dir / "排课结果_全部.xlsx")
        shutil.copy2(state_dir / state["artifacts"]["remaining"], result_dir / "排课失败课程_全部.xlsx")
        policy_file = stage / "runtime-policy.json"
        weeks = int(config.get("weeks", 20))
        if not 1 <= weeks <= 53:
            raise LegacyAdapterError("legacy semester weeks must be between 1 and 53")
        try:
            from .timetable_progress import live_directory
        except ImportError:
            from timetable_progress import live_directory
        progress_dir = live_directory() or (str(Path(artifact_dir).parent) if artifact_dir and Path(artifact_dir).name=='legacy-reschedule' else str(artifact_dir) if artifact_dir else None)
        policy_file.write_text(json.dumps({"weeks": weeks, "progress_dir": progress_dir,
                                           "days": sorted(allowed_days), "periods": sorted(allowed_periods)}))
        log_path = stage/'engine.log'
        if artifact_dir is not None:
            audit = Path(artifact_dir); audit.mkdir(parents=True, exist_ok=True)
            log_path = audit/'engine.log'
        with log_path.open('w',encoding='utf-8') as log:
            run = subprocess.run([sys.executable, "-u", "-c", _WORKER, str(stage), str(policy_file)], cwd=stage,
                                 env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
                                 stdout=log, stderr=subprocess.STDOUT, text=True)
        run.stdout = log_path.read_text(encoding='utf-8'); run.stderr = ''
        if artifact_dir is not None:
            for artifact in result_dir.glob("*.xlsx"):
                shutil.copy2(artifact, audit / artifact.name)
        if run.returncode:
            tail = (run.stdout + "\n" + run.stderr)[-6000:]
            raise LegacyAdapterError(f"legacy rescheduler failed ({run.returncode}): {tail}")
        overall = result_dir / "调课后的整体结果.xlsx"
        if not overall.is_file():
            raise LegacyAdapterError("legacy rescheduler produced no merged schedule workbook")
        dataset = load_dataset(source["courses"], source["rooms"], overall, source["classes"])
        failed_output = result_dir / "调课失败的排课失败课程.xlsx"
        if failed_output.is_file():
            book = Workbook(); sheet = book.active
            sheet.append(["教学班ID", "课程名称"])
            from openpyxl import load_workbook
            failed_book = load_workbook(failed_output, read_only=True, data_only=True)
            try:
                rows = failed_book.active.iter_rows(values_only=True)
                header = [str(value or "").strip() for value in next(rows, ())]
                id_index = next((i for i, value in enumerate(header) if value in ("jxbid", "JXBID", "教学班ID")), None)
                name_index = next((i for i, value in enumerate(header) if value in ("name", "课程名称")), None)
                if id_index is not None:
                    for row in rows:
                        identifier = row[id_index] if id_index < len(row) else None
                        if identifier not in (None, ""):
                            sheet.append([str(identifier), row[name_index] if name_index is not None and name_index < len(row) else ""])
            finally:
                failed_book.close()
            updated_remaining = stage / "remaining.xlsx"
            book.save(updated_remaining); book.close()
            failed_source = updated_remaining
        else:
            failed_source = None
        remaining_ids = []
        if failed_source:
            from openpyxl import load_workbook
            workbook = load_workbook(failed_source, read_only=True, data_only=True)
            try:
                rows = workbook.active.iter_rows(values_only=True); next(rows, None)
                remaining_ids = [str(row[0]).strip() for row in rows if row and row[0] not in (None, "")]
            finally:
                workbook.close()
        placements = [{"course_id": p.course_id, "weeks": sorted(p.weeks), "day": p.day,
                       "periods": list(p.periods), "room_id": p.room_id} for p in dataset.placements]
        before_cells = _cells(state.get("placements", []))
        after_cells = _cells(placements)
        changed_assigned = {course_id for course_id in before_cells if before_cells[course_id] != after_cells.get(course_id, set())}
        unauthorized_moves = sorted(changed_assigned - movable_ids)
        if unauthorized_moves:
            raise LegacyAdapterError(f"legacy result moved existing courses without explicit movable_ids: {unauthorized_moves}")
        if len(changed_assigned) > max_moved:
            raise LegacyAdapterError(f"legacy result moved {len(changed_assigned)} courses, exceeding max_moved_courses={max_moved}")
        before_all = set().union(*before_cells.values()) if before_cells else set()
        added_cells = [cell for rows in after_cells.values() for cell in rows if cell not in before_all]
        outside_days = sorted({cell[2] for cell in added_cells if cell[2] not in allowed_days})
        outside_periods = sorted({cell[3] for cell in added_cells if cell[3] not in allowed_periods})
        if outside_days or outside_periods:
            raise LegacyAdapterError(f"legacy result exceeded the authorized time domain: days={outside_days}, periods={outside_periods}")
        cache = result_dir / "reschedule_timetables.pkl"
        cache_copy = None
        if cache.is_file():
            cache_staging = store.root / "staging"
            cache_staging.mkdir(parents=True, exist_ok=True)
            cache_copy = cache_staging / f"legacy-cache-{uuid4().hex}.pkl"
            shutil.copy2(cache, cache_copy)
        return {"placements": placements, "remaining_course_ids": remaining_ids,
                "legacy_cache": str(cache_copy) if cache_copy else None,
                "run_summary": {"return_code": run.returncode, "stdout_tail": run.stdout[-2000:],
                                "generated_legacy_cache": bool(cache_copy)},
                "engine_validation": {"dataset_issues": dataset.issues, "placement_count": len(placements)}}
