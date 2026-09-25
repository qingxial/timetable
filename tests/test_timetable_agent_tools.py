"""Agent-tool contract tests; all fixtures are temporary and synthetic."""
from contextlib import redirect_stdout
from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

from openpyxl import Workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.repair_courses import RepairConfig
from scripts.timetable_agent_tools import TOOL_SCHEMAS, dispatch_tool, main, _validate


def synthetic_proposal(status="not_found_within_limits"):
    return {"schema_version": 1, "mode": "proposal", "source_hashes": {
        "courses": "a" * 64, "rooms": "b" * 64, "schedule": "c" * 64},
        "config": asdict(RepairConfig(allowed_changes=[])),
        "summary": {"requested": 1, "inserted": 0, "moved": 0},
        "results": [{"course_id": "TARGET", "status": status, "diagnosis": {"reason": status}}],
        "changes": [], "placements": [], "validation": {"new_conflicts": 0}, "data_issues": []}


def write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return str(path)


def workbook(path, headers, rows, double=False):
    book = Workbook()
    sheet = book.active
    if double:
        sheet.append(headers)
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    book.save(path)
    book.close()


def excel_sources(root, courses=None, scheduled=None, failed=None):
    """Create complete, small workbook inputs for actual tool dispatch calls."""
    courses = courses or [{"JXBID": "TARGET"}]
    headers = ["JXBID", "KCM", "ZXS", "LLXS", "KRL", "SKXQ", "SKZCDM", "JSH",
               "RWJSZCDM", "TJBJ", "Prefer_Time", "unavailable_Time", "PKYQMS"]
    rows = []
    for supplied in courses:
        cid = supplied["JXBID"]
        values = {"KCM": "Synthetic " + cid, "ZXS": 2, "LLXS": 4, "KRL": 30,
                  "SKXQ": "CAMPUS", "SKZCDM": "11", "JSH": "TEACHER_" + cid,
                  "RWJSZCDM": "11", "TJBJ": "CLASS01", **supplied}
        rows.append([values.get(key, "") for key in headers])
    paths = {name: root / f"{name}.xlsx" for name in ("course", "room", "schedule", "failed")}
    workbook(paths["course"], headers, rows, True)
    workbook(paths["room"], ["JASDM", "JASMC", "MC", "SKZWS", "SFYXPK"],
             [["R1", "101", "CAMPUS", 40, 1]], True)
    workbook(paths["schedule"], ["教学班ID", "教室代码", "上课周次", "星期", "节次", "周学时"], scheduled or [])
    workbook(paths["failed"], ["jxbid"], [[cid] for cid in (failed if failed is not None else ["TARGET"])])
    workbook(root / "班级表.xlsx", ["BJMC"], [["CLASS01"]])
    return {f"{name}_path": str(path) for name, path in paths.items()}


def optimizer_policy(**changes):
    return {"quality_weights": {"time_preference": 1, "evening": 2, "weekend": 3},
            "max_search_nodes": 10000, "time_limit_seconds": 5,
            "candidate_limit_per_course": 100, **changes}


