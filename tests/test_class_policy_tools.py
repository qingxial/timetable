"""Explicit class-policy exceptions and seed retry safety, using synthetic inputs."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from test_repair_courses import course, dataset, placed, policy, room
from test_timetable_agent_tools import workbook, write_json
from scripts.repair_courses import repair
from scripts.repair_workflows import apply_data_corrections
from scripts.timetable_agent_tools import TOOL_SCHEMAS, dispatch_tool, _validate


def exception(cid='TARGET', expected=True):
    return {'course_id': cid, 'operation': 'set_class_conflict_check',
            'expected_check_class': expected, 'check_class': False,
            'evidence': 'Explicit synthetic decision to exempt this exact course from class conflicts'}


class ClassExceptionSafetyTests(unittest.TestCase):
    def test_only_four_known_class_coverage_issues_are_removed(self):
        removable = ['coverage: missing class identifiers',
            "coverage: generic class scopes cannot identify student conflicts: ['全校']",
            "coverage: unresolved class identifiers: ['COLLEGE']",
            "coverage: class identifiers absent from BJMC registry: ['UNKNOWN']"]
        keep = ['metadata: teacher record invalid', 'coverage: teacher coverage incomplete',
                'constraint: malformed unavailable_Time']
        c = course('TARGET', classes=frozenset({'RAW_CLASS'}), issues=removable + keep,
                   total_hours=4)
        d = dataset([c]); original = deepcopy(d)
        corrected, audit = apply_data_corrections(d, [exception()])
        changed = corrected.courses['TARGET']
        self.assertEqual(d, original)
        self.assertFalse(changed.check_class)
        self.assertEqual(changed.issues, keep)
        for field in ('classes', 'teachers', 'weeks', 'capacity', 'total_hours', 'hours', 'check_room'):
            self.assertEqual(getattr(changed, field), getattr(c, field), field)
        self.assertEqual(len(audit), 1)
        self.assertTrue(audit[0]['before']['check_class'])
        self.assertFalse(audit[0]['after']['check_class'])
        self.assertNotEqual(corrected.source_hashes, d.source_hashes)

    def test_explicit_audited_noop_clears_coverage_without_changing_class_tokens(self):
        c = course('TARGET', check_class=False, issues=['coverage: missing class identifiers'])
        d = dataset([c])
        corrected, audit = apply_data_corrections(d, [exception(expected=False)])
        self.assertFalse(corrected.courses['TARGET'].check_class)
        self.assertEqual(corrected.courses['TARGET'].issues, [])
        self.assertEqual(corrected.courses['TARGET'].classes, c.classes)
        self.assertFalse(audit[0]['before']['check_class'])
        self.assertFalse(audit[0]['after']['check_class'])
        self.assertIn('corrections', corrected.source_hashes)

    def test_strict_boolean_preconditions_and_scheduled_courses_are_protected(self):
        d = dataset([course('TARGET')])
        cases = [dict(expected_check_class=False), dict(expected_check_class=1),
                 dict(expected_check_class='true'), dict(check_class=0),
                 dict(check_class='false'), dict(check_class=True), dict(evidence='')]
        for changed in cases:
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                apply_data_corrections(d, [{**exception(), **changed}])
        with self.assertRaises(ValueError):
            apply_data_corrections(dataset([course('TARGET')], [placed('TARGET')]), [exception()])

    def test_class_bypass_leaves_teacher_and_room_collisions_blocked(self):
        for resource in ('teacher', 'room'):
            with self.subTest(resource=resource):
                baseline = course('BASE')
                target = course('TARGET', classes=baseline.classes,
                    teachers=baseline.teachers if resource == 'teacher' else {'T_TARGET': baseline.weeks},
                    explicit_rooms=frozenset({'R2' if resource == 'teacher' else 'R1'}), total_hours=4)
                d = dataset([baseline, target], [placed('BASE')], [room(), room('R2')])
                corrected, _ = apply_data_corrections(d, [exception()])
                result = repair(corrected, ['TARGET'], policy(allowed_changes=[], periods=[1, 2]))
                self.assertEqual(result['summary']['inserted'], 0, result)
                self.assertEqual(result['summary']['moved'], 0)

    def test_class_bypass_allows_only_the_authorized_course_overlap(self):
        baseline = course('BASE')
        target = course('TARGET', classes=baseline.classes, explicit_rooms=frozenset({'R2'}), total_hours=4)
        other = course('OTHER', classes=baseline.classes, explicit_rooms=frozenset({'R2'}), total_hours=4)
        d = dataset([baseline, target, other], [placed('BASE')], [room(), room('R2')])
        corrected, _ = apply_data_corrections(d, [exception()])
        result = repair(corrected, ['TARGET', 'OTHER'], policy(allowed_changes=[], periods=[1, 2]))
        outcomes = {r['course_id']: r['status'] for r in result['results']}
        self.assertEqual(outcomes['TARGET'], 'placed')
        self.assertNotEqual(outcomes['OTHER'], 'placed')
        self.assertTrue(corrected.courses['OTHER'].check_class)


def inputs(root, target_flag=0):
    columns = ['JXBID','KCM','ZXS','LLXS','KRL','SKXQ','SKZCDM','JSH','RWJSZCDM',
               'TJBJ','IF_CLASS_CONFICT','IF_ROOM_CONFICT','Prefer_Time']
    rows = []
    for cid in ('BASE','GOOD','TARGET','OTHER','EXTERNAL'):
        rows.append([cid, 'Synthetic '+cid, 2, 4, 30, 'CAMPUS', '11', 'T_'+cid, '11',
                     '全校' if cid in ('TARGET','OTHER') else ('CLASS02' if cid == 'GOOD' else 'CLASS01'),
                     target_flag if cid == 'TARGET' else 0, 0,
                     '周二(1-2节)' if cid == 'TARGET' else ''])
    workbook(root/'courses.xlsx', columns, rows, True)
    workbook(root/'rooms.xlsx', ['JASDM','JASMC','MC','SKZWS','SFYXPK'],
             [['R1','Room 1','CAMPUS',40,1],['R2','Room 2','CAMPUS',40,1]], True)
    workbook(root/'schedule.xlsx', ['教学班ID','教室代码','上课周次','星期','节次','周学时'],
             [['BASE','R1','1,2','周一','1-2',2]])
    workbook(root/'failed.xlsx', ['jxbid'], [['GOOD'],['TARGET'],['OTHER']])
    workbook(root/'classes.xlsx', ['BJMC'], [['CLASS01'], ['CLASS02']])
    return {'course_path':str(root/'courses.xlsx'), 'room_path':str(root/'rooms.xlsx'),
            'schedule_path':str(root/'schedule.xlsx'), 'failed_path':str(root/'failed.xlsx'),
            'class_path':str(root/'classes.xlsx')}


class ClassPolicyRetryToolTests(unittest.TestCase):
    def assert_ok(self, name, arguments):
        schema = next(s['parameters'] for s in TOOL_SCHEMAS if s['name'] == name)
        _validate(arguments, schema)
        response = dispatch_tool(name, arguments)
        self.assertTrue(response['ok'], response)
        self.assertTrue(response['result']['source_files_unchanged'])
        return response

    def seed(self, root, target_flag=0):
        paths = inputs(root, target_flag)
        config = {'allowed_changes':[], 'time_scope':'configured_days', 'days':[0,1],
                  'periods':[1,2], 'max_moved_courses':0, 'max_candidates_per_course':100,
                  'max_search_nodes':10000, 'time_limit_seconds':5}
        prefix = [{'course_id':'GOOD','operation':'set_capacity','expected_capacity':30,
                   'capacity':35,'enrollment_frozen':True,'evidence':'Synthetic frozen enrollment'}]
        seed = self.assert_ok('repair_with_fallbacks', {**paths,'config':{**config, 'days':[0]},
            'corrections':prefix,'stages':['daytime'],'total_time_limit_seconds':5})
        self.assertEqual(seed['result']['proposal']['summary']['inserted'], 1)
        seed_path = write_json(root/'seed.json', seed)
        request = {**paths, 'seed_proposal_path':seed_path, 'target_ids':['TARGET'],
                   'corrections':prefix+[exception(expected=target_flag != 1)],
                   'config':config, 'stages':['daytime'], 'total_time_limit_seconds':5}
        return seed, request

    def test_schema_dispatch_success_preserves_seed_and_scope_with_disclosure(self):
        for source_flag in (0, 1):
            with self.subTest(source_flag=source_flag), TemporaryDirectory() as tmp:
                root = Path(tmp); seed, request = self.seed(root, source_flag)
                before = {p.name:sha256(p.read_bytes()).hexdigest() for p in root.glob('*.xlsx')}
                original_request = deepcopy(request)
                response = self.assert_ok('retry_repair_proposal', request)
                result = response['result']['proposal']; old = seed['result']['proposal']
                self.assertEqual(result['summary']['requested'], 3)
                self.assertEqual(result['summary']['inserted'], 2)
                self.assertEqual(result['summary']['moved'], 0)
                self.assertEqual(result['retry']['newly_inserted'], 1)
                self.assertEqual(result['retry']['retry_targets'], ['TARGET'])
                for cid in ('BASE','GOOD'):
                    self.assertEqual([p for p in old['placements'] if p['course_id']==cid],
                                     [p for p in result['placements'] if p['course_id']==cid])
                self.assertEqual(next(r for r in old['results'] if r['course_id']=='OTHER'),
                                 next(r for r in result['results'] if r['course_id']=='OTHER'))
                target = next(r for r in result['results'] if r['course_id']=='TARGET')
                self.assertFalse(target['class_conflict_check'])
                self.assertFalse(next(c for c in result['changes'] if c['course_id']=='TARGET')['class_conflict_check'])
                self.assertEqual(result['validation']['class_conflict_checks_disabled_for_target_ids'], ['TARGET'])
                self.assertEqual(result['validation']['class_conflict_checks_disabled_for_changed_course_ids'], ['TARGET'])
                self.assertIn('class_conflict_policy', response['result'])
                self.assertNotEqual(old['source_hashes'], result['source_hashes'])
                self.assertEqual(request, original_request)
                self.assertEqual(before, {p.name:sha256(p.read_bytes()).hexdigest() for p in root.glob('*.xlsx')})
                retry_path = write_json(root/'retry.json', response)
                self.assertTrue(dispatch_tool('diagnose_remaining', {'proposal_path':retry_path})['ok'])
                comparison = dispatch_tool('compare_proposals', {'proposal_paths':[request['seed_proposal_path'], retry_path]})
                self.assertFalse(comparison['ok'])
                self.assertEqual(comparison['error']['code'], 'incomparable_proposals')

    def test_retry_rejects_prefix_changes_and_non_target_corrections(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp); _, request = self.seed(root)
            cases = []
            absent = deepcopy(request); absent['corrections'] = absent['corrections'][1:]; cases.append(absent)
            altered = deepcopy(request); altered['corrections'][0]['evidence'] += ' changed'; cases.append(altered)
            outside = deepcopy(request); outside['corrections'].append(exception('OTHER')); cases.append(outside)
            retained = deepcopy(request); retained['corrections'].append(exception('GOOD')); cases.append(retained)
            for args in cases:
                with self.subTest(corrections=args['corrections']):
                    self.assertFalse(dispatch_tool('retry_repair_proposal', args)['ok'])

    def test_retry_rejects_duplicate_empty_success_unknown_and_expanded_targets(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp); _, request = self.seed(root)
            for targets in ([], ['TARGET','TARGET'], ['GOOD'], ['EXTERNAL'], ['ABSENT'], ['BASE']):
                args = deepcopy(request); args['target_ids'] = targets
                with self.subTest(targets=targets):
                    self.assertFalse(dispatch_tool('retry_repair_proposal', args)['ok'])
            args = deepcopy(request); args['config']['target_ids']=['OTHER']
            self.assertFalse(dispatch_tool('retry_repair_proposal', args)['ok'])

    def test_changed_source_and_forged_seed_assignments_are_rejected(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp); seed, request = self.seed(root)
            forged = deepcopy(seed)
            next(p for p in forged['result']['proposal']['placements'] if p['course_id']=='BASE')['day']=1
            request['seed_proposal_path']=write_json(root/'forged.json', forged)
            self.assertFalse(dispatch_tool('retry_repair_proposal', request)['ok'])
            request['seed_proposal_path']=str(root/'seed.json')
            workbook(root/'rooms.xlsx', ['JASDM','JASMC','MC','SKZWS','SFYXPK'],
                     [['R1','Room 1','CAMPUS',41,1],['R2','Room 2','CAMPUS',40,1]], True)
            self.assertFalse(dispatch_tool('retry_repair_proposal', request)['ok'])

    def test_wrapper_rejects_nonboolean_or_missing_exception_authorization(self):
        with TemporaryDirectory() as tmp:
            paths = inputs(Path(tmp))
            for changed in ({'expected_check_class':1}, {'check_class':0}, {'check_class':True},
                            {'check_class':'false'}, {'evidence':''}):
                args = {**paths,'corrections':[{**exception(), **changed}]}
                with self.subTest(changed=changed):
                    self.assertFalse(dispatch_tool('preview_data_corrections', args)['ok'])


if __name__ == '__main__':
    unittest.main()
