---
name: timetable-repair
description: 诊断 qingxial/timetable 的未排课程，用确定性工具形成逐课更正计划、审计数据修正、增量试排、有限域优化和冲突解释，核验完整课时与资源冲突。适用于已有课表及失败清单的修复与显式扩域续排。
---

# 排课诊断与试排

定位 `qingxial/timetable` 仓库，确认 `scripts/repair_courses.py` 和 `scripts/timetable_agent_tools.py`，使用仓库 Python 依赖。先读 `docs/参数化排课修复工具.md`、`docs/Agent排课工具接口.md`，接口以实际 `TOOL_SCHEMAS` 为准，无 pickle 也可重建 Excel 状态。

## 工具选择

- `diagnose_remaining` 读取方案状态，`plan_course_corrections` 生成逐课字段、源行、候选值、缺证据和复验条件。
- `preview_data_corrections` 预检显式更正；`propose_repair` 完整试排；`repair_with_fallbacks` 在授权域内依次日间、晚间、周末补排，保留先前成功。
- `extend_repair_proposal` 验证种子后保留成功安排，补入显式新增目标；`compare_proposals` 只比较相同有效源和目标集合。
- `optimize_local_repair` 联合搜索有限候选；`quality_frontier` 固定补排数及候选域比较移动预算；`explain_conflict` 用三值oracle和 QuickXplain 解释固定占用阻塞。

请求显式给 `allowed_changes`，严格场景为空。已保存 policy 只适用于其输入和课程范围，核对哈希及会话授权；不从自然语言自动扩授权。已有授权和证据足以执行时直接完成，无需重复确认。

## 工作流程

1. 固定四个源路径及 SHA-256，保留请求。按精确教学班ID计数，多教师/多课段行不重复计门数；虚班与母课分别报告，子班可能继承整门母课 LLXS，应按分段周次汇总后核对母课总量。
2. 区分静态无教室、时间/资源阻塞、班级覆盖、课时矛盾、表达范围及搜索不足。`not_found_within_limits`、`search_limit` 不是无解；单课独立可排不能相加为整批可排。
3. 逐课读取源字段和更正计划。候选值不等于事实，缺班级映射、冻结人数或具体授课周时不编造。`SFXYPK=0` 先确认业务范围，不直接记失败；原274个这类跳过ID与57个高周、9个零周分别计量。
4. 在已有授权下执行证据齐全的更正预检，保存审计；按相同参数重放更正后试排。更正只在内存生效，记录 `source_hashes.corrections`，不改源工作簿。原值不符应停止该操作并重新核对，不覆盖前置条件。
5. 配置目标、锁定、移动白名单、移动上限和预算。学院指定时间仅在 `reviewed_soft_time_ids` 且允许时间偏好调整时可变。`time_scope: configured_days` 才使用显式全部日期；阶段工作流只是与调用方域取交集，不自动授权周末。精确文本“仅周六周日排课”按硬约束识别，不得用偏好调整解除；不推定其他自由文本也已被支持。
6. 在新输出路径运行，检查全批次课时、资源、前后安排、放宽字段和基线问题。完整方案通过后再报告候选；不把 proposal 自动发布为正式课表。

## 五种显式更正

- `redistribute_hours` 保持源 LLXS 和每个原教学周，用显式整数 `weekly_loads` 或明确选择 `balanced_frontload` 分配。均衡前置是教学计划候选，不是唯一真值；不得改变 LLXS、取整丢小时或对每个虚班重复补母课总量。课时更正工具拒绝独立修改带 `@` 的虚班，需母课联合计划。
- `set_capacity` 必须有来源并确认实际人数已冻结，设置 `enrollment_frozen: true`；不因实际人数小于上限就自动减需求，不变教室容量。
- `replace_classes` 必须有真实完整班级/选课映射且符合注册表，不猜“全校/全院”成员。
- `quarantine_incomplete_segments` 仅在有效记录已满足全部周负荷与 LLXS 时隔离多余坏行；保留有效安排，不删除整门课程、不造周次。
- `set_one_off_week` 只用于零周学时、可由一个授课块完成的正整数 LLXS，须确认源周集合内的具体 `teaching_week` 及 `weeks_confirmed: true`。这是明确确认后的周域缩小；当前真实9门 LLXS=2 尚未指定周，不能自动执行。

工具验证结构及前置条件，不能替代证据真实性。源禁排、教师/班级冲突、必要类型、指定教室、容量和校区保持；不改共占/免查开关制造成功。

## 集中授课及续排

默认 `max_weekly_hours=8`、`max_blocks_per_day=1`、`filter_fixed_occupancy=false`。高周课可按计划显式提高前两项（上限77/11）；16学时按两节块需要8块，单改周上限不足；仅周末16学时可在支持的教学计划中设 `max_blocks_per_day=4`，仍保持周末硬约束。`filter_fixed_occupancy=true` 只用于0移动，将固定占用提前过滤，不能混用于需要解除该占用的诊断域。

新增原失败清单外课程时用 `extend_repair_proposal`，原四个源路径及 `failed_path` 不变；传种子、独立 `additional_target_ids`，不伪造新失败工作簿。重放种子修正前缀，追加修正仅限新目标；新配置须容纳全部种子安排。检查独立种子验证、成功保留、原有效安排不动、新批次完整课时和0新增冲突。原目标、新增目标及本次成本分开报告。

## 有限域结论与报告

联合优化和质量前沿是真实有限域算法，借鉴 Phillips/Lindahl，不是原论文 MIP 复现。报告邻域、候选域哈希、截断、目标和求解状态；保留域内最优不写成全局最优。质量比较须固定成功数与域，共用总预算。

QuickXplain 的oracle必须保留 `SAT / UNSAT / UNKNOWN`。超时、预算不足或不完整域默认UNKNOWN；显式受限证明也不能升级为全局无解。有UNKNOWN不能声称极小核已验证；子集极小不是最少条数。占用标签可解释不代表可放宽，关闭占用不证明原课程可完整排回。

报告新增完整教学班、原课移动数、条件、更正和剩余原因。零小时尾段、缺周、历史冲突及学时不守恒单列，不能凭ID出现就称完成。`new_conflicts=0` 仅针对已知资源增量；缺真实选课明细时不能声称全学生课表无冲突。当前原162补123/剩39；新增57门高周课补53/剩4，保留原123安排。219目标合计补176/剩43（教室23、班级20），20项审计更正；4680只是有记录ID数，不是全校历史课时已全验。9门零周课待具体授课周，274个 `SFXYPK=0` 业务范围未确认，不混算失败。
