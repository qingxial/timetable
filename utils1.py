
import pandas as pd
import os
from typing import Union, List, Dict
from tryloadlimit import *


def _has_non_empty(value) -> bool:
    """判断一个字段是否“真正有值”（排除 None、空串、NaN 等），用于教室筛选中的可选条件。"""
    if value is None:
        return False
    # pandas 的 isna 能同时识别 NaN / NaT 等
    try:
        if pd.isna(value):
            return False
    except Exception:
        pass
    if isinstance(value, str) and value.strip() == "":
        return False
    return True

def pre_selection(courses, classrooms, course_type=None, department=None, campus=None):
    """初步筛选教学班和教室
    
    Args:
        courses: 所有课程对象列表
        classrooms: 所有教室对象列表
        course_type: 可选，指定课程类别筛选条件
        department: 可选，指定开课单位筛选条件
        campus: 可选，指定上课校区筛选条件
    
    Returns:
        tuple: (教学班索引字典, 教室索引字典, 有效教室列表)
    """
    # 筛选有效教学班的JXBID（既需要排课又需要安排教室），只排了周学时在1-8节之间的课程
    valid_jxbids = [
        jxb.JXBID 
        for jxb in courses 
        if jxb.SFXYPK == '1' and jxb.SFXYJAS == '1' and jxb.RWJSZCDM != None and 
            jxb.KCLB != None and jxb.SKXQ != None and jxb.SKZCDM != None and jxb.SKZCDM != '0000000000000000' and jxb.ZXS != None and 0 < jxb.ZXS <= 8 and
            (course_type is None or jxb.KCLB == course_type) and  # 根据课程类别筛选
            (department is None or jxb.YXMC == department) and    # 根据开课单位筛选
            (campus is None or jxb.SKXQ == campus)                # 根据上课校区筛选
    ]  # 列表保留顺序

    # 筛选有效教室的代码（允许排课）,有些教室没有排课是因为本身不允许排课
    valid_jasdms = [
        classroom.JASDM 
        for classroom in classrooms 
        if classroom.SFYXPK == '1'
    ] # 列表保留顺序

    # 筛选有效教室的对象（允许排课）
    valid_classrooms = [
        classroom 
        for classroom in classrooms 
        if classroom.SFYXPK == '1'
    ] # 列表保留顺序

    # 去重
    unique_jxbids = list(dict.fromkeys(valid_jxbids)) 
    unique_jasdms = list(dict.fromkeys(valid_jasdms))

    jxbid_index = {jxbid: i for i, jxbid in enumerate(unique_jxbids)}
    jasdm_index = {jasdm: j for j, jasdm in enumerate(unique_jasdms)}

    return jxbid_index, jasdm_index, valid_classrooms


# 查看实例的各项属性的数据类型，data为实例列表
# 示例：inspect_first_element(courses, "课程数据")
def inspect_first_element(data, data_name="数据名称"):
    """检查数据集第一个元素的所有属性类型
        参数:
        data: 对象列表
        data_name: 用于打印时表明数据名称
    """
    print(f"\n=== 检查 {data_name.upper()} 第一个元素 ===")
    
    if not data:
        print(f"⚠️ 警告：{data_name} 为空")
        return
    
    first_item = data  [0] if isinstance(data, (list, tuple)) else next(iter(data.values())) if isinstance(data, dict) else data.iloc  [0]
    
    print(f"元素类型: {type(first_item)}")
    
    # 获取属性字典
    if hasattr(first_item, '__dict__'):  # 处理自定义类实例
        attrs = vars(first_item)
    elif isinstance(first_item, dict):   # 处理字典类型
        attrs = first_item
    else:                                # 基本数据类型
        print(f"值: {first_item} (类型: {type(first_item).__name__})")
        return
    
    # 打印属性类型
    print("属性类型明细:")
    for attr, value in attrs.items():
        print(f"  - {attr}: {type(value).__name__}")


# 打包具有给定JXBID的所有对象
def get_consecutive_jxbid_old(target_jxbid: str, courses: List[object]) -> List[object]:
    """直接提取连续出现的目标 JXBID 实例列表"""
    group_of_jxb = []
    start_index = -1

    # 定位第一个匹配项的位置
    for i, course in enumerate(courses):
        if course.JXBID == target_jxbid:
            start_index = i
            break

    # 从起始点开始连续截取相同 JXBID 的实例
    for course in courses[start_index:]:
        if course.JXBID != target_jxbid:
            break
        group_of_jxb.append(course)

    return group_of_jxb



