# -*- coding: utf-8 -*-
"""特殊要求·约束放宽调课（Q2 · P4）——针对"完全失败"课程，在调课阶段继续救。

核心：把所有课都排完。在冻结状态上，只对失败课按"分级放宽"再试排：
  桶A 早上偏好被"全周1-2"类别禁排挡住 → L3 覆盖 L2：临时删掉该课那条被覆盖的禁排段，
       按其自身 Prefer_Time 试排（基类课，整数 ZXS）。
  桶B 仅周末+高学时(2连排装不下) → 小数分段 + >2连排：对该课的 @FA/@FB 虚班
       （整数 ZXS、零残差）施加 block_template，把每段 ZXS 按周末天数均分成大块连排。
       两段都排上且 Σ(节×周)==LLXS 守恒 → 提交，否则回滚。
物理硬约束绝不放宽（不制造教师分身/教室双占）。事务性：排上即提交，排不上原样不动。
每门课记录放宽了什么（喂 Q3 松弛记录）。
"""
import os, sys, argparse, pickle, re, math
from collections import defaultdict
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); os.chdir(ROOT)
import pandas as pd
from Basic_Data import load_courses
import utils1
from utils1 import schedule_class, get_teacher_instances, get_class_instances1
from local_reschedule_fractional import occupied_cells, clear_cells

IDX2DAY = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
WEEKDAYS = ['周一', '周二', '周三', '周四', '周五']
A_PREF = re.compile(r'周[一二三四五六日]\(1-[24]节?\)')
A_FORBID = [re.compile(r'全周\(1-2节?\)')]


def pf(s):
    return str(s or '').replace('（', '(').replace('）', ')').strip()


def drop_forbid_segments(unav, drop_pats):
    segs = [s for s in re.split(r'[;；]', pf(unav)) if s.strip()]
    kept, dropped = [], []
    for s in segs:
        (dropped if any(p.search(s.replace(' ', '')) for p in drop_pats) else kept).append(s)
    return ';'.join(kept), dropped


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


def weekend_days_of(pref):
    return [d for d in ['周六', '周日'] if d in pf(pref)]


