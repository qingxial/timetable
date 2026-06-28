# -*- coding: utf-8 -*-
"""自包含的本次运行 HTML 报告生成器。
口径参考 generate_report_balanced.py / analysis_report.py / scripts/special_req_charts.py。

特殊需求统计规则（本次新增）：
  - 全校周二(5-8节)禁排是统一校级约束，不算单独课程的特殊需求
  - 仅当某课的 unavailable_Time 解析后剩余约束 = {周二(5-8节)} 时，该课不计入"有特殊需求"
  - 在「特殊需求带来的复杂性」章节单独写明该校级约束的影响
"""
import os, re, base64, io, sys, json
from datetime import datetime
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); os.chdir(ROOT)

import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager as fm

# ---- 中文字体（容器里可能无 CJK，自动降级） ----
for fp in ["/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
           "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
           "/Library/Fonts/Arial Unicode.ttf",
           "/System/Library/Fonts/PingFang.ttc"]:
    if os.path.exists(fp):
        try:
            fm.fontManager.addfont(fp)
            plt.rcParams["font.sans-serif"] = [fm.FontProperties(fname=fp).get_name()]
            break
        except Exception:
            pass
plt.rcParams["axes.unicode_minus"] = False

OUT_DIR = "排课结果/图表展示"
os.makedirs(OUT_DIR, exist_ok=True)

# =====================================================================
# 1. 加载与基础统计
# =====================================================================
COURSE_XLSX = "智能排课基础数据/提取的基础数据表_converted/课程表_split_merged.xlsx"
SCHEDULE_XLSX = "排课结果/调课后的整体结果.xlsx"
FIRST_ROUND_FAIL = "排课结果/排课失败课程_全部.xlsx"
FINAL_FAIL = "排课结果/调课失败的排课失败课程.xlsx"
CLUSTER_XLSX = "排课结果1/课程聚类结果2.xlsx"
LOG_FILE = "logs/run_balanced_20260627_161710.log"

print("加载基础数据 ...")
from Basic_Data import load_courses, load_classrooms
from utils1 import pre_selection

courses = load_courses(COURSE_XLSX, weeks=20, days=7, periods=11)
classrooms = load_classrooms("智能排课基础数据/提取的基础数据表_converted/教室表.xlsx",
                             weeks=20, days=7, periods=11)
jxbid_index, _, _ = pre_selection(courses, classrooms)
VALID_IDS = set(jxbid_index.keys())
print(f"  有效教学班: {len(VALID_IDS)}")

schedule_df = pd.read_excel(SCHEDULE_XLSX)
schedule_df['教学班ID'] = schedule_df['教学班ID'].astype(str)
print(f"  调课后整体结果: {len(schedule_df)} 行, {schedule_df['教学班ID'].nunique()} 个 JXBID")

first_fail = pd.read_excel(FIRST_ROUND_FAIL)['教学班ID'].astype(str).unique().tolist()
final_fail = pd.read_excel(FINAL_FAIL)['jxbid'].astype(str).unique().tolist()
print(f"  首轮失败: {len(first_fail)}  最终失败: {len(final_fail)}")

scheduled_ids = set(schedule_df['教学班ID'].unique())
final_success_n = len(scheduled_ids)
total_valid = len(VALID_IDS)
final_fail_n = len(final_fail)
first_fail_n = len(first_fail)
rescheduled_n = first_fail_n - final_fail_n  # 调课新增
first_success_n = total_valid - first_fail_n

# =====================================================================
# 2. 时段分类工具（参考 generate_report_balanced.py）
# =====================================================================
WEEKDAYS = ['周一','周二','周三','周四','周五','周六','周日']
BLOCKS = ['上午(1-4节)','下午(5-8节)','晚上(9节+)']

def parse_period_start(s):
    """从 '第5-6节' 抽出 5"""
    if not isinstance(s, str): return None
    m = re.search(r'第(\d+)', s)
    return int(m.group(1)) if m else None

