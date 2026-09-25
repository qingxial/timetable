"""Bounded, transactional timetable repair. Never overwrites the input workbooks.

Import ``repair(dataset, target_ids, config)`` as an agent tool, or run this file
with --config policy.json. Results are proposals, not an approved timetable.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
import csv
import json
import math
from pathlib import Path
import re
import time

try:
    from .repair_data import Placement, load_dataset, parse_time_windows
except ImportError:
    from repair_data import Placement, load_dataset, parse_time_windows

ALLOWED_CHANGES = {"time_preference", "building_preference", "historical_room"}


@dataclass
class RepairConfig:
    allowed_changes: list[str] = field(default_factory=list)
    time_scope: str = "same_day"  # strict, same_day, weekdays, configured_days
    target_ids: list[str] = field(default_factory=list)
    locked_ids: list[str] = field(default_factory=list)
    movable_ids: list[str] = field(default_factory=list)
    reviewed_soft_time_ids: list[str] = field(default_factory=list)
    max_moved_courses: int = 0
    max_candidates_per_course: int = 2000
    max_search_nodes: int = 20000
    time_limit_seconds: float = 60.0
    days: list[int] = field(default_factory=lambda: list(range(5)))
    periods: list[int] = field(default_factory=lambda: list(range(1, 9)))
    additional_forbidden: str = ""  # Additional bans; cannot remove the school ban.
    single_period_starts: str = "odd"  # any expands only one-period blocks.
    max_weekly_hours: int = 8
    max_blocks_per_day: int = 1
    filter_fixed_occupancy: bool = False

    def validate(self):
        unknown = set(self.allowed_changes) - ALLOWED_CHANGES
        if unknown:
            raise ValueError(f"Unsupported adjustments: {sorted(unknown)}")
        if self.time_scope not in {"strict", "same_day", "weekdays", "configured_days"}:
            raise ValueError("time_scope must be strict, same_day, weekdays, or configured_days")
        if self.single_period_starts not in ("odd", "any"):
            raise ValueError("single_period_starts must be odd or any")
        for name, upper in (("max_weekly_hours", 77), ("max_blocks_per_day", 11)):
            if type(getattr(self, name)) is not int or not 1 <= getattr(self, name) <= upper:
                raise ValueError(f"{name} must be an integer between 1 and {upper}")
        if type(self.filter_fixed_occupancy) is not bool:
            raise ValueError("filter_fixed_occupancy must be a boolean")
        if self.filter_fixed_occupancy and self.max_moved_courses != 0:
            raise ValueError("filter_fixed_occupancy requires max_moved_courses=0")
        for name in ("target_ids", "locked_ids", "movable_ids", "allowed_changes", "reviewed_soft_time_ids"):
            value = getattr(self, name)
            if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
                raise ValueError(f"{name} must be a list of strings")
        if not isinstance(self.max_moved_courses, int) or not 0 <= self.max_moved_courses <= 3:
            raise ValueError("max_moved_courses must be between 0 and 3")
        for name in ("max_candidates_per_course", "max_search_nodes"):
            if not isinstance(getattr(self, name), int) or getattr(self, name) < 1:
                raise ValueError(f"{name} must be a positive integer")
        if not math.isfinite(self.time_limit_seconds) or self.time_limit_seconds <= 0:
            raise ValueError("time_limit_seconds must be positive and finite")
        if not self.days or any(type(d) is not int or d not in range(7) for d in self.days):
            raise ValueError("days must contain weekday indexes 0..6")
        if not self.periods or any(type(p) is not int or p not in range(1, 12) for p in self.periods):
            raise ValueError("periods must contain periods 1..11")
        parse_time_windows(self.additional_forbidden)


def placement_dict(p):
    return {"course_id": p.course_id, "weeks": sorted(p.weeks), "day": p.day,
            "periods": list(p.periods), "room_id": p.room_id}


def keys_for(course, placement):
    """Resource keys use complete identifiers and each teacher's own weeks."""
    for week in placement.weeks:
        for period in placement.periods:
            if course.check_room and placement.room_id:
                yield ("room", placement.room_id, week, placement.day, period)
            for teacher, weeks in course.teachers.items():
                if week in weeks:
                    yield ("teacher", teacher, week, placement.day, period)
            if course.check_class:
                for cls in course.classes:
                    yield ("class", cls, week, placement.day, period)


