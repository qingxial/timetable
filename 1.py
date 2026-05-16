# from openai import OpenAI

# client = OpenAI(
#     base_url='https://api-inference.modelscope.cn/v1',
#     api_key='ms-7c42ddc8-2c92-40e2-af21-a3fa92c6825d', # ModelScope Token
# )

# response = client.embeddings.create(
#     model='Qwen/Qwen3-Embedding-0.6B', # ModelScope Model-Id, required
#     input=['你好','世界'],
#     encoding_format="float"
# )

# #print(response.data)
# embeddings_list = [item.embedding for item in response.data]
# print(f"embedding_list[0]: {embeddings_list[0]}")
# print(f"embedding_list[1]: {embeddings_list[1]}")

# Requires transformers>=4.51.0
# Requires sentence-transformers>=2.7.0
import torch
from sentence_transformers import SentenceTransformer
from transformers import AutoModel, AutoTokenizer

import os

download_path = './my_models'
os.makedirs(download_path, exist_ok=True)
# 自动检测设备，选择 CUDA
device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
# model_name = "Qwen/Qwen3-Embedding-0.6B"
# model_tf = AutoModel.from_pretrained(model_name, 
#     config={"attn_implementation": "flash_attention_2", "device_map": "auto"})
# tokenizer_tf = AutoTokenizer.from_pretrained(model_name, padding_side="left",cache_folder=download_path)


# 将模型和分词器传递给 SentenceTransformer
# model = SentenceTransformer(
#     "Qwen/Qwen3-Embedding-0.6B"
# )

# Load the model
#model = SentenceTransformer("Qwen/Qwen3-Embedding-0.6B", cache_folder=download_path)

# We recommend enabling flash_attention_2 for better acceleration and memory saving,
# together with setting `padding_side` to "left":
model = SentenceTransformer(
    "Qwen/Qwen3-Embedding-0.6B",
    cache_folder=download_path,
    model_kwargs={"attn_implementation": "flash_attention_2"},
    tokenizer_kwargs={"padding_side": "left"}
)


# # 显式将模型转换为 float16
model = model.to(device)#.half()  # 将模型转换为float16




# The queries and documents to embed
queries = [
    "What is the capital of China?",
    "Explain gravity",
]
documents = [
    "The capital of China is Beijing.",
    "Gravity is a force that attracts two bodies towards each other. It gives weight to physical objects and is responsible for the movement of planets around the sun.",
]

# Encode the queries and documents. Note that queries benefit from using a prompt
# Here we use the prompt called "query" stored under `model.prompts`, but you can
# also pass your own prompt via the `prompt` argument
with torch.autocast(device_type='cuda', dtype=torch.float16):
    query_embeddings = model.encode(queries, prompt_name="query", device='cuda')
    document_embeddings = model.encode(documents, device='cuda')

# Compute the (cosine) similarity between the query and document embeddings
similarity = model.similarity(query_embeddings, document_embeddings)
print(f"query_embeddings: {query_embeddings}")
print(f"document_embeddings: {document_embeddings}")
print(similarity)
# tensor([[0.7646, 0.1414],
#         [0.1355, 0.6000]])