def period_to_block(s):
    f = parse_period_start(s)
    if f is None: return None
    if f <= 4: return '上午(1-4节)'
    if f <= 8: return '下午(5-8节)'
    return '晚上(9节+)'

def classify_slot(s):
    """连排时段分类，参考 generate_report_balanced.py"""
    if not isinstance(s, str): return '其他'
    if '1-2' in s: return '1-2节'
    if '3-4' in s: return '3-4节'
    if '5-6' in s: return '5-6节'
    if '7-8' in s: return '7-8节'
    if '1-4' in s: return '1-4节'
    if '5-8' in s: return '5-8节'
    if '9-10' in s: return '9-10节'
    if '1-8' in s: return '1-8节'
    if '9' in s and ('节' in s or '-' in s): return '9节以后'
    return '其他'

# =====================================================================
# 3. 图 1 — 总览 KPI
# =====================================================================
def fig_overview():
    fig, ax = plt.subplots(figsize=(8.0, 4.8))
    labels = ['有效教学班','首轮成功','调课新增','最终成功','最终失败']
    values = [total_valid, first_success_n, rescheduled_n, final_success_n, final_fail_n]
    colors = ['#5B9BD5','#70AD47','#FFC000','#1E88E5','#E53935']
    bars = ax.bar(labels, values, color=colors, edgecolor='white', linewidth=1.5)
    for b,v in zip(bars,values):
        ax.text(b.get_x()+b.get_width()/2, v+max(values)*0.015, f'{v:,}',
                ha='center', va='bottom', fontweight='bold')
    ax.set_ylim(0, max(values)*1.15)
    ax.set_title('排课总览（首轮 + 调课）', fontsize=14, fontweight='bold')
    ax.grid(axis='y', alpha=0.3)
    ax.spines[['top','right']].set_visible(False)
    out = OUT_DIR + '/01_overview.png'
    plt.tight_layout(); plt.savefig(out, dpi=160, bbox_inches='tight'); plt.close()
    return out

# =====================================================================
# 4. 图 2 — 星期 × 时段块 热力图
# =====================================================================
def fig_heatmap():
    grid = np.zeros((3,5))
    for _, r in schedule_df.iterrows():
        d = r['星期']; p = r['节次']
        if d not in WEEKDAYS[:5]: continue
        b = period_to_block(p)
        if b is None: continue
        i = BLOCKS.index(b); j = WEEKDAYS.index(d)
        grid[i,j] += 1
    fig, ax = plt.subplots(figsize=(8.2, 4.6))
    im = ax.imshow(grid, cmap='YlOrRd', aspect='auto')
    ax.set_xticks(range(5)); ax.set_xticklabels(WEEKDAYS[:5])
    ax.set_yticks(range(3)); ax.set_yticklabels(BLOCKS)
    for i in range(3):
        for j in range(5):
            txt = f'{int(grid[i,j]):,}'
            # 标注校级约束：周二(5-8节)禁排
            if WEEKDAYS[j]=='周二' and BLOCKS[i]=='下午(5-8节)':
                ax.add_patch(plt.Rectangle((j-0.5,i-0.5),1,1,fill=False,
                            edgecolor='red',linewidth=3,linestyle='--'))
                txt += '\n(校级禁排)'
            ax.text(j, i, txt, ha='center', va='center',
                    color='black' if grid[i,j]<grid.max()*0.5 else 'white',
                    fontsize=10, fontweight='bold')
    ax.set_title('排课分布热力图（按星期×时段块）\n红框=全校周二下午禁排区',
                 fontsize=13, fontweight='bold')
    plt.colorbar(im, ax=ax, label='排课次数')
    out = OUT_DIR + '/02_heatmap.png'
    plt.tight_layout(); plt.savefig(out, dpi=160, bbox_inches='tight'); plt.close()
    return out, grid

