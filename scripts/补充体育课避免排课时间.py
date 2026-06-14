import pandas as pd
import re

# 读取Excel
# df = pd.read_excel("/Users/qingxia/Downloads/Python-projects/毕设/timetable/智能排课基础数据/提取的基础数据表_converted/课程表.xlsx")


import pandas as pd

file_path = "/Users/qingxia/Downloads/Python-projects/毕设/timetable/智能排课基础数据/提取的基础数据表_converted/课程表.xlsx"
# output_file = "/Users/qingxia/Downloads/Python-projects/毕设/timetable/排课结果/课程聚类结果2.xlsx"

# df = pd.read_excel(input_file)

# import pandas as pd


df = pd.read_excel(file_path)

# 体育课程
sport_mask = df["课程名称"].astype(str).str.contains("体育与健康4", na=False)

for idx in df[sport_mask].index:

    old_time = df.at[idx, "避免排课时间"]

    if pd.isna(old_time) or str(old_time).strip() == "":
        df.at[idx, "避免排课时间"] = "全周（1-2节）"

    elif "全周（1-2节）" not in str(old_time):
        df.at[idx, "避免排课时间"] = (
            str(old_time).strip()
            + ";全周（1-2节）"
        )

df.to_excel(file_path, index=False)

print(f"处理完成，共修改 {sport_mask.sum()} 条体育课程")


# # 去掉第二行英文表头
# df = df.iloc[1:].copy()

# df = df.drop_duplicates(subset=["教学班ID"])

# def classify_course(course_name, course_type):

#     course_name = str(course_name)
#     course_type = str(course_type)

#     # 体育
#     if "体育" in course_name:
#         return 1, "体育类"

#     # 英语
#     if "英语" in course_name:
#         return 2, "英语类"

#     # 数学
#     if any(x in course_name for x in [
#         "高等数学",
#         "线性代数",
#         "概率论",
#         "数学"
#     ]):
#         return 3, "数学类"

#     # 思政
#     if any(x in course_name for x in [
#         "形势与政策",
#         "中国近现代史纲要",
#         "毛泽东思想",
#         "习近平新时代中国特色社会主义思想概论",
#         "马克思主义基本原理",
#         "思想道德与法治"
#     ]):
#         return 4, "思政类"

#     # 专业课
#     if any(x in course_type for x in [
#         "专业基础",
#         "专业核心",
#         "专业方向",
#         "专业选修"
#     ]):
#         return 6, "专业课"

#     # 其他公修课
#     return 5, "公修课"


# df[["类别ID", "类别名称"]] = df.apply(
#     lambda row: pd.Series(
#         classify_course(
#             row["课程名称"],
#             row["课程类别名称"]
#         )
#     ),
#     axis=1
# )

# result = df[
#     [
#         "教学班ID",
#         "课程名称",
#         "课程类别名称",
#         "类别ID",
#         "类别名称"
#     ]
# ]

# result.to_excel(
#     output_file,
#     index=False
# )

# print("完成，输出:", output_file)


# # 读取"推荐班级"列
# class_col = df["推荐班级"].dropna()

# # 提取并去重
# class_set = set()

# for text in class_col:
#     # 按空格拆分
#     classes = str(text).split()

#     for cls in classes:
#         cls = cls.strip()
#         if cls:
#             class_set.add(cls)

# # 排序
# class_list = sorted(class_set)

# # 保存到新Excel
# result = pd.DataFrame({
#     "班级": class_list
# })

# result.to_excel("/Users/qingxia/Downloads/Python-projects/毕设/timetable/智能排课基础数据/提取的基础数据表_converted/班级表.xlsx", index=False)

# print(f"提取到 {len(class_list)} 个班级")

# 推荐班级列
# df["推荐班级"] = (
#     df["推荐班级"]
#     .astype(str)
#     .str.replace(";班级:", " ", regex=False)
#     .str.replace("班级:", " ", regex=False)
# )
# 将“避免排课时间”为空（NaN、空字符串、纯空格）的单元格填充
# df["避免排课时间"] = (
#     df["避免排课时间"]
#     .fillna("")
#     .astype(str)
# )

# mask = df["避免排课时间"].str.strip() == ""

# df.loc[mask, "避免排课时间"] = "周二(5-8节）"

# # 覆盖保存回原文件
# # df.to_excel(file_path, index=False)

# df.to_excel("/Users/qingxia/Downloads/Python-projects/毕设/timetable/智能排课基础数据/提取的基础数据表_converted/课程表.xlsx", index=False)