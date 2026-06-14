"""
排课时间约束解析与时段可用性检查模块。

主要内容：
  - parse_cell_to_struct / build_preferences：把「指定/避免排课时间」的中文文本
    解析成结构化的时间约束（周次、星期、节次）
  - new_generate_day_patterns：根据周学时和时间约束生成可行的“天-节次”组合模式
  - new_check_period_availability / new_only_time：检查教师、班级、教室在候选时段是否可用

注意：与 utils1.py 互相导入，两个文件需保持在同一目录。
"""
import reschedule_by_adjusting
import os
from typing import List, Dict, Any, Tuple
from Basic_Data import *
from utils1 import *
import re
import math 


DASH_VARIANTS = "－–—~～至到"   # 常见连接符
WEEK_WORDS = {"星期一":"周一","星期二":"周二","星期三":"周三",
              "星期四":"周四","星期五":"周五","星期六":"周六","星期日":"周日","星期天":"周日","周天":"周日"}

def _normalize_text(s: str) -> str:
    s = s.strip()
    # 标点统一
    s = s.replace("，", "、")
    s = s.replace("(", "（").replace(")", "）")
    # 连字符统一为 '-'
    s = re.sub(f"[{DASH_VARIANTS}]", "-", s)
    # 去掉多余空格（括号内外常见）
    s = re.sub(r"\s+", "", s)
    # 星期 -> 周
    for k, v in WEEK_WORDS.items():
        s = s.replace(k, v)
    return s


DAY2IDX = {"周一":0,"周二":1,"周三":2,"周四":3,"周五":4,"周六":5,"周日":6}
WORKDAYS = [0,1,2,3,4]


def parse_cell_to_struct(cell: str) -> Dict[str, Any]:
    """
    解析一格（Prefer_Time 或 Unavailable_Time）文本为 {preferred_groups, preferred_options}
    若是 Unavailable_Time，只用 preferred_options 当作 unavailable 列表返回，外层再装配。
    """
    if not cell or not isinstance(cell, str):
        return {"preferred_groups": [], "preferred_options": []}
    cell = _normalize_text(cell)
    #cell = cell.strip().replace("，", "、")  # 统一顿号
    groups, options = [], []

    # 1) 抓绑定组 [ ... ]
    cell = cell.replace("【", "[").replace("】", "]") #中文括号可以统一成英文
    group_pat = re.compile(r"\[([^\]]+)\]")
    group_strs = group_pat.findall(cell)
    for g in group_strs:
        # 组内用顿号分
        #parts = [p.strip() for p in g.split("、") if p.strip()]
        parts = [p.strip() for p in re.split(r"[、，, ；;]+", g) if p.strip()]
        group_slots = [parse_one_slot(p) for p in parts]
        groups.append(group_slots)

    # 去掉已解析的组，剩下的是可选（分号切分）
    remain = group_pat.sub("", cell)
    for seg in [s.strip() for s in re.split(r"[；;]", remain) if s.strip()]:
        # 可选段内有些人会用顿号继续列子项，这里允许一条 seg 里多项
        parts = [p.strip() for p in re.split(r"[、，]", seg) if p.strip()]
        for p in parts:
            options.append(parse_one_slot(p))

    return {"preferred_groups": groups, "preferred_options": options}

# 返回的是周，日，可用窗口
def parse_one_slot(text: str) -> Dict[str, Any]:
    """
    支持：
      - 周二全天
      - 周一（1-4节）
      - 全周（1-2节）   —— 默认工作日
      weeks的all指向教师的所有上课周次，days的所有all指向工作日,periods的all指向当天的所有节次（第 1 节到第 N 节）。
      periods表示的不是最后的精确区间，而是，可用窗口
      返回的是周，日，可用窗口
    """
    text = _normalize_text(text)

    # 全周（a-b节）
    m = re.match(r"^全周（(\d+)-(\d+)（?节?）?$", text)
    if m:
        a, b = int(m.group(1))-1, int(m.group(2))-1
        #return {"weeks":"all", "days":"all", "periods":[[a,b]]}
        return { "days": "all", "periods": [[a, b]]}

    # 周X 全天
    m = re.match(r"^(周一|周二|周三|周四|周五|周六|周日)全天$", text)
    if m:
        d = DAY2IDX[m.group(1)]
        #return {"weeks": "all", "days": [d], "periods": "all"}
        return { "days":[d], "periods":"all"}

    # 周X（a-b节），periods表示的不是最后的精确区间，而是，可用窗口
    m = re.match(r"^(周一|周二|周三|周四|周五|周六|周日)（(\d+)-(\d+)（?节?）?$", text)
    if m:
        d = DAY2IDX[m.group(1)]
        a, b = int(m.group(2))-1, int(m.group(3))-1
        return { "days":[d], "periods":[[a,b]]}

    # 单独写“周一/周二… ”视作“当天全天”
    m = re.match(r"^(周一|周二|周三|周四|周五|周六|周日)$", text)
    if m:
        d = DAY2IDX[m.group(1)]
        return { "days":[d], "periods":"all"}

    # 兜底：无效，认为没有要求
    return { "days":[], "periods":[]}

