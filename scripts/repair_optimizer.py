"""Joint optimization of a finite, explicitly bounded timetable neighborhood.

This is an exact backtracking adaptation of candidate-assignment and quality
frontier ideas, not a reproduction of a published IP solver. No global
timetabling optimality or infeasibility claim is made for a retained domain.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import json
import math
import time

try:
    from .repair_courses import (CandidateFactory, Occupancy, RepairConfig, keys_for,
                                 placement_dict, preference_cost, structured_college_time,
                                 required_week_load, validate_changes, week_load)
    from .repair_data import parse_time_windows
except ImportError:
    from repair_courses import (CandidateFactory, Occupancy, RepairConfig, keys_for,
                                placement_dict, preference_cost, structured_college_time,
                                required_week_load, validate_changes, week_load)
    from repair_data import parse_time_windows


@dataclass
class OptimizerConfig:
    quality_weights: dict[str, float]
    time_limit_seconds: float = 60.0
    max_search_nodes: int = 200000
    candidate_limit_per_course: int = 200

    def validate(self):
        names = {"time_preference", "evening", "weekend"}
        if not isinstance(self.quality_weights, dict) or set(self.quality_weights) != names:
            raise ValueError("quality_weights must explicitly contain time_preference, evening, and weekend")
        for key, value in self.quality_weights.items():
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError(f"quality_weights.{key} must be non-negative and finite")
        for key in ("max_search_nodes", "candidate_limit_per_course"):
            if type(getattr(self, key)) is not int or getattr(self, key) < 1:
                raise ValueError(f"{key} must be a positive integer")
        if (type(self.time_limit_seconds) not in (int, float)
                or not math.isfinite(self.time_limit_seconds) or self.time_limit_seconds <= 0):
            raise ValueError("time_limit_seconds must be positive and finite")


@dataclass(frozen=True)
class _Option:
    course_id: str
    entries: tuple
    keys: frozenset
    inserted: bool
    moved: bool
    quality: float
    components: dict


class _Budget:
    def __init__(self, config):
        self.started = time.monotonic()
        self.deadline = self.started + config.time_limit_seconds
        self.node_limit = config.max_search_nodes
        self.nodes = 0
        self.reason = None

    def available(self):
        if self.nodes >= self.node_limit:
            self.reason = "node_limit"
            return False
        if time.monotonic() >= self.deadline:
            self.reason = "time_limit"
            return False
        return True


def _signature(entries):
    return tuple(sorted((tuple(sorted(p.weeks)), p.day, tuple(p.periods), p.room_id) for p in entries))


def _quality(course, entries, weights):
    pref = parse_time_windows(course.prefer)
    patterns = {(p.day, p.periods) for p in entries}
    cells = {(week, p.day, period) for p in entries for week in p.weeks for period in p.periods}
    components = {
        "time_preference": sum(preference_cost(day, periods, pref) for day, periods in patterns),
        "evening": sum(period >= 9 for _, _, period in cells),
        "weekend": sum(day >= 5 for _, day, _ in cells),
    }
    return sum(weights[key] * value for key, value in components.items()), components


def _configs(repair_config, optimizer_config):
    if not isinstance(repair_config, dict) or "allowed_changes" not in repair_config:
        raise ValueError("repair_config requires an explicit allowed_changes whitelist")
    if not isinstance(optimizer_config, dict):
        raise ValueError("optimizer_config must be an object")
    rc, oc = RepairConfig(**repair_config), OptimizerConfig(**optimizer_config)
    rc.validate()
    oc.validate()
    return rc, oc


def _prepare(dataset, target_ids, config, optimizer, budget):
    if not isinstance(target_ids, (list, tuple)) or any(not isinstance(cid, str) for cid in target_ids):
        raise ValueError("target_ids must be a list of course identifiers")
    requested = list(dict.fromkeys(config.target_ids or target_ids))
    unknown = (set(requested) | set(config.movable_ids) | set(config.locked_ids)
               | set(config.reviewed_soft_time_ids)) - dataset.courses.keys()
    if unknown:
        raise ValueError(f"Unknown course IDs: {sorted(unknown)}")
    state = Occupancy(dataset)
    before = {cid: tuple(entries) for cid, entries in state.placements.items()}
    frozen, movable = {}, []
    for cid in sorted(set(config.movable_ids)):
        course = dataset.courses[cid]
        if cid not in before:
            frozen[cid] = "not_previously_scheduled"
        elif cid in config.locked_ids:
            frozen[cid] = "locked"
        elif config.max_moved_courses == 0:
            frozen[cid] = "movement_budget_zero"
        elif cid in state.uncertain_ids:
            frozen[cid] = "incomplete_baseline"
        elif course.issues:
            frozen[cid] = "data_issue"
        elif (course.special.strip() and
              (cid not in config.reviewed_soft_time_ids or not structured_college_time(course))):
            frozen[cid] = "unreviewed_special_requirement"
        else:
            try:
                expected = required_week_load(course)
                parse_time_windows(course.prefer)
            except ValueError:
                frozen[cid] = "unsupported_baseline_metadata"
                continue
            if week_load(before[cid]) != expected:
                frozen[cid] = "baseline_week_load_not_supported"
            else:
                movable.append(cid)
    for cid in movable:
        state.remove(cid)
    new_targets = [cid for cid in requested if cid not in before and cid not in config.locked_ids]
    if len(new_targets) + len(movable) > 800:
        raise ValueError("Select a smaller local neighborhood: backtracking supports at most 800 variable courses")
    factory = CandidateFactory(dataset, config, budget.deadline)
    domains, diagnostics = {}, {}
    retention_truncated, generation_truncated = [], []
    for cid in movable + new_targets:
        course = dataset.courses[cid]
        options = []
        seen = set()
        if cid in before:
            quality, parts = _quality(course, before[cid], optimizer.quality_weights)
            options.append(_Option(cid, before[cid], frozenset(key for p in before[cid] for key in keys_for(course, p)),
                                   False, False, quality, parts))
            seen.add(_signature(before[cid]))
        candidates = factory.get(cid) if budget.available() else []
        diag = dict(factory.diagnostics.get(cid, {"reason": "search_limit", "truncated": True}))
        if diag.get("truncated"):
            generation_truncated.append(cid)
        blocked, retained, filtered = 0, 0, 0
        for _, entries in candidates:
            if not budget.available():
                if cid not in generation_truncated:
                    generation_truncated.append(cid)
                break
            signature = _signature(entries)
            if signature in seen:
                continue
            # Apply the local cap after excluding fixed-neighborhood blockers.
            if state.blockers(cid, entries):
                blocked += 1
                continue
            filtered += 1
            if retained >= optimizer.candidate_limit_per_course:
                retention_truncated.append(cid)
                break
            seen.add(signature)
            quality, parts = _quality(course, entries, optimizer.quality_weights)
            options.append(_Option(cid, tuple(entries), frozenset(key for p in entries for key in keys_for(course, p)),
                                   cid not in before, cid in before, quality, parts))
            retained += 1
        diag.update(fixed_neighborhood_blocked=blocked, retained_alternatives=retained,
                    filtered_candidates_examined=filtered)
        if not options and diag.get("reason") == "candidates_generated":
            diag["reason"] = "blocked_by_fixed_neighborhood"
        diagnostics[cid] = diag
        domains[cid] = sorted(options, key=lambda option: (option.moved, option.quality, _signature(option.entries)))
    initial = {cid: next(option for option in domains[cid] if not option.moved) for cid in movable}
    # Option ordering is stable. Targets with fewer choices are searched first;
    # baseline courses remain mandatory and may only select full-course options.
    order = sorted(new_targets, key=lambda cid: (len(domains[cid]), cid)) + sorted(movable, key=lambda cid: (len(domains[cid]), cid))
    fingerprint = hashlib.sha256(json.dumps({cid: [_signature(o.entries) for o in options]
                                            for cid, options in sorted(domains.items())}, sort_keys=True).encode()).hexdigest()
    return {"dataset": dataset, "config": config, "optimizer": optimizer, "requested": requested,
            "before": before, "original_state": Occupancy(dataset), "domains": domains, "order": order,
            "new_targets": set(new_targets), "movable": movable, "initial": initial, "diagnostics": diagnostics,
            "scope": {"variable_target_ids": new_targets, "variable_movable_ids": movable,
                      "frozen_movable_ids": frozen, "outside_neighborhood": "fixed",
                      "source_generation_cap": config.max_candidates_per_course,
                      "retained_alternative_cap_after_fixed_filter": optimizer.candidate_limit_per_course,
                      "generation_truncated_ids": sorted(set(generation_truncated)),
                      "retention_truncated_ids": sorted(set(retention_truncated)),
                      "retained_counts": {cid: len(options) for cid, options in domains.items()},
                      "candidate_domain_sha256": fingerprint}}


def _score(solution):
    return (sum(o.inserted for o in solution.values()), sum(o.moved for o in solution.values()),
            sum(o.quality for o in solution.values()))


def _search(context, budget, movement_budget, *, fixed_insertions=None, objective="insertions", seed=None):
    domains, order = context["domains"], context["order"]
    targets = context["new_targets"]
    seed = context["initial"] if seed is None else seed
    seed_score = _score(seed)
    valid_seed = seed_score[1] <= movement_budget and (fixed_insertions is None or seed_score[0] == fixed_insertions)
    best = dict(seed) if valid_seed else None
    best_score = seed_score if valid_seed else None
    remaining = [0] * (len(order) + 1)
    for index in range(len(order) - 1, -1, -1):
        remaining[index] = remaining[index + 1] + (order[index] in targets and bool(domains[order[index]]))
    cells, chosen = {}, {}
    start_nodes = budget.nodes
    complete = True

    def preferable(score, previous):
        if previous is None:
            return True
        if objective == "insertions":
            return (-score[0], score[1], score[2]) < (-previous[0], previous[1], previous[2])
        if objective == "quality":
            return (score[2], score[1]) < (previous[2], previous[1])
        return (score[1], score[2]) < (previous[1], previous[2])

    def visit(index, inserted, moved, quality):
        nonlocal best, best_score, complete
        if not budget.available():
            complete = False
            return
        budget.nodes += 1
        if moved > movement_budget:
            return
        upper = inserted + remaining[index]
        if fixed_insertions is not None and (inserted > fixed_insertions or upper < fixed_insertions):
            return
        if best_score is not None:
            if objective == "insertions" and upper <= best_score[0]:
                return
            if objective != "insertions":
                if objective == "quality" and quality > best_score[2]:
                    return
                if objective == "movement_then_quality" and (moved > best_score[1] or moved == best_score[1] and quality > best_score[2]):
                    return
        if index == len(order):
            score = (inserted, moved, quality)
            if preferable(score, best_score):
                best, best_score = dict(chosen), score
            return
        cid = order[index]
        for option in domains[cid]:
            if not budget.available():
                complete = False
                return
            # Existing conflicts between two unchanged baseline options remain
            # baseline facts; every inserted/moved option must be conflict free.
            changing = option.inserted or option.moved
            if any(any(changing or other.inserted or other.moved for other in cells.get(key, ())) for key in option.keys):
                continue
            chosen[cid] = option
            for key in option.keys:
                cells.setdefault(key, []).append(option)
            visit(index + 1, inserted + option.inserted, moved + option.moved, quality + option.quality)
            for key in option.keys:
                cells[key].pop()
                if not cells[key]:
                    del cells[key]
            del chosen[cid]
            if not complete:
                return
        if cid in targets:
            visit(index + 1, inserted, moved, quality)

    visit(0, 0, 0, 0.0)
    return {"solution": best, "score": best_score, "complete": complete,
            "objective": objective,
            "search_nodes": budget.nodes - start_nodes, "stop_reason": None if complete else budget.reason,
            "insertion_upper_bound": best_score[0] if complete and objective == "insertions" and best_score else remaining[0]}


def _phase_report(result):
    return {"status": ("optimal_in_retained_domain" if result["solution"] is not None else "infeasible_in_retained_domain")
            if result["complete"] else "unknown_budget_exhausted",
            "incumbent": None if result["score"] is None else {
                "inserted": result["score"][0], "moved": result["score"][1], "quality": result["score"][2]},
            "search_nodes": result["search_nodes"], "stop_reason": result["stop_reason"],
            "insertion_upper_bound": result["insertion_upper_bound"],
            "bounds_scope": "retained_candidate_domain",
            "quality_lower_bound": result["score"][2] if result["complete"] and result["score"] is not None
            and result["objective"] in ("quality", "movement_then_quality") else 0.0,
            "quality_bound_condition": "For movement_then_quality, the quality bound is conditional on the optimal movement count."}


def _proposal(context, solution, budget, phase1, phase2, movement_budget):
    data, config, before = context["dataset"], context["config"], context["before"]
    final = Occupancy(data)
    changing = {cid for cid, option in solution.items() if option.inserted or option.moved}
    moved = {cid for cid in changing if cid in before}
    for cid in moved:
        final.remove(cid)
    for cid in changing:
        final.put(cid, solution[cid].entries)
    if not before.keys() <= final.placements.keys():
        raise RuntimeError("Optimizer rejected: a previously scheduled course was lost")
    if any(week_load(before[cid]) != week_load(final.placements[cid]) for cid in before):
        raise RuntimeError("Optimizer rejected: original course teaching load changed")
    errors = validate_changes(data, before, final, changing, config)
    if errors:
        raise RuntimeError(f"Optimizer validation rejected changed courses: {errors[:5]}")
    changes = []
    for cid in sorted(changing):
        course, option = data.courses[cid], solution[cid]
        pref = parse_time_windows(course.prefer)
        relaxed = set()
        if pref and any((p.day, period) not in pref for p in option.entries for period in p.periods):
            relaxed.add("time_preference")
            if course.special.strip():
                relaxed.add("reviewed_special_time_requirement")
        for p in option.entries:
            room = data.rooms[p.room_id]
            if course.buildings and room.building not in course.buildings:
                relaxed.add("building_preference")
            if not course.explicit_rooms and course.historical_rooms and room.id not in course.historical_rooms:
                relaxed.add("historical_room")
        changes.append({"course_id": cid, "course_name": course.name,
                        "operation": "move" if cid in before else "insert", "relaxed_fields": sorted(relaxed),
                        "original_time_preference": course.prefer, "original_special_requirement": course.special,
                        "planned_weekly_loads": {str(week): hours for week, hours in required_week_load(course).items()},
                        "before": [placement_dict(p) for p in before.get(cid, ())],
                        "after": [placement_dict(p) for p in option.entries]})
    results = []
    for cid in context["requested"]:
        diag = dict(context["diagnostics"].get(cid, {}))
        if cid in before:
            status = "already_scheduled"
        elif cid in config.locked_ids:
            status = "locked"
        elif cid in solution:
            status = "placed"
        elif not context["domains"].get(cid):
            status = diag.get("reason", "not_found_within_limits")
        else:
            status = "not_selected_in_local_optimum" if phase1["complete"] else "not_found_within_limits"
        results.append({"course_id": cid, "status": status, "diagnosis": diag})
    score = _score(solution)
    return {"schema_version": 1, "mode": "proposal", "config": asdict(replace(config, max_moved_courses=movement_budget)),
            "optimizer_config": asdict(context["optimizer"]), "source_hashes": dict(data.source_hashes),
            "summary": {"requested": len(context["requested"]), "inserted": score[0], "moved": score[1],
                        "baseline_scheduled": len(before), "proposed_scheduled": len(final.placements),
                        "search_nodes": budget.nodes, "elapsed_seconds": round(time.monotonic() - budget.started, 3)},
            "validation": {"changed_courses_checked": len(changing), "new_conflicts": 0, "hours_and_weeks_preserved": True,
                           "baseline_conflict_cells": context["original_state"].recorded_conflict_counts,
                           "baseline_uncertain_course_ids": sorted(final.uncertain_ids),
                           "scope": "Changed courses validated against known resources; existing conflicts and unknown student enrollment remain baseline limitations."},
            "data_issues": list(data.issues), "results": results, "changes": changes,
            "placements": [placement_dict(p) for cid in sorted(final.placements) for p in final.placements[cid]],
            "solver": {"backend": "exact_finite_candidate_backtracking", "phase_1": _phase_report(phase1),
                       "phase_2": _phase_report(phase2), "candidate_scope": context["scope"],
                       "quality_components": {key: sum(option.components[key] for option in solution.values())
                                              for key in context["optimizer"].quality_weights},
                       "quality": score[2], "quality_definition": "Weighted time_preference per unique daily block pattern, plus evening (period>=9) and weekend (day>=5) teaching-period cells including weeks, over variable original courses and inserted targets; fixed courses contribute a constant and are omitted.",
                       "objective": "maximize_insertions_then_minimize_moved_courses_then_quality",
                       "optimality_scope": "Only retained candidates and the explicit movable neighborhood; candidate truncation and outside courses prevent global claims.",
                       "global_optimality_proven": False, "global_infeasibility_proven": False}}


def optimize_local(dataset, target_ids, repair_config: dict, optimizer_config: dict):
    """Maximize complete insertions, then minimize moves and weighted quality."""
    config, optimizer = _configs(repair_config, optimizer_config)
    budget = _Budget(optimizer)
    context = _prepare(dataset, target_ids, config, optimizer, budget)
    phase1 = _search(context, budget, config.max_moved_courses)
    phase2 = _search(context, budget, config.max_moved_courses, fixed_insertions=phase1["score"][0],
                     objective="movement_then_quality", seed=phase1["solution"])
    return _proposal(context, phase2["solution"] or phase1["solution"], budget, phase1, phase2, config.max_moved_courses)


def quality_frontier(dataset, target_ids, repair_config: dict, optimizer_config: dict, movement_budgets):
    """Hold success count and candidate domain fixed; minimize quality per move cap.

    The common success count is the best found at the largest authorized cap.
    It is called proven only when stage one exhausts the retained candidate domain.
    Generation, stage one and every frontier point share one time/node budget.
    """
    config, optimizer = _configs(repair_config, optimizer_config)
    if (not isinstance(movement_budgets, (list, tuple)) or not movement_budgets
            or any(type(value) is not int or not 0 <= value <= config.max_moved_courses for value in movement_budgets)):
        raise ValueError("movement_budgets must be integers within the authorized max_moved_courses")
    caps = sorted(set(movement_budgets))
    budget = _Budget(optimizer)
    context = _prepare(dataset, target_ids, config, optimizer, budget)
    stage1 = _search(context, budget, caps[-1])
    common = stage1["score"][0]
    points = []
    for cap in caps:
        result = _search(context, budget, cap, fixed_insertions=common, objective="quality", seed=stage1["solution"])
        report = {"movement_budget": cap, **_phase_report(result)}
        if result["solution"] is not None:
            proposal = _proposal(context, result["solution"], budget, stage1, result, cap)
            proposal["solver"]["objective"] = "fixed_insertions_minimize_quality_under_movement_budget"
            report["proposal"] = proposal
        points.append(report)
    return {"schema_version": 1, "mode": "quality_frontier", "source_hashes": dict(dataset.source_hashes),
            "config": asdict(config), "optimizer_config": asdict(optimizer), "target_ids": context["requested"],
            "fixed_inserted_count": common, "success_count_proven_in_retained_domain": stage1["complete"],
            "phase_1": _phase_report(stage1), "candidate_scope": context["scope"], "points": points,
            "total_search_nodes": budget.nodes, "elapsed_seconds": round(time.monotonic() - budget.started, 3),
            "global_optimality_proven": False, "global_infeasibility_proven": False,
            "interpretation": "Same retained candidate domain and same insertion count across movement caps. A missing point is infeasible only in this finite neighborhood if search completed; otherwise it is unknown. This is a quality/budget adaptation, not a published IP implementation."}
