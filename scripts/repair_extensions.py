"""Verify a saved insertion proposal before extending its explicit target scope."""
from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import asdict, replace

try:
    from .repair_data import Placement
    from .repair_courses import (RepairConfig, Occupancy, blocks_for, required_week_load,
        supported_special_requirement, validate_changes, placement_dict)
    from .repair_workflows import repair_with_fallbacks
except ImportError:
    from repair_data import Placement
    from repair_courses import (RepairConfig, Occupancy, blocks_for, required_week_load,
        supported_special_requirement, validate_changes, placement_dict)
    from repair_workflows import repair_with_fallbacks


def _placements(rows):
    if not isinstance(rows, list):
        raise ValueError('Proposal placements must be a list')
    parsed = []
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {'course_id', 'weeks', 'day', 'periods', 'room_id'}
                or not isinstance(row['course_id'], str) or not isinstance(row['room_id'], str)
                or type(row['day']) is not int or row['day'] not in range(-1, 7)):
            raise ValueError('Malformed proposal placement')
        for field, maximum in [('weeks', 53), ('periods', 11)]:
            values = row[field]
            if (not isinstance(values, list) or any(type(v) is not int or not 1 <= v <= maximum for v in values)
                    or values != sorted(set(values))):
                raise ValueError(f'Malformed proposal {field}')
        parsed.append(Placement(row['course_id'], frozenset(row['weeks']), row['day'], tuple(row['periods']), row['room_id']))
    return parsed


def _check_insertions(dataset, before, state, inserted, policy):
    """Check saved rows independently of the producer's success/validation flags."""
    errors = validate_changes(dataset, before, state, inserted, policy)
    if errors:
        raise ValueError(f'Seed/current insertion validation failed: {errors[:5]}')
    for cid in inserted:
        course = dataset.courses[cid]
        if (course.issues or not course.weeks or not course.teachers
                or not course.weeks <= set().union(*course.teachers.values())
                or (course.special.strip() and not supported_special_requirement(course))
                or any(marker in course.prefer for marker in ('[', '【', '［'))):
            raise ValueError(f'Seed insertion has unsupported or incomplete metadata: {cid}')
        blocks = blocks_for(course, policy.max_weekly_hours)
        loads = required_week_load(course)
        if course.total_hours is not None and sum(loads.values()) != course.total_hours:
            raise ValueError(f'Seed insertion does not preserve LLXS: {cid}')
        actual = defaultdict(list)
        for entry in state.placements[cid]:
            if not entry.weeks or not entry.periods:
                raise ValueError(f'Incomplete seed insertion: {cid}')
            if entry.periods != tuple(range(entry.periods[0], entry.periods[-1]+1)):
                raise ValueError(f'Nonconsecutive seed teaching block: {cid}')
            if entry.periods[0] % 2 == 0 and (len(entry.periods) > 1 or policy.single_period_starts != 'any'):
                raise ValueError(f'Seed block violates configured start grid: {cid}')
            for week in entry.weeks:
                actual[week].append(len(entry.periods))
        for week, load in loads.items():
            expected, left = [], load
            for block in blocks:
                if left > 0:
                    expected.append(min(block, left)); left -= block
            if sorted(actual[week]) != sorted(expected):
                raise ValueError(f'Seed blocks do not match the declared weekly plan: {cid}/{week}')


def _validated_seed(dataset, seed, policy):
    """Shared independent checks for scope extension and failed-target retry."""
    seed_policy = RepairConfig(**deepcopy(seed['config'])); seed_policy.validate()
    if policy.max_moved_courses or policy.movable_ids or seed['summary']['moved']:
        raise ValueError('Proposal continuation only supports no-move insertion proposals')
    seed_ids = [r['course_id'] for r in seed['results']]
    if len(seed_ids) != len(set(seed_ids)) or set(seed_ids) - dataset.courses.keys():
        raise ValueError('Seed result IDs must be unique known courses')
    if seed['summary']['requested'] != len(seed_ids):
        raise ValueError('Seed requested count does not match its results')
    original = Occupancy(dataset)
    before = dict(original.placements)
    if any(r['status'] == 'already_scheduled' and r['course_id'] not in before for r in seed['results']):
        raise ValueError('Seed already_scheduled result has no original assignment')
    seeded_data = replace(dataset, placements=_placements(seed.get('placements')))
    seeded = Occupancy(seeded_data)
    changes = {}
    for change in seed['changes']:
        cid = change['course_id']
        if cid in changes or change['operation'] != 'insert' or change.get('before') != [] or cid in before:
            raise ValueError('Seed may contain only unique new insertions')
        if Counter(_placements(change['after'])) != Counter(seeded.placements.get(cid, [])):
            raise ValueError('Seed changes disagree with its complete placements')
        changes[cid] = deepcopy(change)
    placed = {r['course_id'] for r in seed['results'] if r['status'] == 'placed'}
    if placed != set(changes) or seed['summary']['inserted'] != len(placed):
        raise ValueError('Seed inserted results/summary do not match its changes')
    if set(seeded.placements) != set(before) | placed:
        raise ValueError('Seed added or removed assignments outside its changes')
    if any(Counter(seeded.placements[cid]) != Counter(rows) for cid, rows in before.items()):
        raise ValueError('Seed modified original assignments')
    _check_insertions(dataset, before, seeded, placed, seed_policy)
    # The new config must continue to permit all retained seed assignments.
    _check_insertions(dataset, before, seeded, placed, policy)
    return seeded_data, seeded, before, changes, placed, seed_ids


