# -*- coding: utf-8 -*-
"""最小偏离软偏好修复（排课"构造→修复"流程的最后一个修复子阶段）。

动机
----
少数课程（典型：体育课）失败的根因是「(教师 × 单一偏好格) 物理超订」——
同一教师有 ≥2 个班都把偏好硬钉在同一个 2 节格、且周次完全重叠。一个教师不能分身，
严格守偏好时每组只能满足 1 个，其余在偏好内**物理无解**，任何搜索/回溯/交换都救不了。

本模块的做法**不是丢偏好**，而是「偏好优先 + 最小偏离让步」：
  1. 偏好仍是主约束，全程已排课不动；
  2. 只对"穷尽常规松弛后仍失败"的课做让步；
  3. 让步**最小**——按与原偏好的偏离度排序：
        dev=0 偏好内 ＞ dev=1 同偏好天(只换节次) ＞ dev=2 同偏好节次(只换天) ＞ dev=3 其它工作日格
     在最小偏离档里选第一个可行格落子；
  4. 所有**硬约束全守**：教师空闲 / 班级空闲(按 IF_CLASS_CONFICT) / 教室空闲(按 IF_ROOM_CONFICT)
     / unavailable 禁排 / 校级周二(5-8节)禁排。

可作为 reschedule 末尾调用：soft_preference_repair(courses, teachers, classes, classrooms, failed_ids)
也可独立 CLI：读 pickle + 失败清单，产出修复明细与更新后的整体结果。
"""
import os, sys, re, argparse
from typing import List, Set, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); os.chdir(ROOT)

from utils1 import trans_week_flags, get_teaching_weeks
from Basic_Data import check_class_conflict, check_room_conflict

DAY2IDX = {"周一": 0, "周二": 1, "周三": 2, "周四": 3, "周五": 4}
IDX2DAY = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
# 标准 2 节连排块起点（0-indexed）：0/2/4/6 → 1-2 / 3-4 / 5-6 / 7-8
GRID_STARTS = (0, 2, 4, 6)


def _norm(s):
    return str(s or '').replace('（', '(').replace('）', ')').replace('；', ';')


def teacher_weeks(jxbs) -> List[int]:
    tw = set()
    for jx in jxbs:
        if getattr(jx, 'RWJSZCDM', None):
            tw |= set(trans_week_flags(jx.RWJSZCDM))
    return sorted(tw)


def forbidden_slots(c) -> Set:
    """unavailable_Time 解析出的 (day,p0) 禁排集合（含 全周/周X(a-b)/周X全天）。"""
    s = _norm(c.unavailable_Time); fb = set()
    for m in re.finditer(r'全周\((\d+)-(\d+)节?\)', s):
        a, b = int(m.group(1)) - 1, int(m.group(2)) - 1
        for d in range(5):
            for p in range(a, b + 1): fb.add((d, p))
    for m in re.finditer(r'(周[一二三四五])\((\d+)-(\d+)节?\)', s):
        d = DAY2IDX[m.group(1)]; a, b = int(m.group(2)) - 1, int(m.group(3)) - 1
        for p in range(a, b + 1): fb.add((d, p))
    for m in re.finditer(r'(周[一二三四五])全天', s):
        d = DAY2IDX[m.group(1)]
        for p in range(11): fb.add((d, p))
    return fb


def preference(c):
    """返回 (偏好天集合, 偏好块起点集合(0-indexed))。"""
    s = _norm(c.Prefer_Time); days, blocks = set(), set()
    for m in re.finditer(r'(周[一二三四五])\((\d+)-(\d+)节?\)', s):
        d = DAY2IDX[m.group(1)]; a, b = int(m.group(2)), int(m.group(3)); days.add(d)
        for st in (1, 3, 5, 7):
            if a <= st and st + 1 <= b:
                blocks.add((d, st - 1))
    return days, blocks


def _deviation(d, p0, pdays, pblocks):
    if (d, p0) in pblocks: return 0          # 完全在偏好内
    if d in pdays: return 1                   # 同偏好天，仅换节次（最贴近：保住星期）
    if any(pb == p0 for (_, pb) in pblocks): return 2  # 同偏好节次，仅换天
    return 3                                  # 其它工作日格


def _classes_of(c, classes):
    return [cl for cl in classes
            if str(cl.BJMC).strip() and str(cl.BJMC).strip() in str(getattr(c, 'TJBJ', '') or '')]


def _suitable_rooms(c, classrooms):
    out = []
    for r in classrooms:
        if str(r.SFYXPK).strip() not in ('1', '1.0'):
            continue
        try:
            if r.SKZWS and c.KRL and float(r.SKZWS or 0) < float(c.KRL or 0):
                continue
        except (TypeError, ValueError):
            pass
        out.append(r)
    return out


