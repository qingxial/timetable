from Basic_Data import *
import os
from utils1 import *
import time
import pandas as pd
import numpy as np
import shutil
import pickle  # 添加pickle模块用于序列化和反序列化
from reschedule_by_adjusting import *
from tryloadlimit import *


# 基础数据路径
# root_path = '智能排课基础数据'
#course_excel = os.path.join(root_path, '更新后的课程表.xlsx')
# classroom_excel = os.path.join(root_path, '更新后的教室表.xlsx')
# teacher_excel = os.path.join(root_path, '教师信息汇总.xlsx')
#banji_excel = os.path.join(root_path, '班级汇总.xlsx')

# 基础数据路径
root_path = '智能排课基础数据'
course_excel = os.path.join(root_path, '课程表2025-2026-1.xlsx')
#course_excel = os.path.join(root_path, '测试课程表.xlsx')
classroom_excel = os.path.join(root_path, '更新后的教室表.xlsx')
teacher_excel = os.path.join(root_path, '更新后的教师名单2025-2026-1.xlsx')
banji_excel = os.path.join(root_path, '班级汇总2025-2026-1.xlsx')

# 排课参数
num_weeks = 17
num_periods = 11     ##一天12节课
num_days = 7    #一周7天，要想办法限制默认将课程排在工作日内

# 创建结果保存目录
results_dir = "排课结果"
if not os.path.exists(results_dir):
    os.makedirs(results_dir)
    print(f"创建目录: {results_dir}")

# 时间表保存文件
timetables_file = os.path.join(results_dir, "saved_timetables.pkl")



# 加载基础数据
teachers = load_teachers(teacher_excel, weeks=num_weeks, days=num_days, periods=num_periods)
classrooms = load_classrooms(classroom_excel, weeks=num_weeks, days=num_days, periods=num_periods)
courses = load_courses(course_excel, weeks=num_weeks, days=num_days, periods=num_periods)
classes = load_classes(banji_excel, weeks=num_weeks, days=num_days, periods=num_periods)

# 加载筛选条件组合
try:
    combinations_df = pd.read_excel('筛选条件组合.xlsx')
    print(f"成功加载{len(combinations_df)}个筛选条件组合")
except Exception as e:
    print(f"加载筛选条件组合失败: {e}")
    print("将使用默认组合进行排课")
    combinations_df = pd.DataFrame([{'开课单位': None, '课程类别': None, '上课校区': None}])#不使用任何条件进行筛选

# 初始化总结果
all_scheduling_results = []
all_failed_courses = []
total_scheduled_count = 0
grand_total_start_time = time.time()

# 创建去重的组合字典，只按开课单位和课程类别划分..
print("正在处理筛选条件组合（只按开课单位和课程类别划分）...")
unique_combinations = {}

for idx, combination in combinations_df.iterrows():
    #把原始表格里的“空白”或“缺失”统一转换成 None
    department = combination['开课单位'] if pd.notna(combination['开课单位']) else None
    course_type = combination['课程类别'] if pd.notna(combination['课程类别']) else None
    
    # 使用开课单位和课程类别作为唯一标识
    key = (department, course_type)
    
    # 如果这个组合还没有被处理过
    if key not in unique_combinations:
        unique_combinations[key] = {
            'department': department,
            'course_type': course_type,
            'combination_name': f"{department or '全部'}_{course_type or '全部'}"
        }

# 转换为列表格式进行处理
valid_combinations = [(key, value) for key, value in unique_combinations.items()]

print(f"去重后有效组合数: {len(valid_combinations)}")
print("注意：已忽略上课校区，因为不同校区的课程不会产生冲突")

