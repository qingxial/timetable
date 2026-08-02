# -*- coding: utf-8 -*-
"""失败课程"精确分类"——不信启发式标签，在冻结状态上逐课复算真实占用后定类。

方法：对每门失败课，用统一优先级 resolve_effective 得到生效约束，然后：
  0) 无教师(JSH缺) → T0 无教师
  1) filter_suitable_classrooms 为空 → R0 无匹配教室(容量/类型/校区)
  2) new_generate_day_patterns 为空 → P0 偏好与学时不自洽(生成不出日模式)
  3) 逐候选(天,连排块)段，按真实占用探测 教师/班级/教室 是否空(仅在 rel_weeks=教师周∩教学周)：
     · 教师在所有候选段都被占       → T1 教师瓶颈(分身/饱和)
     · 存在段 教师空&班级空&教室全占 → R1 教室时段占满
     · 存在段 教师空&班级被占        → C1 班级冲突(学生撞课)
     · 存在段 三者全空(引擎却没排上) → X 本可排·需人工复核
     · 否则(候选段太少/多天凑不齐)   → P1 偏好过窄·多天凑不齐
每类附带证据(候选段数 + 教师占/班级占/教室占/全空 计数)，可审计。
"""
import os, sys, argparse, pickle, re
from collections import defaultdict
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); os.chdir(ROOT); sys.path.insert(0, 'scripts')
import pandas as pd
from utils1 import (trans_week_flags, get_teaching_weeks, get_teacher_instances,
                    filter_suitable_classrooms, get_class_instances1)
from tryloadlimit import build_preferences, new_generate_day_patterns
from Basic_Data import check_room_conflict, check_class_conflict
from special_requirements import resolve_effective, _parse_pref_slots

IDX2DAY = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']

CAT = {
    'T0': '无教师(数据缺失)',
    'R0': '无匹配教室(容量/类型/校区设置)',
    'P0': '偏好与学时不自洽(装不下)',
    'P2': '偏好节次与2节格错位(如6-7节, 跨5-6|7-8边界)',
    'T1': '教师瓶颈(同师多班挤同时段·分身不可能)',
    'R1': '教室时段占满(该时段无空教室)',
    'C1': '班级冲突(学生同时段已有课)',
    'P1': '偏好过窄·多天凑不齐',
    'X': '本可排·需人工复核',
}


