"""Explicit class-check exemptions must not suppress other input validation."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.repair_data import Dataset, Placement, Room, _load_courses
from scripts.repair_courses import keys_for, repair


def source_row(**changes):
    row = dict(JXBID="C1", KCM="Example", ZXS="2", LLXS="4", KRL="20",
               SKXQ="南校区", SKZCDM="11", JSH="T1", RWJSZCDM="11",
               TJBJ="班级2401", IF_ROOM_CONFICT="0", IF_CLASS_CONFICT="0")
    row.update(changes)
    return row


def load_one(row, registry=frozenset({"班级2401"})):
    data = Dataset()
    _load_courses([(3, row)], data, registry)
    return data, data.courses["C1"]


class IgnoreClassLoadingTests(unittest.TestCase):
    def test_explicit_exemption_allows_missing_generic_malformed_and_unknown_classes(self):
        cases = [
            ("", frozenset()),
            ("全校 全院", frozenset({"全校", "全院"})),
            ("电气学院，周一3.48", frozenset({"电气学院", "周一3.48"})),
            ("未注册2402", frozenset({"未注册2402"})),
        ]
        for flag in ("1", "1.0", 1, 1.0):
            for text, expected in cases:
                with self.subTest(flag=flag, text=text):
                    data, course = load_one(source_row(TJBJ=text, IF_CLASS_CONFICT=flag))
                    self.assertFalse(course.check_class)
                    self.assertFalse(course.issues)
                    self.assertEqual(course.classes, expected)
                    self.assertEqual(course.original_class_values, (text,))
                    self.assertEqual(data.to_dict()["courses"]["C1"]["original_class_values"], [text])

    def test_missing_zero_or_blank_flag_keeps_strict_coverage_checks(self):
        for flag in ("0", 0, "0.0", None, "", "omitted"):
            for text in ("", "全校", "电气学院，周一3.48", "未注册2402"):
                with self.subTest(flag=flag, text=text):
                    row = source_row(TJBJ=text, IF_CLASS_CONFICT=flag)
                    if flag == "omitted":
                        row.pop("IF_CLASS_CONFICT")
                    _, course = load_one(row)
                    self.assertTrue(course.check_class)
                    self.assertTrue(any(issue.startswith("coverage:") for issue in course.issues))
                    self.assertEqual(course.original_class_values, (text,))

    def test_invalid_flags_never_authorize_an_exemption(self):
        for flag in ("false", False, "ignore", 2):
            with self.subTest(flag=flag):
                _, course = load_one(source_row(TJBJ="全校", IF_CLASS_CONFICT=flag))
                self.assertTrue(course.check_class)
                self.assertTrue(any("invalid IF_CLASS_CONFICT" in issue for issue in course.issues))
                self.assertTrue(any(issue.startswith("coverage:") for issue in course.issues))

    def test_strict_generic_filter_retains_original_text_for_later_audit(self):
        _, course = load_one(source_row(TJBJ="班级2401，全校"))
        self.assertEqual(course.classes, frozenset({"班级2401"}))
        self.assertEqual(course.original_class_values, ("班级2401，全校",))
        self.assertTrue(any("generic class scopes" in issue for issue in course.issues))

    def test_exemption_does_not_hide_teacher_week_or_other_metadata_errors(self):
        cases = [
            ({"JSH": ""}, "missing teacher identifier"),
            ({"SKZCDM": "invalid"}, "SKZCDM"),
            ({"SKZCDM": "00"}, "no active teaching weeks"),
            ({"RWJSZCDM": "invalid"}, "RWJSZCDM"),
            ({"RWJSZCDM": "00"}, "no responsibility weeks"),
            ({"KRL": "-1"}, "invalid KRL"),
            ({"SKXQ": ""}, "missing campus"),
            ({"LLXS": "invalid"}, "invalid LLXS"),
            ({"Prefer_Time": "周一3.4"}, "constraint: Prefer_Time"),
        ]
        for change, expected in cases:
            with self.subTest(change=change):
                _, course = load_one(source_row(TJBJ="全校", IF_CLASS_CONFICT="1", **change))
                self.assertFalse(course.check_class)
                self.assertTrue(any(expected in issue for issue in course.issues))
                self.assertFalse(any(issue.startswith("coverage:") for issue in course.issues))

    def test_conflicting_source_rows_remain_reported_and_preserve_all_text(self):
        data = Dataset()
        _load_courses([(3, source_row(TJBJ="全校", IF_CLASS_CONFICT="1")),
                       (4, source_row(TJBJ="周一3.4", IF_CLASS_CONFICT="0", KRL="21"))],
                      data, frozenset({"班级2401"}))
        course = data.courses["C1"]
        for field in ("TJBJ", "IF_CLASS_CONFICT", "KRL"):
            self.assertTrue(any(f"inconsistent {field}" in issue for issue in course.issues))
        self.assertEqual(course.classes, frozenset({"全校", "周一3.4"}))
        self.assertEqual(course.original_class_values, ("全校", "周一3.4"))

    def test_exempt_course_can_be_fully_inserted_but_keeps_teacher_and_room_checks(self):
        data, course = load_one(source_row(TJBJ="全校", IF_CLASS_CONFICT="1"))
        data.rooms["R1"] = Room("R1", campus="南校区", capacity=20, enabled=True)
        config = {"allowed_changes": [], "days": [0], "periods": [1, 2]}
        proposed = repair(data, ["C1"], config)
        self.assertEqual(proposed["summary"]["inserted"], 1)
        self.assertEqual(proposed["validation"]["new_conflicts"], 0)
        self.assertTrue(proposed["validation"]["hours_and_weeks_preserved"])
        entry = Placement("C1", frozenset({1, 2}), 0, (1, 2), "R1")
        self.assertEqual({key[0] for key in keys_for(course, entry)}, {"teacher", "room"})
        _load_courses([(4, source_row(JXBID="C2", TJBJ="", IF_CLASS_CONFICT="1"))], data)
        data.placements.append(Placement("C2", frozenset({1, 2}), 0, (1, 2), "R1"))
        blocked = repair(data, ["C1"], config)
        self.assertEqual(blocked["summary"]["inserted"], 0)
        self.assertEqual(data.courses["C1"].classes, frozenset({"全校"}))


if __name__ == "__main__":
    unittest.main()
