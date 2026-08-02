# 高校智能排课系统（毕设）

实现高校排课全流程：基础数据整理 → 排课要求文本解析 → 排课前检查（规则+LLM）→ 首轮排课 → 调课调整 → 失败归因（规则+LLM）→ 结果分析与可视化。

输入目录为 `智能排课基础数据/`，输出目录为 `排课结果/`。

## 一键运行全流程

```bash
python run_pipeline.py              # 数据校验→检查→排课→调课→归因→可视化→报告
python run_pipeline.py --no-llm     # 跳过两个 LLM 环节
python run_pipeline.py --from-stage reschedule   # 已有首轮结果，从调课续跑
python run_pipeline.py --list       # 查看全部环节
```

## 目录结构

```
timetable/
├── 核心排课代码（必须保持在根目录同级，相互之间有 import 依赖）
│   ├── Basic_Data.py                 数据模型与 Excel 加载（Course/Classroom/Teacher/Class）
│   ├── tryloadlimit.py               时间约束文本解析、天-节次模式生成、时段可用性检查
│   ├── utils1.py                     教室筛选、单班排课、整轮排课、结果导出
│   ├── utils_for_reschedule.py       时间表 pkl 保存/加载/恢复、调课腾挪操作、利用率统计
│   ├── reschedule_by_adjusting.py    调课算法主模块（可直接运行）
│   └── test_for_school.py            ★ 首轮排课主入口（可直接运行）
│
├── 数据准备与需求解析
│   ├── course_schedule_pipeline.py   构建待排课程表 pipeline Excel
│   ├── specialneeds.py               LLM 解析排课要求文本(PKYQMS) → timelimit.xlsx
│   ├── course_clustering.py          课程语义嵌入 + K-means 聚类（排课分批依据）
│   └── llm_classroom_static_remediation.py  教室静态筛选无解时的 LLM 修改建议
│
├── 流程编排与 LLM 增强
│   ├── run_pipeline.py               ★ 一键全流程入口
│   ├── llm_api.py                    统一 LLM 客户端（DashScope 兼容模式）
│   ├── llm_preflight_check.py        LLM 排课前合理性复核（规则版证据 → 语义推理）
│   └── llm_failure_analysis.py       LLM 失败归因与调整建议（构成"归因—调整—重排"闭环）
│
├── 检查与分析
│   ├── preflight_schedule_checks.py  排课前数据合理性检查（规则版）
│   ├── postfailure_analysis.py       排课/调课失败原因归因分析（规则版）
│   ├── special_requirements_stats.py 特殊需求课程排课成功率统计
│   ├── scheduling_visualization.py   教室/教师/班级利用率等可视化图表
│   ├── analysis_report.py            排课结果综合分析（图表）
│   └── generate_report.py            生成 HTML 综合分析报告（注意：读取「排课结果 copy/」目录）
│
├── scripts/                          工具与一次性脚本
│   ├── 补全教师表.py                  教师表按 JSH 去重 + 补全课程表中缺失教师（流水线 fix-teachers 环）
│   ├── validate_converted_data.py    校验四张基础数据表格式；ZXS 异常课程自动导出待教务确认清单
│   └── 补充体育课避免排课时间.py
├── .claude/skills/data-convert/      数据转换 skill：任意新数据 → 四张标准表
├── docs/                             设计笔记
│   └── 地理位置需求解析策略（先RAG再召回）.md
│
├── 智能排课基础数据/                  ★ 输入数据（Excel），勿删
│   └── 提取的基础数据表_converted/    当前排课使用的课程/教师/教室/班级四张表
├── 排课结果/                          ★ 当前排课与调课输出（含 saved_timetables.pkl、
│                                       reschedule_timetables.pkl、排课分析报告.html），勿删
├── 排课结果1/                         ★ 历史一轮结果备份（含课程聚类结果、排课前检查报告），勿删
└── 排课要求和基础数据_v1.0.xlsx       原始需求与基础数据汇总
```

## 核心代码依赖关系

```
Basic_Data.py（数据模型，最底层）
    ↑
tryloadlimit.py ←→ utils1.py（互相导入，不可拆开）
    ↑
utils_for_reschedule.py（pkl 读写 + 调课工具）
    ↑
reschedule_by_adjusting.py（调课算法）
    ↑
test_for_school.py（首轮排课主入口）
```

注意：核心模块之间使用 `from xxx import *` 的平铺导入，且 `utils1.py` 与
`tryloadlimit.py` 循环依赖，**请勿将这些 .py 文件移入子目录**，否则会破坏导入。
`reschedule_by_adjusting.py` 顶部用 `__file__` 定位数据目录，数据文件夹也需保持现有位置。

