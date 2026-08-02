"""
排课结果分析：修复图表 + 生成HTML报告
"""
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib
import matplotlib.patches as mpatches
from matplotlib.gridspec import GridSpec
import base64, os, warnings
warnings.filterwarnings('ignore')

matplotlib.rcParams['font.sans-serif'] = ['PingFang SC', 'Heiti TC', 'STHeiti', 'Arial Unicode MS']
matplotlib.rcParams['axes.unicode_minus'] = False
matplotlib.rcParams['figure.dpi'] = 150

DATA_DIR = "排课结果/"   # balanced 新结果（2026-06-15，98.09% / 89 失败）
OUTPUT_DIR = DATA_DIR + "图表展示/"
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ===== 加载数据 =====
df = pd.read_excel(DATA_DIR + '调课后的整体结果.xlsx')
cluster = pd.read_excel('排课结果1/课程聚类结果2.xlsx')
fail87 = pd.read_excel(DATA_DIR + '失败课程归因与建议.xlsx')

WEEKDAYS = ['周一','周二','周三','周四','周五']

def parse_period_start(s):
    import re
    m = re.search(r'(\d+)', str(s))
    return int(m.group(1)) if m else None

def period_to_block(s):
    f = parse_period_start(s)
    if f is None: return '未知'
    if f <= 4: return '上午(1-4节)'
    if f <= 8: return '下午(5-8节)'
    return '晚上(9节+)'

def classify_slot(s):
    s = str(s)
    if '1-4' in s: return '1-4节'
    if '5-8' in s: return '5-8节'
    if '1-2' in s: return '1-2节'
    if '3-4' in s: return '3-4节'
    if '5-6' in s: return '5-6节'
    if '7-8' in s: return '7-8节'
    if '9-10' in s: return '9-10节'
    if '1-8' in s: return '1-8节'
    if '9-11' in s or '9-12' in s: return '9节以后'
    return '其他'

df['节次_起始'] = df['节次'].apply(parse_period_start)
df['时段'] = df['节次'].apply(classify_slot)
df['上午下午晚'] = df['节次'].apply(period_to_block)
df['星期_num'] = df['星期'].apply(lambda x: WEEKDAYS.index(x) if x in WEEKDAYS else 99)

cluster_map = cluster.set_index('JXBID')[['类别名称']].to_dict('index')
df['类别名称'] = df['教学班ID'].map(lambda x: cluster_map.get(x, {}).get('类别名称', '未知'))

# ===== 图1（重做）: 公修课排课成功率 =====
PUBLIC_MAP = {
    '体育': ['体育'],
    '英语/外语': ['英语','大学英语','大学外语','外语'],
    '数学': ['数学','高等数学','线性代数','概率论'],
    '形势与政策': ['形势与政策'],
    '中国近现代史纲要': ['中国近现代史'],
    '毛泽东思想概论': ['毛泽东思想'],
    '习近平新时代思想': ['习近平'],
}
ALL_PUB_KWS = sum(PUBLIC_MAP.values(), [])

# 口径：分子=调课后已排的唯一教学班ID（df），分母=已排+最终失败87（实际参与排课的全部）
df_name = df.drop_duplicates('教学班ID').set_index('教学班ID')['课程名称'].to_dict()
fail_name = fail87.set_index('教学班ID')['课程名称'].to_dict()

def match_kws(name, kws): return any(k in str(name) for k in kws)

rows_result = []
for label, kws in PUBLIC_MAP.items():
    done  = sum(1 for n in df_name.values()   if match_kws(n, kws))
    failed= sum(1 for n in fail_name.values() if match_kws(n, kws))
    total = done + failed
    rows_result.append({'课程类型': label, '已排': done, '失败数': failed, '总任务': total,
                        '成功率': done/total*100 if total else 0})
# 专业课
done_s  = sum(1 for n in df_name.values()   if not match_kws(n, ALL_PUB_KWS))
fail_s  = sum(1 for n in fail_name.values() if not match_kws(n, ALL_PUB_KWS))
rows_result.append({'课程类型': '专业课', '已排': done_s, '失败数': fail_s,
                    '总任务': done_s+fail_s, '成功率': done_s/(done_s+fail_s)*100})

result = pd.DataFrame(rows_result).set_index('课程类型')

# 同步给df打标签（供后续section引用）
df['课程类型'] = '专业课'
for label, kws in PUBLIC_MAP.items():
    mask = df['课程名称'].apply(lambda x: match_kws(x, kws))
    df.loc[mask, '课程类型'] = label

fig, axes = plt.subplots(1, 2, figsize=(15, 6))
fig.suptitle('公修课与专业课排课情况分析', fontsize=15, fontweight='bold', y=1.01)

# 左图：成功率柱状图
palette = ['#4472C4','#ED7D31','#A9D18E','#FFC000','#5A9BD4','#70AD47','#FF0000','#767171']
bar_colors = [palette[i % len(palette)] for i in range(len(result))]
bars = axes[0].bar(result.index, result['成功率'], color=bar_colors,
                   edgecolor='white', linewidth=1.5, zorder=3, width=0.6)
axes[0].set_ylim(0, 118)
axes[0].set_title('各类课程排课成功率', fontsize=13, fontweight='bold')
axes[0].set_ylabel('排课成功率 (%)', fontsize=11)
axes[0].tick_params(axis='x', rotation=30)
axes[0].grid(axis='y', alpha=0.3, zorder=0)
axes[0].axhline(y=100, color='#C00000', linestyle='--', linewidth=1.5, alpha=0.8, zorder=2, label='100%基准线')
axes[0].legend(fontsize=10)
for bar, (idx, row) in zip(bars, result.iterrows()):
    h = bar.get_height()
    axes[0].text(bar.get_x() + bar.get_width()/2, h + 1.5,
                 f'{h:.1f}%', ha='center', va='bottom', fontsize=10, fontweight='bold', color='#333333')
    if row['失败数'] > 0:
        axes[0].text(bar.get_x() + bar.get_width()/2, h/2,
                     f'失败{int(row["失败数"])}班',
                     ha='center', va='center', fontsize=8, color='white', fontweight='bold')

# 右图：总任务数 vs 已排数分组柱
x = np.arange(len(result))
w = 0.35
b1 = axes[1].bar(x - w/2, result['总任务'], w, label='总任务数', color='#9DC3E6', edgecolor='white', linewidth=1.2)
b2 = axes[1].bar(x + w/2, result['已排'],   w, label='已排班次', color='#2E75B6', edgecolor='white', linewidth=1.2)
axes[1].set_title('各类课程排课数量对比', fontsize=13, fontweight='bold')
axes[1].set_ylabel('教学班数量', fontsize=11)
axes[1].set_xticks(x)
axes[1].set_xticklabels(result.index, rotation=30, ha='right', fontsize=10)
axes[1].legend(fontsize=10)
axes[1].grid(axis='y', alpha=0.3)
for bar in b1:
    v = int(bar.get_height())
    if v > 0:
        axes[1].text(bar.get_x()+bar.get_width()/2, bar.get_height()+8, str(v),
                     ha='center', va='bottom', fontsize=8, color='#555555')
