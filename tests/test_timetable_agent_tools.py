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


class ToolContractTests(unittest.TestCase):
    def test_all_advertised_schema_names_have_dispatch(self):
        self.assertEqual({s["name"] for s in TOOL_SCHEMAS},
                         {"diagnose_remaining", "propose_repair", "compare_proposals"})
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


if __name__ == "__main__":
    unittest.main()
