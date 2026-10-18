"""End-to-end audit and policy tests for the constrained repair agent."""
from hashlib import sha256
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

from openpyxl import Workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.timetable_repair_agent import run_agent


def workbook(path, headers, rows, double=False):
    book = Workbook(); sheet = book.active
    if double:
        sheet.append(headers)
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    book.save(path); book.close()


def sources(root, *, room_type="LECTURE", course_type="LECTURE"):
    course, room = root / "course.xlsx", root / "room.xlsx"
    schedule, failed = root / "schedule.xlsx", root / "failed.xlsx"
    headers = ["JXBID", "KCM", "ZXS", "LLXS", "KRL", "SKXQ", "SKZCDM", "JSH", "RWJSZCDM", "TJBJ", "JASLXMC"]
    workbook(course, headers, [["TARGET", "Target", 2, 2, 20, "NORTH", "1", "T", "1", "C", course_type]], True)
    workbook(room, ["JASDM", "JASMC", "MC", "SKZWS", "SFYXPK", "JASLX"], [["R1", "R1", "NORTH", 30, 1, room_type]], True)
    workbook(schedule, ["教学班ID", "教室代码", "上课周次", "星期", "节次"], [])
    workbook(failed, ["jxbid"], [["TARGET"]])
    workbook(root / "班级表.xlsx", ["BJMC"], [["C"]])
    return {"course_path": str(course), "room_path": str(room), "schedule_path": str(schedule), "failed_path": str(failed),
            "config": {"allowed_changes": [], "days": [0], "periods": [1, 2]}}


class TimetableRepairAgentTests(unittest.TestCase):
    def test_repair_writes_complete_audit_and_leaves_sources_unchanged(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary); arguments = sources(root)
            before = {path.name: sha256(path.read_bytes()).hexdigest() for path in root.glob("*.xlsx")}
            result = run_agent({"request": "在日间试排当前失败课", "intent": "repair", "tool_arguments": arguments}, root / "runs")
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["plan"]["selected_tools"], ["propose_repair"])
            self.assertFalse(result["publication_ready"])
            run_dir = Path(result["run_dir"])
            self.assertEqual({"request.json", "plan.json", "tool-calls.jsonl", "result.json", "manifest.json"}, {p.name for p in run_dir.iterdir()})
            event = json.loads((run_dir / "tool-calls.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(event["tool"], "propose_repair")
            self.assertEqual(event["response"]["result"]["proposal"]["summary"]["inserted"], 1)
            self.assertEqual(before, {path.name: sha256(path.read_bytes()).hexdigest() for path in root.glob("*.xlsx")})

    def test_corrections_are_previewed_before_repair(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary); arguments = sources(root)
            arguments["corrections"] = [{"course_id": "TARGET", "operation": "set_capacity_assumption", "expected_capacity": 20,
                                         "capacity": 19, "assumption_only": True, "evidence": "Authorized synthetic scenario"}]
            result = run_agent({"request": "按 19 人试排", "intent": "repair", "tool_arguments": arguments}, root / "runs")
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["plan"]["selected_tools"], ["preview_data_corrections", "propose_repair"])
            records = [json.loads(line) for line in (Path(result["run_dir"]) / "tool-calls.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual([record["tool"] for record in records], ["preview_data_corrections", "propose_repair"])

    def test_rule_pack_is_saved_and_loaded_by_repair(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary); arguments = sources(root, room_type="LECTURE", course_type="LAB")
            rule_pack = {"schema_version": 1, "name": "North substitute", "rules": [{
                "id": "lab-to-lecture", "kind": "room_type_substitution", "course_ids": ["TARGET"],
                "substitute_room_types": ["LECTURE"], "evidence": "Registrar decision #1"}]}
            result = run_agent({"request": "按确认的同校区替代教室类型试排", "intent": "repair", "tool_arguments": arguments,
                                "rule_pack": rule_pack}, root / "runs")
            self.assertTrue(result["ok"], result)
            self.assertIn("timetable-rule-pack", result["plan"]["active_skills"])
            run_dir = Path(result["run_dir"])
            self.assertTrue((run_dir / "rule-pack.json").is_file())
            self.assertEqual(result["final_response"]["result"]["proposal"]["summary"]["inserted"], 1)

    def test_policy_blocks_weekend_or_publication_without_dispatch(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary); arguments = sources(root)
            arguments["config"]["days"] = [5]
            blocked = run_agent({"request": "周末试排", "intent": "repair", "tool_arguments": arguments}, root / "runs")
            self.assertFalse(blocked["ok"])
            self.assertEqual(blocked["status"], "blocked")
            self.assertEqual(blocked["tool_calls"], 0)
            self.assertIn("allow_weekend", blocked["error"]["message"])
            rejected = run_agent({"request": "发布课表", "intent": "repair", "authorization": {"publication": True},
                                  "tool_arguments": sources(root)}, root / "runs")
            self.assertFalse(rejected["ok"])
            self.assertIn("never publishes", rejected["error"]["message"])