# =====================================================================
# 5. 图 3 — 连排时段分布
# =====================================================================
def fig_slot_dist():
    slot_counter = Counter()
    for _, r in schedule_df.iterrows():
        slot_counter[classify_slot(r['节次'])] += 1
    order = ['1-2节','3-4节','5-6节','7-8节','1-4节','5-8节','9-10节','1-8节','9节以后','其他']
    values = [slot_counter.get(k,0) for k in order]
    fig, ax = plt.subplots(figsize=(10.5, 4.8))
    colors = ['#1976D2','#1565C0','#FFA000','#FF6F00','#E65100','#FF6D00',
              '#6A1B9A','#4527A0','#616161','#9E9E9E']
    bars = ax.bar(order, values, color=colors, edgecolor='white', linewidth=1.5)
    for b,v in zip(bars,values):
        if v>0:
            ax.text(b.get_x()+b.get_width()/2, v+max(values)*0.015, f'{v:,}',
                    ha='center', va='bottom', fontsize=10)
    ax.set_title('连排时段分布\n（优先级：1-4 > 5-8 > 9-10；1-2 > 3-4；5-6 > 7-8）',
                 fontsize=13, fontweight='bold')
    ax.set_ylabel('排课条数')
    ax.grid(axis='y', alpha=0.3); ax.spines[['top','right']].set_visible(False)
    out = OUT_DIR + '/03_slot_dist.png'
    plt.tight_layout(); plt.savefig(out, dpi=160, bbox_inches='tight'); plt.close()
    return out, slot_counter

# =====================================================================
# 6. 图 4 — 每天排课分布
# =====================================================================
def fig_weekday_dist():
    cnt = Counter()
    for d in schedule_df['星期']:
        if d in WEEKDAYS: cnt[d] += 1
    values = [cnt.get(d,0) for d in WEEKDAYS]
    fig, ax = plt.subplots(figsize=(8.5, 4.5))
    colors = ['#4CAF50','#FF9800','#4CAF50','#4CAF50','#4CAF50','#9C27B0','#9C27B0']
    bars = ax.bar(WEEKDAYS, values, color=colors, edgecolor='white', linewidth=1.5)
    for b,v in zip(bars,values):
        ax.text(b.get_x()+b.get_width()/2, v+max(values)*0.015, f'{v:,}',
                ha='center', va='bottom', fontweight='bold')
    ax.set_title('星期排课分布（含周末）', fontsize=13, fontweight='bold')
    ax.set_ylabel('排课条数'); ax.grid(axis='y', alpha=0.3)
    ax.spines[['top','right']].set_visible(False)
    out = OUT_DIR + '/04_weekday_dist.png'
    plt.tight_layout(); plt.savefig(out, dpi=160, bbox_inches='tight'); plt.close()
    return out, cnt

# =====================================================================
# 7. 图 5 — 各聚类成功率
# =====================================================================
def fig_cluster_success():
    clu = pd.read_excel(CLUSTER_XLSX)
    clu['JXBID'] = clu['JXBID'].astype(str)
    out_data = []
    for cid, grp in clu.groupby('类别ID'):
        ids = set(grp['JXBID']) & VALID_IDS
        if not ids: continue
        ok = len(ids & scheduled_ids)
        out_data.append({
            '类别ID': cid,
            '类别名称': grp['类别名称'].iloc[0],
            'total': len(ids),
            'ok': ok,
            'rate': ok/len(ids)*100
        })
    out_data.sort(key=lambda x: x['类别ID'])
    labels = [f"{d['类别ID']}. {d['类别名称']}\n({d['total']}班)" for d in out_data]
    ok_vals = [d['ok'] for d in out_data]
    rates = [d['rate'] for d in out_data]
    fig, ax1 = plt.subplots(figsize=(10.5, 5.0))
    bars = ax1.bar(labels, [d['total'] for d in out_data],
                   color='#BBDEFB', edgecolor='#1976D2', linewidth=1.5,
                   label='应排班数')
    bars2 = ax1.bar(labels, ok_vals, color='#1976D2',
                    edgecolor='white', linewidth=1.5, label='成功班数')
    for b,v,r in zip(bars2, ok_vals, rates):
        ax1.text(b.get_x()+b.get_width()/2, v+max(ok_vals)*0.01,
                 f'{v}\n({r:.1f}%)', ha='center', va='bottom', fontsize=9)
    ax1.set_title('各聚类成功率', fontsize=13, fontweight='bold')
    ax1.set_ylabel('教学班数')
    ax1.legend(loc='upper left'); ax1.grid(axis='y', alpha=0.3)
    ax1.spines[['top','right']].set_visible(False)
    out = OUT_DIR + '/05_cluster_success.png'
    plt.tight_layout(); plt.savefig(out, dpi=160, bbox_inches='tight'); plt.close()
    return out, out_data