## 完整运行流程

推荐直接用 `python run_pipeline.py` 一键跑完。各环节也可单独执行：

```bash
# 0. 安装依赖（推荐 Python 3.10+）
pip install -r requirements.txt
# 可选：pip install sentence-transformers torch xlrd

# 1. 构建待排课程表
python course_schedule_pipeline.py

# 2. LLM 解析排课要求文本（需配置 DASHSCOPE_API_KEY）
python specialneeds.py --input ./智能排课基础数据/课程表2025-2026-1_pipeline.xlsx \
  --classroom ./智能排课基础数据/更新后的教室表.xlsx \
  --output ./智能排课基础数据/timelimit.xlsx

# 3. 数据格式校验 + 排课前检查（规则版 + LLM 版）
python scripts/validate_converted_data.py
python llm_preflight_check.py          # 内部会先跑规则版并落盘对照报告
python llm_preflight_check.py --dry-run --max-courses 10   # 调试：只看送给 LLM 的证据包

# 4. 首轮排课（输出 排课结果/排课结果_全部.xlsx、saved_timetables.pkl 等）
python test_for_school.py
# 放置策略消融开关（默认 first=原始方案；详见 docs/排课放置策略调研与设计.md）：
python test_for_school.py --strategy balanced   # 负载均衡放置（时段均衡+教室容量贴合）
python test_for_school.py --strategy random     # GRASP 式随机放置（去倾向性基线）

# 5. 调课调整（输出 排课结果/调课后的整体结果.xlsx、reschedule_timetables.pkl 等）
python reschedule_by_adjusting.py

# 6. 失败归因（规则版 + LLM 版，LLM 版内部会先跑规则版）
python llm_failure_analysis.py
python llm_failure_analysis.py --max-courses 5 --dry-run    # 调试

# 7. 统计 / 可视化 / 报告
python special_requirements_stats.py --no-show
python scheduling_visualization.py all --charts-subdir 图表展示
python analysis_report.py
```

所有脚本都需在**项目根目录**下运行（相对路径以根目录为基准）。

## 关键输出文件（中期汇报素材）

| 文件 | 说明 |
|---|---|
| `排课结果/排课结果_全部.xlsx` | 首轮排课成功的全部教学班 |
| `排课结果/排课失败课程_全部.xlsx` | 首轮排课失败的教学班 |
| `排课结果/调课后的整体结果.xlsx` | 调课后的最终整体结果 |
| `排课结果/失败课程归因与建议.xlsx` | 失败原因归因分析 |
| `排课结果/saved_timetables.pkl` | 首轮排课后的时间表状态（教师/教室/班级占用矩阵） |
| `排课结果/reschedule_timetables.pkl` | 调课后的时间表状态 |
| `排课结果/排课分析报告.html` | HTML 综合分析报告 |
| `排课结果/图表展示/` | 可视化图表 |
| `排课结果1/课程聚类结果2.xlsx` | 课程聚类结果（analysis_report.py 仍引用此处） |
| `排课结果/排课前检查报告.xlsx` | 规则版排课前检查 |
| `排课结果/LLM排课前检查报告.xlsx` | LLM 排课前合理性复核（风险等级/隐性矛盾/建议） |
| `排课结果/LLM失败归因与建议.xlsx` | LLM 失败归因（主因分类/自然语言解释/分条建议） |
| `排课结果/llm_preflight_raw.json`、`llm_failure_raw.json` | LLM 原始输入输出留档（论文消融实验复现用） |
| `排课结果/周学时异常课程_待教务确认.xlsx` | ZXS 不在 (0,8] 的课程全字段清单（这些课被排课程序静默跳过，需教务逐条确认处理方式） |

## 环境变量

- `DASHSCOPE_API_KEY` / `DASHSCOPE_BASE_URL` / `DASHSCOPE_MODEL`：`specialneeds.py`、`llm_api.py`
  （即 LLM 排课前检查与失败归因）使用；未设置时 `llm_api.py` 回退到 `specialneeds.py` 中的配置
- `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL`：`llm_classroom_static_remediation.py` 使用

## 已知事项

- `generate_report.py` 读取的 `排课结果 copy/` 目录当前不存在，运行前需准备该目录
  或将 `DATA_DIR` 改为 `排课结果/`；`analysis_report.py` 直接读取 `排课结果/`，可正常使用。
- 旧入口 `test_new.py`、临时脚本 `1.py`、运行日志 `log.txt`/`log2.txt` 已删除，
  如需找回可用 `git checkout a7c4aab -- <文件名>` 从 git 历史恢复。
