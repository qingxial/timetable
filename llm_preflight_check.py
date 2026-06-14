"""
LLM 驱动的排课前合理性检查（中期报告 3.1 节「合理性检查环节由 LLM 对结构化约束
进行语义级一致性推理，识别规则难以捕捉的隐性冲突」的实现）。

直接运行：python llm_preflight_check.py        （需配置 DASHSCOPE_API_KEY）
调试：    python llm_preflight_check.py --max-courses 20 --dry-run

【输入映射】不把原始表格直接喂给 LLM（4000+ 教学班 token 不可控），而是分两层：
  第一层（程序，定量）：先跑规则版 preflight_schedule_checks.run_preflight 得到问题清单；
    再为每个教学班计算结构化证据包 evidence：
      - 基本信息：教学班ID/课程名/开课单位/课程类别/校区/课容量/周学时/教学周数
      - 原始时间文本：Prefer_Time、unavailable_Time（LLM 语义检查的主要对象）
      - 解析结果：偏好组数/偏好选项数/禁止块数、可行日模式数
      - 教室侧：严格筛选候选教室数、放宽后候选教室数、指定教室/教学楼/教室类型
      - 教师侧：教师人数、教师号缺失、教师周次与课程周次交集为空的教师
      - 规则版已发现的问题列表
  第二层（LLM，语义）：仅对命中「交互信号」的教学班分批送入 LLM。
    送审依据不是"填了偏好"，而是偏好与其他数据的交互出了问题：
      ① 规则版查出问题（偏好装不下周学时、偏好与禁止重叠、指定教室不可行、教师缺失等）；
      ② 教师跨课程偏好冲突（同一教师多门课偏好窗口重叠且周次相交、容量不够——全局计算，
         规则版逐班检查覆盖不到；冲突详情与同教师其他课程一并写入证据包）；
      ③ 偏好窗口临界（可行日模式仅剩 1-2 种，叠加全校占用后高风险）。
    LLM 做规则覆盖不到的推理：解析丢失语义、约束叠加后的隐性不可行、
    字段间语义矛盾、跨课程偏好该保谁让谁等。

【输出映射】每个教学班一条 JSON：
  {教学班ID, 风险等级(高/中/低), 可排性判断(可排/存疑/基本不可排),
   发现的问题:[{类别, 严重程度(一级/二级/三级), 描述, 建议}], 隐性矛盾}
落盘：
  - 排课结果/LLM排课前检查报告.xlsx   （汇总表，可直接给教务人员看）
  - 排课结果/llm_preflight_raw.json   （原始输入证据+LLM输出，供论文消融实验复现）
  - 排课结果/排课前检查报告.xlsx       （规则版报告，作为对照同时生成）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List

import pandas as pd

from Basic_Data import load_courses, load_teachers, load_classrooms, load_classes
from utils1 import (
    get_consecutive_jxbid,
    get_teaching_weeks,
    trans_week_flags,
    get_teacher_instances,
    filter_suitable_classrooms,
)
from tryloadlimit import build_preferences, new_generate_day_patterns, choose_calendar_shape
from preflight_schedule_checks import (
    run_preflight,
    collect_jxbids_for_preflight,
    _expand_blocks_for_items,
)
import llm_api


BASE_DIR = os.path.join(os.path.dirname(__file__), "智能排课基础数据", "提取的基础数据表_converted")
DEFAULT_COURSE = os.path.join(BASE_DIR, "课程表.xlsx")
DEFAULT_TEACHER = os.path.join(BASE_DIR, "教师表.xlsx")
DEFAULT_CLASSROOM = os.path.join(BASE_DIR, "教室表.xlsx")
DEFAULT_BANJI = os.path.join(BASE_DIR, "班级表.xlsx")

RESULTS_DIR = "排课结果"
DEFAULT_OUTPUT = os.path.join(RESULTS_DIR, "LLM排课前检查报告.xlsx")
DEFAULT_RAW_JSON = os.path.join(RESULTS_DIR, "llm_preflight_raw.json")
RULE_REPORT = os.path.join(RESULTS_DIR, "排课前检查报告.xlsx")


SYSTEM_PROMPT = """你是高校智能排课系统的资深教务与调度专家。系统在正式排课前，已用规则程序对每个教学班做了定量检查，现在把每个教学班的「证据包」交给你做语义级合理性复核。

