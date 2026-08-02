# -*- coding: utf-8 -*-
"""软约束松弛记录（Q3）

扫描最终课表，对每一门"有时间偏好但实际未落在偏好窗口内"的课，
逐课记录松弛了哪条软约束、偏离到什么程度，并标注其松弛来源
（软偏好修复 / 调课挪位 / 首轮即偏离）。

偏离档定义（与 soft_preference_repair 一致）：
  0  在偏好窗口内（未松弛）
  1  同偏好"天"、换了节次
  2  同偏好"节次"、换了天
  3  既不同天也不同节次（完全另置）

优先级说明（任课老师 > 课程类别/学院 > 全校）：
  本期源数据 PKYQMS 无"任课老师级"特殊要求，且每门课至多一条要求，
  不存在跨级冲突；故所有课均按其唯一要求处理，无需按优先级取舍。
  该字段预留，未来多级要求并存时只保留最高级（任课老师）的那条。
"""
import os, sys, re
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); os.chdir(ROOT)
import pandas as pd

RES = '排课结果'
DAY2IDX = {'周一': 0, '周二': 1, '周三': 2, '周四': 3, '周五': 4, '周六': 5, '周日': 6}


def norm(s):
    return str(s or '').replace('（', '(').replace('）', ')').replace('，', ',').strip()


def parse_pref(cell):
    """把偏好文本解析成 {day_idx: set(可用节次0-based)}；'全天'→所有节次；无日限用 None。
    返回 (day2periods, allowed_days)。"""
    cell = norm(cell)
    if not cell:
        return {}, set()
    day2p = {}
    for seg in re.split(r'[;；]', cell):
        seg = seg.strip()
        if not seg:
            continue
        # 全周(a-b节)
        m = re.match(r'^全周\((\d+)-(\d+)节?\)$', seg)
        if m:
            a, b = int(m.group(1)) - 1, int(m.group(2)) - 1
            for d in range(5):
                day2p.setdefault(d, set()).update(range(a, b + 1))
            continue
        # 周X全天
        m = re.match(r'^(周[一二三四五六日])全天$', seg)
        if m:
            d = DAY2IDX[m.group(1)]
            day2p.setdefault(d, set()).update(range(0, 11))
            continue
        # 周X(a-b节)
        m = re.match(r'^(周[一二三四五六日])\((\d+)-(\d+)节?\)$', seg)
        if m:
            d = DAY2IDX[m.group(1)]
            a, b = int(m.group(2)) - 1, int(m.group(3)) - 1
            day2p.setdefault(d, set()).update(range(a, b + 1))
            continue
        # 周X (只有天)
        m = re.match(r'^(周[一二三四五六日])$', seg)
        if m:
            d = DAY2IDX[m.group(1)]
            day2p.setdefault(d, set()).update(range(0, 11))
    return day2p, set(day2p.keys())


def parse_actual(day_s, period_s):
    """星期'周三' 节次'第3-4节' → (day_idx, [periods 0-based])"""
    d = DAY2IDX.get(str(day_s).strip())
    m = re.match(r'^第(\d+)-(\d+)节$', str(period_s).strip())
    if d is None or not m:
        return None, []
    a, b = int(m.group(1)) - 1, int(m.group(2)) - 1
    return d, list(range(a, b + 1))


def deviation(day2p, allowed_days, aday, aperiods):
    """0 在窗口内 / 1 同天换节 / 2 同节换天 / 3 完全另置"""
    if aday is None:
        return 3
    pref_p_on_day = day2p.get(aday, set())
    if pref_p_on_day and all(p in pref_p_on_day for p in aperiods):
        return 0
    if aday in allowed_days:
        return 1  # 偏好包含这天，但节次不在窗口内
    # 换了天：看节次是否与"某个偏好天"的窗口一致（同节次换天）
    all_pref_periods = set().union(*day2p.values()) if day2p else set()
    if aperiods and all(p in all_pref_periods for p in aperiods):
        return 2
    return 3


def main():
    final = pd.read_excel(f'{RES}/调课后的整体结果_小数拆分.xlsx', dtype=str)
    soft = pd.read_excel(f'{RES}/软偏好修复明细.xlsx', dtype=str)
    soft_ids = set(soft['教学班ID'].astype(str))

    # 每门课(教学班ID)的会话行聚合：任一会话落在窗口外即视为该课时间偏好被松弛
    rows = []
    for jxbid, grp in final.groupby('教学班ID'):
        pref_cell = ''
        for v in grp['偏好上课时间']:
            if norm(v):
                pref_cell = v; break
        day2p, allowed = parse_pref(pref_cell)
        if not day2p:
            continue  # 无时间偏好 → 无软约束可松弛
        worst = 0
        detail = []
        for _, r in grp.iterrows():
            aday, ap = parse_actual(r['星期'], r['节次'])
            lv = deviation(day2p, allowed, aday, ap)
            worst = max(worst, lv)
            detail.append(f"{r['星期']}{r['节次']}(档{lv})")
        if worst == 0:
            continue  # 完全满足偏好，未松弛
        src = '软偏好修复' if str(jxbid) in soft_ids else '调课挪位/首轮偏离'
        lvname = {1: '同偏好天·换节次', 2: '同偏好节次·换天', 3: '完全另置'}[worst]
        rows.append({
            '教学班ID': jxbid,
            '课程名称': grp['课程名称'].iloc[0],
            '教师': grp['教师'].iloc[0],
            '开课院系': grp['开课院系'].iloc[0],
            '偏好上课时间': pref_cell,
            '实际落位': ' / '.join(detail),
            '松弛的软约束': '时间偏好(Prefer_Time)',
            '偏离档': worst,
            '偏离说明': lvname,
            '松弛来源': src,
            '优先级取舍': '唯一要求·无跨级冲突',
        })

    led = pd.DataFrame(rows).sort_values(['偏离档', '松弛来源'], ascending=[False, True])
    out = f'{RES}/软约束松弛记录.xlsx'
    led.to_excel(out, index=False)

    print(f'✅ {out}')
    print(f'   松弛课程总数: {len(led)}')
    if len(led):
        print('   按偏离档:', led['偏离档'].value_counts().to_dict())
        print('   按来源:', led['松弛来源'].value_counts().to_dict())


if __name__ == '__main__':
    main()
