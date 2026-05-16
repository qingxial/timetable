import pandas as pd
import reschedule_by_adjusting


def process_teacher_data(table1_path, table2_path, output_path=None):
    """
    处理教师数据：将表格1中的新教师添加到表格2中，并自动编号

    参数:
    table1_path (str): 表格1文件路径
    table2_path (str): 表格2文件路径
    output_path (str, optional): 输出文件路径，默认为None（覆盖原文件）
    """
    # 读取两个表格
    df1 = pd.read_excel(table1_path)
    df2 = pd.read_excel(table2_path)

    # 检查必要的列是否存在
    if '教师姓名' not in df1.columns:
        raise ValueError("表格1缺少'教师姓名'列")
    if 'XM' not in df2.columns:
        raise ValueError("表格2缺少'姓名'列")

    # 获取表格1中的教师列表（去重）
    table1_teachers = set(df1['教师姓名'].dropna().unique())

    # 获取表格2中的教师列表（去重）
    table2_teachers = set(df2['XM'].dropna().unique())

    # 找出表格1中存在但表格2中不存在的教师
    new_teachers = list(table1_teachers - table2_teachers)

    if not new_teachers:
        print("没有发现新的教师需要添加")
        return

    print(f"发现 {len(new_teachers)} 个新教师: {', '.join(new_teachers)}")

    # 确定新的教师编号起始值
    max_new_id = 0
    if '教师号' in df2.columns:
        # 提取已有的"newX"格式的编号
        existing_new_ids = df2['教师号'].astype(str).str.extract(r'new(\d+)')[0].dropna()
        if not existing_new_ids.empty:
            existing_new_ids = existing_new_ids.astype(int)
            max_new_id = existing_new_ids.max()

    # 创建新教师的数据
    new_data = []
    for i, teacher in enumerate(new_teachers, start=1):
        new_id = f"new{max_new_id + i}"
        new_data.append({'教师号': new_id, 'XM': teacher})

    # 创建DataFrame并添加到表格2
    new_df = pd.DataFrame(new_data)
    updated_df = pd.concat([df2, new_df], ignore_index=True)

    # 保存结果
    save_path = output_path if output_path else table2_path
    updated_df.to_excel(save_path, index=False)
    print(f"已添加 {len(new_teachers)} 个新教师到 {save_path}")


import pandas as pd

def pipeijiaoshi(table1_path, table2_path, output_path="匹配结果.xlsx"):
    # 读取并统一格式
    df1 = pd.read_excel(table1_path, dtype={'课程号': str})
    df2 = pd.read_excel(table2_path, dtype={'课程号': str})

    # 去掉多余空格
    df1['课程号'] = df1['课程号'].str.strip()
    df2['课程号'] = df2['课程号'].str.strip()

    # 打印可能的重复项
    if df2['课程号'].duplicated().any():
        print("⚠️ 警告：表2中存在重复课程号！请检查：")
        print(df2[df2['课程号'].duplicated(keep=False)])

    # 定义一个查找历史教室代码的函数
    def find_classroom(course_id):
        # 在表2中查找对应的历史教室代码
        matching_row = df2[df2['课程号'] == course_id]
        if not matching_row.empty:
            # 如果找到了匹配的课程号，返回历史教室代码
            return matching_row['历史教室代码'].values[0]
        else:
            return None  # 如果找不到，返回 None

    # 逐行查找历史教室代码并添加到表格1
    df1['历史教室代码'] = df1['课程号'].apply(find_classroom)

    # 保存结果
    df1.to_excel(output_path, index=False)
    print(f"✅ 匹配完成，结果已保存到 {output_path}")


def chaifenteacher(input_path, output_path=None):

    # ===== 1. 读取表格 =====
    df = pd.read_excel(input_path)

    # ===== 2. 拆分“教师姓名”列 =====
    # 假设列名为“教师姓名”，用英文逗号分隔
    df['教师姓名'] = df['教师姓名'].astype(str)  # 确保是字符串
    df_expanded = df.assign(教师姓名=df['教师姓名'].str.split(',')) \
        .explode('教师姓名', ignore_index=True)

    # 去掉空白字符（例如逗号后有空格）
    df_expanded['教师姓名'] = df_expanded['教师姓名'].str.strip()

    # ===== 3. 保存结果 =====
    output_path = "拆分后表1.xlsx"
    df_expanded.to_excel(output_path, index=False)

    print(f"✅ 拆分完成，结果已保存到 {output_path}")

