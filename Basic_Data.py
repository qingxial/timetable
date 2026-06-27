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
    SKZCDM: str          # 上课周次代码
    ZXS: int             # 周学时
    # 教师相关
    JSH: str             # 教师编号
    XM: str              # 教师姓名
    RWJSZCDM: str        # 教师周次代码

    # 排课需求
    SFXYPK: int          # 是否需要排课
    SFXYJAS: int         # 是否需要教室
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

    # 获取英文表头（第二行）
    headers = [cell.value for cell in ws[2]]
    
    # 获取Course类接受的参数
    import inspect
    course_params = inspect.signature(Course.__init__).parameters.keys()
    course_params = [p for p in course_params if p != 'self']
    
    # 数值字段：openpyxl 读出的可能是 str/int/float 混杂，统一转为 float（避免下游算术报错）
    NUMERIC_FIELDS = {"ZXS", "KRL"}

    courses = []
    for row_num in range(3, ws.max_row + 1):
        # 只收集Course类支持的参数
        course_data = {}
        for col_num, header in enumerate(headers):
            if header in course_params:
                raw = ws.cell(row=row_num, column=col_num+1).value
                if header in NUMERIC_FIELDS and raw not in (None, ''):
                    try:
                        raw = float(raw)
                    except (TypeError, ValueError):
                        pass  # 保留原值，由下游容错处理
                course_data[header] = raw

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