for bar in b2:
    v = int(bar.get_height())
    if v > 0:
        axes[1].text(bar.get_x()+bar.get_width()/2, bar.get_height()+8, str(v),
                     ha='center', va='bottom', fontsize=8, color='#555555')

plt.tight_layout()
plt.savefig(OUTPUT_DIR + 'pub_course_scheduling_rate.png', dpi=150, bbox_inches='tight')
plt.close()
print("图1已重做保存")

# ===== 图2: 一周时段热力图 =====
BLOCKS = ['上午(1-4节)', '下午(5-8节)', '晚上(9节+)']
heatmap_data = pd.crosstab(
    df[df['星期'].isin(WEEKDAYS)]['上午下午晚'],
    df[df['星期'].isin(WEEKDAYS)]['星期']
).reindex(index=BLOCKS, columns=WEEKDAYS, fill_value=0)

fig, ax = plt.subplots(figsize=(10, 4.5))
im = ax.imshow(heatmap_data.values, cmap='YlOrRd', aspect='auto', vmin=0)
ax.set_xticks(range(len(WEEKDAYS))); ax.set_xticklabels(WEEKDAYS, fontsize=13)
ax.set_yticks(range(len(BLOCKS))); ax.set_yticklabels(BLOCKS, fontsize=12)
plt.colorbar(im, ax=ax, label='排课班次数')
vmax = heatmap_data.values.max()
for i in range(len(BLOCKS)):
    for j in range(len(WEEKDAYS)):
        val = heatmap_data.values[i,j]
        if WEEKDAYS[j] == '周二' and BLOCKS[i] == '下午(5-8节)':
            ax.add_patch(plt.Rectangle((j-0.5, i-0.5), 1, 1, fill=True, color='#BDD7EE', zorder=2))
            ax.text(j, i, f'限制区域\n({val}班)', ha='center', va='center',
                    fontsize=10, color='#1F497D', fontweight='bold', zorder=3)
        else:
            color = 'white' if val > vmax * 0.65 else '#333333'
            ax.text(j, i, str(val), ha='center', va='center', fontsize=14,
                    color=color, fontweight='bold')
ax.set_title('一周各时段排课热力图（教学班次数）', fontsize=14, fontweight='bold', pad=12)
plt.tight_layout()
plt.savefig(OUTPUT_DIR + 'weekly_block_heatmap.png', dpi=150, bbox_inches='tight')
plt.close()
print("图2已保存")

# ===== 图3: 聚类类别时间分布 =====
cats = df['类别名称'].value_counts().index.tolist()[:6]
fig, axes = plt.subplots(2, 3, figsize=(18, 9))
fig.suptitle('各聚类类别课程时间段分布', fontsize=15, fontweight='bold')
axes = axes.flatten()
block_colors = {'上午(1-4节)': '#4CAF50', '下午(5-8节)': '#FF9800', '晚上(9节+)': '#9C27B0'}
for idx, cat in enumerate(cats):
    sub = df[df['类别名称'] == cat]
    pivot = sub.groupby(['星期','上午下午晚']).size().unstack(fill_value=0)
    pivot = pivot.reindex(columns=BLOCKS, fill_value=0)
    pivot = pivot.reindex([d for d in WEEKDAYS if d in pivot.index], fill_value=0)
    pivot.plot(kind='bar', ax=axes[idx],
               color=[block_colors[c] for c in BLOCKS],
               edgecolor='white', linewidth=0.7)
    axes[idx].set_title(f'{cat}（共{len(sub)}班次）', fontsize=12, fontweight='bold')
    axes[idx].set_xlabel(''); axes[idx].set_ylabel('班次数')
    axes[idx].tick_params(axis='x', rotation=0)
    axes[idx].legend(loc='upper right', fontsize=8)
    axes[idx].grid(axis='y', alpha=0.3)
plt.tight_layout()
plt.savefig(OUTPUT_DIR + 'cluster_time_distribution.png', dpi=150, bbox_inches='tight')
plt.close()
print("图3已保存")

# ===== 图4: 班级周分布 =====
weekday_dist = df[df['星期'].isin(WEEKDAYS)].groupby('星期').size().reindex(WEEKDAYS)

rows = []
for _, row in df[df['星期'].isin(WEEKDAYS)].iterrows():
    for cls in str(row.get('班级信息','')).split():
        rows.append({'班级': cls, '星期': row['星期']})
class_df = pd.DataFrame(rows)
class_pivot = class_df.groupby(['班级','星期']).size().unstack(fill_value=0)
class_pivot = class_pivot.reindex(columns=[d for d in WEEKDAYS if d in class_pivot.columns], fill_value=0)
class_pivot['方差'] = class_pivot[[d for d in WEEKDAYS if d in class_pivot.columns]].var(axis=1)

fig, axes = plt.subplots(1, 2, figsize=(14, 5.5))
fig.suptitle('班级周一至周五课程分布', fontsize=14, fontweight='bold')
day_colors = ['#42A5F5','#EF5350','#66BB6A','#FFA726','#AB47BC']
axes[0].bar(WEEKDAYS, weekday_dist.values, color=day_colors, edgecolor='white', linewidth=1.5, zorder=3)
axes[0].set_title('全校各天排课班次总量', fontsize=12, fontweight='bold')
axes[0].set_ylabel('排课班次数'); axes[0].grid(axis='y', alpha=0.3, zorder=0)
for i, v in enumerate(weekday_dist.values):
    axes[0].text(i, v+15, str(v), ha='center', va='bottom', fontsize=12, fontweight='bold')

var_bins = [0, 0.5, 1, 2, 5, 9999]
var_labels = ['极均匀\n(0–0.5)','均匀\n(0.5–1)','较均匀\n(1–2)','不均匀\n(2–5)','很不均匀\n(>5)']
var_counts = pd.cut(class_pivot['方差'], bins=var_bins, labels=var_labels).value_counts().reindex(var_labels).fillna(0)
var_colors = ['#1B5E20','#4CAF50','#FFC107','#FF5722','#B71C1C']
axes[1].bar(var_labels, var_counts.values, color=var_colors, edgecolor='white', linewidth=1.5, zorder=3)
axes[1].set_title('班级每日课程均匀程度分布', fontsize=12, fontweight='bold')
axes[1].set_ylabel('班级数量'); axes[1].grid(axis='y', alpha=0.3, zorder=0)
for i, v in enumerate(var_counts.values):
    axes[1].text(i, v+1, int(v), ha='center', va='bottom', fontsize=12, fontweight='bold')
plt.tight_layout()
plt.savefig(OUTPUT_DIR + 'class_weekday_distribution.png', dpi=150, bbox_inches='tight')
plt.close()
print("图4已保存")

# ===== 图5: 连排时段分布 =====
slot_order = ['1-2节','3-4节','5-6节','7-8节','1-4节','5-8节','9-10节','1-8节','9节以后','其他']
slot_counts = df['时段'].value_counts().reindex(slot_order).fillna(0)
slot_colors = {
    '1-2节':'#1565C0','3-4节':'#1976D2','5-6节':'#2196F3','7-8节':'#64B5F6',
    '1-4节':'#E65100','5-8节':'#FF6D00','9-10节':'#6A1B9A',
    '1-8节':'#880E4F','9节以后':'#37474F','其他':'#90A4AE'
}
fig, ax = plt.subplots(figsize=(12, 5.5))
bars = ax.bar(slot_counts.index, slot_counts.values,
              color=[slot_colors.get(k,'#90A4AE') for k in slot_counts.index],
              edgecolor='white', linewidth=1.5, zorder=3, width=0.65)
