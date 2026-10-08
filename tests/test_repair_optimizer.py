"""Finite-neighborhood correctness, preservation, bounds, and quality tests."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.repair_courses import repair
from scripts.repair_data import Course, Dataset, Placement, Room
from scripts.repair_optimizer import optimize_local, quality_frontier


WEEKS = frozenset({1, 2})


def course(cid, **changes):
    values = dict(id=cid, name=cid, hours=1.0, weeks=WEEKS, teachers={f"T_{cid}": WEEKS},
                  classes=frozenset({f"CLASS_{cid}"}), campus="CAMPUS", capacity=20.0)
    values.update(changes)
    return Course(**values)


def placement(cid, period, day=0, weeks=WEEKS):
    return Placement(cid, weeks, day, (period,), "ROOM")


def data(courses, placements=()):
    return Dataset(courses={c.id: c for c in courses},
                   rooms={"ROOM": Room("ROOM", campus="CAMPUS", capacity=40.0, enabled=True)},
                   placements=list(placements))


def rc(**changes):
    values = dict(allowed_changes=[], days=[0], periods=[1, 2, 3], single_period_starts="any",
                  max_candidates_per_course=100)
    values.update(changes)
    return values


def oc(**changes):
    values = dict(quality_weights={"time_preference": 1.0, "evening": 0.0, "weekend": 0.0},
                  max_search_nodes=10000, time_limit_seconds=10, candidate_limit_per_course=100)
    values.update(changes)
    return values


def entries(result, cid):
    return [p for p in result["placements"] if p["course_id"] == cid]


class JointOptimizerTests(unittest.TestCase):
    def test_joint_targets_improve_over_sequential_greedy_assignment(self):
        a = course("A", prefer="周一(1-2节)")
        b = course("B", prefer="周一(1-1节);周一(3-3节)")
        c = course("C", prefer=b.prefer)
        dataset = data([a, b, c])
        original = deepcopy(dataset.to_dict())
        greedy = repair(dataset, ["A", "B", "C"], rc())
        joint = optimize_local(dataset, ["A", "B", "C"], rc(), oc())
        self.assertEqual(greedy["summary"]["inserted"], 2)
        self.assertEqual(joint["summary"]["inserted"], 3)
        self.assertEqual(entries(joint, "A")[0]["periods"], [2])
        self.assertEqual(joint["solver"]["phase_1"]["status"], "optimal_in_retained_domain")
        self.assertEqual(joint["validation"]["new_conflicts"], 0)
        self.assertEqual(dataset.to_dict(), original)

    def test_joint_moves_complete_a_chain_without_losing_original_courses(self):
        a = course("A", prefer="周一(1-2节)")
        b = course("B", prefer="周一(2-3节)")
        target = course("TARGET", prefer="周一(1-1节)")
        dataset = data([a, b, target], [placement("A", 1), placement("B", 2)])
        one = optimize_local(dataset, [target.id], rc(movable_ids=["A", "B"], max_moved_courses=1), oc())
        two = optimize_local(dataset, [target.id], rc(movable_ids=["A", "B"], max_moved_courses=2), oc())
        self.assertEqual(one["summary"]["inserted"], 0)
        self.assertEqual(two["summary"]["inserted"], 1)
        self.assertEqual(two["summary"]["moved"], 2)
        self.assertEqual(entries(two, "A")[0]["periods"], [2])
        self.assertEqual(entries(two, "B")[0]["periods"], [3])
        self.assertEqual(entries(two, "TARGET")[0]["periods"], [1])
        self.assertEqual(two["summary"]["proposed_scheduled"], 3)

    def test_mandatory_original_and_locked_courses_cannot_be_dropped(self):
        old = course("OLD", prefer="周一(1-1节)")
        target = course("TARGET", prefer=old.prefer)
        dataset = data([old, target], [placement("OLD", 1)])
        for locked in ([], ["OLD"]):
            result = optimize_local(dataset, [target.id], rc(movable_ids=["OLD"], locked_ids=locked,
                                                            max_moved_courses=1), oc())
            self.assertEqual(result["summary"]["inserted"], 0)
            self.assertEqual(entries(result, "OLD")[0]["periods"], [1])
            self.assertEqual(result["summary"]["moved"], 0)

    def test_local_retention_cap_is_applied_after_filtering_fixed_blockers(self):
        old, target = course("OLD"), course("TARGET")
        result = optimize_local(data([old, target], [placement("OLD", 1)]), [target.id],
                                rc(max_candidates_per_course=3), oc(candidate_limit_per_course=1))
        self.assertEqual(result["summary"]["inserted"], 1)
        self.assertEqual(entries(result, target.id)[0]["periods"], [2])
        scope = result["solver"]["candidate_scope"]
        self.assertEqual(scope["generation_truncated_ids"], [target.id])
        self.assertEqual(scope["retention_truncated_ids"], [target.id])
        self.assertFalse(result["solver"]["global_optimality_proven"])
        self.assertFalse(result["solver"]["global_infeasibility_proven"])

    def test_unchanged_legacy_conflicts_do_not_allow_new_conflicts(self):
        a, b, target = course("A"), course("B"), course("TARGET")
        dataset = data([a, b, target], [placement("A", 1), placement("B", 1)])
        result = optimize_local(dataset, [target.id],
                                rc(movable_ids=["A", "B"], max_moved_courses=2), oc())
        self.assertEqual(result["summary"]["inserted"], 1)
        self.assertEqual(result["summary"]["moved"], 0)
        self.assertEqual(entries(result, "A")[0]["periods"], [1])
        self.assertEqual(entries(result, "B")[0]["periods"], [1])
        self.assertNotEqual(entries(result, target.id)[0]["periods"], [1])
        self.assertEqual(result["validation"]["new_conflicts"], 0)

    def test_malformed_original_metadata_freezes_course_without_dropping_it(self):
        old = course("OLD", weekly_loads={1: 1})
        target = course("TARGET")
        result = optimize_local(data([old, target], [placement("OLD", 1)]), [target.id],
                                rc(movable_ids=["OLD"], max_moved_courses=1), oc())
        self.assertEqual(result["summary"]["inserted"], 1)
        self.assertEqual(result["summary"]["moved"], 0)
        self.assertEqual(entries(result, "OLD")[0]["periods"], [1])
        self.assertEqual(result["solver"]["candidate_scope"]["frozen_movable_ids"],
                         {"OLD": "unsupported_baseline_metadata"})

    def test_node_limit_returns_incumbent_and_unknown_not_infeasible(self):
        target = course("TARGET")
        result = optimize_local(data([target]), [target.id], rc(), oc(max_search_nodes=1))
        self.assertEqual(result["summary"]["search_nodes"], 1)
        self.assertEqual(result["solver"]["phase_1"]["status"], "unknown_budget_exhausted")
        self.assertEqual(result["solver"]["phase_1"]["insertion_upper_bound"], 1)
        self.assertFalse(result["solver"]["global_infeasibility_proven"])

    def test_generation_is_included_in_shared_wall_clock_budget(self):
        target = course("TARGET")
        times = iter([0.0] + [100.0] * 50)
        with patch("scripts.repair_optimizer.time.monotonic", side_effect=lambda: next(times)):
            result = optimize_local(data([target]), [target.id], rc(), oc(time_limit_seconds=1))
        self.assertEqual(result["summary"]["inserted"], 0)
        self.assertEqual(result["solver"]["candidate_scope"]["generation_truncated_ids"], [target.id])
        self.assertEqual(result["solver"]["phase_1"]["stop_reason"], "time_limit")

    def test_explicit_evening_and_weekend_weights_select_quality(self):
        target = course("TARGET", prefer="周六(9-9节)")
        config = rc(allowed_changes=["time_preference"], time_scope="configured_days", days=[0, 5], periods=[1, 9])
        prefer_only = optimize_local(data([target]), [target.id], config, oc())
        weighted = optimize_local(data([target]), [target.id], config,
                                  oc(quality_weights={"time_preference": 1, "evening": 20, "weekend": 500}))
        self.assertEqual((entries(prefer_only, target.id)[0]["day"], entries(prefer_only, target.id)[0]["periods"]), (5, [9]))
        self.assertEqual((entries(weighted, target.id)[0]["day"], entries(weighted, target.id)[0]["periods"]), (0, [1]))

    def test_weekly_loads_are_preserved_when_moving_a_segmented_course(self):
        old = course("OLD", hours=2.0, weekly_loads={1: 2, 2: 1}, total_hours=3.0)
        target = course("TARGET", prefer="周一(1-1节)")
        baseline = [Placement("OLD", frozenset({1}), 0, (1, 2), "ROOM"), placement("OLD", 1, weeks=frozenset({2}))]
        result = optimize_local(data([old, target], baseline), [target.id],
                                rc(days=[0, 2], periods=[1, 2, 3, 4], movable_ids=["OLD"], max_moved_courses=1), oc())
        self.assertEqual(result["summary"]["inserted"], 1)
        original = entries(result, "OLD")
        counts = {week: len({(p["day"], period) for p in original if week in p["weeks"] for period in p["periods"]}) for week in WEEKS}
        self.assertEqual(counts, {1: 2, 2: 1})
        old_change = next(change for change in result["changes"] if change["course_id"] == "OLD")
        self.assertEqual(old_change["planned_weekly_loads"], {"1": 2, "2": 1})


class QualityFrontierTests(unittest.TestCase):
    def test_same_success_count_compares_quality_at_different_movement_budgets(self):
        old, target = course("OLD"), course("TARGET", prefer="周二(1-1节)")
        dataset = data([old, target], [placement("OLD", 9)])
        config = rc(days=[0, 1], periods=[1, 9], movable_ids=["OLD"], max_moved_courses=1)
        optimizer = oc(quality_weights={"time_preference": 0, "evening": 1, "weekend": 0})
        regular = optimize_local(dataset, [target.id], config, optimizer)
        result = quality_frontier(dataset, [target.id], config, optimizer, [0, 1])
        self.assertEqual(regular["summary"]["moved"], 0)
        self.assertEqual(result["fixed_inserted_count"], 1)
        self.assertTrue(result["success_count_proven_in_retained_domain"])
        points = result["points"]
        self.assertEqual([p["incumbent"]["inserted"] for p in points], [1, 1])
        self.assertEqual([p["incumbent"]["moved"] for p in points], [0, 1])
        self.assertGreater(points[0]["incumbent"]["quality"], points[1]["incumbent"]["quality"])
        fingerprints = {p["proposal"]["solver"]["candidate_scope"]["candidate_domain_sha256"] for p in points}
        self.assertEqual(len(fingerprints), 1)

    def test_lower_budget_infeasibility_is_scoped_and_shared_node_budget_holds(self):
        old = course("OLD")
        target = course("TARGET", prefer="周一(1-1节)")
        dataset = data([old, target], [placement("OLD", 1)])
        config = rc(movable_ids=["OLD"], max_moved_courses=1)
        frontier = quality_frontier(dataset, [target.id], config, oc(), [0, 1])
        self.assertEqual(frontier["fixed_inserted_count"], 1)
        self.assertEqual(frontier["points"][0]["status"], "infeasible_in_retained_domain")
        self.assertNotIn("proposal", frontier["points"][0])
        self.assertFalse(frontier["global_infeasibility_proven"])
        limited = quality_frontier(dataset, [target.id], config, oc(max_search_nodes=2), [0, 1])
        self.assertLessEqual(limited["total_search_nodes"], 2)
        self.assertTrue(any(p["status"] == "unknown_budget_exhausted" for p in limited["points"]))

    def test_weights_and_movement_budgets_require_explicit_valid_policy(self):
        target = course("TARGET")
        for optimizer in ({}, oc(quality_weights={"time_preference": -1, "evening": 0, "weekend": 0}),
                          oc(quality_weights={"time_preference": 0, "evening": 0})):
            with self.assertRaises((TypeError, ValueError)):
                optimize_local(data([target]), [target.id], rc(), optimizer)
        with self.assertRaises(ValueError):
            quality_frontier(data([target]), [target.id], rc(max_moved_courses=0), oc(), [1])


if __name__ == "__main__":
    unittest.main()
