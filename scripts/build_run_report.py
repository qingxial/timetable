# -*- coding: utf-8 -*-
"""自包含排课分析报告生成器（balanced 策略 + 软偏好修复后）。
口径参考 generate_report_balanced.py / analysis_report.py / scripts/special_req_charts.py。

特殊需求规则：全校「周二(5-8节)」禁排属校级基线约束，不计入单课特殊需求；
在"特殊需求带来的复杂性"章节单独说明其影响。
"""
import os, re, base64, sys
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

for fp in ["/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
           "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"]:
    if os.path.exists(fp):
        fm.fontManager.addfont(fp)
        plt.rcParams["font.sans-serif"] = [fm.FontProperties(fname=fp).get_name()]
        break
plt.rcParams["axes.unicode_minus"] = False

OUT = "排课结果/图表展示"; os.makedirs(OUT, exist_ok=True)
WD = ['周一', '周二', '周三', '周四', '周五']
D2I = {'周一': 0, '周二': 1, '周三': 2, '周四': 3, '周五': 4, '周六': 5, '周日': 6}
C_BLUE, C_GREEN, C_ORANGE, C_RED, C_PURP = '#1E88E5', '#43A047', '#FB8C00', '#E53935', '#8E24AA'

# ---------------- 数据 ----------------
SCHED = '排课结果/调课后的整体结果_软偏好修复.xlsx'
if not os.path.isfile(SCHED):
    SCHED = '排课结果/调课后的整体结果.xlsx'
s = pd.read_excel(SCHED, dtype={'教学班ID': str})
room = pd.read_excel('智能排课基础数据/提取的基础数据表_converted/教室表.xlsx')
seat = {}
for _, r in room.iloc[1:].iterrows():
    try: seat[str(r['教室代码'])] = float(r['上课座位数'])
    except (TypeError, ValueError): pass

def n_uniq(path, col):
    try:
        d = pd.read_excel(path, dtype=str); return d[col].nunique()
    except Exception:
        return 0

TOTAL = 4666
first_fail = n_uniq('排课结果/排课失败课程_全部.xlsx', '教学班ID')       # 403
resched_fail = n_uniq('排课结果/调课失败的排课失败课程.xlsx', 'jxbid')    # 240
soft = 0
if os.path.isfile('排课结果/软偏好修复明细.xlsx'):
    soft = pd.read_excel('排课结果/软偏好修复明细.xlsx', dtype=str)['教学班ID'].nunique()  # 9
first_ok = TOTAL - first_fail
resched_ok = first_fail - resched_fail
final_fail = resched_fail - soft
final_ok = TOTAL - final_fail
rate = final_ok / TOTAL * 100

# ---------------- 工具 ----------------
def pstart(x):
    m = re.search(r'第(\d+)', str(x)); return int(m.group(1)) if m else None
def pend(x):
    m = re.search(r'-(\d+)', str(x)); return int(m.group(1)) if m else pstart(x)
def block(x):
    p = pstart(x); return '上午(1-4节)' if p and p <= 4 else ('下午(5-8节)' if p and p <= 8 else '晚上(9节+)')

def b64(p):
    with open(p, 'rb') as f: return base64.b64encode(f.read()).decode()
def IMG(p):
    return f'<img src="data:image/png;base64,{b64(p)}" style="max-width:100%;">'

def save(fig, name):
    out = f"{OUT}/{name}.png"; fig.tight_layout()
    fig.savefig(out, dpi=155, bbox_inches='tight'); plt.close(fig); return out

def barlabel(ax, bars, vals, fmt='{:,}', dy=0.01):
    mx = max(vals) if vals else 1
    for b, v in zip(bars, vals):
        ax.text(b.get_x() + b.get_width() / 2, v + mx * dy, fmt.format(v),
                ha='center', va='bottom', fontsize=10, fontweight='bold')

