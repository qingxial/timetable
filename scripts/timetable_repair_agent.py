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
    from .timetable_state import StateStore
    from .timetable_agent_tools import dispatch_tool
except ImportError:
    from repair_rule_packs import RulePackError, load_rule_pack
    from timetable_state import StateStore
    from timetable_agent_tools import dispatch_tool


INTENTS = frozenset({"diagnose", "repair", "legacy_reschedule", "correct_data", "explain", "compare", "rule_pack"})
REPAIR_MODES = frozenset({"new", "retry", "extend"})
PLANNERS = frozenset({"deterministic", "llm", "agent"})
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


def _checkpoint(run_dir, state, input_state_id):
    path = run_dir/'checkpoint.json'
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps({'run_id':run_dir.name,'state_id':state['state_id'],
                                    'input_state_id':input_state_id,'phase':'state_saved'}))
    temporary.replace(path)


def _infer_intent(text: str, has_tool_arguments: bool) -> str:
    lowered = text.lower()
    if any(word in lowered for word in ("原始调课", "旧引擎", "legacy_reschedule", "原调课脚本")):
        return "legacy_reschedule"
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


class Planner:
    """Use an LLM for bounded classification, then let policy code build the plan."""

    def __init__(self, llm_client=None):
        self.llm_client = llm_client

    def classify(self, text: str) -> dict[str, str | None]:
        """The model may select intent/mode and explain; it cannot provide tool inputs."""
        try:
            import llm_api
            chat_json = self.llm_client.chat_json if self.llm_client is not None else llm_api.chat_json
            raw = chat_json(
                """你是高校排课修复工作流的任务分类器。只选择一个 intent：
diagnose（读已有方案的剩余原因）、repair（生成候选）、correct_data（只预检显式更正）、
explain（约束冲突解释）、compare（比较候选）、rule_pack（只保存教务规则包）。
repair_mode 只能是 new、retry、extend 或 null。你不能授予晚间、周末、跨校区、数据更正或发布权限，
也不能生成任何工具参数。只输出 JSON：{"intent":"...","repair_mode":null,"reason":"..."}。""",
                text,
                temperature=0,
            )
        except Exception as error:
            # Provider errors can contain request details. Never persist them in audit files.
            raise AgentError(f"LLM planner failed ({type(error).__name__}); no tool was called") from error
        if not isinstance(raw, dict) or set(raw) != {"intent", "repair_mode", "reason"}:
            raise AgentError("LLM planner returned an unsupported plan shape")
        if (not isinstance(raw["intent"], str) or raw["intent"] not in INTENTS
                or (raw["repair_mode"] is not None and not isinstance(raw["repair_mode"], str))
                or raw["repair_mode"] not in REPAIR_MODES | {None}):
            raise AgentError("LLM planner returned an unsupported intent or repair mode")
        if not isinstance(raw["reason"], str) or not raw["reason"].strip() or len(raw["reason"]) > 500:
            raise AgentError("LLM planner did not provide a valid short reason")
        # Only fixed-schema fields enter the audited decision; discard any provider metadata.
        return {"intent": raw["intent"], "repair_mode": raw["repair_mode"], "reason": raw["reason"].strip()}

    def create_plan(self, request: dict[str, Any]) -> tuple[dict[str, Any], list[tuple[str, dict[str, Any]]]]:
        """Turn a request into a validated, ordered tool plan without model-generated arguments."""
        arguments = deepcopy(request.get("tool_arguments", {}))
        if request.get("state_id"):
            arguments["state_id"] = request["state_id"]
        authorization = request.get("authorization", {})
        _guard_authorization(authorization, arguments, request["request"])
        planner = request.get("planner", "deterministic")
        planner_decision = None
        if request.get("intent") is not None:
            intent = request["intent"]
        elif planner == "llm":
            planner_decision = self.classify(request["request"])
            intent = planner_decision["intent"]
        else:
            intent = _infer_intent(request["request"], bool(arguments))

        skills = ["timetable-repair"]
        if request.get("rule_pack") is not None or arguments.get("rule_pack_path") is not None:
            skills.append("timetable-rule-pack")
        calls: list[tuple[str, dict[str, Any]]]
        if intent == "diagnose":
            calls = [("diagnose_remaining", arguments)]
        elif intent == "legacy_reschedule":
            calls = [("legacy_reschedule", arguments)]
        elif intent == "correct_data":
            calls = [("preview_data_corrections", _preview_arguments(arguments))]
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
                calls.append(("preview_data_corrections", _preview_arguments(arguments)))
            mode = request.get("repair_mode") or (planner_decision or {}).get("repair_mode") or "new"
            calls.append(_repair_tool(arguments, mode, authorization))

        plan = {
            "intent": intent,
            "active_skills": skills,
            "planner": planner,
            "planner_decision": planner_decision,
            "permanent_policy": PERMANENT_POLICY,
            "selected_tools": [name for name, _ in calls],
            "repair_mode": (request.get("repair_mode") or (planner_decision or {}).get("repair_mode") or "new") if intent == "repair" else None,
            "reason": ((planner_decision or {}).get("reason")
                       or "Tools are selected by the local policy after explicit authorization and policy guards."),
        }
        return plan, calls