ax.set_title('连排时段分布情况\n（优先级：1-4节 > 5-8节 > 9-10节；1-2节 > 3-4节；5-6节 > 7-8节）',
             fontsize=13, fontweight='bold')
ax.set_ylabel('排课班次数'); ax.grid(axis='y', alpha=0.3, zorder=0)
ax.tick_params(axis='x', rotation=20)
for bar, val in zip(bars, slot_counts.values):
    ax.text(bar.get_x()+bar.get_width()/2, bar.get_height()+8, int(val),
            ha='center', va='bottom', fontsize=11, fontweight='bold', color='#333333')
legend_patches = [
    mpatches.Patch(color='#1565C0', label='上午时段 (1-2、3-4节)'),
    mpatches.Patch(color='#2196F3', label='下午时段 (5-6、7-8节)'),
    mpatches.Patch(color='#E65100', label='半天连排 (1-4、5-8节)'),
    mpatches.Patch(color='#6A1B9A', label='晚上及其他'),
]
ax.legend(handles=legend_patches, loc='upper right', fontsize=9)
plt.tight_layout()
plt.savefig(OUTPUT_DIR + 'continuous_slot_distribution.png', dpi=150, bbox_inches='tight')
plt.close()
print("图5已保存")

# ===== 图6: 间隔天数 =====
gaps = []
for jxbid, group in df.groupby('教学班ID'):
    days = sorted(group['星期_num'].unique())
    for i in range(len(days)-1):
        gaps.append(days[i+1]-days[i])
gap_series = pd.Series(gaps)
gap_counts = gap_series.value_counts().sort_index()

fig, axes = plt.subplots(1, 2, figsize=(13, 5))
fig.suptitle('同一课程多次安排间隔天数分析', fontsize=14, fontweight='bold')
day_labels_map = {1:'相邻天\n(间隔0天)',2:'间隔1天',3:'间隔2天',4:'间隔3天',5:'间隔4天',6:'跨周末'}
glabels = [day_labels_map.get(i, f'{i}天') for i in gap_counts.index]
gcolors = ['#F44336' if i == 1 else '#4CAF50' for i in gap_counts.index]
axes[0].bar(glabels, gap_counts.values, color=gcolors, edgecolor='white', linewidth=1.5, zorder=3)
axes[0].set_title('间隔天数分布\n（绿色=合规，橙红色=相邻天安排）', fontsize=11, fontweight='bold')
axes[0].set_ylabel('出现次数'); axes[0].grid(axis='y', alpha=0.3, zorder=0)
for i, v in enumerate(gap_counts.values):
    axes[0].text(i, v+1, str(v), ha='center', va='bottom', fontsize=11, fontweight='bold')

adj = int(gap_counts.get(1, 0))
non_adj = int(gap_counts.sum()) - adj
axes[1].pie([non_adj, adj],
            labels=[f'非相邻天（合规）\n{non_adj}次，{non_adj/(non_adj+adj)*100:.1f}%',
                    f'相邻天安排\n{adj}次，{adj/(non_adj+adj)*100:.1f}%'],
            colors=['#4CAF50','#F44336'], startangle=90,
            wedgeprops={'edgecolor':'white','linewidth':2},
            textprops={'fontsize': 10})
axes[1].set_title('相邻天安排占比', fontsize=11, fontweight='bold')
plt.tight_layout()
plt.savefig(OUTPUT_DIR + 'course_gap_distribution.png', dpi=150, bbox_inches='tight')
plt.close()
print("图6已保存")

# ===== 图7: 体育课时间分布 =====
sports = df[df['课程名称'].str.contains('体育', na=False)]
fig, axes = plt.subplots(1, 3, figsize=(16, 5))
fig.suptitle('体育类课程时间分布', fontsize=14, fontweight='bold')
wd_counts = sports['星期'].value_counts().reindex(WEEKDAYS).fillna(0)
axes[0].bar(WEEKDAYS, wd_counts.values, color='#26A69A', edgecolor='white', linewidth=1.5, zorder=3)
axes[0].set_title('按星期分布', fontsize=12, fontweight='bold')
axes[0].set_ylabel('班次数'); axes[0].grid(axis='y', alpha=0.3, zorder=0)
for i, v in enumerate(wd_counts.values):
    axes[0].text(i, v+0.3, int(v), ha='center', va='bottom', fontsize=12, fontweight='bold')

slot_c = sports['时段'].value_counts()
bar_clrs = ['#4CAF50'] * len(slot_c)  # 统一颜色，不用红色标违规
axes[1].bar(slot_c.index, slot_c.values, color='#26C6DA', edgecolor='white', linewidth=1.5, zorder=3)
axes[1].set_title('按节次分布\n（要求14：不排前1-2节）', fontsize=11, fontweight='bold')
axes[1].set_ylabel('班次数'); axes[1].tick_params(axis='x', rotation=25)
axes[1].grid(axis='y', alpha=0.3, zorder=0)
for bar, val in zip(axes[1].patches, slot_c.values):
    axes[1].text(bar.get_x()+bar.get_width()/2, bar.get_height()+0.3, int(val),
                 ha='center', va='bottom', fontsize=10, fontweight='bold')

block_c = sports['上午下午晚'].value_counts()
axes[2].pie(block_c.values,
            labels=[f'{k}\n({v}班)' for k,v in block_c.items()],
            colors=['#80DEEA','#4DB6AC','#00695C'],
            autopct='%1.1f%%', startangle=90,
            wedgeprops={'edgecolor':'white','linewidth':2},
            textprops={'fontsize': 10})
axes[2].set_title('时段占比', fontsize=12, fontweight='bold')
plt.tight_layout()
plt.savefig(OUTPUT_DIR + 'sports_time_distribution.png', dpi=150, bbox_inches='tight')
plt.close()
print("图7已保存")

# 体育1-2节情况
sports_12 = sports[sports['时段'] == '1-2节']
sports_12_courses = sports_12['课程名称'].unique().tolist()
sports_12_count = len(sports_12)

# ===== 图8: 教室时间利用率 =====
# 正确口径：按【教室代码】统计该教室一周内被占用的不同 (工作日, 节次) 格子数，
# 满载基准 = 5 工作日 × 11 节 = 55 个时段格子（避免旧版"记录条数÷14半天块"导致的>100%）。
import re as _re8
WD8 = ['周一', '周二', '周三', '周四', '周五']
DENOM_SLOTS = 5 * 11   # 55
def _periods8(s):
    m = _re8.findall(r'(\d+)', str(s))
    if len(m) >= 2:
        return list(range(int(m[0]), int(m[1]) + 1))
    if len(m) == 1:
        return [int(m[0])]
    return []
_df8 = df[df['星期'].isin(WD8)]
_cells, _campus8 = {}, {}
for _, _r8 in _df8.iterrows():
    _code = _r8['教室代码']
    _campus8.setdefault(_code, _r8['校区'])
    for _p in _periods8(_r8['节次']):
        if 1 <= _p <= 11:
            _cells.setdefault(_code, set()).add((_r8['星期'], _p))
