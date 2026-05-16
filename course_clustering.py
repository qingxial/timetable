
import numpy as np
import pandas as pd
from dataclasses import fields as dataclass_fields
from typing import List, Tuple, Dict

#from vllm import LLM
from sklearn.cluster import KMeans

from Basic_Data import *
import os
from utils1 import *
import time
import shutil
import pickle  # 添加pickle模块用于序列化和反序列化
from reschedule_by_adjusting import *
from tryloadlimit import *
from openai import OpenAI
from sentence_transformers import SentenceTransformer
import torch

import os

download_path = './my_models'
os.makedirs(download_path, exist_ok=True)
# 自动检测设备，选择 CUDA
device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

model = SentenceTransformer(
    "Qwen/Qwen3-Embedding-0.6B",
    cache_folder=download_path,
    model_kwargs={"attn_implementation": "flash_attention_2"},
    tokenizer_kwargs={"padding_side": "left"}
)  #V3
model = model.to(device)

#model = SentenceTransformer("Qwen/Qwen3-Embedding-0.6B")  #v2
# client = OpenAI(
#     base_url='https://api-inference.modelscope.cn/v1',
#     api_key='ms-7c42ddc8-2c92-40e2-af21-a3fa92c6825d', # ModelScope Token
# )  #v1

def get_batch_embeddings(
    texts: List[str],
    model_name: str = "Qwen/Qwen3-Embedding-0.6B",
    batch_size: int = 10,
    encoding_format: str = "float"
) -> List[List[float]]:
    """
    使用 OpenAI client 批量获取 embeddings。
    
    Args:
        texts: 文本列表
        model_name: 模型名称
        batch_size: 每批处理的文本数量（避免 API 限制）
        encoding_format: 编码格式，默认为 "float"
    
    Returns:
        embeddings 列表，每个元素是一个 embedding 向量（List[float]）
    """
    all_embeddings = []
    
    # 分批处理
    for i in range(0, len(texts), batch_size):
        batch_texts = texts[i:i + batch_size]
        
        try:
            ##V1
            # response = client.embeddings.create(
            #     model=model_name,
            #     input=batch_texts,
            #     encoding_format=encoding_format
            # )

            
            # # 提取 embeddings
            # batch_embeddings = [item.embedding for item in response.data]
            ##V2
            #batch_embeddings=model.encode(batch_texts)
            ##V3
            with torch.autocast(device_type='cuda', dtype=torch.float16):
                batch_embeddings = model.encode(batch_texts, prompt_name="cluster", device='cuda') #instruct=cluster
            all_embeddings.extend(batch_embeddings)
            
            print(f"已处理 {min(i + batch_size, len(texts))}/{len(texts)} 个文本")
            
        except Exception as e:
            print(f"处理第 {i//batch_size + 1} 批时出错: {e}，出错文本: {batch_texts}")
            raise
    
    return all_embeddings


def get_course_embeddings_by_client(
    courses_texts: Dict,
    model_name: str = "Qwen/Qwen3-Embedding-0.6B",
    batch_size: int = 10,
) -> Dict:
    """
    使用 OpenAI client 批量获取课程 embeddings，并更新到 courses_texts 字典中。
    
    Args:
        courses_texts: 字典，键为 jxbid，值为包含 'course_text' 的字典
        model_name: 模型名称
        batch_size: 每批处理的文本数量
    
    Returns:
        更新后的 courses_texts 字典，每个 jxbid 对应的字典中增加了 'embedding' 字段
    """
    # 提取所有文本，保持顺序
    jxbids = list(courses_texts.keys())
    texts = [courses_texts[jxbid]["course_text"] for jxbid in jxbids]
    
    embeddings_list = []
    embeddings_list = get_batch_embeddings(texts, model_name=model_name, batch_size=batch_size)
    # # 批量获取 embeddings
    # print(f"开始获取 {len(texts)} 个文本的 embeddings")
    # response = client.embeddings.create(
    #             model=model_name,
    #             input=texts,
    #             encoding_format="float"
    #         )
    # print(f"API 响应: {response}")
    # if response and response.data:
    #     embeddings_list = [item.embedding for item in response.data]
    
    # else:
    #     raise ValueError("API 响应无有效数据")

    # 将 embeddings 添加回 courses_texts 字典
    for jxbid, embedding in zip(jxbids, embeddings_list):
        courses_texts[jxbid]["embedding"] = embedding
    
    return courses_texts


