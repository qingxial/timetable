# -*- coding: utf-8 -*-
"""Q2 · P3：分级要求生效（方案一 —— 维持摊平，回写时做减法）

现状：全校级(周二5-8)/类别级(全周1-2)禁排已逐行摊进 unavailable_Time。
本脚本在排课前对课程表做一次"匹配+裁决+外科减法"：
  1. 解析每门课的 PKYQMS → 取其最高级正向要求 top（L3>L2）。
  2. 把该行 unavailable_Time 拆成禁排段，按已知模式定级（周二5-8=L1、全周1-2=L2）。
  3. 若 top 级别 > 某禁排段级别 且二者时段重叠 → 从该行删掉被覆盖的禁排段（只这一行）。
  4. 把 top(placement) 翻成 Prefer_Time 绑定组；据连排块大小写 max_block/block_template 列。
  5. 产出 课程表_特殊要求生效.xlsx + 特殊要求生效变更记录.xlsx。

存量数据无一门真要违抗全校禁排 → 对现有课基本零改动；机制为未来 L3 例外就位。
用 --demo 注入一个合成 L3 体育课，实测覆盖生效。
"""
import os, sys, re, argparse
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); os.chdir(ROOT); sys.path.insert(0, 'scripts')
import pandas as pd
from special_requirements import classify, resolve, DAY2IDX, L_SCHOOL, L_CATEGORY

CONV = '智能排课基础数据/提取的基础数据表_converted'
SRC = f'{CONV}/课程表_split_merged.xlsx'
OUT = f'{CONV}/课程表_特殊要求生效.xlsx'
LOG = '排课结果/特殊要求生效变更记录.xlsx'

# 已知摊平禁排的定级模式：段文本(归一化) → 级别
FORBID_LEVEL_PATTERNS = [
    (re.compile(r'周二\(?5-8节?\)?'), L_SCHOOL),      # 全校级 周二5-8
    (re.compile(r'全周\(?1-2节?\)?'), L_CATEGORY),    # 类别级 早上1-2(体育)
]


def norm(s):
    return str(s or '').replace('（', '(').replace('）', ')').strip()


def seg_level(seg):
    for pat, lv in FORBID_LEVEL_PATTERNS:
        if pat.search(seg.replace(' ', '')):
            return lv
    return None  # 无法识别 → 视作课程自有禁排，不减


def seg_cells(seg):
    """禁排段 → 覆盖的 (day,period) 集合。period 1-based。"""
    seg = norm(seg)
    cells = set()
    m = re.match(r'^周([一二三四五六日])\(?(\d+)-(\d+)节?\)?$', seg)
    if m:
        d = DAY2IDX['周' + m.group(1)]
        for p in range(int(m.group(2)), int(m.group(3)) + 1):
            cells.add((d, p))
        return cells
    m = re.match(r'^全周\(?(\d+)-(\d+)节?\)?$', seg)
    if m:
        for d in range(5):
            for p in range(int(m.group(1)), int(m.group(2)) + 1):
                cells.add((d, p))
        return cells
    m = re.match(r'^周([一二三四五六日])全天$', seg)
    if m:
        d = DAY2IDX['周' + m.group(1)]
        for p in range(1, 12):
            cells.add((d, p))
        return cells
    return cells


def want_cells(top):
    """生效正向要求 → 想占用的 (day,period) 集合。"""
    cells = set()
    for d, blk in zip(top.allowed_days, top.fixed_periods):
        for p in range(blk[0], blk[1] + 1):
            cells.add((d, p))
    return cells


def block_template_of(top):
    """生效要求的连排块大小列表 + max_block。"""
    blocks = [blk[1] - blk[0] + 1 for blk in top.fixed_periods]
    return blocks, (max(blocks) if blocks else 2)


