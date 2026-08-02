"""
调课算法主模块（直接运行：python reschedule_by_adjusting.py）。

对首轮排课失败的教学班，通过腾挪已排课程寻找可行的时段和教室：
  - re_with_match_courses_new / re_with_match_courses_betchs_new：单班/批量调课匹配
  - reschedule：调课主流程入口
  - 模块顶部定义了基础数据和排课结果的默认文件路径

输入：排课结果/排课失败课程_全部.xlsx、saved_timetables.pkl
输出：排课结果/全部调整结果.xlsx、调课后的整体结果.xlsx、reschedule_timetables.pkl 等
"""
from Basic_Data import *
from utils1 import *
import pickle  # 添加pickle模块用于序列化和反序列化
import time  # 添加time模块用于计时
from tryloadlimit import *
from utils_for_reschedule import *


# 基础数据路径
#root_path = '智能排课基础数据'
##course_excel = os.path.join(root_path, '更新后的课程表.xlsx')
#classroom_excel = os.path.join(root_path, '更新后的教室表.xlsx')
#teacher_excel = os.path.join(root_path, '教师信息汇总.xlsx')
#banji_excel = os.path.join(root_path, '班级汇总.xlsx')

# root_path = '智能排课基础数据'
# course_excel = os.path.join(root_path, '课程表2025-2026-1.xlsx')
# classroom_excel = os.path.join(root_path, '更新后的教室表.xlsx')
# teacher_excel = os.path.join(root_path, '更新后的教师名单2025-2026-1.xlsx')
# banji_excel = os.path.join(root_path, '班级汇总2025-2026-1.xlsx')

BASE_DIR = os.path.join(os.path.dirname(__file__), '智能排课基础数据', '提取的基础数据表_converted')
course_excel = os.path.join(BASE_DIR, '课程表_split.xlsx')   # Issue #14 拆奇数 ZXS 后的副本
classroom_excel =  os.path.join(BASE_DIR, '教室表.xlsx')
teacher_excel = os.path.join(BASE_DIR, '教师表.xlsx')
banji_excel = os.path.join(BASE_DIR, '班级表.xlsx')

# 排课参数
num_weeks = 20
num_periods = 11
num_days = 7

# 结果目录
results_dir = "排课结果"
failed_courses_file = os.path.join(results_dir, "排课失败课程_全部.xlsx")
scheduled_courses_file = os.path.join(results_dir, "排课结果_全部.xlsx")
#scheduled_courses_file=os.path.join(results_dir, '最终排课结果.xlsx')


#排课结果合并
def merge_tables(file1, file2, output_file, id_col="教学班ID"):
    # 读取两个表格
    df1 = pd.read_excel(file1)
    df2 = pd.read_excel(file2)

    # 找出表格2里的所有教学班id
    ids_to_replace = set(df2[id_col])

    # 删除表格1里这些id对应的行
    df1_filtered = df1[~df1[id_col].isin(ids_to_replace)]

    # 合并：剩余的表格1 + 表格2
    df3 = pd.concat([df1_filtered, df2], ignore_index=True)

    # 输出结果
    df3.to_excel(output_file, index=False)
    print(f"合并完成，结果已保存到: {output_file}")

# 时间表保存文件
# 排课程序保存的文件（用于加载）
scheduling_timetables_file = os.path.join(results_dir, "saved_timetables.pkl")
# 调课程序保存的文件
reschedule_timetables_file = os.path.join(results_dir, "reschedule_timetables.pkl")

###从excel加载排课结果

###根据教学班ID (JXBID) 查找对应课程的上课时间和上课教室。
def get_courses_schedule_by_jxbid(course_list, candidate_jxbid):
    """
    根据教学班ID (JXBID) 查找对应课程的上课时间和上课教室。
    
    Args:
        course_list (list): 包含多个 Course 对象的列表。
        candidate_jxbid (str): 目标教学班ID (JXBID)。
    
    Returns:
        list: 包含所有符合条件的上课时间和上课教室的信息列表。
              每个元素是一个元组 (day, period, classroom)。
    """
    schedule_info = []  # 用于存储符合条件的上课时间和教室信息

    # 遍历所有课程
    classroom_jasdm=""
    for course in course_list:
        if course.JXBID == candidate_jxbid:  # 如果教学班ID匹配
            # 遍历每个周次、天、时段
            for week in range(num_weeks):
                for day in range(num_days):
                    for period in range(num_periods):
                        # 如果课程在当前时段有排课
                        #print(f"<UNK>{week}<UNK>{day}<UNK>{period}<UNK>")
                        #print(f"当前时间段安排{course.timetable[week, day, period]}")
                        if course.timetable[week, day, period] != "":
                            classroom_jasdm = course.timetable[week, day, period]
                            #print(classroom_jasdm)
                            schedule_info.append((week,day, period))  # 添加到结果列表
            break #只找到一个符合条件的课程就退出

    return schedule_info,classroom_jasdm

def find_scheduled_courses_by_room_time(courses, allowed_room_codes, preferred_options):
    """
    从内存中的 courses（含时间表）里，筛选教室在 allowed_room_codes 且上课时段落在 preferred_options 内的已排课程。
    preferred_options 现在是形如 (day, start, hours) 的元组列表，或这些元组的组合列表。
    返回去重后的 jxbid 集合。
    """
    if not allowed_room_codes or not preferred_options:
        return set()

    # 将 preferred_options 统一展开成 (day, start, end) 列表
    windows = []

    def _add_window(t):
        if not isinstance(t, (list, tuple)) or len(t) != 3:
            return
        try:
            d, st, hrs = int(t[0]), int(t[1]), int(t[2])
            ed = st + hrs - 1
            windows.append((d, st, ed))
        except Exception:
            return

    for opt in preferred_options:
        # opt 可能是单个 (day, st, hours) 或一组 [(day, st, hours), ...]
        if isinstance(opt, (list, tuple)) and len(opt) == 3 and all(isinstance(x, (int, float)) for x in opt):
            _add_window(opt)
        elif isinstance(opt, (list, tuple)):
            for t in opt:
                _add_window(t)

    if not windows:
        return set()
    windows = list(set(windows))  # 去重，元素是 (day, st, ed) 可哈希

    hit = set()
    for c in courses:
        if c.JXBID in hit:
            continue
        schedule_info, room_code = get_courses_schedule_by_jxbid(courses, c.JXBID)
        if not schedule_info or not room_code:
            continue
        if room_code not in allowed_room_codes:
            continue
        # 任一上课节次落在可用窗口即可
        for _, day, period in schedule_info:
            for d, st, ed in windows:
                if day == d and st <= period <= ed:
                    hit.add(c.JXBID)
                    break
            if c.JXBID in hit:
                break
    return hit  # 返回符合条件的教学班ID集合


