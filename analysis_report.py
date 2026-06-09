"""
排课结果综合分析脚本
"""
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib
from matplotlib.gridspec import GridSpec
import warnings
warnings.filterwarnings('ignore')

# 中文字体
matplotlib.rcParams['font.sans-serif'] = ['PingFang SC', 'Heiti TC', 'STHeiti', 'SimHei', 'Arial Unicode MS']
matplotlib.rcParams['axes.unicode_minus'] = False

OUTPUT_DIR = "排课结果/图表展示/"

# ========== 加载数据 ==========
df = pd.read_excel('排课结果/调课后的整体结果.xlsx')
cluster = pd.read_excel('排课结果1/课程聚类结果2.xlsx')
failed_all = pd.read_excel('排课结果/排课失败课程_全部.xlsx')
try:
    failed_adj = pd.read_excel('排课结果/调课失败的排课失败课程.xlsx')
except:
    failed_adj = pd.DataFrame()

# 节次标准化
def parse_period(s):
    """从'第X-Y节'或'第X节'提取起始节次"""
    import re
    s = str(s)
    m = re.search(r'(\d+)', s)
    return int(m.group(1)) if m else None

def period_to_slot(s):
    """节次字符串 -> 时段标签"""
    s = str(s)
    if '1-2' in s or '第1' in s: return '1-2节'
    if '3-4' in s or '第3' in s: return '3-4节'
    if '5-6' in s or '第5' in s: return '5-6节'
    if '7-8' in s or '第7' in s: return '7-8节'
    if '9-10' in s or '第9' in s: return '9-10节'
    if '1-4' in s: return '1-4节'
    if '5-8' in s: return '5-8节'
    if '9-11' in s or '9-12' in s: return '9节以后'
    return s

def period_to_block(s):
    """节次 -> 上午/下午/晚上"""
    s = str(s)
    first = parse_period(s)
    if first is None: return '未知'
    if first <= 4: return '上午(1-4节)'
    if first <= 8: return '下午(5-8节)'
    return '晚上(9节+)'

df['节次_起始'] = df['节次'].apply(parse_period)
df['时段'] = df['节次'].apply(period_to_slot)
df['上午下午晚'] = df['节次'].apply(period_to_block)

WEEKDAY_ORDER = ['周一','周二','周三','周四','周五','周六','周日']
df['星期_num'] = df['星期'].apply(lambda x: WEEKDAY_ORDER.index(x) if x in WEEKDAY_ORDER else 99)

# 合并聚类信息
cluster_map = cluster.set_index('JXBID')[['类别名称','课程类别名称']].to_dict('index')
df['类别名称'] = df['教学班ID'].map(lambda x: cluster_map.get(x, {}).get('类别名称', '未知'))
df['课程类别名称'] = df['教学班ID'].map(lambda x: cluster_map.get(x, {}).get('课程类别名称', '未知'))

# ========== 公修课关键词识别 ==========
PUBLIC_KEYWORDS = ['体育','英语','数学','形势与政策','中国近现代史','毛泽东思想','习近平']
def is_public(name):
    return any(kw in str(name) for kw in PUBLIC_KEYWORDS)

df['是否公修课'] = df['课程名称'].apply(is_public)

def course_category(name):
    name = str(name)
    if '体育' in name: return '体育'
    if '英语' in name or '大学英语' in name: return '英语'
    if '数学' in name or '高等数学' in name or '线性代数' in name: return '数学'
    if '形势与政策' in name: return '形势与政策'
    if '中国近现代史' in name: return '中国近现代史纲要'
    if '毛泽东' in name: return '毛泽东思想概论'
    if '习近平' in name: return '习近平新时代思想'
    return '专业课'

df['课程类型'] = df['课程名称'].apply(course_category)

# ========== 计算排课总体情况 ==========
# 从日志提取
SCHEDULE_STATS = {
    '总教学班数(排课输入)': 5224,
    '教师数': 1486,
    '教室数': 547,
    '班级数': 1379,
    '排课失败数': 645,
    '调课处理数': 645,
    '调课成功数': 558,
    '调课失败数': 87,
    '最终已排课数': len(df),
    '排课耗时(秒)': 13443,
    '调课耗时(秒)': 771,
}
SCHEDULE_STATS['初次排课成功数'] = SCHEDULE_STATS['总教学班数(排课输入)'] - SCHEDULE_STATS['排课失败数']
SCHEDULE_STATS['初次排课成功率'] = SCHEDULE_STATS['初次排课成功数'] / SCHEDULE_STATS['总教学班数(排课输入)'] * 100
SCHEDULE_STATS['最终排课成功率'] = SCHEDULE_STATS['最终已排课数'] / SCHEDULE_STATS['总教学班数(排课输入)'] * 100