def get_consecutive_jxbid(target_jxbid: str, courses: List[object]) -> List[object]:
    """按 JXBID 聚合：返回该教学班在课程表中的全部行对象，顺序与加载后的列表行序一致，不要求行相邻。"""
    return [c for c in courses if c.JXBID == target_jxbid]


# 解析上课周次代码，返回上课周次列表
def trans_week_flags(flag_str: str) -> list[int]:
    if flag_str is not None:
        return [i for i, flag in enumerate(flag_str) if flag == '1']
    else:
        return None

#获取一组教学班的上课周次并集
def get_teaching_weeks(jxb_list: list) -> list[int]:
    """获取一组教学班的上课周次并集
    对一组教学班的SKZCDM属性进行解析, 获取所有教学班需要安排的周次并集
    Args:
        jxb_list: 教学班对象列表
    Returns:
        所有教学班需要上课的周次并集（去重并排序的列表）
    """
    all_weeks = []
    for jxb in jxb_list:
        if hasattr(jxb, 'SKZCDM') and jxb.SKZCDM is not None:
            weeks = trans_week_flags(jxb.SKZCDM)
            if weeks:  # 确保非None值且非空列表
                all_weeks.extend(weeks)
    
    # 去重并排序
    return sorted(list(set(all_weeks)))



def get_indices(
    target: Union[str, List[str]],
    index_dict: Dict[str, int]
) -> Union[int, List[int]]:
    """获取一个或多个JXBID在矩阵中的序号(支持单值或列表输入)"""
    if isinstance(target, str):
        return index_dict.get(target, -1)
    else:
        return [index_dict.get(jxbid, -1) for jxbid in target]

def get_teacher_instances(jsh_list: list[str], teachers: list) -> list:
    """根据教师编号列表，从教师总表中提取对应实例
    
    Args:
        jsh_list: 需要查询的教师编号列表
        teachers: 所有教师实例的列表
        
    Returns:
        按输入顺序排列的教师实例列表，未找到的项自动过滤
    """
    # 创建教师编号到实例的映射字典（O(n)时间复杂度）
    teacher_map = {teacher.JSH: teacher for teacher in teachers}
    
    # 遍历查询列表，保留有效实例（保持原顺序）
    return [teacher_map[jsh] for jsh in jsh_list if jsh in teacher_map]


def process_teacher_data():
    # 基础数据路径
    root_path = '智能排课基础数据'
    course_excel = os.path.join(root_path, '智能排课任务教师资源数据.xlsx')

    # 读取Excel文件，跳过第一行（因为第一行是中文说明）
    df = pd.read_excel(course_excel, skiprows=1)

    # 打印所有列名
    print("Excel文件中的列名：")
    print(df.columns.tolist())

    # 提取教师号和教师名称列
    teachers_data = pd.concat([
        df[['JSH', 'XM']],  # 使用实际的列名
    ])

    # 去重
    unique_teachers = teachers_data.drop_duplicates()

    # 按教师号排序
    unique_teachers = unique_teachers.sort_values('JSH')

    # 保存到新的Excel文件
    output_file = os.path.join(root_path, '教师信息汇总.xlsx')
    unique_teachers.to_excel(output_file, index=False)

    print(f"\n总教师数: {len(unique_teachers)}")
    print(f"已保存到：{output_file}")