你的职责（规则程序做不到、需要你推理的部分）：
1. 原始时间偏好文本 vs 解析结果：若文本明显有内容但解析出的偏好组/禁止块数为 0 或明显偏少，说明解析丢失了语义，要指出；
2. 多条约束叠加后的隐性不可行：如周学时较大 + 偏好窗口很窄 + 教师授课周次受限叠加后，实际可行空间可能为零；
3. 字段间语义矛盾：如指定教室与上课校区不符、课容量与教室类型常识不符、偏好与禁止时间在语义上互相覆盖；
4. 教师跨课程偏好冲突：若证据包含「教师跨课程偏好冲突」与「同教师其他偏好课程」字段，
   说明该教师名下多门课的偏好窗口重叠——请综合各课的周学时、偏好窗口与周次，判断应优先保谁的偏好、
   建议谁改时间（例如周学时小、窗口选择多的课让步），给出具体的腾挪方案；
5. 对规则版已发现的问题，判断哪些是真问题、哪些可能是误报，并给出更可操作的修改建议。

判断标准：
- 风险等级「高」= 不修改数据大概率排不上；「中」= 可能排上但有明显隐患；「低」= 基本没问题。
- 可排性判断：可排 / 存疑 / 基本不可排。
- 严重程度沿用系统口径：一级（时间约束类，最影响可行性）、二级（数据/资源类）、三级（提示类）。

输出要求：严格输出 JSON 数组，不要任何解释文字或 Markdown 围栏。数组中每个元素对应一个输入的教学班：
[{"教学班ID": "...", "风险等级": "高|中|低", "可排性判断": "可排|存疑|基本不可排",
  "发现的问题": [{"类别": "...", "严重程度": "一级|二级|三级", "描述": "...", "建议": "..."}],
  "隐性矛盾": "规则未覆盖的隐性问题，没有则为空字符串"}]
