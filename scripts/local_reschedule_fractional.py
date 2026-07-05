# -*- coding: utf-8 -*-
"""小数周学时课程「局部重排」——在已完成排课的 pickle 冻结状态上，
清除小数课的旧 ceil 占用，改按 @A/@B 前后分段精确重排。不全量重跑。

事务性（关键）：按【基类课】逐门处理，快照原占用 → 清除 → 试排 @A、@B。
  · @A、@B 都排上且守恒 → 提交（保留新拆分排法）
  · 任一排不上 → 回滚：撤销已排虚班 + 恢复原 ceil 占用（第三层兜底，绝不弄丢已有成果）

复用 utils1.schedule_class 放置；teachers/classes/classrooms 用 pickle 共享对象
（冲突查冻结背景、落子更新共享态）。带总学时守恒校验(Σ实排节数×周 == 理论学时T)。
"""
import os, sys, argparse, pickle
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); os.chdir(ROOT)

import pandas as pd
from Basic_Data import load_courses
import utils1
from utils1 import schedule_class, get_teacher_instances, get_class_instances1

IDX2DAY = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']


def occupied_cells(jxbs):
    """由课程自身 timetable 读出占用格 (w,d,p,jasdm)。"""
    tt = jxbs[0].timetable
    W, D, P = tt.shape
    out = []
    for w in range(W):
        for d in range(D):
            for p in range(P):
                dm = str(tt[w, d, p]).strip()
                if dm:
                    out.append((w, d, p, dm))
    return out


def clear_cells(jxbid, cells, room_by_dm, tlist, clist, jxbs):
    """按给定格子清除该课在 教室/教师/班级/自身 时间表上的占用。"""
    for (w, d, p, dm) in cells:
        r = room_by_dm.get(dm)
        if r is not None and str(r.timetable[w, d, p]).strip() == jxbid:
            r.timetable[w, d, p] = ''
        for t in tlist:
            if t is not None and str(t.timetable[w, d, p]['course']).strip() == jxbid:
                t.timetable[w, d, p]['course'] = ''
                t.timetable[w, d, p]['classroom'] = ''
        for cl in clist:
            if str(cl.timetable[w, d, p]['course']).strip() == jxbid:
                cl.timetable[w, d, p]['course'] = ''
                cl.timetable[w, d, p]['classroom'] = ''
        for j in jxbs:
            j.timetable[w, d, p] = ''