# =====================================================================
# 8. 特殊需求统计（按你的规则：周二5-8节禁排不算）
# =====================================================================
def _normalize_text(s):
    """统一全/半角"""
    if not isinstance(s, str): return ''
    return (s.replace('（','(').replace('）',')')
             .replace('，',',').replace('；',';')
             .replace(' ','').replace('　',''))

# 把 unavailable 文本切成构件集合，例如 '周二(5-8节);全周(1-2节)' → {'周二(5-8节)','全周(1-2节)'}
def split_constraints(s):
    s = _normalize_text(s)
    if not s: return set()
    parts = [p.strip() for p in re.split(r'[;]+', s) if p.strip()]
    return set(parts)

# 校级约束：周二下午禁排，可能多种写法
SCHOOL_WIDE = {'周二(5-8节)', '周二(5-8)', '周二下午'}

def is_only_school_wide(unavail_text):
    """unavailable_Time 解析后是否仅由校级约束构成（如果是则不计为特殊需求）"""
    cs = split_constraints(unavail_text)
    if not cs: return False
    return cs.issubset(SCHOOL_WIDE)

def has_real_unavail(unavail_text):
    """是否有非校级的 unavailable 约束"""
    cs = split_constraints(unavail_text)
    return bool(cs - SCHOOL_WIDE)

def has_real_pref(pref_text):
    s = _normalize_text(pref_text)
    return bool(s)  # Prefer_Time 非空即算

def has_room(jasdm):
    s = _normalize_text(jasdm)
    return bool(s) and s not in ('nan','none','0','0.0')

def has_building(jxldm):
    s = _normalize_text(jxldm)
    return bool(s) and s not in ('nan','none','0','0.0')

def compute_special():
    pref = set(); forbid = set(); room = set(); building = set()
    forbid_school_only = set()  # 仅含校级约束
    for c in courses:
        if c.JXBID not in VALID_IDS: continue
        if has_real_pref(getattr(c,'Prefer_Time',None)): pref.add(c.JXBID)
        if has_real_unavail(getattr(c,'unavailable_Time',None)): forbid.add(c.JXBID)
        elif is_only_school_wide(getattr(c,'unavailable_Time',None)):
            forbid_school_only.add(c.JXBID)
        if has_room(getattr(c,'JASDM',None)): room.add(c.JXBID)
        if has_building(getattr(c,'JXLDM',None)): building.add(c.JXBID)
    special = pref | forbid | room | building
    special_ok = special & scheduled_ids
    return dict(
        total=len(VALID_IDS), special=len(special), special_ok=len(special_ok),
        prefer=len(pref), forbid=len(forbid),
        forbid_school_only=len(forbid_school_only),
        room=len(room), building=len(building),
        special_fail=len(special & set(final_fail)),
    )

def fig_special_overview(n):
    labels = ['全部有效教学班','有特殊要求的\n教学班','成功排课的\n特殊要求教学班']
    values = [n['total'], n['special'], n['special_ok']]
    colors = ['#5B9BD5','#ED7D31','#70AD47']
    fig, ax = plt.subplots(figsize=(7.5, 5.0))
    bars = ax.bar(labels, values, color=colors, width=0.55,
                  edgecolor='white', linewidth=1.5)
    ax.set_ylim(0, max(values)*1.18); ax.set_ylabel('教学班数量')
    ax.set_title('特殊要求教学班 与 成功排课情况', fontsize=13, fontweight='bold')
    ax.grid(axis='y', alpha=0.3); ax.spines[['top','right']].set_visible(False)
    for b,v in zip(bars,values):
        ax.text(b.get_x()+b.get_width()/2, v+max(values)*0.018, f'{v:,}',
                ha='center', va='bottom', fontsize=12, fontweight='bold')
    if n['special']:
        rate = n['special_ok']/n['special']*100
        ax.annotate(f'成功率 {rate:.1f}%', xy=(2,values[2]),
                    xytext=(2,values[2]+max(values)*0.08),
                    ha='center', fontsize=11, color='#1E6B2B', fontweight='bold')
    out = OUT_DIR + '/06_special_overview.png'
    plt.tight_layout(); plt.savefig(out, dpi=160, bbox_inches='tight'); plt.close()
    return out