即使某教学班没有问题，也要输出对应元素（风险等级=低、发现的问题=[]）。"""


def _s(v: Any) -> str:
    if v is None:
        return ""
    return str(v).strip()


def _pref_slot_set(tc: Dict[str, Any]) -> set:
    """把解析后的偏好窗口展开成 (星期, 节次) 槽位集合（每周粒度）。"""
    days_of_week, periods_per_day = choose_calendar_shape(tc)
    items = (tc.get("preferred_options") or []) + [
        slot for grp in (tc.get("preferred_groups") or []) for slot in grp
    ]
    by_day = _expand_blocks_for_items(items, days_of_week, periods_per_day)
    slots = set()
    for d, blocks in by_day.items():
        for s, e in blocks:
            for p in range(s, e + 1):
                slots.add((d, p))
    return slots


def detect_teacher_pref_conflicts(courses: List[Any], jxbid_list: List[str]):
    """教师跨课程偏好冲突检测（规则版逐班检查覆盖不到的交互问题）。

    同一教师名下多门填写了偏好时间的课程，若偏好窗口重叠且授课周次相交，
    检查窗口并集容量是否装得下这些课程的总周学时：
      装不下 → 必然无法全部满足偏好；占用 ≥70% → 余量紧张，叠加禁排/教室约束后高危。
    返回 (conflicts, context)：
      conflicts: jxbid -> [冲突描述]      —— 作为送审依据
      context:   jxbid -> [同教师其他偏好课程的摘要] —— 进证据包给 LLM 推理
    """
    info: Dict[str, Dict[str, Any]] = {}
    for jxbid in jxbid_list:
        jxbs = get_consecutive_jxbid(jxbid, courses)
        if not jxbs or not _s(jxbs[0].Prefer_Time):
            continue
        head = jxbs[0]
        tc = build_preferences(head.Prefer_Time, head.unavailable_Time)
        slots = _pref_slot_set(tc)
        if not slots:
            continue
        try:
            zxs = int(head.ZXS)
        except (TypeError, ValueError):
            continue
        info[jxbid] = {
            "slots": slots,
            "weeks": set(get_teaching_weeks(jxbs)),
            "zxs": zxs,
            "kcm": _s(head.KCM),
            "pref": _s(head.Prefer_Time),
            "teachers": {(_s(j.JSH), _s(j.XM)) for j in jxbs if _s(j.JSH)},
        }

    by_teacher: Dict[str, List[str]] = {}
    teacher_name: Dict[str, str] = {}
    for jxbid, d in info.items():
        for jsh, xm in d["teachers"]:
            by_teacher.setdefault(jsh, []).append(jxbid)
            teacher_name[jsh] = xm

    conflicts: Dict[str, List[str]] = {}
    context: Dict[str, List[Dict[str, Any]]] = {}
    for jsh, ids in by_teacher.items():
        ids = list(dict.fromkeys(ids))
        if len(ids) < 2:
            continue
        # 按「窗口重叠且周次相交」连成分组（连通分量）
        unvisited = set(ids)
        while unvisited:
            seed = unvisited.pop()
            group, queue = [seed], [seed]
            while queue:
                cur = queue.pop()
                for other in list(unvisited):
                    if (info[cur]["slots"] & info[other]["slots"]
                            and info[cur]["weeks"] & info[other]["weeks"]):
                        unvisited.remove(other)
                        group.append(other)
                        queue.append(other)
            if len(group) < 2:
                continue
            union_slots = set().union(*(info[i]["slots"] for i in group))
            total_zxs = sum(info[i]["zxs"] for i in group)
            cap = len(union_slots)
            if total_zxs > cap:
                level = "窗口装不下，必然无法全部满足偏好"
            elif total_zxs * 10 >= cap * 7:
                level = "占窗口容量70%以上，余量紧张（叠加禁排/教室占用后高危）"
            else:
                continue
            desc = (
                f"教师{teacher_name.get(jsh, '')}({jsh}) 名下 {len(group)} 门偏好课程窗口重叠且周次相交："
                f"合计周学时 {total_zxs} vs 偏好窗口并集容量 {cap} 节/周，{level}。"
                f"涉及教学班: {', '.join(group)}"
            )
            for i in group:
                conflicts.setdefault(i, []).append(desc)
                context.setdefault(i, []).extend(
                    {
                        "教学班ID": o,
                        "课程名称": info[o]["kcm"],
                        "周学时": info[o]["zxs"],
                        "偏好时间": info[o]["pref"],
                    }
                    for o in group if o != i
                )
    return conflicts, context


def build_evidence(
    jxbid: str,
    courses: List[Any],
    teachers: List[Any],
    list_of_jas: List[Any],
    num_days: int,
    rule_issues: List[Dict[str, str]],
) -> Dict[str, Any]:
    """为单个教学班组装结构化证据包（LLM 的输入单元）。"""
    jxbs = get_consecutive_jxbid(jxbid, courses)
    if not jxbs:
        return {"教学班ID": jxbid, "错误": "课程表中找不到该教学班"}
    head = jxbs[0]

    teaching_weeks = get_teaching_weeks(jxbs)
    jsh_list = [j.JSH for j in jxbs]
    teacher_objs = get_teacher_instances(jsh_list, teachers)
    missing_jsh = sorted({_s(j) for j, t in zip(jsh_list, teacher_objs) if t is None})
    no_overlap_teachers = []
    for jxb in jxbs:
        tw = trans_week_flags(jxb.RWJSZCDM) if jxb.RWJSZCDM else []
        if tw and teaching_weeks and not set(teaching_weeks).intersection(tw):
            no_overlap_teachers.append(f"{_s(jxb.XM)}({_s(jxb.JSH)})")

    tc = build_preferences(head.Prefer_Time, head.unavailable_Time)
    pattern_error = ""
    try:
        patterns = new_generate_day_patterns(head.ZXS, num_days, tc, flag_reschedule=False)
    except ValueError as e:
        patterns = []
        pattern_error = str(e)

    strict_jas = filter_suitable_classrooms(list_of_jas, head, relax_constraints=False)
    relaxed_jas = filter_suitable_classrooms(list_of_jas, head, relax_constraints=True)

    return {
        "教学班ID": jxbid,
        "课程名称": _s(head.KCM),
        "开课单位": _s(head.YXMC),
        "课程类别": _s(head.KCLB),
        "上课校区": _s(head.SKXQ),
        "课容量": head.KRL,
        "周学时ZXS": head.ZXS,
        "教学周数": len(teaching_weeks),
        "偏好时间原文": _s(head.Prefer_Time) or "(空)",
        "禁止时间原文": _s(head.unavailable_Time) or "(空)",
        "解析_偏好组数": len(tc.get("preferred_groups") or []),
        "解析_偏好选项数": len(tc.get("preferred_options") or []),
        "解析_禁止块数": len(tc.get("unavailable") or []),
        "可行日模式数": len(patterns),
        "日模式生成错误": pattern_error,
        "指定教室代码": _s(head.JASDM),
        "历年教室代码": _s(head.LSJASDM),
        "教学楼代码": _s(head.JXLDM),
        "教室类型": _s(head.JASLXMC) or _s(head.JASLXDM),
        "严格候选教室数": len(strict_jas),
        "放宽候选教室数": len(relaxed_jas),
        "教师人数": len(jxbs),
        "教师号缺失": missing_jsh,
        "教师周次与课程周次无交集": no_overlap_teachers,
        "规则版发现的问题": [
            {"类别": i["问题类别"], "严重程度": i["优先级"], "说明": i["详细说明"]}
            for i in rule_issues
        ],
    }


def run_llm_preflight(
    course_excel: str = DEFAULT_COURSE,
    teacher_excel: str = DEFAULT_TEACHER,
    classroom_excel: str = DEFAULT_CLASSROOM,
    banji_excel: str = DEFAULT_BANJI,
    num_weeks: int = 20,
    num_days: int = 7,
    num_periods: int = 11,
    batch_size: int = 8,
    max_courses: int = 0,
    output_path: str = DEFAULT_OUTPUT,
    raw_json_path: str = DEFAULT_RAW_JSON,
    dry_run: bool = False,
    scope: str = "issues",
    resume: bool = False,
) -> pd.DataFrame:
    os.makedirs(RESULTS_DIR, exist_ok=True)

    # 第一步：规则版检查（同时落盘规则版报告，作为对照与 LLM 输入）
    print("=== 第一步：运行规则版排课前检查 ===")
    rule_df = run_preflight(
        course_excel, teacher_excel, classroom_excel, banji_excel,
        num_weeks, num_days, num_periods,
        combinations_path=None, output_path=RULE_REPORT,
    )
    print(f"规则版检查完成：{len(rule_df)} 条问题，已写入 {RULE_REPORT}")
    issues_by_jxbid: Dict[str, List[Dict[str, str]]] = {}
    for _, row in rule_df.iterrows():
        issues_by_jxbid.setdefault(str(row["教学班ID"]), []).append(row.to_dict())

    # 第二步：组装证据包，筛选可疑教学班
    print("=== 第二步：组装证据包 ===")
    courses = load_courses(course_excel, weeks=num_weeks, days=num_days, periods=num_periods)
    teachers = load_teachers(teacher_excel, weeks=num_weeks, days=num_days, periods=num_periods)
    classrooms = load_classrooms(classroom_excel, weeks=num_weeks, days=num_days, periods=num_periods)
    all_jxbids, list_of_jas = collect_jxbids_for_preflight(courses, classrooms, None)

    # 送审依据 = 偏好与其他数据的交互信号，而非「填了偏好」本身：
    #   ① 规则版查出问题（已覆盖：偏好装不下周学时、偏好与禁止重叠、无法解析、
    #      指定教室/容量/校区/类型不可行、教师号缺失、周次异常等单班交互）
    #   ② 教师跨课程偏好冲突（规则版逐班检查覆盖不到，这里全局计算）
    #   ③ 偏好窗口临界：能生成的日模式只剩 1-2 种（规则版只报 0 种，1-2 种属于
    #      "勉强可行"，叠加全校占用后失败风险高）
    conflicts, sibling_ctx = detect_teacher_pref_conflicts(courses, all_jxbids)
    suspicious: List[str] = []
    n_rule = n_conf = n_tight = 0
    for jxbid in all_jxbids:
        if scope == "all":
            suspicious.append(jxbid)
            continue
        in_rule = jxbid in issues_by_jxbid
        in_conf = jxbid in conflicts
        tight = False
        if not in_rule and not in_conf:
            jxbs = get_consecutive_jxbid(jxbid, courses)
            if jxbs and _s(jxbs[0].Prefer_Time):
                tc = build_preferences(jxbs[0].Prefer_Time, jxbs[0].unavailable_Time)
                try:
                    patterns = new_generate_day_patterns(jxbs[0].ZXS, num_days, tc,
                                                         flag_reschedule=False)
                except ValueError:
                    patterns = []
                tight = 0 < len(patterns) <= 2
        if in_rule or in_conf or tight:
            suspicious.append(jxbid)
            n_rule += in_rule
            n_conf += in_conf
            n_tight += tight
    if scope == "all":
        print(f"全部教学班 {len(all_jxbids)} 个，scope=all 全量送审")
    else:
        print(f"全部教学班 {len(all_jxbids)} 个，送审 {len(suspicious)} 个："
              f"规则版有问题 {n_rule}，教师跨课程偏好冲突 {n_conf}，偏好窗口临界(日模式≤2) {n_tight}")
    if max_courses > 0:
        suspicious = suspicious[:max_courses]
        print(f"--max-courses 生效，仅处理前 {len(suspicious)} 个")

    evidences = []
    for jxbid in suspicious:
        ev = build_evidence(jxbid, courses, teachers, list_of_jas, num_days,
                            issues_by_jxbid.get(jxbid, []))
        if jxbid in conflicts:
            ev["教师跨课程偏好冲突"] = conflicts[jxbid]
            ev["同教师其他偏好课程"] = sibling_ctx.get(jxbid, [])
        evidences.append(ev)

    if dry_run:
        demo = evidences[:batch_size]
        print("=== dry-run：第一批 LLM 输入如下（不实际调用 API）===")
        print(json.dumps(demo, ensure_ascii=False, indent=2))
        return pd.DataFrame()

    # --resume：从已有 raw json 续跑，跳过已成功的教学班
    raw_records: List[Dict[str, Any]] = []
    if resume and os.path.isfile(raw_json_path):
        with open(raw_json_path, "r", encoding="utf-8") as f:
            raw_records = json.load(f)
        processed = {
            _s(r.get("教学班ID"))
            for rec in raw_records for r in (rec.get("输出") or [])
        }
        if processed:
            before = len(evidences)
            evidences = [e for e in evidences if e["教学班ID"] not in processed]
            print(f"--resume：已有 {len(processed)} 条结果，跳过，本次还需处理 {len(evidences)}/{before}")

    # 第三步：分批调用 LLM（每批结束即落盘 raw json，欠费等致命错误立即中止）
    print(f"=== 第三步：调用 LLM（模型 {llm_api.MODEL}，每批 {batch_size} 个）===")
    n_done = 0
    aborted = ""
    n_batches = (len(evidences) + batch_size - 1) // batch_size
    for i in range(0, len(evidences), batch_size):
        batch = evidences[i : i + batch_size]
        user_prompt = (
            f"学期共 {num_weeks} 周，每周 {num_days} 天，每天 {num_periods} 节。"
            f"以下是 {len(batch)} 个教学班的证据包，请逐个复核：\n"
            + json.dumps(batch, ensure_ascii=False)
        )
        try:
            result = llm_api.chat_json(SYSTEM_PROMPT, user_prompt)
        except llm_api.FatalLLMError as e:
            aborted = str(e)
            print(f"\n!!! 致命错误，中止 LLM 调用：{e}")
            print("!!! 已完成批次的结果会照常落盘；账号恢复后运行同样命令加 --resume 续跑。")
            break
        except RuntimeError as e:
            print(f"  批次 {i // batch_size + 1} 失败：{e}，跳过")
            result = []
        if isinstance(result, dict):
            result = [result]
        n_done += len(result)
        raw_records.append({"输入": batch, "输出": result})
        with open(raw_json_path, "w", encoding="utf-8") as f:
            json.dump(raw_records, f, ensure_ascii=False, indent=1)
        print(f"  批次 {i // batch_size + 1}/{n_batches} 完成，本次累计 {n_done} 条")

    # 第四步：整理输出 Excel（合并历史 raw 记录与本次结果）
    info_by_id = {e["教学班ID"]: e for e in evidences}
    all_results: List[Dict[str, Any]] = []
    for rec in raw_records:
        for e in rec.get("输入") or []:
            info_by_id.setdefault(_s(e.get("教学班ID")), e)
        all_results.extend(rec.get("输出") or [])
    rows = []
    for r in all_results:
        jxbid = _s(r.get("教学班ID"))
        ev = info_by_id.get(jxbid, {})
        problems = r.get("发现的问题") or []
        rows.append({
            "教学班ID": jxbid,
            "课程名称": ev.get("课程名称", ""),
            "开课单位": ev.get("开课单位", ""),
            "风险等级": _s(r.get("风险等级")),
            "可排性判断": _s(r.get("可排性判断")),
            "隐性矛盾": _s(r.get("隐性矛盾")),
            "问题数": len(problems),
            "问题明细": "；".join(
                f"[{p.get('严重程度','')}/{p.get('类别','')}] {p.get('描述','')}" for p in problems
            ),
            "修改建议": "；".join(_s(p.get("建议")) for p in problems if _s(p.get("建议"))),
            "偏好时间原文": ev.get("偏好时间原文", ""),
            "禁止时间原文": ev.get("禁止时间原文", ""),
            "规则版问题数": len(ev.get("规则版发现的问题") or []),
        })
    out_df = pd.DataFrame(rows)
    if not out_df.empty:
        risk_order = {"高": 0, "中": 1, "低": 2}
        out_df = out_df.sort_values(
            by="风险等级", key=lambda s: s.map(lambda x: risk_order.get(x, 3))
        )
    out_df.to_excel(output_path, index=False)
    print(f"LLM 排课前检查完成：共 {len(out_df)} 条，已写入 {output_path}")
    print(f"原始输入输出留档：{raw_json_path}")
    if aborted:
        print(f"\n注意：本次运行因致命错误提前中止，结果不完整。恢复后加 --resume 续跑。")
        sys.exit(2)
    return out_df


def main():
    parser = argparse.ArgumentParser(description="LLM 驱动的排课前合理性检查")
    parser.add_argument("--course", default=DEFAULT_COURSE)
    parser.add_argument("--teacher", default=DEFAULT_TEACHER)
    parser.add_argument("--classroom", default=DEFAULT_CLASSROOM)
    parser.add_argument("--banji", default=DEFAULT_BANJI)
    parser.add_argument("--weeks", type=int, default=20)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--periods", type=int, default=11)
    parser.add_argument("--batch-size", type=int, default=8, help="每次 LLM 请求包含的教学班数")
    parser.add_argument("--max-courses", type=int, default=0, help="只处理前 N 个可疑教学班（0=全部），调试用")
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--dry-run", action="store_true", help="只打印第一批 LLM 输入，不调用 API")
    parser.add_argument("--scope", choices=["issues", "all"], default="issues",
                        help="送审范围：issues=命中交互信号的班（规则版有问题/教师跨课程偏好冲突/"
                             "偏好窗口临界，默认）；all=全部教学班")
    parser.add_argument("--resume", action="store_true",
                        help="从已有 llm_preflight_raw.json 续跑，跳过已成功的教学班")
    args = parser.parse_args()

    run_llm_preflight(
        args.course, args.teacher, args.classroom, args.banji,
        args.weeks, args.days, args.periods,
        batch_size=args.batch_size, max_courses=args.max_courses,
        output_path=args.output, dry_run=args.dry_run,
        scope=args.scope, resume=args.resume,
    )


if __name__ == "__main__":
    main()