class ToolContractTests(unittest.TestCase):
    def test_all_advertised_schema_names_have_dispatch(self):
        self.assertEqual({s["name"] for s in TOOL_SCHEMAS},
                         {"diagnose_remaining", "propose_repair", "compare_proposals",
                          "repair_with_fallbacks", "preview_data_corrections", "optimize_local_repair",
                          "quality_frontier", "explain_conflict", "plan_course_corrections",
                          "extend_repair_proposal"})
        self.assertEqual(len(TOOL_SCHEMAS), 10)
        for schema in TOOL_SCHEMAS:
            with self.subTest(name=schema["name"]):
                self.assertFalse(schema["parameters"]["additionalProperties"])
                response = dispatch_tool(schema["name"], {})
                self.assertEqual(response["error"]["code"], "invalid_arguments")

    def test_unknown_tool_bad_types_fields_and_missing_whitelist(self):
        cases = [("made_up_solver", {}, "unknown_tool"),
                 ("diagnose_remaining", {"proposal_path": "x", "execute": "anything"}, "invalid_arguments"),
                 ("diagnose_remaining", {"proposal_path": 3}, "invalid_arguments"),
                 ("compare_proposals", {"proposal_paths": ["x"]}, "invalid_arguments"),
                 ("propose_repair", {"course_path": "c", "room_path": "r", "schedule_path": "s", "failed_path": "f", "config": {}}, "invalid_arguments")]
        for name, arguments, code in cases:
            with self.subTest(name=name, arguments=arguments):
                self.assertEqual(dispatch_tool(name, arguments)["error"]["code"], code)
        base = {"course_path": "c", "room_path": "r", "schedule_path": "s", "failed_path": "f"}
        for config in ({"allowed_changes": [], "unknown": 1},
                       {"allowed_changes": ["capacity"]},
                       {"allowed_changes": [], "max_moved_courses": True},
                       {"allowed_changes": [], "time_limit_seconds": float("nan")},
                       {"allowed_changes": [], "single_period_starts": "even"},
                       {"allowed_changes": [], "days": [0, 0]}):
            self.assertEqual(dispatch_tool("propose_repair", {**base, "config": config})["error"]["code"], "invalid_arguments")

    def test_missing_file_is_structured_error(self):
        with TemporaryDirectory() as temporary:
            response = dispatch_tool("diagnose_remaining", {"proposal_path": str(Path(temporary) / "missing.json")})
            self.assertFalse(response["ok"])
            self.assertEqual(response["error"]["code"], "input_error")

    def test_malformed_envelopes_and_nonfinite_json_return_errors(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "bad.json"
            write_json(path, {"ok": True, "tool": "propose_repair", "result": []})
            self.assertEqual(dispatch_tool("diagnose_remaining", {"proposal_path": str(path)})["error"]["code"], "invalid_proposal")
            path.write_text('{"value": 1e9999}', encoding="utf-8")
            self.assertEqual(dispatch_tool("diagnose_remaining", {"proposal_path": str(path)})["error"]["code"], "input_error")

    def test_corrupt_workbook_is_structured_error(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "bad.xlsx"
            path.write_text("not a workbook", encoding="utf-8")
            arguments = {f"{name}_path": str(path) for name in ("course", "room", "schedule", "failed")}
            arguments["config"] = {"allowed_changes": []}
            result = dispatch_tool("propose_repair", arguments)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "tool_error")

    def test_diagnosis_preserves_unknown_and_does_not_confirm_unsat(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "p.json"
            for status, evidence in (("search_limit", "unknown_within_search_limits"),
                                     ("not_found_within_limits", "unknown_within_search_limits"),
                                     ("infeasible", "unverified_infeasibility_claim"),
                                     ("no_time_pattern", "no_candidate_in_current_model")):
                write_json(path, synthetic_proposal(status))
                result = dispatch_tool("diagnose_remaining", {"proposal_path": str(path)})
                self.assertTrue(result["ok"], result)
                row = result["result"]["courses"][0]
                self.assertEqual(row["status"], status)
                self.assertEqual(row["evidence_status"], evidence)
                self.assertFalse(row["infeasibility_proven"])
                self.assertEqual(result["result"]["status_counts"], {status: 1})

    def test_comparison_rejects_different_baseline_or_targets(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = synthetic_proposal()
            paths = [write_json(root / "a.json", first), str(root / "b.json")]
            changed = deepcopy(first)
            changed["source_hashes"]["schedule"] = "d" * 64
            write_json(root / "b.json", changed)
            self.assertEqual(dispatch_tool("compare_proposals", {"proposal_paths": paths})["error"]["code"], "incomparable_proposals")
            changed = deepcopy(first)
            changed["results"][0]["course_id"] = "OTHER"
            write_json(root / "b.json", changed)
            self.assertEqual(dispatch_tool("compare_proposals", {"proposal_paths": paths})["error"]["code"], "incomparable_proposals")

    def test_comparison_counts_changes_cost_and_policy_differences(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = synthetic_proposal()
            second = synthetic_proposal("placed")
            second["config"]["allowed_changes"] = ["time_preference"]
            second["summary"].update(inserted=1, moved=1)
            second["changes"] = [
                {"course_id": "TARGET", "operation": "insert", "original_time_preference": "周一(1-2节)",
                 "after": [{"day": 0, "periods": [3, 4]}]},
                {"course_id": "OLD", "operation": "move", "original_time_preference": "",
                 "after": [{"day": 0, "periods": [5, 6]}]}]
            paths = [write_json(root / "a.json", first), write_json(root / "b.json", second)]
            response = dispatch_tool("compare_proposals", {"proposal_paths": paths})
            self.assertTrue(response["ok"], response)
            result = response["result"]
            self.assertFalse(result["same_recorded_policy_and_budget"])
            self.assertIn("allowed_changes", result["policy_or_budget_differences"])
            self.assertEqual(result["proposals"][1]["inserted"], 1)
            self.assertEqual(result["proposals"][1]["moved_original_courses"], 1)
            self.assertEqual(result["proposals"][1]["preference_cost"], 2)
            self.assertIn("conditional", result["interpretation"])

    def test_summary_change_disagreement_is_rejected(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = synthetic_proposal()
            second = deepcopy(first)
            second["summary"]["inserted"] = 1
            paths = [write_json(root / "a.json", first), write_json(root / "b.json", second)]
            self.assertEqual(dispatch_tool("compare_proposals", {"proposal_paths": paths})["error"]["code"], "invalid_proposal")

    def test_dry_run_schema_call_sources_unchanged_and_wrapper_reusable(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = {name: root / f"{name}.xlsx" for name in ("course", "room", "schedule", "failed")}
            workbook(paths["course"], ["JXBID", "KCM", "ZXS", "LLXS", "KRL", "SKXQ", "SKZCDM", "JSH", "RWJSZCDM", "TJBJ"],
                     [["TARGET", "Example", 2, 4, 30, "CAMPUS", "11", "TEACHER", "11", "CLASS01"]], True)
            workbook(paths["room"], ["JASDM", "JASMC", "MC", "SKZWS", "SFYXPK"], [["R1", "101", "CAMPUS", 40, 1]], True)
            workbook(paths["schedule"], ["教学班ID", "教室代码", "上课周次", "星期", "节次"], [])
            workbook(paths["failed"], ["jxbid"], [["TARGET"]])
            workbook(root / "班级表.xlsx", ["BJMC"], [["CLASS01"]])
            before = {str(path): sha256(path.read_bytes()).hexdigest() for path in root.glob("*.xlsx")}
            arguments = {f"{name}_path": str(path) for name, path in paths.items()}
            arguments["config"] = {"allowed_changes": [], "days": [0], "periods": [1, 2], "single_period_starts": "any"}
            schema = next(s["parameters"] for s in TOOL_SCHEMAS if s["name"] == "propose_repair")
            _validate(arguments, schema)
            cwd_before = os.getcwd()
            response = dispatch_tool("propose_repair", arguments)
            self.assertTrue(response["ok"], response)
            self.assertEqual(response["result"]["proposal"]["summary"]["inserted"], 1)
            self.assertTrue(response["result"]["source_files_unchanged"])
            self.assertEqual(response["result"]["policy"]["single_period_starts"], "any")
            self.assertEqual(os.getcwd(), cwd_before)
            self.assertEqual(before, {str(path): sha256(path.read_bytes()).hexdigest() for path in root.glob("*.xlsx")})
            self.assertEqual(len(list(root.glob("*.json"))), 0)
            output = write_json(root / "response.json", response)
            self.assertTrue(dispatch_tool("diagnose_remaining", {"proposal_path": output})["ok"])
            arguments["config"]["target_ids"] = ["OTHER"]
            self.assertEqual(dispatch_tool("propose_repair", arguments)["error"]["code"], "invalid_arguments")

    def test_cli_request_fields_and_no_output_overwrite(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            proposal = write_json(root / "proposal.json", synthetic_proposal())
            request = root / "request.json"
            output = root / "response.json"
            write_json(request, {"name": "diagnose_remaining", "arguments": {"proposal_path": proposal}})
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["--request", str(request), "--output", str(output)]), 0)
            original = output.read_bytes()
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["--request", str(request), "--output", str(output)]), 1)
            self.assertEqual(original, output.read_bytes())
            write_json(request, {"name": "diagnose_remaining", "arguments": {"proposal_path": proposal}, "unapproved": True})
            second = root / "error.json"
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["--request", str(request), "--output", str(second)]), 1)
            self.assertEqual(json.loads(second.read_text())["error"]["code"], "invalid_arguments")


class ExtendedToolIntegrationTests(unittest.TestCase):
    def assert_ok(self, name, arguments):
        schema = next(s["parameters"] for s in TOOL_SCHEMAS if s["name"] == name)
        _validate(arguments, schema)
        result = dispatch_tool(name, arguments)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["result"]["source_files_unchanged"])
        return result

    def test_preview_corrections_changes_effective_hash_without_touching_inputs(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = excel_sources(root, courses=[{"JXBID": "TARGET", "KRL": 60}])
            before = {p.name: sha256(p.read_bytes()).hexdigest() for p in root.glob("*.xlsx")}
            correction = {"course_id": "TARGET", "operation": "set_capacity", "expected_capacity": 60,
                          "capacity": 30, "enrollment_frozen": True, "evidence": "Synthetic frozen enrollment record"}
            preview = self.assert_ok("preview_data_corrections", {**sources, "corrections": [correction]})["result"]
            self.assertEqual(preview["mode"], "correction_preview")
            self.assertEqual(preview["audit"][0]["before"], {"capacity": 60})
            self.assertEqual(preview["audit"][0]["after"], {"capacity": 30})
            self.assertFalse(preview["audit"][0]["source_files_modified"])
            self.assertRegex(preview["effective_source_hashes"]["corrections"], r"^[0-9a-f]{64}$")
            config = {"allowed_changes": [], "days": [0], "periods": [1, 2]}
            original = self.assert_ok("propose_repair", {**sources, "config": config})
            corrected = self.assert_ok("propose_repair", {**sources, "config": config, "corrections": [correction]})
            self.assertEqual(original["result"]["proposal"]["summary"]["inserted"], 0)
            self.assertEqual(corrected["result"]["proposal"]["summary"]["inserted"], 1)
            self.assertEqual(corrected["result"]["proposal"]["source_hashes"]["corrections"],
                             preview["effective_source_hashes"]["corrections"])
            paths = [write_json(root / "original.json", original), write_json(root / "corrected.json", corrected)]
            compared = dispatch_tool("compare_proposals", {"proposal_paths": paths})
            self.assertEqual(compared["error"]["code"], "incomparable_proposals")
            self.assertEqual(before, {p.name: sha256(p.read_bytes()).hexdigest() for p in root.glob("*.xlsx")})
            bad = {**correction, "expected_capacity": 61}
            self.assertEqual(dispatch_tool("preview_data_corrections", {**sources, "corrections": [bad]})
                             ["error"]["code"], "input_error")

    def test_fallback_inserts_in_stages_and_preserves_existing_and_earlier_courses(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = excel_sources(root, courses=[
                {"JXBID": "DAY", "Prefer_Time": "周一(1-2节)"},
                {"JXBID": "EVE", "Prefer_Time": "周一(9-10节)"},
                {"JXBID": "SAT", "Prefer_Time": "周六(1-2节)"}, {"JXBID": "OLD"}],
                scheduled=[["OLD", "R1", "1-2", "周一", "第3-4节", 2]], failed=["DAY", "EVE", "SAT"])
            response = self.assert_ok("repair_with_fallbacks", {**sources, "config": {
                "allowed_changes": [], "days": [0, 5], "periods": [1, 2, 9, 10]},
                "stages": ["daytime", "evening", "weekend"], "total_time_limit_seconds": 10})
            proposal = response["result"]["proposal"]
            self.assertEqual(proposal["summary"]["inserted"], 3)
            self.assertEqual(proposal["summary"]["moved"], 0)
            self.assertTrue(proposal["validation"]["prior_placements_unchanged"])
            changes = {row["course_id"]: row for row in proposal["changes"]}
            self.assertEqual({cid: row["fallback_stage"] for cid, row in changes.items()},
                             {"DAY": "daytime", "EVE": "evening", "SAT": "weekend"})
            self.assertEqual([(p["day"], p["periods"]) for p in changes["DAY"]["after"]], [(0, [1, 2])])
            self.assertEqual([(p["day"], p["periods"]) for p in changes["EVE"]["after"]], [(0, [9, 10])])
            old = [p for p in proposal["placements"] if p["course_id"] == "OLD"]
            self.assertEqual(old, [{"course_id": "OLD", "weeks": [1, 2], "day": 0,
                                    "periods": [3, 4], "room_id": "R1"}])

    def test_weekend_fallback_respects_caller_days_and_explicit_time_scope(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = excel_sources(root, courses=[{"JXBID": "TARGET", "Prefer_Time": "周一(1-2节)"}])
            config = {"allowed_changes": ["time_preference"], "time_scope": "configured_days",
                      "days": [5], "periods": [1, 2]}
            response = self.assert_ok("repair_with_fallbacks", {**sources, "config": config})
            proposal = response["result"]["proposal"]
            self.assertEqual(proposal["summary"]["inserted"], 1)
            self.assertEqual({p["day"] for p in proposal["placements"]}, {5})
            runs = proposal["workflow"]["stages"]
            self.assertEqual([r.get("status") for r in runs[:2]],
                             ["outside_caller_domain", "outside_caller_domain"])
            # Requesting the weekend stage itself does not override the caller's
            # weekday-only scope, even when the days list contains Saturday.
            denied = self.assert_ok("repair_with_fallbacks", {
                **sources, "config": {**config, "time_scope": "weekdays"}, "stages": ["weekend"]})
            self.assertEqual(denied["result"]["proposal"]["summary"]["inserted"], 0)
            self.assertEqual(denied["result"]["proposal"]["placements"], [])

    def movement_sources(self, root):
        sources = excel_sources(root, courses=[{"JXBID": "TARGET", "Prefer_Time": "周一(1-2节)"},
                                               {"JXBID": "OLD"}],
                                scheduled=[["OLD", "R1", "1-2", "周一", "第1-2节", 2]])
        config = {"allowed_changes": [], "days": [0], "periods": [1, 2, 3, 4],
                  "movable_ids": ["OLD"], "max_moved_courses": 1}
        return sources, config

    def test_optimizer_jointly_moves_authorized_course_and_preserves_full_hours(self):
        with TemporaryDirectory() as temporary:
            sources, config = self.movement_sources(Path(temporary))
            result = self.assert_ok("optimize_local_repair", {**sources, "config": config,
                                   "optimizer_config": optimizer_policy()})["result"]["proposal"]
            self.assertEqual((result["summary"]["inserted"], result["summary"]["moved"]), (1, 1))
            self.assertEqual(result["validation"]["new_conflicts"], 0)
            self.assertTrue(result["validation"]["hours_and_weeks_preserved"])
            self.assertFalse(result["solver"]["global_optimality_proven"])
            self.assertFalse(result["solver"]["global_infeasibility_proven"])
            changes = {c["course_id"]: c for c in result["changes"]}
            self.assertEqual(changes["OLD"]["operation"], "move")
            self.assertEqual(changes["OLD"]["after"][0]["periods"], [3, 4])
            self.assertEqual(changes["TARGET"]["after"][0]["periods"], [1, 2])
            for change in changes.values():
                hours = sum(len(p["weeks"]) * len(p["periods"]) for p in change["after"])
                self.assertEqual(hours, 4)

    def test_frontier_holds_success_level_and_reports_infeasible_small_budget_locally(self):
        with TemporaryDirectory() as temporary:
            sources, config = self.movement_sources(Path(temporary))
            response = self.assert_ok("quality_frontier", {**sources, "config": config,
                "optimizer_config": optimizer_policy(), "movement_budgets": [0, 1]})["result"]
            self.assertEqual(response["mode"], "quality_frontier")
            self.assertEqual(response["fixed_inserted_count"], 1)
            self.assertTrue(response["success_count_proven_in_retained_domain"])
            zero, one = response["points"]
            self.assertEqual(zero["movement_budget"], 0)
            self.assertEqual(zero["status"], "infeasible_in_retained_domain")
            self.assertNotIn("proposal", zero)
            self.assertEqual(one["proposal"]["summary"]["inserted"], 1)
            self.assertEqual(one["proposal"]["summary"]["moved"], 1)
            self.assertFalse(response["global_infeasibility_proven"])

    def test_explanation_verifies_occupancy_core_and_budget_unknown_is_not_evidence(self):
        with TemporaryDirectory() as temporary:
            sources, config = self.movement_sources(Path(temporary))
            args = {**sources, "config": config, "explain_config": {"max_nodes": 10000}}
            result = self.assert_ok("explain_conflict", args)["result"]
            self.assertEqual(result["status"], "conflict")
            self.assertEqual(result["background_status"], "SAT")
            self.assertEqual(result["full_status"], "UNSAT")
            self.assertEqual(result["core_ids"], ["occupancy:OLD"])
            self.assertTrue(result["minimality_verified"])
            self.assertTrue(all(v["status"] == "SAT" for v in result["verification"]))
            self.assertTrue(result["core"][0]["relaxable"])
            self.assertEqual(result["relaxations_applied"], [])
            self.assertFalse(result["scope"]["global_timetable_infeasibility_proven"])
            unknown = self.assert_ok("explain_conflict", {
                **args, "explain_config": {"max_nodes": 1}})["result"]
            self.assertEqual(unknown["status"], "unknown")
            self.assertFalse(unknown["minimality_verified"])
            self.assertEqual(unknown["core_ids"], [])
            self.assertFalse(unknown["scope"]["global_timetable_infeasibility_proven"])

    def test_action_plan_preserves_source_evidence_and_does_not_apply_capacity_change(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = excel_sources(root, courses=[{"JXBID": "TARGET", "KRL": 60}])
            proposal = self.assert_ok("propose_repair", {
                **sources, "config": {"allowed_changes": [], "days": [0], "periods": [1, 2]}})
            path = write_json(root / "proposal.json", proposal)
            planned = self.assert_ok("plan_course_corrections", {**sources, "proposal_path": path})["result"]
            self.assertEqual(planned["mode"], "action_plan_only")
            self.assertEqual(planned["summary"]["remaining_proposal_courses"], 1)
            plan = planned["course_plans"][0]
            self.assertEqual(plan["course_id"], "TARGET")
            action = next(a for a in plan["actions"] if a["code"] == "resolve_static_room_supply")
            self.assertFalse(action["auto_executable"])
            self.assertEqual(action["current_values"]["KRL"], 60)
            inventory = next(c for c in action["proposed_changes"] if c["field"] == "room_inventory")
            self.assertEqual(inventory["candidate_value"]["minimum_capacity"], 60)
            self.assertTrue(action["required_data_or_confirmation"])
            self.assertTrue(action["revalidation_conditions"])

    def test_new_proposal_cli_envelopes_can_be_diagnosed_directly(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = excel_sources(root)
            base = {**sources, "config": {"allowed_changes": [], "days": [0], "periods": [1, 2]}}
            for name in ("repair_with_fallbacks", "optimize_local_repair"):
                with self.subTest(name=name):
                    args = deepcopy(base)
                    if name == "optimize_local_repair":
                        args["optimizer_config"] = optimizer_policy()
                    request = root / (name + "-request.json")
                    output = root / (name + "-response.json")
                    write_json(request, {"name": name, "arguments": args})
                    with redirect_stdout(io.StringIO()):
                        self.assertEqual(main(["--request", str(request), "--output", str(output)]), 0)
                    diagnosed = dispatch_tool("diagnose_remaining", {"proposal_path": str(output)})
                    self.assertTrue(diagnosed["ok"], diagnosed)
                    self.assertEqual(diagnosed["result"]["remaining_count"], 0)
                    self.assertEqual(diagnosed["result"]["courses"][0]["evidence_status"], "feasible_incremental_proposal")
                    self.assertFalse(diagnosed["result"]["courses"][0]["infeasibility_proven"])

    def test_extension_explicitly_promotes_additional_target_outside_failed_list(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = excel_sources(root, courses=[{"JXBID": "TARGET", "Prefer_Time": "周一(1-2节)"},
                {"JXBID": "EXTRA", "Prefer_Time": "周一(3-4节)"}], failed=["TARGET"])
            config = {"allowed_changes": [], "days": [0], "periods": [1, 2, 3, 4]}
            seed = self.assert_ok("propose_repair", {**sources, "config": config})
            seed_path = write_json(root / "seed.json", seed)
            arguments = {**sources, "seed_proposal_path": seed_path, "additional_target_ids": ["EXTRA"],
                         "config": config, "stages": ["daytime"]}
            extended = self.assert_ok("extend_repair_proposal", arguments)
            proposal = extended["result"]["proposal"]
            self.assertEqual(proposal["summary"]["inserted"], 2)
            self.assertEqual(proposal["summary"]["requested"], 2)
            self.assertEqual(proposal["scope_extension"]["additional_target_ids"], ["EXTRA"])
            self.assertTrue(proposal["validation"]["seed_successes_preserved"])
            path = write_json(root / "extended.json", extended)
            self.assertTrue(dispatch_tool("diagnose_remaining", {"proposal_path": path})["ok"])
            corrupted = deepcopy(seed)
            corrupted["result"]["proposal"]["source_hashes"]["courses"] = "e" * 64
            write_json(root / "seed.json", corrupted)
            self.assertEqual(dispatch_tool("extend_repair_proposal", arguments)["error"]["code"], "incomparable_proposals")

    def test_extension_requires_exact_correction_prefix_and_limits_new_corrections_to_new_targets(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = excel_sources(root, courses=[{"JXBID": "TARGET", "KRL": 60}, {"JXBID": "EXTRA", "KRL": 60}])
            config = {"allowed_changes": [], "days": [0], "periods": [1, 2, 3, 4]}
            correction = {"course_id": "TARGET", "operation": "set_capacity", "expected_capacity": 60,
                          "capacity": 30, "enrollment_frozen": True, "evidence": "Synthetic frozen roster"}
            seed = self.assert_ok("propose_repair", {**sources, "config": config, "corrections": [correction]})
            seed_path = write_json(root / "seed.json", seed)
            args = {**sources, "seed_proposal_path": seed_path, "additional_target_ids": ["EXTRA"], "config": config}
            self.assertEqual(dispatch_tool("extend_repair_proposal", args)["error"]["code"], "invalid_arguments")
            illegal_extra = {**correction, "expected_capacity": 30, "capacity": 25}
            response = dispatch_tool("extend_repair_proposal", {**args, "corrections": [correction, illegal_extra]})
            self.assertEqual(response["error"]["code"], "invalid_arguments")
            new_correction = {**correction, "course_id": "EXTRA"}
            result = self.assert_ok("extend_repair_proposal", {**args, "corrections": [correction, new_correction]})
            self.assertEqual(result["result"]["proposal"]["summary"]["inserted"], 2)

    def test_extension_rejects_forged_already_scheduled_success_in_seed_envelope(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = excel_sources(root, courses=[{"JXBID": "TARGET"}, {"JXBID": "EXTRA"}])
            config = {"allowed_changes": [], "days": [0], "periods": [1, 2, 3, 4]}
            seed = self.assert_ok("propose_repair", {**sources, "config": config})
            forged = seed["result"]["proposal"]
            forged["results"][0]["status"] = "already_scheduled"
            forged["summary"]["inserted"] = 0
            forged["changes"], forged["placements"] = [], []
            seed_path = write_json(root / "forged.json", seed)
            result = dispatch_tool("extend_repair_proposal", {**sources, "config": config,
                "seed_proposal_path": seed_path, "additional_target_ids": ["EXTRA"]})
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "input_error")

    def test_one_off_week_requires_explicit_schema_confirmation_and_retains_llxs(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = excel_sources(root, courses=[{"JXBID": "TARGET", "ZXS": 0, "LLXS": 2}])
            config = {"allowed_changes": [], "days": [0], "periods": [1, 2]}
            action = {"course_id": "TARGET", "operation": "set_one_off_week", "expected_weekly_hours": 0,
                      "expected_total_hours": 2, "teaching_week": 2, "weeks_confirmed": True,
                      "evidence": "Explicit synthetic confirmation that teaching occurs in week 2"}
            untouched = self.assert_ok("propose_repair", {**sources, "config": config})
            self.assertEqual(untouched["result"]["proposal"]["summary"]["inserted"], 0)
            preview = self.assert_ok("preview_data_corrections", {**sources, "corrections": [action]})["result"]
            self.assertEqual(preview["audit"][0]["before"]["weeks"], [1, 2])
            self.assertEqual(preview["audit"][0]["after"]["weeks"], [2])
            self.assertEqual(preview["audit"][0]["after"]["total_hours"], 2)
            corrected = self.assert_ok("propose_repair", {**sources, "config": config, "corrections": [action]})
            proposal = corrected["result"]["proposal"]
            self.assertEqual(proposal["summary"]["inserted"], 1)
            self.assertEqual(proposal["placements"][0]["weeks"], [2])
            self.assertEqual(len(proposal["placements"][0]["periods"]), 2)
            for field, value, code in (("weeks_confirmed", False, "invalid_arguments"),
                                       ("weeks_confirmed", "true", "invalid_arguments"),
                                       ("teaching_week", True, "invalid_arguments"),
                                       ("teaching_week", 3, "input_error")):
                with self.subTest(field=field, value=value):
                    failed = dispatch_tool("preview_data_corrections", {**sources, "corrections": [{**action, field: value}]})
                    self.assertFalse(failed["ok"])
                    self.assertEqual(failed["error"]["code"], code)


if __name__ == "__main__":
    unittest.main()