def build_preferences(prefer_cell: str, unavail_cell: str) -> Dict[str, Any]:
    p = parse_cell_to_struct(prefer_cell)
    u = parse_cell_to_struct(unavail_cell)

    return {
        "preferred_groups": p["preferred_groups"],    # list[list[SlotSpec]]
        "preferred_options": p["preferred_options"],  # list[SlotSpec]
        "unavailable": u["preferred_options"]         # list[SlotSpec]
    }


def summarize_preferred_options(preferred_options):
    """
    返回：
      allowed_days: set[int]
      day2maxlen: Dict[int, int]  # 每天可容纳的最长连续学时
      days_of_week：all代表的一周可用的工作日
      periods_per_day:all代表的一天可用的最长时段
    """
    allowed_days = set()
    day2maxlen = {} #初始化一个空字典：键=天（int），值=该天允许窗口里的最长连续长度。
    for item in preferred_options or []:
        days_spec = item.get('days', [])
        if days_spec == 'all':
            days = list(range(5))   #如果"days"==all的话就是等于[0,1,2,3,4]
        else:
            days=list(days_spec or [])
        if not days:  # 没有可用 day 就跳过这个条目
            continue
        periods_spec = item.get('periods', [])
        if periods_spec == 'all':
            item_max_len = 8 #如果等于all,就应该是8天,periods_per_day
        else:
            item_max_len = 0
            for pr in periods_spec or []:
                if not (isinstance(pr, (list, tuple)) and len(pr) == 2):
                    continue
                hour = pr[1] - pr[0] + 1
                item_max_len = max(item_max_len, hour)  # 用当前窗口长度更新“本条目内的最长长度”
        # 汇总到各天
        for d in days:
            allowed_days.add(d)
            day2maxlen[d] = max(item_max_len, day2maxlen.get(d, 0))
    return allowed_days, day2maxlen


def _fixed_slots_from_preferred_options(preferred_options: List[Dict]) -> Optional[List[Dict]]:
    """将 preferred_options 各项转为与 preferred_groups 内 slot 一致的结构：每项 days=[单天], periods=[[a,b]]。
    若存在 days/periods 为 'all'、非法或无法固定为闭区间块，返回 None。
    """
    out: List[Dict] = []
    for item in preferred_options or []:
        days_spec = item.get("days")
        pspec = item.get("periods")
        if days_spec in (None, [], "all"):
            return None
        if pspec in (None, [], "all"):
            return None
        if not isinstance(days_spec, list):
            return None
        if not isinstance(pspec, list):
            return None
        blocks: List[Tuple[int, int]] = []
        for pr in pspec:
            if not (isinstance(pr, (list, tuple)) and len(pr) == 2):
                return None
            a, b = int(pr[0]), int(pr[1])
            if a > b:
                return None
            blocks.append((a, b))
        if not blocks:
            return None
        for d in days_spec:
            di = int(d)
            for (a, b) in blocks:
                out.append({"days": [di], "periods": [[a, b]]})
    return out if out else None


def _try_promote_options_to_preferred_groups(
    preferred_options: List[Dict], zxs: int
) -> Optional[List[List[Dict]]]:
    """当 options 可完全固定为若干「单天+单连续块」slot，且总学时等于 zxs 时，返回 [[slots...]]，否则 None。"""
    slots = _fixed_slots_from_preferred_options(preferred_options)
    if not slots:
        return None
    total = sum(s["periods"][0][1] - s["periods"][0][0] + 1 for s in slots)
    if total != zxs:
        return None
    return [slots]


