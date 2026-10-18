"""Auditable, policy-constrained orchestration for timetable repair tools.

This is deliberately an agent *workflow*, not an unrestricted autonomous
executor.  It selects from the existing JSON tools, persists every decision,
and refuses publication, cross-campus relaxation, or time-domain expansion
outside the explicit authorization object.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any
from uuid import uuid4

try:
    from .repair_rule_packs import RulePackError, load_rule_pack
    from .timetable_agent_tools import dispatch_tool
except ImportError:
    from repair_rule_packs import RulePackError, load_rule_pack
    from timetable_agent_tools import dispatch_tool


INTENTS = frozenset({"diagnose", "repair", "correct_data", "explain", "compare", "rule_pack"})
REPAIR_MODES = frozenset({"new", "retry", "extend"})
PERMANENT_POLICY = {
    "cross_campus": False,
    "source_workbooks_modified": False,
    "publication": "never_by_agent",
    "interpretation": "Courses never cross campuses. The agent creates only audited dry-run candidates; it does not alter source workbooks or publish a timetable.",
}


class AgentError(ValueError):
    """A request cannot safely enter the timetable repair workflow."""


def _json(path: Path) -> Any:
    def duplicate_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise AgentError(f"Duplicate JSON field: {key}")
            result[key] = value
        return result

    with path.open(encoding="utf-8") as stream:
        return json.load(stream, object_pairs_hook=duplicate_keys)


def _write(path: Path, value: Any) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")


def _infer_intent(text: str, has_tool_arguments: bool) -> str:
    lowered = text.lower()
    if any(word in lowered for word in ("比较", "对比", "compare")):
        return "compare"
    if any(word in lowered for word in ("解释", "冲突核", "quickxplain", "explain")):
        return "explain"
    if any(word in lowered for word in ("诊断", "原因", "为什么排不", "diagnose")) and not has_tool_arguments:
        return "diagnose"
    if any(word in lowered for word in ("规则包", "教室替代", "可互相替代")) and not has_tool_arguments:
        return "rule_pack"
    if any(word in lowered for word in ("更正", "预检", "核对人数")) and "试排" not in lowered:
        return "correct_data"
    return "repair"


def _new_run_dir(artifact_root: Path, supplied: str | None) -> Path:
    if supplied is not None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", supplied):
            raise AgentError("run_id must contain only letters, digits, dot, underscore, or hyphen")
        name = supplied
    else:
        name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    artifact_root.mkdir(parents=True, exist_ok=True)
    run_dir = artifact_root / name
    run_dir.mkdir(exist_ok=False)
    return run_dir


def _require_object(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AgentError(f"{location} must be an object")
    return value


def _validate_request(request: dict[str, Any]) -> None:
    if not isinstance(request, dict):
        raise AgentError("Agent request must be an object")
    allowed = {"request", "intent", "authorization", "tool_arguments", "repair_mode", "rule_pack", "run_id"}
    unknown = set(request) - allowed
    if unknown:
        raise AgentError(f"Unknown agent request fields: {sorted(unknown)}")
    if not isinstance(request.get("request"), str) or not request["request"].strip():
        raise AgentError("request must be a non-empty string")
    if "intent" in request and request["intent"] not in INTENTS:
        raise AgentError(f"intent must be one of {sorted(INTENTS)}")
    if "repair_mode" in request and request["repair_mode"] not in REPAIR_MODES:
        raise AgentError(f"repair_mode must be one of {sorted(REPAIR_MODES)}")
    _require_object(request.get("authorization", {}), "authorization")
    _require_object(request.get("tool_arguments", {}), "tool_arguments")
    if "rule_pack" in request:
        _require_object(request["rule_pack"], "rule_pack")


def _guard_authorization(authorization: dict[str, Any], arguments: dict[str, Any], request_text: str) -> None:
    allowed = {"allow_evening", "allow_weekend", "publication"}
    unknown = set(authorization) - allowed
    if unknown:
        raise AgentError(f"Unknown authorization fields: {sorted(unknown)}")
    if authorization.get("publication", False) is not False:
        raise AgentError("The timetable repair agent never publishes a timetable")
    if "跨校区" in request_text and "不跨校区" not in request_text:
        raise AgentError("Cross-campus adjustment is permanently prohibited")
    config = arguments.get("config", {})
    if not isinstance(config, dict):
        return
    if any(day in (5, 6) for day in config.get("days", [])) and not authorization.get("allow_weekend", False):
        raise AgentError("Weekend search requires authorization.allow_weekend=true")
    if any(period > 8 for period in config.get("periods", [])) and not authorization.get("allow_evening", False):
        raise AgentError("Evening search requires authorization.allow_evening=true")
    stages = arguments.get("stages", [])
    if "weekend" in stages and not authorization.get("allow_weekend", False):
        raise AgentError("Weekend fallback requires authorization.allow_weekend=true")
    if "evening" in stages and not authorization.get("allow_evening", False):
        raise AgentError("Evening fallback requires authorization.allow_evening=true")


def _repair_tool(arguments: dict[str, Any], mode: str, authorization: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if mode == "retry":
        return "retry_repair_proposal", arguments
    if mode == "extend":
        return "extend_repair_proposal", arguments
    if "seed_proposal_path" in arguments:
        raise AgentError("A seed proposal requires repair_mode='retry' or 'extend'")
    if authorization.get("allow_evening", False) or authorization.get("allow_weekend", False):
        derived = ["daytime"]
        if authorization.get("allow_evening", False):
            derived.append("evening")
        if authorization.get("allow_weekend", False):
            derived.append("weekend")
        supplied = arguments.get("stages")
        if supplied is not None and supplied != derived:
            raise AgentError("Fallback stages must exactly match the explicit time-domain authorization")
        updated = deepcopy(arguments)
        updated["stages"] = derived
        return "repair_with_fallbacks", updated
    return "propose_repair", arguments


def _preview_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Preview accepts source inputs and corrections, never repair policy."""
    allowed = {"course_path", "room_path", "schedule_path", "failed_path", "class_path", "rule_pack_path", "corrections"}
    return {key: value for key, value in arguments.items() if key in allowed}