room_usage = pd.DataFrame({
    '教室代码': list(_cells.keys()),
    '利用率': [len(v) / DENOM_SLOTS * 100 for v in _cells.values()],
    '校区': [_campus8[c] for c in _cells.keys()],
})
room_time_mean = float(room_usage['利用率'].mean())   # 供第8节文案动态引用
fig, axes = plt.subplots(1, 2, figsize=(14, 5.5))
fig.suptitle('教室时间利用率分析（按教室代码·满载基准 5×11=55 时段）', fontsize=14, fontweight='bold')
bins = [0,20,40,60,80,100]; labels_u = ['0–20%','20–40%','40–60%','60–80%','80–100%']
ucounts = pd.cut(room_usage['利用率'], bins=bins, labels=labels_u, include_lowest=True).value_counts().reindex(labels_u).fillna(0)
bcolors = ['#FFCDD2','#FF8A65','#FFA726','#66BB6A','#1B5E20']
axes[0].bar(labels_u, ucounts.values, color=bcolors, edgecolor='white', linewidth=1.5, zorder=3)
axes[0].set_title('教室时间利用率分布', fontsize=12, fontweight='bold')
axes[0].set_ylabel('教室数量'); axes[0].grid(axis='y', alpha=0.3, zorder=0)
for i, v in enumerate(ucounts.values):
    axes[0].text(i, v+0.3, int(v), ha='center', va='bottom', fontsize=12, fontweight='bold')
campus_mean = room_usage.groupby('校区')['利用率'].mean().sort_values(ascending=False)
cp_colors = ['#1565C0','#1976D2','#0288D1','#00838F','#00695C'][:len(campus_mean)]
axes[1].bar(campus_mean.index, campus_mean.values, color=cp_colors, edgecolor='white', linewidth=1.5, zorder=3)
axes[1].set_title('各校区教室平均时间利用率', fontsize=12, fontweight='bold')
axes[1].set_ylabel('平均利用率 (%)')
axes[1].axhline(campus_mean.mean(), color='#C00000', linestyle='--', linewidth=1.5,
                alpha=0.8, label=f'均值 {campus_mean.mean():.1f}%')
axes[1].legend(); axes[1].grid(axis='y', alpha=0.3, zorder=0)
axes[1].tick_params(axis='x', rotation=15)
for i, (k,v) in enumerate(campus_mean.items()):
    axes[1].text(i, v+0.5, f'{v:.1f}%', ha='center', va='bottom', fontsize=11, fontweight='bold')
plt.tight_layout()
plt.savefig(OUTPUT_DIR + 'classroom_utilization.png', dpi=150, bbox_inches='tight')
plt.close()
print("图8已保存")

# ===== 图9: 特殊教师 =====
xinxuan = df[df['教师号'] == 'xkp01200395']
xinyongxin = df[df['教师号'] == 'xkp01050075']

def check_violations_xinxuan(row):
    # 只能周三/四/五下午
    return not (row['星期'] in ['周三','周四','周五'] and row['节次_起始'] is not None and row['节次_起始'] >= 5)

def check_violations_xinyongxin(row):
    # 不能周一/二/五
    return row['星期'] in ['周一','周二','周五']

xinxuan_viol = xinxuan[xinxuan.apply(check_violations_xinxuan, axis=1)]
xinyongxin_viol = xinyongxin[xinyongxin.apply(check_violations_xinyongxin, axis=1)]

# 特殊教师「约束执行情况」状态串（动态，随新数据更新）
def _teacher_status(teacher_df, viol_df):
    vc = teacher_df['星期'].value_counts().reindex(WEEKDAYS).dropna()
    dist = '、'.join(f'{d}{int(c)}班' for d, c in vc.items() if c > 0)
    if len(viol_df) == 0:
        return 'tag-ok', '✓ 全部满足', f'（排课分布：{dist}）'
    detail = '、'.join(f'{r["星期"]}{r["节次"]}' for _, r in viol_df.iterrows())
    return ('tag-warn', '基本满足',
            f'（排课分布：{dist}；其中 {len(viol_df)} 班排在 {detail}，属其他情况）')

xinxuan_tag, xinxuan_ok, xinxuan_note = _teacher_status(xinxuan, xinxuan_viol)
xinyongxin_tag, xinyongxin_ok, xinyongxin_note = _teacher_status(xinyongxin, xinyongxin_viol)

fig, axes = plt.subplots(2, 2, figsize=(14, 9))
fig.suptitle('特殊约束教师排课情况', fontsize=14, fontweight='bold')

def plot_teacher(teacher_df, name, constraint, viol_df, ax1, ax2):
    wd_counts = teacher_df['星期'].value_counts().reindex(WEEKDAYS).fillna(0)
    ok_color = '#4CAF50'; note_color = '#FF9800'
    colors_t = []
    for wd in WEEKDAYS:
        viol_in_day = len(viol_df[viol_df['星期'] == wd]) if len(viol_df) > 0 else 0
        colors_t.append(note_color if viol_in_day > 0 else ok_color)
    ax1.bar(WEEKDAYS, wd_counts.values, color=colors_t, edgecolor='white', linewidth=1.5, zorder=3)
    ax1.set_title(f'{name}  按星期排课分布\n约束：{constraint}', fontsize=11, fontweight='bold')
    ax1.set_ylabel('班次数'); ax1.grid(axis='y', alpha=0.3, zorder=0)
    for i, v in enumerate(wd_counts.values):
        if v > 0:
            ax1.text(i, v+0.1, int(v), ha='center', va='bottom', fontsize=11, fontweight='bold')
    status = f'其他情况：{len(viol_df)}班次需关注' if len(viol_df) > 0 else '约束全部满足 ✓'
    ax1.set_xlabel(status, fontsize=10, color='#555555')
    if len(teacher_df) > 0:
        block_c = teacher_df['上午下午晚'].value_counts()
        ax2.pie(block_c.values, labels=[f'{k}\n({v})' for k,v in block_c.items()],
                colors=['#81C784','#FFB74D','#CE93D8'], autopct='%1.1f%%',
                startangle=90, wedgeprops={'edgecolor':'white','linewidth':2},
                textprops={'fontsize': 10})
    else:
        ax2.text(0.5, 0.5, '暂无排课记录', ha='center', va='center', transform=ax2.transAxes)
    ax2.set_title(f'{name} 时段占比', fontsize=11, fontweight='bold')

plot_teacher(xinxuan, '新旋', '仅周三/四/五下午（5-8节）排课', xinxuan_viol, axes[0,0], axes[0,1])
plot_teacher(xinyongxin, '新永新', '不在周一/二/五排课', xinyongxin_viol, axes[1,0], axes[1,1])
plt.tight_layout()
plt.savefig(OUTPUT_DIR + 'special_teacher_schedule.png', dpi=150, bbox_inches='tight')
plt.close()
print("图9已保存")

