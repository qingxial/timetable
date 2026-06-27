"""
首轮排课主入口（直接运行：python test_for_school.py）。

流程：按「筛选条件组合」分组 → 课程聚类分批 → 调用 utils1.schedule_one_round
逐批排课 → 保存排课结果和时间表状态。

输出：排课结果/排课结果_全部.xlsx、排课失败课程_全部.xlsx、saved_timetables.pkl

放置策略（消融实验开关，详见 utils1.py 顶部说明）：
  python test_for_school.py                      # first：原始方案（默认）
  python test_for_school.py --strategy balanced  # 负载均衡放置
  python test_for_school.py --strategy random    # GRASP 式随机放置
"""
import argparse
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


def load_clustering_results(clustering_file, list_of_jxbids):
    """
    加载聚类结果Excel文件，并按类别分组教学班
    
    Args:
        clustering_file: 聚类结果Excel文件路径
        list_of_jxbids: 筛选后的教学班ID列表（集合或列表）
    
    Returns:
        cluster_groups: 字典，键为类别名，值为该类别下的教学班ID列表
    """
    try:
        clustering_df = pd.read_excel(clustering_file)
        print(f"成功加载聚类结果，共{len(clustering_df)}条记录")
        
        # 将list_of_jxbids转换为集合以提高查找效率
        if isinstance(list_of_jxbids, list):
            list_of_jxbids_set = set(list_of_jxbids)
        else:
            list_of_jxbids_set = set(list_of_jxbids)
        
        # 创建聚类类别到教学班ID列表的映射
        cluster_groups = {}
        matched_jxbids = set()  # 记录已匹配的教学班ID
        
        # Issue #14：聚类文件里只有原 JXBID，但 list_of_jxbids 含 @W/@B 后缀。
        # 建一个 base→all_variants 索引，匹配时把所有变体都加入同一聚类
        from collections import defaultdict
        base2variants = defaultdict(list)
        for jx in list_of_jxbids_set:
            base = str(jx).split("@", 1)[0]
            base2variants[base].append(jx)

        for _, row in clustering_df.iterrows():
            jxbid = row['JXBID']
            cluster_name = row['类别名称']
            #cluster_id=row['类别ID']
            cluster_name = row['类别ID']

            # 匹配所有变体（原 JXBID 或带 @W/@B 后缀的虚班）
            base = str(jxbid).split("@", 1)[0]
            variants = base2variants.get(base, [])
            if variants:
                if cluster_name not in cluster_groups:
                    cluster_groups[cluster_name] = []
                for v in variants:
                    if v not in matched_jxbids:
                        cluster_groups[cluster_name].append(v)
                        matched_jxbids.add(v)
        
        # 统计未匹配的教学班
        unmatched_jxbids = list_of_jxbids_set - matched_jxbids
        if unmatched_jxbids:
            print(f"警告：有{len(unmatched_jxbids)}个筛选后的教学班不在聚类结果中，将添加到'未分类'类别")
            cluster_groups['未分类'] = list(unmatched_jxbids)
        
        # 统计聚类结果中但不在筛选列表中的教学班
        clustering_jxbids_set = set(clustering_df['JXBID'].tolist())
        extra_jxbids = clustering_jxbids_set - list_of_jxbids_set
        if extra_jxbids:
            print(f"提示：聚类结果中有{len(extra_jxbids)}个教学班不在筛选后的列表中（这些教学班将被忽略）")
        
        print(f"聚类结果中包含{len(cluster_groups)}个类别")
        total_in_clusters = sum(len(jxbids) for jxbids in cluster_groups.values())
        print(f"聚类分组中共包含{total_in_clusters}个教学班（筛选后共{len(list_of_jxbids_set)}个）")
        
        for cluster_name, jxbids in cluster_groups.items():
            print(f"  {cluster_name}: {len(jxbids)}个教学班")
        
        # 如果聚类结果中没有匹配的教学班，使用原始列表
        if not cluster_groups:
            print("警告：聚类结果中没有匹配的教学班，将使用全部教学班进行排课")
            cluster_groups = {'全部': list(list_of_jxbids_set)}
            
    except Exception as e:
        print(f"加载聚类结果失败: {e}")
        print("将使用全部教学班进行排课")
        if isinstance(list_of_jxbids, list):
            cluster_groups = {'全部': list_of_jxbids}
        else:
            cluster_groups = {'全部': list(list_of_jxbids)}
    
    # return cluster_groups
    # key=str：兼容聚类 ID 为 int 与 '未分类' 字符串混排
    return dict(sorted(cluster_groups.items(), key=lambda kv: str(kv[0])))