def pre_selection_courses(courses, course_type=None, department=None, campus=None):
    """初步筛选教学班
    
    Args:
        courses: 所有课程对象列表
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

    # 去重
    unique_jxbids = list(dict.fromkeys(valid_jxbids)) 

    return unique_jxbids

def build_course_text(jxbid: str, courses: List[Course]) -> str:
    """
    将一个课程对象的所有属性转换为用于 embedding 的文本。
    形式为： "属性名: 属性值；属性名: 属性值；..."
    （排除 timetable 这样的大数组字段）
    """
    jxbs = get_consecutive_jxbid(jxbid, courses) #教学班相同的所有教学班对象     
    parts=[]
    if jxbid is not None:
        parts.append(f"教学班ID: {jxbid}")
    if jxbs[0].KCM is not None:
        parts.append(f"课程名:{jxbs[0].KCM}")
    if jxbs[0].YXMC is not None:
        parts.append(f"开课单位:{jxbs[0].YXMC}")
    if jxbs[0].KCLB is not None:
        parts.append(f"课程类别:{jxbs[0].KCLB}")
    if jxbs[0].SKXQ is not None:
        parts.append(f"上课校区:{jxbs[0].SKXQ}")
    if jxbs[0].KRL is not None:
        parts.append(f"课容量:{jxbs[0].KRL}")
    if jxbs[0].SKZCDM is not None:
        parts.append(f"上课周次:{jxbs[0].SKZCDM}")
    if jxbs[0].ZXS is not None:
        parts.append(f"周学时:{jxbs[0].ZXS}")
    if jxbs[0].JASLXDM is not None:
        parts.append(f"教室类型代码:{jxbs[0].JASLXDM}")
    if jxbs[0].JASLXMC is not None:
        parts.append(f"教室类型名称:{jxbs[0].JASLXMC}")
    if jxbs[0].JXLDM is not None:
        parts.append(f"教学楼代码:{jxbs[0].JXLDM}")
    if jxbs[0].JASDM is not None:
        parts.append(f"教室代码:{jxbs[0].JASDM}")
    if jxbs[0].Prefer_Time is not None:
        parts.append(f"偏好时间:{jxbs[0].Prefer_Time}")
    if jxbs[0].unavailable_Time is not None:
        parts.append(f"禁止时间:{jxbs[0].unavailable_Time}")
    if jxbs[0].LSJASDM is not None:
        parts.append(f"历年教室代码:{jxbs[0].LSJASDM}")
    if jxbs[0].TJBJ is not None:
        parts.append(f"推荐班级:{jxbs[0].TJBJ}")
    teachers=[]
    for jxb in jxbs:
        str_teacher=""
        if jxb.XM is not None:
            str_teacher=f"教师姓名:{jxb.XM}"
        if jxb.JSH is not None:
            str_teacher+=f"，教师编号:{jxb.JSH}"
        if jxb.RWJSZCDM is not None:
            str_teacher+=f"，教师周次代码:{jxb.RWJSZCDM}"
        teachers.append(str_teacher)
    parts.append(f"任课教师信息:{"，".join(teachers)}")
    return "；".join(parts)
    




# def get_course_embedding1(
#     courses_texts: Dict,
#     model_name: str = "Qwen/Qwen3-Embedding-0.6B",
# ) -> Dict:
#     """
#     使用 vLLM 的 embedding 模型对课程做向量化。

#     返回 shape = [num_courses, hidden_dim] 的 tensor。
#     """
#     llm = LLM(model=model_name, task="embed")

#     texts = [courses_texts[jxbid]["course_text"] for jxbid in courses_texts]

#     outputs = llm.embed(texts)

#     # 提取嵌入并加回到 courses_texts 字典中
#     for i, jxbid in enumerate(courses_texts):
#         # 提取对应的 embedding
#         embedding = outputs[i].outputs.embedding
        
#         # 将 embedding 添加到 courses_texts 中，每个 jxbid 对应一个 embedding
#         courses_texts[jxbid]["embedding"] = embedding

#     return courses_texts


