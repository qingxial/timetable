"""Synthetic regression coverage for fractional segment export weeks."""
from pathlib import Path
import re
import sys
import unittest

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.merge_special_relax import build_special_rows


def detail(segment='FA', **changes):
    row = {'教学班ID': 'C1', '虚班': segment, '课程名称': 'Example',
           '教师号': 'T1', '周学时': '4', '教室代码': 'R1', '教室': 'Room',
           '星期': '周一', '节次': '第1-4节', '放宽桶': 'B'}
    row.update(changes)
    return row


class SegmentExportTests(unittest.TestCase):
    def setUp(self):
        # The parent spans six weeks. Neither segment may inherit that range.
        self.courses = pd.DataFrame([{'JXBID': 'C1', 'SKZCMC': '1,2,3,4,5,6'}])
        self.segments = pd.DataFrame([
            {'JXBID': 'C1@FA', 'SKZCDM': '110000'},
            {'JXBID': 'C1@FB', 'SKZCDM': '001111'},
        ])

    def test_each_segment_keeps_its_own_weeks_and_total_hours(self):
        details = pd.DataFrame([
            detail('FA'),
            detail('FB', **{'周学时': '2', '节次': '第1-2节'}),
        ])
        exported = build_special_rows(details, self.courses, self.segments, {})
        self.assertEqual(exported['上课周次'].tolist(), ['1,2', '3,4,5,6'])
        total = 0
        for _, row in exported.iterrows():
            start, end = map(int, re.fullmatch(r'第(\d+)-(\d+)节', row['节次']).groups())
            total += len(row['上课周次'].split(',')) * (end - start + 1)
        self.assertEqual(total, 16)  # 2 weeks * 4 periods + 4 weeks * 2 periods

    def test_optional_at_prefix_uses_the_same_segment_key(self):
        exported = build_special_rows(pd.DataFrame([detail('@FA')]),
                                      self.courses, self.segments, {})
        self.assertEqual(exported.iloc[0]['上课周次'], '1,2')

    def test_unknown_or_missing_segment_fails_instead_of_using_parent_weeks(self):
        for segment in ('FC', '', 'FA'):
            with self.subTest(segment=segment), self.assertRaises(ValueError):
                segments = self.segments if segment != 'FA' else self.segments.iloc[1:]
                build_special_rows(pd.DataFrame([detail(segment)]), self.courses, segments, {})

    def test_empty_invalid_or_conflicting_masks_fail(self):
        for mask in ('', '000000', '110x00', float('nan')):
            with self.subTest(mask=mask), self.assertRaises(ValueError):
                segments = pd.DataFrame([{'JXBID': 'C1@FA', 'SKZCDM': mask}])
                build_special_rows(pd.DataFrame([detail()]), self.courses, segments, {})
        conflicting = pd.concat([self.segments,
                                 pd.DataFrame([{'JXBID': 'C1@FA', 'SKZCDM': '111000'}])])
        with self.assertRaises(ValueError):
            build_special_rows(pd.DataFrame([detail()]), self.courses, conflicting, {})

    def test_bucket_a_retains_parent_weeks(self):
        exported = build_special_rows(pd.DataFrame([detail('', **{'放宽桶': 'A'})]),
                                      self.courses, self.segments, {})
        self.assertEqual(exported.iloc[0]['上课周次'], '1,2,3,4,5,6')


if __name__ == '__main__':
    unittest.main()