def _llm_plan(text: str) -> dict[str, str | None]:
    """Backward-compatible entry point for the constrained Planner."""
    return Planner().classify(text)


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
    allowed = {"request", "intent", "authorization", "tool_arguments", "repair_mode", "rule_pack", "run_id", "planner", "state_id", "state_root", "agent_options"}
    unknown = set(request) - allowed
    if unknown:
        raise AgentError(f"Unknown agent request fields: {sorted(unknown)}")
    if not isinstance(request.get("request"), str) or not request["request"].strip():
        raise AgentError("request must be a non-empty string")
    if "intent" in request and request["intent"] not in INTENTS:
        raise AgentError(f"intent must be one of {sorted(INTENTS)}")
    if "repair_mode" in request and request["repair_mode"] not in REPAIR_MODES:
        raise AgentError(f"repair_mode must be one of {sorted(REPAIR_MODES)}")
    if "planner" in request and request["planner"] not in PLANNERS:
        raise AgentError(f"planner must be one of {sorted(PLANNERS)}")
    if "state_id" in request and (not isinstance(request["state_id"], str)
                                  or not re.fullmatch(r"[a-f0-9]{32}", request["state_id"])):
        raise AgentError("state_id must be a 32-character state version ID")
    if "state_root" in request and (not isinstance(request["state_root"], str) or not request["state_root"].strip()):
        raise AgentError("state_root must be a non-empty path")
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
    if "跨校区" in request_text and not re.search(r"(不|禁止|不得|不能|永不|不允许).{0,3}跨校区", request_text):
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
        if arguments.get("state_id"):
            updated = deepcopy(arguments)
            updated.pop("state_id", None)
            updated.pop("seed_proposal_path", None)
            ids = updated.pop("target_ids", None)
            if ids is not None:
                updated.setdefault("config", {"allowed_changes": []})["target_ids"] = ids
            return "propose_repair", updated
        if not arguments.get("seed_proposal_path"):
            raise AgentError("repair_mode='retry' requires seed_proposal_path")
        return "retry_repair_proposal", arguments
    if mode == "extend":
        if arguments.get("state_id"):
            updated = deepcopy(arguments)
            updated.pop("state_id", None)
            updated.pop("seed_proposal_path", None)
            ids = updated.pop("additional_target_ids", None)
            if not isinstance(ids, list) or not ids:
                raise AgentError("state-based extend requires explicit additional_target_ids")
            updated.setdefault("config", {"allowed_changes": []})["target_ids"] = ids
            return "propose_repair", updated
        if not arguments.get("seed_proposal_path"):
            raise AgentError("repair_mode='extend' requires seed_proposal_path")
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


def _plan(request: dict[str, Any], llm_client=None) -> tuple[dict[str, Any], list[tuple[str, dict[str, Any]]]]:
    return Planner(llm_client=llm_client).create_plan(request)


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


