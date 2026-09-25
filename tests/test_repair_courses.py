"""Repair safety tests using synthetic identifiers and in-memory datasets only."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.repair_data import Course, Dataset, Placement, Room
from scripts.repair_courses import RepairConfig, repair


WEEKS = frozenset({1, 2})


def course(identifier, **changes):
    values = dict(id=identifier, name=identifier, hours=2.0, weeks=WEEKS,
                  teachers={"T_" + identifier: WEEKS},
                  classes=frozenset({"G_" + identifier}), campus="CAMPUS",
                  capacity=30.0, room_type="LECTURE")
    values.update(changes)
    return Course(**values)


def room(identifier="R1", **changes):
    values = dict(id=identifier, name=identifier, campus="CAMPUS", capacity=50.0,
                  type="LECTURE", building="BUILDING", enabled=True)
    values.update(changes)
    return Room(**values)


def placed(identifier, periods=(1, 2), day=0, room_id="R1", weeks=WEEKS):
    return Placement(identifier, weeks, day, periods, room_id)


def dataset(courses, placements=(), rooms=None):
    return Dataset(courses={c.id: c for c in courses},
                   rooms={r.id: r for r in (rooms or [room()])},
                   placements=list(placements))


def policy(**changes):
    values = dict(days=[0], periods=[1, 2, 3, 4])
    values.update(changes)
    return RepairConfig(**values)


def final_for(result, identifier):
    return [p for p in result["placements"] if p["course_id"] == identifier]


class ResourceSafetyTests(unittest.TestCase):
    def test_unknown_baseline_weeks_freeze_room_beyond_twentieth_week(self):
        a = course("A", weeks=frozenset(), teachers={"OLD_TEACHER": frozenset()})
        b = course("B", weeks=frozenset({30}), teachers={"NEW_TEACHER": frozenset({30})})
        data = dataset([a, b], [placed("A", weeks=frozenset())])
        result = repair(data, ["B"], policy())
        self.assertEqual(result["summary"]["inserted"], 0)

    def test_unknown_baseline_periods_reserve_known_resources(self):
        a, b = course("A"), course("B")
        data = dataset([a, b], [placed("A", periods=())])
        result = repair(data, ["B"], policy(movable_ids=["A"], max_moved_courses=1))
        self.assertEqual(result["summary"]["inserted"], 0)
        self.assertEqual(result["summary"]["moved"], 0)

    def test_each_teacher_uses_own_weeks_not_course_week_union(self):
        a = course("A", teachers={"SHARED": frozenset({1}), "OTHER_A": frozenset({2})})
        b = course("B", teachers={"OTHER_B": frozenset({1}), "SHARED": frozenset({2})},
                   explicit_rooms=frozenset({"R2"}), prefer="周一(1-2节)")
        data = dataset([a, b], [placed("A")], [room(), room("R2")])
        result = repair(data, ["B"], policy())
        self.assertEqual(result["summary"]["inserted"], 1)
        self.assertEqual(final_for(result, "B")[0]["periods"], [1, 2])

    def test_teacher_overlap_in_one_week_is_still_a_conflict(self):
        a = course("A", teachers={"SHARED": frozenset({1}), "OTHER_A": frozenset({2})})
        b = course("B", teachers={"SHARED": WEEKS},
                   explicit_rooms=frozenset({"R2"}), prefer="周一(1-2节)")
        data = dataset([a, b], [placed("A")], [room(), room("R2")])
        result = repair(data, ["B"], policy())
        self.assertEqual(result["summary"]["inserted"], 0)

    def test_full_class_identifiers_prevent_cross_room_collision(self):
        a = course("A", classes=frozenset({"GROUP_FULL_ID"}))
        b = course("B", classes=frozenset({"GROUP_FULL_ID"}),
                   explicit_rooms=frozenset({"R2"}), prefer="周一(1-2节)")
        result = repair(dataset([a, b], [placed("A")], [room(), room("R2")]), ["B"], policy())
        self.assertEqual(result["summary"]["inserted"], 0)

    def test_capacity_and_room_type_are_not_relaxable(self):
        for change in ({"capacity": 51.0}, {"room_type": "LAB"}):
            with self.subTest(change=change):
                c = course("TARGET", **change)
                result = repair(dataset([c]), [c.id], policy(allowed_changes=[
                    "time_preference", "building_preference", "historical_room"]))
                self.assertEqual(result["summary"]["inserted"], 0)
                self.assertEqual(result["changes"], [])

    def test_explicit_room_is_not_a_historical_room_preference(self):
        c = course("TARGET", explicit_rooms=frozenset({"NONEXISTENT"}))
        result = repair(dataset([c]), [c.id], policy(allowed_changes=["historical_room"]))
        self.assertEqual(result["summary"]["inserted"], 0)

    def test_teacher_week_coverage_must_be_complete(self):
        c = course("TARGET", teachers={"TEACHER": frozenset({1})})
        result = repair(dataset([c]), [c.id], policy())
        self.assertEqual(result["summary"]["inserted"], 0)

    def test_batch_targets_cannot_both_claim_one_class_slot(self):
        a = course("A", classes=frozenset({"SHARED"}), prefer="周一(1-2节)")
        b = course("B", classes=frozenset({"SHARED"}), prefer="周一(1-2节)")
        result = repair(dataset([a, b], rooms=[room(), room("R2")]), ["A", "B"], policy())
        self.assertEqual(result["summary"]["inserted"], 1)


class ConstraintPolicyTests(unittest.TestCase):
    def test_recognized_college_requirement_needs_review_for_time_relaxation(self):
        a = course("A")
        c = course("TARGET", prefer="周一(1-2节)", special="学院：计算机 资环；时间：周一1.2")
        data = dataset([a, c], [placed("A")])
        strict = repair(data, [c.id], policy(allowed_changes=["time_preference"]))
        reviewed = repair(data, [c.id], policy(allowed_changes=["time_preference"],
                          reviewed_soft_time_ids=[c.id]))
        self.assertEqual(strict["summary"]["inserted"], 0)
        self.assertEqual(reviewed["summary"]["inserted"], 1)
        self.assertEqual(final_for(reviewed, c.id)[0]["periods"], [3, 4])
        self.assertIn("reviewed_special_time_requirement", reviewed["changes"][0]["relaxed_fields"])

    def test_reviewed_special_time_does_not_authorize_building_change(self):
        c = course("TARGET", prefer="周一(1-2节)", special="学院：计算机 资环；时间：周一1.2",
                   buildings=frozenset({"OTHER_BUILDING"}))
        result = repair(dataset([c]), [c.id], policy(
            allowed_changes=["time_preference", "building_preference", "historical_room"],
            reviewed_soft_time_ids=[c.id]))
        self.assertEqual(result["summary"]["inserted"], 0)

    def test_ordinary_time_preference_changes_only_when_authorized(self):
        c = course("TARGET", prefer="周一(2-3节)")
        data = dataset([c])
        strict = repair(data, [c.id], policy())
        relaxed = repair(data, [c.id], policy(allowed_changes=["time_preference"]))
        self.assertEqual(strict["summary"]["inserted"], 0)
        self.assertEqual(relaxed["summary"]["inserted"], 1)
        self.assertIn("time_preference", relaxed["changes"][0]["relaxed_fields"])

    def test_unknown_special_requirements_are_not_assumed_satisfied(self):
        for prefer in ("", "周一(1-2节)"):
            for reviewed in ([], ["TARGET"]):
                with self.subTest(prefer=prefer, reviewed=reviewed):
                    c = course("TARGET", prefer=prefer, special="需两间机房联合上课")
                    result = repair(dataset([c]), [c.id], policy(
                        allowed_changes=["time_preference", "building_preference", "historical_room"],
                        reviewed_soft_time_ids=reviewed))
                    self.assertEqual(result["summary"]["inserted"], 0)

    def test_course_unavailability_is_not_relaxed_by_time_preference_permission(self):
        c = course("TARGET", prefer="周一(1-2节)", unavailable="周一全天")
        result = repair(dataset([c]), [c.id], policy(allowed_changes=["time_preference"]))
        self.assertEqual(result["summary"]["inserted"], 0)

    def test_school_forbidden_periods_are_preserved(self):
        c = course("TARGET", prefer="周二(5-6节)")
        result = repair(dataset([c]), [c.id], policy(days=[1], periods=[5, 6],
                        allowed_changes=["time_preference"],
                        time_scope="weekdays"))
        self.assertEqual(result["summary"]["inserted"], 0)


class TransactionAndBudgetTests(unittest.TestCase):
    def test_candidate_generation_observes_expired_deadline(self):
        c = course("TARGET", hours=8.0, block_template=(2, 2, 2, 2))
        clock_calls = 0

        def elapsed_clock():
            nonlocal clock_calls
            clock_calls += 1
            return 0.0 if clock_calls == 1 else 100.0

        with patch("scripts.repair_courses.time.monotonic", side_effect=elapsed_clock):
            result = repair(dataset([c]), [c.id], policy(days=list(range(5)),
                            periods=list(range(1, 9)), time_limit_seconds=1.0))
        self.assertEqual(result["summary"]["inserted"], 0)
        self.assertEqual(result["results"][0]["status"], "search_limit")
        self.assertEqual(result["results"][0]["diagnosis"]["reason"], "search_limit")
        self.assertEqual(result["summary"]["search_nodes"], 0)

    def test_one_move_inserts_target_and_keeps_caller_data_unchanged(self):
        a = course("A")
        target = course("TARGET", prefer="周一(1-2节)")
        data = dataset([a, target], [placed("A")])
        snapshot = deepcopy(data.to_dict())
        result = repair(data, [target.id], policy(movable_ids=["A"], max_moved_courses=1))
        self.assertEqual(result["summary"]["inserted"], 1)
        self.assertEqual(result["summary"]["moved"], 1)
        self.assertEqual(final_for(result, "TARGET")[0]["periods"], [1, 2])
        self.assertEqual(final_for(result, "A")[0]["periods"], [3, 4])
        self.assertEqual(data.to_dict(), snapshot)

    def test_dead_end_restores_all_previously_moved_blockers(self):
        a = course("A", teachers={"SHARED": WEEKS}, explicit_rooms=frozenset({"R2"}))
        b = course("B", prefer="周一(1-2节)", explicit_rooms=frozenset({"R1"}))
        target = course("TARGET", teachers={"SHARED": WEEKS}, prefer="周一(1-2节)",
                        explicit_rooms=frozenset({"R1"}))
        initial = [placed("A", room_id="R2"), placed("B")]
        data = dataset([a, b, target], initial, [room(), room("R2")])
        result = repair(data, [target.id], policy(movable_ids=["A", "B"], max_moved_courses=2))
        self.assertEqual(result["summary"]["inserted"], 0)
        self.assertEqual(result["summary"]["moved"], 0)
        self.assertEqual(result["changes"], [])
        self.assertEqual(final_for(result, "A")[0]["periods"], [1, 2])
        self.assertEqual(final_for(result, "B")[0]["periods"], [1, 2])
        self.assertEqual(data.placements, initial)

    def test_movement_budget_is_for_entire_batch(self):
        a, b = course("A"), course("B")
        x = course("X", prefer="周一(1-2节)")
        y = course("Y", prefer="周一(5-6节)")
        data = dataset([a, b, x, y], [placed("A"), placed("B", periods=(5, 6))])
        result = repair(data, ["X", "Y"], policy(periods=list(range(1, 9)),
                        movable_ids=["A", "B"], max_moved_courses=1))
        self.assertEqual(result["summary"]["inserted"], 1)
        self.assertEqual(result["summary"]["moved"], 1)
        self.assertEqual(len([c for c in result["changes"] if c["operation"] == "move"]), 1)

    def test_locked_blocker_never_moves(self):
        a, target = course("A"), course("TARGET", prefer="周一(1-2节)")
        result = repair(dataset([a, target], [placed("A")]), ["TARGET"],
                        policy(movable_ids=["A"], locked_ids=["A"], max_moved_courses=1))
        self.assertEqual(result["summary"]["inserted"], 0)
        self.assertEqual(result["changes"], [])

    def test_node_budget_is_not_reset_between_targets(self):
        a = course("A", prefer="周一(1-2节)")
        b = course("B", prefer="周一(3-4节)")
        result = repair(dataset([a, b]), ["A", "B"], policy(max_search_nodes=1))
        self.assertEqual(result["summary"]["inserted"], 1)
        self.assertLessEqual(result["summary"]["search_nodes"], 1)
        self.assertTrue(any(r["status"] == "search_limit" for r in result["results"]))


class CompleteCourseTests(unittest.TestCase):
    def test_weekly_load_must_match_known_total_course_hours(self):
        c = course("TARGET", hours=2.0, total_hours=6.0)
        result = repair(dataset([c]), [c.id], policy())
        self.assertEqual(result["summary"]["inserted"], 0)
        self.assertEqual(result["results"][0]["status"], "source_hours_inconsistent")

    def test_multiple_blocks_preserve_every_weeks_complete_hours(self):
        c = course("TARGET", hours=4.0, block_template=(2, 2))
        result = repair(dataset([c]), [c.id], policy(days=[0, 2]))
        self.assertEqual(result["summary"]["inserted"], 1)
        entries = final_for(result, c.id)
        self.assertEqual(len(entries), 2)
        for week in WEEKS:
            cells = {(p["day"], period) for p in entries if week in p["weeks"] for period in p["periods"]}
            self.assertEqual(len(cells), 4)

    def test_one_available_block_does_not_count_as_four_hour_success(self):
        c = course("TARGET", hours=4.0, block_template=(2, 2))
        result = repair(dataset([c]), [c.id], policy(days=[0], periods=[1, 2]))
        self.assertEqual(result["summary"]["inserted"], 0)
        self.assertEqual(final_for(result, c.id), [])

    def test_moving_multiblock_course_does_not_drop_its_other_block(self):
        a = course("A", hours=4.0, block_template=(2, 2))
        target = course("TARGET", prefer="周一(1-2节)")
        initial = [placed("A"), placed("A", day=2)]
        result = repair(dataset([a, target], initial), [target.id], policy(days=[0, 2],
                        movable_ids=["A"], max_moved_courses=1))
        self.assertEqual(result["summary"]["inserted"], 1)
        self.assertEqual(result["summary"]["moved"], 1)
        self.assertEqual(len(final_for(result, "A")), 2)
        for week in WEEKS:
            cells = {(p["day"], period) for p in final_for(result, "A")
                     if week in p["weeks"] for period in p["periods"]}
            self.assertEqual(len(cells), 4)


if __name__ == "__main__":
    unittest.main()
