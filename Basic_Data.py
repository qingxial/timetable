"""
基础数据模型与加载模块（项目最底层，被几乎所有模块依赖）。

定义 Course / Classroom / Teacher / Class 四个数据类，
以及对应的 load_courses / load_classrooms / load_teachers / load_classes
函数，从「智能排课基础数据」目录下的 Excel 表读取数据。
"""
from dataclasses import dataclass, field
from typing import Optional
from openpyxl import load_workbook
from typing import List
import numpy as np
import pandas as pd

#
# def normalize_empty(v):
#     # 把 Excel 中可能出现的 “伪空值” 统一转成 None
#     if v in (None, "", " "):
#         return None
#     return v


@dataclass
class Course:
    """课程数据模型（含时间表）"""
    # 基础字段
    KCH: str             # 课程号
    KCM: str             # 课程名
    YXMC: str            # 开课单位名称
    KCLB: str            # 课程类别
    SKXQ: str            # 上课校区
    JXBID: str           # 教学班ID
    KRL: int             # 课容量
    LLXS: float          # 理论学时（=0 表示无理论课时，不参与排课，Issue #12）
    SKZCDM: str          # 上课周次代码
    ZXS: int             # 周学时
    # 教师相关
    JSH: str             # 教师编号
    XM: str              # 教师姓名
    RWJSZCDM: str        # 教师周次代码

    # 排课需求
    SFXYPK: int          # 是否需要排课
    SFXYJAS: int         # 是否需要教室
    IF_ROOM_CONFICT: int   # 教室是否允许冲突：1=允许多班共占同一教室(不检查、不写占用)，0/缺省=正常检查(Issue #13)
    IF_CLASS_CONFICT: int  # 班级是否允许冲突：1=该课不参与班级冲突检查(不检查、不写占用)，0/缺省=正常检查(Issue #13)
    JASLXDM: str         # 教室类型代码
    JASLXMC: str         # 教室类型名称
    JXLDM: str           # 教学楼代码
    JASDM: str           # 教室代码
    Prefer_Time: str     #偏好时间
    unavailable_Time: str #禁止时间
    LSJASDM: str         # 历年教室代码
    TJBJ: str            # 推荐班级
    # 时间表维度
    weeks: int           # 周次维度
    days: int            # 日期维度
    periods: int         # 时段维度

    # 当前是否已成功占用时间表（用于调课时只在「已排上」的课程中选匹配对象）
    IF_scheduled: bool = False

    # 虚班拆分（Issue #14）：奇数 ZXS 拆 @W/@B 两条记录，paired_jxbid 指向另一条
    paired_jxbid: Optional[str] = None

    timetable: np.ndarray = field(init=False)
    # 已排上的星期集合（用于 LCV "避同天" 软约束查询，O(1) 命中）
    scheduled_days: set = field(init=False)

    def __post_init__(self):
        # 初始化全空字符串的三维数组
        self.timetable = np.full(
            shape=(self.weeks, self.days, self.periods),
            fill_value="",  # 默认空字符串表示未安排
            dtype='U20'     # 支持最多20个Unicode字符
        )
        self.scheduled_days = set()


