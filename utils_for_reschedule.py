"""
排课/调课结果的持久化与统计模块。

主要内容：
  - save_timetables / load_timetables / restore_timetables：
    时间表状态（教师、教室、班级占用矩阵）以 pkl 形式保存与恢复
    （对应 排课结果/saved_timetables.pkl 和 reschedule_timetables.pkl）
  - rebuild_timetables_from_scheduled_courses：从排课结果 Excel 重建时间表
  - select_candidates / clear_placement / place_placement：调课时的候选腾挪操作
  - compute_class_usage_rates / compute_classroom_usage_rates_new：班级/教室利用率统计

Author: Li Qingxia
"""
from Basic_Data import *
from utils1 import *
import pickle  # 添加pickle模块用于序列化和反序列化
import time  # 添加time模块用于计时
from tryloadlimit import *
from collections import defaultdict

# 基础数据路径
#root_path = '智能排课基础数据'
#course_excel = os.path.join(root_path, '更新后的课程表.xlsx')
#classroom_excel = os.path.join(root_path, '更新后的教室表.xlsx')
#teacher_excel = os.path.join(root_path, '教师信息汇总.xlsx')
#banji_excel = os.path.join(root_path, '班级汇总.xlsx')

# root_path = '智能排课基础数据'
# course_excel = os.path.join(root_path, '课程表2025-2026-1.xlsx')
# classroom_excel = os.path.join(root_path, '更新后的教室表.xlsx')
# teacher_excel = os.path.join(root_path, '更新后的教师名单2025-2026-1.xlsx')
# banji_excel = os.path.join(root_path, '班级汇总2025-2026-1.xlsx')


root_path = os.path.join(os.path.dirname(__file__), '智能排课基础数据', '提取的基础数据表_converted')
course_excel = os.path.join(root_path, '课程表.xlsx')
classroom_excel =  os.path.join(root_path, '教室表.xlsx')
teacher_excel = os.path.join(root_path, '教师表.xlsx')
banji_excel = os.path.join(root_path, '班级表.xlsx')

# 排课参数
num_weeks = 20
num_periods = 11
num_days = 7

# 结果目录
results_dir = "排课结果"
failed_courses_file = os.path.join(results_dir, "排课失败课程_全部.xlsx")
scheduled_courses_file = os.path.join(results_dir, "排课结果_全部.xlsx")

# 时间表保存文件
# 排课程序保存的文件（用于加载）
scheduling_timetables_file = os.path.join(results_dir, "saved_timetables.pkl")
# 调课程序保存的文件
reschedule_timetables_file = os.path.join(results_dir, "reschedule_timetables.pkl")


###从excel加载排课结果
def load_failed_courses():
    """加载所有排课失败的课程"""
    try:
        df = pd.read_excel(failed_courses_file)
        print(f"成功加载{len(df)}个排课失败的课程")
        return df
    except Exception as e:
        print(f"加载排课失败课程文件出错: {e}")
        return None


def load_scheduled_courses():
    """加载所有已排课的课程"""
    try:
        df = pd.read_excel(scheduled_courses_file)
        print(f"成功加载{len(df)}个已排课的课程")
        return df
    except Exception as e:
        print(f"加载已排课课程文件出错: {e}")
        return None


###时间表保存文件pkl
def save_timetables(courses,teachers, classrooms, classes,timetables_file):
    """保存所有对象数据到文件，保持完整状态

    Args:
        teachers: 所有教师对象列表
        classrooms: 所有教室对象列表
        classes: 所有班级对象列表
    """
    try:
        # 保存完整的对象数据
        with open(timetables_file, 'wb') as f:
            pickle.dump({
                'courses':courses,
                'teachers': teachers,
                'classrooms': classrooms,
                'classes': classes
            }, f)
        print(f"所有对象数据已保存至 {timetables_file}")
        return True
    except Exception as e:
        print(f"保存对象数据出错: {e}")

        # 如果保存完整对象失败，尝试只保存时间表数据作为备份
        try:
            backup_file = os.path.join(results_dir, timetables_file+"_backup.pkl")
            timetable_data = {
                'courses': {course.JXBID: course.timetable for course in courses},
                'teachers': {teacher.JSH: teacher.timetable for teacher in teachers},
                'classrooms': {classroom.JASDM: classroom.timetable for classroom in classrooms},
                'classes': {class_obj.BJMC: class_obj.timetable for class_obj in classes}
            }
            with open(backup_file, 'wb') as f:
                pickle.dump(timetable_data, f)
            print(f"保存完整对象失败，但已成功保存时间表数据至备份文件 {backup_file}")
        except Exception as backup_error:
            print(f"备份时间表数据也失败: {backup_error}")

        return False


