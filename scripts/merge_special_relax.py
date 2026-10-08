# -*- coding: utf-8 -*-
"""把特殊要求放宽排入的课(桶A基类 + 桶B @FA/@FB分段)合并进最终课表，更新失败清单。"""
import os, sys, pickle
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import pandas as pd

RES = '排课结果'
CONV = '智能排课基础数据/提取的基础数据表_converted'
OVERALL = ['教学班ID', '课程名称', '开课院系', '教师', '教师号', '周学时', '课容量', '教室', '教室代码',
           '校区', '班级信息', '偏好上课时间', '避免排课时间', '上课周次', '星期', '节次']


def build_special_rows(det, cdf, sp, teacher_names):
    """Build export rows without I/O; bucket B must retain its segment weeks."""
    meta = {str(r['JXBID']): r for _, r in cdf.iterrows()}
    # @FA/@FB 段周次
    seg_masks = {}
    for _, r in sp.iterrows():
        jx = str(r['JXBID'])
        if jx.endswith('@FA') or jx.endswith('@FB'):
            key = tuple(jx.rsplit('@', 1))
            seg_masks.setdefault(key, set()).add(str(r.get('SKZCDM', '')).strip())

    def g(m, k):
        try:
            return m[k]
        except (KeyError, TypeError):
            return ''

    def build(x):
        b = str(x['教学班ID']); m = meta.get(b, {})
        wk = g(m, 'SKZCMC')
        if x['放宽桶'] == 'B':
            segment = str(x.get('虚班', '')).strip()
            if segment.startswith('@'):
                segment = segment[1:]
            masks = seg_masks.get((b, segment), set())
            if segment not in ('FA', 'FB') or len(masks) != 1:
                raise ValueError(f'桶B分段周次缺失或不唯一: {b}@{segment}')
            mask = next(iter(masks))
            if not mask or set(mask) - {'0', '1'} or '1' not in mask:
                raise ValueError(f'桶B分段周次无效或为空: {b}@{segment}')
            wk = ','.join(str(i + 1) for i, ch in enumerate(mask) if ch == '1')
        return {'教学班ID': b, '课程名称': x['课程名称'], '开课院系': g(m, 'YXMC'),
                '教师': teacher_names.get(str(x['教师号']), ''), '教师号': x['教师号'], '周学时': x['周学时'],
                '课容量': g(m, 'KRL'), '教室': x['教室'], '教室代码': x['教室代码'], '校区': g(m, 'SKXQ'),
                '班级信息': g(m, 'TJBJ'), '偏好上课时间': g(m, 'Prefer_Time'), '避免排课时间': g(m, 'unavailable_Time'),
                '上课周次': wk, '星期': x['星期'], '节次': x['节次']}

    return pd.DataFrame([build(x) for _, x in det.iterrows()], columns=OVERALL)


def main():
    sys.path.insert(0, ROOT)
    os.chdir(ROOT)
    det = pd.read_excel(f'{RES}/特殊要求放宽排入明细.xlsx', dtype=str).fillna('')
    final = pd.read_excel(f'{RES}/调课后的整体结果_小数拆分.xlsx', dtype=str)
    cdf = pd.read_excel(f'{CONV}/课程表_split_merged.xlsx', dtype=str, header=1)
    sp = pd.read_excel(f'{CONV}/课程表_frac_split.xlsx', dtype=str, header=1)
    with open(f'{RES}/reschedule_timetables_special.pkl', 'rb') as handle:
        xm = {str(c.JSH): c.XM for c in pickle.load(handle)['courses']}
    new_rows = build_special_rows(det, cdf, sp, xm)
    placed_ids = set(det['教学班ID'].astype(str))
    kept = final[~final['教学班ID'].astype(str).isin(placed_ids)]
    merged = pd.concat([kept, new_rows[OVERALL]], ignore_index=True)
    out = f'{RES}/调课后的整体结果_特殊放宽.xlsx'
    merged.to_excel(out, index=False)

    fail = pd.read_excel(f'{RES}/调课失败的排课失败课程.xlsx', dtype=str)
    still = fail[~fail['jxbid'].astype(str).isin(placed_ids)]
    still.to_excel(f'{RES}/调课失败的排课失败课程_特殊放宽后.xlsx', index=False)

    b0, b1 = final['教学班ID'].nunique(), merged['教学班ID'].nunique()
    print(f'最终课表: {out}')
    print(f'  教学班: {b0} → {b1}  (+{b1 - b0})   {b1}/4666 = {b1/4666*100:.2f}%')
    print(f'  失败清单: {len(fail)} → {len(still)}')


if __name__ == '__main__':
    main()