class Occupancy:
    def __init__(self, dataset):
        self.courses = dataset.courses
        self.cells = defaultdict(set)
        self.placements = defaultdict(list)
        self.uncertain_ids = set()
        for p in dataset.placements:
            if p.course_id not in self.courses:
                raise ValueError(f"Missing metadata for scheduled course {p.course_id}")
            self.placements[p.course_id].append(p)
            if not p.weeks or not p.periods or p.day not in range(7):
                self.uncertain_ids.add(p.course_id)
        for cid, entries in self.placements.items():
            self._add_cells(cid, entries)
        self.recorded_conflict_counts = self.conflict_counts()
        # Unparseable baseline segments are not empty/free slots. Reserve all
        # known resources conservatively, without fabricating timetable rows.
        for cid in self.uncertain_ids:
            c = self.courses[cid]
            weeks = c.weeks or frozenset(range(1, 54))
            room_ids = {p.room_id for p in self.placements[cid] if p.room_id}
            for w in weeks:
                for d in range(7):
                    for p in range(1, 12):
                        for t in c.teachers:
                            self.cells[("teacher", t, w, d, p)].add(cid)
                        if c.check_class:
                            for cls in c.classes:
                                self.cells[("class", cls, w, d, p)].add(cid)
                        if c.check_room:
                            for room in room_ids:
                                self.cells[("room", room, w, d, p)].add(cid)

    def _add_cells(self, cid, entries):
        for p in entries:
            for key in keys_for(self.courses[cid], p):
                self.cells[key].add(cid)

    def remove(self, cid):
        if cid in self.uncertain_ids:
            raise ValueError(f"Cannot move a course with incomplete baseline data: {cid}")
        entries = self.placements.pop(cid, [])
        for p in entries:
            for key in keys_for(self.courses[cid], p):
                self.cells[key].discard(cid)
                if not self.cells[key]:
                    del self.cells[key]
        return entries

    def put(self, cid, entries):
        if cid in self.placements:
            raise ValueError(f"Placement already exists: {cid}")
        self.placements[cid] = list(entries)
        self._add_cells(cid, entries)

    def blockers(self, cid, entries):
        found = set()
        for p in entries:
            for key in keys_for(self.courses[cid], p):
                found.update(self.cells.get(key, set()) - {cid})
        return found

    def conflict_counts(self):
        return dict(Counter(k[0] for k, occupants in self.cells.items() if len(occupants) > 1))


def room_rejections(course, room, changes):
    reasons = []
    if not room.enabled:
        reasons.append("room_disabled")
    if room.campus != course.campus:
        reasons.append("campus")
    if not math.isfinite(room.capacity) or room.capacity < course.capacity:
        reasons.append("capacity")
    if course.room_type and room.type != course.room_type:
        reasons.append("room_type")
    if course.explicit_rooms and room.id not in course.explicit_rooms:
        reasons.append("explicit_room")
    if not course.explicit_rooms and course.historical_rooms and "historical_room" not in changes:
        if room.id not in course.historical_rooms:
            reasons.append("historical_room")
    if course.buildings and room.building not in course.buildings and "building_preference" not in changes:
        reasons.append("building")
    return reasons


def effective_changes(course, config):
    # Free text requirements may encode a hard preference. Preserve them until
    # their semantics have been separately reviewed in the source data.
    if not course.special.strip():
        return set(config.allowed_changes)
    if course.id in config.reviewed_soft_time_ids:
        return set(config.allowed_changes) & {"time_preference"}
    return set()


