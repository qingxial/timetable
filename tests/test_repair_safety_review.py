"""Independent safety checks for audited corrections and proposal extension."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.repair_data import Course, Dataset, Placement, Room
from scripts.repair_courses import repair, week_load
from scripts.repair_workflows import apply_data_corrections, repair_with_fallbacks
from scripts.repair_extensions import extend_repair_proposal


WEEKS = frozenset({1, 2})


def course(cid="TARGET", **updates):
    values = dict(id=cid, name=cid, hours=2, total_hours=4, weeks=WEEKS,
                  teachers={"T_" + cid: WEEKS}, classes=frozenset({"CLASS01"}),
                  campus="C", capacity=30)
    values.update(updates)
    return Course(**values)


def data(courses, placements=()):
    return Dataset(courses={c.id: c for c in courses},
                   rooms={rid: Room(id=rid, name=rid, campus="C", capacity=40, enabled=True)
                          for rid in ("R1", "R2")}, placements=list(placements),
                   class_registry=frozenset({"CLASS01", "CLASS02"}),
                   source_hashes={"courses": "a" * 64, "rooms": "b" * 64,
                                  "schedule": "c" * 64, "classes": "d" * 64})


def hours_correction(original_weekly=2.5, total=5, **updates):
    return {"course_id": "TARGET", "operation": "redistribute_hours",
            "expected_weekly_hours": original_weekly, "expected_total_hours": total,
            "authoritative_field": "LLXS", "evidence": "Synthetic explicitly approved teaching plan",
            **updates}


def policy(**updates):
    return {"allowed_changes": [], "days": [0, 2], "periods": [1, 2, 3, 4], **updates}


class CorrectionSafetyReviewTests(unittest.TestCase):
    def test_failed_later_correction_cannot_partially_mutate_input(self):
        source = data([course()])
        original = deepcopy(source)
        changes = [{"course_id": "TARGET", "operation": "set_capacity", "expected_capacity": 30,
                    "capacity": 20, "enrollment_frozen": True, "evidence": "Synthetic frozen roster"},
                   {"course_id": "TARGET", "operation": "replace_classes",
                    "expected_classes": ["CLASS01"], "classes": ["INVENTED_CLASS"], "evidence": "Invalid mapping"}]
        with self.assertRaises(ValueError):
            apply_data_corrections(source, changes)
        self.assertEqual(source, original)

    def test_registry_membership_and_stale_class_precondition_are_mandatory(self):
        source = data([course()])
        for expected, replacement in ((["CLASS01"], ["UNREGISTERED"]), (["CLASS02"], ["CLASS01"])):
            with self.subTest(expected=expected, replacement=replacement), self.assertRaises(ValueError):
                apply_data_corrections(source, [{"course_id": "TARGET", "operation": "replace_classes",
                    "expected_classes": expected, "classes": replacement, "evidence": "Explicit synthetic mapping"}])

    def test_class_correction_does_not_erase_unrelated_metadata_errors(self):
        source = data([course(issues=["coverage: old mapping needs replacement", "metadata: missing teacher"] )])
        corrected, audit = apply_data_corrections(source, [{"course_id": "TARGET", "operation": "replace_classes",
            "expected_classes": ["CLASS01"], "classes": ["CLASS02"], "evidence": "Verified synthetic class mapping"}])
        self.assertEqual(corrected.courses["TARGET"].classes, frozenset({"CLASS02"}))
        self.assertEqual(corrected.courses["TARGET"].issues, ["metadata: missing teacher"])
        self.assertEqual(repair(corrected, ["TARGET"], policy())["summary"]["inserted"], 0)
        self.assertFalse(audit[0]["source_files_modified"])

    def test_enrollment_frozen_is_an_explicit_boolean_not_truthy_data(self):
        source = data([course()])
        for frozen in (False, 1, "true", None):
            with self.subTest(frozen=frozen), self.assertRaises(ValueError):
                apply_data_corrections(source, [{"course_id": "TARGET", "operation": "set_capacity",
                    "expected_capacity": 30, "capacity": 20, "enrollment_frozen": frozen,
                    "evidence": "Synthetic record without valid confirmation"}])

    def test_equal_total_cannot_replace_a_source_week_or_drop_a_week(self):
        source = data([course(hours=2.5, total_hours=5)])
        for loads in ({"1": 3, "3": 2}, {"1": 5, "2": 0}, {"1": 5}):
            with self.subTest(loads=loads), self.assertRaises(ValueError):
                apply_data_corrections(source, [hours_correction(weekly_loads=loads)])

    def test_arithmetic_strategy_cannot_make_a_source_week_inactive(self):
        source = data([course(hours=1, total_hours=2, weeks=frozenset({1, 2, 3}),
                              teachers={"T_TARGET": frozenset({1, 2, 3})})])
        with self.assertRaises(ValueError):
            apply_data_corrections(source, [hours_correction(1, 2, strategy="balanced_frontload")])

    def test_distinct_exact_week_assignments_have_distinct_effective_hashes(self):
        source = data([course(hours=2.5, total_hours=5)])
        first, _ = apply_data_corrections(source, [hours_correction(weekly_loads={"1": 3, "2": 2})])
        second, _ = apply_data_corrections(source, [hours_correction(weekly_loads={"1": 2, "2": 3})])
        self.assertNotEqual(first.source_hashes["corrections"], second.source_hashes["corrections"])
        self.assertEqual(first.courses["TARGET"].total_hours, second.courses["TARGET"].total_hours)
        self.assertEqual(first.courses["TARGET"].weeks, source.courses["TARGET"].weeks)

    def variable_week_fixture(self, blocker_week):
        target = course(hours=2.5, total_hours=5, explicit_rooms=frozenset({"R1"}),
                        prefer="周一(1-2节);周三(1-1节)",
                        teachers={"SHARED": frozenset({1}), "SECOND": frozenset({2})})
        old = course("OLD", hours=1, total_hours=1, weeks=frozenset({blocker_week}),
                     teachers={"SHARED": frozenset({blocker_week})}, classes=frozenset({"CLASS02"}))
        source = data([target, old], [Placement("OLD", frozenset({blocker_week}), 2, (1,), "R2")])
        return apply_data_corrections(source, [hours_correction(weekly_loads={"1": 3, "2": 2})])[0]

    def test_new_extra_hour_is_checked_against_responsible_teacher_in_its_week(self):
        result = repair(self.variable_week_fixture(1), ["TARGET"], policy())
        self.assertEqual(result["summary"]["inserted"], 0)
        self.assertEqual(result["placements"], [{"course_id": "OLD", "weeks": [1], "day": 2,
                                                  "periods": [1], "room_id": "R2"}])

    def test_fallback_preserves_total_and_weeks_without_phantom_teacher_conflicts(self):
        corrected = self.variable_week_fixture(2)
        original = deepcopy(corrected)
        result = repair_with_fallbacks(corrected, ["TARGET"], policy(), stages=["daytime"])
        self.assertEqual(result["summary"]["inserted"], 1)
        entries = [Placement(p["course_id"], frozenset(p["weeks"]), p["day"], tuple(p["periods"]), p["room_id"])
                   for p in result["placements"] if p["course_id"] == "TARGET"]
        self.assertEqual(week_load(entries), {1: 3, 2: 2})
        self.assertEqual(sum(week_load(entries).values()), 5)
        self.assertTrue(result["validation"]["hours_and_weeks_preserved"])
        self.assertEqual(corrected, original)

    def test_outside_all_requested_stages_is_not_reported_as_budget_exhaustion(self):
        source = data([course()])
        result = repair_with_fallbacks(source, ["TARGET"], policy(days=[5]), stages=["daytime"])
        self.assertEqual(result["results"][0]["status"], "outside_caller_domain")
        self.assertEqual(result["summary"]["search_nodes"], 0)
        self.assertNotIn("budget exhausted", result["results"][0]["diagnosis"]["detail"].lower())

    def test_one_off_decision_cannot_edit_scheduled_course_or_claim_a_boolean_week(self):
        action = {"course_id": "TARGET", "operation": "set_one_off_week", "expected_weekly_hours": 0,
                  "expected_total_hours": 2, "teaching_week": 1, "weeks_confirmed": True,
                  "evidence": "Synthetic confirmed one-off plan"}
        unscheduled = data([course(hours=0, total_hours=2)])
        scheduled = deepcopy(unscheduled)
        scheduled.placements = [Placement("TARGET", frozenset({1}), 0, (1, 2), "R1")]
        with self.assertRaises(ValueError):
            apply_data_corrections(scheduled, [action])
        with self.assertRaises(ValueError):
            apply_data_corrections(unscheduled, [{**action, "teaching_week": True}])
        for value in (False, 1, "true"):
            with self.subTest(confirmation=value), self.assertRaises(ValueError):
                apply_data_corrections(unscheduled, [{**action, "weeks_confirmed": value}])


class ExtensionSafetyReviewTests(unittest.TestCase):
    def fixture(self):
        source = data([course(prefer="周一(1-2节)"), course("EXTRA", prefer="周三(1-2节)"),
                       course("OLD", classes=frozenset({"CLASS02"}))],
                      [Placement("OLD", WEEKS, 0, (3, 4), "R2")])
        seed = repair(source, ["TARGET"], policy())
        self.assertEqual(seed["summary"]["inserted"], 1)
        return source, seed

    def sync_target_after(self, seed):
        seed["changes"][0]["after"] = deepcopy([p for p in seed["placements"] if p["course_id"] == "TARGET"])

    def test_seed_success_and_original_rows_remain_exactly_unchanged(self):
        source, seed = self.fixture()
        original_source, original_seed = deepcopy(source), deepcopy(seed)
        result = extend_repair_proposal(source, seed, ["EXTRA"], policy(), stages=["daytime"])
        self.assertEqual(result["summary"]["inserted"], 2)
        self.assertEqual(result["summary"]["moved"], 0)
        self.assertEqual(result["summary"]["baseline_scheduled"], 1)
        self.assertEqual(result["scope_extension"]["newly_inserted"], 1)
        self.assertTrue(result["validation"]["seed_successes_preserved"])
        for cid in ("TARGET", "OLD"):
            self.assertEqual([p for p in result["placements"] if p["course_id"] == cid],
                             [p for p in seed["placements"] if p["course_id"] == cid])
        self.assertEqual(source, original_source)
        self.assertEqual(seed, original_seed)

    def test_false_already_scheduled_claim_cannot_hide_an_unscheduled_seed_target(self):
        source, seed = self.fixture()
        seed["results"][0]["status"] = "already_scheduled"
        seed["summary"]["inserted"] = 0
        seed["changes"] = []
        seed["placements"] = [p for p in seed["placements"] if p["course_id"] != "TARGET"]
        with self.assertRaises(ValueError):
            extend_repair_proposal(source, seed, ["EXTRA"], policy())

    def test_seed_cannot_rewrite_baseline_or_disagree_with_change_log(self):
        source, seed = self.fixture()
        tampered_baseline = deepcopy(seed)
        next(p for p in tampered_baseline["placements"] if p["course_id"] == "OLD")["periods"] = [7, 8]
        inconsistent_audit = deepcopy(seed)
        inconsistent_audit["changes"][0]["after"][0]["room_id"] = "R2"
        for candidate in (tampered_baseline, inconsistent_audit):
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                extend_repair_proposal(source, candidate, ["EXTRA"], policy())

    def test_seed_validation_flags_do_not_override_missing_weeks_or_noncanonical_types(self):
        source, seed = self.fixture()
        for weeks in ([1], [True, 2], [2, 1], [1, 1, 2]):
            candidate = deepcopy(seed)
            next(p for p in candidate["placements"] if p["course_id"] == "TARGET")["weeks"] = weeks
            self.sync_target_after(candidate)
            with self.subTest(weeks=weeks), self.assertRaises(ValueError):
                extend_repair_proposal(source, candidate, ["EXTRA"], policy())

    def test_seed_rejects_fractional_load_llxs_mismatch_and_metadata_problems(self):
        for mutation in ("fractional", "total", "metadata", "special", "template"):
            source, seed = self.fixture()
            c = source.courses["TARGET"]
            if mutation == "fractional":
                c.hours, c.total_hours = 2.5, 5
            elif mutation == "total":
                c.total_hours = 5
            elif mutation == "metadata":
                c.issues = ["coverage: unknown student mapping"]
            elif mutation == "special":
                c.special = "Unmodeled mandatory requirement"
            else:
                c.block_template = (1, 1)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                extend_repair_proposal(source, seed, ["EXTRA"], policy())

    def test_seed_rejects_nonconsecutive_blocks_and_even_start_of_two_period_block(self):
        for periods in ([1, 3], [2, 3]):
            source, seed = self.fixture()
            source.courses["TARGET"].prefer = ""
            next(p for p in seed["placements"] if p["course_id"] == "TARGET")["periods"] = periods
            self.sync_target_after(seed)
            with self.subTest(periods=periods), self.assertRaises(ValueError):
                extend_repair_proposal(source, seed, ["EXTRA"], policy(single_period_starts="any"))

    def test_new_policy_must_continue_to_authorize_retained_seed_preference_changes(self):
        source = data([course(prefer="周一(1-2节)"), course("EXTRA", prefer="周一(3-4节)")])
        seed = repair(source, ["TARGET"], policy(days=[2], allowed_changes=["time_preference"],
                                                time_scope="configured_days"))
        self.assertEqual(seed["summary"]["inserted"], 1)
        with self.assertRaises(ValueError):
            extend_repair_proposal(source, seed, ["EXTRA"], policy())

    def test_additional_scope_cannot_repeat_a_seed_target_or_move_existing_courses(self):
        source, seed = self.fixture()
        for targets, config in ((["TARGET"], policy()), (["EXTRA", "EXTRA"], policy()),
                                (["EXTRA"], policy(movable_ids=["OLD"], max_moved_courses=1))):
            with self.subTest(targets=targets, config=config), self.assertRaises(ValueError):
                extend_repair_proposal(source, seed, targets, config)


if __name__ == "__main__":
    unittest.main()