#生成每天上几个小时列表
def new_generate_day_patterns(zxs, days_of_week, time_constraints=None,flag_reschedule=False):
    """根据不同ZXS值和时间限制生成特定的上课天数模式

    Args:
        zxs: 周学时数
        days_of_week: 每周可排课天数
        flag_reschedule: 是否为调课模式
        time_constraints: 时间限制字典，包含 preferred_groups, preferred_options, unavailable

    Returns:
        list: 包含(上课天列表，每天学时数)元组的列表
    """
    patterns = []
    time_constraints = time_constraints or {'preferred_groups': [], 'preferred_options': [], 'unavailable': []}
    preferred_groups = list(time_constraints.get('preferred_groups', []) or [])
    preferred_options = time_constraints.get('preferred_options', []) or []
    zxs = math.ceil(zxs)    #先向上取整
    # 无显式 [] 组、仅有 options 且各槽可固定为「单天+单连续块」、总学时=zxs → 提升为 preferred_groups（与解析出的组结构一致），并清空 options，便于后续 availability 走 groups 分支
    if not preferred_groups and preferred_options:
        promoted = _try_promote_options_to_preferred_groups(preferred_options, zxs)
        if promoted is not None:
            time_constraints['preferred_groups'] = promoted
            time_constraints['preferred_options'] = []
            preferred_groups = promoted
            preferred_options = []

    # === 新增：根据是否有时间偏好，确定“基础可用天” ===
    HAS_TIME_PREF = bool(preferred_groups or preferred_options)  # 只看偏好；unavailable 不扩天
    if HAS_TIME_PREF:
        base_days = days_of_week  # 有偏好 → 一周七天都可考虑
    else:
        base_days = min(days_of_week, 5)  # 无偏好 → 只考虑工作日 0..4

    # 处理 preferred_groups（强制时间）
    if preferred_groups:
        for group in preferred_groups:
            days_list = []
            hours_list = []
            total_hours = 0
            for slot in group:
                d = slot.get('days',[])
                if not isinstance(d, list) or len(d) != 1:
                    raise ValueError(f"slot.days 必须是单个天，如 [0]，收到：{d}") ##即数据格式必须和要求严格一致
                day = int(d[0])
                periods = slot.get('periods',[])
                if (not isinstance(periods, list)) or len(periods) != 1:
                    raise ValueError(f"slot.periods 只支持一个连续块，如 [[9,10]]，收到：{periods}")  ##即数据格式必须和要求严格一致
                block = periods[0]
                hours = block[1] - block[0] + 1 #时段

                days_list.append(day)
                hours_list.append(hours)
                total_hours += hours
            if total_hours != zxs:
                # 不符合该课程周学时，丢弃这个 group，要和周学时匹配
                continue
            # 按 day 排序，保证稳定输出，所以先打包然后再解包
            zipped = sorted(zip(days_list, hours_list), key=lambda x: x[0])
            days_list = [d for d, _ in zipped]
            hours_list = [h for _, h in zipped]
            patterns.append((days_list, hours_list))

        return patterns  # 如果有 preferred_groups，直接返回匹配模式

#没有 preferred_groups 时，先按 ZXS 生成，再用 preferred_options 把不合规的模式掐掉（天必须在允许集合里，且每天学时不超过该天最长窗口长度）
    # 默认模式生成（ZXS规则）
    if zxs <= 4:
        for day in range(base_days):
            patterns.append(([day], [zxs]))
        if zxs == 3:
            for start_day in range(base_days - 2):
                if start_day + 2 < base_days:
                    patterns.append(([start_day, start_day + 2], [1, 2]))
                    patterns.append(([start_day, start_day + 2], [2, 1]))
                if start_day + 3 < base_days:
                    patterns.append(([start_day, start_day + 3], [1, 2]))
                    patterns.append(([start_day, start_day + 3], [2, 1]))
        if zxs == 4:
            for start_day in range(base_days - 2):
                if start_day + 2 < base_days:
                    patterns.append(([start_day, start_day + 2], [2, 2]))
                if start_day + 3 < base_days:
                    patterns.append(([start_day, start_day + 3], [2, 2]))

    elif zxs == 5:
        for start_day in range(base_days - 2):
            if start_day + 2 < base_days:
                patterns.append(([start_day, start_day + 2], [3, 2]))
                patterns.append(([start_day, start_day + 2], [2, 3]))
            if start_day + 3 < base_days:
                patterns.append(([start_day, start_day + 3], [2, 3]))
                patterns.append(([start_day, start_day + 3], [3, 2]))
    elif zxs == 6:
        for start_day in range(base_days - 4):
            if start_day + 4 < base_days:
                patterns.append(([start_day, start_day + 2, start_day + 4], [2, 2, 2]))
    elif zxs == 7:
        for start_day in range(base_days - 3):
            if start_day + 3 < base_days:
                patterns.append(([start_day, start_day + 3], [4, 3]))
                patterns.append(([start_day, start_day + 3], [3, 4]))
    elif zxs == 8:
        for start_day in range(base_days - 3):
            if start_day + 3 < base_days:
                patterns.append(([start_day, start_day + 3], [4, 4]))

    # 调课模式：添加灵活模式
    if flag_reschedule:
        for day in range(base_days):
            if ([day], [zxs]) not in patterns:
                patterns.append(([day], [zxs]))
        if zxs >= 4:
            if zxs % 2 == 0:
                split = zxs // 2
                for d1 in range(base_days - 1):
                    for d2 in range(d1 + 1, base_days):
                        if ([d1, d2], [split, split]) not in patterns:
                            patterns.append(([d1, d2], [split, split]))
            for split1 in range(1, zxs):
                split2 = zxs - split1
                for d1 in range(base_days - 1):
                    for d2 in range(d1 + 1, base_days):
                        if ([d1, d2], [split1, split2]) not in patterns:
                            patterns.append(([d1, d2], [split1, split2]))
                        if ([d1, d2], [split2, split1]) not in patterns:
                            patterns.append(([d1, d2], [split2, split1]))