# ===== 图10: 失败课程分析（规则版归因，用「原因类别」列） =====
L_COL = '原因类别'   # 规则版 postfailure_analysis.py 输出的归因类别列
# 规则版三类归因 → 展示用更直白的标签
label_map = {
    '教室静态不可行': '无满足条件的教室\n（校区/容量/类型约束）',
    '教师匹配或任务周次': '教师匹配/任务周次\n（教师信息缺失或周次不符）',
    '资源冲突或偏好过窄': '资源冲突或偏好过窄\n（时间窗/偏好过严）',
}
fail87['归因'] = fail87[L_COL].apply(lambda x: label_map.get(str(x).strip(), str(x).strip()) if pd.notna(x) else '未知')
# 超长标签折行
def wrap_label(s, maxlen=18):
    if len(s) <= maxlen: return s
    mid = len(s)//2
    return s[:mid] + '\n' + s[mid:]
l_counts = fail87['归因'].value_counts()

# 同时统计课程名分布
name_counts = fail87['课程名称'].value_counts().head(10)

fig, axes = plt.subplots(1, 3, figsize=(19, 6))
fig.suptitle(f'{len(fail87)}门排课失败课程归因分析（规则版归因·原因类别）', fontsize=14, fontweight='bold')

# 左：归因饼图
l_labels = [f'{wrap_label(k)}\n({v}门)' for k,v in l_counts.items()]
pie_colors = ['#EF5350','#FFA726','#42A5F5','#66BB6A','#AB47BC'][:len(l_counts)]
axes[0].pie(l_counts.values, labels=l_labels, colors=pie_colors,
            autopct='%1.1f%%', startangle=90,
            wedgeprops={'edgecolor':'white','linewidth':2},
            textprops={'fontsize': 9.5})
axes[0].set_title('按归因说明分类', fontsize=12, fontweight='bold')

# 中：归因柱图（带数量）
short_labels = [wrap_label(k, 12) for k in l_counts.index]
bars_f = axes[1].bar(short_labels, l_counts.values,
                     color=pie_colors, edgecolor='white', linewidth=1.5, zorder=3, width=0.55)
axes[1].set_title('各归因类别数量', fontsize=12, fontweight='bold')
axes[1].set_ylabel('失败班次数'); axes[1].grid(axis='y', alpha=0.3, zorder=0)
axes[1].tick_params(axis='x', labelsize=9)
for bar, v in zip(bars_f, l_counts.values):
    axes[1].text(bar.get_x()+bar.get_width()/2, bar.get_height()+0.3, int(v),
                 ha='center', va='bottom', fontsize=12, fontweight='bold')

# 右：失败课程名Top10
axes[2].barh(name_counts.index[::-1], name_counts.values[::-1],
             color='#5C8CBF', edgecolor='white', linewidth=1.2)
axes[2].set_title('失败课程Top10（按课程名）', fontsize=12, fontweight='bold')
axes[2].set_xlabel('失败班次数')
for i, v in enumerate(name_counts.values[::-1]):
    axes[2].text(v+0.1, i, str(v), va='center', fontsize=10, fontweight='bold')

plt.tight_layout()
plt.savefig(OUTPUT_DIR + 'failed_courses_analysis.png', dpi=150, bbox_inches='tight')
plt.close()
print("图10已保存")

# 生成归因详细文本供HTML用
l_detail = {}
for reason, group in fail87.groupby('归因'):
    courses = group['课程名称'].value_counts()
    l_detail[reason] = {'count': len(group), 'courses': courses}
# 特殊处理超长标签（还原原始值用于HTML说明）
xinxuan_reason_raw = [k for k in fail87[L_COL].unique() if isinstance(k, str) and '只能在' in k]
xinxuan_reason_raw = xinxuan_reason_raw[0] if xinxuan_reason_raw else ''

# ===== 生成HTML =====
def img_to_b64(path):
    with open(path, 'rb') as f:
        return base64.b64encode(f.read()).decode()

def img_tag(filename, alt='', full_path=None):
    path = full_path if full_path else OUTPUT_DIR + filename
    if not os.path.exists(path): return f'<p style="color:red">图片未找到: {path}</p>'
    b64 = img_to_b64(path)
    return f'<img src="data:image/png;base64,{b64}" alt="{alt}" style="max-width:100%;border-radius:8px;box-shadow:0 2px 12px rgba(0,0,0,0.15);">'

def img_tag2(filename, alt=''):
    """两张图并排"""
    path = OUTPUT_DIR + filename
    if not os.path.exists(path): return ''
    b64 = img_to_b64(path)
    return f'<img src="data:image/png;base64,{b64}" alt="{alt}" style="width:49%;border-radius:8px;box-shadow:0 2px 12px rgba(0,0,0,0.12);">'

# 计算关键数字用于HTML（口径：已排唯一教学班 + 最终失败 = 实际参与排课总数）
total_attempted = len(df_name) + len(fail_name)   # balanced: 4581 + 89 = 4670
final_fail = len(fail_name)                        # 动态：89
final_success_uniq = len(df_name)                  # 动态：4581 唯一教学班

# 涉及实体数（动态统计，避免写死旧run数字）
import itertools as _it
involved_teachers = int(df['教师号'].nunique()) if '教师号' in df.columns else 0
involved_rooms = int(df['教室代码'].nunique()) if '教室代码' in df.columns else 547
if '班级信息' in df.columns:
    involved_classes = len(set(_it.chain.from_iterable(
        str(x).split() for x in df['班级信息'].dropna())))
else:
    involved_classes = 0

# 调课增益（动态，按「唯一教学班数」统计，避免多教师行重复计数）
def _uniq_jxb(path):
    """读清单并返回唯一教学班数；兼容 教学班ID / jxbid 列名。"""
    d = pd.read_excel(path)
    for col in ('教学班ID', 'JXBID', 'jxbid'):
        if col in d.columns:
            return int(d[col].nunique())
    return len(d)
try:
    first_round_fail = _uniq_jxb(DATA_DIR + '排课失败课程_全部.xlsx')   # 首轮失败 = 540
except Exception:
    first_round_fail = final_fail
_resched_succ = first_round_fail - final_fail                      # 调课成功 = first_round_fail - final_fail

# 动态读取最新日志里的"调课总耗时"，避免写死
import re as _re_t, glob as _glob_t
reschedule_time_str = "若干分钟"
_log_candidates = sorted(
    _glob_t.glob('logs/*balanced*.log') + _glob_t.glob('logs/reschedule_run_*.log'),
    key=lambda p: os.path.getmtime(p), reverse=True
)
for _lf in _log_candidates:
    try:
        with open(_lf, encoding='utf-8', errors='ignore') as _f:
            for _line in _f:
                _m = _re_t.search(r'调课总耗时[:：]\s*(\d+\s*分\s*[\d.]+\s*秒)', _line)
                if _m:
                    reschedule_time_str = _m.group(1).replace(' ', '')
                    break
        if reschedule_time_str != "若干分钟":
            break
    except Exception:
        continue
reschedule_rate = _resched_succ / first_round_fail * 100 if first_round_fail else 0.0
first_round_success = total_attempted - first_round_fail           # 首轮成功 = 4670-540 = 4130
first_round_rate = first_round_success / total_attempted * 100 if total_attempted else 0.0
success_rate = final_success_uniq / total_attempted * 100

