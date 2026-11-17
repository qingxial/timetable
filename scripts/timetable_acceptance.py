"""One independent acceptance contract for every versioned scheduling engine."""
from collections import defaultdict
from copy import deepcopy
from dataclasses import fields
import json

try:
    from .repair_data import Placement, parse_time_windows, room_metadata_audit
    from .repair_courses import Occupancy, RepairConfig, validate_changes, week_load
except ImportError:
    from repair_data import Placement, parse_time_windows, room_metadata_audit
    from repair_courses import Occupancy, RepairConfig, validate_changes, week_load


def groups(rows):
    result = defaultdict(list)
    for row in rows:
        result[row['course_id']].append(row)
    return result


def signature(rows):
    return sorted(json.dumps(row, sort_keys=True) for row in rows)


def block_allowed(course, day, periods, teaching_weeks=None):
    """Explicit source preference takes precedence over the default block cap."""
    if len(periods) <= course.max_block:
        return True
    preferences = [course.prefer]
    from scripts.teacher_time_constraints import flags, value
    for rule in course.teacher_time_rules:
        if teaching_weeks is None or {w-1 for w in teaching_weeks} & flags(value(rule, 'RWJSZCDM')):
            preferences.append(value(rule, 'Prefer_Time') or '')
    return any(all((day, p) in parse_time_windows(pref) for p in periods) for pref in preferences)


def effective_dataset(dataset, corrections=(), rule_pack=None):
    """Replay audited metadata decisions without rewriting frozen source workbooks."""
    result = deepcopy(dataset)
    try:
        from .repair_workflows import apply_data_corrections
    except ImportError:
        from repair_workflows import apply_data_corrections
    seen = []
    for item in corrections:
        if item in seen:
            continue
        seen.append(item)
        cid = item['course_id']
        saved = result.placements
        if item['operation'] == 'quarantine_incomplete_segments':
            incomplete = [p for p in saved if p.course_id == cid and
                          (not p.weeks or not p.periods or p.day not in range(7))]
            if not incomplete:
                continue  # The recorded quarantine was already materialized.
            result, _ = apply_data_corrections(result, [item])
        else:
            # Already placed courses may inherit a decision made before insertion.
            result.placements = [p for p in saved if p.course_id != cid]
            result, _ = apply_data_corrections(result, [item])
            result.placements = saved
    if rule_pack:
        try:
            from .repair_rule_packs import apply_rule_pack
        except ImportError:
            from repair_rule_packs import apply_rule_pack
        result, _ = apply_rule_pack(result, rule_pack)
    return result


def original_engine_checks(dataset, policy):
    """Replay the original contract for acceptance, without changing source facts.

    Coverage limitations remain audit warnings. The original class resolver,
    time enumerator and room filter define this engine's semantics; the modern
    repair model must not add its Tuesday ban or fixed-room-only policy here.
    """
    from types import SimpleNamespace
    from utils1 import get_class_instances1, filter_suitable_classrooms
    from tryloadlimit import build_preferences, new_only_time
    view = deepcopy(dataset)
    warnings = {cid: list(c.issues) + list(c.time_row_differences) for cid, c in view.courses.items() if c.issues or c.time_row_differences}
    originals = {cid: SimpleNamespace(JXBID=cid, KCM=c.name,
        TJBJ=next(iter(c.original_class_values), '')) for cid, c in view.courses.items()}
    registry = [SimpleNamespace(BJMC=name) for name in (view.class_registry or [])]
    for cid, c in view.courses.items():
        c.classes = frozenset(cls.BJMC for cls in get_class_instances1(originals[cid], registry, list(originals.values())))
        # Different teacher-row time conditions are legitimate relationship data.
        # Only these fields become row-scoped; contradictory hours/campus/etc.
        # remain errors and all original issues are retained in the audit.
        time_differences = tuple('metadata: inconsistent ' + key + ' across course rows:'
                                 for key in ('Prefer_Time', 'unavailable_Time', 'available_Time'))
        c.issues = [issue for issue in c.issues if not issue.startswith(('coverage:', *time_differences))]

    def time_check(c, entries):
        if any(p.day not in policy.days or any(k not in policy.periods for k in p.periods) for p in entries):
            return False
        if c.teacher_time_rules:
            from scripts.teacher_time_constraints import validate_rule_rows
            by_week = {}
            for entry in entries:
                for week in entry.weeks:
                    by_week.setdefault(week-1, []).append((entry.day, min(entry.periods)-1, len(entry.periods)))
            time_check.last_errors = validate_rule_rows(c.teacher_time_rules, by_week)
            return not time_check.last_errors
        # Backward compatibility for synthetic/in-memory metadata without rows.
        time_check.last_errors = []
        pattern = sorted({(p.day, min(p.periods)-1, len(p.periods)) for p in entries})
        options = new_only_time([d for d, _, _ in pattern], [n for _, _, n in pattern],
                                build_preferences(c.prefer, c.unavailable))
        return any(sorted(option) == pattern for option in options)

    def room_check(c, room):
        if not room.enabled: return False
        source = SimpleNamespace(KRL=c.capacity, SKXQ=c.campus, JASLXMC=c.room_type,
            JXLDM=';'.join(sorted(c.buildings)), JASDM=';'.join(sorted(c.explicit_rooms)),
            LSJASDM=';'.join(sorted(c.historical_rooms)))
        candidate = SimpleNamespace(JASDM=room.id, SKZWS=room.capacity, MC=room.campus,
                                    JASLX=room.type, JXLDM=room.building)
        return bool(filter_suitable_classrooms([candidate], source) or
                    filter_suitable_classrooms([candidate], source, relax_constraints=True))
    return view, time_check, room_check, warnings


