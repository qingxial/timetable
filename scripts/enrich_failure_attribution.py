# -*- coding: utf-8 -*-
"""把"卡在哪一步 + 具体根因"逐课回填到失败归因表。

输入：排课结果/失败课程归因与建议.xlsx（postfailure_analysis.py 产出，含枚举拒绝构成）
输出：同文件新增列  卡在步骤 / 具体根因 / 根因证据 / 针对性建议

根因判定（按 jxbid 逐课）：
  ⓪ 教室静态不可行        —— 容量/类型预筛阶段就没有匹配教室
  ⓪ 偏好与ZXS不自洽       —— 连合法日模式都生成不出来
  ① 偏好撞禁排            —— 偏好节次落在「全周(x-y节)」禁排里，被自己禁掉
  ① 偏好节次与连排格子错位 —— 偏好窗口(如6-7节)跨 5-6/7-8 边界，放不下标准2节块
  ① 偏好窗口过窄          —— 窗口合法但与既有占用叠加后无可行位
  ② 教室时段占用
  ③ 教师时段饱和
  ④ 班级时段冲突
"""
import os, sys, re
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); os.chdir(ROOT)

from Basic_Data import load_courses

COURSE_XLSX = "智能排课基础数据/提取的基础数据表_converted/课程表_split_merged.xlsx"
ATTR_XLSX = "排课结果/失败课程归因与建议.xlsx"

courses = load_courses(COURSE_XLSX, weeks=20, days=7, periods=11)
by_id = {c.JXBID: c for c in courses}

def norm(s):
    return str(s or '').replace('（','(').replace('）',')').replace('；',';').replace('，',',').strip()

# 标准 2 节连排块起点(1-indexed)：1,3,5,7,9 → 块 1-2 / 3-4 / 5-6 / 7-8 / 9-10
def block_fits(a, b):
    """偏好节次窗口 [a,b] 内能否放下一个落在标准网格的 2 节块"""
    for start in (1, 3, 5, 7, 9):
        if a <= start and start + 1 <= b:
            return True
    return False

def pref_window(c):
    """从 Prefer_Time 抽 (起节, 止节)；无单时段窗口返回 None"""
    m = re.search(r'周.\((\d+)-(\d+)节?\)', norm(c.Prefer_Time))
    if m:
        return int(m.group(1)), int(m.group(2))
    return None

def forbidden_full_week_periods(c):
    """从 unavailable_Time 抽「全周(a-b节)」禁排的节次区间列表"""
    out = []
    for m in re.finditer(r'全周\((\d+)-(\d+)节?\)', norm(c.unavailable_Time)):
        out.append((int(m.group(1)), int(m.group(2))))
    return out

def parse_reject(s):
    s = str(s)
    if s in ('nan', 'None', ''):
        return {}
    return {k.strip(): int(v) for k, v in re.findall(r'([^×;；]+)×(\d+)', s)}

def deepest_step(items):
    keys = set(items.keys())
    if any('班级' in k for k in keys): return '④ 班级时段冲突'
    if any('教师' in k for k in keys): return '③ 教师时段饱和'
    if any('教室' in k for k in keys): return '② 教室时段占用'
    if keys and keys.issubset({'偏好窗口外', '禁止时段'}): return '①'
    return None