# 没有 preferred_groups 时，先按 ZXS 生成，再用 preferred_options 把不合规的模式掐掉（天必须在允许集合里，且每天学时不超过该天最长窗口长度）
# ===== 在这里加入 preferred_options 过滤（上限约束）=====
    if preferred_options:
        allowed_days, day2maxlen = summarize_preferred_options(preferred_options)
        patterns = [
            (days, hours)
            for (days, hours) in patterns
            if all(d in allowed_days for d in days)
               and all(h <= day2maxlen.get(d, 0) for d, h in zip(days, hours))
        ] #pattern 的所有天都必须在允许天集合里。且pattern 的每天学时 h 都必须 ≤ 该天允许的最大连续长度。

    return patterns

#判断用哪个时间表
def has_specific_time_prefs(time_constraints: dict) -> bool:
    """
    True 表示存在“具体时间偏好”，需要 7天×12节 空间；
    False 表示无具体偏好（或全是 'all'），用 5天×8节。
    """
    tc = time_constraints or {}
    pg = tc.get('preferred_groups', []) or []
    po = tc.get('preferred_options', []) or []
    pu = tc.get('unavailable', []) or []

    def _is_all_days(x): return x == 'all'
    def _is_all_periods(x): return x == 'all'

    for group in pg:
        for slot in group:
            days = slot.get('days', 'all')
            periods = slot.get('periods', 'all')
            #只要有任意一个 slot 不是同时 days='all' 且 periods='all'，就说明存在“具体”的限制（比如只限周一，或只限第1-2节），函数立刻返回 True
            if not (_is_all_days(days) and _is_all_periods(periods)):
                return True

    for item in po:
        days = item.get('days', 'all')
        periods = item.get('periods', 'all')
        if not (_is_all_days(days) and _is_all_periods(periods)):
            return True

    for item in pu:
        days = item.get('days', 'all')
        periods = item.get('periods', 'all')
        if not (_is_all_days(days) and _is_all_periods(periods)):
            return True

    return False

##根据前面的判断选择时间表
def choose_calendar_shape(time_constraints):
    """
    返回 (days_of_week, periods_per_day, base_days)
    - 有具体偏好 => 7天×12节
    - 否则 => 5天×8节
    """
    if has_specific_time_prefs(time_constraints):
        days_of_week = 7
        periods_per_day = 11
    else:
        days_of_week = 5
        periods_per_day = 8
    return days_of_week, periods_per_day