# 对每个有效组合进行排课
for combo_idx, (key, combination_info) in enumerate(valid_combinations):
    department = combination_info['department']
    course_type = combination_info['course_type']
    combination_name = combination_info['combination_name']
    
    print("\n" + "="*80)
    print(f"组合 {combo_idx+1}/{len(valid_combinations)}: 开课单位='{department}', 课程类别='{course_type}'")
    print("="*80)
    
    # 初步筛选出该组合的教学班和教室（校区设为None）
    jxbid_dict, jasdm_dict, list_of_jas = pre_selection(courses, classrooms, course_type, department, None)
    
    # 列表化所有待排教学班的ID
    list_of_jxbids = list(jxbid_dict.keys())
    if not list_of_jxbids:
        print(f"该组合下没有符合条件的教学班，跳过")
        continue
    
    print(f"该组合下待排教学班数量: {len(list_of_jxbids)}")
    
    # 确认有教学班后再创建组合对应的子目录
    combination_dir = os.path.join(results_dir, combination_name.replace('/', '_'))
    if not os.path.exists(combination_dir):
        os.makedirs(combination_dir)
        print(f"创建目录: {combination_dir}")
    
    # 该组合的结果列表
    combination_results = []
    combination_failed = []
    
    # 记录该组合开始时间
    combination_start_time = time.time()
    
    # 如果教学班数量超过50，分批次处理
    batch_size = 50 #分批次处理
    if len(list_of_jxbids) > batch_size:
        # 计算批次数
        num_batches = (len(list_of_jxbids) + batch_size - 1) // batch_size
        print(f"教学班数量超过{batch_size}，将分{num_batches}批次处理")
        
        # 将教学班列表分成多个批次
        jxbid_batches = [list_of_jxbids[i*batch_size:(i+1)*batch_size] for i in range(num_batches)]
    else:
        # 只有一个批次
        jxbid_batches = [list_of_jxbids]
        
    # 对每个批次进行排课
    batch_scheduled_count = 0
    
    for batch_idx, batch_jxbids in enumerate(jxbid_batches):
        print(f"\n----- 批次 {batch_idx+1}/{len(jxbid_batches)} -----")
        print(f"本批次教学班数量: {len(batch_jxbids)}")
        
        # 当前批次待排课程列表(复制一份以便修改)
        current_jxbids = batch_jxbids.copy()
        
        # 循环排课直到当前批次的所有课程排完或无法继续排课
        while current_jxbids:
            print(f"\n------ 第 {batch_scheduled_count + 1} 轮排课 ------")
            print(f"剩余待排教学班数量: {len(current_jxbids)}")
            
            # 调用一轮排课函数，指的是为该批次中剩余资源最少的课程排课
            iteration_start_time = time.time()
            success, jxbid, classroom, arrangement, weeks = schedule_one_round(
                current_jxbids, courses, teachers, classes, list_of_jas, jasdm_dict, num_days
            )
            iteration_end_time = time.time()
            
            print(f"本轮排课耗时: {iteration_end_time - iteration_start_time:.2f}秒")
            
            if success:
                # 更新已排课数
                batch_scheduled_count += 1
                total_scheduled_count += 1
                print(f"成功为教学班 {jxbid} 排课")
                
                # 记录排课结果
                combination_results.append((jxbid, classroom, arrangement, weeks))
                all_scheduling_results.append((jxbid, classroom, arrangement, weeks))
                
                # 从待排课列表中删除已排课的教学班
                if jxbid in current_jxbids:
                    current_jxbids.remove(jxbid)
            else:
                print(f"教学班 {jxbid} 排课失败或无可排课程")
                
                # 如果有特定教学班但排课失败，记录信息
                if jxbid and jxbid in current_jxbids:
                    # 获取课程详细信息
                    jxb_objects = get_consecutive_jxbid(jxbid, courses)
                    if jxb_objects:
                        course_name = jxb_objects[0].KCM
                        perfer_time = jxb_objects[0].Prefer_Time
                        unv_time=jxb_objects[0].unavailable_Time
                        combination_failed.append((jxbid, course_name, perfer_time, unv_time))
                        all_failed_courses.append((jxbid, course_name, department, course_type,perfer_time, unv_time))
                    else:
                        combination_failed.append((jxbid, "未知课程"))
                        all_failed_courses.append((jxbid, "未知课程", department, course_type))
                    
                    # 从待排课列表中移除
                    current_jxbids.remove(jxbid)
                
                # 如果没有找到可排课程，将剩余所有课程记录为失败并退出当前批次
                if not jxbid:
                    print("无法确定下一个要排的课程，本批次排课过程终止")
                    # 记录所有剩余课程为失败课程
                    for remaining_jxbid in current_jxbids:
                        # 获取课程详细信息
                        jxb_objects = get_consecutive_jxbid(remaining_jxbid, courses)
                        if jxb_objects:
                            course_name = jxb_objects[0].KCM
                            perfer_time = jxb_objects[0].Prefer_Time
                            unv_time = jxb_objects[0].unavailable_Time
                            combination_failed.append((remaining_jxbid, course_name, perfer_time, unv_time))
                            all_failed_courses.append((remaining_jxbid, course_name, department, course_type, perfer_time, unv_time))
                        else:
                            combination_failed.append((remaining_jxbid, "未知课程"))
                            all_failed_courses.append((remaining_jxbid, "未知课程", department, course_type))
                    # 退出当前批次
                    break
    
    # 该组合排课结束，计算耗时
    combination_end_time = time.time()
    combination_time = combination_end_time - combination_start_time
    
    print(f"\n---- 组合排课结束 ----")
    print(f"组合: 开课单位='{department}', 课程类别='{course_type}'")
    print(f"共排课 {batch_scheduled_count} 门")
    print(f"耗时: {combination_time:.2f} 秒")
    if batch_scheduled_count > 0:
        print(f"平均每个教学班耗时: {combination_time/batch_scheduled_count:.2f} 秒")
    
    # 保存该组合的排课结果
    if combination_results:
        # 保存到组合目录
        filename = os.path.join(combination_dir, "排课结果.xlsx")
        save_scheduling_results(combination_results, courses, filename)
        print(f"已将该组合排课结果保存至'{filename}'")
        
        # 保存失败课程
        if combination_failed:
            failed_filename = os.path.join(combination_dir, "排课失败课程.xlsx")
            failed_df = pd.DataFrame(combination_failed, columns=["教学班ID", "课程名称","偏好排课时间","避免排课时间"])
            failed_df.to_excel(failed_filename, index=False)
            print(f"已将该组合排课失败信息保存至'{failed_filename}'")