print("=" * 60)
print("排课总体情况")
print("=" * 60)
for k, v in SCHEDULE_STATS.items():
    if isinstance(v, float):
        print(f"  {k}: {v:.2f}%")
    else:
        print(f"  {k}: {v}")

# ========== 图1: 公修课排课成功率 ==========
print("\n分析公修课排课情况...")

# 总任务数需要从聚类结果看
cluster_fail = failed_adj if not failed_adj.empty else pd.DataFrame(columns=['课程名称'])
cluster_still_fail = cluster_fail  # 87门调课还失败的

# 用聚类表作为总任务基准
cluster_counts = cluster.groupby('课程名称').size().reset_index(name='总任务数')
scheduled_counts = df.groupby('课程名称').size().reset_index(name='已排数')
merged = cluster_counts.merge(scheduled_counts, on='课程名称', how='left')
merged['已排数'] = merged['已排数'].fillna(0)
merged['成功率'] = merged['已排数'] / merged['总任务数'] * 100

# 公修课子集
public_courses = {
    '体育': ['体育'],
    '英语': ['英语','大学英语'],
    '数学': ['数学','高等数学','线性代数','概率论'],
    '形势与政策': ['形势与政策'],
    '中国近现代史纲要': ['中国近现代史'],
    '毛泽东思想概论': ['毛泽东思想'],
    '习近平新时代思想': ['习近平'],
}

pub_stats = []
for label, keywords in public_courses.items():
    mask = merged['课程名称'].apply(lambda x: any(kw in str(x) for kw in keywords))
    sub = merged[mask]
    if len(sub) == 0: continue
    pub_stats.append({
        '课程': label,
        '总任务数': sub['总任务数'].sum(),
        '已排数': sub['已排数'].sum(),
    })
pub_df = pd.DataFrame(pub_stats)
pub_df['成功率'] = pub_df['已排数'] / pub_df['总任务数'] * 100

# 专业课
spec_mask = ~merged['课程名称'].apply(lambda x: any(kw in str(x) for kw in
    ['体育','英语','数学','高等数学','线性代数','概率论','形势与政策','中国近现代史','毛泽东','习近平']))
spec_total = merged[spec_mask]['总任务数'].sum()
spec_done = merged[spec_mask]['已排数'].sum()
pub_df = pd.concat([pub_df, pd.DataFrame([{
    '课程': '专业课',
    '总任务数': spec_total,
    '已排数': spec_done,
    '成功率': spec_done/spec_total*100
}])], ignore_index=True)

fig, axes = plt.subplots(1, 2, figsize=(14, 6))
fig.suptitle('公修课与专业课排课情况', fontsize=16, fontweight='bold')

colors = ['#2196F3' if r >= 95 else '#FF9800' if r >= 80 else '#F44336' for r in pub_df['成功率']]
bars = axes[0].bar(pub_df['课程'], pub_df['成功率'], color=colors, edgecolor='white', linewidth=1.5)
axes[0].set_title('各类课程排课成功率', fontsize=13)
axes[0].set_ylabel('成功率 (%)')
axes[0].set_ylim(0, 110)
axes[0].axhline(y=90, color='red', linestyle='--', alpha=0.5, label='90%线')
for bar, val in zip(bars, pub_df['成功率']):
    axes[0].text(bar.get_x()+bar.get_width()/2, bar.get_height()+1, f'{val:.1f}%',
                 ha='center', va='bottom', fontsize=10, fontweight='bold')
axes[0].tick_params(axis='x', rotation=30)
axes[0].legend()

x = np.arange(len(pub_df))
w = 0.35
bars1 = axes[1].bar(x-w/2, pub_df['总任务数'], w, label='总任务数', color='#90CAF9')
bars2 = axes[1].bar(x+w/2, pub_df['已排数'], w, label='已排数', color='#1565C0')
axes[1].set_title('各类课程排课数量对比', fontsize=13)
axes[1].set_ylabel('教学班数量')
axes[1].set_xticks(x)
axes[1].set_xticklabels(pub_df['课程'], rotation=30, ha='right')
axes[1].legend()