def filter_suitable_classrooms(classroom_list, jxb, relax_constraints=False):
    """筛选符合教学班要求的教室
    
    根据座位数和校区要求筛选适合的教室
    
    Args:
        classroom_list: 待筛选的教室对象列表
        jxb: 教学班对象
        
    Returns:
        list: 符合筛选条件的教室对象列表
    """
    # 当放宽条件标志为True时，忽略教室指定和历史教室，直接使用基本条件
    if relax_constraints:
        # 基本筛选条件：座位数和校区
        suitable_classrooms = [
            classroom for classroom in classroom_list 
            if classroom.SKZWS >= jxb.KRL and classroom.MC == jxb.SKXQ
        ]


        
        # 如果教学班“真正”指定了教室类型，才应用此条件；为空/NaN 时不做类型过滤
        if _has_non_empty(getattr(jxb, "JASLXMC", None)):
            jaslxmc = jxb.JASLXMC
            suitable_classrooms = [
                classroom for classroom in suitable_classrooms
                # classroom.JASLX 为空时视为“多媒体教室”
                if (classroom.JASLX or '多媒体教室') == jaslxmc

            ]


        # 如果教学楼代码 JXLDM 为空字符串或 NaN，则不做教学楼过滤
        if _has_non_empty(getattr(jxb, "JXLDM", None)):
            jxl_val = jxb.JXLDM
            jxl_list = jxl_val if isinstance(jxl_val, list) else str(jxl_val).split(';')
            jxl_list = [x.strip() for x in jxl_list if x and not pd.isna(x)]

            if jxl_list:
                suitable_classrooms = [
                    classroom for classroom in suitable_classrooms
                    if classroom.JXLDM in jxl_list  # 确保JXLDM匹配
                ]


        return suitable_classrooms
    # Step 1: 汇总所有指定的教室代码（包括JASDM 和 LSJASDM）
    code_set = set()

    # if jxb.JASDM:
    #     code_set.add(jxb.JASDM.strip())
    if jxb.JASDM:
        codes = [c.strip() for c in jxb.JASDM.split(';') if c.strip()]
        code_set.update(codes)

    # if code_set:
    #     suitable_classrooms = [
    #         classroom for classroom in classroom_list
    #         if classroom.JASDM in code_set and classroom.SKZWS >= jxb.KRL and  classroom.MC == jxb.SKXQ #>= 0.7*classroom.SKZWS
    #     ]

       #return suitable_classrooms


    if jxb.LSJASDM:
        ls_codes = [code.strip() for code in jxb.LSJASDM.split(';') if code.strip()]
        code_set.update(ls_codes)

    # Step 2: 如果有明确指定的教室（JASDM或LSJASDM），按code筛选
    if code_set:
        suitable_classrooms = [
            classroom for classroom in classroom_list
            if classroom.JASDM in code_set and  classroom.SKZWS >= jxb.KRL and classroom.MC == jxb.SKXQ # >= 0.7*classroom.SKZWS
        ]

        # 只有当 JXLDM 真正有值时才做教学楼过滤
        if _has_non_empty(getattr(jxb, "JXLDM", None)):
            jxl_val = jxb.JXLDM
            jxl_list = jxl_val if isinstance(jxl_val, list) else str(jxl_val).split(';')
            jxl_list = [x.strip() for x in jxl_list if x and not pd.isna(x)]
            if jxl_list:
                suitable_classrooms = [
                    classroom for classroom in suitable_classrooms
                    if classroom.JXLDM in jxl_list  # 确保JXLDM匹配
                ]
        return suitable_classrooms

    # Step 3: 否则使用默认筛选逻辑：容量、校区、教室类型
    suitable_classrooms = [
        classroom for classroom in classroom_list
        if classroom.SKZWS >= jxb.KRL and classroom.MC == jxb.SKXQ  #>= 0.7*classroom.SKZWS
    ]

    # 若教学班“真正”要求教室类型，进一步过滤（空/NaN 不过滤；教室类型空视为多媒体教室）
    if _has_non_empty(getattr(jxb, "JASLXMC", None)):
        jaslxmc = jxb.JASLXMC
        suitable_classrooms = [
            classroom for classroom in suitable_classrooms
            if (classroom.JASLX or '多媒体教室') == jaslxmc
        ]

    # 若教学楼代码 JXLDM 为空字符串或 NaN，则不做教学楼过滤
    if _has_non_empty(getattr(jxb, "JXLDM", None)):
        jxl_val = jxb.JXLDM
        jxl_list = jxl_val if isinstance(jxl_val, list) else str(jxl_val).split(';')
        jxl_list = [x.strip() for x in jxl_list if x and not pd.isna(x)]
        if jxl_list:
            suitable_classrooms = [
                classroom for classroom in suitable_classrooms
                if classroom.JXLDM in jxl_list  # 确保JXLDM匹配
            ]
    

    return suitable_classrooms

