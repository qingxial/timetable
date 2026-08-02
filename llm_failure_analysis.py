"""
LLM 驱动的排课失败归因与调整建议（中期报告 3.1 节「将结构化日志与冲突上下文输入 LLM，
生成自然语言原因解释与可操作的调整建议」的实现，构成「归因—调整—重排」闭环）。

直接运行：python llm_failure_analysis.py        （需配置 DASHSCOPE_API_KEY）
调试：    python llm_failure_analysis.py --max-courses 5 --dry-run

【输入映射】失败课程通常只有几十个，但每个的上下文很大（全校占用矩阵）。
因此不喂原始时间表，而是复用规则版 postfailure_analysis.diagnose_jxbid 已经算好的
结构化诊断作为证据包，每个失败教学班包含：
  - 规则版原因类别 / 详细说明 / 修改建议（规则口径，供 LLM 校正与深化）
  - 枚举拒绝构成：在所有候选教室×日模式下试探时被拒的原因计数
    （偏好窗口外×N、教师占用×N、教室占用×N……——失败的定量指纹）
  - 教师占用摘要：相关教师在教学周内已占课情况
  - 时间偏好 / 地点偏好的原始文本与解析摘要
【输出映射】每个失败教学班一条 JSON：
  {教学班ID, 失败主因(固定分类), 原因解释(自然语言，面向教务人员),
   调整建议:[{操作, 预期效果, 优先级(高/中/低)}], 置信度(0-1)}
失败主因分类（与中期报告归因口径对齐）：
  教师信息缺失或数据错误 / 教师时间窗已占满 / 无满足条件的教室资源 /
  时间约束矛盾或过窄 / 班级时间冲突 / 排课顺序或策略原因
落盘：
  - 排课结果/LLM失败归因与建议.xlsx
  - 排课结果/llm_failure_raw.json      （原始输入输出留档，供论文实验复现）
  - 排课结果/失败课程归因与建议.xlsx    （规则版报告，作为对照同时生成）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List

import pandas as pd

from postfailure_analysis import (
    run_postfailure_analysis,
    resolve_default_failed_excel,
    resolve_default_pickle,
)
import llm_api


RESULTS_DIR = "排课结果"
DEFAULT_OUTPUT = os.path.join(RESULTS_DIR, "LLM失败归因与建议.xlsx")
DEFAULT_RAW_JSON = os.path.join(RESULTS_DIR, "llm_failure_raw.json")
RULE_REPORT = os.path.join(RESULTS_DIR, "失败课程归因与建议.xlsx")

CAUSE_CATEGORIES = [
    "教师信息缺失或数据错误",
    "教师时间窗已占满",
    "无满足条件的教室资源",
    "时间约束矛盾或过窄",
    "班级时间冲突",
    "排课顺序或策略原因",
]

SYSTEM_PROMPT = f"""你是高校智能排课系统的失败归因专家。贪心排课与变邻域调课后仍有少量教学班排课失败，规则程序已对每个失败教学班生成了结构化诊断证据，请你基于证据做最终归因，并给出教务人员能直接执行的调整建议。

证据字段说明：
- 「原因类别/详细说明/修改建议」：规则程序的初步判断，可能粗糙或有误，你需要校正与深化；
- 「枚举拒绝构成」：程序在所有候选教室×日模式下试探每个候选时段时，各拒绝原因的累计次数（如 教师占用×120；偏好窗口外×80）。这是失败的定量指纹，但注意枚举有顺序（先查时间规则再查资源占用），次数最多的不一定是业务根因；
- 「教师占用摘要」：相关教师在教学周内的已占课情况；
- 「时间偏好/地点偏好」：课程的原始约束文本与解析摘要。

你的任务，对每个失败教学班输出：
1. 失败主因：必须从以下固定分类中选一个：{ "、".join(CAUSE_CATEGORIES) }；
2. 原因解释：3-5 句自然语言，面向不懂算法的教务人员，说清楚"为什么这门课排不上"，要引用证据中的具体数字或事实，不要套话；
3. 调整建议：2-4 条，每条包含 操作（具体改哪个字段/资源，怎么改）、预期效果、优先级（高/中/低）。建议必须可操作，如"将指定教室 X 改为同教学楼容量≥N 的教室"而非"放宽约束"；
4. 置信度：0-1 之间的小数，表示你对主因判断的把握。

【重要】只能引用证据中明确给出的事实和数字。证据包的「教室资源快照」列出了真实存在的教室（代码/名称/容量/校区/类型），推荐教室时只能从中选择；严禁编造任何教室名称、容量、校区设施等证据之外的信息。若证据不足以给出具体建议，就在建议中写明"需教务进一步核实：……"。

