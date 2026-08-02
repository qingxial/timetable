# -*- coding: utf-8 -*-
"""特殊要求分级解析（Q2 · P1）

把混在一列 PKYQMS 里的自由文本，按"作用范围的具体度"分成三级：
  L3 任课老师级 / 教学班级（最具体：裸时间要求、教师：X）
  L2 课程类别级 / 学院级 / 班级级（学院：X、班级：X、类别隐性策略）
  L1 全校级（校历禁排、全校默认）
外加"周次限制(仅单双周/仅周末)"作为正交维度（改 SKZCDM，不参与时段裁决）。

原则：级别 = 作用范围越窄越高。冲突时同一门课取 level 最大者生效，
低级规则（含类别默认禁排）被更高级要求覆盖；物理资源冲突不在此列、永不可覆盖。
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from typing import List, Optional

DAY2IDX = {'周一': 0, '周二': 1, '周三': 2, '周四': 3, '周五': 4, '周六': 5, '周日': 6}

# ---- 级别常量 ----
L_TEACHER = 3   # 任课老师级 / 教学班级（最具体）
L_CATEGORY = 2  # 课程类别级 / 学院级 / 班级级
L_SCHOOL = 1    # 全校级


@dataclass
class RequirementSpec:
    level: int                       # 3/2/1
    scope: str                       # 'teacher'|'class_group'|'college'|'category'|'course'|'school'
    scope_key: str                   # 归属实体（学院名/班级/教师号/KCLB/'全校'）
    kind: str                        # 'placement'|'week'|'forbid'
    allowed_days: List[int] = field(default_factory=list)   # 允许/指定的天(0-6)
    fixed_periods: List[List[int]] = field(default_factory=list)  # [[a,b]] 1-based闭区间
    block_template: Optional[List[int]] = None              # 连排块，如 [4] / [3,4]
    week_rule: Optional[str] = None                          # 'odd'|'weekend'|...
    source_text: str = ''

    def to_group_str(self) -> str:
        """翻译成 Prefer_Time 绑定组 [周X（a-b节）、...] 供引擎 preferred_groups 通路使用。"""
        if self.kind != 'placement' or not self.allowed_days or not self.fixed_periods:
            return ''
        idx2day = {v: k for k, v in DAY2IDX.items()}
        parts = []
        for d, blk in zip(self.allowed_days, self.fixed_periods):
            parts.append(f"{idx2day[d]}（{blk[0]}-{blk[1]}节）")
        return '[' + '、'.join(parts) + ']'


# ---------- 时间片解析 ----------
_TIME_PAT = re.compile(r'(周[一二三四五六日])\s*[（(]?\s*(\d+)\s*[.\-~至到]\s*(\d+)')


def _parse_time_slots(text: str):
    """从 '周四3.4'、'周四(3-4节)'、'周四3.4 周五5.6' 抽出 [(day_idx,[a,b]), ...]。"""
    out = []
    for m in _TIME_PAT.finditer(text):
        d = DAY2IDX[m.group(1)]
        a, b = int(m.group(2)), int(m.group(3))
        out.append((d, [min(a, b), max(a, b)]))
    return out


def _split_scope_keys(text: str) -> List[str]:
    """把 '计算机 资环' / '材料学院，外语学院' 切成实体列表。"""
    return [s for s in re.split(r'[、，,；;\s]+', text.strip()) if s]


# ---------- 主分级 ----------
def classify(pkyqms: str, kclb: str = '', jsh: str = '') -> List[RequirementSpec]:
    """把一格 PKYQMS 解析成若干 RequirementSpec（已带级别）。"""
    specs: List[RequirementSpec] = []
    text = (pkyqms or '').strip()
    if not text:
        return specs
    norm = text.replace('（', '(').replace('）', ')').replace('：', ':')

    # 1) 周次限制（正交维度）——'仅单周/仅双周/仅周六周日/仅周末'
    if norm.startswith('仅'):
        rule = None
        if '单周' in norm:
            rule = 'odd'
        elif '双周' in norm:
            rule = 'even'
        elif ('周六' in norm and '周日' in norm) or '周末' in norm:
            rule = 'weekend'
        specs.append(RequirementSpec(L_SCHOOL, 'course', '本课', 'week',
                                     week_rule=rule, source_text=text))
        return specs

    # 2) 教师级（L3）——'教师:X' / '老师:X'
    m = re.match(r'^(教师|老师)\s*:?\s*(.*?)(?:[;；]|时间|$)', norm)
    if norm.startswith('教师') or norm.startswith('老师'):
        slots = _parse_time_slots(norm)
        specs.append(RequirementSpec(
            L_TEACHER, 'teacher', (m.group(2).strip() if m else jsh) or jsh, 'placement',
            allowed_days=[d for d, _ in slots], fixed_periods=[p for _, p in slots],
            source_text=text))
        return specs

    # 3) 学院级（L2）——'学院:A，B；时间：周四3.4'
    if norm.startswith('学院'):
        body = re.sub(r'^学院\s*:?', '', norm)
        scope_part = re.split(r'[;；]|时间', body, maxsplit=1)[0]
        keys = _split_scope_keys(scope_part)
        slots = _parse_time_slots(body)
        specs.append(RequirementSpec(
            L_CATEGORY, 'college', ','.join(keys), 'placement',
            allowed_days=[d for d, _ in slots], fixed_periods=[p for _, p in slots],
            source_text=text))
        return specs

    # 4) 班级级（L2）——'班级:机械学院，周五5.6'
    if norm.startswith('班级'):
        body = re.sub(r'^班级\s*:?', '', norm)
        slots = _parse_time_slots(body)
        scope_part = re.split(r'[,，]', body, maxsplit=1)[0]
        specs.append(RequirementSpec(
            L_CATEGORY, 'class_group', scope_part.strip(), 'placement',
            allowed_days=[d for d, _ in slots], fixed_periods=[p for _, p in slots],
            source_text=text))
        return specs

    # 5) 裸时间要求（无作用域前缀）→ 只作用于本教学班，最具体 = L3
    slots = _parse_time_slots(norm)
    if slots:
        specs.append(RequirementSpec(
            L_TEACHER, 'course', '本教学班', 'placement',
            allowed_days=[d for d, _ in slots], fixed_periods=[p for _, p in slots],
            source_text=text))
        return specs

    # 6) 兜底：无法结构化 → 交 LLM（此处先留占位，标 level=None 语义用 -1）
    specs.append(RequirementSpec(-1, 'unknown', '', 'placement', source_text=text))
    return specs


# ---------- 策略声明解析（L1/L2 一句话规则）----------
# 时段词 → 节次闭区间（有显式数字时以数字为准，词仅辅助）
_DAYPART = {'早上': (1, 2), '上午': (1, 4), '中午': (5, 5), '下午': (5, 8), '晚上': (9, 11), '傍晚': (9, 11)}


def parse_policy(sentence: str, colleges: Optional[List[str]] = None,
                 categories: Optional[List[str]] = None) -> RequirementSpec:
    """把一句自然语言校级/类别级策略解析成带级别的 RequirementSpec。

    级别由"作用域词"自动决定：
      含'全校/全体/所有' → L1 全校
      开头是某'课程类别名'(KCLB，如 体育类) → L2 类别
      开头是某'学院名' → L2 学院
    极性：含'不排/禁排/不上/不安排' → forbid；否则 placement。
    """
    text = (sentence or '').strip().replace('（', '(').replace('）', ')')
    forbid = any(k in text for k in ('不排', '禁排', '不上', '不安排', '避免'))
    kind = 'forbid' if forbid else 'placement'

    # 作用域定级
    level, scope, scope_key = L_CATEGORY, 'category', ''
    if any(k in text for k in ('全校', '全体', '所有课', '全部课')):
        level, scope, scope_key = L_SCHOOL, 'school', '全校'
    else:
        for c in (categories or []):
            if c and c in text:
                level, scope, scope_key = L_CATEGORY, 'category', c
                break
        else:
            for col in (colleges or []):
                if col and col in text:
                    level, scope, scope_key = L_CATEGORY, 'college', col
                    break

    # 时段：优先取显式 '周X a-b' / 'a-b节'；否则用时段词
    slots = _parse_time_slots(text)
    if not slots:
        # 无"周X"，找裸 'a-b节' + 天（无天→工作日全周）
        m = re.search(r'(\d+)\s*[.\-~至到]\s*(\d+)\s*节', text)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            days = [d for d in range(5)]  # 无指定天 → 工作日
            # 若句中点名某天
            named = [DAY2IDX[k] for k in DAY2IDX if k in text]
            if named:
                days = named
            slots = [(d, [min(a, b), max(a, b)]) for d in days]
        else:
            for w, (a, b) in _DAYPART.items():
                if w in text:
                    named = [DAY2IDX[k] for k in DAY2IDX if k in text] or list(range(5))
                    slots = [(d, [a, b]) for d in named]
                    break

    return RequirementSpec(level, scope, scope_key, kind,
                           allowed_days=[d for d, _ in slots],
                           fixed_periods=[p for _, p in slots], source_text=sentence)


def resolve(specs: List[RequirementSpec]) -> Optional[RequirementSpec]:
    """同一门课的多条要求裁决：placement 取 level 最大者（同级取首条）。"""
    placements = [s for s in specs if s.kind == 'placement' and s.level is not None and s.level > 0]
    if not placements:
        return None
    maxlv = max(s.level for s in placements)
    top = [s for s in placements if s.level == maxlv]
    return top[0]


# ==========================================================================
# 统一优先级解析 resolve_effective（Q2 高屋建瓴版）——首轮排课与调课共用同一规则
# --------------------------------------------------------------------------
# 一条原理，两条规则，适用于每一门课（不再分桶打补丁）：
#   规则① 具体度优先：课程自身 Prefer_Time = L3（最具体）；摊在其行上的通用禁排
#          (全周1-2=L2类别 / 周二5-8=L1全校) 更粗。冲突→具体者胜→就地解除被覆盖的禁排段。
#   规则② 连排块 = 周学时 ÷ 可用天：装得下 2 连排；天被压缩(如仅周末)则放大到 4/3 连排。
# ==========================================================================
import math as _math

# 已知"摊平在每行禁排列"的通用禁排 → 级别
_FLAT_FORBID_LEVEL = [
    (re.compile(r'周二\(?5-8节?\)?'), L_SCHOOL),     # 全校级
    (re.compile(r'全周\(?1-2节?\)?'), L_CATEGORY),   # 类别级(体育早上)
]


def _norm2(s):
    return str(s or '').replace('（', '(').replace('）', ')').strip()


def _parse_pref_slots(prefer_time):
    """Prefer_Time → [(day_idx, set(1-based periods))]。支持 周X(a-b节)/周X全天/全周(a-b节)。"""
    out = []
    for seg in re.split(r'[;；、]', _norm2(prefer_time)):
        seg = seg.strip()
        if not seg:
            continue
        m = re.match(r'^周([一二三四五六日])\((\d+)-(\d+)节?\)$', seg)
        if m:
            out.append((DAY2IDX['周' + m.group(1)], set(range(int(m.group(2)), int(m.group(3)) + 1)))); continue
        m = re.match(r'^周([一二三四五六日])全天$', seg)
        if m:
            out.append((DAY2IDX['周' + m.group(1)], set(range(1, 12)))); continue
        m = re.match(r'^全周\((\d+)-(\d+)节?\)$', seg)
        if m:
            for d in range(5):
                out.append((d, set(range(int(m.group(1)), int(m.group(2)) + 1))))
    return out


def _forbid_seg_level(seg):
    for pat, lv in _FLAT_FORBID_LEVEL:
        if pat.search(seg.replace(' ', '')):
            return lv
    return None  # 未识别 → 视作课程自有禁排(同级)，保留


def _forbid_seg_cells(seg):
    seg = _norm2(seg)
    m = re.match(r'^周([一二三四五六日])\((\d+)-(\d+)节?\)$', seg)
    if m:
        d = DAY2IDX['周' + m.group(1)]
        return {(d, p) for p in range(int(m.group(2)), int(m.group(3)) + 1)}
    m = re.match(r'^全周\((\d+)-(\d+)节?\)$', seg)
    if m:
        return {(d, p) for d in range(5) for p in range(int(m.group(1)), int(m.group(2)) + 1)}
    m = re.match(r'^周([一二三四五六日])全天$', seg)
    if m:
        d = DAY2IDX['周' + m.group(1)]
        return {(d, p) for p in range(1, 12)}
    return set()


def compute_block_template(hours, ndays):
    """规则②：把 hours 节按 ndays 天均分成连排块（带余靠前），返回 (max_block, template)。
    装得下(≤2/天)则返回 (2, None) 保持默认。"""
    hours = int(_math.ceil(hours)); nd = max(1, ndays)
    if nd * 2 >= hours:
        return 2, None
    q, r = divmod(hours, nd)
    tmpl = [q + 1] * r + [q] * (nd - r)
    return max(tmpl), tmpl


def resolve_effective(prefer_time, unavailable_time, zxs):
    """对一门课解析出"生效约束"。首轮排课与调课共用。
    返回 {prefer_time, unavailable_time, max_block, block_template, dropped_forbids}。"""
    pref_slots = _parse_pref_slots(prefer_time)
    pref_cells = {(d, p) for d, ps in pref_slots for p in ps}
    pref_days = sorted({d for d, _ in pref_slots})

    # 规则① 具体度优先：删被自身偏好(L3)覆盖的低级禁排段
    kept, dropped = [], []
    for seg in [s for s in re.split(r'[;；]', _norm2(unavailable_time)) if s.strip()]:
        lv = _forbid_seg_level(seg)
        if lv is not None and lv < L_TEACHER and (_forbid_seg_cells(seg) & pref_cells):
            dropped.append(seg)
        else:
            kept.append(seg)
    new_un = ';'.join(kept)

    # 规则② 连排块 = 周学时 ÷ 可用偏好天
    mb, tmpl = 2, None
    try:
        z = float(zxs)
    except (TypeError, ValueError):
        z = 0
    if pref_days and z > 0:
        mb, tmpl = compute_block_template(z, len(pref_days))

    return {'prefer_time': _norm2(prefer_time), 'unavailable_time': new_un,
            'max_block': mb, 'block_template': tmpl, 'dropped_forbids': dropped}


if __name__ == '__main__':
    import os, sys
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, ROOT); os.chdir(ROOT)
    import pandas as pd
    from collections import Counter

    df = pd.read_excel('智能排课基础数据/提取的基础数据表_converted/课程表_split_merged.xlsx',
                       dtype=str, header=1)
    lv_ct, kind_ct, unresolved = Counter(), Counter(), []
    rows = []
    for _, r in df.iterrows():
        pk = str(r.get('PKYQMS') or '').strip()
        if not pk or pk.lower() in ('nan', 'none', 'null'):
            continue
        specs = classify(pk, r.get('KCLB', ''), r.get('JSH', ''))
        for s in specs:
            lv_ct[s.level] += 1
            kind_ct[s.kind] += 1
            if s.level == -1:
                unresolved.append(pk)
        top = resolve(specs)
        rows.append({'JXBID': r.get('JXBID'), 'KCLB': r.get('KCLB'), 'PKYQMS': pk,
                     '生效级别': top.level if top else ('周次限制' if specs and specs[0].kind == 'week' else ''),
                     '生效作用域': f'{top.scope}:{top.scope_key}' if top else '',
                     '绑定组': top.to_group_str() if top else ''})
    lvname = {3: 'L3任课老师/教学班', 2: 'L2类别/学院/班级', 1: 'L1全校(周次限制)', -1: '未解析(交LLM)'}
    print('=== 分级结果（真实 PKYQMS）===')
    for lv, n in sorted(lv_ct.items(), reverse=True):
        print(f'  {lvname.get(lv, lv):22s}: {n}')
    print('=== kind 分布 ===', dict(kind_ct))
    print('=== 未能结构化解析(需LLM兜底) ===', len(unresolved))
    for u in unresolved[:10]:
        print('   ·', u[:70])
    out = '排课结果/特殊要求分级结果.xlsx'
    pd.DataFrame(rows).to_excel(out, index=False)
    print('已写出:', out, ' 行:', len(rows))
