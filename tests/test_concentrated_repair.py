"""Explicit concentrated teaching policies preserve complete resource-safe courses."""
from copy import deepcopy
from dataclasses import replace
import itertools
import unittest
from unittest.mock import patch

from test_repair_courses import WEEKS, course, dataset, placed, policy, room
from scripts.repair_courses import (CandidateFactory, Occupancy, Placement, RepairConfig,
                                    blocks_for, pattern_placements, preference_cost, repair,
                                    required_week_load, time_options, validate_changes, week_load)


def concentrated(**changes):
    values = dict(days=[0, 2, 3], periods=list(range(1, 9)), max_weekly_hours=77,
                  max_blocks_per_day=4, max_candidates_per_course=20)
    values.update(changes)
    return RepairConfig(**values)


def entries(proposal, cid):
    return [Placement(p['course_id'], frozenset(p['weeks']), p['day'], tuple(p['periods']), p['room_id'])
            for p in proposal['placements'] if p['course_id'] == cid]


class ConcentratedLoadTests(unittest.TestCase):
    def test_exact_weekend_rule_is_hard_even_when_time_preferences_are_relaxable(self):
        c = course('C', hours=16, weeks=frozenset({1}), teachers={'T': frozenset({1})}, total_hours=16,
                   prefer='周六全天；周日全天', special='仅周六周日排课')
        cfg = concentrated(days=list(range(7)), periods=list(range(1, 12)), max_blocks_per_day=4,
                           time_scope='configured_days', allowed_changes=['time_preference'],
                           reviewed_soft_time_ids=['C'])
        result = repair(dataset([c]), ['C'], cfg)
        planned = entries(result, 'C')
        self.assertEqual(result['summary']['inserted'], 1)
        self.assertEqual({p.day for p in planned}, {5, 6})
        self.assertEqual(week_load(planned), {1: 16})
        self.assertTrue(all(sum(len(p.periods) for p in planned if p.day == day) == 8 for day in (5, 6)))
        self.assertEqual(repair(dataset([c]), ['C'], replace(cfg, max_blocks_per_day=3))['summary']['inserted'], 0)

    def test_weekend_rule_does_not_erase_contradictory_preferences_or_accept_unknown_text(self):
        cfg = concentrated(days=list(range(7)), time_scope='configured_days',
                           allowed_changes=['time_preference'], reviewed_soft_time_ids=['C'])
        conflict = course('C', prefer='周一(1-2节)', special='仅周六周日排课')
        self.assertEqual(repair(dataset([conflict]), ['C'], cfg)['summary']['inserted'], 0)
        unknown = course('C', prefer='周六全天；周日全天', special='仅周六周日排课并使用两间教室')
        response = repair(dataset([unknown]), ['C'], cfg)
        self.assertEqual(response['summary']['inserted'], 0)
        self.assertEqual(response['results'][0]['status'], 'unreviewed_special_requirement')

    def test_sixteen_and_eighteen_hours_keep_whole_course_over_multiple_days_and_blocks(self):
        for hours in (16, 18):
            with self.subTest(hours=hours):
                c = course('C', hours=hours, total_hours=hours * len(WEEKS))
                data = dataset([c]); original = deepcopy(data.to_dict())
                proposal = repair(data, ['C'], concentrated())
                self.assertEqual(proposal['summary']['inserted'], 1)
                planned = entries(proposal, 'C')
                self.assertEqual(week_load(planned), {w: hours for w in WEEKS})
                self.assertEqual(sum(week_load(planned).values()), c.total_hours)
                self.assertGreater(len(planned), len({p.day for p in planned}))
                for w in WEEKS:
                    cells = [(p.day, k) for p in planned if w in p.weeks for k in p.periods]
                    self.assertEqual(len(cells), len(set(cells)))
                    self.assertLessEqual(max(sum(p.day == d and w in p.weeks for p in planned)
                                             for d in {p.day for p in planned}), 4)
                self.assertEqual(data.to_dict(), original)

    def test_default_rejects_high_load_and_one_block_per_day_remains_default(self):
        c = course('C', hours=16, total_hours=32)
        default = repair(dataset([c]), ['C'], policy(days=list(range(7)), periods=list(range(1, 12))))
        self.assertEqual(default['summary']['inserted'], 0)
        self.assertEqual(default['results'][0]['status'], 'unsupported')
        one_per_day = repair(dataset([c]), ['C'], concentrated(days=list(range(7)), max_blocks_per_day=1))
        self.assertEqual(one_per_day['summary']['inserted'], 0)
        self.assertEqual(RepairConfig().max_blocks_per_day, 1)
        self.assertEqual(RepairConfig().max_weekly_hours, 8)

    def test_default_candidate_order_matches_prior_cartesian_enumeration(self):
        c = course('C', hours=4, prefer='周三(3-4节)')
        cfg = policy(days=[0, 2], periods=[1, 2, 3, 4], allowed_changes=['time_preference'],
                     time_scope='weekdays', max_candidates_per_course=100)
        domain, pref = time_options(c, cfg)
        slots = sorted([(d, (s, s + 1)) for d in cfg.days for s in (1, 3)
                        if all((d, k) in domain for k in (s, s + 1))],
                       key=lambda slot: (preference_cost(*slot, pref), slot))
        prior = []
        for combo in itertools.product(slots, repeat=2):
            if combo[0][0] != combo[1][0] and combo[0] < combo[1]:
                prior.append((sum(preference_cost(*slot, pref) for slot in combo), combo))
        prior.sort()
        expected = [(cost, pattern_placements(c, combo, 'R1')) for cost, combo in prior]
        self.assertEqual(CandidateFactory(dataset([c]), cfg).get('C'), expected)

    def test_explicit_weekly_loads_can_exceed_eight_but_obey_config_cap(self):
        c = course('C', hours=18, total_hours=34, weekly_loads={1: 18, 2: 16})
        self.assertEqual(required_week_load(c), {1: 18, 2: 16})
        self.assertEqual(repair(dataset([c]), ['C'], concentrated(max_weekly_hours=16))['summary']['inserted'], 0)
        proposal = repair(dataset([c]), ['C'], concentrated())
        self.assertEqual(week_load(entries(proposal, 'C')), {1: 18, 2: 16})
        with self.assertRaises(ValueError):
            required_week_load(replace(c, hours=78, weekly_loads={1: 78, 2: 16}))

    def test_weekly_role_symmetry_retains_both_shortened_block_assignments(self):
        c = course('C', hours=4, total_hours=6, weekly_loads={1: 4, 2: 2})
        cfg = concentrated(days=[0], periods=[1, 2, 3, 4], max_blocks_per_day=2)
        candidates = CandidateFactory(dataset([c]), cfg).get('C')
        self.assertEqual(len(candidates), 2)
        shorter_week_cells = {tuple(sorted(k for p in ps if 2 in p.weeks for k in p.periods))
                              for _, ps in candidates}
        self.assertEqual(shorter_week_cells, {(1, 2), (3, 4)})

    def test_hard_bans_and_source_total_hours_remain_enforced(self):
        c = course('C', hours=16, total_hours=32, unavailable='周一(1-2节)')
        cfg = concentrated(days=[0, 1, 2, 3], additional_forbidden='周三(3-4节)')
        proposal = repair(dataset([c]), ['C'], cfg)
        self.assertEqual(proposal['summary']['inserted'], 1)
        cells = {(p.day, k) for p in entries(proposal, 'C') for k in p.periods}
        self.assertFalse(cells & {(0, 1), (0, 2), (2, 3), (2, 4), (1, 5), (1, 6), (1, 7), (1, 8)})
        bad = repair(dataset([replace(c, total_hours=30)]), ['C'], cfg)
        self.assertEqual(bad['summary']['inserted'], 0)
        self.assertEqual(bad['results'][0]['status'], 'source_hours_inconsistent')

    def test_configuration_bounds_and_filter_movement_compatibility(self):
        for changes in ({'max_weekly_hours': 0}, {'max_weekly_hours': 78}, {'max_weekly_hours': True},
                        {'max_blocks_per_day': 0}, {'max_blocks_per_day': 12},
                        {'filter_fixed_occupancy': 'yes'},
                        {'filter_fixed_occupancy': True, 'max_moved_courses': 1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                concentrated(**changes).validate()
        concentrated(max_weekly_hours=77, max_blocks_per_day=11).validate()
        self.assertEqual(sum(blocks_for(course('C', hours=77), 77)), 77)


class FixedOccupancyAndBudgetTests(unittest.TestCase):
    def test_teacher_and_class_blockers_are_removed_before_retained_candidate_cap(self):
        for shared in ('teacher', 'class'):
            old = course('OLD')
            c = course('C', hours=16, total_hours=32,
                       teachers=old.teachers if shared == 'teacher' else {'NEW': WEEKS},
                       classes=old.classes if shared == 'class' else frozenset({'NEW'}))
            data = dataset([old, c], [placed('OLD', room_id='R2')], [room(), room('R2')])
            cfg = concentrated(max_candidates_per_course=1)
            plain = repair(data, ['C'], cfg)
            filtered = repair(data, ['C'], replace(cfg, filter_fixed_occupancy=True))
            with self.subTest(shared=shared):
                self.assertEqual(plain['summary']['inserted'], 0)
                self.assertEqual(filtered['summary']['inserted'], 1)
                diag = filtered['results'][0]['diagnosis']
                self.assertTrue(diag['fixed_occupancy_filter_applied'])
                self.assertGreater(diag['fixed_teacher_class_pruned_slots'], 0)
                self.assertEqual(diag['candidates'], 1)
                self.assertEqual(diag['candidate_cap_scope'], 'retained_after_fixed_occupancy_filter')
                self.assertEqual(week_load(entries(filtered, 'C')), {1: 16, 2: 16})

    def test_room_intersection_prunes_prefixes_before_the_cap(self):
        a, b = course('A'), course('B')
        c = course('C', hours=16, total_hours=32)
        data = dataset([a, b, c], [placed('A', periods=tuple(range(1, 9))),
                                 placed('B', day=2, periods=tuple(range(1, 9)), room_id='R2')],
                       [room(), room('R2')])
        cfg = concentrated(max_candidates_per_course=1, filter_fixed_occupancy=True)
        result = repair(data, ['C'], cfg)
        self.assertEqual(result['summary']['inserted'], 1)
        self.assertEqual({p.room_id for p in entries(result, 'C')}, {'R2'})
        self.assertEqual({p.day for p in entries(result, 'C')}, {0, 3})
        self.assertGreater(result['results'][0]['diagnosis']['fixed_room_intersection_pruned_prefixes'], 0)
        self.assertEqual(result['validation']['new_conflicts'], 0)

    def test_factory_without_explicit_occupancy_does_not_hide_explanation_domain(self):
        a, c = course('A'), course('C', prefer='周一(1-2节)')
        data = dataset([a, c], [placed('A')])
        cfg = policy(filter_fixed_occupancy=True)
        explanation_factory = CandidateFactory(data, cfg)
        filtered_factory = CandidateFactory(data, cfg, occupancy=Occupancy(data))
        self.assertTrue(explanation_factory.get('C'))
        self.assertFalse(filtered_factory.get('C'))
        self.assertFalse(explanation_factory.diagnostics['C']['fixed_occupancy_filter_applied'])
        self.assertTrue(filtered_factory.diagnostics['C']['fixed_occupancy_filter_applied'])

    def test_partial_teacher_weeks_do_not_overprune(self):
        a = course('A', teachers={'SHARED': frozenset({1})})
        c = course('C', teachers={'SHARED': frozenset({2}), 'OTHER': frozenset({1})},
                   prefer='周一(1-2节)', explicit_rooms=frozenset({'R2'}))
        data = dataset([a, c], [placed('A')], [room(), room('R2')])
        result = repair(data, ['C'], policy(filter_fixed_occupancy=True, max_candidates_per_course=1))
        self.assertEqual(result['summary']['inserted'], 1)

    def test_same_stage_insertions_are_filtered_before_later_courses_candidate_cap(self):
        a = course('A', prefer='周一(1-2节)')
        b = course('B', prefer='周一(1-2节)')
        data = dataset([a, b], rooms=[room(), room('R2')])
        cfg = policy(filter_fixed_occupancy=True, max_candidates_per_course=1)
        result = repair(data, ['A', 'B'], cfg)
        self.assertEqual(result['summary']['inserted'], 2)
        self.assertEqual({p.room_id for p in entries(result, 'A')}, {'R1'})
        self.assertEqual({p.room_id for p in entries(result, 'B')}, {'R2'})
        self.assertEqual(result['summary']['moved'], 0)
        self.assertEqual(result['validation']['new_conflicts'], 0)

    def test_expired_deadline_does_not_expand_high_load_cartesian_product(self):
        c = course('C', hours=18, total_hours=36)
        calls = 0

        def clock():
            nonlocal calls
            calls += 1
            return 0 if calls == 1 else 100

        with patch('scripts.repair_courses.time.monotonic', side_effect=clock):
            result = repair(dataset([c]), ['C'], concentrated(time_limit_seconds=1))
        self.assertEqual(result['summary']['inserted'], 0)
        self.assertEqual(result['results'][0]['status'], 'search_limit')
        self.assertEqual(result['summary']['search_nodes'], 0)
        self.assertLess(calls, 20)

    def test_impossible_load_above_supported_grid_capacity_stops_before_search(self):
        c = course('C', hours=72, total_hours=144)
        cfg = concentrated(days=list(range(7)), periods=list(range(1, 12)), max_blocks_per_day=11)
        factory = CandidateFactory(dataset([c]), cfg)
        self.assertFalse(factory.get('C'))
        self.assertEqual(factory.diagnostics['C']['generation_nodes'], 0)
        self.assertEqual(factory.diagnostics['C']['insufficient_supported_grid_cells'], 66)

    def test_acceptance_rejects_overlapping_or_too_many_daily_blocks(self):
        c = course('C', hours=4, total_hours=8)
        data = dataset([c])
        state = Occupancy(data)
        state.put('C', [placed('C'), placed('C', periods=(3, 4))])
        errors = validate_changes(data, {}, state, {'C'}, policy())
        self.assertIn('daily_block_limit', {row['reason'] for row in errors})
        state = Occupancy(data)
        state.put('C', [placed('C'), placed('C', periods=(2, 3, 4))])
        errors = validate_changes(data, {}, state, {'C'}, concentrated())
        self.assertIn('overlapping_course_blocks', {row['reason'] for row in errors})


if __name__ == '__main__':
    unittest.main()