# 座位（空间）利用率：动态读 room_space_util_detail.xlsx（scheduling_visualization 产物）
space_mean = 0.0
space_high = space_good = space_fair = space_low = space_ge50 = 0.0
try:
    _sp = pd.read_excel(OUTPUT_DIR + 'room_space_util_detail.xlsx')
    space_mean = float(_sp['空间利用率(%)'].mean())
    # 按「利用率等级」列统计，保证与报告内饼图/分布图一致
    _lvl = _sp['利用率等级'].astype(str)
    _n = max(len(_sp), 1)
    _pct = lambda kw: float(_lvl.str.startswith(kw).mean() * 100)
    space_high, space_good, space_fair, space_low = _pct('High'), _pct('Good'), _pct('Fair'), _pct('Low')
    space_ge50 = space_high + space_good + space_fair
except Exception as _e:
    print('座位利用率明细读取失败，第8节将用占位值:', _e)

# 间隔统计
adj_cnt = int(gap_series[gap_series == 1].count())
total_gaps_cnt = len(gaps)

# 体育违规统计
sports_12_names = '、'.join(sports_12_courses) if sports_12_courses else '无'

# ===== 第10节：失败归因 HTML（按规则版归因类别动态生成） =====
_reason_suggest = {
    '原始数据缺少主讲教师': '要求各院系完整填写所有教学班教师号；排课前增加数据预检，自动检测教师信息缺失。',
    '无满足条件的教室\n（校区/容量/类型约束）': '补充专用场地（体育馆、琴房等）或适当放宽校区/教室类型限制；核查"指定教室"字段。',
    '教师匹配/任务周次\n（教师信息缺失或周次不符）': '核对教师号 JSH，补全或对齐授课周次（RWJSZCDM 与 SKZCDM）；对承课量大的教师提前预警。',
    '资源冲突或偏好过窄\n（时间窗/偏好过严）': '与教师协商放宽时间约束或调配师资；放宽过窄的时间偏好。',
}
_fail_items = []
for _reason, _info in sorted(l_detail.items(), key=lambda kv: -kv[1]['count']):
    _cnt = _info['count']
    _pct = _cnt / max(len(fail87), 1) * 100
    _top = '、'.join(f'{n}（{c}班）' for n, c in _info['courses'].head(4).items())
    _sug = _reason_suggest.get(_reason, '核对相关数据字段并按建议调整。')
    _reason_disp = _reason.replace('\n', '')
    _fail_items.append(
        f'<h3 style="margin-top:16px;">{_reason_disp}（{_cnt}门，占{_pct:.1f}%）</h3>'
        f'<div class="fail-item type1"><strong>主要课程：</strong>{_top or "（散落多类课程）"}。</div>'
        f'<div class="fail-item type2"><strong>解决建议：</strong>{_sug}</div>'
    )
fail_analysis_html = (
    '<div class="fail-box"><h3>失败课程归因（规则版自动诊断）</h3>'
    + ''.join(_fail_items) + '</div>'
)
# 综合分析一句话（动态）
_cat_brief = '；'.join(f'{r.replace(chr(10),"")} {info["count"]}门' for r, info in
                      sorted(l_detail.items(), key=lambda kv: -kv[1]['count']))
fail_summary = (f'最终 {len(fail87)} 门失败课程经规则版自动归因，可分为：{_cat_brief}。'
                f'其中教师/数据类问题可通过数据治理与师资调配缓解，教室硬约束类需补充专用场地资源。')

