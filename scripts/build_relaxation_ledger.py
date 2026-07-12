# -*- coding: utf-8 -*-
"""统一软约束松弛总记录（Q3 汇总）——把所有"放宽了什么"的记录合成一张表，方便复用。

汇集三类松弛，统一 schema：
  · 时间偏好偏离  ← 软偏好修复明细.xlsx（体育课被同偏好占死，最小偏离挪位）
  · 类别禁排覆盖  ← 特殊要求放宽记录.xlsx 桶A（L3 个别要求覆盖 L2 类别禁排：删该行禁排段）
  · 连排放宽      ← 特殊要求放宽记录.xlsx 桶B（小数分段 @FA/@FB + 周末 >2 连排）
统一列：教学班ID / 课程名称 / 松弛类型 / 级别取舍 / 详情 / 结果。
"""
import os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); os.chdir(ROOT)
import pandas as pd

RES = '排课结果'
COLS = ['教学班ID', '课程名称', '松弛类型', '级别取舍', '详情', '结果']


def rd(p):
    return pd.read_excel(p, dtype=str).fillna('') if os.path.isfile(p) else None


def main():
    rows = []

    soft = rd(f'{RES}/软偏好修复明细.xlsx')
    if soft is not None:
        lvname = {'1': '同偏好天·换节次', '2': '同偏好节次·换天', '3': '完全另置'}
        for _, r in soft.iterrows():
            rows.append({'教学班ID': r['教学班ID'], '课程名称': r['课程名称'], '松弛类型': '时间偏好偏离',
                         '级别取舍': '唯一要求·无跨级冲突',
                         '详情': f"偏好{r.get('偏好上课时间','')} → 实排{r.get('星期','')}{r.get('节次','')}"
                                 f"（{lvname.get(str(r.get('软偏好偏离档','')), r.get('软偏好偏离档',''))}）",
                         '结果': '✅已排入'})

    spec = rd(f'{RES}/特殊要求放宽记录.xlsx')
    if spec is not None:
        for _, r in spec.iterrows():
            bucket = str(r.get('放宽桶', ''))
            typ = '类别禁排覆盖(L3>L2)' if bucket == 'A' else ('连排放宽+小数分段' if bucket == 'B' else '特殊放宽')
            tie = 'L3任课/教学班 覆盖 L2类别禁排' if bucket == 'A' else '按周末天数分大块·守恒零残差'
            rows.append({'教学班ID': r['教学班ID'], '课程名称': r.get('课程名称', ''), '松弛类型': typ,
                         '级别取舍': tie, '详情': r.get('放宽内容', ''), '结果': r.get('结果', '')})

    led = pd.DataFrame(rows, columns=COLS)
    out = f'{RES}/软约束松弛总记录.xlsx'
    led.to_excel(out, index=False)
    print(f'✅ {out}  共 {len(led)} 条')
    if len(led):
        print('  按类型:', led['松弛类型'].value_counts().to_dict())
        print('  按结果:', led['结果'].value_counts().to_dict())
    return led


if __name__ == '__main__':
    main()
