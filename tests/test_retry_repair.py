"""Failed-target retries preserve seed successes and the original target scope."""
from copy import deepcopy
from dataclasses import asdict, replace
import unittest

from test_repair_courses import course, dataset, placed, policy, room
from scripts.repair_courses import repair
from scripts.repair_extensions import extend_repair_proposal, retry_repair_proposal


def setup_seed():
    old = course('OLD')
    a = course('A', prefer='周一(3-4节)')
    b = course('B', prefer='周一(5-6节)', classes=frozenset(),
               issues=['coverage: missing class identifiers'])
    c = course('C', capacity=99, prefer='周一(7-8节)')
    data = dataset([old, a, b, c], [placed('OLD')])
    cfg = asdict(policy(periods=list(range(1, 9))))
    initial = repair(data, ['A'], cfg)
    seed = extend_repair_proposal(data, initial, ['B', 'C'], cfg, ['daytime'])
    corrected = deepcopy(data)
    corrected.courses['B'] = replace(b, check_class=False, issues=[])
    corrected.source_hashes = {'courses': 'unchanged', 'corrections': 'explicit-class-opt-out'}
    return corrected, seed, cfg


class RetryRepairTests(unittest.TestCase):
    def test_success_replaces_failed_row_and_preserves_all_seed_and_original_assignments(self):
        data, seed, cfg = setup_seed()
        seed_snapshot = deepcopy(seed); data_snapshot = deepcopy(data.to_dict())
        result = retry_repair_proposal(data, seed, ['B'], cfg, ['daytime'])
        self.assertEqual(result['summary']['requested'], 3)
        self.assertEqual(result['summary']['inserted'], 2)
        self.assertEqual(result['summary']['moved'], 0)
        self.assertEqual(result['summary']['baseline_scheduled'], 1)
        self.assertEqual(result['summary']['proposed_scheduled'], 3)
        self.assertEqual([r['course_id'] for r in result['results']], ['A', 'B', 'C'])
        statuses = {row['course_id']: row['status'] for row in result['results']}
        self.assertEqual(statuses, {'A': 'placed', 'B': 'placed', 'C': 'no_matching_room'})
        for cid in ('OLD', 'A'):
            self.assertEqual([p for p in result['placements'] if p['course_id'] == cid],
                             [p for p in seed['placements'] if p['course_id'] == cid])
        self.assertEqual(result['scope_extension'], seed['scope_extension'])
        self.assertEqual(result['config']['target_ids'], ['A', 'B', 'C'])
        self.assertEqual({c['course_id'] for c in result['changes']}, {'A', 'B'})
        self.assertEqual(result['retry']['retry_targets'], ['B'])
        self.assertEqual(result['retry']['attempted'], 1)
        self.assertEqual(result['retry']['inserted_before'], 1)
        self.assertEqual(result['retry']['newly_inserted'], 1)
        self.assertEqual(result['retry']['seed_summary'], seed['summary'])
        self.assertEqual(result['source_hashes'], data.source_hashes)
        self.assertEqual(result['validation']['changed_courses_checked'], 2)
        self.assertTrue(result['validation']['seed_successes_preserved'])
        self.assertEqual(seed, seed_snapshot)
        self.assertEqual(data.to_dict(), data_snapshot)

    def test_unsuccessful_retry_does_not_add_targets_changes_or_fake_success(self):
        data, seed, cfg = setup_seed()
        result = retry_repair_proposal(data, seed, ['C'], cfg, ['daytime'])
        self.assertEqual(result['summary']['inserted'], 1)
        self.assertEqual(result['summary']['requested'], 3)
        self.assertEqual(result['changes'], seed['changes'])
        self.assertEqual(result['placements'], seed['placements'])
        self.assertEqual(result['retry']['newly_inserted'], 0)
        self.assertEqual(next(r for r in result['results'] if r['course_id'] == 'C')['status'], 'no_matching_room')

    def test_targets_must_be_explicit_unique_previously_failed_subset(self):
        data, seed, cfg = setup_seed()
        for targets in ([], ['B', 'B'], ['A'], ['OLD'], ['UNKNOWN'], 'B', [12]):
            with self.subTest(targets=targets), self.assertRaises(ValueError):
                retry_repair_proposal(data, seed, targets, cfg, ['daytime'])
        with self.assertRaises(ValueError):
            retry_repair_proposal(data, seed, ['B'], {**cfg, 'target_ids': ['C']}, ['daytime'])

    def test_retrying_successful_target_again_is_rejected_and_remaining_retry_does_not_double_count(self):
        data, seed, cfg = setup_seed()
        first = retry_repair_proposal(data, seed, ['B'], cfg, ['daytime'])
        with self.assertRaises(ValueError):
            retry_repair_proposal(data, first, ['B'], cfg, ['daytime'])
        second = retry_repair_proposal(data, first, ['C'], cfg, ['daytime'])
        self.assertEqual(second['summary']['requested'], 3)
        self.assertEqual(second['summary']['inserted'], 2)
        self.assertEqual(second['retry']['inserted_before'], 2)
        self.assertEqual(second['retry']['newly_inserted'], 0)
        self.assertEqual(second['scope_extension'], seed['scope_extension'])

    def test_retry_disallows_movement_and_policy_that_excludes_seed_success(self):
        data, seed, cfg = setup_seed()
        for updated in ({'max_moved_courses': 1}, {'movable_ids': ['OLD']}, {'periods': [5, 6]}):
            with self.subTest(updated=updated), self.assertRaises(ValueError):
                retry_repair_proposal(data, seed, ['B'], {**cfg, **updated}, ['daytime'])

    def test_tampered_seed_original_rows_insertions_results_and_hours_are_rejected(self):
        data, seed, cfg = setup_seed()
        corruptions = []
        bad = deepcopy(seed); bad['placements'] = [r for r in bad['placements'] if r['course_id'] != 'OLD']; corruptions.append(bad)
        bad = deepcopy(seed); bad['summary']['inserted'] = 2; corruptions.append(bad)
        bad = deepcopy(seed); bad['results'].append(deepcopy(bad['results'][0])); corruptions.append(bad)
        bad = deepcopy(seed); bad['changes'][0]['after'][0]['periods'] = [3]; corruptions.append(bad)
        bad = deepcopy(seed); bad['summary']['requested'] = 4; corruptions.append(bad)
        for bad in corruptions:
            with self.subTest(seed=bad), self.assertRaises(ValueError):
                retry_repair_proposal(data, bad, ['B'], cfg, ['daytime'])

    def test_class_opt_out_still_preserves_teacher_and_room_constraints(self):
        for shared_teacher in (False, True):
            data, seed, cfg = setup_seed()
            a = data.courses['A']
            data.courses['B'] = replace(data.courses['B'], prefer=a.prefer,
                                        teachers=a.teachers if shared_teacher else data.courses['B'].teachers,
                                        explicit_rooms=frozenset({'R2'}) if shared_teacher else frozenset())
            if shared_teacher:
                data.rooms['R2'] = room('R2')
            result = retry_repair_proposal(data, seed, ['B'], cfg, ['daytime'])
            self.assertEqual(result['summary']['inserted'], 1)
            self.assertNotEqual(next(r for r in result['results'] if r['course_id'] == 'B')['status'], 'placed')
            self.assertEqual(result['placements'], seed['placements'])

    def test_target_with_baseline_assignment_cannot_be_retried_even_if_result_claims_failure(self):
        data, seed, cfg = setup_seed()
        bad = deepcopy(seed)
        bad['results'].append({'course_id': 'OLD', 'status': 'not_found_within_limits'})
        bad['summary']['requested'] += 1
        with self.assertRaises(ValueError):
            retry_repair_proposal(data, bad, ['OLD'], cfg, ['daytime'])


if __name__ == '__main__':
    unittest.main()