def load_courses(
    excel_path: str,
    weeks: int,
    days: int,
    periods: int
) -> List[Course]:
    """直接映射Excel字段到Course实例"""
    wb = load_workbook(excel_path)
    ws = wb.active

    # 双行表头：第一行中文名、第二行英文字段名
    headers_cn = [cell.value for cell in ws[1]]
    headers = [cell.value for cell in ws[2]]

    # 获取Course类接受的参数
    import inspect
    course_params = inspect.signature(Course.__init__).parameters.keys()
    course_params = [p for p in course_params if p != 'self']

    # 部分新增列（理论学时 / 是否检查教室冲突 / 是否检查班级冲突）当前只有中文表头，
    # 第二行英文字段名为空，这里按中文表头前缀回退识别，保证仍能加载（Issue #12 / #13）。
    def _resolve_param(en, cn):
        if en in course_params:
            return en
        s = str(cn).strip() if cn is not None else ''
        if s.startswith('理论学时'):
            return 'LLXS'
        if s.startswith('是否检查教室冲突'):
            return 'IF_ROOM_CONFICT'
        if s.startswith('是否检查班级冲突'):
            return 'IF_CLASS_CONFICT'
        return None

    col_params = [_resolve_param(en, cn) for en, cn in zip(headers, headers_cn)]

    # 数值字段：openpyxl 读出的可能是 str/int/float 混杂，统一转为 float（避免下游算术报错）
    NUMERIC_FIELDS = {"ZXS", "KRL"}

    courses = []
    for row_num in range(3, ws.max_row + 1):
        # 只收集Course类支持的参数（按列解析后的字段名映射）
        course_data = {}
        for col_num, param in enumerate(col_params):
            if param is not None and param in course_params:
                raw = ws.cell(row=row_num, column=col_num+1).value
                if param in NUMERIC_FIELDS and raw not in (None, ''):
                    try:
                        raw = float(raw)
                    except (TypeError, ValueError):
                        pass  # 保留原值，由下游容错处理
                course_data[param] = raw

        # 新增开关字段在「缺列 / 空表头」时的默认值：
        #   LLXS=None                          → 不因理论学时过滤（保持既有行为）
        #   IF_ROOM_CONFICT / IF_CLASS_CONFICT = 0 → 默认正常检查冲突（向后兼容）
        course_data.setdefault('LLXS', None)
        course_data.setdefault('IF_ROOM_CONFICT', 0)
        course_data.setdefault('IF_CLASS_CONFICT', 0)

        # 添加固定参数
        course_data.update(weeks=weeks, days=days, periods=periods)
        courses.append(Course(**course_data))

    # 虚班拆分（Issue #14）：填充 paired_jxbid
    # JXBID 形如 "原ID@W" / "原ID@B" 的，把同 base 的两条互相挂指针
    _by_base = {}
    for c in courses:
        jx = str(c.JXBID or "")
        if "@" in jx:
            base, suffix = jx.rsplit("@", 1)
            if suffix in ("W", "B"):
                _by_base.setdefault(base, []).append(c)
    for base, group in _by_base.items():
        if len(group) == 2:
            group[0].paired_jxbid = group[1].JXBID
            group[1].paired_jxbid = group[0].JXBID

    return courses


# ---------------------------------------------------------------------------
# 排课开关辅助函数（Issue #12 理论学时 / Issue #13 教室·班级冲突检查开关）
# 这些字段在不同数据源里可能是 int 1/0 或 str '1'/'0'，统一在此判定，避免散落比较。
# 语义（按数据所有者口径，不看中文表头字面）：
#   IF_ROOM_CONFICT  == 1 → 允许教室冲突（体育馆多班共占等），不检查、不写教室占用
#   IF_ROOM_CONFICT  == 0 / 缺省 → 不允许冲突，按正常排课检查并写入
#   IF_CLASS_CONFICT 同上
# ---------------------------------------------------------------------------
def _conflict_allowed(v) -> bool:
    """显式 '1' / 1 / 1.0 才表示「允许冲突」（即跳过检查）；其余一律按需检查。"""
    if v is None:
        return False
    s = str(v).strip()
    return s in ('1', '1.0')


def check_room_conflict(course) -> bool:
    """该课程是否需要检查教室冲突。IF_ROOM_CONFICT==1 → 允许冲突，不检查；其它一律检查。"""
    return not _conflict_allowed(getattr(course, 'IF_ROOM_CONFICT', 0))


def check_class_conflict(course) -> bool:
    """该课程是否需要检查班级冲突。IF_CLASS_CONFICT==1 → 允许冲突，不检查；其它一律检查。"""
    return not _conflict_allowed(getattr(course, 'IF_CLASS_CONFICT', 0))


def llxs_schedulable(course) -> bool:
    """理论学时是否需要排课（Issue #12）。
    仅当 LLXS 是 > 0 的数值才参与排课；==0 / 空 / None / 非数值 一律过滤。"""
    v = getattr(course, 'LLXS', None)
    if v is None:
        return False
    s = str(v).strip()
    if s in ('', 'None', 'nan'):
        return False
    try:
        return float(s) > 0
    except (TypeError, ValueError):
        return False