#如果允许部分课程冲突，要修改 的实际上也是怎么取班级对象
def get_class_instances1(course, classes: list, all_courses: list) -> list:
    """
    根据班级名称字符串，从班级总表中提取对应实例，并加入KCM相同课程的特殊逻辑。

    Args:
        course: 当前课程对象，要求有 KCM 属性。
        class_names_str: 逗号分隔的班级名称字符串。
        classes: 所有班级实例的列表。
        all_courses: 当前所有课程对象列表。

    Returns:
        匹配的班级实例列表，或根据逻辑返回空列表。
    """

    class_names_str=course.TJBJ
    if not class_names_str or pd.isna(class_names_str): #如果是空值
        return []
    # 拆分班级名称
    class_names = [name.strip() for name in class_names_str.split(',') if name.strip()]

    # 先处理“当前数量 >= 8”这一条件
    if len(class_names) >= 8:
        return []

    # 检查是否有KCM相同且班级完全相同的其它课程
    for other_course in all_courses:
        if other_course.JXBID == course.JXBID:
            continue
        if getattr(other_course, 'KCM', None) == getattr(course, 'KCM', None):
            other_class_names_str = getattr(other_course, 'BJMC', '') or ''
            other_class_names = [n.strip() for n in other_class_names_str.split(',') if n.strip()]
            if set(other_class_names) == set(class_names):
                return []

    # 创建班级名称到实例的映射
    class_map = {class_obj.BJMC: class_obj for class_obj in classes}

    return [class_map[name] for name in class_names if name in class_map]



def schedule_class(jxbid, courses, teachers, classes, classrooms, flag_reschedule,num_days=6, relax_constraints=False,class_ralex=False):
    """为指定教学班进行排课
    Args:
        jxbid: 教学班ID
        courses: 所有课程对象列表
        teachers: 所有教师对象列表
        classes: 所有班级对象列表
        classrooms: 所有教室对象列表
        num_days: 每周天数
        relax_constraints: 是否放宽筛选条件
        
    Returns:
        tuple: (使用的教室对象, 排课安排, 教学周次列表)
    """
    # 获取教学班对象
    jxbs = get_consecutive_jxbid(jxbid, courses) #教学班相同的所有教学班对象
    if not jxbs:
        print(f"错误：未找到教学班 {jxbid}")
        return None, None, None
        
    # 获取教学周次
    teaching_weeks = get_teaching_weeks(jxbs)   #为什么要把同一教学班的所有课程的周次写成一个集合
    # 获取任课教师列表
    list_of_JSHs = [jxb.JSH for jxb in jxbs]    #该教学班对应的所有上课教师
    list_of_teacher_week = [
        trans_week_flags(jxb.RWJSZCDM) if hasattr(jxb, 'RWJSZCDM') and jxb.RWJSZCDM else []
        for jxb in jxbs
    ]

    list_of_teachers = get_teacher_instances(list_of_JSHs, teachers)
    teacher_week_map = {
        teacher.JSH: (teacher, weeks)
        for teacher, weeks in zip(list_of_teachers, list_of_teacher_week)
        if teacher is not None and weeks  # 注意这里确保周次非空才加入 map
    }   #老师和上课周次一一对应,确保周次非空才加入 map

    # 获取相关班级列表【修改】一直失败则放宽
    if class_ralex:
        list_of_classes=[]
    else:
        list_of_classes = get_class_instances1(jxbs[0], classes,courses) ##if hasattr(jxbs[0], 'TJBJ') else []
    
    # 获取周学时
    zxs = jxbs[0].ZXS
    time_constraints = build_preferences(jxbs[0].Prefer_Time,jxbs[0].unavailable_Time)  #这个课程的时间限制
    # 生成排课日模式
    day_patterns = new_generate_day_patterns(zxs, num_days,time_constraints,flag_reschedule)
    
    # 筛选适合的教室
    suitable_classrooms = filter_suitable_classrooms(classrooms, jxbs[0], relax_constraints)
    if not suitable_classrooms:
        print(f"错误：未找到适合教学班 {jxbid} 的教室")
        return None, None, None
    
    # 找出最优教室和方案
    best_classroom = None
    selected_arrangement = None
    max_arrangements = -1

    for classroom in suitable_classrooms:
        # 计算当前教室的排课方案
        current_arrangements = []
        for pattern in day_patterns:
            days_list, hours_list = pattern
            arrangements = new_check_period_availability(
                days_list, hours_list, classroom, teacher_week_map, list_of_classes, teaching_weeks,time_constraints)
            current_arrangements.extend(arrangements)
        
        # 如果找到更多方案的教室，更新记录
        if len(current_arrangements) > max_arrangements:
            max_arrangements = len(current_arrangements)
            best_classroom = classroom
            # 记录第一个可行方案，避免后续重复计算
            selected_arrangement = current_arrangements[0] if current_arrangements else None
    
    # 检查是否找到可行的教室和方案
    if max_arrangements <= 0 or not best_classroom:
        print(f"错误：教学班 {jxbid} 没有可行的排课方案")
        return None, None, None
    
    # 检查是否找到可行的排课方案
    if not selected_arrangement:
        print(f"错误：未找到教学班 {jxbid} 的可行排课方案")
        return best_classroom, None, teaching_weeks
    
    print(f"\n为教学班 {jxbid} ({jxbs[0].KCM}) 在教室 {best_classroom.JASMC} 排课")
    
    # 在时间表中标记排课信息
    for day, period, hours in selected_arrangement:
        for week in teaching_weeks:
            # 给课程标记:
            for every_jxb in jxbs:
                #every_jxb.IF_scheduled = True
                for p in range(period, period + hours):
                    every_jxb.timetable[week, day, p] =  best_classroom.JASDM

            # 在教室时间表中标记
            for p in range(period, period + hours):
                best_classroom.timetable[week, day, p] = jxbid
            
            # 在教师时间表中标记
            for teacher_id, (teacher, weeks) in teacher_week_map.items():
                if week in weeks:
                    for p in range(period, period + hours):
                        teacher.timetable[week, day, p]['course'] = jxbid  ###从课程名改成为教学班ID
                        teacher.timetable[week, day, p]['classroom'] = best_classroom.JASMC
            
            # 在班级时间表中标记
            for class_obj in list_of_classes:
                for p in range(period, period + hours):
                    class_obj.timetable[week, day, p]['course'] = jxbid   ###从课程名改成为教学班ID
                    class_obj.timetable[week, day, p]['classroom'] = best_classroom.JASMC


    
    # 打印排课结果
    print("\n排课成功!")
    print("具体安排:")
    for day, period, hours in selected_arrangement:
        day_name = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][day]
        period_range = f"第{period+1}-{period+hours}节"
        print(f"  {day_name} {period_range}")
    
    return best_classroom, selected_arrangement, teaching_weeks