plt.tight_layout()
plt.savefig(OUTPUT_DIR + 'pub_course_scheduling_rate.png', dpi=150, bbox_inches='tight')
plt.close()
print(f"  图1已保存")
print(pub_df.to_string(index=False))

# ========== 图2: 一周分块时间段热力图 ==========
print("\n分析一周时间段热力图...")

# 定义时段块
BLOCKS = ['上午(1-4节)', '下午(5-8节)', '晚上(9节+)']
WEEKDAYS = ['周一','周二','周三','周四','周五']

# 构建热力图矩阵
heatmap_data = pd.crosstab(
    df[df['星期'].isin(WEEKDAYS)]['上午下午晚'],
    df[df['星期'].isin(WEEKDAYS)]['星期']
).reindex(index=BLOCKS, columns=WEEKDAYS, fill_value=0)

fig, ax = plt.subplots(figsize=(10, 5))
im = ax.imshow(heatmap_data.values, cmap='YlOrRd', aspect='auto')

ax.set_xticks(range(len(WEEKDAYS)))
ax.set_xticklabels(WEEKDAYS, fontsize=12)
ax.set_yticks(range(len(BLOCKS)))
ax.set_yticklabels(BLOCKS, fontsize=12)

for i in range(len(BLOCKS)):
    for j in range(len(WEEKDAYS)):
        val = heatmap_data.values[i, j]
        # 标注周二下午禁排
        if WEEKDAYS[j] == '周二' and BLOCKS[i] == '下午(5-8节)':
            ax.text(j, i, f'禁排\n({val})', ha='center', va='center',
                    fontsize=11, color='blue', fontweight='bold')
        else:
            ax.text(j, i, str(val), ha='center', va='center',
                    fontsize=13, color='black' if val < heatmap_data.values.max()*0.7 else 'white',
                    fontweight='bold')

plt.colorbar(im, ax=ax, label='排课班次数')
ax.set_title('一周各时段排课热力图（教学班次数）', fontsize=14, fontweight='bold')
plt.tight_layout()
plt.savefig(OUTPUT_DIR + 'weekly_block_heatmap.png', dpi=150, bbox_inches='tight')
plt.close()
print("  图2已保存")

# ========== 图3: 聚类类别时间分布 ==========
print("\n分析聚类类别时间分布...")

cluster_merged = df.copy()
cat_order = ['体育类','英语类','数学类','思政类','公修课','专业课','未知']

fig, axes = plt.subplots(2, 3, figsize=(18, 10))
fig.suptitle('各聚类类别课程时间段分布', fontsize=15, fontweight='bold')
axes = axes.flatten()

cats = df['类别名称'].value_counts().index.tolist()
for idx, cat in enumerate(cats[:6]):
    sub = df[df['类别名称'] == cat]
    slot_counts = sub.groupby(['星期','上午下午晚']).size().unstack(fill_value=0)
    # 确保列顺序
    for col in BLOCKS:
        if col not in slot_counts.columns:
            slot_counts[col] = 0
    slot_counts = slot_counts[BLOCKS]
    slot_counts = slot_counts.reindex([d for d in WEEKDAYS if d in slot_counts.index])

    slot_counts.plot(kind='bar', ax=axes[idx], color=['#4CAF50','#FF9800','#9C27B0'],
                     edgecolor='white', linewidth=0.5)
    axes[idx].set_title(f'{cat} (共{len(sub)}班次)', fontsize=12)
    axes[idx].set_xlabel('')
    axes[idx].set_ylabel('班次数')
    axes[idx].tick_params(axis='x', rotation=0)
    axes[idx].legend(loc='upper right', fontsize=8)

plt.tight_layout()
plt.savefig(OUTPUT_DIR + 'cluster_time_distribution.png', dpi=150, bbox_inches='tight')
plt.close()
print("  图3已保存")

# ========== 图4: 班级周一到周五课程分布 ==========
print("\n分析班级课程分布...")

# 按星期统计班次
weekday_dist = df[df['星期'].isin(WEEKDAYS)].groupby('星期').size().reindex(WEEKDAYS)