def new_check_period_availability(
    day_pattern, hours_per_day, classroom, teacher_week_map, classes, teaching_weeks,
    time_constraints=None,
    reject_stats=None,
):
    """
    不处理 weeks，默认所有周都生效。
    返回 list[ arrangement ]，arrangement: list[(day, period_start, hours)]

    reject_stats: 可选 dict，传入后在枚举候选时段时累计各类「被拒绝」次数（用于诊断，不影响返回值）。
    键：outside_pref_window, unavailable_overlap, classroom_occupied, teacher_occupied,
        class_occupied, segment_boundary
    """
    # 小工具
    # 判断两个闭区间是否有交叠
    def _overlap(a_s, a_e, b_s, b_e):
        return not (a_e < b_s or b_e < a_s)
    # 判断一个闭区间是否被另外一个完全包含
    def _contained(inner_s, inner_e, outer_s, outer_e):
        return outer_s <= inner_s and inner_e <= outer_e

    # 形态
    time_constraints = time_constraints or {'preferred_groups': [], 'preferred_options': [], 'unavailable': []}
    days_of_week, periods_per_day= choose_calendar_shape(time_constraints) #时间表

    def _reject_bump(key: str, delta: int = 1) -> None:
        if reject_stats is not None:
            reject_stats[key] = reject_stats.get(key, 0) + delta

    # 日内分段
    if periods_per_day == 8:    #只有上午、下午，不做特殊要求一般考虑安排在工作日的白天
        day_segments = [(0, 4), (4, 8)]
    else:   #上午、下午、晚上
        day_segments = [(0, 4), (4, 8), (8, 11)]

    preferred_groups = time_constraints.get('preferred_groups', []) or []
    preferred_options = time_constraints.get('preferred_options', []) or []
    unavailable = time_constraints.get('unavailable', []) or []

    # 预处理 unavailable：day -> [(s,e)]
    unavail_by_day = {d: [] for d in range(days_of_week)} #unavail_by_day 是一个字典，键为 0 到 days_of_week - 1，值为空列表，用于存储不可用时间段。
    for item in unavailable:
        days_spec = item.get('days', [])
        days_list = list(range(days_of_week)) if days_spec == 'all' else days_spec
        pspec = item.get('periods', []) #获取不可用的时段
        blocks = [(0, periods_per_day)] if pspec == 'all' else [
            (int(s), int(e)) for (s, e) in (pspec or []) if isinstance(s, int) and isinstance(e, int) and s <= e
        ] #如果 pspec == 'all'，设为全天 [(0, periods_per_day - 1)]。否则，遍历 pspec，生成 (s, e) 列表，仅保留 s, e 为整数且 s <= e 的时间段。
        for d in days_list:
            for (s, e) in blocks:
                unavail_by_day.setdefault(d, []).append((s, e)) #加上这个值

    # 预处理 groups：list[ dict(day -> [(s,e)]) ]，规范化分组时间偏好，用于后续检查安排是否完全匹配某组。
    groups_norm = []
    if preferred_groups:
        for group in preferred_groups:
            gmap = {}
            for slot in group:
                days_spec = slot.get('days', [])
                days_list = list(range(days_of_week)) if days_spec == 'all' else list(days_spec or [])
                pspec = slot.get('periods', [])
                blocks = [(0, periods_per_day)] if pspec == 'all' else [
                    (int(s), int(e)) for (s, e) in (pspec or []) if isinstance(s, int) and isinstance(e, int) and s <= e
                ]#(0, periods_per_day - 1)
                for d in days_list:
                    gmap.setdefault(d, []).extend(blocks)
            groups_norm.append(gmap)

        # 天集合必须匹配某一组（完全一致），否则直接返回空集合
        days_set = set(day_pattern)


        if not any(days_set == set(g.keys()) for g in groups_norm):
            _reject_bump('pattern_group_day_mismatch')
            return []

    # 预处理 options：day -> [(s,e)]（仅当无 groups 时生效）
    options_by_day = None
    if preferred_options and not preferred_groups:
        options_by_day = {d: [] for d in range(days_of_week)} #一个星期几和对应的可用时段
        for item in preferred_options:
            days_spec = item.get('days', [])
            days_list = list(range(days_of_week)) if days_spec == 'all' else days_spec
            pspec = item.get('periods', [])
            blocks = [(0, periods_per_day)] if pspec == 'all' else [
                (int(s), int(e)) for (s, e) in (pspec or []) if isinstance(s, int) and isinstance(e, int) and s <= e
            ]
            for d in days_list:
                options_by_day.setdefault(d, []).extend(blocks)


 # ============ 核心：为每个 day 生成候选的可用起点，然后组合搜索 ============
    def _gen_day_candidates_for_group(day, hours, gmap_or_none):
        """给定 day / hours，生成该天所有可行 (start,end) 起点，若 gmap_or_none 不为 None，则必须落在该组窗口内。"""
        cands = []
        wins = None  #从这里到情况A结束都是新生成的
        if gmap_or_none is not None:
            wins = gmap_or_none.get(day, [])
            # ---------- 情况 A：有 groups（允许跨段） ----------
        if wins:
            # 在全天范围滑动（0..periods_per_day-hours），步长仍按你原先习惯用 2，允许跨段，但是不允许从第偶数节课开始
            for start in range(0, max(0, periods_per_day - hours + 1), 2):
                end = start + hours - 1
                # 必须完全被某个窗口包含
                if not any(_contained(start, end, s, e) for (s, e) in wins):
                    _reject_bump('outside_pref_window')
                    continue
                # unavailable 冲突
                if any(_overlap(start, end, s, e) for (s, e) in unavail_by_day.get(day, [])):
                    _reject_bump('unavailable_overlap')
                    continue
                # 资源：教室
                if not all(
                        all(classroom.timetable[w, day, p] == '' for p in range(start, end+1))
                        for w in teaching_weeks
                ):
                    _reject_bump('classroom_occupied')
                    continue
                # 资源：教师（各自责任周）
                ok_teachers = True
                for teacher_id, (teacher, weeks) in teacher_week_map.items():
                    rel_weeks = set(weeks).intersection(teaching_weeks)
                    if not all(
                            all(teacher.timetable[w, day, p]['course'] == '' for p in range(start, end+1))
                            for w in rel_weeks
                    ):
                        ok_teachers = False
                        break
                if not ok_teachers:
                    _reject_bump('teacher_occupied')
                    continue
                # 资源：班级
                ok_classes = True
                for class_obj in classes:
                    if not all(
                            all(class_obj.timetable[w, day, p]['course'] == '' for p in range(start, end+1))
                            for w in teaching_weeks
                    ):
                        ok_classes = False
                        break
                if not ok_classes:
                    _reject_bump('class_occupied')
                    continue

                cands.append((start, end))
            return cands
        if options_by_day is not None: #可选可以跨段
            # 在全天范围滑动（0..periods_per_day-hours），步长仍按你原先习惯用 2，允许跨段，但是不允许从第偶数节课开始
            for start in range(0, max(0, periods_per_day - hours + 1), 2):
                end = start + hours - 1
                # 必须完全被某个窗口包含
                wins = options_by_day.get(day, [])
                if not wins or not any(_contained(start, end, s, e) for (s, e) in wins):
                    _reject_bump('outside_pref_window')
                    continue
                # unavailable 冲突
                if any(_overlap(start, end, s, e) for (s, e) in unavail_by_day.get(day, [])):
                    _reject_bump('unavailable_overlap')
                    continue
                # 资源：教室
                if not all(
                        all(classroom.timetable[w, day, p] == '' for p in range(start, end+1))
                        for w in teaching_weeks
                ):
                    _reject_bump('classroom_occupied')
                    continue
                # 资源：教师（各自责任周）
                ok_teachers = True
                for teacher_id, (teacher, weeks) in teacher_week_map.items():
                    rel_weeks = set(weeks).intersection(teaching_weeks)
                    if not all(
                            all(teacher.timetable[w, day, p]['course'] == '' for p in range(start, end+1))
                            for w in rel_weeks
                    ):
                        ok_teachers = False
                        break
                if not ok_teachers:
                    _reject_bump('teacher_occupied')
                    continue
                # 资源：班级
                ok_classes = True
                for class_obj in classes:
                    if not all(
                            all(class_obj.timetable[w, day, p]['course'] == '' for p in range(start, end+1))
                            for w in teaching_weeks
                    ):
                        ok_classes = False
                        break
                if not ok_classes:
                    _reject_bump('class_occupied')
                    continue

                cands.append((start, end))
            return cands

        for (seg_start, seg_end) in day_segments:
            for start in range(seg_start, seg_end, 2):  #从时段0、2、4、6、8开始,时段不能从奇数开始#分段
                end = start + hours - 1
                if end >= seg_end:  #没有偏好时间，不能跨段
                    _reject_bump('segment_boundary')
                    continue
                # unavailable 冲突
                if any(_overlap(start, end, s, e) for (s, e) in unavail_by_day.get(day, [])):
                    _reject_bump('unavailable_overlap')
                    continue

                # if options_by_day is not None: #可选不能跨段
                #     wins = options_by_day.get(day, [])
                #     if not wins or not any(_contained(start, end, s, e) for (s, e) in wins):
                #         continue
                # 资源：教室
                if not all(
                    #all(classroom.timetable[w, day, p] == '' for p in range(start, end + 1))
                    all(classroom.timetable[w, day, p] == '' for p in range(start, end+1))
                    for w in teaching_weeks
                ):
                    _reject_bump('classroom_occupied')
                    continue
                # 资源：教师（各自责任周）
                ok_teachers = True
                for teacher_id, (teacher, weeks) in teacher_week_map.items():
                    rel_weeks = set(weeks).intersection(teaching_weeks)
                    if not all(
                        all(teacher.timetable[w, day, p]['course'] == '' for p in range(start, end+1))
                        for w in rel_weeks
                    ):
                        ok_teachers = False
                        break
                if not ok_teachers:
                    _reject_bump('teacher_occupied')
                    continue
                # 资源：班级
                ok_classes = True
                for class_obj in classes:
                    if not all(
                        all(class_obj.timetable[w, day, p]['course'] == '' for p in range(start, end+1))
                        for w in teaching_weeks
                    ):
                        ok_classes = False
                        break
                if not ok_classes:
                    _reject_bump('class_occupied')
                    continue

                cands.append((start, end))
        return cands

    solutions = []