def print_timetable_slice(classroom, week):
    """打印教室时间表的指定片段
    
    Args:
        classroom: 教室对象
        week: 要打印的周次
        days_range: 要打印的天数范围，默认为全部
        periods_range: 要打印的时段范围，默认为全部
    """
    days_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    
    # 获取时间表的实际维度
    max_days = classroom.timetable.shape[1]
    max_periods = classroom.timetable.shape[2]
        
    print(f"\n教室 {classroom.JASMC} 第{week+1}周时间表:")
    
    for day_idx in range(max_days):
            
        day_name = days_names[day_idx]
        # 仅当这一天有课程时打印
        has_course = False
        for p in range(max_periods):
            if classroom.timetable[week, day_idx, p] != '':
                has_course = True
                break
                
        if has_course:
            print(f"{day_name}:")
            for period in range(max_periods):
                if classroom.timetable[week, day_idx, period]:
                    course_id = classroom.timetable[week, day_idx, period]
                    print(f"  第{period+1}节: {course_id}")


def schedule_one_round(list_of_jxbids, courses, teachers, classes, list_of_jas, jasdm_dict, num_days):
    """执行一轮排课，找出方案数最少的课程并排课
    
    Args:
        list_of_jxbids: 待排课程ID列表
        courses: 所有课程对象列表
        teachers: 所有教师对象列表
        classes: 所有班级对象列表
        list_of_jas: 可用教室列表
        jasdm_dict: 教室代码到索引的映射字典
        num_days: 每周天数
        
    Returns:
        tuple: (排课是否成功, 排课的教学班ID, 使用的教室, 排课安排, 教学周次)
    """
    # 没有待排课程时直接返回
    if not list_of_jxbids:
        return False, None, None, None, None
    
    # 初始化资源列表和最大资源变量
    list_of_jasdms = list(jasdm_dict.keys())
    resource_list = [0 for _ in range(len(list_of_jasdms))]
    resource_max = float('inf')
    next_jxbid = ''
    
    # 记录选中课程是否需要放宽条件
    selected_relaxation_state = False
    
    # 记录是否所有课程都尝试了放宽条件但仍无法排课
    all_courses_failed = True
    
    # 找出资源最紧张的课程
    for i in range(len(list_of_jxbids)):
        # 获取给定教学班的所有对象（对应不同的任课老师）构成的列表
        list_of_jxbs = get_consecutive_jxbid(list_of_jxbids[i], courses)
        # 获取该教学班需要安排的周次
        teaching_weeks = get_teaching_weeks(list_of_jxbs)
        # 获取负责该教学班的所有教师的教师号构成的列表
        list_of_JSHs = [jxb.JSH for jxb in list_of_jxbs]
        # 获取所有负责该教学班的教师实例列表
        list_of_teachers = get_teacher_instances(list_of_JSHs, teachers)
        list_of_teacher_week = [
            trans_week_flags(jxb.RWJSZCDM) if hasattr(jxb, 'RWJSZCDM') and jxb.RWJSZCDM else []
            for jxb in list_of_jxbs
        ]

        teacher_week_map = {
            teacher.JSH: (teacher, weeks)
            for teacher, weeks in zip(list_of_teachers, list_of_teacher_week)
            if teacher is not None and weeks  # 注意这里确保周次非空才加入 map
        }  # 老师和上课周次一一对应,确保周次非空才加入 map

        # 获取相关班级列表
        list_of_classes = get_class_instances1(list_of_jxbs[0], classes,courses)
        # 筛选符合座位数和校区要求的教室对象，或根据教学班的教室代码直接指定教室
        sp_jas = filter_suitable_classrooms(list_of_jas, list_of_jxbs[0])
        time_constraints = build_preferences(list_of_jxbs[0].Prefer_Time, list_of_jxbs[0].unavailable_Time)  # 这个课程的时间限制
        # 获取基于ZXS的上课天模式，是这个flag_rechedule没传进行S
        day_patterns = new_generate_day_patterns(list_of_jxbs[0].ZXS, num_days,time_constraints,flag_reschedule=False)
        # 临时剩余资源表，用于循环内记录当前课程的剩余资源
        tem_resource_list = [0 for _ in range(len(list_of_jasdms))]

        
        for jas in sp_jas:
            num_slots = 0
            for pattern in day_patterns:
                days_list, hours_list = pattern
                # 检查所有可能的时段组合，添加班级参数
                arrangements = new_check_period_availability(
                    days_list, hours_list, jas,  teacher_week_map, list_of_classes, teaching_weeks,time_constraints
                )
                # 累加找到的所有可行方案数
                num_slots += len(arrangements)
            
            tem_resource_list[get_indices(jas.JASDM, jasdm_dict)] = num_slots
        
        # 标记当前课程是否需要放宽条件
        current_needs_relaxation = False
                
        # 如果没有找到可行方案，尝试放宽筛选条件
        if max(tem_resource_list) == 0:
            print(f"教学班 {list_of_jxbs[0].JXBID} 在指定/历史教室中无可用方案，放宽筛选条件")
            
            # 放宽教室筛选条件
            relaxed_sp_jas = filter_suitable_classrooms(list_of_jas, list_of_jxbs[0], relax_constraints=True)
            
            # 使用放宽条件后的教室重新计算方案
            tem_resource_list = [0 for _ in range(len(list_of_jasdms))]
            
            for jas in relaxed_sp_jas:
                num_slots = 0
                for pattern in day_patterns:
                    days_list, hours_list = pattern
                    # 添加班级参数
                    arrangements = new_check_period_availability(
                        days_list, hours_list, jas,  teacher_week_map, list_of_classes, teaching_weeks,time_constraints
                    )
                    num_slots += len(arrangements)
                
                tem_resource_list[get_indices(jas.JASDM, jasdm_dict)] = num_slots
            
            # print(f"放宽条件后，最大方案数: {max(tem_resource_list)}")
            current_needs_relaxation = True
        
        # 记录是否存在可排课程
        if max(tem_resource_list) > 0:
            all_courses_failed = False
            
        # 如果当前课程方案数更少或者还没有选中任何课程，则选择该课程
        if max(tem_resource_list) > 0 and (next_jxbid == '' or max(tem_resource_list) < resource_max):
            resource_list = tem_resource_list.copy()
            resource_max = max(tem_resource_list)
            next_jxbid = list_of_jxbs[0].JXBID
            selected_relaxation_state = current_needs_relaxation  # 使用当前课程的放宽状态
    
    # 判断是否找到了可排课程
    if not next_jxbid:
        if all_courses_failed:
            print("所有课程尝试放宽条件后仍无法排课")
        else:
            print("无法确定下一个要排的课程，排课过程终止")
        return False, '', None, None, None
    
    # 为找到的教学班排课，传递正确 的放宽条件标志
    best_classroom, selected_arrangement, teaching_weeks = schedule_class(
        next_jxbid, courses, teachers, classes, list_of_jas,
        False,
        num_days=num_days, 
        relax_constraints=selected_relaxation_state  # 使用选中课程的放宽状态
    )
    
    # 判断排课是否成功
    if best_classroom and selected_arrangement:
        return True, next_jxbid, best_classroom, selected_arrangement, teaching_weeks
    else:
        return False, next_jxbid, None, None, None


