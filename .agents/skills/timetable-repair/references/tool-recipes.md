# 工具配方与参数边界

工具实现位于仓库 `scripts/timetable_agent_tools.py`，调用 `dispatch_tool(name, arguments)`；CLI为 `python scripts/timetable_agent_tools.py --request request.json --output new-response.json`。输出路径须不存在。工具目前为11个、显式更正为6种；实际使用先核对运行版本的 `TOOL_SCHEMAS`，不要凭本文推定新接口。

## 构造保留成功的失败重试

从本次输入准备 `sources`：`course_path`、`room_path`、`schedule_path`、`failed_path`，可另给 `class_path`。种子可为原proposal或成功工具响应中的 `result.proposal`。

以下为参数构造片段，变量由当前已核验的输入、目标和证据提供；不是自动授权的固定请求。

```python
from copy import deepcopy

config = deepcopy(seed["config"])
config["target_ids"] = []  # 种子里可能保存全部目标，不能直接沿用
config["movable_ids"] = []
config["max_moved_courses"] = 0
actions = deepcopy(seed.get("correction_parameters", [])) + new_actions
arguments = {
    **sources,
    "seed_proposal_path": seed_path,
    "target_ids": failed_subset,
    "corrections": actions,
    "config": config,
    "stages": ["daytime", "evening", "weekend"],
    "total_time_limit_seconds": 180,
}
```

将这份参数交给 `retry_repair_proposal`；阶段只使用config已经允许的日期与节次，不会因例子列出weekend自动授权周末。新配置仍须容纳每个种子成功安排。`failed_subset` 必须是种子未排且无已有安排的ID；新操作仅限这些ID，结果总目标数不变。

有追加更正时，可先将同一 `sources` 与完整 `actions` 交给 `preview_data_corrections`。预检成功只说明结构、原值和规则通过，证据真伪仍要核实。

新增范围改用 `extend_repair_proposal` 和 `additional_target_ids`：这些ID必须在种子目标之外，不能重写原失败Excel来伪装范围不变。两种续排均校验源/修正哈希、精确修正前缀及种子全部安排。对同ID同操作不能重复追加；若要修改已有修正计划，需要重新建立并验证方案版本，不能篡改前缀后继续声称保留原实验。

## 六种更正操作的必要参数

每项都有 `course_id`、`operation`、非空 `evidence`。`evidence`写实际来源或已有用户决策，不把模板文字当事实。

| operation | 其余参数 | 前置条件 |
|---|---|---|
| `set_capacity` | `expected_capacity`、正数 `capacity`、`enrollment_frozen: true` | 未排课程；人数确已冻结并采用该口径。修改需求，不修改教室容量 |
| `redistribute_hours` | `expected_weekly_hours`、`expected_total_hours`、`authoritative_field: "LLXS"`；`weekly_loads` 或 `strategy: "balanced_frontload"` 与 `allocation_unit`（1或2） | 未排；保留原周及LLXS；每周正整数；与显式块模板兼容；非独立虚班 |
| `replace_classes` | `expected_classes`、非空完整 `classes` | 未排；替换班级全部在BJMC注册表；有真实映射 |
| `set_class_conflict_check` | 布尔 `expected_check_class`、`check_class: false` | 未排；精确课程已有用户免查授权；原false允许审计无变化，不能传字符串或数字冒充布尔 |
| `quarantine_incomplete_segments` | 正整数 `expected_incomplete_count` | 已排课其他有效记录已满足全部源周负荷和LLXS，仅去冗余无效记录 |
| `set_one_off_week` | `expected_weekly_hours: 0`、正整数 `expected_total_hours`、`teaching_week`、`weeks_confirmed: true` | 未排；总量能由源最大块完成；具体周已确认且在源周域中；非独立虚班 |

默认 `max_weekly_hours=8`、`max_blocks_per_day=1`。已确认集中课可显式提高，当前接口上限分别77和11。两节块的每周16学时需要8块；如果只许周末，至少要有足够的每日块数，不能只提高周上限。`filter_fixed_occupancy=true`只用于0移动。

`allowed_changes`只能是 `time_preference`、`building_preference`、`historical_room` 的子集；没有 `capacity`、`room_type`、`campus`、`ignore_teacher` 这些白名单选项。忽略班级必须走显式更正，不是把班级名字删空。源指定教室不会因为允许历史房间偏好变化就变成可任意更改。

## 解释与局部优化

- `explain_conflict`：`config`与`explain_config`；后者可设候选、节点、oracle调用、秒数及 `allow_scoped_proof`。默认不把不完整候选域证明为全局UNSAT。
- `optimize_local_repair`：`config`与`optimizer_config`。明确可动 `movable_ids`、锁定集合和移动上限；现接口最多3门。`optimizer_config.quality_weights`须同时给 `time_preference`、`evening`、`weekend` 三个非负权重。
- `quality_frontier`：再给 `movement_budgets`，各值为0–3且不超过配置移动上限。用于相同候选域、相同补排数的质量比较。

这些工具直接读给定源课表，不接受 `seed_proposal_path` 来自动并入全部新增候选。若要移动最新种子里已成功的4门运动队，不能把原4504基线拿来调用优化后声称保留了184门；需先实现或采用经独立验证、包含完整候选的调课入口。当前retry/extend明确禁止移动既有成功。

## 当前未提供的操作

添加真实教室库存、修改必要房间类型/校区、按真实学生拆班，以及对已有完整种子做场地要求变更后的小邻域调课，尚无对应的审计更正/完整续接工具。可写明确扩展规格，不能虚构工具名后声称已执行。现有 `plan_course_corrections` 会提出部分此类方案，但输出建议不等于已具备执行接口。