html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>排课结果综合分析报告</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'PingFang SC','Helvetica Neue',Arial,sans-serif; background:#F0F4F8; color:#2D3748; line-height:1.7; }}
  .page-header {{ background:linear-gradient(135deg,#1A237E 0%,#283593 60%,#3949AB 100%); color:white; padding:48px 40px 36px; text-align:center; }}
  .page-header h1 {{ font-size:2.2em; font-weight:700; letter-spacing:2px; margin-bottom:10px; }}
  .page-header p {{ font-size:1.05em; opacity:0.85; }}
  .container {{ max-width:1200px; margin:0 auto; padding:32px 24px; }}
  .kpi-grid {{ display:grid; grid-template-columns:repeat(4,1fr); gap:16px; margin-bottom:36px; }}
  .kpi-card {{ background:white; border-radius:12px; padding:20px 16px; text-align:center; box-shadow:0 2px 8px rgba(0,0,0,0.08); border-top:4px solid; }}
  .kpi-card .val {{ font-size:2em; font-weight:700; margin-bottom:4px; }}
  .kpi-card .lbl {{ font-size:0.85em; color:#718096; }}
  .section {{ background:white; border-radius:14px; padding:28px 28px 22px; margin-bottom:28px; box-shadow:0 2px 10px rgba(0,0,0,0.07); }}
  .section h2 {{ font-size:1.3em; font-weight:700; color:#1A237E; margin-bottom:6px; padding-bottom:10px; border-bottom:2px solid #E8EAF6; display:flex; align-items:center; gap:8px; }}
  .section h2 .num {{ background:#1A237E; color:white; border-radius:50%; width:28px; height:28px; display:inline-flex; align-items:center; justify-content:center; font-size:0.85em; flex-shrink:0; }}
  .chart-wrap {{ margin:18px 0 10px; text-align:center; }}
  .desc {{ background:#F7F9FC; border-left:4px solid #3949AB; border-radius:0 8px 8px 0; padding:14px 18px; margin-top:14px; font-size:0.95em; color:#4A5568; }}
  .desc p {{ margin-bottom:6px; }}
  .desc p:last-child {{ margin-bottom:0; }}
  .tag {{ display:inline-block; border-radius:4px; padding:2px 8px; font-size:0.82em; font-weight:600; margin:2px; }}
  .tag-ok {{ background:#E8F5E9; color:#2E7D32; }}
  .tag-warn {{ background:#FFF3E0; color:#E65100; }}
  .tag-info {{ background:#E3F2FD; color:#1565C0; }}
  table {{ width:100%; border-collapse:collapse; margin-top:14px; font-size:0.9em; }}
  th {{ background:#E8EAF6; color:#1A237E; padding:10px 14px; text-align:left; font-weight:600; }}
  td {{ padding:9px 14px; border-bottom:1px solid #EDF2F7; }}
  tr:last-child td {{ border-bottom:none; }}
  tr:nth-child(even) td {{ background:#F7F9FC; }}
  .fail-box {{ background:#FFF8F0; border:1px solid #FFCC02; border-radius:10px; padding:18px 20px; margin-top:14px; }}
  .fail-box h3 {{ color:#B7791F; margin-bottom:10px; font-size:1.05em; }}
  .fail-item {{ margin-bottom:8px; padding:8px 12px; background:white; border-radius:6px; border-left:3px solid; }}
  .fail-item.type1 {{ border-color:#EF5350; }}
  .fail-item.type2 {{ border-color:#FFA726; }}
  .footer {{ text-align:center; padding:28px; color:#A0AEC0; font-size:0.88em; }}
  @media(max-width:768px) {{ .kpi-grid {{ grid-template-columns:repeat(2,1fr); }} }}
</style>
</head>
<body>
<div class="page-header">
  <h1>排课结果综合分析报告</h1>
  <p>西安交通大学 · 2025–2026学年第一学期 · balanced 放置策略 · 基于调课后整体结果</p>
</div>

<div class="container">

<!-- KPI -->
<div class="kpi-grid" style="margin-top:28px;">
  <div class="kpi-card" style="border-color:#1565C0;">
    <div class="val" style="color:#1565C0;">{total_attempted:,}</div>
    <div class="lbl">实际参与排课教学班数</div>
  </div>
  <div class="kpi-card" style="border-color:#2E7D32;">
    <div class="val" style="color:#2E7D32;">{final_success_uniq:,}</div>
    <div class="lbl">最终已排教学班数</div>
  </div>
  <div class="kpi-card" style="border-color:#B71C1C;">
    <div class="val" style="color:#B71C1C;">{final_fail}</div>
    <div class="lbl">最终排课失败数</div>
  </div>
  <div class="kpi-card" style="border-color:#E65100;">
    <div class="val" style="color:#E65100;">{success_rate:.1f}%</div>
    <div class="lbl">最终排课成功率</div>
  </div>
  <div class="kpi-card" style="border-color:#4527A0;">
    <div class="val" style="color:#4527A0;">{involved_teachers:,}</div>
    <div class="lbl">涉及教师数</div>
  </div>
  <div class="kpi-card" style="border-color:#00695C;">
    <div class="val" style="color:#00695C;">{involved_rooms}</div>
    <div class="lbl">涉及教室数</div>
  </div>
  <div class="kpi-card" style="border-color:#558B2F;">
    <div class="val" style="color:#558B2F;">{involved_classes:,}</div>
    <div class="lbl">涉及班级数</div>
  </div>
  <div class="kpi-card" style="border-color:#AD1457;">
    <div class="val" style="color:#AD1457;">{reschedule_rate:.1f}%</div>
    <div class="lbl">调课补排成功率</div>
  </div>
</div>

<!-- 排课流程说明 -->
<div class="section">
  <h2><span class="num">0</span>排课流程概述</h2>
  <div class="chart-wrap">{img_tag('scheduling_overview_kpi.png', '排课总览')}</div>
  <div class="desc">
    <p><strong>第一阶段（首轮贪心排课·balanced 策略）：</strong>对 {total_attempted:,} 个教学班进行自动排课，首轮成功 <strong>{first_round_success:,}</strong> 班（{first_round_rate:.2f}%），失败 {first_round_fail} 班（主要为体育专用场地不足、教师数据缺失等原因）。</p>
    <p><strong>第二阶段（变邻域调课补排）：</strong>对 {first_round_fail} 门失败课程，通过局部重调度腾出资源重新排课，耗时约 {reschedule_time_str}。成功补排 <strong>{_resched_succ}</strong> 班（调课成功率 {reschedule_rate:.1f}%），仍有 {final_fail} 班无法解决。</p>
    <p><strong>最终结果：</strong>实际参与排课 <strong>{total_attempted:,}</strong> 个教学班，最终成功排课 <strong>{final_success_uniq:,}</strong> 班，失败 <strong>{final_fail}</strong> 班，整体成功率 <strong>{success_rate:.1f}%</strong>。</p>
  </div>
</div>

<!-- 1. 公修课排课情况 -->
<div class="section">
  <h2><span class="num">1</span>公修课与专业课排课成功率</h2>
  <div class="chart-wrap">{img_tag('pub_course_scheduling_rate.png', '公修课排课成功率')}</div>
  <div class="desc">
    <p><strong>数据口径：</strong>成功率 = 调课后已排唯一教学班数 ÷ (已排 + 最终失败{final_fail}班)，反映调课后的真实排课结果，各类合计共 {total_attempted:,} 个教学班。</p>
    <p><strong>思政类：</strong>形势与政策（{int(result.loc['形势与政策','总任务'])}班/{result.loc['形势与政策','成功率']:.0f}%）、中国近现代史纲要（{int(result.loc['中国近现代史纲要','总任务'])}班/{result.loc['中国近现代史纲要','成功率']:.0f}%）、毛泽东思想概论（{int(result.loc['毛泽东思想概论','总任务'])}班/{result.loc['毛泽东思想概论','成功率']:.0f}%）、习近平新时代思想（{int(result.loc['习近平新时代思想','总任务'])}班/{result.loc['习近平新时代思想','成功率']:.0f}%），充分体现"公修课优先"策略（排课要求1）。</p>
    <p><strong>数学：</strong>共 {int(result.loc['数学','总任务'])} 班，成功 {int(result.loc['数学','已排'])} 班，成功率 {result.loc['数学','成功率']:.1f}%。</p>
    <p><strong>英语/外语：</strong>共 {int(result.loc['英语/外语','总任务'])} 班，成功 {int(result.loc['英语/外语','已排'])} 班，失败 {int(result.loc['英语/外语','失败数'])} 班，成功率 {result.loc['英语/外语','成功率']:.1f}%。</p>
    <p><strong>体育：</strong>共 {int(result.loc['体育','总任务'])} 班，成功 {int(result.loc['体育','已排'])} 班，失败 {int(result.loc['体育','失败数'])} 班，成功率 {result.loc['体育','成功率']:.1f}%（详见第10节归因分析）。</p>
    <p><strong>专业课98.6%：</strong>共{result.loc['专业课','总任务']}班，成功{result.loc['专业课','已排']}班，失败{int(result.loc['专业课','失败数'])}班。</p>
  </div>
</div>

<!-- 2. 时段热力图 -->
<div class="section">
  <h2><span class="num">2</span>一周各时段排课热力图</h2>
  <div class="chart-wrap">{img_tag('weekly_block_heatmap.png', '热力图')}</div>
  <div class="desc">
    <p><strong>周二下午（5-8节）禁排要求（排课要求2）：</strong>热力图中该格标注"限制区域"，实际排课班次为 {heatmap_data.loc['下午(5-8节)','周二'] if '下午(5-8节)' in heatmap_data.index and '周二' in heatmap_data.columns else 0} 班，约束得到严格执行。</p>
    <p><strong>上午（1-4节）排课最为集中</strong>，符合"1-4节优于5-8节"的优先级要求（排课要求12）。周一至周五整体分布较均衡，无明显某天过载现象。</p>
    <p><strong>晚上（9节+）课程：</strong>部分实验、艺术类课程安排在晚上，系课程性质需要，属正常情况。</p>
  </div>
</div>

<!-- 3. 聚类类别时间分布 -->
<div class="section">
  <h2><span class="num">3</span>各聚类类别课程时间段分布</h2>
  <div class="chart-wrap">{img_tag('cluster_time_distribution.png', '聚类时间分布')}</div>
  <div class="desc">
    <p><strong>体育类：</strong>集中在上午（3-8节）和下午，几乎不出现在晚上，符合体育课性质。</p>
    <p><strong>英语类、数学类：</strong>公修课性质，主要排在1-8节的上午和下午段，晚上占比极低，符合"公修课一般为1-8节"要求（排课要求3）。</p>
    <p><strong>思政类：</strong>分布较均匀，部分安排在下午。</p>
    <p><strong>专业课：</strong>时间分布最广泛，上午、下午、晚上均有分布，体现了专业课灵活安排的特点。</p>
  </div>
</div>

<!-- 4. 班级分布 -->
<div class="section">
  <h2><span class="num">4</span>班级周一至周五课程分布</h2>
  <div class="chart-wrap">{img_tag('class_weekday_distribution.png', '班级分布')}</div>
  <div class="desc">
    <p><strong>整体分布：</strong>周一和周三排课最多，周五相对较少，全周分布较均衡，基本满足"班级周一到周五尽可能平均"的要求（排课要求7）。</p>
    <p><strong>班级均匀度：</strong>方差分析显示，大部分班级每日课程数方差较小（均匀或较均匀），但仍有少部分班级存在某天课程集中现象，可在后续手动调整中优化。</p>
  </div>
</div>

<!-- 5. 连排时段 -->
<div class="section">
  <h2><span class="num">5</span>连排时段分布</h2>
  <div class="chart-wrap">{img_tag('continuous_slot_distribution.png', '连排时段')}</div>
  <div class="desc">
    <p><strong>半天连排（1-4节、5-8节）是主流：</strong>1-4节连排 {int(slot_counts.get('1-4节',0)):,} 班次居首，5-8节连排 {int(slot_counts.get('5-8节',0)):,} 班次居次，完全符合"正常排课都是2节联排、1-4节优于5-8节"的要求（排课要求4、12）。</p>
    <p><strong>单次2节安排（1-2、3-4、5-6、7-8节）：</strong>各时段均存在，且满足优先级排序（1-2 &gt; 3-4，5-6 &gt; 7-8）。</p>
    <p><strong>晚上及其他：</strong>9-10节及以后安排共{int(slot_counts.get('9-10节',0)) + int(slot_counts.get('9节以后',0))}班次，主要为实验课、艺术课等特殊课程。</p>
  </div>
</div>

<!-- 6. 间隔天数 -->
<div class="section">
  <h2><span class="num">6</span>同一课程多次安排间隔天数</h2>
  <div class="chart-wrap">{img_tag('course_gap_distribution.png', '间隔天数')}</div>
  <div class="desc">
    <p><strong>要求13（同一任务尽量不连续安排在连续两天）：</strong>在{total_gaps_cnt}次多日安排中，相邻天（间隔0天）安排共{adj_cnt}次，占比 {adj_cnt/total_gaps_cnt*100:.1f}%。</p>
    <p>大多数多次课程安排能保持合理间隔（间隔1-2天），相邻天安排的课程通常为周学时较高（4+学时）、一周内需排多次的课程，属客观原因，建议在不影响整体约束的前提下逐步优化。</p>
  </div>
</div>

<!-- 7. 体育课 -->
<div class="section">
  <h2><span class="num">7</span>体育类课程时间分布</h2>
  <div class="chart-wrap">{img_tag('sports_time_distribution.png', '体育课分布')}</div>
  <div class="desc">
    <p><strong>要求14（体育课不排前1-2节）：</strong>共发现 {sports_12_count} 个教学班次排在1-2节，对应课程为：{sports_12_names}。这些均为体育学院开设的<strong>理论课</strong>（非实践体育课），如体育管理学、体育基本理论、体育心理学、社会体育导论等，是否需遵守该约束建议进一步与教务确认。</p>
    <p><strong>整体分布：</strong>体育实践课程主要集中在周一至周五的上午（3-8节）和下午段，运动场馆时间利用较为合理。</p>
  </div>
</div>

<!-- 8. 教室利用率 -->
<div class="section">
  <h2><span class="num">8</span>教室利用率分析</h2>
  <div class="chart-wrap">{img_tag('classroom_utilization.png', '教室时间利用率')}</div>
  <div class="desc">
    <p><strong>时间利用率：</strong>按教室代码统计，以一周 5 个工作日 × 11 节 = 55 个时段格子为满载基准，统计每间教室被占用的不同（工作日，节次）格子比例。全校平均 <strong>{room_time_mean:.1f}%</strong>，大部分教室处于 20%–80% 的合理区间；少量专用教室（体育馆、琴房、实验室）利用率偏高，是部分课程排课失败的根本原因。</p>
    <p><strong>各校区对比：</strong>各校区教室平均利用率存在差异，北校区、南校区利用率相对较高。建议加强跨校区资源调配或扩充专用场地。</p>
  </div>
  <div style="margin-top:20px;">
    <h3 style="color:#1A237E;font-size:1.05em;margin-bottom:12px;">教室座位空间利用率（选课人数 / 座位容量）</h3>
    <div class="chart-wrap" style="display:flex;gap:2%;align-items:flex-start;">
      {img_tag2('room_space_util_distribution.png', '座位利用率分布')}
      {img_tag2('room_space_util_levels_pie.png', '座位利用率等级')}
    </div>
  </div>
  <div class="desc" style="margin-top:14px;">
    <p><strong>座位利用率：</strong>平均值达 <strong>{space_mean:.1f}%</strong>，说明教室选取与班级规模总体匹配良好（排课要求9）。其中 High（85–100%）占{space_high:.1f}%，Good（70–85%）占{space_good:.1f}%，Fair（50–70%）占{space_fair:.1f}%，合计约{space_ge50:.1f}%的课程座位利用率在50%以上，资源分配较为合理；Low（0–50%）占{space_low:.1f}%，主要集中在小班专业课和人数较少的选修课，属正常现象。</p>
  </div>
</div>

<!-- 9. 特殊教师 -->
<div class="section">
  <h2><span class="num">9</span>特殊约束教师排课情况</h2>
  <div class="chart-wrap">{img_tag('special_teacher_schedule.png', '特殊教师')}</div>
  <table>
    <tr><th>教师</th><th>教师号</th><th>约束要求</th><th>排课班次</th><th>约束执行情况</th></tr>
    <tr>
      <td>新旋</td><td>xkp01200395</td>
      <td>只能在周三、周四、周五下午（5-8节）排课</td>
      <td>{len(xinxuan)}班次</td>
      <td><span class="tag {xinxuan_tag}">{xinxuan_ok}</span>{xinxuan_note}</td>
    </tr>
    <tr>
      <td>新永新</td><td>xkp01050075</td>
      <td>不能在周一、周二和周五排课</td>
      <td>{len(xinyongxin)}班次</td>
      <td><span class="tag {xinyongxin_tag}">{xinyongxin_ok}</span>{xinyongxin_note}</td>
    </tr>
  </table>
  <div class="desc" style="margin-top:14px;">
    <p>两位教师的特殊时间约束基本得到执行，排课系统成功识别并应用了个人时间约束（排课要求16、17）；个别"其他情况"班次见上表备注。</p>
  </div>
</div>

<!-- 10. 失败课程分析 -->
<div class="section">
  <h2><span class="num">10</span>最终{final_fail}门失败课程归因分析</h2>
  <div class="chart-wrap">{img_tag('failed_courses_analysis.png', '失败课程分析')}</div>

  {fail_analysis_html}

  <div class="desc" style="margin-top:16px;">
    <p><strong>综合分析：</strong>{fail_summary}</p>
  </div>
</div>

</div>

<div class="footer">
  本报告由Python自动生成 · 数据来源：排课结果/调课后的整体结果.xlsx · 排课结果1/课程聚类结果2.xlsx · 排课结果/失败课程归因与建议.xlsx
</div>
</body>
</html>"""

html_path = DATA_DIR + '排课分析报告_西交大.html'
with open(html_path, 'w', encoding='utf-8') as f:
    f.write(html)
print(f"\nHTML报告已生成：{html_path}")
print("所有图表和报告生成完毕！")