def save_scheduling_results(results, courses, output_file="排课结果.xlsx"):
    """将排课结果保存到Excel表格
    
    Args:
        results: 排课结果列表，每项包含(jxbid, classroom, arrangement, weeks)
        courses: 所有课程对象列表，用于获取教学班详细信息
        output_file: 输出Excel文件路径
    """
    import pandas as pd
    
    # 准备数据列表
    data = []
    days_map = {0: "周一", 1: "周二", 2: "周三", 3: "周四", 4: "周五", 5: "周六", 6: "周日"}
    
    for jxbid, classroom, arrangement, teaching_weeks in results:
        # 获取教学班的所有对象来提取基本信息
        jxb_objects = get_consecutive_jxbid(jxbid, courses)
        if not jxb_objects:
            continue
            
        # 从第一个对象获取基本信息
        jxb = jxb_objects[0]
        course_name = jxb.KCM if hasattr(jxb, 'KCM') else "未知"
        
        # 获取班级信息
        class_names = jxb.TJBJ if hasattr(jxb, 'TJBJ') and jxb.TJBJ else ""
        
        # 解析上课时间
        for day, period, hours in arrangement:
            # 对每个教师分别生成记录
            for jxb_obj in jxb_objects:
                
                # 解析教师的上课周次
                teacher_weeks = trans_week_flags(jxb_obj.RWJSZCDM)
                # 确保周次在总教学周次范围内
                teacher_weeks = [w for w in teacher_weeks if w in teaching_weeks]
                
                # 如果该教师没有教学周次，跳过
                if not teacher_weeks:
                    continue
                
                # 生成周次文本
                weeks_str = ",".join([str(w+1) for w in teacher_weeks])
                
                # 生成时间段文本
                day_str = days_map.get(day, f"第{day+1}天")
                period_str = f"第{period+1}-{period+hours}节"
                
                # 添加到数据列表
                data.append({
                    "教学班ID": jxbid,
                    "课程名称": course_name,
                    "开课院系":jxb.YXMC,
                    "教师": jxb_obj.XM,
                    "教师号": jxb_obj.JSH,
                    "周学时": jxb.ZXS,
                    "课容量":jxb.KRL,
                    "教室": classroom.JASMC if hasattr(classroom, 'JASMC') else "未知教室",
                    "教室代码": classroom.JASDM if hasattr(classroom, 'JASDM') else "未知",
                    "校区": jxb.SKXQ if hasattr(jxb, 'SKXQ') else "未知",
                    "班级信息": class_names,  # 添加班级信息字段
                    "偏好上课时间":jxb_obj.Prefer_Time,
                    "避免排课时间":jxb_obj.unavailable_Time,
                    "上课周次": weeks_str,
                    "星期": day_str,
                    "节次": period_str
                })
    
    # 创建DataFrame
    if data:
        df = pd.DataFrame(data)
        # 保存到Excel
        df.to_excel(output_file, index=False)
        print(f"\n排课结果已保存到 {output_file}")
    else:
        print("\n没有排课结果可保存")