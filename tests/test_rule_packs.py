"""Declarative, same-campus room-substitution rule pack tests."""
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

from openpyxl import Workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.repair_courses import RepairConfig, repair
from scripts.repair_data import Course, Dataset, Room
from scripts.repair_rule_packs import RulePackError, apply_rule_pack, load_rule_pack
from scripts.timetable_agent_tools import dispatch_tool


def course(identifier="TARGET", **changes):
    values = dict(id=identifier, name=identifier, hours=2, weeks=frozenset({1}),
                  teachers={"T": frozenset({1})}, classes=frozenset({"C"}),
                  campus="NORTH", capacity=20, room_type="LAB")
    values.update(changes)
    return Course(**values)


def pack(course_ids=("TARGET",), substitute_types=("LECTURE",)):
    return {"schema_version": 1, "name": "教务确认的同校区类型替代", "rules": [{
        "id": "north-lab-to-lecture", "kind": "room_type_substitution",
        "course_ids": list(course_ids), "substitute_room_types": list(substitute_types),
        "evidence": "2026 秋季教务处场地调配单 #18",
    }]}


class RulePackTests(unittest.TestCase):
    def write_pack(self, root, value):
        path = Path(root) / "rules.json"
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def test_substitution_is_course_scoped_and_preserves_original_course(self):
        original = course()
        data = Dataset(courses={original.id: original}, rooms={
            "SAME": Room("SAME", campus="NORTH", capacity=30, type="LECTURE", enabled=True),
            "OTHER": Room("OTHER", campus="SOUTH", capacity=30, type="LECTURE", enabled=True),
        })
        with TemporaryDirectory() as temporary:
            effective, summary = apply_rule_pack(data, load_rule_pack(self.write_pack(temporary, pack())))
        self.assertEqual(data.courses["TARGET"].allowed_room_types, frozenset())
        self.assertEqual(effective.courses["TARGET"].allowed_room_types, frozenset({"LAB", "LECTURE"}))
        result = repair(effective, ["TARGET"], RepairConfig(days=[0], periods=[1, 2]))
        self.assertEqual(result["summary"]["inserted"], 1)
        self.assertEqual(result["changes"][0]["after"][0]["room_id"], "SAME")
        self.assertEqual(summary["rules"][0]["campus_constraint"], "same_campus_only")
        self.assertIn("cross-campus placement is never allowed", summary["interpretation"])

    def test_rule_pack_rejects_cross_campus_and_unreviewable_forms(self):
        cases = [
            {**pack(), "campus": "SOUTH"},
            {**pack(), "rules": [{**pack()["rules"][0], "campus": "SOUTH"}]},
            {**pack(), "rules": [{**pack()["rules"][0], "substitute_room_types": ["LECTURE", "LECTURE"]}]},
            {**pack(), "rules": [{**pack()["rules"][0], "course_ids": []}]},
        ]
        with TemporaryDirectory() as temporary:
            for index, value in enumerate(cases):
                with self.subTest(index=index):
                    path = self.write_pack(temporary, value)
                    with self.assertRaises(RulePackError):
                        load_rule_pack(path)

    def test_rule_pack_rejects_unknown_courses(self):
        data = Dataset(courses={"TARGET": course()})
        with TemporaryDirectory() as temporary:
            loaded = load_rule_pack(self.write_pack(temporary, pack(course_ids=("MISSING",))))
        with self.assertRaisesRegex(RulePackError, "unknown course"):
            apply_rule_pack(data, loaded)

    def test_rule_pack_rejects_repeating_the_source_type_when_applied(self):
        data = Dataset(courses={"TARGET": course()})
        with TemporaryDirectory() as temporary:
            loaded = load_rule_pack(self.write_pack(temporary, pack(substitute_types=("LAB",))))
        with self.assertRaisesRegex(RulePackError, "repeats source room type"):
            apply_rule_pack(data, loaded)

    def test_tool_records_rule_pack_hash_and_applied_rules(self):
        def sheet(path, headers, rows, double=False):
            book = Workbook(); active = book.active
            if double:
                active.append(headers)
            active.append(headers)
            for row in rows:
                active.append(row)
            book.save(path); book.close()

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            course_path, room_path = root / "courses.xlsx", root / "rooms.xlsx"
            schedule_path, failed_path = root / "schedule.xlsx", root / "failed.xlsx"
            headers = ["JXBID", "KCM", "ZXS", "LLXS", "KRL", "SKXQ", "SKZCDM", "JSH", "RWJSZCDM", "TJBJ", "JASLXMC"]
            sheet(course_path, headers, [["TARGET", "Target", 2, 2, 20, "NORTH", "1", "T", "1", "C", "LAB"]], True)
            sheet(room_path, ["JASDM", "JASMC", "MC", "SKZWS", "SFYXPK", "JASLX"], [["R1", "R1", "NORTH", 30, 1, "LECTURE"]], True)
            sheet(schedule_path, ["教学班ID", "教室代码", "上课周次", "星期", "节次"], [])
            sheet(failed_path, ["jxbid"], [["TARGET"]])
            sheet(root / "班级表.xlsx", ["BJMC"], [["C"]])
            rule_path = self.write_pack(root, pack())
            response = dispatch_tool("propose_repair", {
                "course_path": str(course_path), "room_path": str(room_path),
                "schedule_path": str(schedule_path), "failed_path": str(failed_path),
                "rule_pack_path": str(rule_path),
                "config": {"allowed_changes": [], "days": [0], "periods": [1, 2]},
            })
        self.assertTrue(response["ok"], response)
        proposal = response["result"]["proposal"]
        self.assertEqual(proposal["summary"]["inserted"], 1)
        self.assertIn("rule_pack", proposal["source_hashes"])
        self.assertEqual(proposal["rule_pack"]["rules"][0]["id"], "north-lab-to-lecture")