def structured_college_time(course):
    raw = course.special.strip().replace("：", ":").replace("；", ";")
    match = re.fullmatch(r"学院:[^;]+;(?:时间:)?\s*((?:周[一二三四五六日]\d+[.\-]\d+\s*)+)", raw)
    if not match:
        return False
    clauses = re.findall(r"(周[一二三四五六日])(\d+)[.\-](\d+)", match.group(1))
    try:
        cells = parse_time_windows(";".join(f"{d}({a}-{b}节)" for d, a, b in clauses))
        return cells == parse_time_windows(course.prefer)
    except ValueError:
        return False


def weekend_only_requirement(course):
    """An exact recognized hard rule; unknown free text remains unsupported."""
    return course.special.strip() == "仅周六周日排课"


def supported_special_requirement(course):
    return structured_college_time(course) or weekend_only_requirement(course)


def time_options(course, config):
    pref = parse_time_windows(course.prefer)
    forbidden = (parse_time_windows(course.unavailable) | parse_time_windows("周二(5-8节)")
                 | parse_time_windows(config.additional_forbidden))
    domain = {(d, p) for d in config.days for p in config.periods} - forbidden
    if config.time_scope == "weekdays":
        domain = {cell for cell in domain if cell[0] < 5}
    changes = effective_changes(course, config)
    if pref:
        if "time_preference" not in changes or config.time_scope == "strict":
            domain &= pref
        elif config.time_scope == "same_day":
            domain = {cell for cell in domain if cell[0] in {d for d, _ in pref}}
    if weekend_only_requirement(course):
        domain = {cell for cell in domain if cell[0] in (5, 6)}
        # Keep both source fields when they disagree; a generic preference
        # relaxation cannot resolve contradictory hard special requirements.
        if pref:
            domain &= pref
    return domain, pref