####使用了深度优先搜索DFS来生成课程安排的组合####
#生成 day_pattern 中每一天的课程安排组合，每一天从 cands_per_day[day] 中选择一个时间段 (start, end)，构建完整的安排 partial
    def _search_combine(cands_per_day, idx, partial):
        """DFS 组合：cands_per_day: Dict[day] -> List[(start,end)]；partial: List[(day,start,hours)]"""
        #cands_per_day：字典，键是日期（day），值是该天的候选时间段列表 [(start, end)]
        #idx：当前处理的 day_pattern 索引，表示正在为 day_pattern[idx] 选择时间段。partial：当前部分安排，格式为 List[(day, start, hours)]，存储已选择的安排
        if idx == len(day_pattern):
            solutions.append(list(partial))
            return                  #处理完所有天就返回，终止条件
        day = day_pattern[idx]
        hours = int(hours_per_day[idx])
        for (st, ed) in cands_per_day[day]:
            partial.append((day, st, hours))
            _search_combine(cands_per_day, idx + 1, partial) #递归
            partial.pop()   # # 默认删除最后一个元素，并返回它



    if groups_norm:
        # 对每个 group 单独构造候选，然后组合，最后并集
        for gmap in groups_norm:
            # 天集合必须完全相等（再次防御）
            if set(day_pattern) != set(gmap.keys()):
                continue
            cands_per_day = {}
            feasible = True
            for i, day in enumerate(day_pattern):
                hours = int(hours_per_day[i])
                cands = _gen_day_candidates_for_group(day, hours, gmap_or_none=gmap)     #生成满足条件的候选起点
                if not cands:
                    feasible = False
                    break
                cands_per_day[day] = cands
            if not feasible:
                continue
            _search_combine(cands_per_day, 0, []) #做笛卡尔积
        return solutions

    else:
        # 没有 groups，使用 options（或无 options 时仅约束 unavailable/资源）
        cands_per_day = {}
        for i, day in enumerate(day_pattern):
            hours = int(hours_per_day[i])
            cands = _gen_day_candidates_for_group(day, hours, gmap_or_none=None)    #生成满足条件的候选起点
            if not cands:
                return []  # 某天无候选，整体无解
            cands_per_day[day] = cands
        _search_combine(cands_per_day, 0, [])   #做笛卡尔积
        return solutions