def _plan(request: dict[str, Any]) -> tuple[dict[str, Any], list[tuple[str, dict[str, Any]]]]:
    arguments = deepcopy(request.get("tool_arguments", {}))
    authorization = request.get("authorization", {})
    _guard_authorization(authorization, arguments, request["request"])
    intent = request.get("intent") or _infer_intent(request["request"], bool(arguments))
    skills = ["timetable-repair"]
    calls: list[tuple[str, dict[str, Any]]]
    if request.get("rule_pack") is not None or arguments.get("rule_pack_path") is not None:
        skills.append("timetable-rule-pack")
    if intent == "diagnose":
        calls = [("diagnose_remaining", arguments)]
    elif intent == "correct_data":
        calls = [("preview_data_corrections", arguments)]
    elif intent == "explain":
        calls = [("explain_conflict", arguments)]
    elif intent == "compare":
        calls = [("compare_proposals", arguments)]
    elif intent == "rule_pack":
        if request.get("rule_pack") is None:
            raise AgentError("rule_pack intent requires a declarative rule_pack object")
        calls = []
    else:
        calls = []
        if arguments.get("corrections"):
            calls.append(("preview_data_corrections", _preview_arguments(deepcopy(arguments))))
        calls.append(_repair_tool(arguments, request.get("repair_mode", "new"), authorization))
    plan = {
        "intent": intent,
        "active_skills": skills,
        "permanent_policy": PERMANENT_POLICY,
        "selected_tools": [name for name, _ in calls],
        "reason": "All repair candidates are produced by the existing bounded tools after explicit authorization and policy guards.",
    }
    return plan, calls


def _source_hashes(response: dict[str, Any]) -> dict[str, str]:
    result = response.get("result", {}) if response.get("ok") else {}
    if not isinstance(result, dict):
        return {}
    proposal = result.get("proposal", {})
    if isinstance(proposal, dict) and isinstance(proposal.get("source_hashes"), dict):
        return proposal["source_hashes"]
    if isinstance(result.get("source_hashes"), dict):
        return result["source_hashes"]
    if isinstance(result.get("effective_source_hashes"), dict):
        return result["effective_source_hashes"]
    return {}


def run_agent(request: dict[str, Any], artifact_root: str | Path = "agent-runs") -> dict[str, Any]:
    """Plan and execute a bounded repair workflow, returning its audit envelope."""
    _validate_request(request)
    run_dir = _new_run_dir(Path(artifact_root), request.get("run_id"))
    _write(run_dir / "request.json", request)
    responses = []
    try:
        plan, calls = _plan(request)
        if request.get("rule_pack") is not None:
            rule_path = run_dir / "rule-pack.json"
            _write(rule_path, request["rule_pack"])
            try:
                load_rule_pack(rule_path)
            except RulePackError as error:
                raise AgentError(f"Invalid rule pack: {error}") from error
            for _, arguments in calls:
                if "rule_pack_path" in arguments:
                    raise AgentError("rule_pack_path must be supplied only by the agent when rule_pack is present")
                arguments["rule_pack_path"] = str(rule_path)
        _write(run_dir / "plan.json", plan)
        for index, (name, arguments) in enumerate(calls, start=1):
            response = dispatch_tool(name, arguments)
            event = {"sequence": index, "tool": name, "arguments": arguments, "response": response}
            responses.append(event)
            if not response["ok"]:
                break
        with (run_dir / "tool-calls.jsonl").open("x", encoding="utf-8") as stream:
            for event in responses:
                stream.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        final = responses[-1]["response"] if responses else {"ok": True, "tool": None, "result": {"rule_pack_saved": True}}
        status = "completed" if final.get("ok") else "needs_attention"
        result = {"ok": final.get("ok", False), "status": status, "run_dir": str(run_dir),
                  "plan": plan, "final_response": final,
                  "source_hashes": _source_hashes(final), "tool_calls": len(responses),
                  "publication_ready": False}
    except AgentError as error:
        plan = {"intent": request.get("intent"), "active_skills": [], "permanent_policy": PERMANENT_POLICY,
                "selected_tools": [], "reason": str(error)}
        _write(run_dir / "plan.json", plan)
        (run_dir / "tool-calls.jsonl").touch(exist_ok=False)
        result = {"ok": False, "status": "blocked", "run_dir": str(run_dir), "plan": plan,
                  "error": {"code": "policy_or_plan_rejected", "message": str(error)},
                  "source_hashes": {}, "tool_calls": 0, "publication_ready": False}
    _write(run_dir / "result.json", result)
    manifest = {"run_id": run_dir.name, "created_at": datetime.now(timezone.utc).isoformat(),
                "artifacts": [path.name for path in sorted(run_dir.iterdir())],
                "status": result["status"], "source_hashes": result["source_hashes"],
                "permanent_policy": PERMANENT_POLICY}
    _write(run_dir / "manifest.json", manifest)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, type=Path, help="Agent request JSON; source files remain read-only")
    parser.add_argument("--artifact-root", type=Path, default=Path("agent-runs"), help="New run directories are created here")
    args = parser.parse_args(argv)
    try:
        request = _json(args.request)
        response = run_agent(request, args.artifact_root)
    except (AgentError, OSError, ValueError) as error:
        print(json.dumps({"ok": False, "status": "input_error", "error": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(response, ensure_ascii=False, indent=2))
    return 0 if response["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