@dataclass
class Classroom:
    """教室资源数据模型（含时间表）"""
    JASDM: str         # 教室代码
    JASMC: str         # 教室名称
    JASLX: str         # 教室类型
    JXLDM: str         # 教学楼代码
    JXLMC: str         # 教学楼名称
    LC: int            # 教室楼层
    XXXQDM: str        # 校区代码
    MC: str            # 校区名称
    SKZWS: int         # 上课座位数
    KSZWS: int         # 考试座位数
    SFYXPK: str        # 是否允许排课
    SFYXKS: str       # 是否允许考试
    SFYXJY: str        # 是否允许借用
    weeks: int         # 时间表维度参数
    days: int 
    periods: int
    timetable: np.ndarray = field(init=False)  # 类型修改

    def __post_init__(self):
        # 初始化全空字符串的三维数组
        self.timetable = np.full(
            shape=(self.weeks, self.days, self.periods),
            fill_value="",  # 默认空字符串表示未安排
            dtype='U30'     # 支持最多20个Unicode字符
        )


def load_classrooms(excel_path: str,
                   weeks: int,
                   days: int,
                   periods: int) -> List[Classroom]:
    """读取教室数据并注入时间表维度"""
    wb = load_workbook(excel_path)
    ws = wb.active

    # 获取第二行英文表头
    headers = [cell.value for cell in ws[2]]
    
    return [
        Classroom(
            **{
                header: ws.cell(row=row_num, column=col_num+1).value
                for col_num, header in enumerate(headers)
            },
            weeks=weeks,
            days=days,
            periods=periods
        )
        for row_num in range(3, ws.max_row + 1)
    ]



# 定义时间表元素的数据类型
TIMETABLE_DTYPE = np.dtype([
    ('course', 'U20'),   # 课程名称（最多20字符）
    ('classroom', 'U10') # 教室地点（最多10字符）
])

@dataclass
class Teacher:
    JSH: str  # 教师编号
    XM: str   # 教师姓名
    weeks: int
    days: int
    periods: int
    
    timetable: np.ndarray = field(init=False)

    def __post_init__(self):
        # 第一步：创建正确形状的空数组
        self.timetable = np.zeros(
            (self.weeks, self.days, self.periods),
            dtype=TIMETABLE_DTYPE
        )
        
        # 第二步：显式填充空值
        self.timetable['course'] = ''
        self.timetable['classroom'] = ''


def load_teachers(
    excel_path: str,
    weeks: int,
    days: int,
    periods: int) -> List[Teacher]:
    """从整理后的教师信息表读取教师基本信息，时间表保持空值"""
    df = pd.read_excel(excel_path)
    
    return [
        Teacher(
            JSH=row['JSH'],
            XM=row['XM'],
            weeks=weeks,
            days=days,
            periods=periods
        )
        for _, row in df.iterrows()
    ]



@dataclass
class Class:
    # BJDM: str  # 班级代码
    BJMC: str  # 班级名称
    # NJDM: str  # 年级代码(可选)
    # ZYMC: str  # 专业名称(可选)
    # YXMC: str  # 院系名称(可选)
    weeks: int
    days: int
    periods: int
    
    timetable: np.ndarray = field(init=False)

    def __post_init__(self):
        # 第一步：创建正确形状的空数组
        self.timetable = np.zeros(
            (self.weeks, self.days, self.periods),
            dtype=TIMETABLE_DTYPE
        )
        
        # 第二步：显式填充空值
        self.timetable['course'] = ''
        self.timetable['classroom'] = ''


def load_classes(
    excel_path: str,
    weeks: int,
    days: int,
    periods: int) -> List[Class]:
    """从班级信息表读取班级基本信息，时间表保持空值"""
    df = pd.read_excel(excel_path)
    
    return [
        Class(
            # BJDM=row['BJDM'],
            BJMC=row['BJMC'],
            # NJDM=row.get('NJDM', ''),  # 使用get方法处理可能不存在的列
            # ZYMC=row.get('ZYMC', ''),
            # YXMC=row.get('YXMC', ''),
            weeks=weeks,
            days=days,
            periods=periods
        )
        for _, row in df.iterrows()
    ]