def run_agent(request: dict[str, Any], artifact_root: str | Path = "agent-runs", *, llm_client=None) -> dict[str, Any]:
    """Plan and execute a bounded repair workflow, returning its audit envelope."""
    _validate_request(request)
    if request.get("planner") == "agent":
        try:
            from .timetable_agent_loop import run_agent_loop
        except ImportError:
            import sys
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            from scripts.timetable_agent_loop import run_agent_loop
        return run_agent_loop(request, artifact_root, llm_client=llm_client)
    run_dir = _new_run_dir(Path(artifact_root), request.get("run_id"))
    _write(run_dir / "request.json", request)
    responses = []
    parent_state = None
    try:
        plan, calls = _plan(request, llm_client=llm_client)
        state_store = None
        parent_state = None
        state_root = Path(request.get("state_root") or (Path(artifact_root) / "timetable-state"))
        arguments = request.get("tool_arguments", {})
        if plan["intent"] in {"repair", "legacy_reschedule"} and (arguments or request.get("state_id")):
            state_store = StateStore(state_root)
            supplied_state = request.get("state_id") or arguments.get("state_id")
            if supplied_state:
                parent_state = state_store.load(supplied_state)
            else:
                sources = {"courses": arguments.get("course_path"), "rooms": arguments.get("room_path"),
                           "schedule": arguments.get("schedule_path"), "failed": arguments.get("failed_path"),
                           "classes": arguments.get("class_path"), "teachers": arguments.get("teacher_path")}
                if not sources["classes"] and sources["courses"]:
                    discovered = Path(sources["courses"]).resolve().parent / "班级表.xlsx"
                    if discovered.is_file():
                        sources["classes"] = str(discovered)
                sources = {key: value for key, value in sources.items() if value}
                parent_state = state_store.create_baseline(sources)
            if parent_state:
                state_id = parent_state["state_id"]
                _checkpoint(run_dir, parent_state, state_id)
                source_files = state_store.dataset_files(parent_state)
                state_schedule = state_store.states / state_id / parent_state["artifacts"]["schedule"]
                state_remaining = state_store.states / state_id / parent_state["artifacts"]["remaining"]
                for _, call_args in calls:
                    call_args.pop("state_id", None)
                    call_args.pop("teacher_path", None)
                    if plan["intent"] == "repair":
                        call_args["course_path"] = str(source_files["courses"])
                        call_args["room_path"] = str(source_files["rooms"])
                        call_args["schedule_path"] = str(state_schedule)
                        call_args["failed_path"] = str(state_remaining)
                        remaining_scope = set(parent_state.get("remaining_course_ids", []))
                        inherited_corrections = [item for item in parent_state.get("operation", {}).get("corrections", [])
                                                 if item.get("course_id") in remaining_scope]
                        requested_corrections = call_args.get("corrections", [])
                        if inherited_corrections and requested_corrections[:len(inherited_corrections)] != inherited_corrections:
                            call_args["corrections"] = deepcopy(inherited_corrections) + requested_corrections
                        elif inherited_corrections:
                            call_args["corrections"] = requested_corrections
                    if "classes" in source_files:
                        call_args["class_path"] = str(source_files["classes"])
                plan["input_state_id"] = state_id
                if plan["intent"] == "repair" and plan.get("repair_mode") == "extend":
                    selected = calls[-1][1].get("config", {}).get("target_ids", [])
                    try:
                        from .repair_data import load_dataset
                    except ImportError:
                        from repair_data import load_dataset
                    dataset = load_dataset(source_files["courses"], source_files["rooms"], state_schedule,
                                           source_files.get("classes"))
                    already_placed = {row["course_id"] for row in parent_state.get("placements", [])}
                    unknown = sorted(set(selected) - set(dataset.courses))
                    occupied = sorted(set(selected) & already_placed)
                    if unknown or occupied:
                        raise AgentError(f"state-based extend IDs must exist and be unscheduled; unknown={unknown}, already_scheduled={occupied}")
                    from openpyxl import Workbook
                    target_file = run_dir / "additional-targets.xlsx"
                    book = Workbook(); sheet = book.active; sheet.append(["教学班ID"])
                    for identifier in selected:
                        sheet.append([identifier])
                    book.save(target_file); book.close()
                    for _, call_args in calls:
                        if "failed_path" in call_args:
                            call_args["failed_path"] = str(target_file)
        elif request.get("state_id") or arguments.get("state_id"):
            raise AgentError("state_id can only be used with a repair intent")
        inherited_rule_pack = (parent_state or {}).get("operation", {}).get("rule_pack_spec")
        effective_rule_pack = request.get("rule_pack") if request.get("rule_pack") is not None else inherited_rule_pack
        if effective_rule_pack is not None:
            if "timetable-rule-pack" not in plan["active_skills"]:
                plan["active_skills"].append("timetable-rule-pack")
            rule_path = run_dir / "rule-pack.json"
            _write(rule_path, effective_rule_pack)
            try:
                load_rule_pack(rule_path)
            except RulePackError as error:
                raise AgentError(f"Invalid rule pack: {error}") from error
            for _, arguments in calls:
                if "rule_pack_path" in arguments:
                    raise AgentError("rule_pack_path must be supplied only by the agent when rule_pack is present")
                arguments["rule_pack_path"] = str(rule_path)
        _write(run_dir / "plan.json", plan)
        legacy_output = None
        for index, (name, arguments) in enumerate(calls, start=1):
            if name == "legacy_reschedule":
                if not parent_state or not state_store:
                    raise AgentError("legacy_reschedule requires an explicit state_id or full baseline file inputs")
                try:
                    from .legacy_reschedule_adapter import run_legacy_reschedule
                except ImportError:
                    from legacy_reschedule_adapter import run_legacy_reschedule
                legacy_output = run_legacy_reschedule(state_store, parent_state["state_id"],
                                                      arguments.get("config", {}), request.get("authorization", {}), artifact_dir=run_dir/"legacy-reschedule")
                try:
                    from .legacy_reschedule_adapter import _cells
                except ImportError:
                    from legacy_reschedule_adapter import _cells
                before_by_course = _cells(parent_state.get('placements', []))
                after_by_course = _cells(legacy_output['placements'])
                legacy_output["summary"] = {
                    "requested": len(parent_state.get("remaining_course_ids", [])),
                    "inserted": len(set(parent_state.get("remaining_course_ids", [])) & set(after_by_course)),
                    "remaining": len(legacy_output["remaining_course_ids"]),
                    "moved": sum(1 for course_id in before_by_course.keys() & after_by_course.keys()
                                 if before_by_course[course_id] != after_by_course[course_id]),
                }
                response = {"ok": True, "tool": name, "result": legacy_output}
            else:
                response = dispatch_tool(name, arguments)
            event = {"sequence": index, "tool": name, "arguments": arguments, "response": response}
            responses.append(event)
            if not response["ok"]:
                break
        final = responses[-1]["response"] if responses else {"ok": True, "tool": None, "result": {"rule_pack_saved": True}}
        status = "completed" if final.get("ok") else "needs_attention"
        result = {"ok": final.get("ok", False), "status": status, "run_dir": str(run_dir),
                  "plan": plan, "final_response": final,
                  "source_hashes": _source_hashes(final), "tool_calls": len(responses),
                  "publication_ready": False}
        if parent_state:
            result["input_state_id"] = parent_state["state_id"]
            result["initial_state_id"] = parent_state["state_id"] if parent_state["parent_state_id"] is None else None
            proposal = final.get("result", {}).get("proposal") if isinstance(final.get("result"), dict) else None
            if result["ok"] and isinstance(proposal, dict) and state_store:
                target_results = {row["course_id"]: row["status"] for row in proposal.get("results", [])}
                prior_remaining = set(parent_state.get("remaining_course_ids", []))
                unresolved = {course_id for course_id, status_value in target_results.items()
                              if status_value not in ("placed", "already_scheduled")}
                remaining_ids = (prior_remaining - set(target_results)) | unresolved
                child = state_store.commit(
                    parent_state["state_id"], proposal.get("placements", []), sorted(remaining_ids),
                    {"engine": "repair", "tool": final.get("tool"), "parameters": proposal.get("config", {}), "run_id":run_dir.name,
                     "corrections": proposal.get("correction_parameters", []),
                     "correction_history": parent_state.get("operation", {}).get("correction_history",
                                           parent_state.get("operation", {}).get("corrections", [])) +
                                          [item for item in proposal.get("correction_parameters", [])
                                           if item not in parent_state.get("operation", {}).get("correction_history",
                                                                            parent_state.get("operation", {}).get("corrections", []))],
                     "rule_pack": proposal.get("rule_pack"),
                     "rule_pack_spec": effective_rule_pack},
                    proposal.get("validation", {}))
                _checkpoint(run_dir, child, parent_state["state_id"])
                result["output_state_id"] = child["state_id"]
                result["state_path"] = str(state_store.state_path(child["state_id"]))
                final.setdefault("result", {})["output_state_id"] = child["state_id"]
                final["result"]["parent_state_id"] = parent_state["state_id"]
                final["result"]["schedule_export"] = str(state_store.states / child["state_id"] / "schedule-export.xlsx")
            elif result["ok"] and legacy_output and state_store:
                cache_staging = legacy_output.get("legacy_cache")
                try:
                    child = state_store.commit(
                        parent_state["state_id"], legacy_output["placements"], legacy_output["remaining_course_ids"],
                        {"engine": "legacy", "tool": "reschedule_by_adjusting.reschedule", "run_id":run_dir.name,
                         "parameters": {"input_state_id": parent_state["state_id"],
                                        "config": arguments.get("config", {}),
                                        "authorization": request.get("authorization", {})},
                         "corrections": parent_state.get("operation", {}).get("corrections", []),
                         "correction_history": parent_state.get("operation", {}).get("correction_history",
                                                          parent_state.get("operation", {}).get("corrections", [])),
                         "rule_pack_spec": parent_state.get("operation", {}).get("rule_pack_spec")},
                        legacy_output["engine_validation"], legacy_output.get("legacy_cache"))
                finally:
                    if cache_staging:
                        Path(cache_staging).unlink(missing_ok=True)
                        legacy_output.pop("legacy_cache", None)
                _checkpoint(run_dir, child, parent_state["state_id"])
                result["output_state_id"] = child["state_id"]
                result["state_path"] = str(state_store.state_path(child["state_id"]))
                final["result"]["output_state_id"] = child["state_id"]
                final["result"]["parent_state_id"] = parent_state["state_id"]
                final["result"]["schedule_export"] = str(state_store.states / child["state_id"] / "schedule-export.xlsx")
    except (AgentError, OSError, ValueError) as error:
        plan = {"intent": request.get("intent"), "active_skills": [], "permanent_policy": PERMANENT_POLICY,
                "selected_tools": [], "reason": str(error)}
        if not (run_dir / 'plan.json').exists():
            _write(run_dir / 'plan.json', plan)
        else:
            _write(run_dir / 'rejection.json', {'reason': str(error),
                   'input_state_id': parent_state['state_id'] if parent_state else None})
        result = {"ok": False, "status": "blocked", "run_dir": str(run_dir), "plan": plan,
                  "error": {"code": "policy_or_plan_rejected", "message": str(error)},
                  "source_hashes": parent_state["source_hashes"] if parent_state else {}, "tool_calls": len(responses), "publication_ready": False}
    if not result.get('ok') and parent_state:
        result['input_state_id'] = parent_state['state_id']
        result['output_state_id'] = parent_state['state_id']
        result['preserved_state_id'] = parent_state['state_id']
    with (run_dir / "tool-calls.jsonl").open("x", encoding="utf-8") as stream:
        for event in responses:
            stream.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
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
