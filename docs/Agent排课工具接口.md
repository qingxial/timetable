# Agent 排课工具接口

`scripts/timetable_agent_tools.py` 提供十个确定性工具及 JSON Schema，无需模型 SDK、网络请求或 pickle。Agent 根据已有授权选择参数、读取证据和解释结果；更正预检、候选搜索和校验由工具执行，输出是候选方案，不发布正式课表。

下表的“四个输入”指 `course_path`、`room_path`、`schedule_path`、`failed_path`；这些工具均可另传 `class_path` 和显式 `corrections`。

| 工具 | 必要输入 | 作用与限制 |
|---|---|---|
| `diagnose_remaining` | `proposal_path` | 剩余数量、逐课状态与证据；搜索失败不是无解证明 |
| `propose_repair` | 四个输入、`config` | 有界完整补排，按白名单允许少量原课程移动 |
| `compare_proposals` | 至少两个 `proposal_paths` | 同有效输入与目标集合比较，披露参数和预算差异 |
| `repair_with_fallbacks` | 四个输入、`config` | 日间、晚间、周末依次补排，保留前阶段成功安排；仅0移动 |
| `preview_data_corrections` | 四个输入、`corrections` | 检查原值与更正规则，返回审计和有效哈希，不写 Excel |
| `optimize_local_repair` | 四个输入、`config`、`optimizer_config` | 联合有限候选搜索，先最大化完整补排数，再最小化移动数及质量代价 |
| `quality_frontier` | 四个输入、`config`、`optimizer_config`、`movement_budgets` | 固定候选域及补排数，比较移动上限与质量 |
| `explain_conflict` | 四个输入、`config`、`explain_config` | 三值oracle和 QuickXplain；解释占用阻塞，不执行挪课 |
| `plan_course_corrections` | 四个输入、`proposal_path` | 逐课给原字段、候选更正、所需证据和复验条件；可传 `source_workbook`、`skipped_path` 补充源行和跳过项 |
| `extend_repair_proposal` | 四个输入、`seed_proposal_path`、`additional_target_ids`、`config` | 验证既有0移动方案，保留成功安排，再试排显式新增目标 |

调用返回 `{ok, tool, result}` 或 `{ok: false, tool, error: {code, message, ...}}`。字段及类型以 `TOOL_SCHEMAS` 为准；未知字段、非有限数字、错误类型及不支持的调整会报错。凡传 `config`，必须显式给 `allowed_changes`；严格场景用空列表。

## 调用与留档

从仓库根目录运行，使用 Python 3.10+、pandas、openpyxl：

```python
import json
from pathlib import Path
from scripts.timetable_agent_tools import TOOL_SCHEMAS, dispatch_tool

base = Path("智能排课基础数据/提取的基础数据表_converted")
sources = {
    "course_path": str(base / "课程表_split_merged.xlsx"),
    "room_path": str(base / "教室表.xlsx"),
    "schedule_path": "排课结果/调课后的整体结果_特殊放宽.xlsx",
    "failed_path": "排课结果/调课失败的排课失败课程_特殊放宽后.xlsx",
}
response = dispatch_tool("propose_repair", {
    **sources,
    "config": {"allowed_changes": [], "max_moved_courses": 0},
})
if not response["ok"]:
    raise RuntimeError(response["error"])
with open("repair-strict-response.json", "x", encoding="utf-8") as stream:
    json.dump(response, stream, ensure_ascii=False, indent=2)
```

JSON 命令行接受同一接口：

```json
{
  "name": "diagnose_remaining",
  "arguments": {"proposal_path": "repair-strict-response.json"}
}
```

```bash
python scripts/timetable_agent_tools.py --request request.json --output diagnosis-response.json
```

输出独占创建，不覆盖已有文件。方案读取同时接受底层 `proposal.json` 和包装工具的方案响应。Python `dispatch_tool` 只返回结果；读取源文件的工具在运行前后检查哈希。保存请求、响应、源哈希、修正审计和验证，才能复现同一场景。

## 授权范围与分阶段补排

参数详见 [参数化排课修复工具](参数化排课修复工具.md)。`configs/repair-approved-college-times.json` 记录早期已审核108个教学班的工作日1–8节场景；配套 policy 是当时输入及授权的审计记录，不会自动授权晚间、周末或新课程。

`days`、`periods` 决定搜索域；`time_scope: "configured_days"` 允许已授权时间偏好调整到所列全部日，`weekdays` 仍限工作日。`single_period_starts: "any"` 只补充单节的偶数起点。源禁排、固定周二5–8节禁排、容量、校区、必要类型、指定教室及资源冲突约束继续生效。对精确自由文本“仅周六周日排课”（去首尾空白后完全匹配），引擎按周末硬约束识别，不能用 `time_preference` 放宽到工作日；这不表示任意自由文本都能自动理解。