def place_one(jxbs, teachers_by_jsh, classes, classrooms):
    """为单门课找最小偏离可行格并落子（更新占用）。返回 dict 或 None。"""
    c = jxbs[0]
    # 本修复仅处理单个 2 节连排块（ZXS<=2）的课——这正是"偏好超订"算法侧失败的全部场景。
    # ZXS>2 需要多块拼排，且这类失败基本是数据问题（偏好与周学时不自洽），不在算法修复范围。
    try:
        if float(c.ZXS) > 2:
            return None
    except (TypeError, ValueError):
        return None
    tw = teacher_weeks(jxbs)
    if not tw:
        return None
    teacher = teachers_by_jsh.get(c.JSH)
    fb = forbidden_slots(c)
    pdays, pblocks = preference(c)

    # 守卫：仅修复"偏好本身合法、只是被占"的课（=算法侧"资源超订"）。
    # 即偏好内必须存在至少一个 网格对齐 + 未被 unavailable/校级禁排 的 2 节格；
    # 否则属于数据问题（偏好节次错位 / 偏好撞禁排 / 无偏好），不在算法修复范围，交回数据侧。
    pref_has_valid = any(
        (d0, p0_) not in fb and (d0, p0_ + 1) not in fb and not (d0 == 1 and p0_ >= 4)
        for (d0, p0_) in pblocks
    )
    if not pref_has_valid:
        return None
    chk_cls = check_class_conflict(c); chk_room = check_room_conflict(c)
    cls = _classes_of(c, classes) if chk_cls else []
    rooms = _suitable_rooms(c, classrooms) if chk_room else []

    cands = []
    for d in range(5):
        for p0 in GRID_STARTS:
            p1 = p0 + 1
            if (d, p0) in fb or (d, p1) in fb:
                continue
            if d == 1 and p0 >= 4:            # 校级硬约束：周二 5-8 节禁排
                continue
            if teacher is not None and any(
                    str(teacher.timetable[w, d, p]['course']).strip()
                    for w in tw for p in (p0, p1)):
                continue
            if cls and any(
                    str(cl.timetable[w, d, p]['course']).strip()
                    for cl in cls for w in tw for p in (p0, p1)):
                continue
            room = None
            if chk_room:
                for r in rooms:
                    if all(str(r.timetable[w, d, p]).strip() == '' for w in tw for p in (p0, p1)):
                        room = r; break
                if room is None:
                    continue
            cands.append((_deviation(d, p0, pdays, pblocks), d, p0, p1, room))

    if not cands:
        return None
    cands.sort(key=lambda x: (x[0], x[1], x[2]))
    dev, d, p0, p1, room = cands[0]

    # 落子：写教师/教室/班级占用
    for w in tw:
        for p in (p0, p1):
            if teacher is not None:
                teacher.timetable[w, d, p]['course'] = c.JXBID
                teacher.timetable[w, d, p]['classroom'] = room.JASMC if room is not None else ''
            if chk_room and room is not None:
                room.timetable[w, d, p] = c.JXBID
            for cl in cls:
                cl.timetable[w, d, p]['course'] = c.JXBID
        for jx in jxbs:
            jx.timetable[w, d, p0] = room.JASDM if room is not None else (jx.JASDM or '')
            jx.timetable[w, d, p1] = room.JASDM if room is not None else (jx.JASDM or '')

    return dict(
        jxbid=c.JXBID, kcm=c.KCM, teacher=c.XM, jsh=c.JSH, krl=c.KRL,
        prefer=c.Prefer_Time, unavail=c.unavailable_Time,
        weeks=tw, day=d, p0=p0, p1=p1, deviation=dev,
        room_code=(str(room.JASDM) if room is not None else '(共占)'),
        room_name=(str(room.JASMC) if room is not None else '(共占)'),
        slot=f"{IDX2DAY[d]}第{p0+1}-{p1+1}节",
    )


def soft_preference_repair(courses, teachers, classes, classrooms, failed_ids, verbose=True):
    """对 failed_ids 逐个尝试最小偏离软偏好落子。返回 (rescued_list, still_failed_list)。
    按候选灵活度从少到多排序（偏好越窄、周次越多者先排），减少彼此挤兑。"""
    teachers_by_jsh = {t.JSH: t for t in teachers}
    by_id = {}
    for c in courses:
        by_id.setdefault(c.JXBID, []).append(c)

    targets = [jx for jx in failed_ids if jx in by_id]

    def flexibility(jx):
        jxbs = by_id[jx]; c = jxbs[0]
        pdays, pblocks = preference(c)
        return (len(pblocks) if pblocks else 99, -len(teacher_weeks(jxbs)))
    targets.sort(key=flexibility)

    rescued, still = [], []
    for jx in targets:
        r = place_one(by_id[jx], teachers_by_jsh, classes, classrooms)
        if r:
            rescued.append(r)
            if verbose:
                tag = {0: '偏好内', 1: '同偏好天换节次', 2: '同偏好节次换天', 3: '其它工作日格'}[r['deviation']]
                print(f"  ✅ {jx} {str(r['kcm'])[:10]:<10} 偏好={str(r['prefer'])[:12]:<12} → {r['slot']:<14} 教室={str(r['room_name'])[:8]:<8} [偏离:{tag}]")
        else:
            still.append(jx)
            if verbose:
                print(f"  ❌ {jx} {by_id[jx][0].KCM[:10]:<10} 仍无解（硬约束下无任何可行格→数据问题）")
    return rescued, still