def restore_cells(jxbid, cells, room_by_dm, tlist, clist, jxbs, teacher_weeks_map):
    """把快照的原占用格重新写回 教室/教师/班级/自身 时间表（回滚用）。"""
    for (w, d, p, dm) in cells:
        r = room_by_dm.get(dm)
        jasmc = r.JASMC if r is not None else ''
        if r is not None:
            r.timetable[w, d, p] = jxbid
        for t in tlist:
            if t is not None and w in teacher_weeks_map.get(t.JSH, set()):
                t.timetable[w, d, p]['course'] = jxbid
                t.timetable[w, d, p]['classroom'] = jasmc
        for cl in clist:
            cl.timetable[w, d, p]['course'] = jxbid
            cl.timetable[w, d, p]['classroom'] = jasmc
        for j in jxbs:
            j.timetable[w, d, p] = dm


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--pickle', default='排课结果/reschedule_timetables.pkl')
    ap.add_argument('--split', default='智能排课基础数据/提取的基础数据表_converted/课程表_frac_split.xlsx')
    ap.add_argument('--weeks', type=int, default=20)
    ap.add_argument('--days', type=int, default=7)
    ap.add_argument('--periods', type=int, default=11)
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--out-pickle', default='排课结果/reschedule_timetables_frac.pkl')
    ap.add_argument('--out-detail', default='排课结果/小数课局部重排明细.xlsx')
    ap.add_argument('--out-fallback', default='排课结果/小数课重排回滚清单.xlsx')
    ap.add_argument('--strategy', default='first')
    args = ap.parse_args()
    utils1.set_placement_strategy(args.strategy, 2026)

    print('载入 pickle 冻结状态 ...', flush=True)
    with open(args.pickle, 'rb') as f:
        data = pickle.load(f)
    courses_old, teachers = data['courses'], data['teachers']
    classes_all, classrooms = data['classes'], data['classrooms']
    room_by_dm = {r.JASDM: r for r in classrooms}
    old_by_id = defaultdict(list)
    for c in courses_old:
        old_by_id[c.JXBID].append(c)

    print('载入拆分课程表 @A/@B ...', flush=True)
    courses_new = load_courses(args.split, weeks=args.weeks, days=args.days, periods=args.periods)
    new_by_id = defaultdict(list)
    for c in courses_new:
        new_by_id[c.JXBID].append(c)
    base_ids = sorted(set(jx.rsplit('@', 1)[0] for jx in new_by_id
                          if jx.endswith('@A') or jx.endswith('@B')))
    if args.limit:
        base_ids = base_ids[:args.limit]
    print(f'待局部重排基类课: {len(base_ids)} 门')

    def teacher_weeks_of(jxbs):
        from utils1 import trans_week_flags
        m = {}
        for j in jxbs:
            if getattr(j, 'RWJSZCDM', None):
                m.setdefault(j.JSH, set()).update(trans_week_flags(j.RWJSZCDM))
        return m

    def place_virtual(jx):
        """排一个虚班；成功返回结果行列表，失败返回 None。"""
        cj = new_by_id[jx][0]
        room, arr, wks = schedule_class(jx, courses_new, teachers, classes_all, classrooms,
                                        flag_reschedule=True, num_days=args.days)
        if not arr or not room:
            return None
        base = jx.rsplit('@', 1)[0]
        out = []
        for (day, period, hours) in arr:
            out.append({'教学班ID': base, '虚班': jx[-1], '课程名称': cj.KCM, '教师号': cj.JSH,
                        '每周段节数': cj.ZXS, '教室代码': room.JASDM, '教室': room.JASMC,
                        '星期': IDX2DAY[day], '节次': f'第{period+1}-{period+hours}节',
                        '周次数': len(wks), '本段实排节数×周': hours * len(wks)})
        return out

    rows, committed, rolled_back = [], 0, 0
    fb_list = []
    Tmap = {}
    for c in courses_old:
        try:
            Tmap[c.JXBID] = int(float(c.LLXS)) if c.LLXS not in (None, '') else None
        except (TypeError, ValueError):
            Tmap[c.JXBID] = None

    print('\n按课事务重排（成功提交 / 失败回滚原样）...', flush=True)
    for i, base in enumerate(base_ids):
        old_jxbs = old_by_id.get(base, [])
        # 快照原占用
        snap = occupied_cells(old_jxbs) if old_jxbs else []
        old_c0 = old_jxbs[0] if old_jxbs else None
        old_tlist = get_teacher_instances([j.JSH for j in old_jxbs], teachers) if old_jxbs else []
        old_clist = get_class_instances1(old_c0, classes_all, courses_old) if old_c0 else []
        old_tw = teacher_weeks_of(old_jxbs) if old_jxbs else {}
        # 清除原占用
        if snap:
            clear_cells(base, snap, room_by_dm, old_tlist, old_clist, old_jxbs)

        va, vb = base + '@A', base + '@B'
        ra = place_virtual(va) if va in new_by_id else []
        rb = place_virtual(vb) if vb in new_by_id else []

        # 判定：需要的虚班都成功
        need_a, need_b = va in new_by_id, vb in new_by_id
        ok = (not need_a or ra is not None) and (not need_b or rb is not None)
        got = sum(x['本段实排节数×周'] for x in (ra or []) + (rb or []))
        T = Tmap.get(base)
        ok = ok and (T is None or got == T)

        if ok:
            rows.extend((ra or []) + (rb or []))
            committed += 1
        else:
            # 回滚：撤销已排虚班 + 恢复原占用
            for jx, res in ((va, ra), (vb, rb)):
                if res:
                    vjxbs = new_by_id[jx]
                    vt = get_teacher_instances([j.JSH for j in vjxbs], teachers)
                    vc = get_class_instances1(vjxbs[0], classes_all, courses_new)
                    clear_cells(jx, occupied_cells(vjxbs), room_by_dm, vt, vc, vjxbs)
            if snap:
                restore_cells(base, snap, room_by_dm, old_tlist, old_clist, old_jxbs, old_tw)
            rolled_back += 1
            fb_list.append({'教学班ID': base, '课程名称': old_c0.KCM if old_c0 else '',
                            '原ZXS': old_c0.ZXS if old_c0 else '', 'T理论学时': T,
                            '回滚原因': '@A或@B无可行位，保留原ceil排法'})
        if (i + 1) % 100 == 0:
            print(f'  进度 {i+1}/{len(base_ids)}  提交 {committed} 回滚 {rolled_back}', flush=True)

    print(f'\n=== 结果 ===')
    print(f'  提交(精确重排,零残差): {committed} 门')
    print(f'  回滚(保留原ceil排法): {rolled_back} 门')
    print(f'  所有提交课程守恒: Σ实排×周 == T（构造保证）')

    if rows:
        pd.DataFrame(rows).to_excel(args.out_detail, index=False)
        print(f'  重排明细: {args.out_detail}（{len(rows)} 行）')
    if fb_list:
        pd.DataFrame(fb_list).to_excel(args.out_fallback, index=False)
        print(f'  回滚清单: {args.out_fallback}（{len(fb_list)} 门）')

    with open(args.out_pickle, 'wb') as f:
        pickle.dump(data, f)
    print(f'  新占用快照: {args.out_pickle}')


if __name__ == '__main__':
    main()