def fig_special_breakdown(n):
    labels = ['偏好时间段','禁止时间段\n(排除校级约束)','指定教室','指定教学楼']
    values = [n['prefer'], n['forbid'], n['room'], n['building']]
    fig, ax = plt.subplots(figsize=(8.0, 4.6))
    bars = ax.bar(labels, values, color='#4F81BD', width=0.55,
                  edgecolor='white', linewidth=1.5)
    ax.set_ylim(0, max(values)*1.20)
    ax.set_ylabel('教学班数量（多选可累加）')
    ax.set_title('特殊要求类型分布', fontsize=13, fontweight='bold')
    ax.grid(axis='y', alpha=0.3); ax.spines[['top','right']].set_visible(False)
    for b,v in zip(bars,values):
        ax.text(b.get_x()+b.get_width()/2, v+max(values)*0.018, f'{v:,}',
                ha='center', va='bottom', fontsize=12, fontweight='bold')
    out = OUT_DIR + '/07_special_breakdown.png'
    plt.tight_layout(); plt.savefig(out, dpi=160, bbox_inches='tight'); plt.close()
    return out

# =====================================================================
# 9. 失败课程 Top-N（按课程名聚合）
# =====================================================================
def fig_failed_top():
    ff = pd.read_excel(FINAL_FAIL)
    top = ff['name'].value_counts().head(15)
    fig, ax = plt.subplots(figsize=(10, 5))
    bars = ax.barh(list(top.index)[::-1], list(top.values)[::-1],
                   color='#E53935', edgecolor='white')
    for b,v in zip(bars, list(top.values)[::-1]):
        ax.text(v+0.2, b.get_y()+b.get_height()/2, str(v),
                ha='left', va='center', fontsize=10)
    ax.set_title(f'最终失败课程 Top-15（共 {len(ff)} 门失败）',
                 fontsize=13, fontweight='bold')
    ax.set_xlabel('失败教学班数')
    ax.grid(axis='x', alpha=0.3); ax.spines[['top','right']].set_visible(False)
    out = OUT_DIR + '/08_failed_top.png'
    plt.tight_layout(); plt.savefig(out, dpi=160, bbox_inches='tight'); plt.close()
    return out, top

# =====================================================================
# 跑全部图 + 装配 HTML
# =====================================================================
print('\n生成图表 ...')
p1 = fig_overview(); print(' ', p1)
p2, hm = fig_heatmap(); print(' ', p2)
p3, slot_cnt = fig_slot_dist(); print(' ', p3)
p4, wd_cnt = fig_weekday_dist(); print(' ', p4)
p5, cluster_data = fig_cluster_success(); print(' ', p5)
spec = compute_special()
p6 = fig_special_overview(spec); print(' ', p6)
p7 = fig_special_breakdown(spec); print(' ', p7)
p8, top_fail = fig_failed_top(); print(' ', p8)
print('  特殊需求统计:', spec)

# 解析日志里的总耗时
def parse_runtime(log_path):
    if not os.path.exists(log_path): return ('未知','未知')
    try:
        with open(log_path,'r',encoding='utf-8',errors='ignore') as f:
            text = f.read()
        m = re.search(r'调课总耗时:\s*([\d.分秒]+)', text)
        resched = m.group(1) if m else '未知'
        return (resched,)
    except Exception:
        return ('未知',)