def block_template_for(zxs_int, ndays):
    """把整数 ZXS 按 ndays 天均分成大块，和=zxs（带余靠前）。"""
    nd = ndays or 2
    q, r = divmod(zxs_int, nd)
    return [q + 1] * r + [q] * (nd - r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--pickle', default='排课结果/reschedule_timetables_frac.pkl')
    ap.add_argument('--out-pickle', default='排课结果/reschedule_timetables_special.pkl')
    ap.add_argument('--failed', default='排课结果/调课失败的排课失败课程.xlsx')
    ap.add_argument('--course', default='智能排课基础数据/提取的基础数据表_converted/课程表_split_merged.xlsx')
    ap.add_argument('--split', default='智能排课基础数据/提取的基础数据表_converted/课程表_frac_split.xlsx')
    ap.add_argument('--days', type=int, default=7)
    ap.add_argument('--strategy', default='first')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()
    utils1.set_placement_strategy(args.strategy, 2026)

    print('载入冻结状态 ...', flush=True)
    data = pickle.load(open(args.pickle, 'rb'))
    courses, teachers = data['courses'], data['teachers']
    classes_all, classrooms = data['classes'], data['classrooms']
    room_by_dm = {r.JASDM: r for r in classrooms}
    by_id = defaultdict(list)
    for c in courses:
        by_id[str(c.JXBID)].append(c)

    print('载入小数拆分虚班 @FA/@FB ...', flush=True)
    courses_new = load_courses(args.split, weeks=20, days=args.days, periods=11)
    new_by_id = defaultdict(list)
    for c in courses_new:
        new_by_id[str(c.JXBID)].append(c)

    fail = pd.read_excel(args.failed, dtype=str)
    cdf = pd.read_excel(args.course, dtype=str, header=1)
    cdf['JXBID'] = cdf['JXBID'].astype(str)
    crow = {r['JXBID']: r for _, r in cdf.iterrows()}

    def base(j): return j.rsplit('@', 1)[0]

    def get_crow(j):
        return crow.get(j) if j in crow else crow.get(base(j))

    fids = [str(x) for x in fail['jxbid']]
    if args.limit:
        fids = fids[:args.limit]

    placed, rows, relax_log = 0, [], []
    bct = defaultdict(int); tried = defaultdict(int)

    def place_base_A(jx, jxbs, row):
        """桶A：删被覆盖的类别禁排段，按自身偏好排基类课。"""
        c0 = jxbs[0]
        bak_un = c0.unavailable_Time
        new_un, dropped = drop_forbid_segments(c0.unavailable_Time, A_FORBID)
        for j in jxbs:
            j.unavailable_Time = new_un
        room, arr, wks = schedule_class(str(c0.JXBID), courses, teachers, classes_all, classrooms,
                                        flag_reschedule=True, num_days=args.days)
        if room and arr:
            for (day, period, hours) in arr:
                rows.append({'教学班ID': base(jx), '虚班': '', '课程名称': c0.KCM, '教师号': c0.JSH,
                             '周学时': c0.ZXS, '教室代码': room.JASDM, '教室': room.JASMC,
                             '星期': IDX2DAY[day], '节次': f'第{period+1}-{period+hours}节',
                             '周次数': len(wks) if wks else '', '放宽桶': 'A'})
            return True, f'删类别禁排段(L3覆盖L2): {dropped}'
        for j in jxbs:
            j.unavailable_Time = bak_un
        return False, f'删禁排后仍无可行位: {dropped}'

    def place_frac_B(jx, row):
        """桶B：对 @FA/@FB 虚班施加 block_template 在周末大块连排，事务提交/回滚。"""
        b = base(jx)
        vids = [b + '@FA', b + '@FB']
        vids = [v for v in vids if v in new_by_id]
        if not vids:
            return False, '无@FA/@FB拆分，跳过'
        wknd = weekend_days_of(row['Prefer_Time'])
        # 每个虚班设块模板
        for v in vids:
            vc = new_by_id[v][0]
            zi = int(round(float(vc.ZXS)))
            tmpl = block_template_for(zi, len(wknd) or 2)
            for j in new_by_id[v]:
                j.max_block = max(tmpl)
                j.block_template = ','.join(map(str, tmpl))
        # 逐虚班试排
        seg_rows, ok_all, got = [], True, 0
        placed_v = []
        T = None
        try:
            T = int(float(new_by_id[vids[0]][0].LLXS))
        except (TypeError, ValueError):
            T = None
        for v in vids:
            vc = new_by_id[v][0]
            room, arr, wks = schedule_class(v, courses_new, teachers, classes_all, classrooms,
                                            flag_reschedule=True, num_days=args.days)
            if not (room and arr):
                ok_all = False
                break
            placed_v.append(v)
            for (day, period, hours) in arr:
                got += hours * (len(wks) if wks else 0)
                seg_rows.append({'教学班ID': b, '虚班': v.rsplit('@', 1)[1], '课程名称': vc.KCM,
                                 '教师号': vc.JSH, '周学时': vc.ZXS, '教室代码': room.JASDM, '教室': room.JASMC,
                                 '星期': IDX2DAY[day], '节次': f'第{period+1}-{period+hours}节',
                                 '周次数': len(wks) if wks else '', '放宽桶': 'B'})
        ok_all = ok_all and (T is None or got == T)
        if ok_all:
            rows.extend(seg_rows)
            tmpls = {v: new_by_id[v][0].block_template for v in vids}
            return True, f'小数分段+周末大连排 {tmpls} 守恒Σ={got}=LLXS{T}'
        # 回滚已排虚班占位
        for v in placed_v:
            vjxbs = new_by_id[v]
            vt = get_teacher_instances([j.JSH for j in vjxbs], teachers)
            vc_ = get_class_instances1(vjxbs[0], classes_all, courses_new)
            clear_cells(v, occupied_cells(vjxbs), room_by_dm, vt, vc_, vjxbs)
        return False, f'@FA/@FB周末大连排仍无解或不守恒(got={got},T={T})'

    for jx in fids:
        row = get_crow(jx)
        if row is None:
            continue
        bucket, z = classify_failed(row)
        bct[bucket] += 1
        if bucket == 'C':
            continue
        tried[bucket] += 1
        if bucket == 'A':
            jxbs = by_id.get(jx) or by_id.get(base(jx))
            if not jxbs:
                continue
            ok, msg = place_base_A(jx, jxbs, row)
            kcm = jxbs[0].KCM
        else:
            ok, msg = place_frac_B(jx, row)
            kcm = (new_by_id.get(base(jx) + '@FA') or new_by_id.get(base(jx) + '@FB') or [None])[0]
            kcm = kcm.KCM if kcm else ''
        if ok:
            placed += 1
        relax_log.append({'教学班ID': base(jx), '课程名称': kcm, '放宽桶': bucket,
                          '放宽内容': msg, '结果': '✅已排入' if ok else '❌仍排不上'})

    print(f'\n=== 分桶(全部失败课) ===  A={bct["A"]} B={bct["B"]} C={bct["C"]}')
    print(f'=== 结果 ===  新排入 {placed} 门 / 尝试 {tried["A"]+tried["B"]} 门 (A试{tried["A"]} B试{tried["B"]})')

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