# 【这个函数明天再修改一下，简化完整化】
###从pkl文件中加载data['courses'], data['teachers'], data['classrooms'], data['classes']，该pkl文件中的数据是含有时间表的完整对象
def load_timetables(filename=scheduling_timetables_file):
    """从文件加载完整的对象数据
    优先加载排课程序保存的文件，如果没有则尝试加载调课程序保存的文件

    Returns:
        tuple: (课程列表, 教师列表, 教室列表, 班级列表) 或 None（如果加载失败）
    """
    try:
        # 优先尝试加载排课程序保存的文件
        if os.path.exists(filename):
            with open(filename, 'rb') as f:
                data = pickle.load(f)
            print(f"成功从排课程序文件 {filename} 加载完整对象数据")
            courses_count = len(data.get('courses', []))
            print(
                f"加载了 {courses_count} 个课程, {len(data['teachers'])} 个教师, {len(data['classrooms'])} 个教室, {len(data['classes'])} 个班级")
            return data.get('courses'), data['teachers'], data['classrooms'], data['classes']
        # 如果排课程序文件不存在，尝试加载调课程序保存的文件
        elif os.path.exists(reschedule_timetables_file):
            with open(reschedule_timetables_file, 'rb') as f:
                data = pickle.load(f)
            print(f"成功从调课程序文件 {reschedule_timetables_file} 加载完整对象数据")
            courses_count = len(data.get('courses', []))
            print(
                f"加载了 {courses_count} 个课程, {len(data['teachers'])} 个教师, {len(data['classrooms'])} 个教室, {len(data['classes'])} 个班级")
            return data.get('courses'), data['teachers'], data['classrooms'], data['classes']
        else:
            print(f"未找到对象数据文件: {scheduling_timetables_file} 或 {reschedule_timetables_file}")

            # 检查是否有备份的时间表数据
            backup_file = os.path.join(results_dir, "reschedule_timetables_backup.pkl")
            if os.path.exists(backup_file):
                print(f"找到备份时间表数据文件，尝试恢复基本时间表...")
                # 从基础数据创建对象
                courses = load_courses(course_excel, weeks=num_weeks, days=num_days, periods=num_periods)
                teachers = load_teachers(teacher_excel, weeks=num_weeks, days=num_days, periods=num_periods)
                classrooms = load_classrooms(classroom_excel, weeks=num_weeks, days=num_days, periods=num_periods)
                classes = load_classes(banji_excel, weeks=num_weeks, days=num_days, periods=num_periods)

                # 加载备份的时间表数据
                with open(backup_file, 'rb') as f:
                    timetable_data = pickle.load(f)

                # 恢复时间表
                restored = restore_timetables(timetable_data, courses, teachers, classrooms, classes)
                if restored:
                    print("成功从备份文件恢复基本时间表数据")
                    return courses, teachers, classrooms, classes

            return None
    except Exception as e:
        print(f"加载对象数据出错: {e}")
        print("尝试从备份恢复基本时间表...")

        try:
            # 检查备份文件
            backup_file = os.path.join(results_dir, "reschedule_timetables_backup.pkl")
            if os.path.exists(backup_file):
                # 从基础数据创建对象
                courses = load_courses(course_excel, weeks=num_weeks, days=num_days, periods=num_periods)
                teachers = load_teachers(teacher_excel, weeks=num_weeks, days=num_days, periods=num_periods)
                classrooms = load_classrooms(classroom_excel, weeks=num_weeks, days=num_days, periods=num_periods)
                classes = load_classes(banji_excel, weeks=num_weeks, days=num_days, periods=num_periods)

                # 加载备份的时间表数据
                with open(backup_file, 'rb') as f:
                    timetable_data = pickle.load(f)

                # 恢复时间表
                restored = restore_timetables(timetable_data, courses, teachers, classrooms, classes)
                if restored:
                    print("成功从备份文件恢复基本时间表数据")
                    return courses, teachers, classrooms, classes
        except Exception as backup_error:
            print(f"从备份恢复也失败: {backup_error}")

        return None


# 使用保存的时间表数据恢复对象的时间表
def restore_timetables(timetable_data, courses, teachers, classrooms, classes):
    """使用保存的时间表数据恢复对象的时间表

    Args:
        timetable_data: 保存的时间表数据字典
        courses: 课程对象列表
        teachers: 教师对象列表
        classrooms: 教室对象列表
        classes: 班级对象列表

    Returns:
        bool: 是否成功恢复
    """
    try:
        # 创建映射字典以提高查找效率
        course_map = {course.JXBID: course for course in courses}
        teacher_map = {teacher.JSH: teacher for teacher in teachers}
        classroom_map = {classroom.JASDM: classroom for classroom in classrooms}
        class_map = {class_obj.BJMC: class_obj for class_obj in classes}

        # 恢复时间表数据
        restored_courses = 0
        if 'courses' in timetable_data:
            for jxbid, timetable in timetable_data['courses'].items():
                if jxbid in course_map:
                    course_map[jxbid].timetable = timetable
                    restored_courses += 1
        
        restored_teachers = 0
        if 'teachers' in timetable_data:
            for jsh, timetable in timetable_data['teachers'].items():
                if jsh in teacher_map:
                    teacher_map[jsh].timetable = timetable
                    restored_teachers += 1

        restored_classrooms = 0
        if 'classrooms' in timetable_data:
            for jasdm, timetable in timetable_data['classrooms'].items():
                if jasdm in classroom_map:
                    classroom_map[jasdm].timetable = timetable
                    restored_classrooms += 1

        restored_classes = 0
        if 'classes' in timetable_data:
            for bjmc, timetable in timetable_data['classes'].items():
                if bjmc in class_map:
                    class_map[bjmc].timetable = timetable
                    restored_classes += 1

        print(f"已恢复时间表: {restored_courses}个课程, {restored_teachers}个教师, {restored_classrooms}个教室, {restored_classes}个班级")
        return True
    except Exception as e:
        print(f"恢复时间表出错: {e}")
        return False