def _attempt_after_seed(dataset, validated, targets, policy, stages, total_time_limit_seconds):
    seeded_data, seeded, before, changes, _, _ = validated
    args = asdict(policy); args['target_ids'] = targets
    extra = repair_with_fallbacks(seeded_data, targets, args, stages, total_time_limit_seconds)
    current = Occupancy(replace(dataset, placements=_placements(extra['placements'])))
    for cid, rows in seeded.placements.items():
        if Counter(current.placements.get(cid, [])) != Counter(rows):
            raise RuntimeError(f'Continuation changed a retained seed assignment: {cid}')
    for change in extra['changes']:
        cid = change['course_id']
        if cid in changes or cid not in targets or change['operation'] != 'insert' or change.get('before') != []:
            raise RuntimeError('Continuation returned an out-of-scope or duplicate change')
        changes[cid] = deepcopy(change)
    _check_insertions(dataset, before, current, set(changes), policy)
    output = deepcopy(extra)
    output['changes'] = [changes[cid] for cid in sorted(changes)]
    output['config'] = asdict(policy)
    output['summary'].update(inserted=len(changes), baseline_scheduled=len(before),
                             proposed_scheduled=len(current.placements))
    output['validation'].update(changed_courses_checked=len(changes), seed_successes_preserved=True,
                                 original_placements_unchanged=True)
    return output, extra


def extend_repair_proposal(dataset, seed, additional_target_ids, config, stages=None, total_time_limit_seconds=180):
    """Preserve seed insertions; hash/correction replay is checked by the JSON boundary."""
    policy = RepairConfig(**deepcopy(config)); policy.validate()
    targets = list(additional_target_ids)
    seed_ids = [r['course_id'] for r in seed['results']]
    if not targets or len(targets) != len(set(targets)) or set(targets) & set(seed_ids):
        raise ValueError('Additional targets must be nonempty, unique, and outside the seed target set')
    if policy.target_ids and set(policy.target_ids) != set(targets):
        raise ValueError('config.target_ids must be empty or exactly the additional target set')
    if set(targets) - dataset.courses.keys():
        raise ValueError('Unknown additional target IDs')
    validated = _validated_seed(dataset, seed, policy)
    if set(targets) & set(validated[1].placements):
        raise ValueError('Additional target already has baseline/seed assignments')
    output, extra = _attempt_after_seed(dataset, validated, targets, policy, stages, total_time_limit_seconds)
    output['results'] = deepcopy(seed['results']) + extra['results']
    output['config']['target_ids'] = seed_ids + targets
    output['summary']['requested'] = len(seed_ids) + len(targets)
    output['scope_extension'] = {
        'original_target_ids': seed.get('scope_extension', {}).get('original_target_ids', seed_ids),
        'additional_target_ids': seed.get('scope_extension', {}).get('additional_target_ids', []) + targets,
        'seed_inserted': len(validated[4]), 'newly_inserted': extra['summary']['inserted'],
        'seed_source_hashes': deepcopy(seed['source_hashes']),
        'seed_summary': deepcopy(seed['summary']),
        'interpretation': 'Explicit scope promotion; previously skipped targets are no longer counted as silently skipped. Search time/nodes describe this extension only.'}
    return output


def retry_repair_proposal(dataset, seed, target_ids, config, stages=None, total_time_limit_seconds=180):
    """Retry an explicit failed subset without changing target scope or seed successes.

    The JSON boundary verifies original hashes and correction replay; this layer
    independently validates every retained insertion and the merged placements.
    """
    policy = RepairConfig(**deepcopy(config)); policy.validate()
    if (not isinstance(target_ids, (list, tuple)) or not target_ids
            or any(not isinstance(cid, str) for cid in target_ids)
            or len(target_ids) != len(set(target_ids))):
        raise ValueError('Retry targets must be a nonempty unique list of course IDs')
    targets = list(target_ids)
    if policy.target_ids and set(policy.target_ids) != set(targets):
        raise ValueError('config.target_ids must be empty or exactly the retry target set')
    validated = _validated_seed(dataset, seed, policy)
    _, seeded, _, _, placed, seed_ids = validated
    seed_results = {row['course_id']: row for row in seed['results']}
    if (set(targets) - set(seed_ids) or set(targets) & set(seeded.placements)
            or any(seed_results[cid]['status'] in ('placed', 'already_scheduled') for cid in targets)):
        raise ValueError('Retry targets must be failed seed results without baseline/seed assignments')
    output, extra = _attempt_after_seed(dataset, validated, targets, policy, stages, total_time_limit_seconds)
    updated = {row['course_id']: row for row in extra['results']}
    if set(updated) != set(targets) or len(updated) != len(extra['results']):
        raise RuntimeError('Retry returned missing or duplicate target results')
    output['results'] = [deepcopy(updated.get(cid, seed_results[cid])) for cid in seed_ids]
    output['config']['target_ids'] = seed_ids
    output['summary']['requested'] = len(seed_ids)
    if 'scope_extension' in seed:
        output['scope_extension'] = deepcopy(seed['scope_extension'])
    output['retry'] = {
        'retry_targets': targets, 'attempted': len(targets),
        'inserted_before': len(placed), 'newly_inserted': extra['summary']['inserted'],
        'seed_summary': deepcopy(seed['summary']),
        'seed_source_hashes': deepcopy(seed['source_hashes']),
        'interpretation': 'Retries only explicit previously failed targets; results are replaced by course ID and the requested scope is unchanged. Search time/nodes describe this retry only.'}
    return output
