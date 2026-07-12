# -*- coding: utf-8 -*-
"""特殊要求·约束放宽调课（Q2 · P4）——针对"完全失败"课程，在调课阶段继续救。

核心：把所有课都排完。在冻结状态上，只对 229 门失败课按"分级放宽"再试排：
  桶A 早上偏好被"全周1-2"类别禁排挡住 → L3 覆盖 L2：临时删掉该课那条被覆盖的禁排段，
       按其自身 Prefer_Time 试排（仍守全部物理硬约束：教师/班级/教室/容量）。
  桶B 仅周末+高学时(2连排装不下) → 允许 >2 连排：block_template = 把周学时按可用天均分成大块，
       在周末大块连排试排。
物理硬约束绝不放宽（不制造教师分身/教室双占）。事务性：排上即提交，排不上原样不动。
每门课记录放宽了什么（喂 Q3 松弛记录）。
"""
import os, sys, argparse, pickle, re, math
from collections import defaultdict
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); os.chdir(ROOT)
import pandas as pd
import utils1
from utils1 import schedule_class

IDX2DAY = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
DAY2IDX = {d: i for i, d in enumerate(IDX2DAY)}


def pf(s):
    return str(s or '').replace('（', '(').replace('）', ')').strip()


def drop_forbid_segments(unav, drop_pats):
    """从 unavailable_Time 删掉匹配 drop_pats 的禁排段，返回(新串, 被删列表)。"""
    segs = [s for s in re.split(r'[;；]', pf(unav)) if s.strip()]
    kept, dropped = [], []
    for s in segs:
        if any(p.search(s.replace(' ', '')) for p in drop_pats):
            dropped.append(s)
        else:
            kept.append(s)
    return ';'.join(kept), dropped


# 桶A：早上偏好 (周X(1-k节)) 且被 全周(1-2) 挡住
A_PREF = re.compile(r'周[一二三四五六日]\(1-[24]节?\)')
A_FORBID = [re.compile(r'全周\(1-2节?\)')]
# 桶B：仅周末
WEEKDAYS = ['周一', '周二', '周三', '周四', '周五']


def classify_failed(row):
    pref = pf(row['Prefer_Time']); un = pf(row['unavailable_Time'])
    try:
        z = math.ceil(float(row['ZXS']))
    except (TypeError, ValueError):
        z = 0
    if A_PREF.search(pref) and '全周(1-2' in un:
        return 'A', z
    wk = [d for d in ['周六', '周日'] if d in pref]
    if wk and not any(d in pref for d in WEEKDAYS) and len(wk) * 2 < z:
        return 'B', z
    return 'C', z