# 从excel已排课程数据重构时间表
def rebuild_timetables_from_scheduled_courses(scheduled_df, teachers, classrooms, classes, courses):
    """从已排课程数据重构时间表

    Args:
        scheduled_df: 已排课程的DataFrame
        teachers: 所有教师对象列表
        classrooms: 所有教室对象列表
        classes: 所有班级对象列表
        courses: 所有课程对象列表

    Returns:
        bool: 重构是否成功
    """
    if scheduled_df is None or scheduled_df.empty:
        print("没有已排课程数据，无法重构时间表")
        return False

    try:
        print("开始从排课结果重构时间表...")
        rebuild_count = 0
        missing_rooms_count = 0

        # 建立教室代码到教室对象的映射字典，提高查找效率
        classroom_map = {classroom.JASDM: classroom for classroom in classrooms}

        # 按教学班ID分组处理记录，确保每个时间段的记录只处理一次
        unique_jxbids = scheduled_df['教学班ID'].unique()

        for jxbid in unique_jxbids:
            # 获取当前教学班的所有记录
            jxb_records = scheduled_df[scheduled_df['教学班ID'] == jxbid]

            # 获取课程对象
            jxbs = get_consecutive_jxbid(jxbid, courses)
            if not jxbs:
                print(f"警告：未找到课程对象 {jxbid}，跳过重构该课程")
                continue

            # 获取教室对象
            classroom_code = jxb_records.iloc[0]['教室代码']
            classroom = classroom_map.get(classroom_code)

            if not classroom:
                # 找不到教室时，尝试查找符合条件的替代教室
                course_name = jxb_records.iloc[0]['课程名称']
                missing_rooms_count += 1

                # 获取校区信息
                campus = jxb_records.iloc[0]['校区'] if '校区' in jxb_records.columns else None

                # 尝试找到一个合适的 替代教室
                for room in classrooms:
                    if (campus is None or room.MC == campus) and str(room.SFYXPK).strip() in ('1', '1.0'):
                        classroom = room
                        print(
                            f"注意：未找到原教室 {classroom_code}，为课程 {jxbid} ({course_name}) 分配替代教室 {room.JASDM} ({room.JASMC})")
                        break

                if not classroom:
                    print(f"错误：未找到教室 {classroom_code} 且无法分配替代教室，跳过重构课程 {jxbid} ({course_name})")
                    continue

            # 获取教师和班级列表
            list_of_JSHs = [jxb.JSH for jxb in jxbs]
            list_of_classes = get_class_instances1(jxbs[0], classes, courses)
            list_of_teacher_week = [
                trans_week_flags(jxb.RWJSZCDM) if hasattr(jxb, 'RWJSZCDM') and jxb.RWJSZCDM else []
                for jxb in jxbs
            ]

            list_of_teachers = get_teacher_instances(list_of_JSHs, teachers)
            teacher_week_map = {
                teacher.JSH: (teacher, weeks)
                for teacher, weeks in zip(list_of_teachers, list_of_teacher_week)
                if teacher is not None and weeks  # 注意这里确保周次非空才加入 map
            }  # 老师和上课周次一一对应,确保周次非空才加入 map

            # 解析上课周次
            weeks_str = jxb_records.iloc[0]['上课周次']
            teaching_weeks = [int(w) - 1 for w in weeks_str.split(',')]  # 转换为0-索引

            # 解析每个时间段的安排
            day_map = {"周一": 0, "周二": 1, "周三": 2, "周四": 3, "周五": 4, "周六": 5, "周日": 6}

            # 获取不重复的时间段记录
            unique_timeslots = jxb_records.drop_duplicates(subset=['星期', '节次'])

            for _, record in unique_timeslots.iterrows():
                day_text = record['星期']
                period_text = record['节次']

                # 解析星期和节次
                day = day_map.get(day_text, 0)
                period_range = period_text.replace("第", "").replace("节", "").split("-")
                start_period = int(period_range[0]) - 1
                end_period = int(period_range[1]) if len(period_range) > 1 else start_period + 1
                hours = end_period - start_period

                # 更新时间表
                for week in teaching_weeks:
                    for every_jxb in jxbs:
                       #every_jxb.IF_scheduled = True
                        for p in range(start_period, start_period + hours):
                            every_jxb.timetable[week, day, p] = classroom.JASDM

                    # 更新教室时间表
                    for p in range(start_period, start_period + hours):
                        classroom.timetable[week, day, p] = jxbid

                    # 更新教师时间表
                    # 在教师时间表中标记
                    for teacher_id, (teacher, weeks) in teacher_week_map.items():
                        if week in weeks:
                            for p in range(start_period, start_period + hours):
                                teacher.timetable[week, day, p]['course'] = jxbs[0].KCM
                                teacher.timetable[week, day, p]['classroom'] = classroom.JASMC

                    # 更新班级时间表
                    for class_obj in list_of_classes:
                        for p in range(start_period, start_period + hours):
                            class_obj.timetable[week, day, p]['course'] = jxbs[0].KCM
                            class_obj.timetable[week, day, p]['classroom'] = classroom.JASMC

       

            rebuild_count += 1

        print(f"成功重构 {rebuild_count} 个教学班的时间表")
        if missing_rooms_count > 0:
            print(f"注意：有 {missing_rooms_count} 个教学班的原教室未找到，已分配替代教室")
        return True
    except Exception as e:
        print(f"重构时间表出错: {e}")
        return False

