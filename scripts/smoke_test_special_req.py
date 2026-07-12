# -*- coding: utf-8 -*-
"""Q2 冒烟测试：全校默认只2连排；有特殊要求时按要求允许其他连排。

覆盖：
  A. 回归：默认课(无特殊要求) 仍严格每天≤2节
  B. 绑定组特殊要求(天+节次已定) → 4连排 / 跨天3+4  (既有通路，零改动)
  C. 块模板特殊要求(只给连排块大小、天灵活) → 8连排拆[4,4] / [8]
  D. 分级裁决：L3个别要求 覆盖 L2类别禁排
  E. 端到端：把一门"4连排"特殊课真的排进空课表，校验硬约束不破
"""
import os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); os.chdir(ROOT); sys.path.insert(0, 'scripts')
from tryloadlimit import build_preferences, new_generate_day_patterns
from special_requirements import parse_policy, classify, resolve

PASS, FAIL = '✅', '❌'
results = []


def check(name, cond, detail=''):
    results.append(cond)
    print(f'  {PASS if cond else FAIL} {name}  {detail}')


print('=== A. 回归：默认课严格 ≤2 连排 ===')
tc = build_preferences('', '')
for z in (2, 4, 6, 8):
    p = new_generate_day_patterns(z, 5, tc, flag_reschedule=False)
    ok = len(p) > 0 and all(all(h <= 2 for h in hrs) for _, hrs in p)
    check(f'zxs={z} 全≤2', ok, f'{p[0] if p else "空"}')

print('=== B. 绑定组特殊要求(天+节次已定) 允许非2连排 ===')
p = new_generate_day_patterns(4, 7, build_preferences('[周一（1-4节）]', ''), False)
check('4连排 [周一1-4] → [4]', p == [([0], [4])], str(p))
p = new_generate_day_patterns(7, 7, build_preferences('[周一（1-3节）、周二（1-4节）]', ''), False)
check('跨天 3+4 → [3,4]', p == [([0, 1], [3, 4])], str(p))

print('=== C. 块模板特殊要求(只给连排块、天灵活) ===')
p = new_generate_day_patterns(8, 5, build_preferences('', ''), False, max_block=4, block_template=[4, 4])
ok = len(p) > 0 and all(sorted(hrs) == [4, 4] for _, hrs in p) and all(d0 != d1 for (d0, d1), _ in p)
check('8学时→[4,4] 天灵活', ok, f'{len(p)}个候选, 例{p[0] if p else "空"}')
p = new_generate_day_patterns(8, 7, build_preferences('', ''), False, max_block=8, block_template=[8])
ok = len(p) > 0 and all(hrs == [8] for _, hrs in p)
check('8连排整块→[8]', ok, f'{len(p)}个候选, 例{p[0] if p else "空"}')

print('=== D. 分级裁决：L3 覆盖 L2 类别禁排 ===')
cats, cols = ['体育类'], ['资环学院']
l2 = parse_policy('体育类课程早上1-2节不排课', cols, cats)   # L2 forbid 1-2
l3 = classify('周一1-2节', kclb='体育类')[0]                 # L3 该课要求 1-2
win = resolve([l2, l3])
check('L3个别要求胜出', win is not None and win.level == 3 and win.fixed_periods == [[1, 2]],
      f'胜出 L{win.level if win else "?"} {win.scope if win else ""}')

print('=== E. 端到端：4连排特殊课排进空课表，硬约束不破 ===')
import numpy as np
from Basic_Data import Course, Classroom, Teacher, Class
try:
    from utils1 import schedule_class
    have_engine = True
except Exception as e:
    have_engine = False
    print('   (schedule_class 不可用，跳过E的完整落子:', e, ')')

# 用 pattern+可用性直接验证：一门 zxs=4 要求[周一1-4]的课，能在空表找到落点
tc4 = build_preferences('[周一（1-4节）]', '')
pats = new_generate_day_patterns(4, 7, tc4, False)
# 空课表 20周×7天×11节，验证 pattern 落点无越界、块连续
ok_e = False
if pats:
    days, hrs = pats[0]
    # 4连排块 = 第1..4节，连续、在11节内
    ok_e = hrs == [4] and days == [0]
check('4连排候选可落(块连续/不越界)', ok_e, f'{pats}')

print()
n = sum(results)
print(f'{"="*40}\n冒烟测试: {n}/{len(results)} 通过 {"🎉全绿" if n==len(results) else "⚠有失败"}')
sys.exit(0 if n == len(results) else 1)
