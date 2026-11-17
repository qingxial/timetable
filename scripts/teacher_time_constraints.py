"""Time rules scoped to (teaching class, week, teacher, slot).

Legacy engine weeks/start periods are zero-based. Source rules are never copied
between teaching classes. Week-specific plans retain the legacy list interface
but consumers must use iter_segments rather than multiplying by all weeks.
"""
from collections import defaultdict
from itertools import product
from math import prod
from types import SimpleNamespace
import sys


def text(value):
    return '' if value is None else str(value).strip()


def flags(value):
    return frozenset(i for i, bit in enumerate(text(value)) if bit == '1')


def value(row, key):
    return row.get(key) if isinstance(row, dict) else getattr(row, key, None)


def signature(row):
    return tuple(text(value(row, k)) for k in ('Prefer_Time', 'unavailable_Time', 'available_Time'))


def needs_scoped_time(rows):
    return len({signature(r) for r in rows}) > 1 or any(text(value(r, 'available_Time')) for r in rows)


def build_course_preferences(rows):
    from tryloadlimit import build_preferences
    if not rows:
        return build_preferences('', '')
    if not needs_scoped_time(rows):
        return build_preferences(value(rows[0], 'Prefer_Time'), value(rows[0], 'unavailable_Time'))
    if len({text(value(row, 'JXBID')) for row in rows}) != 1:
        raise ValueError('teacher time rules must belong to exactly one teaching class')
    return {'preferred_groups': [], 'preferred_options': [], 'unavailable': [],
            '_teacher_rows': tuple(rows), '_flag_reschedule': False}


def teacher_week_map(rows, teachers):
    """Join by ID, not filtered-list zip; union repeated responsibility rows."""
    lookup = {text(t.JSH): t for t in teachers}
    active = defaultdict(set)
    for row in rows:
        tid = text(value(row, 'JSH'))
        if tid in lookup:
            active[tid].update(flags(value(row, 'RWJSZCDM')) & flags(value(row, 'SKZCDM')))
    return {tid: (lookup[tid], sorted(weeks)) for tid, weeks in active.items() if weeks}


class WeeklyArrangement(list):
    def __init__(self, by_week):
        self.by_week = {int(w): tuple(tuple(int(x) for x in segment) for segment in plan)
                        for w, plan in by_week.items()}
        super().__init__(sorted({segment for plan in self.by_week.values() for segment in plan}))


def iter_segments(arrangement, teaching_weeks):
    if isinstance(arrangement, WeeklyArrangement):
        grouped = defaultdict(list)
        for week, plan in sorted(arrangement.by_week.items()):
            if week in teaching_weeks:
                grouped[plan].append(week)
        for plan, weeks in grouped.items():
            for day, start, hours in plan:
                yield weeks, day, start, hours
    else:
        for day, start, hours in arrangement:
            yield teaching_weeks, day, start, hours


def placements_from_result(cid, room, arrangement, weeks):
    return [{'course_id': str(cid), 'weeks': sorted(int(w)+1 for w in active), 'day': int(day),
             'periods': list(range(int(start)+1, int(start)+int(hours)+1)), 'room_id': str(room.JASDM)}
            for active, day, start, hours in iter_segments(arrangement, weeks)]


def source_rules(records):
    return tuple({'source_row': number, **{key: value(row, key) for key in
        ('JXBID', 'JSH', 'RWJSZCDM', 'SKZCDM', 'Prefer_Time', 'unavailable_Time', 'available_Time', 'ZXS', 'max_block', 'block_template')}}
        for number, row in records)