def get_current_arrangements(schedule_info):
    current_arrangements = []
    # 用于按 day 分组，记录每个 day 的课程安排（去重，因为可能包含多个周次的重复数据）
    day_grouped = {}

    # 1. 按照 day 进行分组，并对 (day, period) 进行去重
    # 因为 schedule_info 可能包含多个周次的重复数据，我们需要去重
    seen_periods = set()  # 用于去重 (day, period) 组合
    for _, day, period in schedule_info:
        key = (day, period)
        if key not in seen_periods:
            seen_periods.add(key)
            if day not in day_grouped:
                day_grouped[day] = []
            day_grouped[day].append(period)
    
    # 2. 对每个 day，处理连续节次，记录起始节次和连续节次数量
    for day, periods in day_grouped.items():
        periods.sort()  # 排序，确保节次是按顺序排列的
        if not periods:  # 如果为空，跳过
            continue
        start_period = periods[0]
        hours = 1  # 初始化，默认每节课为1小时
        for i in range(1, len(periods)):
            # 如果当前节次和上一个节次连续，增加小时数
            if periods[i] == periods[i - 1] + 1:
                hours += 1
            else:
                # 如果不连续，记录之前的安排
                current_arrangements.append((day, start_period, hours))
                start_period = periods[i]  # 更新为新的起始节次
                hours = 1  # 重置小时数为1
        # 处理最后一组连续的节次
        current_arrangements.append((day, start_period, hours))
    return current_arrangements