def day_allowed_periods(pref_slots, day, unavail_str, periods):
    """该天允许的节次集合(0-based)：有偏好取偏好窗口，无偏好取全天；再挖掉禁排。"""
    allowed = set()
    for d, ps in pref_slots:
        if d == day:
            allowed |= {p - 1 for p in ps}          # 1-based → 0-based
    if not any(d == day for d, _ in pref_slots):
        allowed = set(range(periods))
    # 挖禁排
    for seg in re.split(r'[;；]', str(unavail_str or '').replace('（', '(').replace('）', ')')):
        seg = seg.strip()
        m = re.match(r'^周([一二三四五六日])\((\d+)-(\d+)节?\)$', seg)
        if m and IDX2DAY.index('周' + m.group(1)) == day:
            allowed -= {p - 1 for p in range(int(m.group(2)), int(m.group(3)) + 1)}
        m2 = re.match(r'^全周\((\d+)-(\d+)节?\)$', seg)
        if m2 and day < 5:
            allowed -= {p - 1 for p in range(int(m2.group(1)), int(m2.group(2)) + 1)}
        m3 = re.match(r'^周([一二三四五六日])全天$', seg)          # 整天禁排
        if m3 and IDX2DAY.index('周' + m3.group(1)) == day:
            allowed = set()
    return allowed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--pickle', default='排课结果/reschedule_timetables_special.pkl')
    ap.add_argument('--failed', default='排课结果/调课失败的排课失败课程_特殊放宽后.xlsx')
    ap.add_argument('--course', default='智能排课基础数据/提取的基础数据表_converted/课程表_split_merged.xlsx')
    ap.add_argument('--out', default='排课结果/失败课程精确分类.xlsx')
    ap.add_argument('--periods', type=int, default=11)
    args = ap.parse_args()

    print('载入冻结状态 ...', flush=True)
    data = pickle.load(open(args.pickle, 'rb'))
    courses, teachers = data['courses'], data['teachers']
    classes_all, classrooms = data['classes'], data['classrooms']
    by_id = defaultdict(list)
    for c in courses:
        by_id[str(c.JXBID)].append(c)

    fail = pd.read_excel(args.failed, dtype=str)
    cdf = pd.read_excel(args.course, dtype=str, header=1)
    cdf['JXBID'] = cdf['JXBID'].astype(str)
    crow = {r['JXBID']: r for _, r in cdf.iterrows()}

    def base(j): return j.rsplit('@', 1)[0]

    rows = []
    for jx in [str(x) for x in fail['jxbid']]:
        jxbs = by_id.get(jx) or by_id.get(base(jx))
        r = crow[jx] if jx in crow else (crow[base(jx)] if base(jx) in crow else None)
        if not jxbs or r is None:
            continue
        c0 = jxbs[0]
        kcm, jsh = c0.KCM, str(c0.JSH or '').strip()
        eff = resolve_effective(r['Prefer_Time'], r['unavailable_Time'], r['ZXS'])
        pref_slots = _parse_pref_slots(eff['prefer_time'])

        cat, ev = None, ''
        # 0 无教师
        if not jsh or jsh.lower() in ('nan', 'none', '', '0'):
            cat = 'T0'
        if cat is None:
            # 1 无匹配教室
            rooms = filter_suitable_classrooms(classrooms, c0)
            if not rooms:
                cat, ev = 'R0', f'ZXS={r["ZXS"]} 容量={r.get("KRL")} 类型={r.get("JASLXMC")}'
        if cat is None:
            # 2 生成不出日模式
            for j in jxbs:
                j.max_block = eff['max_block']
                j.block_template = ','.join(map(str, eff['block_template'])) if eff['block_template'] else ''
            tc = build_preferences(eff['prefer_time'], eff['unavailable_time'])
            _bt = eff['block_template']
            patterns = new_generate_day_patterns(c0.ZXS, 7, tc, flag_reschedule=True,
                                                 max_block=eff['max_block'] or 2, block_template=_bt)
            if not patterns:
                cat, ev = 'P0', f'ZXS={r["ZXS"]} 偏好={eff["prefer_time"] or "无"}'
        if cat is None:
            # 3 逐候选段探测真实占用
            tobjs = get_teacher_instances([j.JSH for j in jxbs], teachers)
            tw = set()
            for j in jxbs:
                tw |= set(trans_week_flags(j.RWJSZCDM) or [])
            teach_w = set(get_teaching_weeks(jxbs))
            rel = sorted(tw & teach_w) or sorted(teach_w) or [0]
            clist = [] if not check_class_conflict(c0) else get_class_instances1(c0, classes_all, courses)
            write_room = check_room_conflict(c0)
            # 引擎口径：教师查 rel(=责任周∩教学周)，教室/班级查教学周
            twk = sorted(teach_w) or [0]

            def tfree(day, s, h):
                return all(not str(t.timetable[w, day, p]['course']).strip()
                           for t in tobjs for w in rel for p in range(s, s + h))

            def cfree(day, s, h):
                return all(not str(cl.timetable[w, day, p]['course']).strip()
                           for cl in clist for w in twk for p in range(s, s + h))

            def rfree(rm, day, s, h):
                return all(not str(rm.timetable[w, day, p]).strip() for w in twk for p in range(s, s + h))

            # 按"整模式(多天) + 同一间教室"耦合判定，贴合引擎(一门课整段用同一教室)
            # ★ 关键：引擎块起点按 range(0,·,2) 对齐(偶数0基)，即2节格 1-2/3-4/5-6/7-8；
            #    偏好如"6-7节"(0基[5,6])跨格 → 无对齐块可落 → 偏好节次错位。
            placeable = teacher_free_any = class_blocks = room_blocks = False
            any_candidate = False
            seg_t = seg_c = seg_tc = 0
            for (days_list, hrs_list) in patterns:
                day_tc = {}   # (day,h) -> [可行起点(教师&班级都空)]
                ok_days = True
                for day, h in zip(days_list, hrs_list):
                    allowed = day_allowed_periods(pref_slots, day, eff['unavailable_time'], args.periods)
                    starts = [s for s in range(0, args.periods - h + 1, 2)
                              if all((s + k) in allowed for k in range(h))]   # 对齐偶数起点
                    if starts:
                        any_candidate = True
                    tc = []
                    for s in starts:
                        tf = tfree(day, s, h)
                        if tf:
                            teacher_free_any = True
                        cf = cfree(day, s, h) if tf else False
                        if tf and cf:
                            tc.append(s); seg_tc += 1
                        elif tf and not cf:
                            class_blocks = True; seg_c += 1
                        elif not tf:
                            seg_t += 1
                    day_tc[(day, h)] = tc
                    if not tc:
                        ok_days = False
                if not ok_days:
                    continue
                # 所有天都有教师&班级都空的起点 → 检查是否存在一间教室对每天都有可行起点
                if not write_room:
                    placeable = True; break
                for rm in rooms:
                    if all(any(rfree(rm, day, s, h) for s in tc) for (day, h), tc in day_tc.items()):
                        placeable = True; break
                if placeable:
                    break
                room_blocks = True
            ev = f'候选段: 教师占{seg_t}/班级占{seg_c}/教师&班级都空{seg_tc}; ' + \
                 ('存在整模式无空教室' if room_blocks else '教师/班级即挡住')
            if not any_candidate:
                cat = 'P2' if pref_slots else 'P0'   # 偏好窗口无对齐块可落
            elif placeable:
                cat = 'X'
            elif not teacher_free_any:
                cat = 'T1'
            elif room_blocks:
                cat = 'R1'
            elif class_blocks:
                cat = 'C1'
            else:
                cat = 'P1'

        rows.append({'教学班ID': base(jx), '课程名称': kcm, '教师号': jsh, '周学时': r['ZXS'],
                     '偏好上课时间': eff['prefer_time'], '精确类别代码': cat, '精确类别': CAT[cat],
                     '证据': ev, '教务处置方向': {
                         'T0': '补齐授课教师',
                         'R0': '补/放宽教室匹配(容量·类型·校区)',
                         'P0': '放宽偏好或拆班使窗口容得下周学时',
                         'P2': '把偏好节次改到对齐的2节格(6-7节→5-6或7-8节)',
                         'T1': '把同一教师名下扎堆的班错峰到不同时段',
                         'R1': '错峰该时段教室 或 扩教室池',
                         'C1': '错开学生班级已占的时段',
                         'P1': '放宽偏好窗口到更多天/时段',
                         'X': '人工复核(疑似可排)'}[cat]})

    out = pd.DataFrame(rows)
    out.to_excel(args.out, index=False)
    print(f'\n✅ {args.out}  共 {len(out)} 门')
    print('=== 精确分类分布 ===')
    for code, n in out['精确类别代码'].value_counts().items():
        print(f'  {code} {CAT[code]:30s}: {n}')


if __name__ == '__main__':
    main()