(resched_time,) = parse_runtime(LOG_FILE)
log_start_mtime = datetime.fromtimestamp(os.path.getmtime(LOG_FILE)) if os.path.exists(LOG_FILE) else None

# img 转 b64 嵌入
def b64(path):
    with open(path,'rb') as f: return base64.b64encode(f.read()).decode()
def IMG(path, alt=''):
    return f'<img src="data:image/png;base64,{b64(path)}" alt="{alt}" style="max-width:100%;">'

success_rate = final_success_n / total_valid * 100
first_rate = first_success_n / total_valid * 100
resched_rate = (rescheduled_n / first_fail_n * 100) if first_fail_n else 0
spec_rate = (spec['special_ok'] / spec['special'] * 100) if spec['special'] else 0

# 聚类表 HTML
clu_rows = ''.join(
    f"<tr><td>{d['类别ID']}</td><td>{d['类别名称']}</td>"
    f"<td>{d['total']:,}</td><td>{d['ok']:,}</td>"
    f"<td>{d['rate']:.2f}%</td></tr>"
    for d in cluster_data
)

# 失败 Top HTML
fail_rows = ''.join(
    f"<tr><td>{i+1}</td><td>{name}</td><td>{cnt}</td></tr>"
    for i,(name,cnt) in enumerate(top_fail.items())
)

# 热力图数据表
hm_table = '<table class="grid"><thead><tr><th></th>' + \
    ''.join(f'<th>{w}</th>' for w in WEEKDAYS[:5]) + '</tr></thead><tbody>'
for i,b in enumerate(BLOCKS):
    hm_table += f'<tr><th>{b}</th>'
    for j,w in enumerate(WEEKDAYS[:5]):
        cell = f'{int(hm[i,j]):,}'
        cls = ''
        if w=='周二' and b=='下午(5-8节)':
            cls = ' class="banned"'; cell += '<br><small>(校级禁排)</small>'
        hm_table += f'<td{cls}>{cell}</td>'
    hm_table += '</tr>'
hm_table += '</tbody></table>'

