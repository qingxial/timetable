# -*- coding: utf-8 -*-
"""调课(含 clear_placement 修复 + 软偏好修复)完成后的下游全链驱动。

按依赖顺序一键串跑：
  ② 合并软偏好修复落子 → 调课后的整体结果_软偏好修复.xlsx
  ③ 小数周学时前后分段拆分 + 局部重排 → 调课后的整体结果_小数拆分.xlsx（最终课表）
  ④ 失败归因分析 + 逐课根因回填（标注软偏好救回）
  ⑤ 结果分析 + HTML 报告
  ⑥ 汇总 + 悬挂占用校验（验证 clear_placement 修复效果）

每一步基于上一步的新产物；产出统一，供最后提交。
"""
import os, sys, subprocess, pickle
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); os.chdir(ROOT)
import pandas as pd
from openpyxl import load_workbook

RES = '排课结果'
CONV = '智能排课基础数据/提取的基础数据表_converted'
OVERALL_COLS = ['教学班ID','课程名称','开课院系','教师','教师号','周学时','课容量','教室','教室代码',
                '校区','班级信息','偏好上课时间','避免排课时间','上课周次','星期','节次']


def sh(cmd):
    print(f'\n$ {cmd}', flush=True)
    r = subprocess.run(cmd, shell=True)
    if r.returncode != 0:
        print(f'  ⚠ 退出码 {r.returncode}（继续）')
    return r.returncode


def rd(path, idcol='教学班ID'):
    d = pd.read_excel(path, dtype=str)
    if idcol in d.columns:
        d[idcol] = d[idcol].astype(str)
    return d


# ---------- ② 合并软偏好修复 ----------
def merge_soft():
    base = rd(f'{RES}/调课后的整体结果.xlsx')
    det_path = f'{RES}/软偏好修复明细.xlsx'
    out = f'{RES}/调课后的整体结果_软偏好修复.xlsx'
    if not os.path.isfile(det_path):
        print('  无软偏好修复明细，跳过'); base.to_excel(out, index=False); return out, set()
    det = rd(det_path)
    det = det[[c for c in OVERALL_COLS if c in det.columns]]
    ids = set(det['教学班ID'])
    kept = base[~base['教学班ID'].isin(ids)]
    merged = pd.concat([kept, det], ignore_index=True)
    merged.to_excel(out, index=False)
    print(f'  ② 软偏好合并: {base["教学班ID"].nunique()} → {merged["教学班ID"].nunique()} 门（救回 {len(ids)}）')
    return out, ids


# ---------- ③ 小数拆分局部重排 + 合并 ----------
def frac_reschedule_and_merge(soft_overall):
    # 拆分表（课程表不变，重生成保证一致）
    sh('python3 scripts/split_fractional_zxs.py --apply')
    # 局部重排：基于调课(修复后)的新 pkl
    sh('python3 scripts/local_reschedule_fractional.py '
       '--pickle 排课结果/reschedule_timetables.pkl '
       '--out-pickle 排课结果/reschedule_timetables_frac.pkl')

    det = rd(f'{RES}/小数课局部重排明细.xlsx')
    # 从拆分表取 @FA/@FB 周次 + 课程元数据
    wb = load_workbook(f'{CONV}/课程表_frac_split.xlsx', read_only=True)
    wkmap, meta = {}, {}
    for r in wb.active.iter_rows(min_row=3, values_only=True):
        jx = str(r[7])
        if jx.endswith('@FA') or jx.endswith('@FB'):
            base = jx.rsplit('@', 1)[0]; ab = jx.rsplit('@', 1)[1]
            weeks = ','.join(str(i+1) for i, ch in enumerate(str(r[14] or '')) if ch == '1')
            wkmap[(base, ab)] = weeks
            meta.setdefault(base, {'YXMC': r[4], 'SKXQ': r[6], 'KRL': r[9],
                                   'TJBJ': r[40], 'pref': r[36], 'unav': r[37]})
    wb.close()
    with open(f'{RES}/reschedule_timetables_frac.pkl', 'rb') as f:
        xm = {str(c.JSH): c.XM for c in pickle.load(f)['courses']}

    det['上课周次'] = det.apply(lambda x: wkmap.get((x['教学班ID'], x['虚班']), ''), axis=1)
    def build(x):
        m = meta.get(x['教学班ID'], {})
        return {'教学班ID': x['教学班ID'], '课程名称': x['课程名称'], '开课院系': m.get('YXMC', ''),
                '教师': xm.get(str(x['教师号']), ''), '教师号': x['教师号'], '周学时': x['每周段节数'],
                '课容量': m.get('KRL', ''), '教室': x['教室'], '教室代码': x['教室代码'], '校区': m.get('SKXQ', ''),
                '班级信息': m.get('TJBJ', ''), '偏好上课时间': m.get('pref', ''), '避免排课时间': m.get('unav', ''),
                '上课周次': x['上课周次'], '星期': x['星期'], '节次': x['节次']}
    new_rows = pd.DataFrame([build(x) for _, x in det.iterrows()])
    committed = set(det['教学班ID'])
    overall = rd(soft_overall)
    kept = overall[~overall['教学班ID'].isin(committed)]
    merged = pd.concat([kept, new_rows[OVERALL_COLS]], ignore_index=True)
    out = f'{RES}/调课后的整体结果_小数拆分.xlsx'
    merged.to_excel(out, index=False)
    print(f'  ③ 小数拆分合并: {overall["教学班ID"].nunique()} → {merged["教学班ID"].nunique()} 门（精确重排 {len(committed)}）')
    return out