def pipeiJSH(table1_path, table2_path, output_path="匹配结果.xlsx"):
    # 读取并统一格式
    df1 = pd.read_excel(table1_path, dtype={'教师姓名': str})
    df2 = pd.read_excel(table2_path, dtype={'XM': str})

    # 去掉多余空格
    df1['教师姓名'] = df1['教师姓名'].str.strip()
    df2['XM'] = df2['XM'].str.strip()

    # 打印可能的重复项
    if df2['XM'].duplicated().any():
        print("⚠️ 警告：表2中存在重复XM！请检查：")
        print(df2[df2['XM'].duplicated(keep=False)])

    # 定义一个查找历史教室代码的函数
    def find_classroom(course_id):
        # 在表2中查找对应的历史教室代码
        matching_row = df2[df2['XM'] == course_id]
        if not matching_row.empty:
            # 如果找到了匹配的课程号，返回历史教室代码
            return matching_row['JSH'].values[0]
        else:
            return None  # 如果找不到，返回 None

    # 逐行查找历史教室代码并添加到表格1
    df1['教师编号'] = df1['教师姓名'].apply(find_classroom)

    # 保存结果
    df1.to_excel(output_path, index=False)
    print(f"✅ 匹配完成，结果已保存到 {output_path}")

def week_to_str():

    # ===== 1. 读取表格 =====
    input_path = r"D:\Projects\pycharm_projects\new智能排课\智能排课基础数据\课程表2025-2026-1.xlsx"  # 修改为你的文件路径
    df = pd.read_excel(input_path)

    # ===== 2. 定义解析函数 =====
    def parse_weeks(week_str):
        """
        将上课周次解析为16位二进制字符串。
        支持示例：
          1-16周         -> 1111111111111111
          9-16周         -> 0000000011111111
          1-3周,5周       -> 1110100000000000
          1-8周(单)       -> 1010101000000000
          2-4周(双),7周   -> 0101001000000000
        """
        week_str = str(week_str).strip()
        bits = ['0'] * 16

        # 拆分多个片段（用逗号、顿号、空格分隔）
        parts = re.split(r'[，,、\s]+', week_str)
        for part in parts:
            if not part:
                continue

            # 检查单双周标志
            is_single = '单' in part
            is_double = '双' in part

            # 提取数字区间
            segments = re.findall(r'(\d+(?:-\d+)?)', part)
            for seg in segments:
                if '-' in seg:
                    start, end = map(int, seg.split('-'))
                    week_range = range(start, end + 1)
                else:
                    week_range = [int(seg)]

                for w in week_range:
                    if 1 <= w <= 16:
                        # 按单双周过滤
                        if is_single and w % 2 == 0:
                            continue
                        if is_double and w % 2 == 1:
                            continue
                        bits[w - 1] = '1'

        return ''.join(bits)

    # ===== 3. 应用函数到“上课周次”列 =====
    df['周次代码'] = df['上课周次'].apply(parse_weeks)

    # ===== 4. 保存结果 =====
    output_path = "周次解析结果.xlsx"
    df.to_excel(output_path, index=False)

    print(f"✅ 解析完成，结果已保存到 {output_path}")


def get_class():

    # 输入输出文件路径
    input_file = r"D:\Projects\pycharm_projects\new智能排课\智能排课基础数据\课程表2025-2026-1.xlsx"
    output_file = r"D:\Projects\pycharm_projects\new智能排课\智能排课基础数据\班级汇总2025-2026-1.xlsx"


    # 读取原表
    df = pd.read_excel(input_file)

    # 假设目标列名是“推荐班级”
    col_name = "推荐班级"

    # 拆分每一行的班级，用英文逗号分隔
    bjmc_list = []
    for value in df[col_name].dropna():
        classes = [c.strip() for c in value.split(",") if c.strip()]
        bjmc_list.extend(classes)

    # 生成新的 DataFrame
    df_out = pd.DataFrame({"BJMC": bjmc_list})

    # 保存为新表格
    df_out.to_excel(output_file, index=False)

    print(f"已生成表格2：{output_file}")