def record_failed_course(jxbid, courses, department, course_type, combination_failed, all_failed_courses):
    """
    记录排课失败的课程信息
    
    Args:
        jxbid: 教学班ID
        courses: 课程对象列表
        department: 开课单位
        course_type: 课程类别
        combination_failed: 组合失败列表（用于追加）
        all_failed_courses: 全部失败列表（用于追加）
    """
    jxb_objects = get_consecutive_jxbid(jxbid, courses)
    if jxb_objects:
        course_name = jxb_objects[0].KCM
        perfer_time = jxb_objects[0].Prefer_Time
        unv_time = jxb_objects[0].unavailable_Time
        combination_failed.append((jxbid, course_name, perfer_time, unv_time))
        all_failed_courses.append((jxbid, course_name, department, course_type, perfer_time, unv_time))
    else:
        combination_failed.append((jxbid, "未知课程"))
        all_failed_courses.append((jxbid, "未知课程", department, course_type))


def schedule_batch(batch_jxbids, courses, teachers, classes, list_of_jas, jasdm_dict, 
                   num_days, department, course_type, combination_results, all_scheduling_results,
                   combination_failed, all_failed_courses, scheduled_count):
    """
    对一个批次的教学班进行排课
    
    Args:
        batch_jxbids: 批次中的教学班ID列表
        courses: 课程对象列表
        teachers: 教师对象列表
        classes: 班级对象列表
        list_of_jas: 教室列表
        jasdm_dict: 教室代码字典
        num_days: 一周天数
        department: 开课单位
        course_type: 课程类别
        combination_results: 组合结果列表（用于追加）
        all_scheduling_results: 全部结果列表（用于追加）
        combination_failed: 组合失败列表（用于追加）
        all_failed_courses: 全部失败列表（用于追加）
        scheduled_count: 已排课数量（用于更新）
    
    Returns:
        scheduled_count: 更新后的已排课数量
        should_continue: 是否应该继续处理下一个批次
    """
    current_jxbids = batch_jxbids.copy()
    
    # 循环排课直到当前批次的所有课程排完或无法继续排课
    while current_jxbids:
        print(f"\n------ 第 {scheduled_count + 1} 轮排课 ------")
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
            scheduled_count += 1
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
                record_failed_course(jxbid, courses, department, course_type, 
                                    combination_failed, all_failed_courses)
                # 从待排课列表中移除
                current_jxbids.remove(jxbid)
            
            # 如果没有找到可排课程，将剩余所有课程记录为失败并退出当前批次
            if not jxbid:
                print("无法确定下一个要排的课程，本批次排课过程终止")
                # 记录所有剩余课程为失败课程
                for remaining_jxbid in current_jxbids:
                    record_failed_course(remaining_jxbid, courses, department, course_type,
                                        combination_failed, all_failed_courses)
                # 退出当前批次，继续处理下一个批次
                return scheduled_count, False
    
    return scheduled_count, True