def re_with_match_courses_new(jxbid,zxs,unique_jxbids,scheduled_df,
                          classrooms,courses,classes,teachers,classroom_map,teacher_week_map,
                          adjustment_results,rescheduled_results,restored_count,adjustment_success:False,class_ralex:False):
    # 尝试调整已排课程
    for candidate_jxbid in unique_jxbids:
        print(f"\n尝试调整课程: {candidate_jxbid}")

        ##获取当前课程对象,12.19新增
        candidate_jxbs = get_consecutive_jxbid(candidate_jxbid, courses)
        candidate_zxs = candidate_jxbs[0].ZXS #获取课程的周学时
        if not candidate_jxbs:
            print(f"未找到课程对象: {candidate_jxbid}")
            continue
        candidate_name = candidate_jxbs[0].KCM #获取课程名称
        print(f"课程名称: {candidate_name}")
        schedule_info,schedule_classroom_jasdm = get_courses_schedule_by_jxbid(courses, candidate_jxbid)
        if schedule_info and schedule_classroom_jasdm:
            print(f"找到课程: {candidate_name} 的上课时间和上课教室")
        else:
            print(f"未找到课程: {candidate_name} 的上课时间和上课教室")
            continue

        current_classroom = classroom_map.get(schedule_classroom_jasdm)
        #获取上课安排            
        current_arrangements = get_current_arrangements(schedule_info)
        #上课周次
        teaching_weeks = get_teaching_weeks(candidate_jxbs)
        

        # 获取候选课程的教师和班级列表
        clist_of_JSHs = [jxb.JSH for jxb in candidate_jxbs]
        clist_of_classes = get_class_instances1(candidate_jxbs[0], classes, courses)


        clist_of_teacher_week = [
            trans_week_flags(jxb.RWJSZCDM) if hasattr(jxb, 'RWJSZCDM') and jxb.RWJSZCDM else []
            for jxb in candidate_jxbs
        ]

        clist_of_teachers = get_teacher_instances(clist_of_JSHs, teachers)
        cteacher_week_map = {
            teacher.JSH: (teacher, weeks)
            for teacher, weeks in zip(clist_of_teachers, clist_of_teacher_week)
            if teacher is not None and weeks  # 注意这里确保周次非空才加入 map
        }  # 老师和上课周次一一对应,确保周次非空才加入 map
        # 先从时间表中移除该课程
        print(f"从时间表中临时移除课程 {candidate_jxbid}")
        clear_placement(current_classroom, current_arrangements, teaching_weeks, candidate_jxbid, candidate_jxbs, cteacher_week_map,
                        clist_of_classes)

        # 尝试为原来排课失败的课程排课
        print(f"尝试为原失败课程 {jxbid} 排课...")
        best_classroom, selected_arrangement, teaching_weeks_for_failed = schedule_class(
            jxbid, courses, teachers, classes, classrooms, flag_reschedule=True, num_days=num_days, relax_constraints=True,class_ralex=class_ralex
        )

        if best_classroom and selected_arrangement:
            print(f"成功为原失败课程 {jxbid} 排课")

            # 记录排课成功结果
            rescheduled_results.append((jxbid, best_classroom, selected_arrangement, teaching_weeks_for_failed))

            # 为被移出的课程寻找新位置（与原来位置不同）
            print(f"为被调整的匹配课程 {candidate_jxbid} 寻找新位置...")
            # 使用更灵活的排课模式
            candidate_time_constraints = build_preferences(candidate_jxbs[0].Prefer_Time,
                                                           candidate_jxbs[0].unavailable_Time)  # 这个课程的时间限制
            # 生成排课日模式
            day_patterns = new_generate_day_patterns(candidate_zxs, num_days, candidate_time_constraints, flag_reschedule=True)


            suitable_classrooms = filter_suitable_classrooms(classrooms, candidate_jxbs[0], relax_constraints=True)

            # 找出最优教室和方案（避免使用相同安排）
            found_new_arrangement = False
            best_candidate_classroom = None
            selected_candidate_arrangement = None

            for classroom in suitable_classrooms:
                # 计算当前教室的排课方案
                alternative_arrangements = []
                for pattern in day_patterns:
                    days_list, hours_list = pattern
                    # arrangements = check_period_availability(
                    # days_list, hours_list, classroom, cteacher_week_map, clist_of_classes, teaching_weeks
                    # )
                    # arrangements = new_check_period_availability(
                    #     days_list, hours_list, classroom, cteacher_week_map, clist_of_classes, teaching_weeks,
                    #     candidate_time_constraints)
                    arrangements=new_check_period_availability( days_list, hours_list, classroom, cteacher_week_map, [], teaching_weeks,
                         candidate_time_constraints)    #不检查班级

                    alternative_arrangements.extend(arrangements)

                # 找到与原来不同的排课方案
                for arrangement in alternative_arrangements:
                    # 检查与原安排是否不同,,没必要
                    is_different = True
                    # for orig_day, orig_period, orig_hours in current_arrangements:
                    #     for new_day, new_period, new_hours in arrangement:
                    #         if orig_day == new_day and orig_period == new_period and orig_hours == new_hours:
                    #             is_different = False
                    #             break
                    #     if not is_different:
                    #         break

                    if is_different:
                        best_candidate_classroom = classroom
                        selected_candidate_arrangement = arrangement
                        found_new_arrangement = True
                        break

                if found_new_arrangement:
                    break

            if found_new_arrangement:
                print(f"找到新的排课方案，在教室 {best_candidate_classroom.JASMC}")

                # 将调整的课程安排到新位置
                # place_placement(best_candidate_classroom, selected_candidate_arrangement, teaching_weeks,
                #                 candidate_jxbid, candidate_jxbs, cteacher_week_map, clist_of_classes)
                place_placement(best_candidate_classroom, selected_candidate_arrangement, teaching_weeks,
                                candidate_jxbid, candidate_jxbs, cteacher_week_map, [])
                # 记录调整结果
                adjustment_results.append(
                    (candidate_jxbid, best_candidate_classroom, selected_candidate_arrangement, teaching_weeks))
                adjustment_success = True

                print(f"成功调整匹配课程 {candidate_jxbid} 到新位置")
                # 一旦成功，跳出循环
                break
            else:
                print(f"未能找到匹配课程 {candidate_jxbid} 的替代方案")

                # 将课程放回原位置
                print(
                    f"将匹配课程 {candidate_jxbid} 放回原位置 (教室: {current_classroom.JASMC}, 时间: {', '.join([f'周{d + 1}第{p + 1}-{p + h}节' for d, p, h in current_arrangements])})")
                place_placement(current_classroom, current_arrangements, teaching_weeks,
                                candidate_jxbid,candidate_jxbs, cteacher_week_map, clist_of_classes)

                # 由于放回原位置，需要将之前排好 的失败课程也从时间表中移除
                print(
                    f"将临时排好的失败课程 {jxbid} 从时间表中移除 (教室: {best_classroom.JASMC}, 时间: {', '.join([f'周{d + 1}第{p + 1}-{p + h}节' for d, p, h in selected_arrangement])})")
                jxbs_failed = get_consecutive_jxbid(jxbid, courses)
                list_of_classes_failed = get_class_instances1(jxbs_failed[0], classes, courses)
                clear_placement(best_classroom, selected_arrangement, teaching_weeks_for_failed, jxbid,jxbs_failed,
                                teacher_week_map, list_of_classes_failed)

                # 重置失败课程记录
                rescheduled_results.pop()
                print(f"时间表已完全恢复至匹配课程 {candidate_jxbid} 临时移除之前的状态")
                restored_count += 1
        else:
            # 排课失败，将被调整的课程放回原位置
            print(
                f"排课失败，将匹配课程 {candidate_jxbid} 放回原位置 (教室: {current_classroom.JASMC}, 时间: {', '.join([f'周{d + 1}第{p + 1}-{p + h}节' for d, p, h in current_arrangements])})")
            place_placement(current_classroom, current_arrangements, teaching_weeks,
                            candidate_jxbid, candidate_jxbs,cteacher_week_map, clist_of_classes)

            print(f"时间表已完全恢复至匹配课程 {candidate_jxbid} 临时移除之前的状态")
            restored_count += 1
    return adjustment_success, restored_count