# 均匀程度: 计算各班级每天的方差
def extract_classes(班级信息):
    if pd.isna(班级信息): return []
    return str(班级信息).split()

# 展开班级信息
rows = []
for _, row in df[df['星期'].isin(WEEKDAYS)].iterrows():
    classes = extract_classes(row.get('班级信息',''))
    for cls in classes:
        rows.append({'班级': cls, '星期': row['星期']})
class_df = pd.DataFrame(rows)

if len(class_df) > 0:
    class_pivot = class_df.groupby(['班级','星期']).size().unstack(fill_value=0)
    class_pivot = class_pivot.reindex(columns=[d for d in WEEKDAYS if d in class_pivot.columns], fill_value=0)
    class_pivot['方差'] = class_pivot.var(axis=1)
    class_pivot['总课次'] = class_pivot[WEEKDAYS].sum(axis=1)

    fig, axes = plt.subplots(1, 2, figsize=(14, 6))
    fig.suptitle('班级周一到周五课程分布', fontsize=14, fontweight='bold')

    # 全局每天分布
    axes[0].bar(WEEKDAYS, weekday_dist.values, color=['#42A5F5','#EF5350','#66BB6A','#FFA726','#AB47BC'],
                edgecolor='white', linewidth=1.5)
    axes[0].set_title('全校各天总排课班次', fontsize=12)
    axes[0].set_ylabel('排课班次数')
    for i, v in enumerate(weekday_dist.values):
        axes[0].text(i, v+10, str(v), ha='center', va='bottom', fontsize=11, fontweight='bold')

    # 班级每天方差分布
    var_bins = [0, 0.5, 1, 2, 5, 100]
    var_labels = ['极均匀\n(0-0.5)', '均匀\n(0.5-1)', '较均匀\n(1-2)', '不均匀\n(2-5)', '很不均匀\n(>5)']
    var_counts = pd.cut(class_pivot['方差'], bins=var_bins, labels=var_labels).value_counts().reindex(var_labels)
    colors_v = ['#1B5E20','#4CAF50','#FFC107','#FF5722','#B71C1C']
    axes[1].bar(var_labels, var_counts.values, color=colors_v, edgecolor='white', linewidth=1.5)
    axes[1].set_title('班级每日课程均匀程度分布', fontsize=12)
    axes[1].set_ylabel('班级数量')
    for i, v in enumerate(var_counts.values):
        axes[1].text(i, v+1, str(v), ha='center', va='bottom', fontsize=11, fontweight='bold')

    plt.tight_layout()
    plt.savefig(OUTPUT_DIR + 'class_weekday_distribution.png', dpi=150, bbox_inches='tight')
    plt.close()
    print("  图4已保存")

# ========== 图5: 连排时段分布 ==========
print("\n分析连排时段分布...")

# 节次 -> 连排类型
def classify_continuous(period_str):
    s = str(period_str)
    if '1-4' in s: return '1-4节(半天连排)'
    if '5-8' in s: return '5-8节(半天连排)'
    if '1-2' in s: return '1-2节'
    if '3-4' in s: return '3-4节'
    if '5-6' in s: return '5-6节'
    if '7-8' in s: return '7-8节'
    if '9-10' in s: return '9-10节'
    if '1-8' in s: return '1-8节(全天连排)'
    if '9-11' in s or '9-12' in s: return '9节以后'
    return '其他'

df['连排类型'] = df['节次'].apply(classify_continuous)

slot_order = ['1-2节','3-4节','5-6节','7-8节','1-4节(半天连排)','5-8节(半天连排)','9-10节','1-8节(全天连排)','9节以后','其他']
slot_counts = df['连排类型'].value_counts().reindex(slot_order).dropna()

# 按要求的优先级着色 (要求12: 1-4>5-8>9-10, 1-2>3-4, 5-6>7-8)
priority_colors = {
    '1-2节': '#1B5E20', '3-4节': '#4CAF50',
    '5-6节': '#0D47A1', '7-8节': '#1976D2',
    '1-4节(半天连排)': '#F57F17', '5-8节(半天连排)': '#FF8F00',
    '9-10节': '#B71C1C', '1-8节(全天连排)': '#6A1B9A', '9节以后': '#880E4F', '其他': '#757575'
}

