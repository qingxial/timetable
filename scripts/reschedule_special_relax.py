# -*- coding: utf-8 -*-
"""特殊要求·统一优先级放宽调课（Q2）——首轮/调课共用 special_requirements.resolve_effective。

不再分桶打补丁。对每门失败课调用同一条优先级解析规则 resolve_effective：
  规则① 具体度优先：课程自身偏好(L3) 覆盖 摊在其行上的通用禁排(全周1-2=L2/周二5-8=L1)，删被覆盖段。
  规则② 连排块 = 周学时 ÷ 可用偏好天：装得下 2 连排、天被压缩(周末)自动放大。
只对"解析后确有变化(删了禁排/放大了连排)"的课再试排；结构上小数课排 @FA/@FB 分段(每段块守恒)。
物理硬约束绝不放宽。事务性：排上即提交，排不上原样回滚。
"""
import os, sys, argparse, pickle, re
from collections import defaultdict
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); os.chdir(ROOT); sys.path.insert(0, 'scripts')
import pandas as pd
from Basic_Data import load_courses
import utils1
from utils1 import schedule_class, get_teacher_instances, get_class_instances1
from local_reschedule_fractional import occupied_cells, clear_cells
from special_requirements import resolve_effective, compute_block_template, _parse_pref_slots

IDX2DAY = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']


def deviate_pref(prefer_time):
    """最小偏离：把 '周X(a-b节)' 的天松开为 '全周(a-b节)'（保留时段窗口、任意工作日）。
    用于精确偏好那一格被物理占死时的兜底重试。返回(新偏好, 是否有变化)。"""
    s = str(prefer_time or '').replace('（', '(').replace('）', ')')
    new = re.sub(r'周[一二三四五六日]\((\d+-\d+)节?\)', r'全周(\1节)', s)
    return new, (new != s)


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
    ap.add_argument('--deviate', action='store_true', help='精确偏好格被占死时, 保留时段窗口松开天再试(最小偏离)')
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
    tried = 0

    def rec(jx, kcm, msg, ok):
        relax_log.append({'教学班ID': base(jx), '课程名称': kcm, '放宽内容': msg,
                          '结果': '✅已排入' if ok else '❌仍排不上'})

    for jx in fids:
        row = get_crow(jx)
        if row is None:
            continue
        eff = resolve_effective(row['Prefer_Time'], row['unavailable_Time'], row['ZXS'])
        changed = bool(eff['dropped_forbids']) or eff['block_template'] is not None
        if not changed:
            continue   # 优先级解析对本课无能为力(纯数据问题) → 不试排
        tried += 1
        pref_days = sorted({d for d, _ in _parse_pref_slots(row['Prefer_Time'])})
        b = base(jx)
        vids = [v for v in (b + '@FA', b + '@FB') if v in new_by_id]

        if vids:  # —— 小数课：排 @FA/@FB 分段(每段按可用天分大块，守恒) ——
            for v in vids:
                vc = new_by_id[v][0]
                seg_mb, seg_tmpl = compute_block_template(float(vc.ZXS), len(pref_days) or 2)
                for j in new_by_id[v]:
                    j.unavailable_Time = eff['unavailable_time']
                    j.max_block = seg_mb
                    j.block_template = ','.join(map(str, seg_tmpl)) if seg_tmpl else ''
            seg_rows, ok_all, got, placed_v = [], True, 0, []
            try:
                T = int(float(new_by_id[vids[0]][0].LLXS))
            except (TypeError, ValueError):
                T = None
            for v in vids:
                vc = new_by_id[v][0]
                room, arr, wks = schedule_class(v, courses_new, teachers, classes_all, classrooms,
                                                flag_reschedule=True, num_days=args.days)
                if not (room and arr):
                    ok_all = False; break
                placed_v.append(v)
                for (day, period, hours) in arr:
                    got += hours * (len(wks) if wks else 0)
                    seg_rows.append({'教学班ID': b, '虚班': v.rsplit('@', 1)[1], '课程名称': vc.KCM,
                                     '教师号': vc.JSH, '周学时': vc.ZXS, '教室代码': room.JASDM, '教室': room.JASMC,
                                     '星期': IDX2DAY[day], '节次': f'第{period+1}-{period+hours}节',
                                     '周次数': len(wks) if wks else '', '放宽桶': 'B'})
            ok_all = ok_all and (T is None or got == T)
            if ok_all:
                rows.extend(seg_rows); placed += 1
                rec(jx, new_by_id[vids[0]][0].KCM,
                    f"小数分段+连排放宽 守恒Σ={got}=LLXS{T}", True)
            else:
                for v in placed_v:
                    vj = new_by_id[v]
                    vt = get_teacher_instances([j.JSH for j in vj], teachers)
                    vc_ = get_class_instances1(vj[0], classes_all, courses_new)
                    clear_cells(v, occupied_cells(vj), room_by_dm, vt, vc_, vj)
                rec(jx, new_by_id[vids[0]][0].KCM, f"@FA/@FB放宽后仍无解或不守恒(got={got},T={T})", False)
        else:  # —— 整数课：删被覆盖禁排 + 生效连排块，排基类 ——
            jxbs = by_id.get(jx) or by_id.get(b)
            if not jxbs:
                continue
            c0 = jxbs[0]
            bak = (c0.unavailable_Time, getattr(c0, 'max_block', None), getattr(c0, 'block_template', None))
            for j in jxbs:
                j.unavailable_Time = eff['unavailable_time']
                j.max_block = eff['max_block']
                j.block_template = ','.join(map(str, eff['block_template'])) if eff['block_template'] else ''
            room, arr, wks = schedule_class(str(c0.JXBID), courses, teachers, classes_all, classrooms,
                                            flag_reschedule=True, num_days=args.days)
            dev_msg = ''
            if not (room and arr) and args.deviate:
                # 兜底：精确偏好格被占死 → 保留时段窗口、松开"天"再试(最小偏离)
                dev_pref, changed_p = deviate_pref(c0.Prefer_Time)
                if changed_p:
                    bak_pref = c0.Prefer_Time
                    for j in jxbs:
                        j.Prefer_Time = dev_pref
                    room, arr, wks = schedule_class(str(c0.JXBID), courses, teachers, classes_all, classrooms,
                                                    flag_reschedule=True, num_days=args.days)
                    if room and arr:
                        dev_msg = f" + 最小偏离(松开天:{bak_pref}→{dev_pref})"
                    else:
                        for j in jxbs:
                            j.Prefer_Time = bak_pref
            if room and arr:
                placed += 1
                for (day, period, hours) in arr:
                    rows.append({'教学班ID': b, '虚班': '', '课程名称': c0.KCM, '教师号': c0.JSH,
                                 '周学时': c0.ZXS, '教室代码': room.JASDM, '教室': room.JASMC,
                                 '星期': IDX2DAY[day], '节次': f'第{period+1}-{period+hours}节',
                                 '周次数': len(wks) if wks else '', '放宽桶': 'A'})
                rec(jx, c0.KCM, f"删被覆盖禁排段{eff['dropped_forbids']} + 连排块{eff['block_template'] or '2'}{dev_msg}", True)
            else:
                for j in jxbs:
                    j.unavailable_Time = bak[0]
                rec(jx, c0.KCM, f"删禁排{eff['dropped_forbids']}后仍无可行位(含最小偏离)", False)

    print(f'\n=== 统一优先级放宽 ===  解析有变化并试排 {tried} 门, 新排入 {placed} 门')
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
