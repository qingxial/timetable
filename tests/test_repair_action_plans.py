"""Action plans must preserve evidence, uncertainty, and declared totals."""
from copy import deepcopy
from pathlib import Path
import json
import sys
import tempfile
import unittest

from openpyxl import Workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.repair_action_plans import build_action_plans, load_action_plan_supplemental
from scripts.repair_data import Course, Dataset, Placement, Room


def course(identifier="C1", **changes):
    args = dict(id=identifier, name="Example", hours=2, total_hours=4,
                weeks=frozenset({1, 2}), teachers={"PRIVATE_TEACHER": frozenset({1, 2})},
                campus="South", capacity=30, room_type="Lab")
    args.update(changes)
    return Course(**args)


def proposal(status="source_hours_inconsistent", identifier="C1", placements=None):
    return {"config": {"periods": list(range(1, 12)), "single_period_starts": "any"},
            "results": [{"course_id": identifier, "status": status, "diagnosis": {}}],
            "placements": placements or []}


def source(values, row=2):
    return {"file": "source.xlsx", "sheet": "教学任务", "row": row, "values": values,
            "cells": {"课程序号": f"A{row}", "人数上限": f"R{row}", "实际人数": f"S{row}",
                      "周课时": f"U{row}", "理论学时": f"Y{row}"}}


def action(plan, code):
    return next(a for a in plan["course_plans"][0]["actions"] if a["code"] == code)


class HoursPlanTests(unittest.TestCase):
    def test_integer_conflict_preserves_total_and_exposes_source_disagreement(self):
        c = course(hours=6, total_hours=64, weeks=frozenset(range(1, 17)))
        data = Dataset(courses={c.id: c})
        original = source({"课程序号": "C1", "周课时": "6", "理论学时": "64", "周数": "16"})
        supplement = {"original_courses": {"C1": [original]}}
        before = deepcopy(data)
        result = build_action_plans(data, proposal(), supplement)
        item = action(result, "reconcile_total_and_weekly_load")
        candidate = item["proposed_changes"][0]["candidate_value"]
        self.assertEqual(candidate["segments"], [{"weekly_hours": 4, "week_count": 16, "assigned_weeks": list(range(1,17))}])
        self.assertEqual(candidate["arithmetic_total"], 64)
        self.assertTrue(item["evidence"]["original_and_converted_weekly_agree"])
        self.assertTrue(item["evidence"]["original_and_converted_total_agree"])
        self.assertEqual(item["execution_status"], "needs_confirmation")
        self.assertFalse(item["auto_executable"])
        self.assertEqual(data, before)
        json.dumps(result, allow_nan=False)

    def test_five_fractional_examples_have_exact_concrete_segment_counts(self):
        cases = [(5.5, 34, 6, [(4, 1), (6, 5)]), (5.5, 64, 17, [(2, 2), (4, 15)]),
                 (5.5, 96, 17, [(4, 3), (6, 14)]), (5.5, 96, 18, [(4, 6), (6, 12)]),
                 (3.5, 32, 10, [(2, 4), (4, 6)])]
        for weekly, total, count, expected in cases:
            with self.subTest(total=total, weeks=count):
                c = course(hours=weekly, total_hours=total, weeks=frozenset(range(1, count+1)))
                result = build_action_plans(Dataset(courses={c.id: c}), proposal("unsupported"))
                candidate = action(result, "reconcile_total_and_weekly_load")["proposed_changes"][0]["candidate_value"]
                self.assertEqual([(x["weekly_hours"], x["week_count"]) for x in candidate["segments"]], expected)
                self.assertEqual(candidate["arithmetic_total"], total)
                self.assertTrue(all(x["assigned_weeks"] is None for x in candidate["segments"]))

    def test_virtual_class_does_not_inherit_whole_parent_total_twice(self):
        c = course("C1@W", hours=4, total_hours=64)
        result = build_action_plans(Dataset(courses={c.id: c}), proposal(identifier=c.id))
        item = action(result, "reconcile_total_and_weekly_load")
        self.assertIsNone(item["proposed_changes"][0]["candidate_value"])
        self.assertIn("虚班", " ".join(item["required_data_or_confirmation"]))

    def test_other_unsupported_model_is_not_misdiagnosed_as_hours(self):
        c=course(prefer="[周一(1-2节)]")
        result=build_action_plans(Dataset(courses={c.id:c}),proposal("unsupported"))
        self.assertIn("support_reported_model_condition",[a["code"] for a in result["course_plans"][0]["actions"]])
        self.assertNotIn("reconcile_total_and_weekly_load",[a["code"] for a in result["course_plans"][0]["actions"]])