def schedule_cluster(cluster_name, cluster_jxbids, courses, teachers, classes, list_of_jas, 
                     jasdm_dict, num_days, department, course_type, batch_size,
                     combination_results, all_scheduling_results, combination_failed, 
                     all_failed_courses, scheduled_count):
    """
    对一个聚类类别的教学班进行排课
    
    Args:
        cluster_name: 类别名称
        cluster_jxbids: 类别中的教学班ID列表
        courses: 课程对象列表
        teachers: 教师对象列表
        classes: 班级对象列表
        list_of_jas: 教室列表
        jasdm_dict: 教室代码字典
        num_days: 一周天数
        department: 开课单位
        course_type: 课程类别
        batch_size: 批次大小
        combination_results: 组合结果列表（用于追加）
        all_scheduling_results: 全部结果列表（用于追加）
        combination_failed: 组合失败列表（用于追加）
        all_failed_courses: 全部失败列表（用于追加）
        scheduled_count: 已排课数量（用于更新）
    
    Returns:
        scheduled_count: 更新后的已排课数量
    """
    print(f"\n----- 聚类类别: {cluster_name} -----")
    print(f"本类别教学班数量: {len(cluster_jxbids)}")
    
    # 如果类别中的教学班数量超过batch_size，分批次处理
    if len(cluster_jxbids) > batch_size:
        # 计算批次数
        num_batches = (len(cluster_jxbids) + batch_size - 1) // batch_size
        print(f"本类别教学班数量超过{batch_size}，将分{num_batches}批次处理")
        
        # 将教学班列表分成多个批次
        jxbid_batches = [cluster_jxbids[i*batch_size:(i+1)*batch_size] for i in range(num_batches)]
    else:
        # 只有一个批次
        jxbid_batches = [cluster_jxbids]
    
    # 对每个批次进行排课
    for batch_idx, batch_jxbids in enumerate(jxbid_batches):
        print(f"\n  --- 批次 {batch_idx+1}/{len(jxbid_batches)} ---")
        print(f"  本批次教学班数量: {len(batch_jxbids)}")
        
        scheduled_count, should_continue = schedule_batch(
            batch_jxbids, courses, teachers, classes, list_of_jas, jasdm_dict,
            num_days, department, course_type, combination_results, all_scheduling_results,
            combination_failed, all_failed_courses, scheduled_count
        )
        
        # 注意：即使should_continue为False，也继续处理下一个批次
        # 因为should_continue只表示当前批次无法继续，不代表后续批次也无法处理
        # 只有当所有批次都处理完后才停止
    
    return scheduled_count


def schedule_combination(combination_info, courses, teachers, classes, classrooms, 
                         clustering_file, results_dir, batch_size, num_days,
                         all_scheduling_results, all_failed_courses):
    """
    对一个筛选条件组合进行排课
    
    Args:
        combination_info: 组合信息字典，包含department, course_type, combination_name
        courses: 课程对象列表
        teachers: 教师对象列表
        classes: 班级对象列表
        classrooms: 教室对象列表
        clustering_file: 聚类结果文件路径
        results_dir: 结果保存目录
        batch_size: 批次大小
        num_days: 一周天数
        all_scheduling_results: 全部结果列表（用于追加）
        all_failed_courses: 全部失败列表（用于追加）
    
    Returns:
        combination_results: 组合排课结果列表
        combination_failed: 组合排课失败列表
        combination_scheduled_count: 组合排课数量
        combination_time: 组合排课耗时
    """
    department = combination_info['department']
    course_type = combination_info['course_type']
    combination_name = combination_info['combination_name']
    
    print("\n" + "="*80)
    print(f"组合: 开课单位='{department}', 课程类别='{course_type}'")
    print("="*80)
    
    # 初步筛选出该组合的教学班和教室（校区设为None）
    jxbid_dict, jasdm_dict, list_of_jas = pre_selection(courses, classrooms, course_type, department, None)
    #print(jxbid_dict)
    
    # 列表化所有待排教学班的ID
    list_of_jxbids = list(jxbid_dict.keys())
    if not list_of_jxbids:
        print(f"该组合下没有符合条件的教学班，跳过")
        return [], [], 0, 0.0
    
    print(f"该组合下待排教学班数量: {len(list_of_jxbids)}")
    
    # 读取聚类结果
    cluster_groups = load_clustering_results(clustering_file, list_of_jxbids)
    
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
    
    # 对每个聚类类别进行排课
    combination_scheduled_count = 0
    
    # 按类别名称排序，确保处理顺序一致
    sorted_cluster_names = sorted(cluster_groups.keys())
    
    for cluster_idx, cluster_name in enumerate(sorted_cluster_names):
        cluster_jxbids = cluster_groups[cluster_name]
        print(f"\n----- 聚类类别 {cluster_idx+1}/{len(sorted_cluster_names)}: {cluster_name} -----")
        
        combination_scheduled_count = schedule_cluster(
            cluster_name, cluster_jxbids, courses, teachers, classes, list_of_jas,
            jasdm_dict, num_days, department, course_type, batch_size,
            combination_results, all_scheduling_results, combination_failed, all_failed_courses, 
            combination_scheduled_count
        )
    
    # 该组合排课结束，计算耗时
    combination_end_time = time.time()
    combination_time = combination_end_time - combination_start_time
    
    print(f"\n---- 组合排课结束 ----")
    print(f"组合: 开课单位='{department}', 课程类别='{course_type}'")
    print(f"共排课 {combination_scheduled_count} 门")
    print(f"耗时: {combination_time:.2f} 秒")
    if combination_scheduled_count > 0:
        print(f"平均每个教学班耗时: {combination_time/combination_scheduled_count:.2f} 秒")
    
    return combination_results, combination_failed, combination_scheduled_count, combination_time