def diagnose(row):
    """返回 (卡在步骤, 具体根因, 根因证据, 针对性建议)"""
    jx = str(row['教学班ID']).strip()
    c = by_id.get(jx)
    reason = str(row['原因类别'])
    detail = str(row['详细说明'])
    items = parse_reject(row['枚举拒绝构成'])

    pref = norm(c.Prefer_Time) if c else ''
    un = norm(c.unavailable_Time) if c else ''
    zxs = c.ZXS if c else ''
    teach = str(row.get('教师占用摘要', '') or '')

    # ⓪ 教室静态不可行
    if reason == '教室静态不可行':
        return ('⓪ 教室预筛',
                '教室静态不可行(容量/类型预筛无匹配教室)',
                f'ZXS={zxs} | Prefer={pref or "无"}',
                '扩候选教室池 / 放宽教室类型或容量匹配 / 错峰教室资源')

    # ⓪ 连日模式都没生成
    if '无法生成任何上课日模式' in detail or ('日模式' in detail and not items):
        return ('⓪ 日模式生成',
                '偏好与ZXS不自洽(生成不出合法日模式)',
                f'ZXS={zxs} | Prefer={pref or "无"} | unavail={un or "无"}',
                '核对 ZXS 与偏好组总学时是否相等；如 ZXS=7 却只偏好周末全天则需改偏好或拆班')

    step = deepest_step(items)

    # ②/③/④：通过了时间窗口，卡在资源占用
    if step == '② 教室时段占用':
        return (step, '教室时段被占满(已通过时间窗口)',
                f'ZXS={zxs} | Prefer={pref or "无"}', '扩候选教室 / 错峰该教室既有占用')
    if step == '③ 教师时段饱和':
        return (step, '教师时段饱和(一对一/带班过多，窗口内被占满)',
                f'{teach[:120]}', '减少该教师任务量 / 放宽偏好窗口 / 拆分一对一课到其它时段')
    if step == '④ 班级时段冲突':
        return (step, '班级时段冲突(学生班级在偏好时段已有课)',
                f'Prefer={pref or "无"}', '调整班级课表 / 放宽偏好窗口')

    # ① 卡在偏好/禁止时段 —— 细分三种具体缺陷
    if step == '①' or (not items and reason == '资源冲突或偏好过窄'):
        win = pref_window(c) if c else None
        forb = forbidden_full_week_periods(c) if c else []
        # 缺陷1：偏好节次落在「全周(x-y节)」禁排区间内
        if win:
            a, b = win
            for fa, fb in forb:
                if not (b < fa or a > fb):  # 区间相交
                    return ('① 偏好撞禁排',
                            '偏好节次被自己的「全周禁排」覆盖(数据自相矛盾)',
                            f'Prefer=周X({a}-{b}节) ⊂ unavail全周({fa}-{fb}节) | 完整unavail={un}',
                            '二选一：删掉 unavailable 里的「全周(x-y节)」，或把偏好改到未被禁排的节次')
            # 缺陷2：偏好窗口与标准 2 节连排格子错位
            if not block_fits(a, b):
                return ('① 偏好节次错位',
                        '偏好节次窗口与标准2节连排格子(1-2/3-4/5-6/7-8)不对齐',
                        f'Prefer=周X({a}-{b}节) 跨块边界，放不下标准2节块',
                        f'把偏好改成对齐网格的相邻块，如 {a}-{b}节 → 5-6节 或 7-8节')
        # 缺陷3：窗口合法但太窄/被占
        return ('① 偏好窗口过窄',
                '偏好窗口合法但与既有占用叠加后无可行连排位',
                f'Prefer={pref or "无"} | unavail={un or "无"} | ZXS={zxs}',
                '放宽 Prefer_Time 到更大区间，或将偏好降级为软约束(窗口排不下时允许溢出)')

    # 兜底
    return ('⓪ 其它', '其它资源叠加冲突',
            f'拒绝构成={dict(items)}', '结合枚举拒绝构成与教师占用摘要逐项排查')

df = pd.read_excel(ATTR_XLSX)
df['教学班ID'] = df['教学班ID'].astype(str)

res = df.apply(diagnose, axis=1, result_type='expand')
res.columns = ['卡在步骤', '具体根因', '根因证据', '针对性建议']

# 插到原"原因类别"后面，保持可读
for col in ['卡在步骤', '具体根因', '根因证据', '针对性建议']:
    df[col] = res[col]

# 重排列：把新列放到原因类别之后
cols = list(df.columns)
for c in ['卡在步骤', '具体根因', '根因证据', '针对性建议']:
    cols.remove(c)
idx = cols.index('原因类别') + 1
new_order = cols[:idx] + ['卡在步骤', '具体根因', '根因证据', '针对性建议'] + cols[idx:]
df = df[new_order]

df.to_excel(ATTR_XLSX, index=False)
print(f'已回填到: {ATTR_XLSX}  (共 {len(df)} 门)')

# 打印汇总
print('\n=== 卡在步骤 分布 ===')
for v, n in df['卡在步骤'].value_counts().items():
    print(f'  {n:>4}  {v}')
print('\n=== 具体根因 分布 ===')
for v, n in df['具体根因'].value_counts().items():
    print(f'  {n:>4}  {v}')

# 体育类逐课样例
print('\n=== 体育/专项 失败课逐课根因样例(前10) ===')
pe = df[df['课程名称'].astype(str).str.contains('体育|专项|武术', na=False)]
for _, r in pe.head(10).iterrows():
    print(f"  {r['教学班ID']:<18} {str(r['课程名称'])[:10]:<10} | {r['卡在步骤']:<12} | {r['具体根因']}")
print(f'  ... 体育/专项失败共 {len(pe)} 门')