fig, ax = plt.subplots(figsize=(12, 6))
bars = ax.bar(slot_counts.index, slot_counts.values,
              color=[priority_colors.get(k,'#757575') for k in slot_counts.index],
              edgecolor='white', linewidth=1.5)
ax.set_title('连排时段分布情况', fontsize=14, fontweight='bold')
ax.set_ylabel('排课班次数')
ax.tick_params(axis='x', rotation=25)
for bar, val in zip(bars, slot_counts.values):
    ax.text(bar.get_x()+bar.get_width()/2, bar.get_height()+5, str(val),
            ha='center', va='bottom', fontsize=11, fontweight='bold')
plt.tight_layout()
plt.savefig(OUTPUT_DIR + 'continuous_slot_distribution.png', dpi=150, bbox_inches='tight')
plt.close()
print("  图5已保存")
print(slot_counts.to_string())

# ========== 图6: 同一任务间隔天数分布 ==========
print("\n分析同一任务排课间隔...")

# 对每个教学班，如果有多个星期安排，计算间隔
def compute_gap(group):
    days = sorted(group['星期_num'].unique())
    if len(days) < 2: return []
    return [days[i+1]-days[i] for i in range(len(days)-1)]

gaps = []
for jxbid, group in df.groupby('教学班ID'):
    g = compute_gap(group)
    gaps.extend(g)

gap_series = pd.Series(gaps)
gap_counts = gap_series.value_counts().sort_index()

fig, axes = plt.subplots(1, 2, figsize=(14, 5))
fig.suptitle('同一课程多次安排间隔分析', fontsize=14, fontweight='bold')

day_labels = {1:'相邻天(1天)', 2:'间隔1天(2天)', 3:'间隔2天', 4:'间隔3天', 5:'间隔4天', 6:'周末跨越'}
gap_plot = gap_counts.copy()
gap_plot.index = [day_labels.get(i, f'{i}天') for i in gap_plot.index]
colors_g = ['#F44336' if '相邻' in str(i) else '#4CAF50' for i in gap_plot.index]

axes[0].bar(gap_plot.index, gap_plot.values, color=colors_g, edgecolor='white', linewidth=1.5)
axes[0].set_title('间隔天数分布（红色=相邻天，违反要求13）', fontsize=11)
axes[0].set_ylabel('出现次数')
for bar, val in zip(axes[0].patches, gap_plot.values):
    axes[0].text(bar.get_x()+bar.get_width()/2, bar.get_height()+2, str(val),
                 ha='center', va='bottom', fontsize=10)
axes[0].tick_params(axis='x', rotation=20)

# 按班次数分组显示间隔合规率
total_gaps = len(gaps)
adj_gaps = sum(1 for g in gaps if g == 1)
non_adj = total_gaps - adj_gaps
wedges, texts, autotexts = axes[1].pie(
    [non_adj, adj_gaps],
    labels=[f'非相邻天\n({non_adj}, {non_adj/total_gaps*100:.1f}%)',
            f'相邻天(违规)\n({adj_gaps}, {adj_gaps/total_gaps*100:.1f}%)'],
    colors=['#4CAF50','#F44336'], autopct='', startangle=90,
    wedgeprops={'edgecolor':'white','linewidth':2})
axes[1].set_title('多次排课相邻天违规比例', fontsize=11)

plt.tight_layout()
plt.savefig(OUTPUT_DIR + 'course_gap_distribution.png', dpi=150, bbox_inches='tight')
plt.close()
print("  图6已保存")

# ========== 图7: 体育课时间分布 ==========
print("\n分析体育课时间分布...")

sports = df[df['课程名称'].str.contains('体育', na=False)]

fig, axes = plt.subplots(1, 3, figsize=(16, 5))
fig.suptitle('体育类课程时间分布', fontsize=14, fontweight='bold')

# 按星期
wd_counts = sports['星期'].value_counts().reindex(WEEKDAYS).fillna(0)
axes[0].bar(WEEKDAYS, wd_counts.values, color='#26A69A', edgecolor='white', linewidth=1.5)
axes[0].set_title('体育课按星期分布', fontsize=12)
axes[0].set_ylabel('班次数')
for i, v in enumerate(wd_counts.values):
    axes[0].text(i, v+0.5, int(v), ha='center', va='bottom', fontsize=11, fontweight='bold')