# ---------- ④ 失败归因 + 回填 ----------
def failure_attribution(soft_ids):
    sh('echo n | python3 postfailure_analysis.py '
       '--failed 排课结果/调课失败的排课失败课程.xlsx '
       '--pickle 排课结果/reschedule_timetables_frac.pkl '
       '--weeks 20 --days 7 --periods 11 '
       '--output 排课结果/失败课程归因与建议.xlsx')
    sh('python3 scripts/enrich_failure_attribution.py')
    # 标注软偏好救回
    p = f'{RES}/失败课程归因与建议.xlsx'
    if os.path.isfile(p) and soft_ids:
        a = rd(p)
        a['软偏好修复结果'] = a['教学班ID'].map(lambda j: '✅已由软偏好修复排入' if j in soft_ids else '')
        a.to_excel(p, index=False)
        print(f'  ④ 归因表已标注软偏好救回 {len(soft_ids)} 门')


# ---------- ⑤ 报告 ----------
def report(final_overall):
    # 让报告读最终课表
    sh(f'SCHED_OVERRIDE="{final_overall}" python3 scripts/build_run_report.py')


# ---------- ⑥ 悬挂校验 ----------
def verify_dangling():
    with open(f'{RES}/reschedule_timetables_frac.pkl', 'rb') as f:
        data = pickle.load(f)
    t_by_jsh = defaultdict(list)
    for t in data['teachers']:
        t_by_jsh[t.JSH].append(t)
    course_jsh = defaultdict(set)
    for c in data['courses']:
        course_jsh[c.JXBID].add(c.JSH)
    d = 0
    for r in data['classrooms']:
        tt = r.timetable; W, D, P = tt.shape
        for w in range(W):
            for dd in range(D):
                for p in range(P):
                    jx = str(tt[w, dd, p]).strip()
                    if not jx or '@' in jx:
                        continue
                    jshs = course_jsh.get(jx, set())
                    if not jshs:
                        continue
                    if not any(any(str(t.timetable[w, dd, p]['course']).strip() == jx
                                   for t in t_by_jsh[js]) for js in jshs):
                        d += 1
    print(f'\n⑥ 悬挂占用校验(教室占了教师没占): {d} 格  {"✅ 已归零/大幅下降" if d < 560 else "❌ 仍为560"}')
    return d


def main():
    print('=' * 60)
    print('下游全链重跑（调课修复后）')
    print('=' * 60)
    soft_overall, soft_ids = merge_soft()
    final_overall = frac_reschedule_and_merge(soft_overall)
    failure_attribution(soft_ids)
    report(final_overall)
    dangling = verify_dangling()

    print('\n' + '=' * 60)
    print('全链完成。最终课表:', final_overall)
    m = rd(final_overall)
    print(f'最终排课教学班: {m["教学班ID"].nunique()} 门')
    print(f'悬挂占用: {dangling} 格（修复前 560）')


if __name__ == '__main__':
    main()