def rule_errors(row, plan, week):
    """Evaluate only this source relation, in this responsibility week."""
    from scripts.repair_data import parse_time_windows
    from tryloadlimit import build_preferences, new_only_time
    if week not in flags(value(row, 'RWJSZCDM')) & flags(value(row, 'SKZCDM')):
        return []
    cells = {(day, p+1) for day, start, hours in plan for p in range(start, start+hours)}
    unavailable = text(value(row, 'unavailable_Time'))
    available = text(value(row, 'available_Time'))
    prefer = text(value(row, 'Prefer_Time'))
    errors = []
    banned = cells & parse_time_windows(unavailable)
    if banned: errors.append(('unavailable', sorted(banned)))
    if available:
        outside = cells - parse_time_windows(available)
        if outside: errors.append(('outside_available', sorted(outside)))
    if prefer:
        constraints = build_preferences(prefer, '')
        # Preserve legacy grouped/preferred-window semantics, including block
        # promotion, rather than flattening alternative groups into a union.
        import utils1
        course = row if not isinstance(row, dict) else SimpleNamespace(**row)
        if isinstance(row, dict) and value(row, 'ZXS') is not None:
            course.ZXS = float(value(row, 'ZXS'))
        if hasattr(course, 'ZXS'):
            utils1.course_day_patterns(course, 7, constraints)
        choices = new_only_time([d for d, _, _ in plan], [n for _, _, n in plan], constraints)
        if not any(sorted(candidate) == sorted(plan) for candidate in choices):
            errors.append(('outside_preferred', sorted(cells)))
    return errors


def validate_rule_rows(rows, by_week):
    errors = []
    for row in rows:
        for week, plan in by_week.items():
            for reason, cells in rule_errors(row, plan, week):
                errors.append({'course_id': text(value(row, 'JXBID')), 'teacher_id': text(value(row, 'JSH')),
                    'source_row': value(row, 'source_row'), 'week': week+1, 'reason': reason,
                    'slots': [{'day': d, 'period': p} for d, p in cells]})
    return errors


class WeeklyCandidates:
    """A factored Cartesian product; count and selection need no full expansion."""
    def __init__(self, groups):
        self.groups = groups

    def __len__(self):
        return min(sys.maxsize, prod(len(options) for _, options in self.groups)) if self.groups else 0

    def __iter__(self):
        if not self.groups: return
        for choices in product(*(options for _, options in self.groups)):
            yield WeeklyArrangement({w: plan for (weeks, _), plan in zip(self.groups, choices) for w in weeks})

    def restrict(self, domain):
        if domain is None: return self
        groups = [(weeks, [plan for plan in options if all((d, p+1) in domain
                  for d, start, hours in plan for p in range(start, start+hours))]) for weeks, options in self.groups]
        return WeeklyCandidates(groups if all(options for _, options in groups) else [])


def generate_candidates(rows, room, teachers, classes, teaching_weeks, *, flag_reschedule=False, resource_check=True):
    import utils1 as engine
    from tryloadlimit import build_preferences, new_check_period_availability
    import numpy as np
    by_active = defaultdict(list)
    for week in sorted(teaching_weeks):
        active = tuple(i for i, row in enumerate(rows) if week in
                       flags(value(row, 'RWJSZCDM')) & flags(value(row, 'SKZCDM')))
        if not active: return WeeklyCandidates([])
        by_active[active].append(week)
    groups = []
    for indices, weeks in by_active.items():
        active_rows = [rows[i] for i in indices]
        if resource_check and any(text(value(r, 'JSH')) not in teachers for r in active_rows):
            return WeeklyCandidates([])
        seeds = [row for row in active_rows if text(value(row, 'Prefer_Time'))] or active_rows[:1]
        options = []; seen = set()
        checked_room = room if resource_check else SimpleNamespace(timetable=np.full((max(teaching_weeks)+1,7,11), '', dtype='U1'))
        for seed in seeds:
            constraints = build_preferences(value(seed, 'Prefer_Time') or value(seed, 'available_Time'), value(seed, 'unavailable_Time'))
            for days, hours in engine.course_day_patterns(seed, 7, constraints, flag_reschedule):
                candidates = new_check_period_availability(days, hours, checked_room,
                    teachers if resource_check else {}, classes if resource_check else [], weeks, constraints)
                for plan in candidates:
                    key = tuple(plan)
                    if key in seen: continue
                    seen.add(key)
                    if not any(rule_errors(row, plan, weeks[0]) for row in active_rows):
                        options.append(plan)
        if not options:
            if len(weeks)>1:
                # Same responsibility group may still need different slots in
                # different weeks because actual resource occupancy changes.
                for week in weeks:
                    split = generate_candidates(rows, room, teachers, classes, [week],
                        flag_reschedule=flag_reschedule, resource_check=resource_check)
                    if not len(split): return WeeklyCandidates([])
                    groups.extend(split.groups)
                continue
            return WeeklyCandidates([])
        groups.append((weeks, options))
    return WeeklyCandidates(groups)