# 按时段
period_counts = sports['连排类型'].value_counts()
axes[1].bar(period_counts.index, period_counts.values, color='#26C6DA', edgecolor='white', linewidth=1.5)
axes[1].set_title('体育课按节次分布（不应排1-2节）', fontsize=11)
axes[1].set_ylabel('班次数')
axes[1].tick_params(axis='x', rotation=25)
# 标记违规
for bar, idx in zip(axes[1].patches, period_counts.index):
    if '1-2' in str(idx):
        bar.set_color('#F44336')
        axes[1].text(bar.get_x()+bar.get_width()/2, bar.get_height()+0.5,
                     '违规!', ha='center', va='bottom', color='red', fontsize=9)
    else:
        axes[1].text(bar.get_x()+bar.get_width()/2, bar.get_height()+0.5,
                     str(int(bar.get_height())), ha='center', va='bottom', fontsize=10)

# 上午/下午/晚上
block_counts = sports['上午下午晚'].value_counts()
axes[2].pie(block_counts.values, labels=[f'{k}\n({v})' for k,v in block_counts.items()],
            colors=['#80DEEA','#4DB6AC','#00695C'], autopct='%1.1f%%',
            wedgeprops={'edgecolor':'white','linewidth':2})
axes[2].set_title('体育课时段占比', fontsize=12)

plt.tight_layout()
plt.savefig(OUTPUT_DIR + 'sports_time_distribution.png', dpi=150, bbox_inches='tight')
plt.close()
print("  图7已保存")

# 违规检查：体育课排在1-2节
sports_12 = sports[sports['节次'].str.contains('1-2|第1节|第2节', na=False)]
print(f"  体育课排在1-2节(违规): {len(sports_12)}班次")
if len(sports_12) > 0:
    print(sports_12[['教学班ID','课程名称','星期','节次']].to_string())

# ========== 图8: 教室利用率 ==========
print("\n分析教室利用率...")

# 时间利用率: 总可用时段 = 5天 × (1-8节共4时段) = 20, 去掉周二下午=19
TOTAL_SLOTS_PER_ROOM = 5 * 3 - 1  # 14 (上午+下午+晚上，去掉周二下午)
room_usage = df.groupby('教室').agg(
    已排班次=('教学班ID','count'),
    平均课容量=('课容量','mean'),
    教室代码=('教室代码','first'),
    校区=('校区','first')
).reset_index()

# 人数利用率: 课容量/教室实际容量（这里用课容量作为代理）
room_time_util = room_usage['已排班次'] / TOTAL_SLOTS_PER_ROOM * 100
room_time_util = room_time_util.clip(0, 100)

fig, axes = plt.subplots(1, 2, figsize=(14, 6))
fig.suptitle('教室利用率分析', fontsize=14, fontweight='bold')

# 时间利用率分布
util_bins = [0,20,40,60,80,100,200]
util_labels = ['0-20%','20-40%','40-60%','60-80%','80-100%','>100%']
util_counts = pd.cut(room_time_util, bins=util_bins, labels=util_labels).value_counts().reindex(util_labels).fillna(0)
bar_colors = ['#B71C1C','#FF5722','#FFA726','#66BB6A','#1B5E20','#4A148C']
axes[0].bar(util_labels, util_counts.values, color=bar_colors, edgecolor='white', linewidth=1.5)
axes[0].set_title('教室时间利用率分布', fontsize=12)
axes[0].set_ylabel('教室数量')
axes[0].set_xlabel('时间利用率')
for i, v in enumerate(util_counts.values):
    axes[0].text(i, v+0.3, int(v), ha='center', va='bottom', fontsize=11, fontweight='bold')

# 校区对比
campus_util = pd.DataFrame({'利用率': room_time_util, '校区': room_usage['校区']})
campus_mean = campus_util.groupby('校区')['利用率'].mean().sort_values(ascending=False)
axes[1].bar(campus_mean.index, campus_mean.values, color=['#1565C0','#0288D1','#00ACC1','#00897B'][:len(campus_mean)],
            edgecolor='white', linewidth=1.5)
