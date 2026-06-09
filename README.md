# 高校智能排课项目

## 项目简介

该仓库实现了高校排课全流程的数据整理、需求解析、可行性检查、调度后分析和可视化。

核心目标是：

- 将原始教学任务、选课课程、教室与教师资源整理成可排课的数据格式；
- 从教师填写的排课要求中抽取结构化时间与地点约束；
- 对排课前的数据合理性进行预检查；
- 分析失败原因并辅助调课；
- 生成排课结果与教室/教师利用率可视化报告。

该项目以 `智能排课基础数据/` 为输入目录，以 `排课结果/` 为默认输出目录。

## 目录结构

- `course_schedule_pipeline.py`：构建待排课程表 pipeline Excel 的脚本。
- `specialneeds.py`：从 `PKYQMS` 文本中抽取时间偏好、禁止时段、教学楼/教室约束；使用 LLM 生成结构化结果。
- `preflight_schedule_checks.py`：排课前需求合理性检查，校验课程、教师、教室、班级数据是否符合排课条件。
- `postfailure_analysis.py`：排课/调课失败原因分析与归因建议。
- `llm_classroom_static_remediation.py`：当教室静态筛选无可行教室时，调用 LLM 给出可落地的修改建议。
- `special_requirements_stats.py`：统计特殊需求课程的排课成功率并生成图表。
- `scheduling_visualization.py`：生成教室、教师、班级利用率和其他可视化报表。
- `utils_for_reschedule.py`：调课/排课结果加载、保存与时间表辅助函数。
- `Basic_Data.py`：课程、教室、教师、班级数据模型与加载函数。
- `course_clustering.py`：基于文本嵌入的课程聚类与相似度分析。
- `reschedule_by_adjusting.py`：调课调整算法主实现；`reschedule_by_adjusting_oldversion.py` 为旧版本备份。
- `原始excel处理.py`：原始 Excel 数据预处理脚本。
- 其他测试/实验脚本：`test_new.py`、`test_old.py`、`1.py` 等。

## 关键数据文件

- `智能排课基础数据/课程表2025-2026-1_pipeline.xlsx`：pipeline 后用于后续需求解析与排课的数据表。
- `智能排课基础数据/更新后的教室表.xlsx`：教室资源表，包含教室代码、教学楼、校区、容量、类型等字段。
- `智能排课基础数据/更新后的教师名单2025-2026-1.xlsx`：教师信息。
- `智能排课基础数据/班级汇总2025-2026-1.xlsx`：班级信息。
- `智能排课基础数据/课程教室对照表.xlsx`：历史教室映射表。
- `排课结果/`：默认排课和调课结果输出目录。
- `排课结果/图表展示/`：可视化图表输出目录。

## 安装依赖

推荐使用 Python 3.11+。

```bash
pip install -r requirements.txt
```

### 可选依赖

某些脚本需要额外依赖：

```bash
pip install openai sentence-transformers torch xlrd
```

## 环境变量

- `DASHSCOPE_API_KEY`：`specialneeds.py` 读取 LLM API 密钥。
- `DASHSCOPE_BASE_URL`：可选，LLM API 基础地址。
- `DASHSCOPE_MODEL`：可选，LLM 模型名称，默认 `qwen3.5-35b-a3b`。
- `OPENAI_API_KEY`：`llm_classroom_static_remediation.py` 及其它 OpenAI 接口调用时使用。
- `OPENAI_BASE_URL`、`OPENAI_MODEL`：可选，OpenAI API 地址与模型。

> 注意：`specialneeds.py` 当前由 `DASHSCOPE_API_KEY` 驱动，若未设置会抛出错误；建议在运行前配置好环境变量。

## 主要使用说明

### 1. 构建课程排课 pipeline

```bash
python course_schedule_pipeline.py --output ./智能排课基础数据/课程表2025-2026-1_pipeline.xlsx
```

如果不传入参数，脚本会使用默认路径下的原始文件。

### 2. 解析特殊需求文本

```bash
python specialneeds.py --input ./智能排课基础数据/课程表2025-2026-1_pipeline.xlsx \
  --classroom ./智能排课基础数据/更新后的教室表.xlsx \
  --output ./智能排课基础数据/timelimit.xlsx
```

#### 常用参数

- `--max-rows 100`：只处理前 100 行数据，适合调试与节省资源。
- `--sample-texts`：打印前 30 个非空 `PKYQMS` 文本并退出。

如果当前行的 `PKYQMS` 为空，`specialneeds.py` 会跳过 LLM 调用，直接写入空结果；这有助于节省 API 资源。

### 3. 排课前合理性检查

```bash
python preflight_schedule_checks.py --root 智能排课基础数据 --output 排课结果/排课前检查报告.xlsx
```

常用参数：

- `--course`：课程表文件
- `--teacher`：教师表文件
- `--classroom`：教室表文件
- `--banji`：班级表文件
- `--weeks`、`--days`、`--periods`：时间表维度
- `--combinations`：筛选条件组合文件

### 4. 调课与失败分析

- `postfailure_analysis.py`：分析失败原因并输出建议。
- `reschedule_by_adjusting.py`：调课算法与调整策略实现。
- `utils_for_reschedule.py`：保存、加载排课/调课时的 `pkl` 对象。

### 5. 特殊需求统计

```bash
python special_requirements_stats.py --no-show
```

该脚本统计带有特殊要求的教学班是否已成功排课，并生成统计图表。

### 6. 可视化结果

```bash
python scheduling_visualization.py all --charts-subdir 图表展示
```

可生成教室利用率、教师利用率、时间段负载等图表，输出到 `排课结果/图表展示/`。

### 7. LLM 辅助教室 remediation

```bash
python llm_classroom_static_remediation.py
```

该脚本用于当严格条件下无可用教室时，分析当前课程行并给出可行的修改建议。

## 其他说明

- `Basic_Data.py` 提供了课程、教室、教师、班级模型及 Excel 读取器。
- `utils_for_reschedule.py` 负责调课结果的读写与备份恢复。
- `course_clustering.py` 通过文本嵌入做课程聚类与语义分析，需额外安装 `sentence-transformers` 和 `torch`。
- `原始excel处理.py` 用于原始 Excel 数据的预处理与清洗。

## 运行建议

1. 准备 `智能排课基础数据/` 下的原始输入文件；
2. 先运行 `course_schedule_pipeline.py` 生成 pipeline 数据；
3. 用 `specialneeds.py` 解析 `PKYQMS` 生成时间/地点约束；
4. 运行 `preflight_schedule_checks.py` 做排课前检查；
5. 完成调度后，用 `special_requirements_stats.py` 和 `scheduling_visualization.py` 生成分析结果。

## 版权与备注

本仓库为高校排课研究与项目实现，适用于课程需求解析、教室资源匹配与调度分析。请根据实际数据路径与机构规范调整脚本参数。