def blocks_for(course, max_weekly_hours=8):
    rounded = round(course.hours)
    if not math.isfinite(course.hours) or abs(course.hours - rounded) > 1e-5 or not 1 <= rounded <= max_weekly_hours:
        raise ValueError(f"unsupported_hours: requires an integer weekly load between 1 and {max_weekly_hours}")
    if course.block_template:
        blocks = tuple(course.block_template)
    else:
        blocks = (2,) * (rounded // 2) + ((1,) if rounded % 2 else ())
    if sum(blocks) != rounded or any(b < 1 or b > course.max_block for b in blocks):
        raise ValueError("invalid_block_template")
    return blocks


def required_week_load(course):
    """Expected teaching load; explicit corrected plans preserve every source week."""
    if not course.weekly_loads:
        return {w: round(course.hours) for w in course.weeks}
    loads = course.weekly_loads
    if set(loads) != set(course.weeks) or any(type(w) is not int or type(n) is not int
                                              or not 1 <= n <= 77 for w, n in loads.items()):
        raise ValueError("weekly_loads must cover every teaching week with 1..77 integer hours")
    if course.hours != max(loads.values()):
        raise ValueError("hours must equal the maximum explicit weekly load")
    if course.block_template and len(set(loads.values())) > 1:
        raise ValueError("Variable weekly loads cannot silently shorten an explicit block template")
    return dict(loads)


def pattern_placements(course, combo, room_id):
    if not course.weekly_loads:
        return tuple(Placement(course.id, course.weeks, day, periods, room_id) for day, periods in combo)
    loads = required_week_load(course)
    entries, consumed = [], 0
    for day, periods in combo:
        groups = defaultdict(set)
        for week, load in loads.items():
            length = max(0, min(len(periods), load - consumed))
            if length:
                groups[tuple(periods[:length])].add(week)
        entries.extend(Placement(course.id, frozenset(weeks), day, ps, room_id)
                       for ps, weeks in sorted(groups.items()))
        consumed += len(periods)
    return tuple(entries)


def preference_cost(day, periods, pref):
    if not pref or all((day, p) in pref for p in periods):
        return 0
    # Weekday changes dominate shifts within the same day.
    same = [p for d, p in pref if d == day]
    return (0 if same else 100) + min(abs(periods[0] - p) for p in (same or [p for _, p in pref])) + 1


class CandidateFactory:
    def __init__(self, dataset, config, deadline=None, occupancy=None):
        self.dataset, self.config = dataset, config
        self.cache, self.diagnostics = {}, {}
        self.deadline = deadline if deadline is not None else float("inf")
        # Occupancy is an explicit caller input: explanation tools that do not
        # supply it retain the unpruned constraint domain.
        self.occupancy = occupancy if config.filter_fixed_occupancy else None
        if self.occupancy is not None and config.max_moved_courses != 0:
            raise ValueError("Fixed-occupancy candidate filtering cannot be used with movements")

    def get(self, cid):
        if cid in self.cache:
            return self.cache[cid]
        c = self.dataset.courses[cid]
        diag = {"course_id": cid, "course_name": c.name, "issues": list(c.issues),
                "special_requirements_preserved": bool(c.special.strip() and c.id not in self.config.reviewed_soft_time_ids),
                "truncated": False,
                "fixed_occupancy_filter_requested": self.config.filter_fixed_occupancy,
                "fixed_occupancy_filter_applied": self.occupancy is not None}
        self.diagnostics[cid] = diag
        self.cache[cid] = []
        if c.issues:
            diag["reason"] = "data_issue"
            return []
        if c.special.strip() and not supported_special_requirement(c):
            diag.update(reason="unreviewed_special_requirement", detail="Special text is not a supported structured time requirement")
            return []
        if any(marker in c.prefer for marker in ("[", "【", "［")):
            diag.update(reason="unsupported", detail="Grouped preference alternatives require a coupled pattern model")
            return []
        try:
            blocks = blocks_for(c, self.config.max_weekly_hours)
            expected_load = required_week_load(c)
            domain, pref = time_options(c, self.config)
        except ValueError as exc:
            diag.update(reason="unsupported", detail=str(exc))
            return []
        if not c.weeks or not c.teachers:
            diag.update(reason="data_issue", detail="missing teaching weeks or teacher")
            return []
        total = getattr(c, "total_hours", None)
        if total is not None and abs(sum(expected_load.values()) - total) > 1e-5:
            diag.update(reason="source_hours_inconsistent", total_hours=total,
                        weekly_hours_times_weeks=sum(expected_load.values()))
            return []
        covered = set().union(*c.teachers.values())
        if not c.weeks <= covered:
            diag.update(reason="data_issue", detail="teacher responsibility does not cover all teaching weeks")
            return []
        changes = effective_changes(c, self.config)
        rooms = [r for r in self.dataset.rooms.values() if not room_rejections(c, r, changes)]
        diag["matching_rooms"] = len(rooms)
        if not rooms:
            # Counts are independent tests, not mutually exclusive categories.
            diag["room_rejection_counts"] = dict(Counter(
                reason for r in self.dataset.rooms.values() for reason in room_rejections(c, r, changes)))
            diag["reason"] = "no_matching_room"
            return []
        # Preserve explicit block lengths and the configured daily block cap. Grid
        # alignment is an explicit engine convention, not physical impossibility.
        slots = {}
        for length in set(blocks):
            step = 1 if length == 1 and self.config.single_period_starts == "any" else 2
            slots[length] = sorted([
                (d, tuple(range(start, start + length)))
                for d in sorted(set(self.config.days)) for start in range(1, 12, step)
                if all((d, p) in domain for p in range(start, start + length))
            ], key=lambda s: (preference_cost(*s, pref), s))
        if any(not slots[b] for b in blocks):
            diag["reason"] = "no_time_pattern"
            diag["preference_grid_mismatch"] = bool(pref and not any(
                all((d, p) in pref for p in range(s, s + 2))
                for d in range(7) for s in range(1, 11, 2)))
            if self.config.single_period_starts == "any":
                # A valid even single period blocked by a ban is not a grid
                # mismatch. Leave the legacy diagnostic unchanged in odd mode.
                diag["preference_grid_mismatch"] = bool(pref and any(
                    not any(all((d, p) in pref for p in range(start, start + length))
                            for d in range(7)
                            for start in range(1, 12, 1 if length == 1 else 2))
                    for length in set(blocks)))
            return []
        # Equivalent blocks must have the same actual weekly role before their
        # order can be canonicalized. A shortened final block is not symmetric
        # with a full block just because their maximum lengths are equal.
        roles, consumed = [], 0
        for length in blocks:
            roles.append((length, tuple((w, max(0, min(length, load - consumed)))
                                        for w, load in sorted(expected_load.items()))))
            consumed += length
        rooms.sort(key=lambda r: (r.id not in (c.explicit_rooms | c.historical_rooms),
                                  r.building not in c.buildings if c.buildings else False,
                                  r.capacity - c.capacity, r.id))
        room_ids = tuple(r.id for r in rooms)
        patterns, combo, occupied, day_counts = [], [], set(), Counter()
        diag.update(generation_nodes=0, fixed_teacher_class_pruned_slots=0,
                    fixed_room_pruned_slots=0, fixed_room_intersection_pruned_prefixes=0,
                    candidate_cap_scope="retained_after_fixed_occupancy_filter" if self.occupancy is not None
                    else "generated_patterns_and_room_assignments")
        timed_out = False
        slot_cache = {}

        def expired():
            nonlocal timed_out
            if time.monotonic() >= self.deadline:
                timed_out = True
                diag.update(reason="search_limit", truncated=True)
                return True
            return False

        def usable_slots(role):
            if role in slot_cache:
                return slot_cache[role]
            options = []
            for day, periods in slots[role[0]]:
                if expired():
                    break
                available_rooms = None
                if self.occupancy is not None:
                    groups = defaultdict(set)
                    for week, length in role[1]:
                        if length:
                            groups[periods[:length]].add(week)
                    entries = tuple(Placement(cid, frozenset(weeks), day, ps, "")
                                    for ps, weeks in groups.items())
                    # An empty room ID yields only actual teacher/class keys.
                    if any(self.occupancy.cells.get(key, set()) - {cid}
                           for p in entries for key in keys_for(c, p)):
                        diag["fixed_teacher_class_pruned_slots"] += 1
                        continue
                    if c.check_room:
                        available_rooms = frozenset(rid for rid in room_ids if not any(
                            self.occupancy.cells.get(("room", rid, week, day, period), set()) - {cid}
                            for p in entries for week in p.weeks for period in p.periods))
                        if not available_rooms:
                            diag["fixed_room_pruned_slots"] += 1
                            continue
                options.append(((day, periods), available_rooms))
            slot_cache[role] = options
            return options

        # Incremental construction rejects bad prefixes instead of expanding a
        # Cartesian product. All blocks coexist in a maximum-load week, so day
        # counts and overlap checks on the full pattern are conservative/exact
        # for the supported prefix-truncated weekly plans.
        def extend(index, cost, compatible_rooms):
            if expired():
                return
            diag["generation_nodes"] += 1
            if index == len(blocks):
                feasible_rooms = room_ids if compatible_rooms is None else tuple(
                    rid for rid in room_ids if rid in compatible_rooms)
                patterns.append((cost, tuple(combo), feasible_rooms))
                if len(patterns) >= self.config.max_candidates_per_course:
                    diag["truncated"] = True
                return
            if sum(max(0, self.config.max_blocks_per_day - day_counts[d])
                   for d in set(self.config.days)) < len(blocks) - index:
                return
            role = roles[index]
            for slot, available_rooms in usable_slots(role):
                if expired() or len(patterns) >= self.config.max_candidates_per_course:
                    return
                day, periods = slot
                if day_counts[day] >= self.config.max_blocks_per_day:
                    continue
                if any((day, period) in occupied for period in periods):
                    continue
                if any(roles[prior] == role and combo[prior] >= slot for prior in range(index)):
                    continue
                remaining_rooms = compatible_rooms
                if available_rooms is not None:
                    remaining_rooms = available_rooms if compatible_rooms is None else compatible_rooms & available_rooms
                    if not remaining_rooms:
                        diag["fixed_room_intersection_pruned_prefixes"] += 1
                        continue
                combo.append(slot)
                day_counts[day] += 1
                occupied.update((day, period) for period in periods)
                extend(index + 1, cost + preference_cost(day, periods, pref), remaining_rooms)
                occupied.difference_update((day, period) for period in periods)
                day_counts[day] -= 1
                combo.pop()
                if timed_out:
                    return

        supported_cells = {(day, period) for options in slots.values()
                           for day, periods in options for period in periods}
        if sum(blocks) <= len(supported_cells):
            extend(0, 0, None)
        else:
            diag["insufficient_supported_grid_cells"] = len(supported_cells)
        if timed_out:
            return []
        patterns.sort()
        result = []
        # Round robin over each pattern's feasible rooms preserves time-pattern
        # diversity. No blocked pattern or blocked room consumes the output cap.
        for room_index in range(max((len(available) for _, _, available in patterns), default=0)):
            for cost, pattern, available in patterns:
                if expired():
                    return []
                if room_index >= len(available):
                    continue
                entries = pattern_placements(c, pattern, available[room_index])
                result.append((cost, entries))
                if len(result) >= self.config.max_candidates_per_course:
                    diag["truncated"] = True
                    break
            if len(result) >= self.config.max_candidates_per_course:
                break
        result.sort(key=lambda item: item[0])
        diag.update(reason="candidates_generated" if result else "no_time_pattern", candidates=len(result))
        self.cache[cid] = result
        return result


def week_load(entries):
    cells = defaultdict(set)
    for p in entries:
        for w in p.weeks:
            cells[w].update((p.day, period) for period in p.periods)
    return {w: len(value) for w, value in cells.items()}


def validate_changes(dataset, before, state, changed_ids, config):
    """Independent acceptance checks run over the final batch, including moved courses."""
    errors = []
    for cid in changed_ids:
        c, entries = dataset.courses[cid], state.placements[cid]
        domain, _ = time_options(c, config)
        expected = week_load(before[cid]) if cid in before else required_week_load(c)
        if week_load(entries) != expected:
            errors.append({"course_id": cid, "reason": "hours_or_weeks_changed"})
        occupied = Counter((w, p.day, period) for p in entries for w in p.weeks for period in p.periods)
        daily_blocks = Counter((w, p.day) for p in entries for w in p.weeks)
        if any(count > 1 for count in occupied.values()):
            errors.append({"course_id": cid, "reason": "overlapping_course_blocks"})
        if any(count > config.max_blocks_per_day for count in daily_blocks.values()):
            errors.append({"course_id": cid, "reason": "daily_block_limit"})
        if any(load > config.max_weekly_hours for load in week_load(entries).values()):
            errors.append({"course_id": cid, "reason": "weekly_hours_limit"})
        for p in entries:
            if not set(p.weeks) <= c.weeks or any((p.day, k) not in domain for k in p.periods):
                errors.append({"course_id": cid, "reason": "forbidden_time_or_week"})
            room = dataset.rooms.get(p.room_id)
            if room is None or room_rejections(c, room, effective_changes(c, config)):
                errors.append({"course_id": cid, "reason": "room_constraint"})
        blockers = state.blockers(cid, entries)
        if blockers:
            errors.append({"course_id": cid, "reason": "resource_conflict", "with": sorted(blockers)})
    return errors


def repair(dataset, target_ids, config=None):
    """Return a JSON-compatible proposal. Caller data and input files are unchanged."""
    if config is None:
        config = RepairConfig()
    elif isinstance(config, dict):
        config = RepairConfig(**config)
    config.validate()
    requested = list(dict.fromkeys(config.target_ids or target_ids))
    unknown = (set(requested) | set(config.locked_ids) | set(config.movable_ids)
               | set(config.reviewed_soft_time_ids)) - dataset.courses.keys()
    if unknown:
        raise ValueError(f"Unknown course IDs: {sorted(unknown)}")
    state = Occupancy(dataset)
    before = {cid: list(entries) for cid, entries in state.placements.items()}
    baseline_conflicts = state.recorded_conflict_counts
    results, changed, moved = {}, set(), set()
    start = time.monotonic()
    factory = CandidateFactory(dataset, config, start + config.time_limit_seconds, occupancy=state)
    nodes = 0
    stopped = False

    def exhausted():
        return nodes >= config.max_search_nodes or time.monotonic() - start >= config.time_limit_seconds

    def movable(cid):
        c = dataset.courses[cid]
        try:
            expected = required_week_load(c)
        except ValueError:
            return False
        return (cid in before and cid in config.movable_ids and cid not in config.locked_ids
                and cid not in state.uncertain_ids
                and (not c.special.strip() or cid in config.reviewed_soft_time_ids)
                and not c.issues and cid not in moved
                and week_load(before[cid]) == expected)

    def try_insert(cid):
        nonlocal nodes, stopped
        if config.filter_fixed_occupancy:
            # The sorting pass used the initial occupancy. Earlier successful
            # insertions are now fixed too; regenerate against this monotonically
            # growing state so they do not consume the retained-candidate cap.
            # The factory keeps its original deadline for the entire batch.
            factory.cache.pop(cid, None)
        candidates = factory.get(cid)
        # Try zero-disruption insertions before considering any neighborhood move.
        ranked = []
        blocking_counts = Counter()
        blocking_ids = set()
        for cost, entries in candidates:
            if exhausted():
                stopped = True
                return None
            nodes += 1
            blockers = state.blockers(cid, entries)
            if not blockers:
                state.put(cid, entries)
                return {"cost": cost, "moved_ids": []}
            blocking_ids.update(blockers)
            blocking_counts.update({key[0] for p in entries for key in keys_for(dataset.courses[cid], p)
                                    if state.cells.get(key, set()) - {cid}})
            if (0 < len(blockers) <= config.max_moved_courses - len(moved)
                    and all(movable(b) for b in blockers)):
                ranked.append((len(blockers), cost, sorted(blockers), entries))
        factory.diagnostics[cid]["blocked_candidate_counts"] = dict(blocking_counts)
        factory.diagnostics[cid]["blocking_course_ids"] = sorted(blocking_ids)
        ranked.sort(key=lambda item: (item[0], item[1], item[2]))
        for _, cost, blockers, entries in ranked:
            if exhausted():
                stopped = True
                return None
            saved = {b: state.remove(b) for b in blockers}
            placed_blockers = []
            success = False
            state.put(cid, entries)
            try:
                for b in blockers:
                    replacement = None
                    for _, option in factory.get(b):
                        if exhausted():
                            stopped = True
                            break
                        nodes += 1
                        if not state.blockers(b, option):
                            replacement = option
                            break
                    if replacement is None:
                        break
                    state.put(b, replacement)
                    placed_blockers.append(b)
                success = len(placed_blockers) == len(blockers)
                if success:
                    moved.update(blockers)
                    return {"cost": cost, "moved_ids": blockers}
            finally:
                if not success:
                    state.remove(cid)
                    for b in placed_blockers:
                        state.remove(b)
                    for b, old in saved.items():
                        state.put(b, old)
        return None

    # Most constrained courses first, with stable tie-breaking.
    todo = []
    for cid in requested:
        if cid in state.placements:
            results[cid] = {"status": "already_scheduled"}
        elif cid in config.locked_ids:
            results[cid] = {"status": "locked"}
        else:
            options = factory.get(cid)
            todo.append((len(options), cid))
    for _, cid in sorted(todo):
        answer = try_insert(cid)
        if answer:
            changed.add(cid)
            changed.update(answer["moved_ids"])
            results[cid] = {"status": "placed", **answer}
        else:
            diag = factory.diagnostics[cid]
            reason = diag.get("reason")
            status = reason if reason != "candidates_generated" else "not_found_within_limits"
            if stopped:
                status = "search_limit"
            results[cid] = {"status": status}

    validation_errors = validate_changes(dataset, before, state, changed, config)
    if validation_errors:
        raise RuntimeError(f"Repair rejected; no output schedule accepted: {validation_errors[:5]}")
    # The policy never creates a hard-constraint relaxation switch.
    changes = []
    for cid in sorted(changed):
        c = dataset.courses[cid]
        new_entries = state.placements[cid]
        pref = parse_time_windows(c.prefer)
        relaxed = set()
        if pref and any((p.day, period) not in pref for p in new_entries for period in p.periods):
            relaxed.add("time_preference")
            if c.special.strip():
                relaxed.add("reviewed_special_time_requirement")
        for p in new_entries:
            r = dataset.rooms[p.room_id]
            if c.buildings and r.building not in c.buildings:
                relaxed.add("building_preference")
            if not c.explicit_rooms and c.historical_rooms and r.id not in c.historical_rooms:
                relaxed.add("historical_room")
        changes.append({"course_id": cid, "course_name": c.name,
                        "operation": "move" if cid in before else "insert",
                        "relaxed_fields": sorted(relaxed),
                        "original_time_preference": c.prefer,
                        "original_special_requirement": c.special,
                        "planned_weekly_loads": {str(w): h for w, h in required_week_load(c).items()},
                        "before": [placement_dict(p) for p in before.get(cid, [])],
                        "after": [placement_dict(p) for p in new_entries]})
    return {
        "schema_version": 1, "mode": "proposal", "config": asdict(config),
        "source_hashes": dataset.source_hashes,
        "summary": {"requested": len(requested), "inserted": sum(r["status"] == "placed" for r in results.values()),
                    "moved": len(moved), "baseline_scheduled": len(before),
                    "proposed_scheduled": len(state.placements), "search_nodes": nodes,
                    "elapsed_seconds": round(time.monotonic() - start, 3)},
        "validation": {"changed_courses_checked": len(changed), "new_conflicts": 0,
                       "hours_and_weeks_preserved": True,
                       "baseline_conflict_cells": baseline_conflicts,
                       "baseline_uncertain_course_ids": sorted(state.uncertain_ids),
                       "baseline_uncertainty_policy": "freeze_known_resources_all_periods_for_teaching_weeks",
                       "scope": "Known teacher/class/room resources and source conflict flags; not student enrollment validation."},
        "data_issues": dataset.issues,
        "results": [{"course_id": cid, **results[cid], "diagnosis": factory.diagnostics.get(cid, {})} for cid in requested],
        "changes": changes,
        "placements": [placement_dict(p) for cid in sorted(state.placements) for p in state.placements[cid]],
    }


def main(argv=None):
    root = Path(__file__).resolve().parents[1]
    base = root / "智能排课基础数据/提取的基础数据表_converted"
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--course-file", type=Path, default=base / "课程表_split_merged.xlsx")
    ap.add_argument("--room-file", type=Path, default=base / "教室表.xlsx")
    ap.add_argument("--schedule-file", type=Path, default=root / "排课结果/调课后的整体结果_特殊放宽.xlsx")
    ap.add_argument("--failed-file", type=Path, default=root / "排课结果/调课失败的排课失败课程_特殊放宽后.xlsx")
    ap.add_argument("--config", type=Path, help="JSON RepairConfig parameters; omitted means strict constraints")
    ap.add_argument("--output-dir", required=True, type=Path, help="A new directory for proposal and audit files")
    args = ap.parse_args(argv)
    config = RepairConfig(**json.loads(args.config.read_text())) if args.config else RepairConfig()
    config.validate()
    if args.output_dir.exists():
        ap.error("output-dir already exists; choose a new directory to preserve prior runs")
    import pandas as pd
    import hashlib
    failed = pd.read_excel(args.failed_file, dtype=str).fillna("")
    column = "jxbid" if "jxbid" in failed.columns else "教学班ID"
    targets = failed[column].tolist()
    data = load_dataset(args.course_file, args.room_file, args.schedule_file)
    data.source_hashes[str(args.failed_file)] = hashlib.sha256(args.failed_file.read_bytes()).hexdigest()
    result = repair(data, targets, config)
    args.output_dir.mkdir(parents=True, exist_ok=False)
    (args.output_dir / "proposal.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    with (args.output_dir / "diagnosis.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["course_id", "status", "diagnosis"])
        writer.writeheader()
        for row in result["results"]:
            writer.writerow({"course_id": row["course_id"], "status": row["status"],
                             "diagnosis": json.dumps(row["diagnosis"], ensure_ascii=False)})
    print(json.dumps(result["summary"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
