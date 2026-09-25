"""Finite-domain consistency checks and verified QuickXplain explanations.

This module never treats failure of the timetable repair heuristic as UNSAT.
Candidates are complete assignments for one course, with indivisible resource
sets. Required courses choose exactly one candidate; optional courses may stay
unassigned. Resource exclusivity is always a background constraint.

``explain_finite_conflict`` accepts a JSON-compatible problem with ``domains``,
``required_courses``, ``constraints``, ``background_ids``, ``foreground_ids``
and ``domains_complete``. Constraints have kind ``require_course`` (course_id),
``block_resources`` (resource_keys), or ``block_candidates`` (referenced by a
candidate's blocked_by list). Foreground order chooses deterministically among
multiple irreducible conflicts; it does not minimize conflict cardinality.

Every invocation shares one node, oracle-call and wall-clock budget. UNKNOWN
aborts the explanation: a timed-out or truncated search is not an UNSAT proof.
See Junker, AAAI 2004, Figure 1 and the author's QuickXplain erratum.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import json
import math
import time


class _Unknown(Exception):
    pass


def _strings(value, label):
    if not isinstance(value, list) or any(not isinstance(v, str) or not v for v in value):
        raise ValueError(f"{label} must be a list of nonempty strings")
    if len(value) != len(set(value)):
        raise ValueError(f"{label} contains duplicates")
    return tuple(value)


@dataclass(frozen=True)
class _Candidate:
    id: str
    resources: frozenset
    blocked_by: frozenset


class _Problem:
    def __init__(self, problem):
        if not isinstance(problem, dict) or not isinstance(problem.get("domains"), dict):
            raise ValueError("problem.domains must be a mapping")
        # A defensive JSON copy freezes all semantics throughout the explanation.
        try:
            raw = json.loads(json.dumps(problem, ensure_ascii=False, allow_nan=False))
        except (TypeError, ValueError) as exc:
            raise ValueError("problem must contain finite JSON-compatible values") from exc
        self.raw = raw
        self.domains = {}
        for cid, items in sorted(raw["domains"].items()):
            if not isinstance(cid, str) or not cid or not isinstance(items, list):
                raise ValueError("domains must map nonempty course IDs to candidate lists")
            parsed = []
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
                    raise ValueError("each candidate requires a nonempty string id")
                parsed.append(_Candidate(item["id"],
                    frozenset(_strings(item.get("resource_keys", []), "resource_keys")),
                    frozenset(_strings(item.get("blocked_by", []), "blocked_by"))))
            if len({p.id for p in parsed}) != len(parsed):
                raise ValueError(f"duplicate candidate IDs for {cid}")
            self.domains[cid] = tuple(sorted(parsed, key=lambda p: p.id))
        self.required = frozenset(_strings(raw.get("required_courses", list(self.domains)), "required_courses"))
        if self.required - self.domains.keys():
            raise ValueError("required_courses contains an unknown course")
        self.fixed = frozenset(_strings(raw.get("fixed_resource_keys", []), "fixed_resource_keys"))
        self.constraints = {}
        if not isinstance(raw.get("constraints", []), list):
            raise ValueError("constraints must be a list")
        for source in raw.get("constraints", []):
            if not isinstance(source, dict) or not isinstance(source.get("id"), str) or not source["id"]:
                raise ValueError("every constraint requires a nonempty string id")
            c = dict(source)
            if c["id"] in self.constraints:
                raise ValueError("duplicate constraint IDs")
            if c.get("kind") not in {"require_course", "block_resources", "block_candidates"}:
                raise ValueError(f"unsupported constraint kind: {c.get('kind')}")
            for field, default in (("explainable", True), ("relaxable", False)):
                c.setdefault(field, default)
                if type(c[field]) is not bool:
                    raise ValueError(f"constraint {field} must be Boolean")
            if c["kind"] == "require_course" and c.get("course_id") not in self.domains:
                raise ValueError("require_course references an unknown course")
            if c["kind"] == "block_resources":
                _strings(c.get("resource_keys", []), "constraint.resource_keys")
            self.constraints[c["id"]] = c
        self.background = _strings(raw.get("background_ids", [
            cid for cid, c in sorted(self.constraints.items()) if not c["explainable"]]), "background_ids")
        self.foreground = _strings(raw.get("foreground_ids", [
            cid for cid in sorted(self.constraints) if cid not in self.background]), "foreground_ids")
        b, f = set(self.background), set(self.foreground)
        if b & f or b | f != self.constraints.keys():
            raise ValueError("background_ids and foreground_ids must partition all constraints")
        if any(not self.constraints[cid]["explainable"] for cid in self.foreground):
            raise ValueError("foreground constraints must be explainable")
        for candidates in self.domains.values():
            for candidate in candidates:
                if candidate.blocked_by - self.constraints.keys():
                    raise ValueError("candidate.blocked_by references an unknown constraint")
        for field, default in (("domains_complete", False), ("model_complete", True)):
            if type(raw.get(field, default)) is not bool:
                raise ValueError(f"{field} must be Boolean")
        self.complete = raw.get("domains_complete", False)
        self.model_complete = raw.get("model_complete", True)
        # Include policy/domain metadata: callers can match certificates to inputs.
        semantic = {k: v for k, v in raw.items() if k != "preprocessing_seconds"}
        self.domain_hash = sha256(json.dumps(semantic, ensure_ascii=False, sort_keys=True,
                                           separators=(",", ":")).encode()).hexdigest()


class _Budget:
    def __init__(self, max_nodes, max_oracle_calls, time_limit_seconds, preprocessing_seconds=0):
        for name, value in (("max_nodes", max_nodes), ("max_oracle_calls", max_oracle_calls)):
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        for name, value in (("time_limit_seconds", time_limit_seconds),
                            ("preprocessing_seconds", preprocessing_seconds)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be nonnegative and finite")
        self.max_nodes, self.max_calls = max_nodes, max_oracle_calls
        self.nodes = self.calls = 0
        self.start = time.monotonic() - preprocessing_seconds
        self.deadline = self.start + time_limit_seconds
        self.exhausted = None

    def check_time(self):
        if time.monotonic() >= self.deadline:
            self.exhausted = "time_limit"
            raise _Unknown(self.exhausted)

    def call(self):
        self.check_time()
        if self.calls >= self.max_calls:
            self.exhausted = "oracle_call_limit"
            raise _Unknown(self.exhausted)
        self.calls += 1

    def node(self):
        self.check_time()
        if self.nodes >= self.max_nodes:
            self.exhausted = "node_limit"
            raise _Unknown(self.exhausted)
        self.nodes += 1

    def metrics(self):
        return {"nodes": self.nodes, "oracle_calls": self.calls,
                "elapsed_seconds": time.monotonic() - self.start,
                "budget_exhausted": self.exhausted}


class _Oracle:
    def __init__(self, problem, budget, allow_scoped_proof):
        if type(allow_scoped_proof) is not bool:
            raise ValueError("allow_scoped_proof must be Boolean")
        self.problem, self.budget = problem, budget
        self.allow_scoped_proof = allow_scoped_proof
        self.cache = {}
        self.trace = []

    def check(self, active):
        active = frozenset(active) | frozenset(self.problem.background)
        if active - self.problem.constraints.keys():
            raise ValueError("unknown active constraint IDs")
        # Time remains a total budget even when a prior certificate is reused.
        self.budget.check_time()
        if active in self.cache:
            return self.cache[active]
        start_nodes = self.budget.nodes
        try:
            self.budget.call()
            if not self.problem.model_complete:
                raise _Unknown("incomplete_model")
            result = self._search(active)
            self.cache[active] = result
            self.trace.append({"active_constraint_ids": sorted(active), "status": result["status"],
                               "nodes": self.budget.nodes - start_nodes})
            return result
        except _Unknown as exc:
            self.trace.append({"active_constraint_ids": sorted(active), "status": "UNKNOWN",
                               "reason": str(exc), "nodes": self.budget.nodes - start_nodes})
            raise

    def _unsat(self):
        if not self.problem.complete and not self.allow_scoped_proof:
            raise _Unknown("incomplete_domains")
        return {"status": "UNSAT", "witness": None}

    def _search(self, active):
        required, occupied = set(self.problem.required), set(self.problem.fixed)
        for cid in sorted(active):
            self.budget.check_time()
            c = self.problem.constraints[cid]
            if c["kind"] == "require_course":
                required.add(c["course_id"])
            elif c["kind"] == "block_resources":
                occupied.update(c.get("resource_keys", []))
        allowed = {}
        for cid in sorted(required):
            items = []
            for candidate in self.problem.domains[cid]:
                self.budget.node()
                if not (candidate.blocked_by & active or candidate.resources & occupied):
                    items.append(candidate)
            if not items:
                return self._unsat()
            allowed[cid] = items
        order = sorted(required, key=lambda cid: (len(allowed[cid]), cid))
        # Iterative complete backtracking avoids Python recursion-depth limits.
        indices, chosen = [0] * len(order), [None] * len(order)
        depth = 0
        while depth >= 0:
            self.budget.check_time()
            if depth == len(order):
                return {"status": "SAT", "witness": {cid: chosen[i].id for i, cid in enumerate(order)}}
            items = allowed[order[depth]]
            if indices[depth] >= len(items):
                indices[depth] = 0
                depth -= 1
                if depth >= 0:
                    occupied.difference_update(chosen[depth].resources)
                    chosen[depth] = None
                continue
            candidate = items[indices[depth]]
            indices[depth] += 1
            self.budget.node()
            if candidate.resources & occupied:
                continue
            chosen[depth] = candidate
            occupied.update(candidate.resources)
            depth += 1
        return self._unsat()


def _setup(problem, max_nodes, max_oracle_calls, time_limit_seconds, allow_scoped_proof):
    preprocessing = problem.get("preprocessing_seconds", 0) if isinstance(problem, dict) else 0
    budget = _Budget(max_nodes, max_oracle_calls, time_limit_seconds, preprocessing)
    parsed = _Problem(problem)
    return parsed, budget, _Oracle(parsed, budget, allow_scoped_proof)


def _scope(problem, allow_scoped_proof):
    return {"description": deepcopy(problem.raw.get("scope", {})), "domain_hash": problem.domain_hash,
            "domains_complete": problem.complete, "model_complete": problem.model_complete,
            "proof_scope": "declared_finite_model" if problem.complete else "supplied_candidates_only",
            "allow_scoped_proof": allow_scoped_proof,
            "global_timetable_infeasibility_proven": False}


def check_finite_consistency(problem, active_constraint_ids=None, *, max_nodes=100000,
                             max_oracle_calls=200, time_limit_seconds=10,
                             allow_scoped_proof=False):
    """Return SAT/UNSAT/UNKNOWN for a fixed finite model; never mutate its input.

    Background IDs are always active. Missing candidates permit SAT witnesses,
    but prevent UNSAT unless an explicitly scoped proof has been requested.
    ``max_nodes`` counts candidate filtering and backtracking examinations.
    """
    p, budget, oracle = _setup(problem, max_nodes, max_oracle_calls, time_limit_seconds, allow_scoped_proof)
    active = p.foreground if active_constraint_ids is None else _strings(active_constraint_ids, "active_constraint_ids")
    try:
        result = dict(oracle.check(active))
    except _Unknown as exc:
        result = {"status": "UNKNOWN", "reason": str(exc), "witness": None}
    return {**result, **budget.metrics(), "scope": _scope(p, allow_scoped_proof)}


def explain_finite_conflict(problem, *, max_nodes=100000, max_oracle_calls=200,
                            time_limit_seconds=10, allow_scoped_proof=False):
    """Compute and independently deletion-check one subset-minimal conflict.

    Non-relaxable constraints may appear in an explanation. Removing a label
    is a diagnostic hypothetical, never authorization to alter a timetable.
    All oracle calls (including final verification) share the same budget.
    """
    p, budget, oracle = _setup(problem, max_nodes, max_oracle_calls, time_limit_seconds, allow_scoped_proof)
    result = {"status": "unknown", "background_status": None, "full_status": None,
              "core_ids": [], "core": [], "known_conflict_ids": [], "minimality_verified": False,
              "verification": [], "scope": _scope(p, allow_scoped_proof)}

    def qx(background, delta, candidates):
        if delta and oracle.check(background)["status"] == "UNSAT":
            return ()
        if len(candidates) == 1:
            return candidates
        split = len(candidates) // 2
        first, second = candidates[:split], candidates[split:]
        d2 = qx(background | set(first), first, second)
        d1 = qx(background | set(d2), d2, first)
        return d1 + d2

    try:
        background = oracle.check(p.background)
        result["background_status"] = background["status"]
        if background["status"] == "UNSAT":
            result.update(status="background_unsat", reason="background_inconsistent")
        else:
            full = oracle.check(p.foreground)
            result["full_status"] = full["status"]
            if full["status"] == "SAT":
                result.update(status="no_conflict", witness=full["witness"])
            else:
                result["known_conflict_ids"] = list(p.foreground)
                core = qx(set(p.background), (), p.foreground)
                core_set = set(core)
                core = [cid for cid in p.foreground if cid in core_set]
                if oracle.check(core)["status"] != "UNSAT":
                    raise RuntimeError("QuickXplain produced an invalid conflict")
                for cid in core:
                    deleted = oracle.check([other for other in core if other != cid])
                    result["verification"].append({"removed_constraint_id": cid,
                                                   "status": deleted["status"],
                                                   "witness": deleted["witness"]})
                    if deleted["status"] != "SAT":
                        raise RuntimeError("QuickXplain produced a non-minimal conflict")
                result.update(status="conflict", core_ids=core,
                              core=[deepcopy(p.constraints[cid]) for cid in core],
                              known_conflict_ids=core, minimality_verified=True)
    except _Unknown as exc:
        if result["background_status"] is None:
            result["background_status"] = "UNKNOWN"
        elif result["full_status"] is None:
            result["full_status"] = "UNKNOWN"
        result.update(status="unknown", reason=str(exc), minimality_verified=False)
    result.update(budget.metrics())
    result["oracle_trace"] = oracle.trace
    result["relaxations_applied"] = []
    return result


def make_occupancy_problem(dataset, target_ids, repair_config=None, *,
                           max_candidates_per_course=None, time_limit_seconds=10):
    """Build a fixed-occupancy insertion model, without occupancy-pruning domains.

    Each existing course blocks resource keys under one explainable label.
    Historical fixed-fixed collisions are not modeled as contradictions: this
    is explicitly an incremental insertion model, not global schedule validation.
    Turning off an occupancy label does NOT establish that course can be moved
    and fully reinserted. No source data, placements or repair policy is changed.
    """
    try:
        from .repair_courses import CandidateFactory, Occupancy, RepairConfig, keys_for, placement_dict
    except ImportError:
        from repair_courses import CandidateFactory, Occupancy, RepairConfig, keys_for, placement_dict
    started = time.monotonic()
    if isinstance(time_limit_seconds, bool) or not isinstance(time_limit_seconds, (float, int)) or not math.isfinite(time_limit_seconds) or time_limit_seconds < 0:
        raise ValueError("time_limit_seconds must be nonnegative and finite")
    deadline = started + time_limit_seconds
    if repair_config is None:
        config = RepairConfig()
    elif isinstance(repair_config, dict):
        config = RepairConfig(**deepcopy(repair_config))
    else:
        config = deepcopy(repair_config)
    if max_candidates_per_course is not None:
        config.max_candidates_per_course = max_candidates_per_course
    config.validate()
    targets = sorted(_strings(target_ids, "target_ids"))
    unknown = (set(targets) | set(config.movable_ids) | set(config.locked_ids)
               | set(config.reviewed_soft_time_ids)) - dataset.courses.keys()
    if unknown:
        raise ValueError(f"unknown course IDs: {sorted(unknown)}")
    existing = {p.course_id for p in dataset.placements}
    if existing & set(targets):
        raise ValueError("targets already present in the baseline cannot use insertion explanations")
    problem = {"domains": {cid: [] for cid in targets}, "required_courses": targets,
               "constraints": [], "background_ids": [], "foreground_ids": [],
               "domains_complete": True, "model_complete": True,
               "scope": {"kind": "fixed_occupancy_joint_insertion", "target_ids": targets,
                         "source_hashes": deepcopy(dataset.source_hashes),
                         "background_rules": ["school_and_course_bans", "capacity", "campus", "room_type",
                                              "specified_rooms", "complete_hours_and_weeks", "configured_grid",
                                              "unrelaxed_preferences", "target_resource_exclusivity"],
                         "days": list(config.days), "periods": list(config.periods),
                         "single_period_starts": config.single_period_starts,
                         "allowed_changes": list(config.allowed_changes),
                         "reviewed_soft_time_ids": list(config.reviewed_soft_time_ids),
                         "max_moved_courses": config.max_moved_courses,
                         "baseline_globally_validated": False,
                         "removing_occupancy_does_not_reinsert_course": True},
               "construction_diagnostics": {}}

    def finish_timeout():
        problem.update(model_complete=False, domains_complete=False,
                       construction_status="time_limit", preprocessing_seconds=time.monotonic() - started)
        return problem

    if time.monotonic() >= deadline:
        return finish_timeout()
    factory = CandidateFactory(dataset, config, deadline)
    relevant_keys = set()
    for cid in targets:
        if time.monotonic() >= deadline:
            return finish_timeout()
        candidates = factory.get(cid)
        diag = factory.diagnostics[cid]
        problem["construction_diagnostics"][cid] = deepcopy(diag)
        if diag.get("truncated"):
            problem["domains_complete"] = False
        if diag.get("reason") not in {"candidates_generated", "no_matching_room", "no_time_pattern"}:
            problem["domains_complete"] = False
            if diag.get("reason") != "search_limit":
                problem["model_complete"] = False
        for index, (_, placements) in enumerate(candidates):
            if time.monotonic() >= deadline:
                return finish_timeout()
            keys = {key for placement in placements for key in keys_for(dataset.courses[cid], placement)}
            relevant_keys.update(keys)
            problem["domains"][cid].append({"id": f"{cid}:p{index:06d}",
                "resource_keys": sorted(json.dumps(key, ensure_ascii=False, separators=(",", ":")) for key in keys),
                "placements": [placement_dict(p) for p in placements]})
    if time.monotonic() >= deadline:
        return finish_timeout()
    occupancy = Occupancy(dataset)
    if time.monotonic() >= deadline:
        return finish_timeout()
    resources_by_course = defaultdict(set)
    for key in sorted(relevant_keys):
        if time.monotonic() >= deadline:
            return finish_timeout()
        encoded = json.dumps(key, ensure_ascii=False, separators=(",", ":"))
        for cid in occupancy.cells.get(key, ()):
            resources_by_course[cid].add(encoded)
    for cid in sorted(resources_by_course):
        if time.monotonic() >= deadline:
            return finish_timeout()
        uncertain = cid in occupancy.uncertain_ids
        constraint = {"id": "occupancy:" + cid, "kind": "block_resources", "course_id": cid,
                      "resource_keys": sorted(resources_by_course[cid]), "explainable": True,
                      "relaxable": cid in config.movable_ids and cid not in config.locked_ids and not uncertain,
                      "data_uncertainty": uncertain,
                      "meaning": "release occupancy only; reinsertion feasibility is not established"}
        problem["constraints"].append(constraint)
        problem["foreground_ids"].append(constraint["id"])
    problem["construction_status"] = "complete" if problem["model_complete"] else "unsupported_data_or_model"
    problem["scope"]["baseline_recorded_conflict_cells"] = occupancy.recorded_conflict_counts
    problem["preprocessing_seconds"] = time.monotonic() - started
    return problem


def explain_conflict(dataset, target_ids, repair_config=None, explain_config=None):
    """Convenience adapter + explanation, sharing one construction/search budget."""
    config = dict(explain_config or {})
    candidate_limit = config.pop("max_candidates_per_course", None)
    allowed = {"max_nodes", "max_oracle_calls", "time_limit_seconds", "allow_scoped_proof"}
    if config.keys() - allowed:
        raise ValueError(f"unknown explain_config keys: {sorted(config.keys() - allowed)}")
    problem = make_occupancy_problem(dataset, target_ids, repair_config,
        max_candidates_per_course=candidate_limit,
        time_limit_seconds=config.get("time_limit_seconds", 10))
    result = explain_finite_conflict(problem, **config)
    result["construction_diagnostics"] = problem["construction_diagnostics"]
    result["construction_status"] = problem["construction_status"]
    return result