def re_with_match_courses_betchs_new(
    jxbid, zxs, unique_jxbids, scheduled_df,
    classrooms, courses, classes, teachers, classroom_map, teacher_week_map,
    adjustment_results, rescheduled_results, restored_count,
    adjustment_success: bool = False, class_ralex: bool = False
):
    """
    批量（同时）移走 unique_jxbids 中的所有候选课程 -> 给失败课程 jxbid 排课。
    若失败课程排课成功，再为每个候选逐一寻找新位置；若任意候选无法安放，则回滚到操作前的完整状态。
    返回: (adjustment_success, restored_count)
    """
    # --------- 收集候选课程原始占位快照（用于批量清空与回滚）---------
    candidates_info = []  # 每个元素: dict( jxbid, classroom, arrangements, weeks, teachers_map, classes_list )

    for candidate_jxbid in unique_jxbids:
        print(f"\n尝试调整课程: {candidate_jxbid}")

        ##获取当前课程对象,12.19新增
        candidate_jxbs = get_consecutive_jxbid(candidate_jxbid, courses)
        candidate_zxs = candidate_jxbs[0].ZXS #获取课程的周学时
        if not candidate_jxbs:
            print(f"未找到课程对象: {candidate_jxbid}")
            continue
        candidate_name = candidate_jxbs[0].KCM  #获取课程名称
        print(f"课程名称: {candidate_name}")
        schedule_info,schedule_classroom_jasdm = get_courses_schedule_by_jxbid(courses, candidate_jxbid)
        if schedule_info and schedule_classroom_jasdm:
            print(f"找到课程: {candidate_name} 的上课时间和上课教室")
        else:
            print(f"未找到课程: {candidate_name} 的上课时间和上课教室")
            continue

        current_classroom = classroom_map.get(schedule_classroom_jasdm)
        #获取上课安排            
        current_arrangements = get_current_arrangements(schedule_info)
        #上课周次
        teaching_weeks = get_teaching_weeks(candidate_jxbs)

        # 老师/班级集合
        candidate_jxbs = get_consecutive_jxbid(candidate_jxbid, courses)
        if not candidate_jxbs:
            continue
        clist_of_JSHs = [jxb.JSH for jxb in candidate_jxbs]
        clist_of_teachers = get_teacher_instances(clist_of_JSHs, teachers)
        clist_of_teacher_week = [
            trans_week_flags(jxb.RWJSZCDM) if getattr(jxb, 'RWJSZCDM', None) else []
            for jxb in candidate_jxbs
        ]
        cteacher_week_map = {
            t.JSH: (t, weeks)
            for t, weeks in zip(clist_of_teachers, clist_of_teacher_week)
            if (t is not None) and weeks
        }
        clist_of_classes = get_class_instances1(candidate_jxbs[0], classes, courses)

        candidates_info.append(dict(
            jxbid=candidate_jxbid,
            classroom=current_classroom,
            arrangements=current_arrangements,
            weeks=teaching_weeks,
            teachers_map=cteacher_week_map,
            classes_list=clist_of_classes,
            jxbs=candidate_jxbs  # 备用
        ))

    if not candidates_info:
        print("未找到可移走的候选课程，放弃本轮调课。")
        return adjustment_success, restored_count

    # --------- 第一步：批量清空所有候选课程的占位（原子）---------
    print(f"\n== 同时移走 {len(candidates_info)} 门候选课程 ==")
    for info in candidates_info:
        print(f"临时移除 {info['jxbid']}")
        clear_placement(
            info['classroom'],
            info['arrangements'],
            info['weeks'],
            info['jxbid'],
            info['jxbs'],
            info['teachers_map'],
            info['classes_list']
        )

    # --------- 第二步：尝试为失败课程排课（用最新状态）---------
    print(f"\n尝试为原失败课程 {jxbid} 排课...")
    best_classroom, selected_arrangement, teaching_weeks_for_failed = schedule_class(
        jxbid, courses, teachers, classes, classrooms,
        flag_reschedule=True, num_days=num_days, relax_constraints=True, class_ralex=class_ralex
    )

    if not (best_classroom and selected_arrangement):
        print("失败课程仍无法排课，执行回滚。")
        # 回滚：把所有候选放回原位
        for info in candidates_info:
            place_placement(
                info['classroom'],
                info['arrangements'],
                info['weeks'],
                info['jxbid'],
                info['jxbs'],
                info['teachers_map'],
                info['classes_list'],
            )
        restored_count += len(candidates_info)
        return adjustment_success, restored_count

    # 失败课排课成功
    rescheduled_results.append((jxbid, best_classroom, selected_arrangement, teaching_weeks_for_failed))
    print(f"成功为失败课程 {jxbid} 排课，开始为候选逐一找新位置...")

    # --------- 第三步：使用启发式方法为候选找新位置（按方案数从少到多排序）---------
    placed_candidates = []  # 已成功安放的新安排（用于统计与回滚）
    adj_len_before = len(adjustment_results)  # 记录进入批量安放前的调整结果长度
    
    # 3.1 计算每个候选课程的方案总数（启发式排序）
    print("\n计算每个候选课程的排课方案数...")
    candidates_with_slots = []  # 存储 (info, total_slots) 元组
    
    for info in candidates_info:
        candidate_jxbid = info['jxbid']
        candidate_jxbs = info['jxbs']
        cteacher_week_map = info['teachers_map']
        teaching_weeks = info['weeks']
        
        # 生成"新"日模式
        candidate_time_constraints = build_preferences(
            candidate_jxbs[0].Prefer_Time, candidate_jxbs[0].unavailable_Time
        )
        candidate_zxs = candidate_jxbs[0].ZXS
        day_patterns = new_generate_day_patterns(candidate_zxs, num_days, candidate_time_constraints, flag_reschedule=True)
        
        # 候选允许的教室池（宽松）
        suitable_classrooms = filter_suitable_classrooms(classrooms, candidate_jxbs[0], relax_constraints=True)
        
        # 根据 class_ralex 或课程 IF_CLASS_CONFICT 开关决定是否检查班级冲突（Issue #13）
        if class_ralex or not check_class_conflict(info['jxbs'][0]):
            re_list_of_classes = []  # 不检查班级冲突
        else:
            re_list_of_classes = info['classes_list']  # 检查班级冲突
        
        # 计算该候选课程在所有可用教室中的方案总数
        total_slots = 0
        for room in suitable_classrooms:
            for pattern in day_patterns:
                days_list, hours_list = pattern
                arrangements = new_check_period_availability(
                    days_list, hours_list, room, cteacher_week_map, re_list_of_classes, 
                    teaching_weeks, candidate_time_constraints
                )
                total_slots += len(arrangements)
        
        candidates_with_slots.append((info, total_slots))
        print(f"候选课程 {candidate_jxbid} 共有 {total_slots} 个可用方案")
    
    # 3.2 按方案数从少到多排序（方案数少的优先安排，资源最紧张）
    candidates_with_slots.sort(key=lambda x: x[1])  # 按方案数排序
    print(f"\n按方案数排序后的候选课程顺序:")
    for idx, (info, slots) in enumerate(candidates_with_slots):
        print(f"  {idx+1}. {info['jxbid']} - {slots} 个方案")
    
    # 3.3 依次为每个候选找新位置；若任一失败 -> 全量回滚
    try:
        for info, total_slots in candidates_with_slots:
            candidate_jxbid = info['jxbid']
            candidate_jxbs = info['jxbs']
            cteacher_week_map = info['teachers_map']
            current_arrangements = info['arrangements']
            teaching_weeks = info['weeks']

            # 生成"新"日模式
            candidate_time_constraints = build_preferences(
                candidate_jxbs[0].Prefer_Time, candidate_jxbs[0].unavailable_Time
            )
            candidate_zxs = candidate_jxbs[0].ZXS
            day_patterns = new_generate_day_patterns(candidate_zxs, num_days, candidate_time_constraints, flag_reschedule=True)

            # 候选允许的教室池（宽松）
            suitable_classrooms = filter_suitable_classrooms(classrooms, candidate_jxbs[0], relax_constraints=True)
            
            # 根据 class_ralex 或课程 IF_CLASS_CONFICT 开关决定是否检查班级冲突（Issue #13）
            if class_ralex or not check_class_conflict(info['jxbs'][0]):
                re_list_of_classes = []  # 不检查班级冲突
            else:
                re_list_of_classes = info['classes_list']  # 检查班级冲突

            found_new_arrangement = False
            best_candidate_classroom = None
            selected_candidate_arrangement = None

            # 遍历所有可用教室，找到第一个可用方案
            for room in suitable_classrooms:
                alternative_arrangements = []
                for pattern in day_patterns:
                    days_list, hours_list = pattern
                    arrangements = new_check_period_availability(
                        days_list, hours_list, room, cteacher_week_map, re_list_of_classes, 
                        teaching_weeks, candidate_time_constraints
                    )
                    alternative_arrangements.extend(arrangements)

                # 选择第一个可用方案
                if alternative_arrangements:
                    best_candidate_classroom = room
                    selected_candidate_arrangement = alternative_arrangements[0]
                    found_new_arrangement = True
                    break

            if not found_new_arrangement:
                # 任意一个候选找不到新位置 -> 整体回滚
                print(f"候选 {candidate_jxbid} 找不到替代方案，回滚所有操作。")
                raise RuntimeError("place-candidate-failed")

            # 找到了替代方案 -> 写入
            place_placement(
                best_candidate_classroom,
                selected_candidate_arrangement,
                teaching_weeks,
                candidate_jxbid,
                candidate_jxbs,
                cteacher_week_map,
                re_list_of_classes,
            )

            adjustment_results.append(
                (candidate_jxbid, best_candidate_classroom, selected_candidate_arrangement, teaching_weeks)
            )
            placed_candidates.append(  # 记录（用于必要时清回）
                (candidate_jxbid, best_candidate_classroom, selected_candidate_arrangement, teaching_weeks,
                 cteacher_week_map, re_list_of_classes)
            )

        # 所有候选都安放成功
        adjustment_success = True
        print("所有候选已成功安放到新位置。")

    except Exception as e:
        # --------- 回滚区：任一候选失败，撤销全部改动 ---------
        print(f"[回滚] 触发，原因：{e}")
        # 1) 移除刚刚给失败课程安排的占位
        if best_classroom and selected_arrangement:
            jxbs_failed = get_consecutive_jxbid(jxbid, courses)
            list_of_classes_failed = get_class_instances1(jxbs_failed[0], classes, courses)
            clear_placement(best_classroom, selected_arrangement, teaching_weeks_for_failed, jxbid,jxbs_failed,
                            teacher_week_map, list_of_classes_failed)
            if rescheduled_results and rescheduled_results[-1][0] == jxbid:
                rescheduled_results.pop()

        # 2) 清除已成功安放的候选新安排
        for (cid, room, arr, weeks, tmap, clslist) in reversed(placed_candidates):
            cjxbs = get_consecutive_jxbid(cid, courses)
            clear_placement(room, arr, weeks, cid, cjxbs, tmap, clslist)
        # 同步回滚 adjustment_results 中本轮新增的记录
        if len(adjustment_results) > adj_len_before:
            adjustment_results[:] = adjustment_results[:adj_len_before]

        # 3) 把所有候选放回原位
        for info in candidates_info:
            place_placement(
                info['classroom'],
                info['arrangements'],
                info['weeks'],
                info['jxbid'],
                info['jxbs'],
                info['teachers_map'],
                info['classes_list']
            )

        restored_count += len(candidates_info)
        adjustment_success = False

    return adjustment_success, restored_count