def inherited_virtual_pairs(dataset):
    """Same LLXS inherited by an existing @W/@B pair is one mother minimum."""
    pairs = defaultdict(dict)
    for cid, course in dataset.courses.items():
        mother, separator, suffix = cid.rpartition('@')
        if separator and suffix in {'W', 'B'}:pairs[mother][suffix] = cid
    return {mother: (parts['W'], parts['B']) for mother, parts in pairs.items()
            if set(parts) == {'W', 'B'} and dataset.courses[parts['W']].total_hours is not None
            and dataset.courses[parts['W']].total_hours == dataset.courses[parts['B']].total_hours}


def hour_plan_differences(dataset, rows):
    """Measure actual schedules against weekly plans; LLXS is not a blocker."""
    grouped = groups(rows); result = []
    for cid in sorted(grouped):
        course = dataset.courses.get(cid)
        if course is None: continue
        expected = course.weekly_loads or {w: course.hours for w in course.weeks}
        actual = week_load([Placement(cid, frozenset(r['weeks']), r['day'], tuple(r['periods']), r['room_id']) for r in grouped[cid]])
        if set(expected) == set(actual) and all(abs(actual[w]-n) < 1e-5 for w,n in expected.items()): continue
        result.append({'course_id': cid, 'course_name': course.name,
                       'expected_weekly_load': {str(w): n for w,n in expected.items()},
                       'actual_weekly_load': {str(w): n for w,n in actual.items()},
                       'expected_total': sum(expected.values()), 'actual_total': sum(actual.values()),
                       'shortage_weeks': [w for w,n in expected.items() if actual.get(w,0)+1e-5<n]})
    return result