def occupied_cells(jxbs):
    tt = jxbs[0].timetable; W, D, P = tt.shape
    return [(w, d, p, str(tt[w, d, p]).strip())
            for w in range(W) for d in range(D) for p in range(P) if str(tt[w, d, p]).strip()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--pickle', default='排课结果/reschedule_timetables_frac.pkl')
    ap.add_argument('--out-pickle', default='排课结果/reschedule_timetables_special.pkl')
    ap.add_argument('--failed', default='排课结果/调课失败的排课失败课程.xlsx')
    ap.add_argument('--course', default='智能排课基础数据/提取的基础数据表_converted/课程表_split_merged.xlsx')
    ap.add_argument('--days', type=int, default=7)
    ap.add_argument('--strategy', default='first')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()
    utils1.set_placement_strategy(args.strategy, 2026)

    print('载入冻结状态 ...', flush=True)
    data = pickle.load(open(args.pickle, 'rb'))
    courses = data['courses']; teachers = data['teachers']
    classes_all, classrooms = data['classes'], data['classrooms']
    room_by_dm = {r.JASDM: r for r in classrooms}
    by_id = defaultdict(list)
    for c in courses:
        by_id[str(c.JXBID)].append(c)

    fail = pd.read_excel(args.failed, dtype=str)
    cdf = pd.read_excel(args.course, dtype=str, header=1)
    cdf['JXBID'] = cdf['JXBID'].astype(str)
    crow = {r['JXBID']: r for _, r in cdf.iterrows()}

    def base(j): return j.rsplit('@', 1)[0]

    def get_crow(j):
        if j in crow: return crow[j]
        if base(j) in crow: return crow[base(j)]
        return None

    def get_jxbs(j):
        return by_id.get(j) or by_id.get(base(j))

    fids = [str(x) for x in fail['jxbid']]
    if args.limit:
        fids = fids[:args.limit]

    placed, rows, relax_log = 0, [], []
    bucket_ct = defaultdict(int)
    tried = defaultdict(int)

    for jx in fids:
        row = get_crow(jx)
        if row is None:
            continue
        bucket, z = classify_failed(row)
        bucket_ct[bucket] += 1
        if bucket == 'C':
            continue
        jxbs = get_jxbs(jx)
        if not jxbs:
            continue
        c0 = jxbs[0]
        # 备份可变属性
        bak = (c0.unavailable_Time, getattr(c0, 'max_block', None), getattr(c0, 'block_template', None))
        relaxed = []
        if bucket == 'A':
            new_un, dropped = drop_forbid_segments(c0.unavailable_Time, A_FORBID)
            for j in jxbs:
                j.unavailable_Time = new_un
            relaxed.append(f'删类别禁排段(L3覆盖L2): {dropped}')
        elif bucket == 'B':
            days = [d for d in ['周六', '周日'] if d in pf(row['Prefer_Time'])]
            nd = len(days) or 2
            q, r = divmod(z, nd)
            tmpl = [q + 1] * r + [q] * (nd - r)   # 均分成 nd 个大块，和=z
            for j in jxbs:
                j.max_block = max(tmpl)
                j.block_template = ','.join(map(str, tmpl))
            relaxed.append(f'允许连排块 {tmpl}(仅周末{nd}天装{z}节)')
        tried[bucket] += 1

        # 试排（事务：失败即回滚属性，占位由 schedule_class 内部只在成功时写）
        ok = False
        try:
            room, arr, wks = schedule_class(jx if jx in by_id else base(jx), courses, teachers,
                                            classes_all, classrooms, flag_reschedule=True, num_days=args.days)
            if room and arr:
                ok = True
                for (day, period, hours) in arr:
                    rows.append({'教学班ID': base(jx), '课程名称': c0.KCM, '教师号': c0.JSH,
                                 '周学时': c0.ZXS, '教室代码': room.JASDM, '教室': room.JASMC,
                                 '星期': IDX2DAY[day], '节次': f'第{period+1}-{period+hours}节',
                                 '周次数': len(wks) if wks else '', '放宽桶': bucket})
        except Exception as e:
            relaxed.append(f'异常:{e}')
        if ok:
            placed += 1
            relax_log.append({'教学班ID': base(jx), '课程名称': c0.KCM, '放宽桶': bucket,
                              '放宽内容': ' | '.join(relaxed), '结果': '✅已排入'})
        else:
            # 回滚属性
            for j in jxbs:
                j.unavailable_Time = bak[0]
                if bak[1] is None and hasattr(j, 'max_block'):
                    try: delattr(j, 'max_block')
                    except Exception: j.max_block = 2
                if bak[2] is None and hasattr(j, 'block_template'):
                    try: delattr(j, 'block_template')
                    except Exception: j.block_template = None
            relax_log.append({'教学班ID': base(jx), '课程名称': c0.KCM, '放宽桶': bucket,
                              '放宽内容': ' | '.join(relaxed), '结果': '❌仍排不上'})

    print(f'\n=== 分桶(全部失败课) ===  A={bucket_ct["A"]} B={bucket_ct["B"]} C={bucket_ct["C"]}')
    print(f'=== 尝试放宽 ===  A试{tried["A"]} B试{tried["B"]}')
    print(f'=== 结果 ===  新排入 {placed} 门 / 尝试 {tried["A"]+tried["B"]} 门')

    if not args.dry_run:
        pd.DataFrame(rows).to_excel('排课结果/特殊要求放宽排入明细.xlsx', index=False)
        pd.DataFrame(relax_log).to_excel('排课结果/特殊要求放宽记录.xlsx', index=False)
        with open(args.out_pickle, 'wb') as f:
            pickle.dump(data, f)
        print('已写: 特殊要求放宽排入明细.xlsx / 特殊要求放宽记录.xlsx /', args.out_pickle)
    else:
        print('(dry-run: 不落盘)')


if __name__ == '__main__':
    main()