def process(demo=False):
    raw = pd.read_excel(SRC, dtype=str, header=None)
    hdr_cn, hdr_code = raw.iloc[0].tolist(), raw.iloc[1].tolist()
    df = pd.read_excel(SRC, dtype=str, header=1)

    if demo:
        # 注入一门合成 L3 体育课：PKYQMS 明确要周一1-2，禁排含 全周(1-2节)
        i = df.index[df['KCLB'] == '体育类'][0]
        df.at[i, 'PKYQMS'] = '周一1-2节'
        df.at[i, 'unavailable_Time'] = '周二(5-8节）;全周（1-2节）'
        df.at[i, 'Prefer_Time'] = ''
        print(f'  [demo] 注入 L3 例外于行 {i}: JXBID={df.at[i,"JXBID"]} 要求周一1-2, 禁排含全周1-2')

    # 新增引擎用列
    if 'max_block' not in df.columns:
        df['max_block'] = ''
    if 'block_template' not in df.columns:
        df['block_template'] = ''

    changes = []
    for i, r in df.iterrows():
        pk = norm(r.get('PKYQMS'))
        if not pk or pk.lower() in ('nan', 'none'):
            continue
        specs = classify(pk, r.get('KCLB', ''), r.get('JSH', ''))
        top = resolve(specs)
        if top is None or not top.allowed_days:
            continue
        W = top.level
        wanted = want_cells(top)

        # ② 连排块 → max_block/block_template
        blocks, mb = block_template_of(top)
        if mb > 2:
            df.at[i, 'max_block'] = str(mb)
            df.at[i, 'block_template'] = ','.join(map(str, blocks))
        # ① 回写 Prefer_Time 为绑定组：仅当"需要>2连排"或"当前为空"时才写，
        #    避免覆盖已填好且正常工作的存量偏好(零回归)。
        grp = top.to_group_str()
        old_pref = norm(r.get('Prefer_Time'))
        wrote_pref = ''
        if grp and (mb > 2 or not old_pref or old_pref.lower() in ('nan', 'none')):
            df.at[i, 'Prefer_Time'] = grp
            wrote_pref = grp
        # ③ 外科减法：删掉被更高级要求覆盖的低级禁排段
        un = norm(r.get('unavailable_Time'))
        segs = [s for s in re.split(r'[;；]', un) if s.strip()]
        kept, dropped = [], []
        for s in segs:
            lv = seg_level(s)
            if lv is not None and lv < W and (seg_cells(s) & wanted):
                dropped.append(s)          # 被 L{W} 覆盖，删
            else:
                kept.append(s)
        if dropped:
            df.at[i, 'unavailable_Time'] = ';'.join(kept)
        if wrote_pref or dropped or mb > 2:
            changes.append({
                'JXBID': r.get('JXBID'), '课程名称': r.get('KCM'), 'KCLB': r.get('KCLB'),
                '生效级别': f'L{W}', '生效作用域': f'{top.scope}:{top.scope_key}',
                'PKYQMS': pk, '回写Prefer_Time': wrote_pref or old_pref,
                'max_block': df.at[i, 'max_block'], 'block_template': df.at[i, 'block_template'],
                '删除的低级禁排': ';'.join(dropped), '保留禁排': ';'.join(kept) if dropped else '',
            })

    # 保留双行表头写回
    with pd.ExcelWriter(OUT, engine='openpyxl') as w:
        df.to_excel(w, index=False, startrow=2, header=False)
        ws = w.sheets['Sheet1']
        for c, (cn, code) in enumerate(zip(hdr_cn + ['偏好连排上限', '连排块模板'],
                                           hdr_code + ['max_block', 'block_template']), start=1):
            ws.cell(1, c, cn); ws.cell(2, c, code)
    chg = pd.DataFrame(changes)
    chg.to_excel(LOG, index=False)

    print(f'  产出: {OUT}')
    print(f'  变更记录: {LOG}  受影响门数: {len(changes)}')
    if len(chg):
        print('  其中触发"外科减法"(高级覆盖低级禁排)的:', (chg['删除的低级禁排'].astype(str).str.strip() != '').sum())
        print('  设了非2连排(max_block>2)的:', (chg['max_block'].astype(str).str.strip() != '').sum())
    return chg


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--demo', action='store_true', help='注入一个合成L3例外实测覆盖')
    a = ap.parse_args()
    chg = process(demo=a.demo)
    if a.demo and len(chg):
        row = chg[chg['删除的低级禁排'].astype(str).str.strip() != '']
        print('\n=== demo 覆盖验证 ===')
        if len(row):
            r = row.iloc[0]
            print(f"  课程 {r['课程名称']}({r['JXBID']}) {r['生效级别']}")
            print(f"  Prefer_Time 回写 = {r['回写Prefer_Time']}")
            print(f"  删除被覆盖禁排 = {r['删除的低级禁排']}   → L3 成功盖过 L2 ✅")
        else:
            print('  ❌ 未触发覆盖')