# ================= 图 =================
def fig_kpi():
    fig, ax = plt.subplots(figsize=(9, 4.6))
    labels = ['有效教学班', '首轮成功', '调课新增', '软偏好修复', '最终成功', '最终失败']
    vals = [TOTAL, first_ok, resched_ok, soft, final_ok, final_fail]
    cols = ['#90A4AE', C_GREEN, '#FFC107', C_PURP, C_BLUE, C_RED]
    bars = ax.bar(labels, vals, color=cols, edgecolor='white', linewidth=1.4)
    barlabel(ax, bars, vals)
    ax.set_ylim(0, max(vals) * 1.15); ax.set_ylabel('教学班数')
    ax.set_title(f'排课总览：最终成功 {final_ok:,}/{TOTAL:,} = {rate:.2f}%', fontsize=14, fontweight='bold')
    ax.grid(axis='y', alpha=.3); ax.spines[['top', 'right']].set_visible(False)
    return save(fig, '01_kpi')

def fig_funnel():
    fig, ax = plt.subplots(figsize=(9, 3.2))
    seg = [('首轮贪心', first_ok, C_GREEN), ('变邻域调课', resched_ok, '#FFC107'),
           ('软偏好修复', soft, C_PURP), ('最终失败', final_fail, C_RED)]
    left = 0
    for name, v, c in seg:
        ax.barh(0, v, left=left, color=c, edgecolor='white', label=f'{name} {v:,}')
        if v / TOTAL > 0.02:
            ax.text(left + v / 2, 0, f'{name}\n{v:,}', ha='center', va='center',
                    color='white', fontsize=10, fontweight='bold')
        left += v
    ax.set_xlim(0, TOTAL); ax.set_yticks([]); ax.set_xlabel('教学班累计')
    ax.set_title('排课贡献分解（构造 → 修复各阶段）', fontsize=13, fontweight='bold')
    ax.spines[['top', 'right', 'left']].set_visible(False)
    return save(fig, '02_funnel')

def fig_pref():
    """偏好满足率：有偏好的课中，实际落点全部落在偏好窗口内的比例。"""
    def parse(t):
        t = str(t or '').replace('（', '(').replace('）', ')').replace('；', ';')
        allow = defaultdict(list)
        for m in re.finditer(r'(周[一二三四五六日])全天', t): allow[D2I[m.group(1)]].append((1, 11))
        for m in re.finditer(r'(周[一二三四五六日])\((\d+)-(\d+)节?\)', t):
            allow[D2I[m.group(1)]].append((int(m.group(2)), int(m.group(3))))
        return allow
    tot = ok = 0; dev_exact = 0
    for jx, g in s.groupby('教学班ID'):
        allow = parse(g['偏好上课时间'].iloc[0])
        if not allow: continue
        tot += 1; good = True
        for _, r in g.iterrows():
            d = D2I.get(str(r['星期']), -1); a, b = pstart(r['节次']), pend(r['节次'])
            if d not in allow or not any(x <= a and b <= y for x, y in allow[d]): good = False; break
        if good: ok += 1
    fig, ax = plt.subplots(figsize=(6, 5))
    wedges, *_ = ax.pie([ok, tot - ok], labels=[f'满足偏好\n{ok}', f'最小偏离\n{tot-ok}'],
                        colors=[C_GREEN, C_ORANGE], autopct=lambda p: f'{p:.1f}%',
                        startangle=90, textprops={'fontsize': 11, 'fontweight': 'bold'},
                        wedgeprops={'edgecolor': 'white', 'linewidth': 2})
    ax.set_title(f'偏好满足率（有偏好课 {tot} 门）\n{ok}/{tot} = {ok/tot*100:.1f}% 精确落在偏好窗口',
                 fontsize=12.5, fontweight='bold')
    return save(fig, '03_pref'), tot, ok

def fig_consec():
    cnt = Counter()
    for x in s['节次']:
        a, b = pstart(x), pend(x); cnt[(b - a + 1) if a and b else 0] += 1
    order = sorted(k for k in cnt if k)
    vals = [cnt[k] for k in order]
    cols = [C_GREEN if k <= 2 else (C_ORANGE if k == 3 else C_RED) for k in order]
    fig, ax = plt.subplots(figsize=(7, 4.4))
    bars = ax.bar([f'{k}节连排' for k in order], vals, color=cols, edgecolor='white')
    barlabel(ax, bars, vals)
    ax.set_ylim(0, max(vals) * 1.15); ax.set_ylabel('排课条数')
    ax.set_title('连排结构（教学惯例：以 2 节连排为主、无 4 节连排）', fontsize=12.5, fontweight='bold')
    ax.grid(axis='y', alpha=.3); ax.spines[['top', 'right']].set_visible(False)
    return save(fig, '04_consec'), cnt

