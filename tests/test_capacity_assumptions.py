"""Capacity scenarios are explicit assumptions, not confirmed enrollment facts."""
from copy import deepcopy
from dataclasses import asdict
import unittest

from test_repair_courses import course, dataset, placed, policy, room
from scripts.repair_courses import repair, week_load, Placement
from scripts.repair_workflows import apply_data_corrections


def assumption(**changes):
    values = {
        'course_id': 'C', 'operation': 'set_capacity_assumption',
        'evidence': 'Try the source actual count as an unverified scenario',
        'expected_capacity': 80.0, 'capacity': 40, 'assumption_only': True,
    }
    values.update(changes)
    return values


def confirmed(**changes):
    values = {
        'course_id': 'C', 'operation': 'set_capacity',
        'evidence': 'Confirmed frozen source enrollment',
        'expected_capacity': 80.0, 'capacity': 40, 'enrollment_frozen': True,
    }
    values.update(changes)
    return values


class CapacityAssumptionTests(unittest.TestCase):
    def setUp(self):
        self.data = dataset([course('C', capacity=80.0, total_hours=4)])
        self.data.source_hashes = {'courses': 'unchanged-course-source', 'rooms': 'unchanged-room-source'}

    def test_explicit_scenario_changes_only_demand_and_audits_uncertainty(self):
        original = deepcopy(self.data)
        changed, audits = apply_data_corrections(self.data, [assumption()])
        self.assertEqual(self.data, original)
        expected_course = asdict(original.courses['C'])
        expected_course['capacity'] = 40.0
        self.assertEqual(asdict(changed.courses['C']), expected_course)
        self.assertEqual(changed.rooms, original.rooms)
        self.assertEqual(changed.placements, original.placements)
        self.assertEqual(changed.issues, original.issues)
        self.assertEqual(changed.class_registry, original.class_registry)
        for key, value in original.source_hashes.items():
            self.assertEqual(changed.source_hashes[key], value)
        self.assertIn('corrections', changed.source_hashes)
        audit = audits[0]
        self.assertEqual(audit['before'], {'capacity': 80.0})
        self.assertEqual(audit['after'], {'capacity': 40})
        self.assertEqual(audit['basis'], 'unverified_capacity_scenario')
        self.assertIs(audit['requires_enrollment_confirmation'], True)
        self.assertIs(audit['source_files_modified'], False)
        self.assertIn('not confirmed frozen', audit['interpretation'])

    def test_assumption_confirmation_requires_literal_true(self):
        for value in [False, 1, 1.0, 'true', None]:
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'assumption_only=true'):
                apply_data_corrections(self.data, [assumption(assumption_only=value)])
        missing = assumption()
        del missing['assumption_only']
        with self.assertRaisesRegex(ValueError, 'assumption_only=true'):
            apply_data_corrections(self.data, [missing])

    def test_capacity_requires_positive_finite_number(self):
        for value in [0, -1, True, '40', float('nan'), float('inf')]:
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'positive and finite'):
                apply_data_corrections(self.data, [assumption(capacity=value)])

    def test_original_capacity_precondition_is_enforced(self):
        for value in [79, True, '80', float('nan')]:
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'precondition changed'):
                apply_data_corrections(self.data, [assumption(expected_capacity=value)])

    def test_scheduled_course_cannot_change_capacity_basis(self):
        self.data.placements = [placed('C')]
        with self.assertRaisesRegex(ValueError, 'unscheduled'):
            apply_data_corrections(self.data, [assumption()])

    def test_assumption_and_confirmed_corrections_cannot_mix_in_either_order(self):
        sequences = [
            [assumption(), confirmed(expected_capacity=40, capacity=30)],
            [confirmed(), assumption(expected_capacity=40, capacity=30)],
        ]
        original = deepcopy(self.data)
        for corrections in sequences:
            with self.subTest(first=corrections[0]['operation']), self.assertRaisesRegex(ValueError, 'cannot be mixed'):
                apply_data_corrections(self.data, corrections)
            self.assertEqual(self.data, original)

    def test_different_courses_can_use_different_capacity_bases(self):
        self.data.courses['B'] = course('B', capacity=80.0)
        changed, audits = apply_data_corrections(self.data, [assumption(), confirmed(course_id='B')])
        self.assertEqual(changed.courses['C'].capacity, 40)
        self.assertEqual(changed.courses['B'].capacity, 40)
        self.assertIn('basis', audits[0])
        self.assertNotIn('basis', audits[1])

    def test_duplicate_assumptions_and_missing_evidence_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Duplicate correction'):
            apply_data_corrections(self.data, [assumption(), assumption(expected_capacity=40)])
        with self.assertRaisesRegex(ValueError, 'explicit evidence'):
            apply_data_corrections(self.data, [assumption(evidence=' ')])

    def test_old_confirmed_capacity_still_rejects_unfrozen_enrollment(self):
        with self.assertRaisesRegex(ValueError, 'confirmed frozen'):
            apply_data_corrections(self.data, [confirmed(enrollment_frozen=False)])

    def test_old_confirmed_audit_and_hash_remain_compatible_with_saved_seeds(self):
        changed, audits = apply_data_corrections(self.data, [confirmed()])
        self.assertEqual(audits, [{
            'course_id': 'C', 'operation': 'set_capacity',
            'evidence': 'Confirmed frozen source enrollment', 'source_files_modified': False,
            'before': {'capacity': 80.0}, 'after': {'capacity': 40},
            'interpretation': 'Capacity demand corrected using explicitly confirmed enrollment; room capacities stay fixed.',
        }])
        # Captured before adding scenario support; old seed identities must not change.
        self.assertEqual(changed.source_hashes['corrections'],
                         '55af4522fa45357648bca9eea3396dd2c581edc3ac8a1422d4b9c3aa78d8d8b7')

    def test_capacity_assumption_enables_full_synthetic_schedule_without_source_edits(self):
        strict = repair(self.data, ['C'], policy())
        self.assertEqual(strict['results'][0]['status'], 'no_matching_room')
        changed, _ = apply_data_corrections(self.data, [assumption()])
        proposed = repair(changed, ['C'], policy())
        self.assertEqual(proposed['summary']['inserted'], 1)
        self.assertEqual(proposed['summary']['moved'], 0)
        self.assertEqual(proposed['validation']['new_conflicts'], 0)
        entries = [Placement(p['course_id'], frozenset(p['weeks']), p['day'], tuple(p['periods']), p['room_id'])
                   for p in proposed['placements']]
        self.assertEqual(week_load(entries), {1: 2, 2: 2})
        self.assertEqual(self.data.courses['C'].capacity, 80.0)
        self.assertEqual(self.data.rooms['R1'].capacity, 50.0)

    def test_assumption_does_not_release_room_type_campus_or_time_constraints(self):
        for replacement in [room(type='OTHER'), room(campus='OTHER')]:
            with self.subTest(room=replacement):
                source = dataset([course('C', capacity=80.0)], rooms=[replacement])
                changed, _ = apply_data_corrections(source, [assumption()])
                self.assertEqual(repair(changed, ['C'], policy())['summary']['inserted'], 0)
        source = dataset([course('C', capacity=80.0, unavailable='周一全天')])
        changed, _ = apply_data_corrections(source, [assumption()])
        self.assertEqual(repair(changed, ['C'], policy())['summary']['inserted'], 0)


if __name__ == '__main__':
    unittest.main()