def process_combinations(combinations_df, courses, teachers, classes, classrooms, 
                        clustering_file, results_dir, batch_size, num_days):
    """
    处理所有筛选条件组合
    
    Args:
        combinations_df: 筛选条件组合DataFrame
        courses: 课程对象列表
        teachers: 教师对象列表
        classes: 班级对象列表
        classrooms: 教室对象列表
        clustering_file: 聚类结果文件路径
        results_dir: 结果保存目录
        batch_size: 批次大小
        num_days: 一周天数
    
    Returns:
        all_scheduling_results: 全部排课结果列表
        all_failed_courses: 全部失败课程列表
        total_scheduled_count: 总排课数量
        grand_total_time: 总耗时
    """
    # 创建去重的组合字典，只按开课单位和课程类别划分
    print("正在处理筛选条件组合（只按开课单位和课程类别划分）...")
    unique_combinations = {}
    
    for idx, combination in combinations_df.iterrows():
        # 把原始表格里的"空白"或"缺失"统一转换成 None
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
    
    # 初始化总结果
    all_scheduling_results = []
    all_failed_courses = []
    total_scheduled_count = 0
    grand_total_start_time = time.time()
    
    # 对每个有效组合进行排课
    for combo_idx, (key, combination_info) in enumerate(valid_combinations):
        print(f"\n组合 {combo_idx+1}/{len(valid_combinations)}")
        #print(courses)
        
        combination_results, combination_failed, combination_scheduled_count, combination_time = schedule_combination(
            combination_info, courses, teachers, classes, classrooms,
            clustering_file, results_dir, batch_size, num_days,
            all_scheduling_results, all_failed_courses
        )
        
        # 注意：all_scheduling_results 和 all_failed_courses 已经在 schedule_combination 中被更新
        # 这里只需要更新计数
        total_scheduled_count += combination_scheduled_count
        
        # 保存该组合的排课结果
        if combination_results:
            combination_dir = os.path.join(results_dir, combination_info['combination_name'].replace('/', '_'))
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
    
    return all_scheduling_results, all_failed_courses, total_scheduled_count, grand_total_time


def save_final_results(all_scheduling_results, all_failed_courses, total_scheduled_count, 
                       grand_total_time, courses, results_dir):
    """
    保存最终排课结果
    
    Args:
        all_scheduling_results: 全部排课结果列表
        all_failed_courses: 全部失败课程列表
        total_scheduled_count: 总排课数量
        grand_total_time: 总耗时
        courses: 课程对象列表
        results_dir: 结果保存目录
    """
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


