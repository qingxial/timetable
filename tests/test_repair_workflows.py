from copy import deepcopy
from dataclasses import asdict
import unittest
from test_repair_courses import course, room, placed, dataset, policy
from scripts.repair_courses import repair, week_load, Placement
from scripts.repair_workflows import apply_data_corrections, repair_with_fallbacks


class WorkflowTests(unittest.TestCase):
    def test_weekend_needs_configured_days_and_keeps_hard_bans(self):
        c=course('C',prefer='周一(1-2节)',unavailable='周一全天;周二全天;周三全天;周四全天;周五全天;周六全天')
        d=dataset([c])
        base=dict(allowed_changes=['time_preference'],days=[5,6],periods=[1,2])
        self.assertEqual(repair(d,['C'],{**base,'time_scope':'weekdays'})['summary']['inserted'],0)
        p=repair(d,['C'],{**base,'time_scope':'configured_days'})
        self.assertEqual(p['summary']['inserted'],1)
        self.assertEqual(p['changes'][0]['after'][0]['day'],6)

    def test_fallback_only_adds_remaining_and_preserves_day_insertions(self):
        a=course('A',prefer='周一(1-2节)')
        b=course('B',prefer='周一(1-2节)',unavailable='周一全天;周二全天;周三全天;周四全天;周五全天')
        p=repair_with_fallbacks(dataset([a,b]),['A','B'],{'allowed_changes':['time_preference'],
            'time_scope':'configured_days','days':list(range(7)),'periods':list(range(1,12))})
        self.assertEqual(p['summary']['inserted'],2)
        changes={r['course_id']:r for r in p['changes']}
        self.assertEqual(changes['A']['fallback_stage'],'daytime')
        self.assertEqual(changes['A']['after'][0]['day'],0)
        self.assertEqual(changes['B']['fallback_stage'],'weekend')
        self.assertTrue(p['validation']['prior_placements_unchanged'])
        self.assertEqual(p['workflow']['stages'][1]['summary']['requested'],1)

    def test_hours_correction_preserves_source_and_all_weeks_total(self):
        c=course('C',hours=3.5,total_hours=7)
        d=dataset([c]);original=deepcopy(d)
        corrected,audit=apply_data_corrections(d,[{'course_id':'C','operation':'redistribute_hours',
          'expected_weekly_hours':3.5,'expected_total_hours':7,'authoritative_field':'LLXS',
          'strategy':'balanced_frontload','evidence':'Use LLXS as the explicit planning authority'}])
        p=repair(corrected,['C'],{'allowed_changes':[]})
        self.assertEqual(d,original)
        self.assertEqual(p['summary']['inserted'],1)
        es=[Placement(e['course_id'],frozenset(e['weeks']),e['day'],tuple(e['periods']),e['room_id']) for e in p['placements']]
        self.assertEqual(week_load(es),{1:4,2:3})
        self.assertEqual(sum(week_load(es).values()),7)
        self.assertIn('corrections',p['source_hashes'])
        self.assertEqual(audit[0]['after']['total_hours'],7)

    def test_corrections_reject_stale_values_loss_of_weeks_and_unfrozen_enrollment(self):
        d=dataset([course('C',hours=2,total_hours=4)])
        cases=[{'operation':'redistribute_hours','expected_weekly_hours':3,'expected_total_hours':4,'authoritative_field':'LLXS','strategy':'balanced_frontload'},
               {'operation':'redistribute_hours','expected_weekly_hours':2,'expected_total_hours':4,'authoritative_field':'LLXS','weekly_loads':{'1':4}},
               {'operation':'set_capacity','expected_capacity':30,'capacity':20,'enrollment_frozen':False}]
        for x in cases:
            with self.subTest(case=x),self.assertRaises(ValueError):
                apply_data_corrections(d,[{'course_id':'C','evidence':'test',**x}])

    def test_class_mapping_must_match_real_registry(self):
        c=course('C',classes=frozenset(),issues=['coverage: missing class identifiers','metadata: another issue'])
        d=dataset([c]);d.class_registry=frozenset({'REAL'})
        action={'course_id':'C','operation':'replace_classes','expected_classes':[], 'classes':['REAL'],'evidence':'Verified source mapping'}
        new,audit=apply_data_corrections(d,[action])
        self.assertEqual(new.courses['C'].classes,frozenset({'REAL'}))
        self.assertEqual(new.courses['C'].issues,['metadata: another issue'])
        action['classes']=['MADE_UP']
        with self.assertRaises(ValueError):apply_data_corrections(d,[action])

    def test_quarantine_requires_complete_valid_hours_and_preserves_valid_records(self):
        c=course('C',total_hours=4)
        valid=placed('C');bad=placed('C',weeks=frozenset())
        d=dataset([c],[valid,bad])
        action={'course_id':'C','operation':'quarantine_incomplete_segments','expected_incomplete_count':1,'evidence':'Complete valid records meet unchanged LLXS'}
        new,audit=apply_data_corrections(d,[action])
        self.assertEqual(new.placements,[valid]);self.assertEqual(d.placements,[valid,bad])
        d.placements=[bad]
        with self.assertRaises(ValueError):apply_data_corrections(d,[action])

    def test_weekly_plan_does_not_silently_shorten_explicit_template(self):
        c=course('C',hours=4,total_hours=7,block_template=(2,2))
        with self.assertRaises(ValueError):
            apply_data_corrections(dataset([c]),[{'course_id':'C','operation':'redistribute_hours',
              'expected_weekly_hours':4,'expected_total_hours':7,'authoritative_field':'LLXS',
              'strategy':'balanced_frontload','evidence':'test'}])

    def test_fallback_never_broadens_caller_domain_or_strict_preference(self):
        c=course('C',prefer='周一(1-2节)')
        p=repair_with_fallbacks(dataset([c]),['C'],{'allowed_changes':['time_preference'],
             'days':[4],'periods':[7,8],'time_scope':'strict'})
        self.assertEqual(p['summary']['inserted'],0)

    def test_weekdays_scope_also_applies_without_a_preference(self):
        p=repair(dataset([course('C')]),['C'],{'allowed_changes':[], 'days':[6], 'time_scope':'weekdays'})
        self.assertEqual(p['summary']['inserted'],0)

    def test_chained_corrections_keep_distinct_effective_data_identities(self):
        d=dataset([course('C')]);d.class_registry=frozenset({'REAL'})
        hashes=[]
        for capacity in [20,25]:
            first,_=apply_data_corrections(d,[{'course_id':'C','operation':'set_capacity','expected_capacity':30,
              'capacity':capacity,'enrollment_frozen':True,'evidence':'frozen'}])
            second,_=apply_data_corrections(first,[{'course_id':'C','operation':'replace_classes',
              'expected_classes':['G_C'],'classes':['REAL'],'evidence':'mapping'}])
            hashes.append(second.source_hashes['corrections'])
        self.assertNotEqual(*hashes)

    def test_one_off_week_requires_confirmation_and_preserves_total(self):
        d=dataset([course('C',hours=0,total_hours=2)])
        action={'course_id':'C','operation':'set_one_off_week','expected_weekly_hours':0,
          'expected_total_hours':2,'teaching_week':2,'weeks_confirmed':True,'evidence':'Verified week 2 one-off teaching decision'}
        corrected,audit=apply_data_corrections(d,[action])
        p=repair(corrected,['C'],{'allowed_changes':[]})
        self.assertEqual(p['summary']['inserted'],1)
        self.assertEqual(corrected.courses['C'].weekly_loads,{2:2})
        self.assertEqual(d.courses['C'].weeks,frozenset({1,2}))
        self.assertEqual(audit[0]['after']['total_hours'],2)
        for key,value in [('weeks_confirmed',False),('teaching_week',3),('expected_total_hours',4)]:
            with self.subTest(key=key),self.assertRaises(ValueError):
                apply_data_corrections(d,[{**action,key:value}])

    def test_virtual_class_cannot_duplicate_inherited_parent_hours(self):
        d=dataset([course('PARENT@W',hours=2,total_hours=8)])
        with self.assertRaisesRegex(ValueError,'joint parent-course'):
            apply_data_corrections(d,[{'course_id':'PARENT@W','operation':'redistribute_hours',
              'expected_weekly_hours':2,'expected_total_hours':8,'authoritative_field':'LLXS',
              'strategy':'balanced_frontload','evidence':'Inherited parent LLXS is not a child entitlement'}])

if __name__=='__main__':unittest.main()
