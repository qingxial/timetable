# 排课修复 Agent 工作流

`scripts/timetable_repair_agent.py` 是现有确定性修复工具的受限编排层。它不替代求解器、不调用任意代码、不修改源 Excel，也不发布课表。它把一次请求写成独立运行包，保存请求、计划、规则包、每次工具调用、结果和清单。

## 请求格式

```json
{
  "request": "允许晚间与周末，保留已有成功安排，重试指定失败课",
  "intent": "repair",
  "repair_mode": "retry",
  "authorization": {
    "allow_evening": true,
    "allow_weekend": true,
    "publication": false
  },
  "tool_arguments": {
    "course_path": "课程表.xlsx",
    "room_path": "教室表.xlsx",
    "schedule_path": "已排课.xlsx",
    "failed_path": "失败课.xlsx",
    "seed_proposal_path": "已有方案.json",
    "target_ids": ["一个失败教学班ID"],
    "config": {"allowed_changes": []}
  }
}
```

`intent` 可为 `diagnose`、`repair`、`correct_data`、`explain`、`compare` 或 `rule_pack`。省略时，Agent 只依据请求中的明确关键词作有限分类；它不假装从自然语言推断新授权。

运行：

```bash
python3 scripts/timetable_repair_agent.py --request agent-request.json
```

每次运行在 `agent-runs/<run-id>/` 创建以下不可覆盖的审计包：

- `request.json`：原始用户意图与显式参数；
- `plan.json`：选中的 skill、工具和永久策略；
- `rule-pack.json`：如本次有教务类型替代规则；
- `tool-calls.jsonl`：工具参数及结果，按顺序保存；
- `result.json` 与 `manifest.json`：最终状态、源哈希与清单。

## 固定流程

1. Agent 选择 `timetable-repair`；规则包存在时另选择 `timetable-rule-pack`。
2. 有 `corrections` 的修复先调用 `preview_data_corrections`，成功后才试排。
3. 未提供种子方案的常规修复默认调用 `propose_repair`。明确授权晚间或周末时，调用 `repair_with_fallbacks`，并由授权派生阶段顺序。
4. 种子方案只允许显式 `retry` 或 `extend`，以保留既有成功安排。
5. 返回结果只称为候选；正式发布不属于 Agent 能力。

## 永久策略

- 永远不允许跨校区；
- 源工作簿永远只读；
- 人数假设必须保持条件标记，不能伪装为冻结人数；
- 晚间与周末必须分别在 `authorization` 中显式允许；
- 工具搜索失败、预算耗尽和 `UNKNOWN` 不能写成现实无解；
- Agent 不提供发布工具。
