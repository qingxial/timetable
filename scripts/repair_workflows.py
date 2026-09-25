"""Audited input corrections and day/evening/weekend fallback proposals."""
from collections import Counter
from copy import deepcopy
from dataclasses import asdict, replace
import hashlib
import json
import math
import time

try:
    from .repair_data import Placement
    from .repair_courses import (RepairConfig, Occupancy, placement_dict, repair,
                                 required_week_load, validate_changes, week_load)
except ImportError:
    from repair_data import Placement
    from repair_courses import (RepairConfig, Occupancy, placement_dict, repair,
                                 required_week_load, validate_changes, week_load)


def apply_data_corrections(dataset, corrections):
    """Return a copy. Corrections are explicit planning decisions, never source edits."""
    result = deepcopy(dataset)
    audits = []
    seen = set()
    for item in corrections:
        cid = item['course_id']; operation = item['operation']
        if cid not in result.courses:
            raise ValueError(f'Unknown correction course: {cid}')
        if (cid, operation) in seen:
            raise ValueError(f'Duplicate correction: {cid}/{operation}')
        seen.add((cid, operation))
        if not isinstance(item.get('evidence'), str) or not item['evidence'].strip():
            raise ValueError('Every correction requires an explicit evidence/decision basis')
        c = result.courses[cid]
        scheduled = [p for p in result.placements if p.course_id == cid]
        audit = {'course_id': cid, 'operation': operation, 'evidence': item['evidence'],
                 'source_files_modified': False}
        if operation in {'redistribute_hours', 'set_one_off_week'} and '@' in cid:
            raise ValueError('Virtual-class load corrections require a joint parent-course hour plan; inherited LLXS must not be allocated independently to each child')
        if operation == 'redistribute_hours':
            if scheduled:
                raise ValueError('Weekly load correction only supports unscheduled courses')
            if item.get('authoritative_field') != 'LLXS':
                raise ValueError('Redistribution must explicitly preserve LLXS')
            if c.total_hours is None or not math.isfinite(c.total_hours) or c.total_hours != int(c.total_hours):
                raise ValueError('A positive integer LLXS is required')
            if c.hours != item['expected_weekly_hours'] or c.total_hours != item['expected_total_hours']:
                raise ValueError('Hours correction precondition changed')
            weeks = sorted(c.weeks)
            if not weeks:
                raise ValueError('Teaching weeks missing')
            if item.get('weekly_loads') is not None:
                loads = {int(w): h for w, h in item['weekly_loads'].items()}
                if len(loads) != len(item['weekly_loads']):
                    raise ValueError('Duplicate normalized teaching weeks')
                strategy = 'explicit_weekly_loads'
            elif item.get('strategy') == 'balanced_frontload':
                unit = item.get('allocation_unit', 1)
                if type(unit) is not int or unit not in (1, 2) or int(c.total_hours) % unit:
                    raise ValueError('allocation_unit must be 1 or 2 and divide LLXS')
                base, extra = divmod(int(c.total_hours)//unit, len(weeks))
                loads = {w: (base + (index < extra))*unit for index, w in enumerate(weeks)}
                strategy = 'balanced_frontload'
            else:
                raise ValueError('Provide explicit weekly_loads or balanced_frontload strategy')
            if sum(loads.values()) != c.total_hours:
                raise ValueError('Weekly loads must sum exactly to unchanged LLXS')
            corrected = replace(c, hours=float(max(loads.values())), weekly_loads=loads)
            required_week_load(corrected)
            if c.block_template and tuple(c.block_template) != (2,) * (int(corrected.hours)//2) + ((1,) if int(corrected.hours)%2 else ()):
                raise ValueError('Explicit block template requires a separate reviewed teaching plan')
            result.courses[cid] = corrected
            audit.update(before={'weekly_hours': c.hours, 'total_hours': c.total_hours, 'weeks': weeks},
                         after={'weekly_hours_max': corrected.hours, 'total_hours': c.total_hours,
                                'weekly_loads': {str(w): h for w, h in loads.items()}},
                         strategy=strategy, interpretation='Candidate teaching plan preserving LLXS and every source teaching week; not a uniquely inferred correction of source truth.')
        elif operation == 'set_one_off_week':
            week = item['teaching_week']
            if (scheduled or c.hours != 0 or c.hours != item['expected_weekly_hours']
                    or c.total_hours != item['expected_total_hours']
                    or c.total_hours is None or not math.isfinite(c.total_hours)
                    or c.total_hours != int(c.total_hours) or not 1 <= c.total_hours <= c.max_block
                    or c.block_template):
                raise ValueError('One-off planning requires an unscheduled zero-weekly-hours course with a positive single-block LLXS and matching original values')
            if item.get('weeks_confirmed') is not True or type(week) is not int or week not in c.weeks:
                raise ValueError('One-off teaching week must be explicitly confirmed within the source week domain')
            corrected = replace(c, weeks=frozenset({week}), hours=float(c.total_hours),
                                weekly_loads={week: int(c.total_hours)})
            required_week_load(corrected)
            result.courses[cid] = corrected
            audit.update(before={'weekly_hours': c.hours, 'total_hours': c.total_hours, 'weeks': sorted(c.weeks)},
                after={'weekly_loads': {str(week): int(c.total_hours)}, 'total_hours': c.total_hours, 'weeks': [week]},
                interpretation='Explicitly confirmed one-off teaching week selected from the original domain; LLXS preserved, source weeks deliberately narrowed by the supplied decision, never inferred automatically.')
        elif operation == 'set_capacity':
            if scheduled:
                raise ValueError('Capacity correction only supports unscheduled courses')
            if item.get('enrollment_frozen') is not True:
                raise ValueError('Actual enrollment must be explicitly confirmed frozen')
            new = item['capacity']
            if type(new) not in (int, float) or not math.isfinite(new) or new <= 0:
                raise ValueError('Capacity must be positive and finite')
            if c.capacity != item['expected_capacity']:
                raise ValueError('Capacity correction precondition changed')
            result.courses[cid] = replace(c, capacity=float(new))
            audit.update(before={'capacity': c.capacity}, after={'capacity': new},
                         interpretation='Capacity demand corrected using explicitly confirmed enrollment; room capacities stay fixed.')
        elif operation == 'replace_classes':
            if scheduled:
                raise ValueError('Class correction only supports unscheduled courses')
            if sorted(c.classes) != sorted(item['expected_classes']):
                raise ValueError('Class correction precondition changed')
            classes = frozenset(item['classes'])
            if not classes or result.class_registry is None or not classes <= result.class_registry:
                raise ValueError('All replacement classes must be in the input BJMC registry')
            issues = [i for i in c.issues if not i.startswith('coverage:')]
            result.courses[cid] = replace(c, classes=classes, issues=issues)
            audit.update(before={'classes': sorted(c.classes), 'issues': c.issues},
                         after={'classes': sorted(classes), 'issues': issues})
        elif operation == 'quarantine_incomplete_segments':
            invalid = [p for p in scheduled if not p.weeks or not p.periods or p.day not in range(7)]
            valid = [p for p in scheduled if p not in invalid]
            if not invalid or c.total_hours is None:
                raise ValueError('Requires incomplete baseline records and declared LLXS')
            if week_load(valid) != required_week_load(c) or sum(week_load(valid).values()) != c.total_hours:
                raise ValueError('Known complete records must already satisfy all teaching weeks and LLXS')
            if len(invalid) != item['expected_incomplete_count']:
                raise ValueError('Incomplete-record count precondition changed')
            result.placements = [p for p in result.placements if p not in invalid]
            audit.update(before=[placement_dict(p) for p in invalid], after=[],
                         interpretation='Quarantine unverifiable surplus export records; retain every complete source assignment and all declared hours. No missing weeks fabricated.')
        else:
            raise ValueError(f'Unknown correction operation: {operation}')
        audits.append(audit)
    if audits:
        identity = {'previous_corrections_hash': dataset.source_hashes.get('corrections'), 'actions': audits}
        result.source_hashes['corrections'] = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return result, audits


def repair_with_fallbacks(dataset, target_ids, config, stages=None, total_time_limit_seconds=180):
    """Insert remaining courses stagewise; earlier successful placements stay fixed."""
    policy = RepairConfig(**deepcopy(config)); policy.validate()
    if policy.max_moved_courses != 0 or policy.movable_ids:
        raise ValueError('Fallback preserves prior placements; use local optimization for explicit movements')
    stages = ['daytime', 'evening', 'weekend'] if stages is None else stages
    valid_stages = ['daytime', 'evening', 'weekend']
    if not stages or any(s not in valid_stages for s in stages) or stages != sorted(set(stages), key=valid_stages.index):
        raise ValueError('Stages must be unique and ordered daytime, evening, weekend')
    if type(total_time_limit_seconds) not in (int, float) or not math.isfinite(total_time_limit_seconds) or total_time_limit_seconds <= 0:
        raise ValueError('Total time limit must be positive and finite')
    requested = list(dict.fromkeys(policy.target_ids or target_ids))
    current = deepcopy(dataset)
    before_state = Occupancy(dataset)
    before = dict(before_state.placements)
    outcomes, changes, runs = {}, {}, []
    start = time.monotonic(); remaining = requested[:]; total_nodes = 0
    last_policy = asdict(policy)
    for stage in stages:
        left = total_time_limit_seconds - (time.monotonic() - start)
        if left <= 0 or not remaining:
            break
        args = asdict(policy)
        args.update(target_ids=remaining, time_limit_seconds=min(policy.time_limit_seconds, left),
                    days=sorted(set(policy.days) & set(range(7 if stage == 'weekend' else 5))),
                    periods=sorted(set(policy.periods) & set(range(1, 9 if stage == 'daytime' else 12))))
        if not args['days'] or not args['periods']:
            runs.append({'stage': stage, 'status': 'outside_caller_domain', 'config': args})
            continue
        proposal = repair(current, remaining, args)
        last_policy = deepcopy(args)
        total_nodes += proposal['summary']['search_nodes']
        for row in proposal['results']:
            outcomes[row['course_id']] = {**row, 'attempt_stage': stage}
        for row in proposal['changes']:
            changes[row['course_id']] = {**row, 'fallback_stage': stage}
        current.placements = [Placement(p['course_id'], frozenset(p['weeks']), p['day'], tuple(p['periods']), p['room_id']) for p in proposal['placements']]
        runs.append({'stage': stage, 'config': args, 'summary': proposal['summary'],
                     'remaining': sum(r['status'] not in ('placed', 'already_scheduled') for r in proposal['results'])})
        remaining = [cid for cid in remaining if outcomes[cid]['status'] not in ('placed', 'already_scheduled')]
    for cid in requested:
        unattempted = bool(runs) and all(r.get('status') == 'outside_caller_domain' for r in runs) and len(runs) == len(stages)
        outcomes.setdefault(cid, {'course_id': cid,
            'status': 'outside_caller_domain' if unattempted else 'search_limit',
            'diagnosis': {'detail': 'No requested stage intersects the caller domain' if unattempted else 'Workflow total budget exhausted before this target was attempted'}})
    state = Occupancy(current)
    assert all(state.placements[cid] == entries for cid, entries in before.items())
    check_config = RepairConfig(**last_policy)
    errors = validate_changes(current, before, state, set(changes), check_config)
    if errors:
        raise RuntimeError(f'Fallback proposal rejected: {errors[:5]}')
    last_policy['target_ids'] = requested
    return {'schema_version': 1, 'mode': 'proposal', 'config': last_policy,
            'source_hashes': dataset.source_hashes,
            'summary': {'requested': len(requested), 'inserted': len(changes), 'moved': 0,
                        'baseline_scheduled': len(before), 'proposed_scheduled': len(state.placements),
                        'search_nodes': total_nodes, 'elapsed_seconds': round(time.monotonic()-start, 3)},
            'results': [outcomes[cid] for cid in requested], 'changes': [changes[cid] for cid in sorted(changes)],
            'placements': [placement_dict(p) for cid in sorted(state.placements) for p in state.placements[cid]],
            'data_issues': dataset.issues,
            'validation': {'changed_courses_checked': len(changes), 'new_conflicts': 0,
                           'hours_and_weeks_preserved': True, 'prior_placements_unchanged': True,
                           'baseline_conflict_cells': before_state.recorded_conflict_counts,
                           'baseline_uncertain_course_ids': sorted(before_state.uncertain_ids),
                           'scope': 'Known teacher/class/room resources only; original data issues remain reported.'},
            'workflow': {'name': 'day_evening_weekend_fallback', 'stages': runs,
                         'total_time_limit_seconds': total_time_limit_seconds,
                         'interpretation': 'Earlier successful insertions stay fixed; movement/quality optimization is a separate explicit tool.'}}