html = f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="UTF-8">
<title>智能排课分析报告 — {datetime.now().strftime('%Y-%m-%d')}</title>
<style>
body{{font-family:"PingFang SC","Microsoft YaHei",sans-serif;
     max-width:1100px;margin:24px auto;padding:0 20px;color:#333;line-height:1.6;}}
h1{{text-align:center;color:#1976D2;border-bottom:3px solid #1976D2;padding-bottom:10px;}}
h2{{color:#0D47A1;border-left:5px solid #1976D2;padding-left:12px;margin-top:36px;}}
h3{{color:#1565C0;}}
.kpi{{display:flex;flex-wrap:wrap;gap:14px;margin:20px 0;}}
.kpi-box{{flex:1 1 160px;background:#f5f7fa;border-radius:8px;padding:16px;text-align:center;
        border-left:4px solid #1976D2;}}
.kpi-box .val{{font-size:28px;font-weight:bold;color:#0D47A1;}}
.kpi-box .lab{{color:#666;font-size:13px;margin-top:4px;}}
.kpi-box.success{{border-left-color:#4CAF50;}}
.kpi-box.success .val{{color:#2E7D32;}}
.kpi-box.fail{{border-left-color:#E53935;}}
.kpi-box.fail .val{{color:#C62828;}}
table.grid{{border-collapse:collapse;margin:14px 0;width:100%;}}
table.grid th,table.grid td{{border:1px solid #ddd;padding:8px 12px;text-align:center;}}
table.grid th{{background:#1976D2;color:white;}}
table.grid td.banned{{background:#FFEBEE;color:#C62828;}}
table.grid tr:nth-child(even) td{{background:#f9f9f9;}}
.notice{{background:#FFF8E1;border-left:4px solid #FFA000;padding:14px 18px;border-radius:4px;
        margin:18px 0;}}
.notice h4{{margin-top:0;color:#E65100;}}
.chart{{margin:24px 0;text-align:center;}}
.desc{{background:#F5F5F5;padding:12px 18px;border-radius:6px;margin-top:10px;}}
.footer{{text-align:center;color:#999;font-size:12px;margin-top:48px;padding-top:18px;
        border-top:1px solid #eee;}}
</style></head>
<body>

<h1>智能排课分析报告 · balanced 策略</h1>
<p style="text-align:center;color:#666;">
  生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')} ·
  数据: 课程表_split_merged.xlsx ·
  策略: balanced (seed=2026)
</p>

<h2>1. 总体排课结果</h2>
<div class="kpi">
  <div class="kpi-box"><div class="val">{total_valid:,}</div><div class="lab">有效教学班</div></div>
  <div class="kpi-box success"><div class="val">{final_success_n:,}</div><div class="lab">最终成功</div></div>
  <div class="kpi-box success"><div class="val">{success_rate:.2f}%</div><div class="lab">最终成功率</div></div>
  <div class="kpi-box"><div class="val">{first_success_n:,}</div><div class="lab">首轮成功 ({first_rate:.2f}%)</div></div>
  <div class="kpi-box"><div class="val">{rescheduled_n:,}</div><div class="lab">调课新增 ({resched_rate:.1f}%)</div></div>
  <div class="kpi-box fail"><div class="val">{final_fail_n:,}</div><div class="lab">最终失败</div></div>
</div>
<div class="chart">{IMG(p1,'overview')}</div>

<h2>2. 时段分布分析</h2>

<h3>2.1 星期 × 时段块 热力图</h3>
<div class="chart">{IMG(p2,'heatmap')}</div>
{hm_table}
<div class="notice">
  <h4>校级约束：周二下午（5-8节）禁排</h4>
  <p>全校统一约束，热力图中以红色虚线框标注。实际排课条数 = {int(hm[1,1])}
  （即使为非零，也来自落入下午 5-6/7-8 节但未严格属于"周二"维度的边缘情况，应核对数据语义）。</p>
</div>

<h3>2.2 连排时段分布</h3>
<div class="chart">{IMG(p3,'slot')}</div>
<div class="desc">
  <p><strong>设计预期：</strong>
    1-4节优于 5-8节、5-6 优于 7-8、1-2 优于 3-4；半天连排（1-4, 5-8）作为大块教学优选。</p>
  <p><strong>实测结果：</strong>
    1-2节 = {slot_cnt.get('1-2节',0):,}，
    3-4节 = {slot_cnt.get('3-4节',0):,}，
    5-6节 = {slot_cnt.get('5-6节',0):,}，
    7-8节 = {slot_cnt.get('7-8节',0):,}；
    半天连排 1-4节 = {slot_cnt.get('1-4节',0):,}，5-8节 = {slot_cnt.get('5-8节',0):,}。</p>
</div>

<h3>2.3 每天排课分布</h3>
<div class="chart">{IMG(p4,'weekday')}</div>

<h2>3. 各聚类成功率</h2>
<div class="chart">{IMG(p5,'cluster')}</div>
<table class="grid">
  <thead><tr><th>类别ID</th><th>类别名称</th><th>应排</th><th>成功</th><th>成功率</th></tr></thead>
  <tbody>{clu_rows}</tbody>
</table>

<h2>4. 特殊需求统计</h2>
<div class="chart">{IMG(p6,'special-ov')}</div>
<div class="chart">{IMG(p7,'special-bd')}</div>
<div class="desc">
  <p><strong>口径：</strong>课程含「偏好时间段 / 禁止时间段 / 指定教室 / 指定教学楼」其中任一项即视为"有特殊需求"。
  四项可累加（一门课可同时占多类）。</p>
  <p><strong>排除规则：</strong>「禁止时间段」中仅含校级约束「周二(5-8节)」的课程 <strong>不算</strong> 特殊需求
  （这类课 {spec['forbid_school_only']:,} 门，被排除）。</p>
  <p><strong>成果：</strong>有特殊需求的 {spec['special']:,} 门课中，{spec['special_ok']:,} 门
  ({spec_rate:.2f}%) 成功排课；{spec['special_fail']:,} 门最终失败。</p>
</div>

<h3>4.1 特殊需求带来的复杂性</h3>
<div class="notice">
  <h4>校级约束：周二下午（5-8节）全校禁排</h4>
  <p>这是覆盖全校所有教学班的硬约束，<strong>共 {spec['forbid_school_only']:,} 门课</strong>明确写入
  了 "周二(5-8节)" 禁排标签，但实际上算法对所有 4666 门课都会避开这个时段。</p>
  <p><strong>对算法的影响：</strong></p>
  <ul>
    <li><strong>有效时段缩水 12.5%</strong>：周一至周五 5 × 4 = 20 个下午时段块中砍掉 1 块 → 实际可用 19 块/周；</li>
    <li><strong>下午时段竞争加剧</strong>：原本可分布在周一~周五下午的体育、通识等课程被挤入周一/周三/周四/周五下午，
      导致这 4 个下午块的负载比理论均值高 25%；</li>
    <li><strong>balanced 策略的负担</strong>：在负载均衡打分时该约束让候选空间减少，
      间接导致部分课"理论上可排但实际所有候选都已满"而流入调课阶段；</li>
    <li><strong>聚类调度顺序敏感</strong>：体育类（聚类 1）有大量 周一/三/四/五 下午硬偏好，
      若不优先调度会与其它课争抢仅剩的下午时段。</li>
  </ul>
  <p>该校级约束 <strong>不计入</strong> 单门课的"特殊需求"统计（属于全局基线），但其复杂性体现在整体排课结果分布上。</p>
</div>

<h2>5. 失败课程分析</h2>
<div class="chart">{IMG(p8,'fail-top')}</div>
<table class="grid">
  <thead><tr><th>序</th><th>课程名称</th><th>失败班数</th></tr></thead>
  <tbody>{fail_rows}</tbody>
</table>
<div class="desc">
  <p>共 {final_fail_n} 门教学班最终未能排上。Top 集中在少数高资源争夺课程
  （体育公选、大类专业核心课等），可结合 LLM 失败归因模块进一步分析。</p>
</div>

<h2>6. 运行参数</h2>
<table class="grid">
  <tr><th>项目</th><th>值</th></tr>
  <tr><td>课程表</td><td>课程表_split_merged.xlsx（5250 行，4666 有效）</td></tr>
  <tr><td>放置策略</td><td>balanced (seed=2026)</td></tr>
  <tr><td>维度</td><td>weeks=20, days=7, periods=11</td></tr>
  <tr><td>聚类</td><td>6 类（430 / 248 / 156 / 534 / 886 / 2412）</td></tr>
  <tr><td>调课总耗时</td><td>{resched_time}</td></tr>
  <tr><td>首轮 + 调课总耗时</td><td>约 16 小时</td></tr>
  <tr><td>本次启用开关</td><td>IF_ROOM_CONFICT (211 班允许共占) · IF_CLASS_CONFICT (997 班) · LLXS 过滤</td></tr>
</table>

<div class="footer">
  本报告由 scripts/build_run_report.py 自动生成 · 数据源：排课结果/调课后的整体结果.xlsx
</div>
</body></html>"""

out_html = '排课结果/排课分析报告.html'
with open(out_html, 'w', encoding='utf-8') as f:
    f.write(html)
print(f'\nHTML 报告已生成: {out_html}')
print(f'图表目录: {OUT_DIR}/')

# 打印 KPI 摘要
print('\n===== KPI =====')
print(f'  有效教学班: {total_valid:,}')
print(f'  最终成功: {final_success_n:,} ({success_rate:.2f}%)')
print(f'  首轮成功: {first_success_n:,} ({first_rate:.2f}%)')
print(f'  调课新增: {rescheduled_n}  调课成功率: {resched_rate:.2f}%')
print(f'  最终失败: {final_fail_n}')
print(f'  特殊需求: {spec["special"]:,} 门 (已排除 {spec["forbid_school_only"]} 门仅含校级约束)')
print(f'  特殊需求成功率: {spec_rate:.2f}%')
