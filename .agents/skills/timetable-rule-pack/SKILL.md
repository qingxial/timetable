---
name: timetable-rule-pack
description: 将教务老师已确认的同校区教室替代决策整理为可审计规则包，并供 qingxial/timetable 排课工具按需加载。适用于指定课程的可替代教室类型及后续规则扩展。
---

# 教务排课规则包

把可重复使用、已被教务确认的排课决策写入独立 JSON 规则包，再以 `rule_pack_path` 传给 `scripts/timetable_agent_tools.py` 的排课工具。规则包是数据而不是可执行代码；工具会记录它的 SHA-256、规则内容和依据到候选方案。

## 当前可用规则

- `room_type_substitution`：为精确列出的教学班增加同校区可用教室类型。填写每门课、替代类型和教务依据；不要把未确认的全局惯例写成规则。
- **校区绝不允许替代。** 规则包没有校区放宽字段。无论类型替代、晚间/周末、人数假设或局部优化，课程都只能使用源 `SKXQ` 所属校区的教室。

使用前读 [规则包格式](references/rule-pack-schema.md)。先在候选输入上试排，再检查输出中的 `rule_pack`、源哈希、`validation` 和每个变更；规则包不等同于发布课表。

## 扩展规则种类

新的教务规则若无法由现有种类表达，先明确其范围、不可放宽的资源约束和验证办法，再在 `scripts/repair_rule_packs.py` 添加受测的声明式处理器与 schema 检查。不得把自然语言、表达式或任意代码放进 JSON 供工具执行。