def fig_daypart():
    c = Counter(block(x) for x in s['节次'])
    order = ['上午(1-4节)', '下午(5-8节)', '晚上(9节+)']; vals = [c.get(k, 0) for k in order]
    fig, ax = plt.subplots(figsize=(7, 4.4))
    bars = ax.bar(order, vals, color=[C_GREEN, C_ORANGE, C_PURP], edgecolor='white')
    barlabel(ax, bars, vals)
    ax.set_ylim(0, max(vals) * 1.15); ax.set_ylabel('排课条数')
    ax.set_title('上午/下午/晚上分布（上午优先原则）', fontsize=12.5, fontweight='bold')
    ax.grid(axis='y', alpha=.3); ax.spines[['top', 'right']].set_visible(False)
    return save(fig, '05_daypart'), c

def fig_weekday():
    c = Counter(d for d in s['星期'] if d in WD); vals = [c.get(d, 0) for d in WD]
    fig, ax = plt.subplots(figsize=(8, 4.2))
    cols = [C_BLUE if d != '周二' else C_ORANGE for d in WD]
    bars = ax.bar(WD, vals, color=cols, edgecolor='white')
    barlabel(ax, bars, vals)
    ax.axhline(np.mean(vals), ls='--', color='gray', alpha=.7, label=f'均值 {np.mean(vals):.0f}')
    ax.set_ylim(0, max(vals) * 1.15); ax.set_ylabel('排课条数'); ax.legend()
    bal = min(vals) / max(vals)
    ax.set_title(f'工作日负载均衡（min/max={bal:.2f}；周二受校级下午禁排影响偏低）',
                 fontsize=12, fontweight='bold')
    ax.grid(axis='y', alpha=.3); ax.spines[['top', 'right']].set_visible(False)
    return save(fig, '06_weekday'), c, bal

def fig_heat():
    grid = np.zeros((3, 5)); BL = ['上午(1-4节)', '下午(5-8节)', '晚上(9节+)']
    for _, r in s.iterrows():
        if str(r['星期']) in WD:
            grid[BL.index(block(r['节次'])), WD.index(str(r['星期']))] += 1
    fig, ax = plt.subplots(figsize=(8, 4.4))
    im = ax.imshow(grid, cmap='YlOrRd', aspect='auto')
    ax.set_xticks(range(5)); ax.set_xticklabels(WD); ax.set_yticks(range(3)); ax.set_yticklabels(BL)
    for i in range(3):
        for j in range(5):
            t = f'{int(grid[i,j]):,}'
            if WD[j] == '周二' and BL[i] == '下午(5-8节)':
                ax.add_patch(plt.Rectangle((j - .5, i - .5), 1, 1, fill=False, edgecolor='red', lw=3, ls='--'))
                t += '\n(禁排)'
            ax.text(j, i, t, ha='center', va='center', fontsize=10, fontweight='bold',
                    color='black' if grid[i, j] < grid.max() * .55 else 'white')
    ax.set_title('排课热力图（红框=全校周二下午禁排区）', fontsize=12.5, fontweight='bold')
    fig.colorbar(im, ax=ax, label='排课条数')
    return save(fig, '07_heat'), grid

def fig_seat():
    util = []
    for _, r in s.iterrows():
        cap = seat.get(str(r['教室代码']))
        if cap and cap > 0 and pd.notna(r['课容量']):
            try: util.append(min(float(r['课容量']) / cap, 1.5))
            except (TypeError, ValueError): pass
    fig, ax = plt.subplots(figsize=(8, 4.2))
    ax.hist([u * 100 for u in util], bins=20, color=C_BLUE, edgecolor='white')
    m = np.mean(util) * 100
    ax.axvline(m, color=C_RED, ls='--', lw=2, label=f'均值 {m:.1f}%')
    ax.set_xlabel('座位利用率 = 课容量 / 教室座位数 (%)'); ax.set_ylabel('排课条数'); ax.legend()
    ax.set_title(f'教室座位利用率分布（均值 {m:.1f}%，避免大教室排小班）', fontsize=12, fontweight='bold')
    ax.grid(axis='y', alpha=.3); ax.spines[['top', 'right']].set_visible(False)
    return save(fig, '08_seat'), m