class RoomAndCoverageTests(unittest.TestCase):
    def test_missing_teacher_metadata_is_not_misreported_as_class_coverage(self):
        c=course(issues=["metadata: missing teacher identifier"])
        result=build_action_plans(Dataset(courses={c.id:c}),proposal("data_issue"))
        self.assertIn("restore_verified_metadata",[a["code"] for a in result["course_plans"][0]["actions"]])
    def test_actual_enrollment_candidate_keeps_type_campus_and_confirmation(self):
        c = course(capacity=304)
        data = Dataset(courses={c.id: c}, rooms={
            "R1": Room("R1", campus="South", type="Lab", capacity=80, enabled=True),
            "R2": Room("R2", campus="North", type="Lab", capacity=1000, enabled=True),
            "R3": Room("R3", campus="South", type="Other", capacity=1000, enabled=True)})
        sup = {"original_courses": {"C1": [source({"人数上限": "304", "实际人数": "63"}, 1030)]}}
        item = action(build_action_plans(data, proposal("no_matching_room"), sup), "resolve_static_room_supply")
        candidate = item["proposed_changes"][0]
        self.assertEqual(candidate["candidate_value"], 63)
        self.assertEqual(candidate["static_room_candidates"], ["R1"])
        self.assertFalse(item["auto_executable"])
        self.assertEqual(item["evidence"]["source_records"][0]["cells"]["实际人数"], "S1030")
        self.assertEqual(c.capacity, 304)

    def test_broken_original_class_cannot_be_fabricated_from_time_label(self):
        c = course(issues=["coverage: unresolved class identifiers"])
        bad = "电气学院，周一3.48"
        sup = {"original_courses": {"C1": [source({"行政班": bad, "教学班名称": "班级:"+bad}, 2849)]},
               "converted_courses": {"C1": [{"values": {"TJBJ": bad}}]}}
        item = action(build_action_plans(Dataset(courses={c.id: c}), proposal("data_issue"), sup), "restore_verified_class_coverage")
        self.assertEqual(item["current_values"]["original_行政班"], bad)
        self.assertIsNone(item["proposed_changes"][0]["candidate_value"])
        self.assertFalse(item["auto_executable"])