`repair_with_fallbacks` 可传有序 `stages: ["daytime", "evening", "weekend"]` 和 `total_time_limit_seconds`。每阶段只取调用方日/节范围的交集，不自动扩大授权；要求 `max_moved_courses: 0` 且 `movable_ids` 为空。在共享总时间预算内保留先前成功安排。

## 有证据的数据更正

用 `plan_course_corrections` 获取逐课建议，再将已授权且证据齐全的操作交给 `preview_data_corrections`。候选值、`needs_confirmation` 或缺证据项不能直接当作源数据事实。操作均需 `course_id`、`operation`、`evidence` 及原值前置条件：

| 操作 | 关键参数 | 条件与含义 |
|---|---|---|
| `redistribute_hours` | `expected_weekly_hours`、`expected_total_hours`、`authoritative_field: "LLXS"`；显式 `weekly_loads`，或 `strategy: "balanced_frontload"` 配 `allocation_unit` 1/2 | 保持源 LLXS 和每个原教学周；各周为正整数，总和等于 LLXS。均衡前置是候选教学计划，不是唯一可推导的真值 |
| `set_capacity` | `expected_capacity`、`capacity`、`enrollment_frozen: true` | 实际人数有来源且已明确冻结；不修改教室容量，不能仅为塞入小教室降低需求 |
| `replace_classes` | `expected_classes`、完整 `classes` | 有真实班级/选课映射，替换值全部存在于班级注册表；不猜测“全校”的成员 |
| `quarantine_incomplete_segments` | `expected_incomplete_count` | 其余完整记录已满足逐周负荷和全部 LLXS，才可隔离多余缺周/无效行；不删除整门课程、不补造周次 |
| `set_one_off_week` | `expected_weekly_hours: 0`、`expected_total_hours`、`teaching_week`、`weeks_confirmed: true` | 零周学时、正整数 LLXS 且可由一个授课块完成；确认具体周后设置一次授课，所选周必须属于原源周集合（本批总量为2） |

`set_one_off_week` 是明确确认后的周域缩小，区别于保持所有原教学周的 `redistribute_hours`。当前真实9门零周学时课尚未选择具体周，不能自动执行。

更正只影响本次内存数据，有效 `source_hashes` 增加 `corrections` 摘要，源 Excel 保持原样。试排、比较、逐课计划和续排必须重放相同修正；只比较工作簿哈希不足以证明有效数据相同。审计保存原值、新值、证据与计划假设；工具不能替调用方证明证据文本或冻结声明的真实性。

虚班不能机械地以继承的母课 LLXS 逐个补足；两个课时更正操作会拒绝单独修改带 `@` 的虚班，需另行提供母课联合分段计划。现有52个虚班对应26门母课，其中50个有正 LLXS 的虚班均继承整门母课值；应按各分段周次和负荷汇总到母课后核对总量。

## 集中授课与显式续排

默认仍为 `max_weekly_hours: 8`、`max_blocks_per_day: 1`、`filter_fixed_occupancy: false`。前两项可显式提高，上限分别77与11，这是模型表达上限，不是现实可用时数。`filter_fixed_occupancy: true` 在生成候选时提前排除固定占用，仅允许0移动；不能用于需要释放占用的挪课搜索，也不能把预筛后的域用于证明“解除占用后仍无解”。

原失败清单以外的新目标用 `extend_repair_proposal`：四个源路径仍指原数据，包括原162门失败清单；`seed_proposal_path` 指原方案，`additional_target_ids` 非空、去重且不与种子目标重合。`config.target_ids` 应为空或恰好等于新增集合；配置必须继续允许全部种子成功安排，不允许移动原课或种子成功课。

工具重放种子修正并核对有效哈希，独立检查种子课时、约束、结果与安排一致性，再补入新目标。新 `corrections` 的前缀必须与种子完全一致，追加修正仅限新增目标。`scope_extension` 区分原目标、新目标、保留数、本次新增数；耗时和节点只属于本次续排。不要重写失败 Excel 来伪装目标集合未变。

57门原 `ZXS>8` 跳过课程作为新增目标。其中54门源 `ZXS×有效周数=LLXS`，3门版式设计10.5×3≠32，追加保持原3周的11+11+10候选计划；总修正由17项增至20项。当前已验证保留原123门安排、新增53门，57门仅余4门“全校”班级映射缺失的高水平运动队。

