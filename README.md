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
## 未排课程的诊断与参数化修复

`scripts/repair_courses.py` 无需 pickle，可从 Excel 重建已知资源，在显式授权范围内完整补排、少量挪课并校验回滚。`scripts/timetable_agent_tools.py` 提供十一个确定性工具：诊断、逐课行动计划、更正预检、试排、分阶段补排、保留旧成功的扩域续排及失败重试、方案比较、有限域联合优化、质量预算比较及 QuickXplain 冲突解释。无需模型 API，不改源 Excel。

2026-09-25 已核验的原162门失败集合，经17项显式修正和已授权日间/晚间/周末试排，完整补入123门、原课移动0门，剩39门（班级映射16、静态无匹配教室23）。17项修正包括16份保持 LLXS 与原教学周的整数周负荷计划，以及隔离运动生理学两条多余缺周记录；这些负荷分配是候选计划，不是唯一源数据真值。新增课已知资源冲突为0，不代表输入4504个已排ID均已完成历史课时与冲突校验。

另340个跳过ID分为274个 `SFXYPK=0`（先确认是否需要排课）、57个高周学时（已显式扩域续排）、9个零周学时（总学时为2，待确认具体授课周）。不要都当作失败，也不要逐个虚班重复累计继承的母课 LLXS。容量更正必须有冻结人数证据；替换班级需要真实映射。用户逐课明确授权按无班级处理时，可审计关闭相应班级/学生冲突检查，并披露未验证范围，教师及教室检查保持。

2026-09-25已核验的续排在保持原123门安排的基础上，从57门高周学时课新增53门，余4门高水平运动队缺班级映射。共219个试排目标补176、剩43（静态教室23、班级映射20），0门原课移动；另追加3份版式设计11+11+10候选计划，共20项审计更正。4680是结果中有记录ID数，不代表全校历史课时都已验证。

2026-09-26最新候选：按逐课授权免查20门班级/学生冲突，保留冻结种子的176门成功及4680个有记录ID安排，再补入8门。219个目标现为184门技术候选、35门未排（33门首报教室、2门学时矛盾且同时缺教室），原课移动0门。20门全部有审计，16门检查→免查、4门原已免查；源Excel未改，未自动减人数。184门在仍开启的检查下独立复验新增冲突0，但4门新排高水平队的源人数/场地要求不完整，当前分配场地与运动项目不符，仍须校核后才能发布；不能声称这20门学生无冲突。详见 [20门处理结果与剩余35门](docs/20门班级免查与剩余35门.md)。

默认支持周负荷至8、一日一块；本批集中授课用 `max_weekly_hours: 18`、`max_blocks_per_day: 4` 和0移动固定占用预筛选，保留禁排与资源约束。精确文本“仅周六周日排课”按硬要求识别，偏好调整不会解除它。`extend_repair_proposal` 验证既有方案后保留其成功安排，再处理显式新增ID，原失败源表保持不变。联合优化与质量比较是真实有限域算法，并非论文 MIP 复现；QuickXplain 使用严格三值oracle，搜索失败或UNKNOWN不能写成现实无解。

- [参数化排课修复工具](docs/参数化排课修复工具.md)：输入、参数、集中授课与验证范围。
- [Agent排课工具接口](docs/Agent排课工具接口.md)：十一个工具、六种审计更正、续排和结果语义。
- [排课修复方法论](docs/排课修复方法论.md)：按错误模式选择动作，区分数据、教学计划、模型与算法，并定义独立验收。
- [排课修复skill](.agents/skills/timetable-repair/SKILL.md)：按需读取错误模式、调用配方和有时间范围的案例；可用于新批次，避免沿用旧名单与统计。
- [三篇论文接入设计](docs/三篇论文接入设计.md)：当前实际实现、私人教练单课验证与未完成的批量论文实验。
- [剩余课程诊断与补排实验](docs/剩余课程诊断与补排实验.md)、[74门复核与日间73门说明](docs/74门复核与当前73门说明.md)、[本轮修复结果与工具说明](docs/本轮修复结果与工具说明.md)：早期诊断、日间89/73和晚间96/66的历史场景，不能当作最新219目标184/35的原因清单。

班级免查重试前的联合验证与两步复现配置见 [本轮周末与集中授课修复结果](docs/本轮周末与集中授课修复结果.md)。