def validate_transition(dataset, before_rows, rows, previous_remaining, remaining_ids,
                        config=None, *, weeks=53, full=False):
    """Validate full output against an explicit parent, not an engine's success flag.

    Imported incomplete baseline rows may stay unchanged. Every inserted, changed
    or newly resolved course is checked independently. No existing course may be
    lost and no unresolved target may silently disappear.
    """
    supplied = dict(config or {})
    original_contract = supplied.get("time_policy") == "test_for_school"
    names = {f.name for f in fields(RepairConfig)}
    policy = RepairConfig(**{k: v for k, v in supplied.items() if k in names})
    if type(policy.max_moved_courses) is not int or policy.max_moved_courses < 0:
        raise ValueError('invalid movement budget')
    checked_policy = deepcopy(policy)
    # The original engine can move more than the bounded modern repair tool.
    checked_policy.max_moved_courses = min(policy.max_moved_courses, 30)
    checked_policy.validate()
    if type(weeks) is not int or not 1 <= weeks <= 53:
        raise ValueError('invalid semester weeks')
    time_check = room_check = None
    original_warnings = {}
    if original_contract:
        dataset, time_check, room_check, original_warnings = original_engine_checks(dataset, policy)
    # 原调课结果：偏好时间是软约束，排在偏好外只记“偏好偏离”警告；unavailable/available 仍是硬约束。
    preference_soft = supplied.get('preference_as_soft', False)
    if type(preference_soft) is not bool:
        raise ValueError('preference_as_soft must be boolean')
    preference_deviations = {}
    if preference_soft and time_check is not None:
        strict_time_check = time_check
        def time_check(c, entries):
            # 先清空：越出授权时间范围时严格检查会直接返回 False 而不更新 last_errors，
            # 不能沿用上一门课残留的结果。
            strict_time_check.last_errors = []
            ok = strict_time_check(c, entries)
            errors = getattr(strict_time_check, 'last_errors', [])
            hard = [e for e in errors if e.get('reason') != 'outside_preferred']
            soft = [e for e in errors if e.get('reason') == 'outside_preferred']
            if soft:
                preference_deviations[c.id] = soft
            time_check.last_errors = hard
            # 无源行规则时只能整体判定，不区分偏好与禁排：保持严格结果。
            return not hard if errors else ok
    from scripts.class_conflict_policy import enabled, acceptance_policy
    if not enabled():supplied.update(acceptance_policy())
    check_classes = supplied.get('check_class_conflicts', True)
    if type(check_classes) is not bool:
        raise ValueError('check_class_conflicts must be boolean')
    class_evidence = supplied.get('class_conflict_exemption_evidence', '')
    if not check_classes:
        if not isinstance(class_evidence, str) or not class_evidence.strip():
            raise ValueError('class conflict exemption requires explicit evidence')
        dataset = deepcopy(dataset)
        for course in dataset.courses.values():
            course.check_class = False
            course.issues = [issue for issue in course.issues if not issue.startswith(('coverage:', 'metadata: inconsistent TJBJ', 'metadata: inconsistent IF_CLASS_CONFICT', 'metadata: invalid IF_CLASS_CONFICT'))]
    old, new = groups(before_rows), groups(rows)
    changed = {cid for cid in old.keys() | new.keys()
               if signature(old.get(cid, [])) != signature(new.get(cid, []))}
    prior_remaining, remaining = set(previous_remaining), set(remaining_ids)
    if len(remaining) != len(remaining_ids):
        raise ValueError('remaining course IDs contain duplicates')
    extensions = set(policy.target_ids) - set(old)
    if extensions - set(dataset.courses):
        raise ValueError('state rejected: extension targets do not exist in source courses')
    universe = set(old) | prior_remaining | extensions
    if set(old) - set(new):
        raise ValueError('state rejected: existing courses were lost')
    if set(new) - universe or remaining - universe:
        raise ValueError('state rejected: result contains out-of-scope course IDs')
    if remaining - (prior_remaining | extensions):
        raise ValueError('state rejected: assigned courses returned to remaining without authorization')
    resolved = prior_remaining - remaining
    if resolved - set(new):
        raise ValueError('state rejected: unresolved courses disappeared from remaining')
    inserted = set(new) - set(old)
    if inserted & remaining or (extensions - set(new)) - remaining:
        raise ValueError('state rejected: assigned/remaining course reconciliation failed')
    to_check = set(new) if full else changed | resolved
    for cid in to_check:
        c = dataset.courses.get(cid)
        if c is None or any(not (str(issue).startswith('metadata: inconsistent LLXS') or 'invalid LLXS:' in str(issue)) for issue in c.issues):
            raise ValueError('state rejected: unknown course or invalid metadata: ' + cid)
        teacher_weeks = set().union(*c.teachers.values()) if c.teachers else set()
        if not c.weeks <= teacher_weeks:
            raise ValueError('state rejected: teacher responsibility weeks do not cover course: ' + cid)
        for row in new[cid]:
            day, periods, teaching_weeks = row['day'], row['periods'], row['weeks']
            if (type(day) is not int or day not in range(7) or not periods or not teaching_weeks
                    or any(type(p) is not int or p not in range(1, 12) for p in periods)
                    or any(type(w) is not int or not 1 <= w <= weeks for w in teaching_weeks)
                    or len(set(teaching_weeks)) != len(teaching_weeks)
                    or list(periods) != list(range(min(periods), max(periods) + 1))):
                raise ValueError('state rejected: malformed/non-contiguous course segment: ' + cid)
            room = dataset.rooms.get(row['room_id'])
            if not room or not room.campus or not c.campus or room.campus != c.campus:
                raise ValueError('state rejected: unknown campus or cross-campus placement: ' + cid)
            if not block_allowed(c, day, periods, teaching_weeks):
                raise ValueError('state rejected: block exceeds cap outside explicit preference: ' + cid)

    baseline = deepcopy(dataset)
    # The explicit JSON parent is authoritative; the dataset supplies metadata only.
    baseline.placements = [Placement(r['course_id'], frozenset(r['weeks']), r['day'],
                                    tuple(r['periods']), r['room_id']) for r in before_rows]
    before = Occupancy(baseline)
    final = Occupancy(baseline)
    for cid in changed:
        if cid in final.placements:
            final.remove(cid)
        final.put(cid, [Placement(cid, frozenset(r['weeks']), r['day'], tuple(r['periods']), r['room_id'])
                        for r in new.get(cid, [])])
    expected_before = {cid: entries for cid, entries in before.placements.items() if cid not in resolved}
    errors = validate_changes(baseline, expected_before, final, to_check, policy,
                              time_check=time_check, room_check=room_check, minimum_source_hours=True, accepted_hour_differences=supplied.get('accepted_hour_differences', []))
    # LLXS may include practical/other hours. The user confirmed that weekly
    # hours and teaching weeks are authoritative; never reject their product
    # merely because it differs from LLXS, including inherited virtual totals.
    hour_differences = hour_plan_differences(baseline, rows)
    if errors:
        raise ValueError('independent acceptance failed: ' + json.dumps(errors[:10], ensure_ascii=False))
    def cells(items):
        return {(w, r['day'], p, r['room_id']) for r in items for w in r['weeks'] for p in r['periods']}
    moved = {cid for cid in set(old) & changed if cells(old[cid]) != cells(new[cid])}
    if moved - set(policy.movable_ids) or moved & set(policy.locked_ids):
        raise ValueError('state rejected: movement outside authorized IDs')
    if len(moved) > policy.max_moved_courses:
        raise ValueError('state rejected: movement budget exceeded')
    before_conflicts, after_conflicts = before.conflict_counts(), final.conflict_counts()
    if any(value > before_conflicts.get(key, 0) for key, value in after_conflicts.items()):
        raise ValueError('state rejected: new resource conflicts')
    overrides = sorted(cid for cid in to_check if any(len(r['periods']) > baseline.courses[cid].max_block for r in new[cid]))
    return {'valid': True, 'validator': 'timetable_acceptance.v1',
            'scope': 'complete_root' if full else 'changed_and_resolved_courses',
            'changed_course_ids': sorted(changed), 'inserted_ids': sorted(inserted), 'moved_ids': sorted(moved),
            'hours_and_weeks_preserved': True, 'hour_acceptance_policy': 'weekly_plan_with_user_review',
            'source_hour_minima_and_weeks_satisfied': True, 'virtual_total_policy': 'weekly_children; LLXS_not_enforced',
            'hour_plan_differences': hour_differences, 'accepted_hour_differences': supplied.get('accepted_hour_differences', []),
            'LLXS_is_blocking': False, 'new_conflicts': 0,
            'class_conflict_check': {'enabled': check_classes, 'evidence': class_evidence if not check_classes else None},
            'preference_policy': 'soft_warning' if preference_soft else 'hard',
            'course_unavailable_relaxation_authorized': policy.relax_course_unavailable,
            'time_relaxation_levels': dict(policy.time_relaxation_levels),
            'course_block_limits': deepcopy(policy.course_block_limits),
            'preference_deviations': preference_deviations,
            'baseline_conflict_cells': before_conflicts, 'baseline_uncertain_course_ids': sorted(before.uncertain_ids),
            'room_metadata_audit': room_metadata_audit(dataset),
            'source_issues': list(dataset.issues), 'preference_block_overrides': overrides,
            'engine_contract': 'test_for_school' if original_contract else 'modern_repair',
            'source_course_warnings': original_warnings,
            'teacher_time_rules_audit': {cid: list(dataset.courses[cid].teacher_time_rules) for cid in sorted(to_check)
                if len({(str(rule.get('Prefer_Time') or ''), str(rule.get('unavailable_Time') or ''),
                         str(rule.get('available_Time') or '')) for rule in dataset.courses[cid].teacher_time_rules})>1
                   or any(rule.get('available_Time') for rule in dataset.courses[cid].teacher_time_rules)},
            'teacher_time_policy': 'teaching_class_week_teacher_slot' if original_contract else 'modern_repair',
            'checks': ['scope', 'remaining_reconciliation', 'hours', 'weeks', 'campus', 'resources',
                       'room_constraints', 'time_policy', 'movement_budget', 'preference_block_priority']}