class SkippedAndBaselineTests(unittest.TestCase):
    def test_promoted_skipped_courses_are_not_double_counted(self):
        courses={cid:course(cid) for cid in ['PLACED','FAILED','OFF']}
        p={'results':[{'course_id':'PLACED','status':'placed'},
                      {'course_id':'FAILED','status':'data_issue'}],
           'scope_extension':{'additional_target_ids':['PLACED','FAILED']},'config':{}}
        sup={'skipped_courses':{cid:[{'values':{'SFXYPK':'0' if cid=='OFF' else '1'}}] for cid in courses}}
        result=build_action_plans(Dataset(courses=courses),p,sup)
        self.assertEqual(result['summary']['remaining_proposal_courses'],1)
        self.assertEqual(result['summary']['silently_skipped_courses'],1)
        self.assertEqual(result['summary']['explicitly_promoted_skipped_courses'],2)
        self.assertEqual({r['course_id'] for r in result['course_plans']},{'FAILED','OFF'})
        p['scope_extension']['additional_target_ids'].append('UNKNOWN')
        with self.assertRaises(ValueError):build_action_plans(Dataset(courses=courses),p,sup)

    def test_skipped_categories_are_separate_and_zero_hours_are_not_exempt(self):
        courses = {"OFF": course("OFF", hours=0, total_hours=None),
                   "HIGH": course("HIGH", hours=16, total_hours=16, weeks=frozenset({1})),
                   "ZERO": course("ZERO", hours=0, total_hours=2, weeks=frozenset(range(1, 18)))}
        sup = {"skipped_courses": {cid: [{"values": {"SFXYPK": "0" if cid == "OFF" else "1"}}] for cid in courses}}
        result = build_action_plans(Dataset(courses=courses), {"results": [], "config": {}}, sup)
        self.assertEqual(result["summary"]["silently_skipped_courses"], 3)
        plans = {r["course_id"]: r for r in result["course_plans"]}
        self.assertEqual(plans["OFF"]["category"], "business_scope_not_scheduled")
        self.assertEqual(plans["OFF"]["actions"][0]["execution_status"], "needs_business_confirmation")
        high = plans["HIGH"]["actions"][0]["proposed_changes"][0]["candidate_value"]
        self.assertEqual(high["required_weekly_load"], 16)
        self.assertTrue(high["needs_multiple_blocks_per_day_if_workdays"])
        zero = plans["ZERO"]["actions"][0]["proposed_changes"][0]["candidate_value"]
        self.assertEqual(zero["weekly_hours"], 2)
        self.assertEqual(zero["week_count"], 1)
        self.assertIsNone(zero["assigned_weeks"])

    def test_uncertain_baseline_proposes_audited_rows_not_automatic_deletion(self):
        target = course("C1", hours=1, total_hours=2)
        broken = course("OLD", hours=1, total_hours=2)
        entries = [Placement("OLD", frozenset({1, 2}), 0, (1,), "R1"),
                   Placement("OLD", frozenset(), 1, (1, 2), "R1")]
        data = Dataset(courses={"C1": target, "OLD": broken}, placements=entries)
        rows = [{"file": "baseline.xlsx", "sheet": "Sheet1", "row": 8199,
                 "values": {"上课周次": "", "节次": "第1-2节", "周学时": "4"}}]
        result = build_action_plans(data, proposal("not_found_within_limits"), {"schedule_rows": {"OLD": rows}})
        item = action(result, "resolve_uncertain_baseline_rows")
        self.assertEqual(item["evidence"][0]["invalid_rows"][0]["row"], 8199)
        self.assertTrue(item["evidence"][0]["valid_rows_already_match_LLXS"])
        self.assertFalse(item["auto_executable"])
        self.assertEqual(data.placements, entries)

    def test_fixed_shortfall_uses_configured_domain_and_anonymous_teachers(self):
        c = course(hours=2, total_hours=4, unavailable="周一全天；周二全天；周三(1-4节)；周四(1-4节)；周五(1-4节)")
        data = Dataset(courses={c.id: c})
        result = build_action_plans(data, proposal("not_found_within_limits"))
        item = action(result, "resolve_fixed_resource_blockers")
        self.assertEqual(item["evidence"]["weekly_teacher_supply"][0]["teacher_free_period_count"], 21)
        self.assertNotIn("PRIVATE_TEACHER", json.dumps(result))


class SupplementalReaderTests(unittest.TestCase):
    def test_records_exact_cells_without_teacher_pii_or_changing_workbook(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"original.xlsx"
            book = Workbook()
            sheet = book.active
            sheet.title = "教学任务"
            sheet.append(["课程序号", "课程名称", "主讲教师", "人数上限", "实际人数", "周课时", "理论学时"])
            sheet.append(["C1", "Example", "Private Person", 100, 40, 4, 32])
            book.save(path)
            before = path.read_bytes()
            result = load_action_plan_supplemental(source_path=path)
            record = result["original_courses"]["C1"][0]
            self.assertEqual(record["row"], 2)
            self.assertEqual(record["cells"]["实际人数"], "E2")
            self.assertEqual(path.read_bytes(), before)
            self.assertNotIn("Private Person", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