def schedule_scoped_class(engine, cid, rows, courses, teachers, classes, rooms, *, flag_reschedule,
                          relax_constraints, class_ralex, time_domain):
    teacher_map = teacher_week_map(rows, teachers)
    weeks = engine.get_teaching_weeks(rows)
    class_list = [] if class_ralex or not engine.check_class_conflict(rows[0]) else engine.get_class_instances1(rows[0], classes, courses)
    eligible = engine.filter_suitable_classrooms(rooms, rows[0], relax_constraints)
    facts = [(room, generate_candidates(rows, room, teacher_map, class_list, weeks,
                flag_reschedule=flag_reschedule).restrict(time_domain)) for room in eligible]
    facts = [(room, candidates) for room, candidates in facts if len(candidates)]
    if not facts: return None, None, None
    plans = []
    if engine.PLACEMENT_STRATEGY == 'first':
        room, choices = max(facts, key=lambda entry: len(entry[1]))
        arrangement = next(iter(choices))
    elif engine.PLACEMENT_STRATEGY == 'random':
        room, choices = engine._PLACEMENT_RNG.choices(facts, weights=[len(c) for _, c in facts], k=1)[0]
        by_week = {}
        for active, options in choices.groups:
            plan = engine._PLACEMENT_RNG.choice(options)
            by_week.update({w: plan for w in active})
        arrangement = WeeklyArrangement(by_week)
    else:
        paired = next((c.scheduled_days for c in courses if c.JXBID == getattr(rows[0], 'paired_jxbid', None)), None)
        for room, choices in facts:
            by_week = {}
            for active, options in choices.groups:
                _, plan = engine._select_balanced([(room, p) for p in options], rooms, rows[0].KRL, active, paired)
                by_week.update({w: plan for w in active})
            plans.append((room, WeeklyArrangement(by_week)))
        room, arrangement = engine._select_balanced(plans, rooms, rows[0].KRL, weeks, paired)
    from utils_for_reschedule import place_placement
    place_placement(room, arrangement, weeks, cid, rows, teacher_map, class_list)
    return room, arrangement, weeks