axes[1].set_title('各校区教室平均时间利用率', fontsize=12)
axes[1].set_ylabel('平均利用率 (%)')
axes[1].axhline(y=campus_mean.mean(), color='red', linestyle='--', alpha=0.7, label=f'总均值 {campus_mean.mean():.1f}%')
for i, (k, v) in enumerate(campus_mean.items()):
    axes[1].text(i, v+0.5, f'{v:.1f}%', ha='center', va='bottom', fontsize=11, fontweight='bold')
axes[1].legend()
axes[1].tick_params(axis='x', rotation=15)

plt.tight_layout()
plt.savefig(OUTPUT_DIR + 'classroom_utilization.png', dpi=150, bbox_inches='tight')
plt.close()
print("  图8已保存")

# ========== 图9: 新旋、新永新老师排课情况 ==========
print("\n分析新旋、新永新老师排课情况...")

xinxuan = df[df['教师号'] == 'xkp01200395']
xinyongxin = df[df['教师号'] == 'xkp01050075']

fig, axes = plt.subplots(2, 2, figsize=(14, 10))
fig.suptitle('特殊约束教师排课情况分析', fontsize=14, fontweight='bold')

def teacher_analysis(teacher_df, teacher_name, constraint, ax1, ax2):
    if len(teacher_df) == 0:
        ax1.text(0.5, 0.5, f'{teacher_name}\n无排课记录', ha='center', va='center',
                 transform=ax1.transAxes, fontsize=14)
        ax2.text(0.5, 0.5, '无数据', ha='center', va='center', transform=ax2.transAxes)
        return

    wd_counts = teacher_df['星期'].value_counts().reindex(WEEKDAYS).fillna(0)
    colors_t = []
    violations = []
    for i, (wd, cnt) in enumerate(zip(WEEKDAYS, wd_counts)):
        # 判断是否违规
        if teacher_name == '新旋' and wd not in ['周三','周四','周五']:
            colors_t.append('#F44336' if cnt > 0 else '#E0E0E0')
            if cnt > 0: violations.append(f'{wd}排课{int(cnt)}次(违规)')
        elif teacher_name == '新永新' and wd in ['周一','周二','周五']:
            colors_t.append('#F44336' if cnt > 0 else '#E0E0E0')
            if cnt > 0: violations.append(f'{wd}排课{int(cnt)}次(违规)')
        else:
            colors_t.append('#4CAF50')

    ax1.bar(WEEKDAYS, wd_counts.values, color=colors_t, edgecolor='white', linewidth=1.5)
    ax1.set_title(f'{teacher_name} 按星期排课分布\n约束: {constraint}', fontsize=11)
    ax1.set_ylabel('班次数')
    for i, v in enumerate(wd_counts.values):
        if v > 0:
            ax1.text(i, v+0.1, int(v), ha='center', va='bottom', fontsize=11, fontweight='bold')

    if violations:
        viol_text = '违规: ' + '; '.join(violations)
        ax1.set_xlabel(viol_text, color='red', fontsize=9)
    else:
        ax1.set_xlabel('✓ 无违规', color='green', fontsize=10)

    # 时段分布
    slot_c = teacher_df['上午下午晚'].value_counts()
    ax2.pie(slot_c.values, labels=[f'{k}\n({v})' for k,v in slot_c.items()],
            colors=['#81C784','#FFB74D','#CE93D8'], autopct='%1.1f%%',
            wedgeprops={'edgecolor':'white','linewidth':2})
    ax2.set_title(f'{teacher_name} 时段占比', fontsize=11)

teacher_analysis(xinxuan, '新旋', '只能周三/四/五下午排课', axes[0,0], axes[0,1])
teacher_analysis(xinyongxin, '新永新', '不能周一/二/五排课', axes[1,0], axes[1,1])

plt.tight_layout()
plt.savefig(OUTPUT_DIR + 'special_teacher_schedule.png', dpi=150, bbox_inches='tight')
plt.close()
print("  图9已保存")

# 详细违规报告
print("\n  === 新旋老师排课明细 ===")
if len(xinxuan) > 0:
    print(xinxuan[['课程名称','星期','节次','教室','班级信息']].to_string())
    # 违规检查：只能周三、周四、周五下午
    xinxuan_viol = xinxuan[~(xinxuan['星期'].isin(['周三','周四','周五']) & (xinxuan['节次_起始'] >= 5))]
    print(f"  违规班次: {len(xinxuan_viol)}")
    if len(xinxuan_viol) > 0:
        print(xinxuan_viol[['教学班ID','课程名称','星期','节次']].to_string())