def compute_classroom_usage_rates_old(total_weeks, days_of_week, periods_per_day, output_file='classroom_usage.xlsx'):
    records = []
    timetables_data = load_timetables(reschedule_timetables_file)

    if timetables_data:
        _, _, classrooms, _ = timetables_data
        print("使用已保存的时间表数据计算占有率")
    else:
        print("未能加载时间表数据")
        return

    slots_per_week = days_of_week * periods_per_day

    for classroom in classrooms:
        code = getattr(classroom, 'JASDM', '')
        name = getattr(classroom, 'JASMC', '')
        kind = getattr(classroom, 'JASLX', '')
        IFcoulduse= getattr(classroom, 'SFYXPK', '')
        campus = getattr(classroom, 'MC', '')
        capacity = getattr(classroom, 'SKZWS', '')

        if str(IFcoulduse).strip() in ('0', '0.0'):
            continue

        for w in range(total_weeks):
            used_slots = 0
            for d in range(days_of_week):
                for p in range(periods_per_day):
                    if classroom.timetable[w, d, p]:  # 非空表示使用中
                        used_slots += 1
            usage_rate = used_slots / slots_per_week * 100

            records.append({
                '教室代码': code,
                '教室名称': name,
                '教室类型': kind,
                '校区': campus,
                '上课座位数': capacity,
                '周次': f'第{w + 1}周',
                '使用率(%)': round(usage_rate, 2)
            })

    df = pd.DataFrame(records)

    # pivot横向展开
    df_pivot = df.pivot(index=['教室代码', '教室名称', '教室类型', '校区', '上课座位数'],
                        columns='周次',
                        values='使用率(%)')

    # 周次列排序
    sorted_columns = sorted(df_pivot.columns, key=lambda x: int(x.replace('第', '').replace('周', '')))
    df_pivot = df_pivot[sorted_columns]

    # 填空值为0.0（表示完全未使用）
    df_pivot = df_pivot.fillna(0.0)

    # 可选：添加平均使用率
    df_pivot['平均使用率(%)'] = df_pivot.mean(axis=1)

    # 重置索引便于导出
    df_pivot.reset_index(inplace=True)

    # 保存到 Excel
    output_path = os.path.join(results_dir, output_file)
    df_pivot.to_excel(output_path, index=False)
    print(f"教室每周使用率（横向表格）已导出到 {output_path}")



##计算每个班级每周的占用率
def compute_class_usage_rates_old(total_weeks, days_of_week, periods_per_day, output_file='class_usage.xlsx'):
    records = []
    timetables_data = load_timetables(reschedule_timetables_file)

    if timetables_data:
        _, _, _, classes = timetables_data
        print("使用已保存的时间表数据计算占有率")
    else:
        print("未能加载时间表数据")
        return

    slots_per_week = days_of_week * periods_per_day

    for classs in classes:
        class_name = getattr(classs, 'BJMC', '')
        for w in range(total_weeks):
            used_slots = 0
            for d in range(days_of_week):
                for p in range(periods_per_day):
                    if classs.timetable[w, d, p]:
                        used_slots += 1
            usage_rate = used_slots / slots_per_week * 100
            records.append({
                '班级名称': class_name,
                '周次': f'第{w+1}周',
                '使用率(%)': round(usage_rate, 2)
            })

    df = pd.DataFrame(records)
    df_pivot = df.pivot(index='班级名称', columns='周次', values='使用率(%)')

    # 列排序
    sorted_columns = sorted(df_pivot.columns, key=lambda x: int(x.replace('第', '').replace('周', '')))
    df_pivot = df_pivot[sorted_columns]

    # 填空白
    df_pivot = df_pivot.fillna(0.0)  # 或者 df_pivot.fillna(np.nan)

    df_pivot.reset_index(inplace=True)
    output_path = os.path.join(results_dir, output_file)
    df_pivot.to_excel(output_path, index=False)
    print(f"班级每周使用率（横向表格）已导出到 {output_path}")




###新增的
def prepare_state(timetables_data):
    # 如果成功加载时间表，直接使用加载的数据
    if timetables_data:
        teachers, classrooms, classes = timetables_data
        print("使用已保存的时间表数据进行调课")
        # 仍然需要加载课程数据
        courses = load_courses(course_excel, weeks=num_weeks, days=num_days, periods=num_periods)
    else:
        # 如果加载失败，从基础数据重新构建
        print("从基础数据重新构建时间表")
        teachers = load_teachers(teacher_excel, weeks=num_weeks, days=num_days, periods=num_periods)
        classrooms = load_classrooms(classroom_excel, weeks=num_weeks, days=num_days, periods=num_periods)
        courses = load_courses(course_excel, weeks=num_weeks, days=num_days, periods=num_periods)
        classes = load_classes(banji_excel, weeks=num_weeks, days=num_days, periods=num_periods)

        # 加载已排课的课程数据，重构时间表
        print("尝试从已排课数据重构时间表...")
        scheduled_df = load_scheduled_courses()  # excel数据
        if scheduled_df is not None:
            rebuild_timetables_from_scheduled_courses(scheduled_df, teachers, classrooms, classes, courses)


    # 加载排课失败的课程
    failed_df = load_failed_courses()
    # 加载已排课的课程
    scheduled_df = load_scheduled_courses()

    return teachers, classrooms, classes, courses, failed_df, scheduled_df


def select_candidates(
    scheduled_df, zxs, teaching_weeks_failed,
    classroom_map_for_sheduled, teacher_week_map,
    require_same_zxs: bool=False,
    require_weeks_cover: bool=True,
    require_classroom: bool=True,
    require_teacher: bool=False
):
    df = scheduled_df.copy()
    mask = pd.Series(True, index=df.index)

    # 1) 周学时
    if require_same_zxs:
        if '周学时' not in df.columns:
            return df.head(0)  # 没有必要列则无解
        mask &= pd.to_numeric(df['周学时'], errors='coerce') == pd.to_numeric(zxs)

    # 2) 教师匹配
    if require_teacher:
        if '教师号' not in df.columns:
            return df.head(0)
        allowed_teachers = set(map(str, teacher_week_map.keys()))
        mask &= df['教师号'].astype(str).isin(allowed_teachers)

    # 3) 教室匹配
    if require_classroom:
        if '教室代码' not in df.columns:
            return df.head(0)
        allowed_rooms = set(map(str, classroom_map_for_sheduled))  # 或 classroom_map_for_sheduled.keys()
        mask &= df['教室代码'].astype(str).isin(allowed_rooms)

    # 4) 周次覆盖匹配（按教学班ID挑一条代表行判断）
    if require_weeks_cover:
        if '教学班ID' not in df.columns or '上课周次' not in df.columns:
            return df.head(0)

        valid_ids = []
        # 只看每个教学班的一条代表记录（
        reps = df.loc[mask].drop_duplicates(subset=['教学班ID'])
        failed_set = set(teaching_weeks_failed)

        for _, rec in reps.iterrows():
            weeks_str = rec['上课周次']
            if not isinstance(weeks_str, str) or not weeks_str.strip():
                continue
            try:
                weeks_list = [
                    int(w.strip()) - 1
                    for w in weeks_str.split(',')
                    if w.strip().isdigit()
                ]
                if failed_set.issubset(set(weeks_list)):
                    valid_ids.append(rec['教学班ID'])
            except Exception:
                # 解析失败则判为不匹配
                pass

        if not valid_ids:
            return df.head(0)

        mask &= df['教学班ID'].isin(valid_ids)
    # 最终去重
    return df.loc[mask].drop_duplicates(subset=['教学班ID']).copy()