本批在已授权7日、1–11节、单节任意起点的配置中增加以下参数；这是配置差异片段，仍须保留种子授权、白名单和完整源修正：

```json
{
  "max_weekly_hours": 18,
  "max_blocks_per_day": 4,
  "filter_fixed_occupancy": true,
  "max_moved_courses": 0
}
```

16周学时按两节块需8块，若源要求仅周末，两个可授课日须允许每日4块；扩大每日块数不解除该周末硬约束。

## 联合优化、质量比较与解释

`optimizer_config` 必须提供完整非负 `quality_weights`：`time_preference`、`evening`、`weekend`；可另设 `time_limit_seconds`、`max_search_nodes`、`candidate_limit_per_course`。局部优化联合搜索完整课程模式，报告两阶段状态、保留域、截断、界限及质量分项。质量包含不同日/节模式的偏好代价，以及按教学周计数的晚间/周末授课节次。

`quality_frontier` 的 `movement_budgets` 为0–3且不超过 `config.max_moved_courses`，还需明确 `movable_ids`。各点共用候选域、完整补排数及时间/节点预算。保留域内最优不是全局最优，候选截断须披露。这两项借鉴 Phillips 的分阶段扰动目标和 Lindahl 的质量权衡，是真实有限域算法，并非原论文 MIP 复现。

`explain_config` 可设候选、节点、oracle调用数及时间预算，`allow_scoped_proof` 默认false。oracle 返回 `SAT / UNSAT / UNKNOWN`：合法完整见证可报SAT；候选/模型不完整且未明确允许受限域证明、超时或预算耗尽必须保留UNKNOWN。启用受限证明也只能说明所供候选范围。QuickXplain 先查背景与全集，再逐项删除复验；任何UNKNOWN都不能标记极小性已证实。`minimality_verified` 表示子集极小，不是最少条数或全部冲突。

当前解释器检查新目标在固定占用下能否插入，历史原课间冲突单列。关闭占用标签仅是诊断假设，不表示原课已完整排回，更不构成移动或解除规则的授权。实现与真实单课实验见 [三篇论文接入设计](三篇论文接入设计.md)。

## 结果与当前数据口径

- `placed`：本次完整教学班加入候选并通过增量验证；`already_scheduled` 仅表示输入有记录，不证明旧课时完整。
- `not_found_within_limits`、`search_limit`：当前搜索未找到，不是不可行证明。
- `no_matching_room`、`no_time_pattern`：当前数据及模型无候选，需解释筛选证据，不自动证明现实无解。
- `data_issue`、`source_hours_inconsistent`、`unsupported`：先处理数据、教学计划或表达范围。
- 优化中的 `optimal_in_retained_domain`、`not_selected_in_local_optimum` 仅针对明确保留域和移动邻域。

2026-09-25 核验的原162门场景使用17项显式修正：16份保持 LLXS 和原周的周负荷计划，另隔离运动生理学2条多余缺周记录。在已授权日间、晚间及周末范围联合补排123门、原课移动0门，剩39门：16门班级映射问题、23门静态无匹配教室。新增123门完整课时保留且已知资源新增冲突为0，源文件未修改。输入4504个已排ID有历史问题，不能把4627个结果ID当作4627门已全面验证的课程。

另340个原跳过ID包括274个 `SFXYPK=0`、57个高周学时、9个零周学时。274个先确认是否属于排课任务，不直接计为失败；9个源 LLXS 为2，待确认具体授课周。39+340的379份逐课行动计划是扩域前的问题及业务范围快照，不是379门已确认待排。

当前扩域候选的219个试排目标合计补入176门、剩43门（23门静态教室、20门班级映射），组成是原162的123/39加新增57的53/4。原123门安排保持不变，全部176门新增课的课时与已知资源复验通过，原课程移动0门；20项更正只在内存生效。4680是有安排记录的教学班ID数，不能称全校4680门历史课时都已校验。9门待确认授课周和274门业务范围项仍单列，未借此次扩域自动排入。

`compare_proposals` 拒绝不同有效源哈希或目标集合；同源时也披露参数差异，不把更改约束后的成功数当作纯算法收益。旧日间89/73、晚间96/66是历史对照。

## 验证

```bash
python -m unittest discover -s tests -p 'test_*.py'
```

覆盖源数据、事务回滚、完整课时、分段导出、参数及哈希、修正预检、逐课计划、有限域优化、三值解释及集中授课。真实数据数值是指定场景快照；新参数、数据或授权须保留新的请求和验证。