print("\n  === 新永新老师排课明细 ===")
if len(xinyongxin) > 0:
    print(xinyongxin[['课程名称','星期','节次','教室','班级信息']].to_string())
    # 违规检查：不能周一、周二、周五
    xinyongxin_viol = xinyongxin[xinyongxin['星期'].isin(['周一','周二','周五'])]
    print(f"  违规班次: {len(xinyongxin_viol)}")
    if len(xinyongxin_viol) > 0:
        print(xinyongxin_viol[['教学班ID','课程名称','星期','节次']].to_string())

# ========== 图10: 综合KPI总览 ==========
print("\n生成综合KPI图...")

fig = plt.figure(figsize=(16, 10))
fig.patch.set_facecolor('#F5F5F5')
gs = GridSpec(3, 4, figure=fig, hspace=0.5, wspace=0.4)

ax_title = fig.add_subplot(gs[0, :])
ax_title.axis('off')
ax_title.text(0.5, 0.7, '排课系统综合分析报告', ha='center', va='center',
              fontsize=20, fontweight='bold', color='#1A237E',
              transform=ax_title.transAxes)
ax_title.text(0.5, 0.2, '基于调课后整体结果 · 2024-2025学年', ha='center', va='center',
              fontsize=12, color='#546E7A', transform=ax_title.transAxes)

kpis = [
    ('总教学班数', f"{SCHEDULE_STATS['总教学班数(排课输入)']:,}", '#1565C0'),
    ('最终已排课数', f"{SCHEDULE_STATS['最终已排课数']:,}", '#2E7D32'),
    ('最终失败数', f"{SCHEDULE_STATS['调课失败数']:,}", '#B71C1C'),
    ('最终成功率', f"{SCHEDULE_STATS['最终排课成功率']:.1f}%", '#E65100'),
    ('教师数', f"{SCHEDULE_STATS['教师数']:,}", '#4527A0'),
    ('教室数', f"{SCHEDULE_STATS['教室数']:,}", '#00695C'),
    ('班级数', f"{SCHEDULE_STATS['班级数']:,}", '#558B2F'),
    ('调课成功率', '86.5%', '#AD1457'),
]

positions = [(1,0),(1,1),(1,2),(1,3),(2,0),(2,1),(2,2),(2,3)]
for (row, col), (label, value, color) in zip(positions, kpis):
    ax = fig.add_subplot(gs[row, col])
    ax.set_facecolor(color)
    ax.axis('off')
    ax.text(0.5, 0.6, value, ha='center', va='center', fontsize=22, fontweight='bold',
            color='white', transform=ax.transAxes)
    ax.text(0.5, 0.15, label, ha='center', va='center', fontsize=11,
            color='white', alpha=0.9, transform=ax.transAxes)
    for spine in ax.spines.values():
        spine.set_visible(False)

plt.savefig(OUTPUT_DIR + 'comprehensive_kpi.png', dpi=150, bbox_inches='tight')
plt.close()
print("  图10已保存")

print("\n" + "="*60)
print("所有分析图表已保存至:", OUTPUT_DIR)
print("="*60)

# ========== 输出文字总结 ==========
print("""
================================================================================
                          排课结果综合分析总结
================================================================================

【一、排课规模】
  · 总教学班数: 5,224班  教师: 1,486人  教室: 547间  班级: 1,379个
  · 初次排课成功: 4,579班 (87.66%)，失败: 645班
  · 经调课后成功: 558班，最终仍失败: 87班
  · 最终排课成功率: 98.33%
  · 排课耗时: ~3.73小时；调课耗时: ~12分52秒

【二、公修课排课情况】
  见 pub_course_scheduling_rate.png

【三、约束遵守情况】
  · 周二下午(5-8节)禁排: 通过热力图可验证
  · 体育课不排1-2节: 见 sports_time_distribution.png
  · 新旋老师(周三/四/五下午): 见 special_teacher_schedule.png
  · 新永新老师(不能周一/二/五): 见 special_teacher_schedule.png

【四、时段分布】
  · 见 continuous_slot_distribution.png、weekly_block_heatmap.png

【五、班级均匀分布】
  · 见 class_weekday_distribution.png

【六、教室利用率】
  · 见 classroom_utilization.png

================================================================================
""")