#该调课思路为，找到匹配课程，然后移走匹配课程，再排失败课程，然后修改匹配课程的学时划分，重新给匹配课程排课
#筛选上课老师相同的课程
def reschedule(timetables_data):
    """通过调整周学时匹配的已排课程，为失败课程腾出资源"""
    # 记录开始时间
    start_time = time.time()
    
    print("\n=== 开始通过周学时匹配调课为排课失败的课程腾出资源 ===")
    
    # 尝试加载保存的时间表
    #timetables_data = load_timetables(scheduling_timetables_file)
    
    # 如果成功加载时间表，直接使用加载的数据
    if timetables_data:
        courses, teachers, classrooms, classes = timetables_data
        print("使用已保存的时间表数据进行调课")
        if courses is None:
            print("警告：从pkl文件加载的courses为None，将从Excel重新加载")
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
        scheduled_df = load_scheduled_courses() #excel数据
        if scheduled_df is not None:
            rebuild_timetables_from_scheduled_courses(scheduled_df, teachers, classrooms, classes, courses)
    
    # 加载排课失败的课程
    failed_df = load_failed_courses()
    total_failed_courses = len(failed_df)
    if failed_df is None:
        return
    
    # 加载已排课的课程
    scheduled_df = load_scheduled_courses()
    if scheduled_df is None:
        return
    
    # 检查已排课程是否包含班级信息列，如果没有则添加空列
    if '班级信息' not in scheduled_df.columns:
        scheduled_df['班级信息'] = ""
        print("已排课表中没有班级信息列，已添加空列")
    
    # 调课结果记录
    adjustment_results = []  # 记录调整的已排课程
    rescheduled_results = []  # 记录本次排课成功的 失败课程
    still_failed = []  # 记录仍然失败的课程
    restored_count = 0  # 记录尝试调整后恢复原状态的课程数
    
    # 建立教室代码到教室对象的映射字典，提高查找效率
    classroom_map = {str(classroom.JASDM): classroom for classroom in classrooms}
    
    # 按原顺序处理失败课程（直接使用failed_df，不进行排序）
    for _, row in failed_df.iterrows():
        jxbid = row['教学班ID']
        course_name = row['课程名称']
        print(f"\n===== 处理失败课程: {jxbid} ({course_name}) =====")
        
        # 获取课程对象
        jxbs = get_consecutive_jxbid(jxbid, courses)
        if not jxbs:
            print(f"错误：未找到教学班 {jxbid}")
            continue
        
        # 获取课程的周学时
        zxs = jxbs[0].ZXS if hasattr(jxbs[0], 'ZXS') else 0
        # zxs = math.ceil(zxs)
        print(f"课程周学时: {zxs}")
        
        # 获取课程的教学周次
        teaching_weeks_failed = get_teaching_weeks(jxbs)
        if not teaching_weeks_failed:
            print(f"警告：课程 {jxbid} 的教学周次为空，无法进行教学周次匹配")
            still_failed.append({"jxbid": jxbid, "name": course_name})
            continue
            
        print(f"课程教学周次: {','.join(str(w+1) for w in teaching_weeks_failed)}")

        # 获取任课教师列表
        list_of_JSHs = [jxb.JSH for jxb in jxbs]  # 该教学班对应的所有上课教师
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

        # 先尝试直接排课（不调整其他课程），放宽班级要求
        print("尝试直接排课...")

        best_classroom, selected_arrangement, teaching_weeks = schedule_class(
            jxbid, courses, teachers, classes, classrooms, flag_reschedule=True,num_days=num_days, relax_constraints=True,class_ralex=False
        )
        
        # 如果直接排课成功，记录结果并继续下一个
        if best_classroom and selected_arrangement:
            rescheduled_results.append((jxbid, best_classroom, selected_arrangement, teaching_weeks))
            print(f"直接排课成功: {jxbid}")
            continue
        #找出该课程可用的教室,筛选要移走的课程
        suitable_classrooms1 = filter_suitable_classrooms(classrooms, jxbs[0], relax_constraints=True)
        classroom_map_for_sheduled = {c.JASDM: c for c in suitable_classrooms1}  #为了后续高效查找

        # 找出所有具有相同周学时和相同授课周次的已排课程
        matching_courses = select_candidates(scheduled_df, zxs,
                                             teaching_weeks_failed, classroom_map_for_sheduled, teacher_week_map,
                                             True, True, True, False)
        # 计算需要移走的最大课程数量
       #max_len = max(len(matching_courses), 100)
        if matching_courses.empty:
            print(f"没有找到周学时为 {zxs} 且教学周次、教室匹配的已排课程可调整")
            #still_failed.append({"jxbid": jxbid, "name": course_name})
            #continue
            matching_courses = select_candidates(scheduled_df, zxs,
                                                 teaching_weeks_failed, classroom_map_for_sheduled, teacher_week_map,
                                                 False, True, True, False)
            if matching_courses.empty:
                print(f"没有找到教学周次、教室匹配的已排课程可调整")
                still_failed.append({"jxbid": jxbid, "name": course_name})
                continue

            print(f"找到 {len(matching_courses)} 个教学周次、教室匹配的已排课程")
        else:
            print(f"找到 {len(matching_courses)} 个周学时为 {zxs} 且教学周次、教室匹配的已排课程")
        #max_len = max(len(matching_courses), 100)
        # 记录是否成功调课
        adjustment_success = False

    
        # 按教学班ID分组，确保每个教学班只处理一次
        unique_jxbids = matching_courses['教学班ID'].unique()
        max_len = min(len(unique_jxbids), 50)
        # 只取前 max_len 个教学班ID
        unique_jxbids = unique_jxbids[:max_len]
        
        # 尝试调整已排课程
        adjustment_success, restored_count=re_with_match_courses_new(jxbid,zxs,unique_jxbids,scheduled_df,
                          classrooms,courses,classes,teachers,classroom_map,teacher_week_map,
                          adjustment_results,rescheduled_results,restored_count,adjustment_success,class_ralex=False)


        # 如果尝试完所有课程后仍未成功排课
        if not adjustment_success:
            #still_failed.append({"jxbid": jxbid, "name": course_name})
            print(f"尝试调整所有周学时匹配的课程后，课程 {jxbid} 仍无法排课，放宽周学时匹配的条件且不检查班级冲突。")
            best_classroom, selected_arrangement, teaching_weeks = schedule_class(
                jxbid, courses, teachers, classes, classrooms, flag_reschedule=True, num_days=num_days,
                relax_constraints=True,class_ralex=True
            )

            # 如果直接排课成功，记录结果并继续下一个排课
            if best_classroom and selected_arrangement:
                rescheduled_results.append((jxbid, best_classroom, selected_arrangement, teaching_weeks))
                print(f"直接排课成功: {jxbid}")
                continue
            #否则逐个排课
            # 找出所有具有相同授课周次的已排课程
            # 找出所有具有相同周学时和相同授课周次的已排课程
            matching_courses = select_candidates(scheduled_df, zxs,
                                                 teaching_weeks_failed, classroom_map_for_sheduled, teacher_week_map,
                                                 False, True, True, False)

            if matching_courses.empty:
                print(f"没有找到教学周次、教室匹配的已排课程可调整")
                still_failed.append({"jxbid": jxbid, "name": course_name})
                continue
            #max_len = max(len(matching_courses), 100)
            print(f"找到 {len(matching_courses)} 个教学周次、教室匹配的已排课程")

            # 按教学班ID分组，确保每个教学班只处理一次
            unique_jxbids = matching_courses['教学班ID'].unique()
            max_len = min(len(unique_jxbids), 50)
            # 只取前 max_len 个教学班ID
            unique_jxbids = unique_jxbids[:max_len]

            # 尝试调整已排课程
            adjustment_success, restored_count = re_with_match_courses_new(jxbid, zxs, unique_jxbids, scheduled_df,
                                                                       classrooms, courses, classes, teachers,
                                                                      classroom_map, teacher_week_map,
                                                                       adjustment_results, rescheduled_results,
                                                                       restored_count, adjustment_success,class_ralex=True)
        if not adjustment_success:
            # 新增：基于课程时间表的“教室+可用时段”筛选，批量移走
            print("尝试基于课程时间表的“教室+可用时段”筛选，批量移走课程调课")
            time_constraints_failed = build_preferences(jxbs[0].Prefer_Time, jxbs[0].unavailable_Time)
            day_patterns_failed = new_generate_day_patterns(zxs, num_days, time_constraints_failed,flag_reschedule=True)
            arrangement_failed=[]
            for pattern in day_patterns_failed:
                days_list_failed, hours_list_failed = pattern
                arr_failed = new_only_time(days_list_failed, hours_list_failed,time_constraints_failed)
                arrangement_failed.extend(arr_failed)
            room_codes_allowed = {c.JASDM for c in suitable_classrooms1}
            candidates_jxbids_rt = find_scheduled_courses_by_room_time(
                courses, room_codes_allowed, arrangement_failed
            )
            if candidates_jxbids_rt:
                max_len = min(len(candidates_jxbids_rt), 20)
                unique_jxbids_rt = list(candidates_jxbids_rt)[:max_len]
                adj_rt, restored_count = re_with_match_courses_betchs_new(
                    jxbid, zxs, unique_jxbids_rt, scheduled_df,
                    classrooms, courses, classes, teachers, classroom_map, teacher_week_map,
                    adjustment_results, rescheduled_results, restored_count,
                    adjustment_success, class_ralex=True
                )
                if adj_rt:
                    adjustment_success = True
                    print("基于课程时间表的“教室+可用时段”筛选，批量移走课程调课成功")
            else:
                print("没有找到符合教室和可用时段的已排课程")

        
        if not adjustment_success:
            print(f"尝试调整所有教室匹配的课程后，课程 {jxbid} 仍无法排课，放宽教室匹配的条件，限制教师匹配，此时不用周次匹配。")
            # 前面已经放宽过班级了，直接逐个排课
            # 找出所有具有相同授课周次的已排课程
            # 找出所有具有相同周学时和相同授课周次的已排课程
            matching_courses = select_candidates(scheduled_df, zxs,
                                                 teaching_weeks_failed, classroom_map_for_sheduled, teacher_week_map,
                                                 False, False, False, True)

            if matching_courses.empty:
                print(f"没有找到教师匹配的已排课程可调整")
                still_failed.append({"jxbid": jxbid, "name": course_name})
                continue

            print(f"找到 {len(matching_courses)} 个教师匹配的已排课程")

            # 按教学班ID分组，确保每个教学班只处理一次
            unique_jxbids = matching_courses['教学班ID'].unique()
            max_len = min(len(unique_jxbids), 50)
            # 只取前 max_len 个教学班ID
            unique_jxbids = unique_jxbids[:max_len]

            # 尝试调整已排课程
            adjustment_success, restored_count = re_with_match_courses_betchs_new(jxbid, zxs, unique_jxbids, scheduled_df,
                                                                       classrooms, courses, classes, teachers,
                                                                      classroom_map, teacher_week_map,
                                                                     adjustment_results, rescheduled_results,
                                                                       restored_count, adjustment_success,class_ralex=True)
        ##如果还不成功就记录在失败课程里
        if not adjustment_success:
            still_failed.append({"jxbid": jxbid, "name": course_name})

    # 保存调课和重新排课结果
    all_results = adjustment_results + rescheduled_results
    if all_results:
        output_file = os.path.join(results_dir, "全部调整结果.xlsx")
        save_scheduling_results(all_results, courses, output_file)
        print(f"\n成功调整和重排 {len(all_results)} 门课程，结果已保存至 {output_file}")
        
        if adjustment_results:
            adjustment_file = os.path.join(results_dir, "调课时调整的已排课程.xlsx") 
            save_scheduling_results(adjustment_results, courses, adjustment_file)
            print(f"其中调整了 {len(adjustment_results)} 门已排课程，结果已保存至 {adjustment_file}")
        
        if rescheduled_results:
            rescheduled_file = os.path.join(results_dir, "调课成功的排课失败课程.xlsx")
            save_scheduling_results(rescheduled_results, courses, rescheduled_file)
            print(f"成功为 {len(rescheduled_results)} 门失败课程重新排课，结果已保存至 {rescheduled_file}")
    
    # ========== 修复末段子阶段：最小偏离软偏好修复（Issue：偏好超订的体育类等）==========
    # 对穷尽常规松弛后仍失败、且属于"资源超订"(偏好格被同教师同偏好班占死)的 ZXS<=2 课，
    # 在守住全部硬约束(教师/班级/教室/禁排/校级)的前提下，按"最小偏离"挪到最近可行格。
    # 不丢偏好：偏好仍是主约束，仅对物理无解者做最小让步。异常隔离，绝不影响上面已得结果。
    try:
        from scripts.soft_preference_repair import soft_preference_repair, _expand_rows
        sf_ids = [str(d.get('jxbid') or d.get('教学班ID')) for d in still_failed] if still_failed else []
        if sf_ids:
            print("\n[软偏好修复] 对仍失败课程做最小偏离软偏好落子 ...")
            rescued, sf_rest = soft_preference_repair(
                courses, teachers, classes, classrooms, sf_ids, verbose=True)
            if rescued:
                _by_id = {}
                for _c in courses:
                    _by_id.setdefault(_c.JXBID, []).append(_c)
                det = _expand_rows(rescued, _by_id)
                det.to_excel(os.path.join(results_dir, "软偏好修复明细.xlsx"), index=False)
                # 更新 still_failed：剔除已救回
                _rescued_ids = {r['jxbid'] for r in rescued}
                still_failed = [d for d in still_failed
                                if str(d.get('jxbid') or d.get('教学班ID')) not in _rescued_ids]
                print(f"[软偏好修复] 救回 {len(rescued)} 门，剩余真失败 {len(still_failed)} 门")
    except Exception as _e:
        print(f"[软偏好修复] 跳过（不影响主结果）：{_e}")

    # 保存仍然失败的课程
    if still_failed:
        failed_output = os.path.join(results_dir, "调课失败的排课失败课程.xlsx")
        failed_df = pd.DataFrame(still_failed)
        failed_df.to_excel(failed_output, index=False)
        print(f"仍有 {len(still_failed)} 门课程无法排课，信息已保存至 {failed_output}")

    # 保存调课后更新的时间表（含软偏好修复落子）
    save_timetables(courses, teachers, classrooms, classes,reschedule_timetables_file)
    
    # 生成调课结果汇总报告
    print("\n" + "="*80)
    print("调课结果汇总报告（上课周次条件：已排课程的周次完全覆盖失败课程的周次）")
    print("="*80)
    
    # 1. 汇总仍然无法排课的课程
    if still_failed:
        print("\n【仍然无法排课的课程】")
        for i, course in enumerate(still_failed):
            print(f"{i+1}. {course['jxbid']} - {course['name']}")
    else:
        print("\n【仍然无法排课的课程】：无")
    
    # 2. 汇总调整的已排课程信息
    if adjustment_results:
        print("\n【被调整的已排课程】")
        for i, (jxbid, classroom, arrangement, weeks) in enumerate(adjustment_results):
            # 获取课程信息
            jxbs = get_consecutive_jxbid(jxbid, courses)
            course_name = jxbs[0].KCM if jxbs and hasattr(jxbs[0], 'KCM') else "未知课程"
            
            # 获取原始安排
            original_records = scheduled_df[scheduled_df['教学班ID'] == jxbid]
            original_room = original_records.iloc[0]['教室'] if not original_records.empty else "未知教室"
            original_room_code = original_records.iloc[0]['教室代码'] if not original_records.empty else "未知"
            
            # 获取原始时间安排
            original_time = []
            for _, record in original_records.drop_duplicates(subset=['星期', '节次']).iterrows():
                original_time.append(f"{record['星期']} {record['节次']}")
            
            # 获取新安排
            new_room = classroom.JASMC if hasattr(classroom, 'JASMC') else "未知教室"
            new_time = []
            for day, period, hours in arrangement:
                day_name = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][day]
                period_range = f"第{period+1}-{period+hours}节"
                new_time.append(f"{day_name} {period_range}")
            
            # 打印调整信息
            print(f"{i+1}. {jxbid} - {course_name}")
            print(f"   原安排: {original_room}({original_room_code}) {', '.join(original_time)}")
            print(f"   新安排: {new_room}({classroom.JASDM}) {', '.join(new_time)}")
    else:
        print("\n【被调整的已排课程】：无")
    
    # 3. 汇总重新排课的失败课程信息
    if rescheduled_results:
        print("\n【成功排课的失败课程】")
        for i, (jxbid, classroom, arrangement, weeks) in enumerate(rescheduled_results):
            # 获取课程信息
            jxbs = get_consecutive_jxbid(jxbid, courses)
            course_name = jxbs[0].KCM if jxbs and hasattr(jxbs[0], 'KCM') else "未知课程"
            
            # 获取安排
            room = classroom.JASMC if hasattr(classroom, 'JASMC') else "未知教室"
            time_slots = []
            for day, period, hours in arrangement:
                day_name = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][day]
                period_range = f"第{period+1}-{period+hours}节"
                time_slots.append(f"{day_name} {period_range}")
            
            # 周次
            weeks_str = ", ".join([str(w+1) for w in weeks])
            
            # 打印安排信息
            print(f"{i+1}. {jxbid} - {course_name}")
            print(f"   安排: {room}({classroom.JASDM}) {', '.join(time_slots)}")
            print(f"   周次: {weeks_str}")
    else:
        print("\n【成功排课的失败课程】：无")
    
    # 4. 总结数据
    print("\n【调课统计】")
    print(f"- 处理的失败课程总数: {total_failed_courses}")
    print(f"- 成功调整的已排课程数: {len(adjustment_results)}")
    print(f"- 成功重新排课的失败课程数: {len(rescheduled_results)}")
    print(f"- 仍然无法排课的课程数: {len(still_failed)}")
    print(f"- 尝试调整后恢复原状态的匹配课程数: {restored_count}")
    
    success_rate = len(rescheduled_results) / total_failed_courses * 100 if total_failed_courses > 0 else 0
    print(f"- 调课成功率: {success_rate:.2f}%")
    
    # 计算并显示运行时间
    end_time = time.time()
    elapsed_time = end_time - start_time
    minutes, seconds = divmod(elapsed_time, 60)
    print(f"- 调课总耗时: {int(minutes)}分{seconds:.2f}秒")
    print("="*80)
    output_file_1=os.path.join(results_dir, '调课后的整体结果.xlsx')
    merge_tables(scheduled_courses_file, output_file,output_file_1)
    print(f"调课后的整体结果已保存至 {output_file}")




if __name__ == "__main__":
    # 运行基于周学时和授课周次匹配的调课逻辑
    # 尝试加载保存的时间表
    timetables_data = load_timetables(scheduling_timetables_file)
    reschedule(timetables_data)
    #compute_classroom_usage_rates_new(num_weeks,num_days,num_periods,reschedule_timetables_file,results_dir,output_file='教室排课率统计.xlsx')
    #compute_class_usage_rates(num_weeks, num_days, num_periods,reschedule_timetables_file, results_dir,output_file='班级排课率统计.xlsx')
