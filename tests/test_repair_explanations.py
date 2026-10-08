"""Exact finite-domain diagnosis tests, using only synthetic course identities."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.repair_explanations import (
    check_finite_consistency, explain_conflict, explain_finite_conflict, make_occupancy_problem,
)
from scripts.repair_data import Course, Dataset, Placement, Room


def finite(domains, constraints=(), required=None, **changes):
    result = {"domains": domains, "constraints": list(constraints), "domains_complete": True,
              "required_courses": list(domains) if required is None else required}
    result.update(changes)
    return result


def candidate(identifier, *resources, blocked_by=()):
    return {"id": identifier, "resource_keys": list(resources), "blocked_by": list(blocked_by)}


def block(identifier, *resources, relaxable=False):
    return {"id": identifier, "kind": "block_resources", "resource_keys": list(resources),
            "explainable": True, "relaxable": relaxable}


def require(identifier, course_id):
    return {"id": identifier, "kind": "require_course", "course_id": course_id,
            "explainable": True, "relaxable": False}


class FiniteOracleTests(unittest.TestCase):
    def test_search_backtracks_joint_assignments_instead_of_greedy_failure(self):
        problem = finite({"A": [candidate("first", "r1"), candidate("second", "r2")],
                          "B": [candidate("first", "r1"), candidate("second", "r3")],
                          "C": [candidate("first", "r1"), candidate("second", "r3")]})
        result = check_finite_consistency(problem)
        self.assertEqual(result["status"], "SAT")
        self.assertEqual(result["witness"]["A"], "second")
        self.assertEqual(len(result["witness"]), 3)

    def test_complete_multi_resource_candidate_is_indivisible(self):
        problem = finite({"A": [candidate("whole", "week1:slot1", "week2:slot1")],
                          "B": [candidate("whole", "week2:slot1")]})
        self.assertEqual(check_finite_consistency(problem)["status"], "UNSAT")
        problem["domains"]["B"] = [candidate("whole", "week3:slot1")]
        self.assertEqual(check_finite_consistency(problem)["status"], "SAT")

    def test_incomplete_domain_does_not_mean_unsat(self):
        problem = finite({"A": []}, domains_complete=False)
        result = check_finite_consistency(problem)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["reason"], "incomplete_domains")
        scoped = check_finite_consistency(problem, allow_scoped_proof=True)
        self.assertEqual(scoped["status"], "UNSAT")
        self.assertEqual(scoped["scope"]["proof_scope"], "supplied_candidates_only")
        self.assertFalse(scoped["scope"]["global_timetable_infeasibility_proven"])

    def test_sat_witness_valid_even_with_incomplete_domains(self):
        problem = finite({"A": [candidate("p", "r")]}, domains_complete=False)
        self.assertEqual(check_finite_consistency(problem)["status"], "SAT")

    def test_missing_constraint_model_cannot_be_overridden_with_scoped_proof(self):
        problem = finite({"A": [candidate("p", "r")]}, model_complete=False)
        result = check_finite_consistency(problem, allow_scoped_proof=True)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["reason"], "incomplete_model")

    def test_background_constraint_cannot_be_switched_off(self):
        c = block("hard-ban", "r")
        c["explainable"] = False
        problem = finite({"A": [candidate("p", "r")]}, [c])
        self.assertEqual(check_finite_consistency(problem, [])["status"], "UNSAT")

    def test_candidate_constraint_labels_do_not_change_domains(self):
        problem = finite({"A": [candidate("p", "r", blocked_by=["preference"])]},
                         [{"id": "preference", "kind": "block_candidates"}])
        before = deepcopy(problem)
        self.assertEqual(check_finite_consistency(problem)["status"], "UNSAT")
        self.assertEqual(check_finite_consistency(problem, [])["status"], "SAT")
        self.assertEqual(problem, before)

    def test_unrecognized_labels_and_invalid_budgets_are_rejected(self):
        problem = finite({"A": [candidate("p", blocked_by=["missing"])]})
        with self.assertRaises(ValueError):
            check_finite_consistency(problem)
        with self.assertRaises(ValueError):
            check_finite_consistency(finite({}), time_limit_seconds=float("nan"))
        with self.assertRaises(ValueError):
            check_finite_consistency(finite({}), max_nodes=-1)


class QuickXplainTests(unittest.TestCase):
    def two_member_problem(self):
        return finite({"A": [candidate("p1", "r1"), candidate("p2", "r2")]},
                      [block("one", "r1", relaxable=True), block("two", "r2"),
                       block("irrelevant", "unused")])

    def test_core_is_irreducible_and_checked_by_removing_every_member(self):
        problem = self.two_member_problem()
        before = deepcopy(problem)
        result = explain_finite_conflict(problem)
        self.assertEqual(result["status"], "conflict")
        self.assertEqual(result["core_ids"], ["one", "two"])
        self.assertTrue(result["minimality_verified"])
        self.assertEqual({r["removed_constraint_id"] for r in result["verification"]}, {"one", "two"})
        self.assertTrue(all(r["status"] == "SAT" for r in result["verification"]))
        self.assertEqual([c["relaxable"] for c in result["core"]], [True, False])
        self.assertEqual(result["relaxations_applied"], [])
        self.assertEqual(problem, before)

    def test_background_unsat_is_not_blame_on_foreground(self):
        problem = finite({"A": [], "B": [candidate("p", "r")]},
                         [block("irrelevant", "unused")])
        result = explain_finite_conflict(problem)
        self.assertEqual(result["status"], "background_unsat")
        self.assertEqual(result["background_status"], "UNSAT")
        self.assertIsNone(result["full_status"])
        self.assertEqual(result["core_ids"], [])
        self.assertFalse(result["minimality_verified"])

    def test_no_conflict_provides_complete_witness(self):
        problem = finite({"A": [candidate("p", "r")]}, [block("unused", "x")])
        result = explain_finite_conflict(problem)
        self.assertEqual(result["status"], "no_conflict")
        self.assertEqual(result["witness"], {"A": "p"})
        self.assertFalse(result["minimality_verified"])

    def test_required_course_tags_explain_joint_resource_conflict(self):
        problem = finite({"A": [candidate("p", "r")], "B": [candidate("p", "r")]},
                         [require("need-A", "A"), require("need-B", "B")], required=[])
        result = explain_finite_conflict(problem)
        self.assertEqual(result["background_status"], "SAT")
        self.assertEqual(result["core_ids"], ["need-A", "need-B"])

    def test_multiple_conflicts_return_one_core_not_a_global_repair(self):
        problem = finite({"A": [candidate("p", "r")]}, [block("first", "r"), block("second", "r")],
                         foreground_ids=["first", "second"], background_ids=[])
        result = explain_finite_conflict(problem)
        self.assertEqual(result["core_ids"], ["first"])
        # Removing the explained member from the ORIGINAL problem leaves another conflict.
        self.assertEqual(check_finite_consistency(problem, ["second"])["status"], "UNSAT")
        self.assertEqual(result["relaxations_applied"], [])

    def test_all_calls_share_budget_and_unknown_never_claims_minimality(self):
        result = explain_finite_conflict(self.two_member_problem(), max_oracle_calls=2)
        self.assertEqual(result["background_status"], "SAT")
        self.assertEqual(result["full_status"], "UNSAT")
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["oracle_calls"], 2)
        self.assertEqual(result["budget_exhausted"], "oracle_call_limit")
        self.assertTrue(result["known_conflict_ids"])
        self.assertEqual(result["core_ids"], [])
        self.assertFalse(result["minimality_verified"])

    def test_zero_time_budget_stops_before_background_proof(self):
        result = explain_finite_conflict(self.two_member_problem(), time_limit_seconds=0)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["background_status"], "UNKNOWN")
        self.assertEqual(result["oracle_calls"], 0)
        self.assertFalse(result["minimality_verified"])

    def test_candidate_examination_budget_is_shared(self):
        result = explain_finite_conflict(self.two_member_problem(), max_nodes=3)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["nodes"], 3)
        self.assertEqual(result["budget_exhausted"], "node_limit")

    def test_preprocessing_time_is_part_of_total_budget(self):
        problem = self.two_member_problem()
        problem["preprocessing_seconds"] = 2.0
        result = explain_finite_conflict(problem, time_limit_seconds=1)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["budget_exhausted"], "time_limit")
        self.assertEqual(result["oracle_calls"], 0)

    def test_deterministic_core_queries_and_candidate_order(self):
        p = self.two_member_problem()
        a, b = explain_finite_conflict(p), explain_finite_conflict(p)
        for field in ("core", "core_ids", "oracle_calls", "nodes", "verification", "oracle_trace", "scope"):
            self.assertEqual(a[field], b[field])


WEEKS = frozenset({1, 2})


def course(cid, **changes):
    values = {"id": cid, "name": cid, "hours": 2, "weeks": WEEKS,
              "teachers": {"T_" + cid: WEEKS}, "classes": frozenset({"G_" + cid}),
              "campus": "C", "capacity": 20, "room_type": "LECTURE"}
    values.update(changes)
    return Course(**values)


def data(courses, placements=(), **changes):
    values = {"courses": {c.id: c for c in courses},
              "rooms": {"R": Room(id="R", name="R", campus="C", capacity=30,
                                  type="LECTURE", enabled=True)}, "placements": list(placements)}
    values.update(changes)
    return Dataset(**values)


def policy(**changes):
    return {"days": [0], "periods": [1, 2], **changes}


class OccupancyAdapterTests(unittest.TestCase):
    def test_occupied_candidates_survive_and_switching_label_does_not_rebuild_domain(self):
        dataset = data([course("A"), course("B")], [Placement("A", WEEKS, 0, (1, 2), "R")])
        before = deepcopy(dataset)
        p = make_occupancy_problem(dataset, ["B"], policy(movable_ids=["A"], max_moved_courses=1))
        self.assertEqual(len(p["domains"]["B"]), 1)
        self.assertTrue(p["domains_complete"])
        self.assertEqual(check_finite_consistency(p)["status"], "UNSAT")
        self.assertEqual(check_finite_consistency(p, [])["status"], "SAT")
        result = explain_finite_conflict(p)
        self.assertEqual(result["core_ids"], ["occupancy:A"])
        self.assertTrue(result["core"][0]["relaxable"])
        self.assertTrue(result["scope"]["description"]["removing_occupancy_does_not_reinsert_course"])
        self.assertEqual(dataset, before)

    def test_locked_course_is_explainable_but_not_relaxable(self):
        dataset = data([course("A"), course("B")], [Placement("A", WEEKS, 0, (1, 2), "R")])
        r = explain_conflict(dataset, ["B"], policy(movable_ids=["A"], locked_ids=["A"]))
        self.assertEqual(r["status"], "conflict")
        self.assertFalse(r["core"][0]["relaxable"])

    def test_uncertain_occupancy_never_becomes_authorized_relaxation(self):
        dataset = data([course("A"), course("B")], [Placement("A", frozenset(), 0, (1, 2), "R")])
        r = explain_conflict(dataset, ["B"], policy(movable_ids=["A"]))
        self.assertEqual(r["core_ids"], ["occupancy:A"])
        self.assertTrue(r["core"][0]["data_uncertainty"])
        self.assertFalse(r["core"][0]["relaxable"])

    def test_historical_collisions_do_not_make_insertion_background_unsat(self):
        dataset = data([course("A"), course("B"), course("TARGET")],
                       [Placement("A", WEEKS, 0, (1, 2), "R"),
                        Placement("B", WEEKS, 0, (1, 2), "R")])
        r = explain_conflict(dataset, ["TARGET"], policy())
        self.assertEqual(r["background_status"], "SAT")
        self.assertEqual(r["status"], "conflict")
        self.assertFalse(r["scope"]["description"]["baseline_globally_validated"])
        self.assertEqual(len(r["core_ids"]), 1)

    def test_cap_marks_unknown_instead_of_certifying_missing_candidate_failure(self):
        dataset = data([course("A"), course("B")], [Placement("A", WEEKS, 0, (1, 2), "R")])
        p = make_occupancy_problem(dataset, ["B"], policy(periods=[1, 2, 3, 4]), max_candidates_per_course=1)
        self.assertFalse(p["domains_complete"])
        r = explain_finite_conflict(p)
        self.assertEqual(r["status"], "unknown")
        self.assertEqual(r["reason"], "incomplete_domains")
        self.assertFalse(r["minimality_verified"])

    def test_unsupported_data_is_unknown_even_if_scoped_proof_requested(self):
        dataset = data([course("B", hours=1.5)])
        p = make_occupancy_problem(dataset, ["B"], policy())
        self.assertFalse(p["model_complete"])
        r = explain_finite_conflict(p, allow_scoped_proof=True)
        self.assertEqual(r["status"], "unknown")
        self.assertEqual(r["reason"], "incomplete_model")

    def test_static_capacity_failure_is_background_not_an_occupancy_core(self):
        dataset = data([course("B", capacity=100)])
        r = explain_conflict(dataset, ["B"], policy())
        self.assertEqual(r["status"], "background_unsat")
        self.assertEqual(r["core_ids"], [])
        self.assertEqual(r["construction_diagnostics"]["B"]["reason"], "no_matching_room")

    def test_scheduled_target_is_rejected_instead_of_self_blocking(self):
        dataset = data([course("A")], [Placement("A", WEEKS, 0, (1, 2), "R")])
        with self.assertRaises(ValueError):
            make_occupancy_problem(dataset, ["A"], policy())

    def test_target_ids_cannot_be_accidentally_split_from_a_string(self):
        dataset = data([course("A"), course("B")])
        with self.assertRaises(ValueError):
            make_occupancy_problem(dataset, "AB", policy())

    def test_construction_timeout_cannot_yield_a_certificate(self):
        dataset = data([course("B")])
        p = make_occupancy_problem(dataset, ["B"], policy(), time_limit_seconds=0)
        self.assertEqual(p["construction_status"], "time_limit")
        self.assertFalse(p["model_complete"])
        self.assertEqual(explain_finite_conflict(p)["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