def new_only_time(
    day_pattern, hours_per_day, time_constraints=None
):
    """
    不处理 weeks，默认所有周都生效。
    返回 list[ arrangement ]，arrangement: list[(day, period_start, hours)]
    """
    # 小工具
    # 判断两个闭区间是否有交叠
    def _overlap(a_s, a_e, b_s, b_e):
        return not (a_e < b_s or b_e < a_s)
    # 判断一个闭区间是否被另外一个完全包含
    def _contained(inner_s, inner_e, outer_s, outer_e):
        return outer_s <= inner_s and inner_e <= outer_e

    # 形态
    time_constraints = time_constraints or {'preferred_groups': [], 'preferred_options': [], 'unavailable': []}
    days_of_week, periods_per_day= choose_calendar_shape(time_constraints) #时间表

    # 日内分段
    if periods_per_day == 8:    #只有上午、下午，不做特殊要求一般考虑安排在工作日的白天
        day_segments = [(0, 4), (4, 8)]
    else:   #上午、下午、晚上
        day_segments = [(0, 4), (4, 8), (8, 11)]

    preferred_groups = time_constraints.get('preferred_groups', []) or []
    preferred_options = time_constraints.get('preferred_options', []) or []
    unavailable = time_constraints.get('unavailable', []) or []

    # 预处理 unavailable：day -> [(s,e)]
    unavail_by_day = {d: [] for d in range(days_of_week)} #unavail_by_day 是一个字典，键为 0 到 days_of_week - 1，值为空列表，用于存储不可用时间段。
    for item in unavailable:
        days_spec = item.get('days', [])
        days_list = list(range(days_of_week)) if days_spec == 'all' else days_spec
        pspec = item.get('periods', []) #获取不可用的时段
        blocks = [(0, periods_per_day)] if pspec == 'all' else [
            (int(s), int(e)) for (s, e) in (pspec or []) if isinstance(s, int) and isinstance(e, int) and s <= e
        ] #如果 pspec == 'all'，设为全天 [(0, periods_per_day - 1)]。否则，遍历 pspec，生成 (s, e) 列表，仅保留 s, e 为整数且 s <= e 的时间段。
        for d in days_list:
            for (s, e) in blocks:
                unavail_by_day.setdefault(d, []).append((s, e)) #加上这个值

    # 预处理 groups：list[ dict(day -> [(s,e)]) ]，规范化分组时间偏好，用于后续检查安排是否完全匹配某组。
    groups_norm = []
    if preferred_groups:
        for group in preferred_groups:
            gmap = {}
            for slot in group:
                days_spec = slot.get('days', [])
                days_list = list(range(days_of_week)) if days_spec == 'all' else list(days_spec or [])
                pspec = slot.get('periods', [])
                blocks = [(0, periods_per_day)] if pspec == 'all' else [
                    (int(s), int(e)) for (s, e) in (pspec or []) if isinstance(s, int) and isinstance(e, int) and s <= e
                ]#(0, periods_per_day - 1)
                for d in days_list:
                    gmap.setdefault(d, []).extend(blocks)
            groups_norm.append(gmap)

        # 天集合必须匹配某一组（完全一致），否则直接返回空集合
        days_set = set(day_pattern)
        if not any(days_set == set(g.keys()) for g in groups_norm):
            return []

    # 预处理 options：day -> [(s,e)]（仅当无 groups 时生效）
    options_by_day = None
    if preferred_options and not preferred_groups:
        options_by_day = {d: [] for d in range(days_of_week)} #一个星期几和对应的可用时段
        for item in preferred_options:
            days_spec = item.get('days', [])
            days_list = list(range(days_of_week)) if days_spec == 'all' else days_spec
            pspec = item.get('periods', [])
            blocks = [(0, periods_per_day)] if pspec == 'all' else [
                (int(s), int(e)) for (s, e) in (pspec or []) if isinstance(s, int) and isinstance(e, int) and s <= e
            ]
            for d in days_list:
                options_by_day.setdefault(d, []).extend(blocks)


 # ============ 核心：为每个 day 生成候选的可用起点，然后组合搜索 ============
    def _gen_day_candidates_for_group(day, hours, gmap_or_none):
        """给定 day / hours，生成该天所有可行 (start,end) 起点，若 gmap_or_none 不为 None，则必须落在该组窗口内。"""
        cands = []
        wins = None  #从这里到情况A结束都是新生成的
        if gmap_or_none is not None:
            wins = gmap_or_none.get(day, [])
            # ---------- 情况 A：有 groups（允许跨段） ----------
        if wins:
            # 在全天范围滑动（0..periods_per_day-hours），步长仍按你原先习惯用 2，允许跨段，但是不允许从第偶数节课开始
            for start in range(0, max(0, periods_per_day - hours + 1), 2):
                end = start + hours - 1
                # 必须完全被某个窗口包含
                if not any(_contained(start, end, s, e) for (s, e) in wins):
                    continue
                # unavailable 冲突
                if any(_overlap(start, end, s, e) for (s, e) in unavail_by_day.get(day, [])):
                    continue

                cands.append((start, end))
            return cands
        if options_by_day is not None: #可选可以跨段
            # 在全天范围滑动（0..periods_per_day-hours），步长仍按你原先习惯用 2，允许跨段，但是不允许从第偶数节课开始
            for start in range(0, max(0, periods_per_day - hours + 1), 2):
                end = start + hours - 1
                # 必须完全被某个窗口包含
                wins = options_by_day.get(day, [])
                if not wins or not any(_contained(start, end, s, e) for (s, e) in wins):
                    continue
                # unavailable 冲突
                if any(_overlap(start, end, s, e) for (s, e) in unavail_by_day.get(day, [])):
                    continue

                cands.append((start, end))
            return cands

        for (seg_start, seg_end) in day_segments:
            for start in range(seg_start, seg_end, 2):  #从时段0、2、4、6、8开始,时段不能从奇数开始#分段
                end = start + hours - 1
                if end >= seg_end:  #没有偏好时间，不能跨段
                    continue
                # unavailable 冲突
                if any(_overlap(start, end, s, e) for (s, e) in unavail_by_day.get(day, [])):
                    continue

                # if options_by_day is not None: #可选不能跨段
                #     wins = options_by_day.get(day, [])
                #     if not wins or not any(_contained(start, end, s, e) for (s, e) in wins):
                #         continue
               
                cands.append((start, end))
        return cands

    solutions = []