def remove_duplicates(input_file, output_file):
    # 读取表格
    df = pd.read_excel(input_file)

    # 去重操作，只保留唯一的 BJMC 值
    df_unique = df.drop_duplicates(subset=['BJMC'])

    # 保存去重后的结果
    df_unique.to_excel(output_file, index=False)
    print(f"✅ 去重完成，结果已保存到 {output_file}")

def dettle():

    # 读取文件
    df = pd.read_excel(r"D:\Projects\pycharm_projects\new智能排课\智能排课基础数据\课程表2025-2026-1.xlsx")

    # 删除整行为空的行
    df = df.dropna(how='all')

    # 保存到新文件
    df.to_excel("去空行后的数据.xlsx", index=False)

def keneizhouxueshi():

    # ==== 1. 文件路径 ====
    file1 = r"D:\Projects\pycharm_projects\new智能排课\智能排课基础数据\课程表2025-2026-1.xlsx"
    file2 = r"D:\Projects\pycharm_projects\new智能排课\智能排课基础数据\2025.10.20-2025-2026-1选课课程- (1).xlsx"
    output_file = '匹配后_课内周学时.xlsx'

    # ==== 2. 读取并清理空行 ====
    table1 = pd.read_excel(file1, engine='openpyxl').dropna(how='all')
    table2 = pd.read_excel(file2, engine='openpyxl').dropna(how='all')

    #table2 = pd.read_excel(file2, engine='xlrd').dropna(how='all')

    # ==== 3. 统一课程号格式 ====
    for df in [table1, table2]:
        df['课程号'] = df['课程号'].astype(str).str.strip().str.replace(r'\.0$', '', regex=True)

    # ==== 4. 去重，只保留每个课程号第一次出现 ====
    table2_unique = table2.drop_duplicates(subset=['课程号'], keep='first')

    # ==== 5. 左连接合并 ====
    merged = pd.merge(
        table1,
        table2_unique[['课程号', '课内周学时']],
        on='课程号',
        how='left'
    )

    # ==== 6. 输出结果 ====
    merged.to_excel(output_file, index=False)

    # ==== 7. 匹配统计 ====
    #matched = merged['课内周学时'].notna().sum()
    #total = len(merged)
    #print(f"✅ 匹配完成！共 {total} 条课程，其中 {matched} 条匹配成功，匹配率 {matched / total:.2%}")
    print(f"已输出文件：{output_file}")


# 使用示例
if __name__ == "__main__":
    # 文件路径设置
    #table1_path = r"D:\Projects\pycharm_projects\new智能排课\智能排课基础数据\课程表2025-2026-1.xlsx"  # 包含原始教师数据的表格
    #table2_path = r"D:\Projects\pycharm_projects\new智能排课\智能排课基础数据\更新后的教师名单2025-2026-1.xlsx"  # 包含完整教师名单的表格
    #chaifenteacher(table1_path)
    # 处理数据（将结果保存到新文件）
    #pipeiJSH(table1_path, table2_path)
    #process_teacher_data(table1_path=table1_path, table2_path=table2_path,output_path="更新后的教师名单.xlsx")

    #pipeijiaoshi(r"D:\Projects\pycharm_projects\new智能排课\智能排课基础数据\2025-2026-1排课任务.xlsx",r"D:\Projects\pycharm_projects\new智能排课\智能排课基础数据\课程教室对照表.xlsx")
    #week_to_str()
    #get_class()
    #pipeijiaoshi(r"D:\Projects\pycharm_projects\new智能排课\智能排课基础数据\课程表2025-2026-1.xlsx",
     #            r"D:\Projects\pycharm_projects\new智能排课\智能排课基础数据\课程教室对照表.xlsx")
     #remove_duplicates(r"D:\Projects\pycharm_projects\new智能排课\智能排课基础数据\班级汇总2025-2026-1.xlsx","去重.xlsx")
     #dettle()
     keneizhouxueshi()