# ----------------------------- CLI -----------------------------
def _expand_rows(rescued, by_id):
    """把 rescued 落点展开为「调课后的整体结果.xlsx」同构的行。"""
    import pandas as pd
    rows = []
    for r in rescued:
        c = by_id[r['jxbid']][0]
        weeks_1 = ','.join(str(w + 1) for w in r['weeks'])
        classinfo = str(getattr(c, 'TJBJ', '') or '')
        rows.append({
            '教学班ID': r['jxbid'], '课程名称': r['kcm'], '开课院系': getattr(c, 'YXMC', ''),
            '教师': r['teacher'], '教师号': r['jsh'], '周学时': c.ZXS, '课容量': c.KRL,
            '教室': r['room_name'], '教室代码': r['room_code'], '校区': getattr(c, 'SKXQ', ''),
            '班级信息': classinfo, '偏好上课时间': r['prefer'], '避免排课时间': r['unavail'],
            '上课周次': weeks_1, '星期': IDX2DAY[r['day']], '节次': f"第{r['p0']+1}-{r['p1']+1}节",
            '软偏好偏离档': r['deviation'],
        })
    return pd.DataFrame(rows)


def main():
    import pickle
    import pandas as pd
    ap = argparse.ArgumentParser(description="最小偏离软偏好修复（构造-修复流程的末段修复子阶段）")
    ap.add_argument('--pickle', default='排课结果/reschedule_timetables.pkl')
    ap.add_argument('--failed', default='排课结果/调课失败的排课失败课程.xlsx')
    ap.add_argument('--attribution', default='排课结果/失败课程归因与建议.xlsx',
                    help='归因表；用其"卡在步骤"把范围限定在算法侧(②③④ 资源超订)，'
                         '不去硬塞数据问题课(①/⓪)。传 none 则对全部失败课尝试。')
    ap.add_argument('--schedule', default='排课结果/调课后的整体结果.xlsx')
    ap.add_argument('--out-detail', default='排课结果/软偏好修复明细.xlsx')
    ap.add_argument('--out-schedule', default='排课结果/调课后的整体结果_软偏好修复.xlsx')
    args = ap.parse_args()

    print("加载 pickle 占用状态 ...", flush=True)
    with open(args.pickle, 'rb') as f:
        data = pickle.load(f)
    courses, teachers = data['courses'], data['teachers']
    classes, classrooms = data['classes'], data['classrooms']
    by_id = {}
    for c in courses:
        by_id.setdefault(c.JXBID, []).append(c)

    df_fail = pd.read_excel(args.failed)
    col = 'jxbid' if 'jxbid' in df_fail.columns else '教学班ID'
    failed_ids = [str(x).strip() for x in df_fail[col]]

    # 限定到算法侧（②③④ 资源超订）；数据问题课(①偏好错位/撞禁排、⓪日模式/教室静态)不硬塞
    if args.attribution and args.attribution.lower() != 'none' and os.path.isfile(args.attribution):
        attr = pd.read_excel(args.attribution)
        attr['教学班ID'] = attr['教学班ID'].astype(str)
        algo_ids = set(attr.loc[
            attr['卡在步骤'].astype(str).str.startswith(('②', '③', '④')), '教学班ID'])
        scoped = [j for j in failed_ids if j in algo_ids]
        print(f"失败课程 {len(failed_ids)} 门 → 限定算法侧(②③④资源超订) {len(scoped)} 门\n")
        failed_ids = scoped
    else:
        print(f"待修复失败课程: {len(failed_ids)} 门（未限定范围）\n")

    rescued, still = soft_preference_repair(courses, teachers, classes, classrooms, failed_ids)

    print(f"\n==== 修复结果 ====")
    print(f"  ✅ 软偏好修复成功: {len(rescued)} 门")
    print(f"  ❌ 仍失败(真·数据问题): {len(still)} 门")
    by_dev = {}
    for r in rescued:
        by_dev[r['deviation']] = by_dev.get(r['deviation'], 0) + 1
    names = {0: '偏好内', 1: '同偏好天换节次', 2: '同偏好节次换天', 3: '其它工作日格'}
    for k in sorted(by_dev):
        print(f"     偏离[{names[k]}]: {by_dev[k]} 门")

    # 写明细
    det = _expand_rows(rescued, by_id)
    det.to_excel(args.out_detail, index=False)
    print(f"\n修复明细: {args.out_detail}")

    # 合并进整体结果
    base = pd.read_excel(args.schedule)
    merged = pd.concat([base, det.drop(columns=['软偏好偏离档'], errors='ignore')], ignore_index=True)
    merged.to_excel(args.out_schedule, index=False)
    print(f"修复后整体结果: {args.out_schedule}  ({base['教学班ID'].nunique()} → {merged['教学班ID'].nunique()} 个教学班)")


if __name__ == '__main__':
    main()
