# 规则包格式（schema_version 1）

规则包只含审核过的声明式规则。当前工具严格拒绝未知字段、重复 JSON 字段、未知规则种类、空依据和未知教学班，避免规则在未察觉时失效。

```json
{
  "schema_version": 1,
  "name": "2026 秋季北校区教室类型替代",
  "rules": [
    {
      "id": "north-media-substitution-01",
      "kind": "room_type_substitution",
      "course_ids": ["61660003.001", "61660003.002"],
      "substitute_room_types": ["固定桌椅多媒体"],
      "evidence": "教务处 2026-09-30 场地调配单第 18 条"
    }
  ]
}
```

`course_ids` 必须列出精确教学班 ID；`substitute_room_types` 是在该课源教室类型之外增加的类型。源类型会保留，多个已审核规则可叠加到同一课程。

调用示例：

```json
{
  "name": "repair_with_fallbacks",
  "arguments": {
    "course_path": "课程表.xlsx",
    "room_path": "教室表.xlsx",
    "schedule_path": "已排课.xlsx",
    "failed_path": "未排课.xlsx",
    "rule_pack_path": "教务规则包.json",
    "config": {"allowed_changes": []},
    "stages": ["daytime", "evening", "weekend"]
  }
}
```

校区、教室容量、指定教室、教师/班级冲突、禁排、课时和占用均不在此规则中放宽。特别是不存在 `campus` 字段；填写它会被拒绝，跨校区安排永远不会成为候选。