##清除时间表
def clear_placement(current_classroom, arrangements,teaching_weeks,jxbid,jxbs,teacher_week_map,  classes) :
    """
    将某课程在 教室/教师/班级 时间表中的占位清空。
    - 教室.timetable 里存的是教学班ID（或类似标记），用 classroom_marker 对比清空。
    - 教师/班级 timetable 的 cell 是 {'course': 课程名, 'classroom': 教室名}，用 course_name 对比清空。
    """
    for day, period, hours in arrangements:
        for week in teaching_weeks:
            for every_jxb in jxbs:
                for p in range(period, period + hours):
                    every_jxb.timetable[week, day, p] = ""
            # 清除教室时间表
            for p in range(period, period + hours):
                #if current_classroom.timetable[week, day, p] == jxbid:
                current_classroom.timetable[week, day, p] = ""

            # 在教师时间表中标记
            for teacher_id, (teacher, weeks) in teacher_week_map.items():
                if week in weeks:
                    for p in range(period, period + hours):
                        #if teacher.timetable[week, day, p]['course'] == jxbid:
                        teacher.timetable[week, day, p]['course'] = ""
                        teacher.timetable[week, day, p]['classroom'] = ""

            # 清除班级时间表
            for class_obj in classes:
                for p in range(period, period + hours):
                    #if class_obj.timetable[week, day, p]['course'] == jxbid:
                    class_obj.timetable[week, day, p]['course'] = ""
                    class_obj.timetable[week, day, p]['classroom'] = ""

##写入时间表
def place_placement(current_classroom, arrangements, teaching_weeks, jxbid,jxbs, teacher_week_map, classes):
    """
    将“候选课程”安排到新位置：同时写入 教室/教师/班级 三张时间表。
    教师/班级 timetable 的单元为 dict，按你的原代码用 candidate_jxbid 记录到 'course' 字段。
    """
    for day, period, hours in arrangements:
        for week in teaching_weeks:
            for every_jxb in jxbs:
               #every_jxb.IF_scheduled = True
                for p in range(period, period + hours):
                    every_jxb.timetable[week, day, p] = current_classroom.JASDM
            # 教室：直接写入教学班ID
            for p in range(period, period + hours):
                current_classroom.timetable[week, day, p] = jxbid

            # 教师：仅在该教师的有效周内写入
            for _, (teacher, weeks) in teacher_week_map.items():
                if week in weeks:
                    for p in range(period, period + hours):
                        teacher.timetable[week, day, p]['course'] = jxbid
                        teacher.timetable[week, day, p]['classroom'] = current_classroom.JASMC

            # 班级：写入课程与教室
            for class_obj in classes:
                for p in range(period, period + hours):
                    class_obj.timetable[week, day, p]['course'] = jxbid
                    class_obj.timetable[week, day, p]['classroom'] = current_classroom.JASMC

######新的班占用
import os
import numpy as np
import pandas as pd

def _is_occupied_by_value(x):
    """判断单元格是否被占用的通用规则（更宽松）"""
    try:
        # None / False / 0 / nan / 空字符串 视为未占用
        if x is None:
            return False
        if isinstance(x, float) and np.isnan(x):
            return False
        if x is False:
            return False
        if isinstance(x, (int, np.integer)) and x == 0:
            return False
        if isinstance(x, str) and x.strip() == '':
            return False
        # 其它情况尽量认为是占用（与旧实现 bool(val) 行为保持相似）
        return bool(x)
    except Exception:
        return False

def _safe_index_convert(timetable, total_weeks, days_of_week, periods_per_day, class_name=None, verbose=False):
    """逐格索引回退策略，行为与旧版逐格判断一致（较慢但稳妥）"""
    out = np.zeros((total_weeks, days_of_week, periods_per_day), dtype=bool)

    def try_get(w, d, p):
        try:
            return timetable[w, d, p]
        except Exception:
            try:
                week = timetable[w]
                try:
                    return week[d][p]
                except Exception:
                    try:
                        return week[d, p]
                    except Exception:
                        return None
            except Exception:
                return None

    for w in range(total_weeks):
        for d in range(days_of_week):
            for p in range(periods_per_day):
                val = try_get(w, d, p)
                if _is_occupied_by_value(val):
                    out[w, d, p] = True

    if verbose:
        print(f"[safe_convert] class={class_name} occupied_slots={int(out.sum())}")
    return out

