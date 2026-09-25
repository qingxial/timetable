# Agent 排课工具接口

`scripts/timetable_agent_tools.py` 提供三个确定性工具，以及供调用方注册的 JSON Schema。无需模型 SDK、网络请求或 pickle。Agent 负责从已有授权选择参数、读取证据和解释结果；实际排课与校验由工具执行。

| 工具 | 输入 | 输出 |
|---|---|---|
| `diagnose_remaining` | `proposal_path` | 剩余数量、逐课状态与证据、原始参数、增量校验结果 |
| `propose_repair` | 四个 Excel 路径、显式 `config`；可选 `class_path` | 完整试排方案、改动前后记录、源文件哈希检查结果 |
| `compare_proposals` | 至少两个 `proposal_paths` | 完整补排数、移动原课程数、偏好代价、策略及搜索预算差异 |

所有调用返回 `{ok, tool, result}` 或 `{ok: false, tool, error: {code, message}}`。字段名及类型以模块中的 `TOOL_SCHEMAS` 为准；未知字段、未知工具及不支持的放宽项会报错。`allowed_changes` 必须显式传入，严格场景用空列表。

## Python 调用

从仓库根目录运行，使用 Python 3.10+、pandas、openpyxl：

```python
import json
from pathlib import Path
from scripts.timetable_agent_tools import TOOL_SCHEMAS, dispatch_tool

base = Path("智能排课基础数据/提取的基础数据表_converted")
config = json.loads(Path("configs/repair-approved-college-times.json").read_text())
response = dispatch_tool("propose_repair", {
    "course_path": str(base / "课程表_split_merged.xlsx"),
    "room_path": str(base / "教室表.xlsx"),
    "schedule_path": "排课结果/调课后的整体结果_特殊放宽.xlsx",
    "failed_path": "排课结果/调课失败的排课失败课程_特殊放宽后.xlsx",
    "config": config,
})
if not response["ok"]:
    raise RuntimeError(response["error"])
with open("repair-runs-approved-response.json", "x", encoding="utf-8") as stream:
    json.dump(response, stream, ensure_ascii=False, indent=2)
```

本次授权配置允许108个已审核教学班调整学院指定上课时段，搜索范围为工作日1–8节，保留原有禁排、容量、必要教室类型和校区要求，原课程移动上限为0。配套 `.policy.json` 保存本次源数据哈希和授权范围；该文件是审计记录，工具不会从自然语言推断授权，也不会自动把旧授权扩展到新数据。更换数据时需核对范围。

当前 `propose_repair` 支持的调整参数完整说明见 [参数化排课修复工具](参数化排课修复工具.md)。增加 `max_candidates_per_course` 或 `max_search_nodes` 是扩大搜索，不改变约束。需要挪动原课时，显式传入 `movable_ids` 白名单与 `max_moved_courses`（0–3）；失败的局部重排会回滚。

`periods: [1,2,3,4,5,6,7,8,9,10,11]` 将未被源数据禁止的晚间纳入搜索；`single_period_starts: "any"` 允许单节课从偶数节开始。两者都不会移除原禁排；双节及更长连排块仍用原网格。调用方应保存这些参数，并在比较中说明相对日间默认场景的差异。

## JSON 命令行

将下面请求写入 `request.json`，其中路径替换为已生成的方案路径：

```json
{
  "name": "diagnose_remaining",
  "arguments": {"proposal_path": "repair-runs-approved-response.json"}
}
```

```bash
python scripts/timetable_agent_tools.py --request request.json --output diagnosis-response.json
```

诊断和比较同时接受原 `repair_courses.py` 的 `proposal.json` 与新工具的响应文件。输出采用独占创建，已有文件不会覆盖。Python `dispatch_tool` 只返回结果；`propose_repair` 在运行前后检查输入文件哈希。

## 结果语义

- `placed`：完整教学班已加入候选方案，并通过本次增量校验。
- `already_scheduled`：原输入中已有记录，不能由此推断该课程总学时正确。
- `not_found_within_limits`、`search_limit`：当前搜索没有找到，不是不可行证明。
- `no_matching_room`、`no_time_pattern`：当前数据与模型没有生成候选；需结合具体容量、类型、禁排及网格证据解释。
- `data_issue`、`source_hours_inconsistent`、`unsupported`：先处理数据或表达范围问题，再求解。

`compare_proposals` 要求课程、教室、课表、班级表源哈希及目标集合一致。它会列出参数差异，不能把不同约束下的成功数直接当成算法优劣。偏好代价来自当前引擎：对每门改动课程的不同日/节模式求和，不乘周数；这是简单指标，尚未实现 Lindahl 的完整质量模型。

## 当前边界与论文接入

当前实现包括有界完整插入、少量直接阻塞课程重放、失败回滚、证据分类及方案比较。尚未实现 Phillips 的联合整数规划、Lindahl 的质量恢复算法或 QuickXplain 冲突核。三篇的接入方案及必要条件见 [三篇论文接入设计](三篇论文接入设计.md)。

96门含晚间候选方案仅证明这些新增课程对输入中已知教师、班级和教室资源没有新增冲突；旧课表的历史冲突、缺周和学时差异仍需单独处理。源 Excel 保持原样。

## 验证

```bash
python -m unittest discover -s tests -p 'test_*.py'
```

本次62项测试覆盖原数据解析、增量修复、导出周次以及11项工具接口测试。接口测试包括完整临时 Excel 试排、文件哈希不变、错误参数、跨基线拒绝比较、结果一致性、未知状态和命令行不覆盖。
