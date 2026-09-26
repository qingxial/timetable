"""Regression tests for ambiguous input and resource ownership across rows."""
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.repair_data import (
    Dataset, Room, _load_courses, _load_placements, _load_rooms,
    parse_time_windows, load_dataset,
)


def course_row(**changes):
    row = dict(JXBID="C1", KCM="Example", ZXS="2", KRL="40", SKXQ="南校区",
               SKZCDM="1100", JSH="T1", RWJSZCDM="1100", TJBJ="班级2401",
               IF_ROOM_CONFICT="0", IF_CLASS_CONFICT="0")
    row.update(changes)
    return row


def schedule_row(**changes):
    row = {"教学班ID": "C1", "教室代码": "R1", "上课周次": "1,2",
           "星期": "周一", "节次": "第1-2节"}
    row.update(changes)
    return row


class TimeWindowsTests(unittest.TestCase):
    def test_canonical_and_frontend_group(self):
        value = "[周一（1-2节）、周三(3-4节)];【周日全天】"
        expected = {(0, 1), (0, 2), (2, 3), (2, 4)} | {(6, p) for p in range(1, 12)}
        self.assertEqual(parse_time_windows(value), expected)
        self.assertEqual(len(parse_time_windows("全周（1-2节）")), 14)
        self.assertEqual(parse_time_windows(""), set())

    def test_partial_invalid_and_out_of_range_input_is_rejected(self):
        for value in ("周一(1-2节)请尽量", "周一3.4", "周一(1-0节)", "周一(1-12节)",
                      "周一(1-2节);未知时间", "[周一(1-2节)", "[]", "周一(1-2节);;周二全天"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_time_windows(value)


class InputAggregationTests(unittest.TestCase):
    def test_registry_accepts_known_class_without_digits(self):
        dataset = Dataset()
        _load_courses([(3, course_row(TJBJ="实验班"))], dataset, frozenset({"实验班"}))
        self.assertEqual(dataset.courses["C1"].classes, frozenset({"实验班"}))
        self.assertFalse(dataset.courses["C1"].issues)

    def test_unknown_registry_tokens_retained_and_explicit_exemption_respected(self):
        dataset = Dataset()
        _load_courses([(3, course_row(TJBJ="班级2401 错误2402")),
                       (4, course_row(JXBID="C2", TJBJ="错误2402", IF_CLASS_CONFICT="1"))],
                      dataset, frozenset({"班级2401"}))
        self.assertEqual(dataset.courses["C1"].classes, frozenset({"班级2401", "错误2402"}))
        self.assertTrue(any("absent from BJMC registry" in i for i in dataset.courses["C1"].issues))
        self.assertFalse(dataset.courses["C2"].check_class)
        self.assertFalse(dataset.courses["C2"].issues)

    def test_registry_autodiscovery_hash_and_missing_registry_coverage(self):
        from hashlib import sha256
        from openpyxl import Workbook

        def fixture(path, row, double_header):
            workbook = Workbook()
            sheet = workbook.active
            if double_header:
                sheet.append(list(row))
            sheet.append(list(row))
            sheet.append(list(row.values()))
            workbook.save(path)
            workbook.close()

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            course_path, room_path, schedule_path = (root / f"{name}.xlsx" for name in ("course", "room", "schedule"))
            fixture(course_path, course_row(TJBJ="实验班"), True)
            fixture(room_path, dict(JASDM="R1", JASMC="101", MC="南校区", SFYXPK="1", SKZWS="40"), True)
            fixture(schedule_path, schedule_row(), False)
            class_path = root / "班级表.xlsx"
            fixture(class_path, {"BJMC": "实验班"}, False)
            dataset = load_dataset(course_path, room_path, schedule_path)
            self.assertFalse(dataset.courses["C1"].issues)
            self.assertEqual(dataset.source_hashes["classes"], sha256(class_path.read_bytes()).hexdigest())
            renamed = root / "custom_registry.xlsx"
            class_path.rename(renamed)
            explicit = load_dataset(course_path, room_path, schedule_path, renamed)
            self.assertFalse(explicit.courses["C1"].issues)
            fallback = load_dataset(course_path, room_path, schedule_path)
            self.assertNotIn("classes", fallback.source_hashes)
            self.assertTrue(any("class registry unavailable" in i for i in fallback.issues))
            self.assertTrue(any("unresolved class identifiers" in i for i in fallback.courses["C1"].issues))

    def test_total_hours_require_consistent_positive_source_values(self):
        dataset = Dataset()
        _load_courses([(3, course_row(LLXS="32")),
                       (4, course_row(LLXS=32.0)),
                       (5, course_row(JXBID="C2", LLXS="")),
                       (6, course_row(JXBID="C3", LLXS="0")),
                       (7, course_row(JXBID="C4", LLXS="16")),
                       (8, course_row(JXBID="C4", LLXS="32")),
                       (9, course_row(JXBID="C5", LLXS="invalid"))], dataset)
        self.assertEqual(dataset.courses["C1"].total_hours, 32.0)
        self.assertFalse(dataset.courses["C1"].issues)
        for identifier in ("C2", "C3", "C4", "C5"):
            self.assertIsNone(dataset.courses[identifier].total_hours)
        self.assertTrue(any("inconsistent LLXS" in issue for issue in dataset.courses["C4"].issues))
        self.assertTrue(any("invalid LLXS" in issue for issue in dataset.courses["C5"].issues))

    def test_teacher_responsibility_is_union_intersect_course_weeks(self):
        dataset = Dataset()
        rows = [(3, course_row(SKZCDM="1100", RWJSZCDM="1010")),
                (4, course_row(SKZCDM="0010", RWJSZCDM="0101")),
                (5, course_row(JSH="T2", SKZCDM="0010", RWJSZCDM="0011"))]
        _load_courses(rows, dataset)
        course = dataset.courses["C1"]
        self.assertEqual(course.weeks, frozenset({1, 2, 3}))
        self.assertEqual(course.teachers, {"T1": frozenset({1, 2, 3}), "T2": frozenset({3})})
        self.assertFalse(course.issues)

    def test_many_classes_and_original_conflict_flags_are_retained(self):
        dataset = Dataset()
        classes = [f"班级24{i:02d}" for i in range(10)]
        _load_courses([(3, course_row(TJBJ="，".join(classes) + " 全校 全院",
                                    IF_ROOM_CONFICT="1", IF_CLASS_CONFICT="0"))], dataset)
        course = dataset.courses["C1"]
        self.assertEqual(course.classes, frozenset(classes))
        self.assertFalse(course.check_room)
        self.assertTrue(course.check_class)
        self.assertTrue(any(i.startswith("coverage:") for i in course.issues))

    def test_conflicting_shared_constraints_and_invalid_time_are_diagnostics(self):
        dataset = Dataset()
        _load_courses([(3, course_row(Prefer_Time="周一(1-2节)")),
                       (4, course_row(Prefer_Time="周二3.4"))], dataset)
        issues = dataset.courses["C1"].issues
        self.assertTrue(any("inconsistent Prefer_Time" in i for i in issues))
        self.assertTrue(any(i.startswith("constraint:") for i in issues))

    def test_inconsistent_classes_preserve_union_for_baseline_occupancy(self):
        dataset = Dataset()
        _load_courses([(3, course_row(TJBJ="班级2401")),
                       (4, course_row(TJBJ="班级2402"))], dataset)
        course = dataset.courses["C1"]
        self.assertEqual(course.classes, frozenset({"班级2401", "班级2402"}))
        self.assertTrue(any("inconsistent TJBJ" in issue for issue in course.issues))

    def test_room_and_weeks_are_per_placement_and_duplicate_teachers_deduplicate(self):
        dataset = Dataset(rooms={"R1": Room("R1"), "R2": Room("R2")})
        _load_courses([(3, course_row())], dataset)
        rows = [(2, schedule_row()), (3, schedule_row()),
                (4, schedule_row(**{"教室代码": "R2", "上课周次": "3,4"}))]
        _load_placements(rows, dataset)
        self.assertEqual(len(dataset.placements), 2)
        self.assertEqual([(p.room_id, p.weeks) for p in dataset.placements],
                         [("R1", frozenset({1, 2})), ("R2", frozenset({3, 4}))])
        json.dumps(dataset.to_dict(), ensure_ascii=False, allow_nan=False)

    def test_invalid_baseline_is_preserved_without_fabricated_room_or_weeks(self):
        dataset = Dataset()
        _load_placements([(2, schedule_row(**{"教室代码": "UNKNOWN", "上课周次": "", "节次": "第1-0节"}))], dataset)
        placement = dataset.placements[0]
        self.assertEqual(placement.room_id, "UNKNOWN")
        self.assertEqual(placement.weeks, frozenset())
        self.assertEqual(placement.periods, ())
        self.assertEqual(dataset.rooms, {})
        self.assertIn("baseline:row 2", dataset.issues[0])
        self.assertIn("missing course metadata", dataset.issues[0])

    def test_only_explicit_zero_hour_reversed_export_rows_are_placeholders(self):
        dataset = Dataset(rooms={"R1": Room("R1")})
        _load_courses([(3, course_row())], dataset)
        rows = [(2, schedule_row(**{"周学时": "0", "节次": "第1-0节"})),
                (3, schedule_row(**{"周学时": 0.0, "节次": "第3-2节"})),
                (4, schedule_row(**{"周学时": "2", "节次": "第3-2节"})),
                (5, schedule_row(**{"周学时": "", "节次": "第1-0节"})),
                (6, schedule_row(**{"周学时": "0", "节次": "第1-2节"}))]
        _load_placements(rows, dataset)
        self.assertEqual(sum(i.startswith("baseline:zero_hour_placeholder") for i in dataset.issues), 2)
        # Two unknown/nonzero invalid rows deduplicate into one invalid placement;
        # a valid interval is never discarded on the strength of a zero alone.
        self.assertEqual(len(dataset.placements), 2)
        self.assertEqual(dataset.placements[0].periods, ())
        self.assertEqual(dataset.placements[1].periods, (1, 2))
        self.assertEqual(sum(i.startswith("baseline:row") for i in dataset.issues), 2)

    def test_shared_room_marker_requires_disabled_room_conflict_check(self):
        for flag in ("0", "1"):
            with self.subTest(flag=flag):
                dataset = Dataset()
                _load_courses([(3, course_row(IF_ROOM_CONFICT=flag))], dataset)
                _load_placements([(2, schedule_row(**{"教室代码": "(共占)"}))], dataset)
                expected_room = "" if flag == "1" else "(共占)"
                self.assertEqual(dataset.placements[0].room_id, expected_room)
                self.assertEqual(bool(dataset.issues), flag == "0")
                self.assertEqual(dataset.rooms, {})

    def test_unknown_real_room_stays_an_error_even_for_shared_course(self):
        dataset = Dataset()
        _load_courses([(3, course_row(IF_ROOM_CONFICT="1"))], dataset)
        _load_placements([(2, schedule_row(**{"教室代码": "UNKNOWN"}))], dataset)
        self.assertEqual(dataset.placements[0].room_id, "UNKNOWN")
        self.assertIn("missing room metadata", dataset.issues[0])

    def test_conflicting_duplicate_room_is_disabled(self):
        dataset = Dataset()
        base = dict(JASDM="R1", JASMC="101", MC="南校区", SFYXPK="1", SKZWS="40")
        _load_rooms([(3, base), (4, dict(base, SKZWS="60"))], dataset)
        self.assertFalse(dataset.rooms["R1"].enabled)
        self.assertTrue(dataset.issues)


if __name__ == "__main__":
    unittest.main()