# 全部组合排课结束
grand_total_end_time = time.time()
grand_total_time = grand_total_end_time - grand_total_start_time

print("\n" + "="*80)
print(f"全部组合排课结束")
print(f"共排课 {total_scheduled_count} 门")
print(f"总耗时: {grand_total_time:.2f} 秒")
if total_scheduled_count > 0:
    print(f"平均每个教学班耗时: {grand_total_time/total_scheduled_count:.2f} 秒")

# 统计排课失败信息
print(f"\n==== 排课失败信息 ====")
print(f"排课失败课程数: {len(all_failed_courses)}")

# 保存全部排课结果
if all_scheduling_results:
    overall_filename = os.path.join(results_dir, "排课结果_全部.xlsx")
    save_scheduling_results(all_scheduling_results, courses, overall_filename)
    print(f"已将全部排课结果保存至'{overall_filename}'")

# 保存排课失败信息到文件
if all_failed_courses:
    failed_filename = os.path.join(results_dir, "排课失败课程_全部.xlsx")
    failed_df = pd.DataFrame(all_failed_courses, 
                           columns=["教学班ID", "课程名称", "开课单位", "课程类别","偏好排课时间","避免排课时间"])
    failed_df.to_excel(failed_filename, index=False)
    print(f"已将排课失败信息保存至'{failed_filename}'")

# 保存完整的时间表数据，用于调课
print("\n=== 保存排课结果数据 ===")
save_timetables(courses, teachers, classrooms, classes,timetables_file)
# try:
#     # 保存完整的对象数据
#     with open(timetables_file, 'wb') as f:
#         pickle.dump({
#             'courses':courses,
#             'teachers': teachers,
#             'classrooms': classrooms,
#             'classes': classes
#         }, f)
#     print(f"所有对象数据已保存至 {timetables_file}")
# except Exception as e:
#     print(f"保存对象数据出错: {e}")
    
#     # 如果保存完整对象失败，尝试只保存时间表数据作为备份
#     try:
#         backup_file = os.path.join(results_dir, "timetables_backup.pkl")
#         timetable_data = {
#             'courses': {course.JXBID: course.timetable for course in courses},
#             'teachers': {teacher.JSH: teacher.timetable for teacher in teachers},
#             'classrooms': {classroom.JASDM: classroom.timetable for classroom in classrooms},
#             'classes': {class_obj.BJMC: class_obj.timetable for class_obj in classes}
#         }
#         with open(backup_file, 'wb') as f:
#             pickle.dump(timetable_data, f)
#         print(f"保存完整对象失败，但已成功保存时间表数据至备份文件 {backup_file}")
#     except Exception as backup_error:
#         print(f"备份时间表数据也失败: {backup_error}")
#         print("调课时将需要重建时间表")

###调课
timetables_data = load_timetables(scheduling_timetables_file)
reschedule(timetables_data)
#compute_classroom_usage_rates(num_weeks, num_days, num_periods, output_file='教室排课率统计.xlsx')
#compute_class_usage_rates(num_weeks, num_days, num_periods, output_file='班级排课率统计.xlsx')