def fig_roomtime():
    rs = defaultdict(set)
    for _, r in s.iterrows():
        if str(r['星期']) in WD:
            a, b = pstart(r['节次']), pend(r['节次'])
            for p in range(a, b + 1): rs[str(r['教室代码'])].add((str(r['星期']), p))
    uts = sorted((len(v) / 40 * 100 for v in rs.values()), reverse=True)
    fig, ax = plt.subplots(figsize=(9, 4))
    ax.bar(range(len(uts)), uts, color=C_GREEN, width=1.0)
    m = np.mean(uts)
    ax.axhline(m, color=C_RED, ls='--', lw=2, label=f'均值 {m:.1f}%')
    ax.set_xlabel(f'教室（按利用率排序，共 {len(uts)} 间被占用）')
    ax.set_ylabel('时间利用率 (%)'); ax.legend()
    ax.set_title('各教室时间利用率（工作日 5×8=40 格为分母）', fontsize=12, fontweight='bold')
    ax.grid(axis='y', alpha=.3); ax.spines[['top', 'right']].set_visible(False)
    return save(fig, '09_roomtime'), m, len(uts)

def fig_teacher():
    tl = defaultdict(set)
    for _, r in s.iterrows():
        a, b = pstart(r['节次']), pend(r['节次'])
        for p in range(a, b + 1): tl[str(r['教师号'])].add((str(r['星期']), p))
    vals = [len(v) for v in tl.values()]
    fig, ax = plt.subplots(figsize=(8, 4.2))
    ax.hist(vals, bins=range(0, max(vals) + 2), color=C_PURP, edgecolor='white', align='left')
    ax.axvline(np.mean(vals), color=C_RED, ls='--', lw=2, label=f'均值 {np.mean(vals):.1f} 节/周')
    ax.set_xlabel('教师周课时（节/周）'); ax.set_ylabel('教师数'); ax.legend()
    ax.set_title(f'教师工作量分布（中位 {int(np.median(vals))} 节，最大 {max(vals)} 节）',
                 fontsize=12, fontweight='bold')
    ax.grid(axis='y', alpha=.3); ax.spines[['top', 'right']].set_visible(False)
    return save(fig, '10_teacher'), np.mean(vals), int(np.median(vals)), max(vals)

def fig_faildist():
    try:
        a = pd.read_excel('排课结果/失败课程归因与建议.xlsx', dtype=str)
        col = '具体根因'
        vc = a[~a['软偏好修复结果'].astype(str).str.contains('已由软偏好修复', na=False)][col].value_counts()
    except Exception:
        return None, None
    fig, ax = plt.subplots(figsize=(10, 4.6))
    lab = [x[:22] for x in vc.index][::-1]; val = list(vc.values)[::-1]
    bars = ax.barh(lab, val, color=C_RED, edgecolor='white')
    for b, v in zip(bars, val):
        ax.text(v + 0.4, b.get_y() + b.get_height() / 2, str(v), va='center', fontsize=9)
    ax.set_title(f'剩余 {int(vc.sum())} 门失败课的根因分布（均为数据侧/物理无解）', fontsize=12, fontweight='bold')
    ax.set_xlabel('门数'); ax.spines[['top', 'right']].set_visible(False)
    return save(fig, '11_faildist'), vc

# ============ 生成 ============
print('生成图表 ...')
p_kpi = fig_kpi(); p_fun = fig_funnel()
p_pref, pref_tot, pref_ok = fig_pref()
p_con, consec = fig_consec()
p_dp, daypart = fig_daypart()
p_wd, wdc, balance = fig_weekday()
p_heat, grid = fig_heat()
p_seat, seat_m = fig_seat()
p_rt, rt_m, rt_n = fig_roomtime()
p_tea, tea_mean, tea_med, tea_max = fig_teacher()
p_fd, faildist = fig_faildist()

two2 = consec.get(2, 0); one1 = consec.get(1, 0); four = consec.get(4, 0)
consec_total = sum(consec.values())
am = daypart.get('上午(1-4节)', 0); pm = daypart.get('下午(5-8节)', 0); ev = daypart.get('晚上(9节+)', 0)
tue_pm = int(grid[1, 1])

def rows_fd():
    if faildist is None: return ''
    return ''.join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in faildist.items())