def compute_class_usage_rates(
    total_weeks,
    days_of_week,
    periods_per_day,
    reschedule_timetables_file,
    results_dir,
    output_file='class_usage.xlsx',
    period_ranges=None,        # e.g. [(1,2), (5,6)]  (1-based inclusive)
    weeks_of_interest=None,     # e.g. [1,5,12] (1-based)
    verbose=False
):
    """
    计算并导出每个班级的上课/占用率评估表（多个 sheet）。
    兼容结构化 ndarray（含 'course'/'classroom' 字段）与常见非结构化格式。
    返回包含各 DataFrame 与输出路径的字典。
    """
    if period_ranges is None:
        period_ranges = [(1, 2), (3,4),(5, 6),(7,8)]
    if weeks_of_interest is None:
        weeks_of_interest = [1, 5, 9,13]

    timetables_data = load_timetables(reschedule_timetables_file)
    if not timetables_data:
        print("未能加载时间表数据")
        return

    _, _, _, classes = timetables_data
    if verbose:
        print("使用已保存的时间表数据计算班级占有率（verbose=True）")
    else:
        print("使用已保存的时间表数据计算班级占有率")

    slots_per_week = days_of_week * periods_per_day
    total_slots_semester = total_weeks * slots_per_week

    # 0-based 索引
    weeks_idx = [w - 1 for w in weeks_of_interest if 1 <= w <= total_weeks]
    period_ranges_idx = []
    for (a, b) in period_ranges:
        a0 = max(0, a - 1)
        b0 = min(periods_per_day - 1, b - 1)
        if a0 <= b0:
            period_ranges_idx.append((a0, b0))

    overall_records = []
    period_specific_records = []
    weeks_records = []

    for cls_obj in classes:
        class_name = getattr(cls_obj, 'BJMC', '')
        timetable = getattr(cls_obj, 'timetable', None)

        # 默认空矩阵
        tt_bool = np.zeros((total_weeks, days_of_week, periods_per_day), dtype=bool)

        if timetable is None:
            if verbose:
                print(f"[WARN] class {class_name}: timetable is None -> treat as empty")
            tt_bool = np.zeros((total_weeks, days_of_week, periods_per_day), dtype=bool)
        else:
            try:
                # 1) 如果是 numpy 结构化数组（record array），优先使用字段判断
                if isinstance(timetable, np.ndarray) and getattr(timetable.dtype, 'names', None):
                    # 常见字段：'course' 或 'classroom'
                    names = timetable.dtype.names
                    if 'course' in names:
                        # 空字符串视为未占用
                        field = timetable['course']
                        # field 可能是 shape (weeks, days, periods) 的字符串数组
                        tt_bool = (field.astype('U') != '')
                    elif 'classroom' in names:
                        field = timetable['classroom']
                        tt_bool = (field.astype('U') != '')
                    else:
                        # 没有已知字段，尝试把 record 转为 object 并使用通用判定
                        arr = timetable.astype(object)
                        # arr 的每个元素是一个 record，需拆字段判断：用 repr(record) 判定是否含信息
                        vec = np.vectorize(lambda rec: not (rec is None or str(rec).strip() == ''), otypes=[bool])
                        tt_bool = vec(arr)
                else:
                    # 2) 普通 ndarray / list / DataFrame 等
                    if isinstance(timetable, pd.DataFrame):
                        arr = timetable.values
                    else:
                        arr = np.asarray(timetable, dtype=object)

                    # 如果是布尔型 ndarray，直接使用（并进行裁/补）
                    if isinstance(arr, np.ndarray) and arr.dtype == bool and arr.ndim == 3:
                        tt_bool = arr.copy()
                    else:
                        # 尝试对 object/混合类型做向量化判断
                        try:
                            # 先尝试直接向量化判断（快）
                            vec = np.vectorize(_is_occupied_by_value, otypes=[bool])
                            tt_bool = vec(arr)
                        except Exception:
                            # 回退到逐格索引（最稳妥）
                            if verbose:
                                print(f"[INFO] class {class_name}: fallback to safe indexing conversion")
                            tt_bool = _safe_index_convert(timetable, total_weeks, days_of_week, periods_per_day, class_name=class_name, verbose=verbose)

                # 3) 确保 tt_bool 是 3D 并按需裁/补齐
                if not isinstance(tt_bool, np.ndarray) or tt_bool.ndim != 3:
                    # 如果是 2D（单周），扩展为多周
                    if isinstance(tt_bool, np.ndarray) and tt_bool.ndim == 2:
                        h, w = tt_bool.shape
                        if h == days_of_week and w == periods_per_day:
                            tt_bool = np.repeat(tt_bool[np.newaxis, :, :], total_weeks, axis=0)
                        else:
                            # 形状不匹配时回退逐格
                            tt_bool = _safe_index_convert(timetable, total_weeks, days_of_week, periods_per_day, class_name=class_name, verbose=verbose)
                    else:
                        tt_bool = _safe_index_convert(timetable, total_weeks, days_of_week, periods_per_day, class_name=class_name, verbose=verbose)

                # 裁或补齐到期望尺寸
                wdim, ddim, pdim = tt_bool.shape
                if wdim < total_weeks:
                    pad = np.zeros((total_weeks - wdim, ddim, pdim), dtype=bool)
                    tt_bool = np.concatenate([tt_bool, pad], axis=0)
                if ddim < days_of_week or pdim < periods_per_day:
                    new_tt = np.zeros((total_weeks, days_of_week, periods_per_day), dtype=bool)
                    new_tt[:tt_bool.shape[0], :ddim, :pdim] = tt_bool[:total_weeks, :ddim, :pdim]
                    tt_bool = new_tt
                else:
                    tt_bool = tt_bool[:total_weeks, :days_of_week, :periods_per_day]

            except Exception as e:
                if verbose:
                    print(f"[ERROR] class {class_name}: parse timetable failed -> {e}")
                # 兜底：空矩阵
                tt_bool = np.zeros((total_weeks, days_of_week, periods_per_day), dtype=bool)

        # ---- 1) 全学期全天所有时段 ----
        used_slots_semester = int(tt_bool.sum())
        overall_usage_rate = (used_slots_semester / total_slots_semester) * 100 if total_slots_semester > 0 else 0.0
        overall_records.append({
            '班级名称': class_name,
            '被占格数(学期)': int(used_slots_semester),
            '总格数(学期)': int(total_slots_semester),
            '学期使用率(%)': round(overall_usage_rate, 4)
        })

        # ---- 2) 指定时段使用率 ----
        for (a0, b0) in period_ranges_idx:
            selected = tt_bool[:, :, a0:b0 + 1]  # 所有周、所有日、指定节段
            used = int(selected.sum())
            slots_per_week_range = days_of_week * (b0 - a0 + 1)
            total_slots_range = total_weeks * slots_per_week_range
            usage_rate_range = (used / total_slots_range) * 100 if total_slots_range > 0 else 0.0
            period_specific_records.append({
                '班级名称': class_name,
                '节段': f'{a0+1}-{b0+1}节',
                '被占格数(学期内该节段)': int(used),
                '总格数(该节段,学期)': int(total_slots_range),
                '该节段使用率(%)': round(usage_rate_range, 4)
            })

        # ---- 3) 指定周（周内所有时段） ----
        for w_idx in weeks_idx:
            if w_idx < 0 or w_idx >= tt_bool.shape[0]:
                continue
            week_slice = tt_bool[w_idx, :, :]
            used_in_week = int(week_slice.sum())
            usage_rate_week = (used_in_week / slots_per_week) * 100 if slots_per_week > 0 else 0.0
            weeks_records.append({
                '班级名称': class_name,
                '周次': f'第{w_idx + 1}周',
                '被占格数(周内)': int(used_in_week),
                '总格数(周内)': int(slots_per_week),
                '该周使用率(%)': round(usage_rate_week, 4)
            })

    # === DataFrame 与透视 ===
    df_overall = pd.DataFrame(overall_records)
    df_period = pd.DataFrame(period_specific_records)
    df_weeks = pd.DataFrame(weeks_records)

    # 学期全时段：直接输出（按班级）
    if not df_overall.empty:
        df_over_pivot = df_overall.sort_values('班级名称').reset_index(drop=True)
    else:
        df_over_pivot = pd.DataFrame()

    # 指定时段：pivot 每个节段为列，并计算平均
    if not df_period.empty:
        df_period_pivot = df_period.pivot_table(
            index=['班级名称'],
            columns='节段',
            values='该节段使用率(%)'
        )
        col_order = [f'{a}-{b}节' for (a, b) in [(r[0]+1, r[1]+1) for r in period_ranges_idx]]
        col_order = [c for c in col_order if c in df_period_pivot.columns]
        df_period_pivot = df_period_pivot[col_order] if col_order else df_period_pivot
        df_period_pivot = df_period_pivot.reset_index()
        if col_order:
            df_period_pivot['平均使用率(%)'] = df_period_pivot[col_order].mean(axis=1)
    else:
        df_period_pivot = pd.DataFrame()

    # 指定周：pivot 每周为列，并计算平均
    if not df_weeks.empty:
        df_weeks_pivot = df_weeks.pivot_table(
            index=['班级名称'],
            columns='周次',
            values='该周使用率(%)'
        )
        expected_week_cols = [f'第{w}周' for w in weeks_of_interest if f'第{w}周' in df_weeks_pivot.columns]
        df_weeks_pivot = df_weeks_pivot[expected_week_cols] if expected_week_cols else df_weeks_pivot
        df_weeks_pivot = df_weeks_pivot.reset_index()
        if expected_week_cols:
            df_weeks_pivot['平均使用率(%)'] = df_weeks_pivot[expected_week_cols].mean(axis=1)
    else:
        df_weeks_pivot = pd.DataFrame()

    # 汇总平均（班级层面）
    summary_rows = []
    if not df_over_pivot.empty and '学期使用率(%)' in df_over_pivot.columns:
        summary_rows.append({'指标': '全学期全天平均使用率(%)', '值': round(df_over_pivot['学期使用率(%)'].mean(), 4)})
    if not df_period_pivot.empty and '平均使用率(%)' in df_period_pivot.columns:
        summary_rows.append({'指标': '各指定节段平均使用率(%)', '值': round(df_period_pivot['平均使用率(%)'].mean(), 4)})
    if not df_weeks_pivot.empty and '平均使用率(%)' in df_weeks_pivot.columns:
        summary_rows.append({'指标': f'指定周（{",".join(map(str, weeks_of_interest))}）平均使用率(%)', '值': round(df_weeks_pivot['平均使用率(%)'].mean(), 4)})
    df_summary = pd.DataFrame(summary_rows)

    # 保存到 Excel 多 sheet
    os.makedirs(results_dir, exist_ok=True)
    output_path = os.path.join(results_dir, output_file)
    with pd.ExcelWriter(output_path, engine='openpyxl') as writer:
        if not df_over_pivot.empty:
            df_over_pivot.to_excel(writer, sheet_name='班级_学期_全时段', index=False)
        if not df_period_pivot.empty:
            df_period_pivot.to_excel(writer, sheet_name='班级_指定时段', index=False)
        if not df_weeks_pivot.empty:
            df_weeks_pivot.to_excel(writer, sheet_name='班级_指定周', index=False)
        if not df_summary.empty:
            df_summary.to_excel(writer, sheet_name='班级_汇总平均', index=False)

    print(f"班级使用率评估已导出到: {output_path}")
    return {
        'overall': df_over_pivot,
        'period_specific': df_period_pivot,
        'weeks': df_weeks_pivot,
        'summary': df_summary,
        'output_path': output_path
    }



