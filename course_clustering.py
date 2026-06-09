
"""
课程聚类模块
用于基于课程属性的语义嵌入，对教学班进行 K-means 聚类。

核心导出接口：
  - cluster_courses_from_excel(excel_path, weeks, days, periods, n_clusters, **kwargs)
  - ClusteringConfig 配置类
  - get_course_embeddings_by_client()
  - cluster_courses()


  ###导入示例:
  # 基础导入
from course_clustering import (
    ClusteringConfig,           # 聚类配置类
    cluster_courses_from_excel, # 端到端聚类函数（最常用）
    cluster_courses,            # 对已有嵌入进行聚类
    get_course_embeddings_by_client,  # 仅获取嵌入
    get_embedding_model,        # 获取嵌入模型（如需直接使用）
)

# 方式 1: 使用默认配置（最简单）
courses, courses_texts, cluster_dict = cluster_courses_from_excel(
    excel_path="./智能排课基础数据/课程表.xlsx",
    weeks=17,
    days=7,
    periods=11,
    n_clusters=90,
    save_path="./排课结果/聚类结果.xlsx"
)

# 方式 2: 自定义配置
config = ClusteringConfig(
    model_name="Qwen/Qwen3-Embedding-0.6B",
    batch_size=10,
    device="auto",
    use_flash_attention=True,
)

courses, courses_texts, cluster_dict = cluster_courses_from_excel(
    excel_path="./智能排课基础数据/课程表.xlsx",
    weeks=17,
    days=7,
    periods=11,
    n_clusters=90,
    config=config,
    course_type="必修",  # 可选：按课程类别筛选
    department="计算机学院",  # 可选：按开课单位筛选
)
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import List, Tuple, Dict, Optional
import os
import pickle
import torch
from sklearn.cluster import KMeans
from sentence_transformers import SentenceTransformer

from Basic_Data import Course, load_courses


# ==================== 配置类 ====================

@dataclass
class ClusteringConfig:
    """聚类配置"""
    model_name: str = "Qwen/Qwen3-Embedding-0.6B"
    model_cache_folder: str = "./my_models"
    batch_size: int = 10
    device: str = "auto"  # "auto", "cuda", "cpu"
    use_flash_attention: bool = True
    
    def __post_init__(self):
        """初始化配置后自动创建缓存目录"""
        os.makedirs(self.model_cache_folder, exist_ok=True)
        if self.device == "auto":
            self.device = "cuda:0" if torch.cuda.is_available() else "cpu"


# ==================== 全局模型管理 ====================

_embedding_model = None
_current_config = None


def initialize_embedding_model(config: Optional[ClusteringConfig] = None) -> SentenceTransformer:
    """
    初始化或获取嵌入模型。
    使用全局缓存以避免重复加载。
    
    Args:
        config: 聚类配置对象，如果为 None 则使用默认配置
    
    Returns:
        SentenceTransformer 模型对象
    """
    global _embedding_model, _current_config
    
    if config is None:
        config = ClusteringConfig()
    
    # 如果已加载且配置相同，直接返回
    if _embedding_model is not None and _current_config == config:
        return _embedding_model
    
    # 加载新模型
    model_kwargs = {
        "attn_implementation": "flash_attention_2" if config.use_flash_attention else "sdpa"
    }
    tokenizer_kwargs = {"padding_side": "left"}
    
    _embedding_model = SentenceTransformer(
        config.model_name,
        cache_folder=config.model_cache_folder,
        model_kwargs=model_kwargs,
        tokenizer_kwargs=tokenizer_kwargs
    )
    _embedding_model = _embedding_model.to(config.device)
    _current_config = config
    
    return _embedding_model


def get_embedding_model(config: Optional[ClusteringConfig] = None) -> SentenceTransformer:
    """获取嵌入模型（懒加载）"""
    return initialize_embedding_model(config)



# ==================== 嵌入与聚类 ====================

def get_batch_embeddings(
    texts: List[str],
    config: Optional[ClusteringConfig] = None,
) -> List[np.ndarray]:
    """
    批量获取文本嵌入。
    
    Args:
        texts: 文本列表
        config: 聚类配置
    
    Returns:
        嵌入列表，每个元素是 numpy array
    """
    if config is None:
        config = ClusteringConfig()
    
    model = get_embedding_model(config)
    all_embeddings = []
    
    for i in range(0, len(texts), config.batch_size):
        batch_texts = texts[i:i + config.batch_size]
        try:
            with torch.autocast(device_type='cuda' if 'cuda' in config.device else 'cpu', dtype=torch.float16):
                batch_embeddings = model.encode(
                    batch_texts,
                    prompt_name="cluster",
                    device=config.device
                )
            all_embeddings.extend(batch_embeddings)
            print(f"  嵌入进度: {min(i + config.batch_size, len(texts))}/{len(texts)}")
        except Exception as e:
            print(f"  ❌ 处理第 {i//config.batch_size + 1} 批时出错: {e}")
            raise
    
    return all_embeddings




def get_course_embeddings_by_client(
    courses_texts: Dict[str, Dict],
    config: Optional[ClusteringConfig] = None,
) -> Dict[str, Dict]:
    """
    批量为课程获取嵌入，并更新 courses_texts 字典。
    
    Args:
        courses_texts: 字典，键为 jxbid，值为包含 'course_text' 的字典
        config: 聚类配置
    
    Returns:
        更新后的 courses_texts 字典，每个 jxbid 对应的字典中增加了 'embedding' 字段
    """
    if config is None:
        config = ClusteringConfig()
    
    jxbids = list(courses_texts.keys())
    texts = [courses_texts[jxbid]["course_text"] for jxbid in jxbids]
    
    print(f"获取 {len(texts)} 个课程的嵌入...")
    embeddings_list = get_batch_embeddings(texts, config=config)
    
    # 将嵌入添加回字典
    for jxbid, embedding in zip(jxbids, embeddings_list):
        courses_texts[jxbid]["embedding"] = embedding
    
    return courses_texts




def pre_selection_courses(
    courses: List[Course],
    course_type: Optional[str] = None,
    department: Optional[str] = None,
    campus: Optional[str] = None
) -> List[str]:
    """
    从课程列表中筛选有效教学班 JXBID。
    
    筛选条件：
      - SFXYPK == '1' (需要排课)
      - SFXYJAS == '1' (需要安排教室)
      - RWJSZCDM 不为 None (有授课周次)
      - KCLB/SKXQ/SKZCDM 不为 None (课程类别/校区/周次不为空)
      - SKZCDM != '0000000000000000' (周次代码有效)
      - 0 < ZXS <= 8 (周学时在 1-8 节)
    
    Args:
        courses: Course 对象列表
        course_type: 可选，课程类别代码
        department: 可选，开课单位名称
        campus: 可选，上课校区
    
    Returns:
        有效教学班 JXBID 列表（已去重，保持顺序）
    """
    valid_jxbids = [
        jxb.JXBID 
        for jxb in courses 
        if jxb.SFXYPK == '1' and jxb.SFXYJAS == '1' and jxb.RWJSZCDM is not None and 
            jxb.KCLB is not None and jxb.SKXQ is not None and jxb.SKZCDM is not None and 
            jxb.SKZCDM != '0000000000000000' and jxb.ZXS is not None and 
            0 < jxb.ZXS <= 8 and
            (course_type is None or jxb.KCLB == course_type) and
            (department is None or jxb.YXMC == department) and
            (campus is None or jxb.SKXQ == campus)
    ]
    
    # 去重，保持顺序
    return list(dict.fromkeys(valid_jxbids))



def build_course_text(jxbid: str, courses: List[Course]) -> str:
    """
    将课程对象转换为文本表示，用于语义嵌入。
    
    格式为："属性名: 属性值；属性名: 属性值；..."
    
    Args:
        jxbid: 教学班 ID
        courses: 所有课程对象列表
    
    Returns:
        课程文本表示
    """
    from utils1 import get_consecutive_jxbid
    
    jxbs = get_consecutive_jxbid(jxbid, courses)
    parts = []
    
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
    
    teachers = []
    for jxb in jxbs:
        str_teacher = ""
        if jxb.XM is not None:
            str_teacher = f"教师姓名:{jxb.XM}"
        if jxb.JSH is not None:
            str_teacher += f"，教师编号:{jxb.JSH}"
        if jxb.RWJSZCDM is not None:
            str_teacher += f"，教师周次代码:{jxb.RWJSZCDM}"
        if str_teacher:
            teachers.append(str_teacher)
    
    if teachers:
        parts.append(f"任课教师信息:{"，".join(teachers)}")
    
    return "；".join(parts)
    




def cluster_courses(
    courses_texts: Dict[str, Dict],
    n_clusters: int,
    random_state: int = 42,
) -> Dict[str, List[str]]:
    """
    对课程向量进行 K-means 聚类。
    
    Args:
        courses_texts: 字典，键为 jxbid，值为包含 'embedding' 的字典
        n_clusters: 聚类数量
        random_state: 随机种子
    
    Returns:
        字典，格式为 {"第一类": [jxbid列表], "第二类": [...], ...}
    
    Raises:
        ValueError: 如果参数或数据有效性检查失败
    """
    if len(courses_texts) == 0:
        raise ValueError("课程字典为空，无法聚类")

    if n_clusters <= 0 or n_clusters > len(courses_texts):
        raise ValueError(f"n_clusters 必须在 1 到 课程数量({len(courses_texts)}) 之间")

    # 提取所有 jxbid 和对应的 embedding
    jxbids = list(courses_texts.keys())
    embeddings_list = []
    
    print(f"开始聚类 {len(jxbids)} 个课程到 {n_clusters} 类...")
    
    for jxbid in jxbids:
        if "embedding" not in courses_texts[jxbid]:
            raise ValueError(f"教学班 {jxbid} 缺少 embedding 信息")
        
        embedding = courses_texts[jxbid]["embedding"]
        # embedding 可能是 list、numpy array 或其他格式，统一转为 numpy array
        if not isinstance(embedding, np.ndarray):
            embedding = np.array(embedding)
        embeddings_list.append(embedding)
    
    # 转换为 numpy array 用于 K-means
    embeddings_array = np.array(embeddings_list)
    
    # 使用 K-means 进行聚类
    kmeans = KMeans(n_clusters=n_clusters, random_state=random_state, n_init=10)
    labels = kmeans.fit_predict(embeddings_array)
    
    # 组织结果
    cluster_dict = {}
    for i, (jxbid, label) in enumerate(zip(jxbids, labels)):
        cluster_name = f"第{label + 1}类"
        if cluster_name not in cluster_dict:
            cluster_dict[cluster_name] = []
        cluster_dict[cluster_name].append(jxbid)
    
    return cluster_dict




# ==================== 端到端聚类流程 ====================

def cluster_courses_from_excel(
    excel_path: str,
    weeks: int,
    days: int,
    periods: int,
    n_clusters: int,
    config: Optional[ClusteringConfig] = None,
    save_path: Optional[str] = None,
    course_type: Optional[str] = None,
    department: Optional[str] = None,
    campus: Optional[str] = None,
) -> Tuple[List[Course], Dict[str, Dict], Dict[str, List[str]]]:
    """
    端到端聚类流程：读取课程 → 构建文本 → 获取嵌入 → K-means聚类 → 可选保存。
    
    Args:
        excel_path: 课程 Excel 文件路径
        weeks: 教学周数
        days: 每周天数
        periods: 每天节数
        n_clusters: 聚类数量
        config: 聚类配置（如为 None 使用默认配置）
        save_path: 可选，结果保存路径
        course_type: 可选，课程类别筛选
        department: 可选，开课单位筛选
        campus: 可选，上课校区筛选
    
    Returns:
        三元组：
          - courses: 所有课程对象列表
          - courses_texts: 包含嵌入的字典
          - cluster_dict: 聚类结果 {"第N类": [jxbid列表], ...}
    """
    if config is None:
        config = ClusteringConfig()
    
    print("=" * 60)
    print("课程聚类流程开始")
    print("=" * 60)
    
    # 1. 读取课程
    print(f"\n1️⃣ 读取课程数据: {excel_path}")
    courses = load_courses(excel_path, weeks=weeks, days=days, periods=periods)
    print(f"   加载了 {len(courses)} 个课程对象")
    
    # 2. 初步筛选教学班
    print(f"\n2️⃣ 筛选有效教学班...")
    jxbids = pre_selection_courses(courses, course_type=course_type, department=department, campus=campus)
    print(f"   筛选出 {len(jxbids)} 个有效教学班")
    
    # 3. 构建课程文本
    print(f"\n3️⃣ 构建课程文本表示...")
    courses_texts = {}
    for jxbid in jxbids:
        course_text = build_course_text(jxbid, courses)
        courses_texts[jxbid] = {
            "jxbid": jxbid,
            "course_text": course_text
        }
    print(f"   构建了 {len(courses_texts)} 个课程文本")
    
    # 4. 获取嵌入
    print(f"\n4️⃣ 获取课程嵌入 (模型: {config.model_name})...")
    courses_texts = get_course_embeddings_by_client(courses_texts, config=config)
    
    # 5. 聚类
    print(f"\n5️⃣ 执行 K-means 聚类...")
    cluster_dict = cluster_courses(courses_texts, n_clusters=n_clusters)
    print(f"   聚类完成！")
    
    # 6. 可选：保存结果
    if save_path is not None:
        print(f"\n6️⃣ 保存结果到: {save_path}")
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        
        data = []
        for cluster_name, jxbids_in_cluster in cluster_dict.items():
            for jxbid in jxbids_in_cluster:
                data.append({
                    "JXBID": jxbid,
                    "类别": cluster_name,
                })
        result_df = pd.DataFrame(data)
        result_df.to_excel(save_path, index=False)
        print(f"   ✓ 结果已保存")
    
    print("\n" + "=" * 60)
    print(f"聚类完成: {len(courses_texts)} 个教学班已聚成 {n_clusters} 类")
    print("=" * 60)
    
    return courses, courses_texts, cluster_dict




# ==================== CLI 入口 ====================

if __name__ == "__main__":
    """
    课程聚类示例。
    根据需要修改参数。
    """
    # 配置参数
    root_path = '智能排课基础数据'
    excel_path = os.path.join(root_path, '课程表2025-2026-1.xlsx')
    
    weeks, days, periods = 17, 7, 11
    n_clusters = 90
    
    save_path = os.path.join("排课结果", "课程聚类结果.xlsx")
    
    # 创建聚类配置
    config = ClusteringConfig(
        model_name="Qwen/Qwen3-Embedding-0.6B",
        model_cache_folder="./my_models",
        batch_size=10,
        device="auto",
        use_flash_attention=True,
    )
    
    # 执行聚类
    courses, courses_texts, cluster_dict = cluster_courses_from_excel(
        excel_path=excel_path,
        weeks=weeks,
        days=days,
        periods=periods,
        n_clusters=n_clusters,
        config=config,
        save_path=save_path,
    )
    
    # 打印结果摘要
    print("\n📊 聚类结果摘要:")
    for cluster_name in sorted(cluster_dict.keys()):
        jxbids = cluster_dict[cluster_name]
        print(f"  {cluster_name}: {len(jxbids)} 个教学班")