def modern_scoped_candidates(factory, cid, course, diagnostic):
    """Use the bounded repair engine per active responsibility group, then
    combine its candidates without losing weeks or importing other courses' rules.
    """
    from copy import copy, deepcopy
    import heapq, time
    from scripts.repair_data import parse_time_windows
    from scripts.repair_courses import CandidateFactory, required_week_load
    if course.issues:
        diagnostic['reason'] = 'data_issue';return []
    try:
        expected = required_week_load(course)
    except ValueError as error:
        diagnostic.update(reason='unsupported', detail=str(error));return []
    # As in ordinary candidates and independent acceptance, source weeks/ZXS
    # determine the plan; LLXS is not a second gate on ceil-rounded periods.
    groups = defaultdict(list)
    rules = modern_policy_rules(course, factory.config)
    for week in sorted(course.weeks):
        indices = tuple(i for i, row in enumerate(rules) if week-1 in
                        flags(value(row, 'RWJSZCDM')) & flags(value(row, 'SKZCDM')))
        if not indices:
            diagnostic.update(reason='data_issue',detail='teacher responsibility does not cover all teaching weeks');return []
        groups[indices].append(week)
    candidates=[];children=[]
    for indices, weeks in [(idx,[w]) for idx,active_weeks in groups.items() for w in active_weeks]:
        if time.monotonic()>=factory.deadline:
            diagnostic.update(reason='search_limit',truncated=True);return []
        active=[rules[i] for i in indices]
        child=deepcopy(course);child.teacher_time_rules=()
        child.weeks=frozenset(weeks)
        child.teachers={tid:frozenset(set(w)&set(weeks)) for tid,w in course.teachers.items() if set(w)&set(weeks)}
        if child.weekly_loads:
            child.weekly_loads={w:expected[w] for w in weeks};child.hours=max(child.weekly_loads.values())
        child.total_hours=sum(expected[w] for w in weeks)
        child.prefer=next((text(value(row,'Prefer_Time')) for row in active if text(value(row,'Prefer_Time'))),'')
        banned=set().union(*(parse_time_windows(text(value(row,'unavailable_Time'))) for row in active))
        for row in active:
            preferred=text(value(row,'Prefer_Time'))
            if preferred:
                banned|={(d,p) for d in range(7) for p in range(1,12)}-parse_time_windows(preferred)
            available=text(value(row,'available_Time'))
            if available:
                banned|={(d,p) for d in range(7) for p in range(1,12)}-parse_time_windows(available)
        child.unavailable=';'.join('周'+ '一二三四五六日'[d]+f'({p}-{p}节)' for d,p in sorted(banned))
        data=copy(factory.dataset)
        data.courses=dict(factory.dataset.courses)
        data.courses[cid]=child
        child_config=deepcopy(factory.config)
        # Bans below include the retained available_Time and intersected row
        # preferences. The source unavailable_Time was removed above only.
        child_config.relax_course_unavailable=False
        search=CandidateFactory(data,child_config,factory.deadline,occupancy=factory.occupancy)
        options=search.get(cid)
        options=[(cost,entries) for cost,entries in options if not validate_rule_rows(active,
                 {week-1:[(e.day,min(e.periods)-1,len(e.periods)) for e in entries if week in e.weeks] for week in weeks})]
        children.append({'weeks':weeks,'diagnostic':search.diagnostics.get(cid,{})})
        if not options:
            diagnostic.update(reason='no_joint_teacher_time_pattern',teacher_week_groups=children,
                              truncated=search.diagnostics.get(cid,{}).get('truncated',False))
            return []
        candidates.append(options)
    initial=(0,)*len(candidates)
    heap=[(sum(candidates[i][0][0] for i in range(len(candidates))),initial)];seen={initial};result=[]
    while heap and len(result)<factory.config.max_candidates_per_course:
        if time.monotonic()>=factory.deadline:
            diagnostic.update(reason='search_limit',truncated=True);return []
        cost,indices=heapq.heappop(heap)
        entries=tuple(e for i,index in enumerate(indices) for e in candidates[i][index][1])
        result.append((cost,entries))
        for i,index in enumerate(indices):
            if index+1>=len(candidates[i]):continue
            next_indices=indices[:i]+(index+1,)+indices[i+1:]
            if next_indices in seen:continue
            seen.add(next_indices)
            heapq.heappush(heap,(sum(candidates[j][k][0] for j,k in enumerate(next_indices)),next_indices))
    diagnostic.update(reason='candidates_generated',candidates=len(result),teacher_week_groups=children,
                      truncated=bool(heap) or any(c['diagnostic'].get('truncated') for c in children))
    return result


def modern_policy_rules(course, config):
    """Apply the same explicit operation policy to every teacher relation."""
    from scripts.repair_courses import effective_changes
    from scripts.repair_data import parse_time_windows
    rows = [dict(row) for row in course.teacher_time_rules]
    relax = 'time_preference' in effective_changes(course, config)
    for row in rows:
        pref = parse_time_windows(text(value(row, 'Prefer_Time')))
        if relax and config.time_scope == 'same_day' and pref:
            row['Prefer_Time'] = ';'.join('周'+'一二三四五六日'[d]+'(1-11节)' for d in sorted({d for d,p in pref}))
        elif relax and config.time_scope in ('weekdays', 'configured_days'):
            row['Prefer_Time'] = ''
        if config.relax_course_unavailable:
            row['unavailable_Time'] = ''
    return rows


def relaxation_details(course, entries):
    from scripts.repair_data import parse_time_windows
    fields=set();level='strict'
    rows=course.teacher_time_rules or ({'Prefer_Time':course.prefer,'unavailable_Time':course.unavailable},)
    for row in rows:
        weeks = (flags(value(row,'RWJSZCDM')) & flags(value(row,'SKZCDM'))) if course.teacher_time_rules else {w-1 for w in course.weeks}
        cells={(e.day,p) for e in entries if any(w-1 in weeks for w in e.weeks) for p in e.periods}
        pref=parse_time_windows(text(value(row,'Prefer_Time')))
        if pref and cells-pref:
            fields.add('time_preference')
            if level != 'course_unavailable':
                candidate='same_day' if {d for d,p in cells}<={d for d,p in pref} else 'weekdays'
                if level!='weekdays':level=candidate
        if cells & parse_time_windows(text(value(row,'unavailable_Time'))):
            fields.add('course_unavailable');level='course_unavailable'
    return level, fields