def compute_classroom_usage_rates_new(
    total_weeks,
    days_of_week,
    periods_per_day,
    reschedule_timetables_file,
    results_dir,
    output_file='classroom_usage.xlsx',
    period_ranges=None,            # e.g. [(1,2), (3,4), (5,6)]
    weeks_of_interest=None         # e.g. [1, 5, 12]
):
    """
    计算并导出教室使用率评估表（多个 sheet）：
      - 全学期全天使用率（每教室）
      - 指定时段的使用率（每教室，对每个 period_range 单独列）
      - 指定周的周内所有时段使用率（例如第1、5、12周）
    """

    # 默认参数
    if period_ranges is None:
        period_ranges = [(1, 2),(3,4), (5, 6),(7,8)]
    if weeks_of_interest is None:
        weeks_of_interest = [1, 5, 9,13]

    # 加载时间表数据（需用户实现 load_timetables）
    timetables_data = load_timetables(reschedule_timetables_file)
    if not timetables_data:
        print("未能加载时间表数据")
        return

    _, _, classrooms, _ = timetables_data
    print("使用已保存的时间表数据计算占有率")

    slots_per_week = days_of_week * periods_per_day
    total_slots_semester = total_weeks * slots_per_week

    # 转为 0-based 索引
    weeks_idx = [w - 1 for w in weeks_of_interest if 1 <= w <= total_weeks]
    period_ranges_idx = [(max(0, a-1), min(periods_per_day-1, b-1)) for a,b in period_ranges if a<=b]

    # 初始化列表
    overall_records = []
    period_specific_records = []
    weeks_records = []

    for classroom in classrooms:
        IFcoulduse = getattr(classroom, 'SFYXPK', '0')
        if str(IFcoulduse).strip() in ('0', '0.0'):
            continue  # 不允许排课跳过

        code = getattr(classroom, 'JASDM', '')
        name = getattr(classroom, 'JASMC', '')
        kind = getattr(classroom, 'JASLX', '')
        campus = getattr(classroom, 'MC', '')
        capacity = getattr(classroom, 'SKZWS', '')

        timetable = getattr(classroom, 'timetable', None)
        if timetable is None:
            tt_bool = np.zeros((total_weeks, days_of_week, periods_per_day), dtype=bool)
        else:
            tt = np.asarray(timetable)
            tt_bool = np.vectorize(bool)(tt) if tt.dtype == object else tt.astype(bool)

        # ---- 1) 全学期全天 ----
        used_slots_semester = int(tt_bool.sum())
        overall_usage_rate = (used_slots_semester / total_slots_semester) * 100 if total_slots_semester else 0.0
        overall_records.append({
            '教室代码': code,
            '教室名称': name,
            '教室类型': kind,
            '校区': campus,
            '上课座位数': capacity,
            '被占格数(学期)': used_slots_semester,
            '总格数(学期)': total_slots_semester,
            '学期使用率(%)': round(overall_usage_rate, 4)
        })

        # ---- 2) 指定节段 ----
        record_period = {
            '教室代码': code,
            '教室名称': name,
            '教室类型': kind,
            '校区': campus,
            '上课座位数': capacity
        }
        for a0,b0 in period_ranges_idx:
            selected = tt_bool[:, :, a0:b0+1]
            used = int(selected.sum())
            total_slots_range = total_weeks * days_of_week * (b0 - a0 + 1)
            record_period[f'{a0+1}-{b0+1}节'] = round((used / total_slots_range) * 100 if total_slots_range else 0.0, 4)
        values = [v for k,v in record_period.items() if '-' in k]
        record_period['平均使用率(%)'] = round(np.mean(values) if values else 0.0, 4)
        period_specific_records.append(record_period)

        # ---- 3) 指定周 ----
        record_week = {
            '教室代码': code,
            '教室名称': name,
            '教室类型': kind,
            '校区': campus,
            '上课座位数': capacity
        }
        for w_idx in weeks_idx:
            week_slice = tt_bool[w_idx, :, :] if w_idx < tt_bool.shape[0] else np.zeros((days_of_week, periods_per_day))
            used_week = int(week_slice.sum())
            record_week[f'第{w_idx+1}周'] = round((used_week / slots_per_week) * 100 if slots_per_week else 0.0, 4)
        week_values = [v for k,v in record_week.items() if '第' in k]
        record_week['平均使用率(%)'] = round(np.mean(week_values) if week_values else 0.0, 4)
        weeks_records.append(record_week)

    # === DataFrame ===
    df_overall = pd.DataFrame(overall_records).sort_values('教室代码').reset_index(drop=True)
    df_period_pivot = pd.DataFrame(period_specific_records).sort_values('教室代码').reset_index(drop=True)
    df_weeks_pivot = pd.DataFrame(weeks_records).sort_values('教室代码').reset_index(drop=True)

    # 汇总平均
    df_summary = pd.DataFrame([
        {'指标': '全学期全天平均使用率(%)', '值': round(df_overall['学期使用率(%)'].mean(),4)},
        {'指标': '各指定节段平均使用率(%)', '值': round(df_period_pivot['平均使用率(%)'].mean(),4)},
        {'指标': f'指定周平均使用率(%)', '值': round(df_weeks_pivot['平均使用率(%)'].mean(),4)}
    ])

    # 保存 Excel
    os.makedirs(results_dir, exist_ok=True)
    output_path = os.path.join(results_dir, output_file)
    with pd.ExcelWriter(output_path, engine='openpyxl') as writer:
        df_overall.to_excel(writer, sheet_name='学期_全时段_教室明细', index=False)
        df_period_pivot.to_excel(writer, sheet_name='教室指定时段使用率', index=False)
        df_weeks_pivot.to_excel(writer, sheet_name='教室指定周使用率', index=False)
        df_summary.to_excel(writer, sheet_name='教室汇总平均', index=False)

    print(f"教室使用率评估已导出到: {output_path}")
    return {
        'overall': df_overall,
        'period_specific': df_period_pivot,
        'weeks': df_weeks_pivot,
        'summary': df_summary,
        'output_path': output_path
    }