输出要求：严格输出 JSON 数组，不要任何解释文字或 Markdown 围栏：
[{{"教学班ID": "...", "失败主因": "...", "原因解释": "...",
   "调整建议": [{{"操作": "...", "预期效果": "...", "优先级": "高|中|低"}}], "置信度": 0.9}}]"""


def _s(v: Any) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    return str(v).strip()


def _room_brief(c: Any) -> Dict[str, Any]:
    return {
        "教室代码": _s(getattr(c, "JASDM", "")),
        "教室名称": _s(getattr(c, "JASMC", "")),
        "容量": getattr(c, "SKZWS", ""),
        "校区": _s(getattr(c, "MC", "")),
        "教室类型": _s(getattr(c, "JASLX", "")),
        "教学楼": _s(getattr(c, "JXLMC", "")) or _s(getattr(c, "JXLDM", "")),
    }


def build_room_snapshot(jxbid: str, courses: List[Any], classrooms: List[Any]) -> Dict[str, Any]:
    """从教室表查出真实可引用的资源事实，防止 LLM 编造教室信息。"""
    from utils1 import get_consecutive_jxbid

    jxbs = get_consecutive_jxbid(jxbid, courses)
    if not jxbs:
        return {}
    head = jxbs[0]
    campus = _s(head.SKXQ)
    rtype = _s(head.JASLXMC) or _s(head.JASLXDM)
    pk_rooms = [c for c in classrooms if str(c.SFYXPK).strip() in ("1", "1.0")]

    def _cap(c):
        try:
            return int(c.SKZWS)
        except (TypeError, ValueError):
            return -1

    same_campus_type = sorted(
        (c for c in pk_rooms
         if (not campus or _s(c.MC) == campus) and (not rtype or _s(c.JASLX) == rtype)),
        key=_cap, reverse=True,
    )
    same_type = sorted(
        (c for c in pk_rooms if not rtype or _s(c.JASLX) == rtype),
        key=_cap, reverse=True,
    )
    return {
        "课程要求校区": campus or "(未限定)",
        "课程要求教室类型": rtype or "(未限定)",
        "课容量": head.KRL,
        "同校区同类型容量Top3": [_room_brief(c) for c in same_campus_type[:3]],
        "全校同类型容量Top3": [_room_brief(c) for c in same_type[:3]],
    }


def evidence_from_rule_row(row: pd.Series) -> Dict[str, Any]:
    """把规则版归因报告的一行转成 LLM 证据包。"""
    return {
        "教学班ID": _s(row.get("教学班ID")),
        "课程名称": _s(row.get("课程名称")),
        "规则版原因类别": _s(row.get("原因类别")),
        "规则版优先级": _s(row.get("优先级")),
        "规则版详细说明": _s(row.get("详细说明")),
        "规则版修改建议": _s(row.get("修改建议")),
        "首要枚举冲突": _s(row.get("首要枚举冲突")),
        "枚举拒绝构成": _s(row.get("枚举拒绝构成")),
        "教师占用摘要": _s(row.get("教师占用摘要")),
        "时间偏好": _s(row.get("时间偏好")),
        "地点偏好": _s(row.get("地点偏好")),
    }


def run_llm_failure_analysis(
    failed_excel: str = None,
    pickle_path: str = None,
    num_weeks: int = 20,
    num_days: int = 7,
    num_periods: int = 11,
    batch_size: int = 5,
    max_courses: int = 0,
    output_path: str = DEFAULT_OUTPUT,
    raw_json_path: str = DEFAULT_RAW_JSON,
    dry_run: bool = False,
) -> pd.DataFrame:
    os.makedirs(RESULTS_DIR, exist_ok=True)
    failed_excel = failed_excel or resolve_default_failed_excel(RESULTS_DIR)
    pickle_path = pickle_path or resolve_default_pickle(RESULTS_DIR)
    print(f"失败清单：{failed_excel}")
    print(f"时间表状态：{pickle_path or '(无 pkl，将从 Excel 加载基础数据)'}")

    # --max-courses 在规则归因前截断，调试时不必诊断全部失败课程
    rule_report_path = RULE_REPORT
    if max_courses > 0:
        df_subset = pd.read_excel(failed_excel).head(max_courses)
        failed_excel = os.path.join(RESULTS_DIR, "_tmp_failed_subset.xlsx")
        df_subset.to_excel(failed_excel, index=False)
        rule_report_path = os.path.join(RESULTS_DIR, "_tmp_规则归因_subset.xlsx")
        print(f"--max-courses 生效，仅处理前 {len(df_subset)} 条")

    # 第一步：规则版归因（产出结构化证据，同时落盘规则版报告作为对照）
    print("=== 第一步：运行规则版失败归因 ===")
    rule_df = run_postfailure_analysis(
        failed_excel, rule_report_path,
        pickle_path=pickle_path,
        num_weeks=num_weeks, num_days=num_days, num_periods=num_periods,
    )
    print(f"规则版归因完成：{len(rule_df)} 条，已写入 {rule_report_path}")

    # 给每个证据包附上真实教室资源快照（LLM 推荐教室时只能从中选）
    print("附加教室资源快照……")
    from Basic_Data import load_courses, load_classrooms
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "智能排课基础数据", "提取的基础数据表_converted")
    snap_courses = load_courses(os.path.join(base, "课程表.xlsx"),
                                weeks=num_weeks, days=num_days, periods=num_periods)
    snap_rooms = load_classrooms(os.path.join(base, "教室表.xlsx"),
                                 weeks=num_weeks, days=num_days, periods=num_periods)
    evidences = []
    for _, row in rule_df.iterrows():
        ev = evidence_from_rule_row(row)
        ev["教室资源快照"] = build_room_snapshot(ev["教学班ID"], snap_courses, snap_rooms)
        evidences.append(ev)

    if dry_run:
        print("=== dry-run：第一批 LLM 输入如下（不实际调用 API）===")
        print(json.dumps(evidences[:batch_size], ensure_ascii=False, indent=2))
        return pd.DataFrame()

    # 第二步：分批调用 LLM
    print(f"=== 第二步：调用 LLM（模型 {llm_api.MODEL}，每批 {batch_size} 个）===")
    all_results: List[Dict[str, Any]] = []
    raw_records: List[Dict[str, Any]] = []
    aborted = ""
    for i in range(0, len(evidences), batch_size):
        batch = evidences[i : i + batch_size]
        user_prompt = (
            f"学期共 {num_weeks} 周，每周 {num_days} 天，每天 {num_periods} 节。"
            f"以下是 {len(batch)} 个排课失败教学班的诊断证据，请逐个归因：\n"
            + json.dumps(batch, ensure_ascii=False)
        )
        try:
            result = llm_api.chat_json(SYSTEM_PROMPT, user_prompt)
        except llm_api.FatalLLMError as e:
            aborted = str(e)
            print(f"\n!!! 致命错误，中止 LLM 调用：{e}")
            print("!!! 已完成批次的结果会照常落盘。")
            break
        except RuntimeError as e:
            print(f"  批次 {i // batch_size + 1} 失败：{e}，跳过")
            result = []
        if isinstance(result, dict):
            result = [result]
        all_results.extend(result)
        raw_records.append({"输入": batch, "输出": result})
        with open(raw_json_path, "w", encoding="utf-8") as f:
            json.dump(raw_records, f, ensure_ascii=False, indent=1)
        print(f"  批次 {i // batch_size + 1}/{(len(evidences) + batch_size - 1) // batch_size} 完成")

    # 第三步：整理输出 Excel（保留规则版结论作对照列）
    rule_by_id = {_s(r.get("教学班ID")): r for _, r in rule_df.iterrows()}
    rows = []
    for r in all_results:
        jxbid = _s(r.get("教学班ID"))
        rule_row = rule_by_id.get(jxbid, {})
        suggestions = r.get("调整建议") or []
        rows.append({
            "教学班ID": jxbid,
            "课程名称": _s(rule_row.get("课程名称")) if hasattr(rule_row, "get") else "",
            "失败主因(LLM)": _s(r.get("失败主因")),
            "原因解释(LLM)": _s(r.get("原因解释")),
            "调整建议(LLM)": "\n".join(
                f"{idx}. [{_s(sg.get('优先级'))}] {_s(sg.get('操作'))} → {_s(sg.get('预期效果'))}"
                for idx, sg in enumerate(suggestions, 1)
            ),
            "置信度": r.get("置信度", ""),
            "原因类别(规则版对照)": _s(rule_row.get("原因类别")) if hasattr(rule_row, "get") else "",
            "枚举拒绝构成": _s(rule_row.get("枚举拒绝构成")) if hasattr(rule_row, "get") else "",
        })
    out_df = pd.DataFrame(rows)
    out_df.to_excel(output_path, index=False)
    print(f"LLM 失败归因完成：共 {len(out_df)} 条，已写入 {output_path}")
    print(f"原始输入输出留档：{raw_json_path}")

    if not out_df.empty and "失败主因(LLM)" in out_df.columns:
        print("\n失败主因分布：")
        print(out_df["失败主因(LLM)"].value_counts().to_string())
    if aborted:
        print("\n注意：本次运行因致命错误提前中止，结果不完整。")
        sys.exit(2)
    return out_df


def main():
    parser = argparse.ArgumentParser(description="LLM 驱动的排课失败归因与调整建议")
    parser.add_argument("--failed", default=None,
                        help="失败课程 Excel；默认优先 调课失败的排课失败课程.xlsx")
    parser.add_argument("--pickle", default=None,
                        help="时间表 pkl；默认优先 reschedule_timetables.pkl")
    parser.add_argument("--weeks", type=int, default=20)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--periods", type=int, default=11)
    parser.add_argument("--batch-size", type=int, default=5)
    parser.add_argument("--max-courses", type=int, default=0, help="只处理前 N 条（0=全部），调试用")
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--dry-run", action="store_true", help="只打印第一批 LLM 输入，不调用 API")
    args = parser.parse_args()

    run_llm_failure_analysis(
        failed_excel=args.failed, pickle_path=args.pickle,
        num_weeks=args.weeks, num_days=args.days, num_periods=args.periods,
        batch_size=args.batch_size, max_courses=args.max_courses,
        output_path=args.output, dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
