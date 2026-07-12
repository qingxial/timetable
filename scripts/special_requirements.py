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