####使用了深度优先搜索DFS来生成课程安排的组合####
#生成 day_pattern 中每一天的课程安排组合，每一天从 cands_per_day[day] 中选择一个时间段 (start, end)，构建完整的安排 partial
    def _search_combine(cands_per_day, idx, partial):
        """DFS 组合：cands_per_day: Dict[day] -> List[(start,end)]；partial: List[(day,start,hours)]"""
        #cands_per_day：字典，键是日期（day），值是该天的候选时间段列表 [(start, end)]
        #idx：当前处理的 day_pattern 索引，表示正在为 day_pattern[idx] 选择时间段。partial：当前部分安排，格式为 List[(day, start, hours)]，存储已选择的安排
        if idx == len(day_pattern):
            solutions.append(list(partial))
            return                  #处理完所有天就返回，终止条件
        day = day_pattern[idx]
        hours = int(hours_per_day[idx])
        for (st, ed) in cands_per_day[day]:
            partial.append((day, st, hours))
            _search_combine(cands_per_day, idx + 1, partial) #递归
            partial.pop()   # # 默认删除最后一个元素，并返回它


    if groups_norm:
        # 对每个 group 单独构造候选，然后组合，最后并集
        for gmap in groups_norm:
            # 天集合必须完全相等（再次防御）
            if set(day_pattern) != set(gmap.keys()):
                continue
            cands_per_day = {}
            feasible = True
            for i, day in enumerate(day_pattern):
                hours = int(hours_per_day[i])
                cands = _gen_day_candidates_for_group(day, hours, gmap_or_none=gmap)     #生成满足条件的候选起点
                if not cands:
                    feasible = False
                    break
                cands_per_day[day] = cands
            if not feasible:
                continue
            _search_combine(cands_per_day, 0, []) #做笛卡尔积
        return solutions

    else:
        # 没有 groups，使用 options（或无 options 时仅约束 unavailable/资源）
        cands_per_day = {}
        for i, day in enumerate(day_pattern):
            hours = int(hours_per_day[i])
            cands = _gen_day_candidates_for_group(day, hours, gmap_or_none=None)    #生成满足条件的候选起点
            if not cands:
                return []  # 某天无候选，整体无解
            cands_per_day[day] = cands
        _search_combine(cands_per_day, 0, [])   #做笛卡尔积
        return solutions