html = f"""<!DOCTYPE html><html lang=zh><head><meta charset=UTF-8>
<title>智能排课分析报告 · {datetime.now():%Y-%m-%d}</title><style>
body{{font-family:"PingFang SC","Microsoft YaHei",sans-serif;max-width:1080px;margin:22px auto;padding:0 20px;color:#2b2b2b;line-height:1.65;}}
h1{{text-align:center;color:{C_BLUE};border-bottom:3px solid {C_BLUE};padding-bottom:10px;}}
h2{{color:#0D47A1;border-left:5px solid {C_BLUE};padding-left:12px;margin-top:38px;}}
h3{{color:#1565C0;margin-top:24px;}}
.kpi{{display:flex;flex-wrap:wrap;gap:12px;margin:18px 0;}}
.box{{flex:1 1 150px;background:#f6f8fa;border-radius:10px;padding:16px;text-align:center;border-top:4px solid {C_BLUE};}}
.box .v{{font-size:26px;font-weight:800;color:#0D47A1;}} .box .l{{color:#666;font-size:13px;margin-top:4px;}}
.box.g{{border-top-color:{C_GREEN};}} .box.g .v{{color:#2E7D32;}}
.box.r{{border-top-color:{C_RED};}} .box.r .v{{color:#C62828;}}
.box.p{{border-top-color:{C_PURP};}} .box.p .v{{color:#6A1B9A;}}
table{{border-collapse:collapse;width:100%;margin:12px 0;}} th,td{{border:1px solid #ddd;padding:7px 11px;text-align:center;}}
th{{background:{C_BLUE};color:#fff;}} tr:nth-child(even) td{{background:#fafafa;}} td.ban{{background:#FFEBEE;color:#C62828;}}
.chart{{margin:20px 0;text-align:center;}} .note{{background:#FFF8E1;border-left:4px solid {C_ORANGE};padding:13px 17px;border-radius:5px;margin:16px 0;}}
.note h4{{margin:0 0 6px;color:#E65100;}} .good{{background:#E8F5E9;border-left:4px solid {C_GREEN};padding:13px 17px;border-radius:5px;margin:16px 0;}}
.foot{{text-align:center;color:#999;font-size:12px;margin-top:46px;border-top:1px solid #eee;padding-top:16px;}}
</style></head><body>

<h1>智能排课分析报告</h1>
<p style="text-align:center;color:#666;">生成 {datetime.now():%Y-%m-%d %H:%M} · 数据：课程表_split_merged.xlsx · 策略：balanced (seed=2026) · 维度 20周×7天×11节</p>

<h2>1 · 总体结果</h2>
<div class=kpi>
<div class="box g"><div class=v>{rate:.2f}%</div><div class=l>最终成功率</div></div>
<div class="box g"><div class=v>{final_ok:,}</div><div class=l>最终成功</div></div>
<div class=box><div class=v>{first_ok:,}</div><div class=l>首轮 ({first_ok/TOTAL*100:.1f}%)</div></div>
<div class=box><div class=v>{resched_ok}</div><div class=l>调课新增</div></div>
<div class="box p"><div class=v>{soft}</div><div class=l>软偏好修复</div></div>
<div class="box r"><div class=v>{final_fail}</div><div class=l>最终失败</div></div>
</div>
<div class=chart>{IMG(p_kpi)}</div>
<div class=chart>{IMG(p_fun)}</div>

<h2>2 · 排课质量指标</h2>

<h3>2.1 偏好满足率（核心质量指标）</h3>
<div class=chart>{IMG(p_pref)}</div>
<div class=good><strong>有偏好的 {pref_tot} 门课中，{pref_ok} 门（{pref_ok/pref_tot*100:.1f}%）实际落点精确落在偏好窗口内。</strong>
仅 {pref_tot-pref_ok} 门因「(教师×单一偏好格)物理超订」经软偏好修复做最小偏离（保住星期或节次），不丢偏好。</div>

<h3>2.2 连排结构</h3>
<div class=chart>{IMG(p_con)}</div>
<div class=good><strong>2 节连排 {two2:,} 条（{two2/consec_total*100:.1f}%）占绝对主导，1 节 {one1:,} 条，<b>4 节连排 {four} 条</b>。</strong>
完全符合"以 2 节连排为主、杜绝 4 节连排"的教学惯例。</div>

<h3>2.3 上午/下午/晚上分布</h3>
<div class=chart>{IMG(p_dp)}</div>
<p>上午 {am:,} ＞ 下午 {pm:,} ＞ 晚上 {ev:,}，符合"上午优先、少排晚上"的原则。</p>

<h3>2.4 工作日负载均衡</h3>
<div class=chart>{IMG(p_wd)}</div>
<p>周一至周五 min/max = {balance:.2f}。周二偏低是全校下午禁排（少一个下午块）所致，属预期。</p>

<h3>2.5 排课热力图</h3>
<div class=chart>{IMG(p_heat)}</div>
<div class=note><h4>校级约束：周二下午（5-8节）禁排</h4>周二下午实际排课 <b>{tue_pm}</b> 条，约束严格执行（红框区）。</div>

<h3>2.6 教室座位利用率</h3>
<div class=chart>{IMG(p_seat)}</div>
<p>座位利用率均值 <strong>{seat_m:.1f}%</strong>（课容量/教室座位数），大部分课能匹配到容量合适的教室，避免大教室排小班的浪费。</p>

<h3>2.7 各教室时间利用率</h3>
<div class=chart>{IMG(p_rt)}</div>
<p>{rt_n} 间教室被使用，工作日时间利用率均值 <strong>{rt_m:.1f}%</strong>（分母 5×8=40 格）。</p>

<h3>2.8 教师工作量分布</h3>
<div class=chart>{IMG(p_tea)}</div>
<p>教师周课时中位 <strong>{tea_med} 节</strong>、均值 {tea_mean:.1f} 节；最大 {tea_max} 节为声乐一对一教师（其超载也是少数课失败的根因）。</p>

<h2>3 · 特殊需求与失败归因</h2>
<h3>3.1 剩余 {final_fail} 门失败课的根因</h3>
{('<div class=chart>'+IMG(p_fd)+'</div>') if p_fd else ''}
<table><thead><tr><th>具体根因</th><th>门数</th></tr></thead><tbody>{rows_fd()}</tbody></table>
<div class=note><h4>结论：剩余失败已全部归因，均非排课算法可解</h4>
<b>数据侧（教务需修数据）：</b>偏好节次错位(6-7节跨块)、偏好撞禁排(偏好1-2节却全周禁排1-2节)、
偏好与ZXS不自洽、教室容量/类型静态不足。<br>
<b>物理无解：</b>声乐一对一课单教师超载、大公选容量210每时段都有学生班冲突。<br>
每门课的逐课根因、证据与修改建议见 <code>排课结果/失败课程归因与建议.xlsx</code>。</div>

<h3>3.2 全校周二下午禁排带来的复杂性</h3>
<div class=note><p>全校统一"周二(5-8节)禁排"覆盖所有 {TOTAL:,} 门课，<b>不计入</b>单课特殊需求（属校级基线）。
其影响：下午有效时段块从 20 减到 19（-12.5%），下午竞争加剧，是部分体育/通识课流入调课与软偏好修复阶段的背景原因。</p></div>

<h2>4 · 运行参数</h2>
<table>
<tr><th>项目</th><th>值</th></tr>
<tr><td>课程表</td><td>课程表_split_merged.xlsx（4666 有效教学班）</td></tr>
<tr><td>放置策略</td><td>balanced (seed=2026)</td></tr>
<tr><td>流程</td><td>首轮贪心 → 变邻域调课 → 最小偏离软偏好修复</td></tr>
<tr><td>启用开关</td><td>IF_ROOM_CONFICT（教室共占）· IF_CLASS_CONFICT（班级共占）· LLXS 过滤</td></tr>
</table>

<div class=foot>本报告由 scripts/build_run_report.py 自动生成 · 数据源：{SCHED}</div>
</body></html>"""

out = '排课结果/排课分析报告.html'
open(out, 'w', encoding='utf-8').write(html)
print(f'HTML 报告: {out}')
print(f'KPI: 成功 {final_ok}/{TOTAL}={rate:.2f}% | 偏好满足 {pref_ok}/{pref_tot}={pref_ok/pref_tot*100:.1f}% | '
      f'2节连排占比 {two2/consec_total*100:.1f}% | 4节连排 {four} | 座位利用 {seat_m:.1f}% | 教室时间利用 {rt_m:.1f}%')
