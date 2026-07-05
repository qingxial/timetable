# -*- coding: utf-8 -*-
"""小数周学时课程「局部重排」——在已完成排课的 pickle 冻结状态上，
清除小数课的旧 ceil 占用，改按 @A/@B 前后分段精确重排。不全量重跑。

流程：
  1. pickle 载入全量占用（teachers/classes/classrooms 带既有占用，作冻结背景）
  2. 拆分课程表载入 @A/@B 虚班定义（scripts/split_fractional_zxs 已验证无损）
  3. 清除：把每门被拆基类课的旧占用从 teacher/class/classroom 时间表精确抹掉
  4. 放置：对每个 @A/@B 调用 utils1.schedule_class（复用已验证算法），
           teachers/classes/classrooms 用 pickle 共享对象 → 冲突查冻结背景、落子更新共享态
  5. 合并回写 + 总学时守恒校验（Σ 实排节数×周 == T）
"""
import os, sys, argparse, pickle, time
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); os.chdir(ROOT)

import pandas as pd
from Basic_Data import load_courses
import utils1
from utils1 import (schedule_class, get_consecutive_jxbid, get_teacher_instances,
                    get_class_instances1, get_teaching_weeks, trans_week_flags)

IDX2DAY = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']


def clear_course(base_jxbid, courses_old_by_id, teachers, classes, classrooms, classes_all, courses_old):
    """把一门基类课的旧占用从共享时间表精确抹掉。返回清除的格子数。"""
    jxbs = courses_old_by_id.get(base_jxbid, [])
    if not jxbs:
        return 0
    c0 = jxbs[0]
    room_by_dm = {r.JASDM: r for r in classrooms}
    tlist = get_teacher_instances([j.JSH for j in jxbs], teachers)
    clist = get_class_instances1(c0, classes_all, courses_old)
    cleared = 0
    tt = c0.timetable
    W, D, P = tt.shape
    for w in range(W):
        for d in range(D):
            for p in range(P):
                dm = str(tt[w, d, p]).strip()
                if not dm:
                    continue
                # 清教室
                r = room_by_dm.get(dm)
                if r is not None and str(r.timetable[w, d, p]).strip() == base_jxbid:
                    r.timetable[w, d, p] = ''
                # 清教师
                for t in tlist:
                    if t is not None and str(t.timetable[w, d, p]['course']).strip() == base_jxbid:
                        t.timetable[w, d, p]['course'] = ''
                        t.timetable[w, d, p]['classroom'] = ''
                # 清班级
                for cl in clist:
                    if str(cl.timetable[w, d, p]['course']).strip() == base_jxbid:
                        cl.timetable[w, d, p]['course'] = ''
                        cl.timetable[w, d, p]['classroom'] = ''
                # 清课程自身
                for j in jxbs:
                    j.timetable[w, d, p] = ''
                cleared += 1
    return cleared


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--pickle', default='排课结果/reschedule_timetables.pkl')
    ap.add_argument('--split', default='智能排课基础数据/提取的基础数据表_converted/课程表_frac_split.xlsx')
    ap.add_argument('--weeks', type=int, default=20)
    ap.add_argument('--days', type=int, default=7)
    ap.add_argument('--periods', type=int, default=11)
    ap.add_argument('--limit', type=int, default=0, help='只处理前 N 门基类课（0=全部），调试用')
    ap.add_argument('--out-pickle', default='排课结果/reschedule_timetables_frac.pkl')
    ap.add_argument('--out-detail', default='排课结果/小数课局部重排明细.xlsx')
    ap.add_argument('--strategy', default='first')
    args = ap.parse_args()
    utils1.set_placement_strategy(args.strategy, 2026)

    print('载入 pickle 冻结状态 ...', flush=True)
    with open(args.pickle, 'rb') as f:
        data = pickle.load(f)
    courses_old, teachers = data['courses'], data['teachers']
    classes_all, classrooms = data['classes'], data['classrooms']
    old_by_id = defaultdict(list)
    for c in courses_old:
        old_by_id[c.JXBID].append(c)

    print('载入拆分课程表 @A/@B ...', flush=True)
    courses_new = load_courses(args.split, weeks=args.weeks, days=args.days, periods=args.periods)
    # 把 @A/@B 的 teacher/classroom timetable 指向 pickle 的共享对象：
    # schedule_class 用 teachers/classes/classrooms 形参（pickle），courses 形参用 courses_new。
    new_by_id = defaultdict(list)
    for c in courses_new:
        new_by_id[c.JXBID].append(c)
    virt_ids = [jx for jx in new_by_id if jx.endswith('@A') or jx.endswith('@B')]
    base_ids = sorted(set(jx.rsplit('@', 1)[0] for jx in virt_ids))
    if args.limit:
        base_ids = base_ids[:args.limit]
        virt_ids = [jx for jx in virt_ids if jx.rsplit('@', 1)[0] in set(base_ids)]
    print(f'待局部重排基类课: {len(base_ids)} 门 → 虚班 {len(virt_ids)} 个')

    # 3. 清除旧占用
    print('\n清除旧 ceil 占用 ...', flush=True)
    tot_clear = 0
    for b in base_ids:
        tot_clear += clear_course(b, old_by_id, teachers, classes_all, classrooms, classes_all, courses_old)
    print(f'  共清除 {tot_clear} 个占用格')

    # 4. 放置 @A/@B（先 @A 后 @B，@B 可借 paired 软约束靠拢 @A）
    print('\n放置 @A/@B ...', flush=True)
    rows = []
    placed = defaultdict(dict)   # base -> {'A':periods_per_week*weeks, 'B':...}
    fail = []
    order = [jx for jx in virt_ids if jx.endswith('@A')] + [jx for jx in virt_ids if jx.endswith('@B')]
    for jx in order:
        cj = new_by_id[jx][0]
        room, arr, wks = schedule_class(jx, courses_new, teachers, classes_all, classrooms,
                                        flag_reschedule=True, num_days=args.days)
        if not arr or not room:
            fail.append(jx); continue
        base = jx.rsplit('@', 1)[0]
        # 记录结果行 + 每周实排节数
        wk_periods = 0
        for (day, period, hours) in arr:
            wk_periods += hours
            rows.append({
                '教学班ID': base, '虚班': jx[-1], '课程名称': cj.KCM, '教师号': cj.JSH,
                '周学时段': cj.ZXS, '教室代码': room.JASDM,
                '星期': IDX2DAY[day], '节次': f'第{period+1}-{period+hours}节',
                '周次数': len(wks), '上课周(0based)': ','.join(str(w) for w in sorted(wks)),
            })
        placed[base][jx[-1]] = wk_periods * len(wks)

    # 5. 守恒校验
    print('\n=== 守恒校验（Σ实排节数×周 == 理论学时T）===')
    detail = pd.DataFrame(rows)
    # 取每门课的 T（LLXS）
    Tmap = {}
    for c in courses_old:
        try:
            Tmap[c.JXBID] = int(float(c.LLXS)) if c.LLXS not in (None, '') else None
        except (TypeError, ValueError):
            Tmap[c.JXBID] = None
    ok = bad = 0
    bad_list = []
    for b in base_ids:
        if b in fail_bases(fail):
            continue
        got = sum(placed.get(b, {}).values())
        T = Tmap.get(b)
        if T is None:
            continue
        if got == T:
            ok += 1
        else:
            bad += 1; bad_list.append((b, got, T))
    print(f'  守恒 OK: {ok}   不守恒: {bad}   放置失败虚班: {len(fail)}')
    if bad_list[:5]:
        print('  不守恒样例:', bad_list[:5])

    if len(detail):
        detail.to_excel(args.out_detail, index=False)
        print(f'\n明细: {args.out_detail}（{len(detail)} 行）')

    # 6. 保存新 pickle
    with open(args.out_pickle, 'wb') as f:
        pickle.dump(data, f)
    print(f'新占用快照: {args.out_pickle}')


def fail_bases(fail):
    return set(jx.rsplit('@', 1)[0] for jx in fail)


if __name__ == '__main__':
    main()