def cluster_courses(
    courses_texts: Dict,
    n_clusters: int,
    random_state: int = 42,
) -> Dict[str, List[str]]:
    """
    对课程向量进行聚类，使用 courses_texts 中的 embedding 信息。
    
    Args:
        courses_texts: 字典，键为 jxbid，值为包含 'embedding' 的字典
        n_clusters: 聚类数量
        random_state: 随机种子
    
    Returns:
        字典，格式为 {"第一类": [第一类教学班id的列表], "第二类": [...], ...}
    """
    if len(courses_texts) == 0:
        raise ValueError("课程字典为空，无法聚类")

    if n_clusters <= 0 or n_clusters > len(courses_texts):
        raise ValueError(f"n_clusters 必须在 1 到 课程数量({len(courses_texts)}) 之间")

    # 提取所有 jxbid 和对应的 embedding，保持顺序
    jxbids = list(courses_texts.keys())
    embeddings_list = []
    print(f"开始聚类")
    
    for jxbid in jxbids:
        if "embedding" not in courses_texts[jxbid]:
            raise ValueError(f"教学班 {jxbid} 缺少 embedding 信息")
        embedding = courses_texts[jxbid]["embedding"]
        # embedding 可能是 list 或 numpy array，统一转为 numpy array
        if isinstance(embedding, list):
            embedding = np.array(embedding)
        embeddings_list.append(embedding)
    
    # 转换为 numpy array 用于 K-means
    embeddings_array = np.array(embeddings_list)
    
    # 使用 K-means 进行聚类
    kmeans = KMeans(n_clusters=n_clusters, random_state=random_state)
    labels = kmeans.fit_predict(embeddings_array)
    
    # 组织结果：{"第一类": [jxbid列表], "第二类": [jxbid列表], ...}
    cluster_dict = {}
    for i, (jxbid, label) in enumerate(zip(jxbids, labels)):
        cluster_name = f"第{label + 1}类"
        if cluster_name not in cluster_dict:
            cluster_dict[cluster_name] = []
        cluster_dict[cluster_name].append(jxbid)
    
    return cluster_dict


def cluster_courses_from_excel(
    excel_path: str,
    weeks: int,
    days: int,
    periods: int,
    n_clusters: int,
    model_name: str = "Qwen/Qwen3-Embedding-0.6B",
    save_path: str | None = None,
) -> Tuple[List[Course], Dict, Dict[str, List[str]]]:
    """
    一步完成：
        1. 从 Excel 读取课程（复用 Basic_Data.load_courses）
        2. 调用 embedding 模型得到向量
        3. 使用 KMeans 聚类
        4. 可选：把聚类结果存成 Excel

    返回：(courses, courses_texts, cluster_dict)
        courses: 课程对象列表
        courses_texts: 包含 embedding 的字典
        cluster_dict: 聚类结果字典，格式为 {"第一类": [jxbid列表], ...}
    """
    # 1. 读取课程
    courses = load_courses(excel_path, weeks=weeks, days=days, periods=periods)
    # 初步筛选出该组合的教学班和教室（校区设为None）
    jxbid_dict = pre_selection_courses(courses)

    courses_texts={}
    for jxbid in jxbid_dict:
          
        course_text=build_course_text(jxbid, courses)
        # 使用 jxbid 作为字典的键，值是包含 'jxbid' 和 'course_text' 的字典
        courses_texts[jxbid] = {
            "jxbid": jxbid,
            "course_text": course_text
        }

      
    # 2. 得到 embedding（使用 OpenAI client 批量获取）
    courses_texts = get_course_embeddings_by_client(courses_texts, model_name=model_name)

    # 3. 聚类
    cluster_dict = cluster_courses(courses_texts, n_clusters=n_clusters)

    # 4. 可选：保存到 Excel
    if save_path is not None:
        # 将聚类结果转换为 DataFrame 格式保存
        data = []
        for cluster_name, jxbids in cluster_dict.items():
            for jxbid in jxbids:
                data.append({
                    "JXBID": jxbid,
                    "类别": cluster_name,
                    "embedding":courses_texts[jxbid]["embedding"]
                })
        result_df = pd.DataFrame(data)
        result_df.to_excel(save_path, index=False)

    return courses, courses_texts, cluster_dict


if __name__ == "__main__":
    """
    简单示例：在排课前先对课程做聚类。
    根据你本地的实际 Excel 路径修改 excel_path / save_path。
    """
    root_path = '智能排课基础数据'
    excel_path = os.path.join(root_path, '课程表2025-2026-1.xlsx')


    weeks, days, periods = 17, 7, 11
    n_clusters = 90
    save_path = os.path.join("排课结果", "课程聚类结果.xlsx")
    os.makedirs(os.path.dirname(save_path), exist_ok=True)

    courses, courses_texts, cluster_dict = cluster_courses_from_excel(
        excel_path=excel_path,
        weeks=weeks,
        days=days,
        periods=periods,
        n_clusters=n_clusters,
        save_path=save_path,
    )

    print(f"共 {len(courses_texts)} 个教学班，已聚成 {n_clusters} 类，结果已保存到: {save_path}")
    print("\n聚类结果：")
    for cluster_name, jxbids in cluster_dict.items():
        print(f"{cluster_name}: {len(jxbids)} 个教学班")