def main():
    """主函数"""
    parser = argparse.ArgumentParser(description="首轮贪心排课")
    parser.add_argument("--strategy", choices=["first", "random", "balanced"], default="first",
                        help="放置策略：first=原始（方案数最多教室的第一个时段）；"
                             "random=GRASP式随机；balanced=负载均衡（消融实验用）")
    parser.add_argument("--seed", type=int, default=2026, help="random/balanced 策略的随机种子")
    args = parser.parse_args()
    set_placement_strategy(args.strategy, args.seed)

    # 基础数据路径（4670 那套：converted 数据，2026-06-23 切回）
    BASE_DIR = os.path.join(os.path.dirname(__file__), '智能排课基础数据', '提取的基础数据表_converted')
    course_excel = os.path.join(BASE_DIR, '课程表_split_merged.xlsx')   # Issue #14 拆奇数 ZXS + 合并冲突开关列（含 LLXS/IF_ROOM_CONFICT/IF_CLASS_CONFICT）
    classroom_excel = os.path.join(BASE_DIR, '教室表.xlsx')
    teacher_excel = os.path.join(BASE_DIR, '教师表.xlsx')
    banji_excel = os.path.join(BASE_DIR, '班级表.xlsx')

    
    # 排课参数
    num_weeks = 20
    num_periods = 11     ##一天12节课
    num_days = 7    #一周7天，要想办法限制默认将课程排在工作日内
    batch_size = 50  # 批次大小
    
    # 创建结果保存目录
    results_dir = "排课结果"
    if not os.path.exists(results_dir):
        os.makedirs(results_dir)
        print(f"创建目录: {results_dir}")
    
    # 时间表保存文件
    timetables_file = os.path.join(results_dir, "saved_timetables.pkl")
    
    # 聚类结果（4670 那套使用 课程聚类结果2.xlsx，列含 类别ID/类别名称）
    clustering_file = os.path.join(os.path.dirname(__file__), "排课结果1", "课程聚类结果2.xlsx")
    
    # 加载基础数据
    print("正在加载基础数据...")
    teachers = load_teachers(teacher_excel, weeks=num_weeks, days=num_days, periods=num_periods)
    classrooms = load_classrooms(classroom_excel, weeks=num_weeks, days=num_days, periods=num_periods)
    courses = load_courses(course_excel, weeks=num_weeks, days=num_days, periods=num_periods)
    classes = load_classes(banji_excel, weeks=num_weeks, days=num_days, periods=num_periods)
    print("基础数据加载完成")
    
    # 加载筛选条件组合
    try:
        combinations_df = pd.read_excel('筛选条件组合.xlsx')
        print(f"成功加载{len(combinations_df)}个筛选条件组合")
    except Exception as e:
        print(f"加载筛选条件组合失败: {e}")
        print("将使用默认组合进行排课")
        combinations_df = pd.DataFrame([{'开课单位': None, '课程类别': None, '上课校区': None}])  # 不使用任何条件进行筛选
    
    # 处理所有组合
    all_scheduling_results, all_failed_courses, total_scheduled_count, grand_total_time = process_combinations(
        combinations_df, courses, teachers, classes, classrooms,
        clustering_file, results_dir, batch_size, num_days
    )
    
    # 保存最终结果
    save_final_results(all_scheduling_results, all_failed_courses, total_scheduled_count,
                      grand_total_time, courses, results_dir)
    
    # 保存完整的时间表数据，用于调课
    print("\n=== 保存排课结果数据 ===")
    save_timetables(courses, teachers, classrooms, classes, timetables_file)
    
    # 调课：用子进程跑 reschedule_by_adjusting.py（独立 main 入口，使用最新写入的 pkl）
    print("\n=== 启动变邻域调课 ===")
    import subprocess, sys as _sys
    rc = subprocess.run([_sys.executable, "reschedule_by_adjusting.py"],
                        cwd=os.path.dirname(os.path.abspath(__file__))).returncode
    print(f"调课退出码: {rc}")


if __name__ == "__main__":
    main()

