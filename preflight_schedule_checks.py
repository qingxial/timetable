"""
排课前检查：校验课程需求合理性（教师、周次、偏好、教室等），导出 Excel 报告。
独立于正式排课流程，仅依赖 Basic_Data / utils1 / tryloadlimit。
"""
from __future__ import annotations

import argparse
import os
import re
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from Basic_Data import load_classrooms, load_courses, load_teachers, load_classes
from utils1 import (
    pre_selection,
    get_consecutive_jxbid,
    get_teaching_weeks,
    trans_week_flags,
    get_teacher_instances,
    filter_suitable_classrooms,
)
from tryloadlimit import (
    build_preferences,
    choose_calendar_shape,
    new_generate_day_patterns,
    parse_one_slot,
)


def _issue(
    jxbid: str,
    kcm: str,
    yxmc: str,
    category: str,
    priority: str,
    detail: str,
    suggestion: str,
) -> Dict[str, Any]:
    return {
        "教学班ID": jxbid,
        "课程名称": kcm,
        "开课单位": yxmc,
        "问题类别": category,
        "优先级": priority,
        "详细说明": detail,
        "修改建议": suggestion,
    }


def _expand_blocks_for_items(
    items: List[Dict[str, Any]], days_of_week: int, periods_per_day: int
) -> Dict[int, List[Tuple[int, int]]]:
    by_day: Dict[int, List[Tuple[int, int]]] = defaultdict(list)
    for item in items or []:
        days_spec = item.get("days", [])
        days_list = list(range(days_of_week)) if days_spec == "all" else list(days_spec or [])
        pspec = item.get("periods", [])
        if pspec == "all":
            blocks = [(0, periods_per_day - 1)]
        else:
            blocks = []
            for pr in pspec or []:
                if isinstance(pr, (list, tuple)) and len(pr) == 2:
                    s, e = int(pr[0]), int(pr[1])
                    if s <= e:
                        blocks.append((s, e))
        for d in days_list:
            for b in blocks:
                by_day[d].append(b)
    return by_day


def _ranges_overlap(a: Tuple[int, int], b: Tuple[int, int]) -> bool:
    return not (a[1] < b[0] or b[1] < a[0])


def _pref_unavail_conflict(
    prefer_cell: str, unavail_cell: str, time_constraints: Dict[str, Any]
) -> List[str]:
    """检测偏好窗口与禁止时段在同一星期、节次上是否相交。"""
    days_of_week, periods_per_day = choose_calendar_shape(time_constraints)
    conflicts: List[str] = []
    pref_opts = (time_constraints.get("preferred_options") or []) + [
        slot for grp in (time_constraints.get("preferred_groups") or []) for slot in grp
    ]
    unavail = time_constraints.get("unavailable") or []
    pb = _expand_blocks_for_items(pref_opts, days_of_week, periods_per_day)
    ub = _expand_blocks_for_items(unavail, days_of_week, periods_per_day)
    for d in range(days_of_week):
        for p1 in pb.get(d, []):
            for p2 in ub.get(d, []):
                if _ranges_overlap(p1, p2):
                    conflicts.append(f"第{d + 1}天 偏好区间{p1} 与 禁止区间{p2} 重叠")
    return conflicts


def _raw_segments_from_cell(cell: str) -> List[str]:
    if not cell or not isinstance(cell, str):
        return []
    s = cell.strip()
    if not s:
        return []
    parts = re.split(r"[；;\n]+", s)
    out = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        out.extend([x.strip() for x in re.split(r"[、，,]+", p) if x.strip()])
    return out


def _unparsed_preference_fragments(prefer_cell: str, unavail_cell: str) -> List[str]:
    bad = []
    for label, cell in (("偏好时间", prefer_cell), ("禁止时间", unavail_cell)):
        for seg in _raw_segments_from_cell(cell):
            slot = parse_one_slot(seg)
            days = slot.get("days", [])
            pspec = slot.get("periods", [])
            if days == [] and pspec == []:
                bad.append(f"{label}: «{seg}»")
    return bad


def collect_jxbids_for_preflight(
    courses, classrooms, combinations_path: Optional[str]
) -> Tuple[List[str], List]:
    """与正式排课范围对齐：按筛选条件组合并集；无组合文件时用 (None, None) 全局筛选。"""
    list_of_jas = [c for c in classrooms if str(c.SFYXPK).strip() in ("1", "1.0")]
    jxbids: set = set()
    if combinations_path and os.path.isfile(combinations_path):
        df = pd.read_excel(combinations_path)
        for _, row in df.iterrows():
            dept = row["开课单位"] if pd.notna(row.get("开课单位")) else None
            ctype = row["课程类别"] if pd.notna(row.get("课程类别")) else None
            jxbid_dict, _, _ = pre_selection(courses, classrooms, ctype, dept, None)
            jxbids.update(jxbid_dict.keys())
    else:
        jxbid_dict, _, _ = pre_selection(courses, classrooms, None, None, None)
        jxbids.update(jxbid_dict.keys())
    return sorted(jxbids), list_of_jas


def _load_optional_jxbid_credit(course_excel: str) -> Dict[str, float]:
    """第二行英文表头与 Basic_Data 一致时，尝试读取 JXBID 与学分列。"""
    credit_col = None
    for name in ("XF", "学分", "credit"):
        try:
            df = pd.read_excel(course_excel, header=1)
            if name in df.columns and "JXBID" in df.columns:
                credit_col = name
                break
        except Exception:
            return {}
    if not credit_col:
        return {}
    df = pd.read_excel(course_excel, header=1)
    m: Dict[str, float] = {}
    for _, row in df.iterrows():
        jid = row.get("JXBID")
        if pd.isna(jid):
            continue
        v = row.get(credit_col)
        if pd.notna(v):
            try:
                m[str(jid)] = float(v)
            except (TypeError, ValueError):
                pass
    return m


def run_preflight(
    course_excel: str,
    teacher_excel: str,
    classroom_excel: str,
    banji_excel: str,
    num_weeks: int,
    num_days: int,
    num_periods: int,
    combinations_path: Optional[str] = None,
    output_path: Optional[str] = None,
) -> pd.DataFrame:
    teachers = load_teachers(teacher_excel, weeks=num_weeks, days=num_days, periods=num_periods)
    classrooms = load_classrooms(classroom_excel, weeks=num_weeks, days=num_days, periods=num_periods)
    courses = load_courses(course_excel, weeks=num_weeks, days=num_days, periods=num_periods)
    classes = load_classes(banji_excel, weeks=num_weeks, days=num_days, periods=num_periods)

    teacher_by_jsh = {t.JSH: t for t in teachers}
    xm_to_jshs: Dict[str, List[str]] = defaultdict(list)
    for t in teachers:
        xm_to_jshs[str(t.XM).strip()].append(t.JSH)

    credit_by_jxb = _load_optional_jxbid_credit(course_excel)

    issues: List[Dict[str, Any]] = []
    dup_name_rows: List[Dict[str, str]] = []
    for xm, jshs in xm_to_jshs.items():
        if len(set(jshs)) > 1:
            dup_name_rows.append({"教师姓名": xm, "教师号列表": ",".join(sorted(set(jshs)))})

    jxbid_list, list_of_jas = collect_jxbids_for_preflight(courses, classrooms, combinations_path)

    for jxbid in jxbid_list:
        jxbs = get_consecutive_jxbid(jxbid, courses)
        if not jxbs:
            continue
        head = jxbs[0]
        kcm, yxmc = head.KCM, head.YXMC

        missing_jsh_reported = set()
        for jxb in jxbs:
            jsh = jxb.JSH
            if jsh not in teacher_by_jsh and jsh not in missing_jsh_reported:
                missing_jsh_reported.add(jsh)
                issues.append(
                    _issue(
                        jxbid,
                        kcm,
                        yxmc,
                        "教师匹配",
                        "三级",
                        f"教师号 {jsh}（{jxb.XM}）不在教师名单中",
                        "在教务库核对教师号；同名教师请按开课院系与工号区分",
                    )
                )

        sk = head.SKZCDM
        if sk is None or str(sk).strip() == "":
            issues.append(
                _issue(
                    jxbid,
                    kcm,
                    yxmc,
                    "上课周次",
                    "二级",
                    "SKZCDM 为空",
                    "补全上课周次编码；单双周需在周次串中可区分或单独字段标注",
                )
            )
        else:
            s = str(sk)
            tw = trans_week_flags(s)
            if not tw:
                issues.append(
                    _issue(
                        jxbid,
                        kcm,
                        yxmc,
                        "上课周次",
                        "二级",
                        "SKZCDM 解析后无上课周（全 0 或无效）",
                        "核对周次位串是否与学期周数一致；避免占位全零串",
                    )
                )
            if len(s) < num_weeks:
                issues.append(
                    _issue(
                        jxbid,
                        kcm,
                        yxmc,
                        "上课周次",
                        "二级",
                        f"SKZCDM 长度 {len(s)} 小于学期周数 {num_weeks}",
                        "按校历补足周次位串长度或调整 num_weeks 配置",
                    )
                )

        zxs = head.ZXS
        try:
            zxs_int = int(zxs)
        except (TypeError, ValueError):
            zxs_int = -1
        if zxs_int <= 0 or zxs_int > 8:
            issues.append(
                _issue(
                    jxbid,
                    kcm,
                    yxmc,
                    "周学时",
                    "二级",
                    f"ZXS={zxs} 不在有效范围 (0,8]",
                    "核对课程周学时；当前排课筛选仅支持 1–8",
                )
            )

        course_weeks_set = set(get_teaching_weeks(jxbs))
        for jxb in jxbs:
            tw_t = trans_week_flags(jxb.RWJSZCDM) if jxb.RWJSZCDM else []
            if tw_t and course_weeks_set:
                if not course_weeks_set.intersection(tw_t):
                    issues.append(
                        _issue(
                            jxbid,
                            kcm,
                            yxmc,
                            "教师周次与课程周次",
                            "二级",
                            f"教师 {jxb.XM}({jxb.JSH}) 的 RWJSZCDM 周集合与课程 SKZCDM 周集合无交集",
                            "对齐教师任务周次与教学班上课周次",
                        )
                    )

        tc = build_preferences(head.Prefer_Time, head.unavailable_Time)
        conf = _pref_unavail_conflict(head.Prefer_Time, head.unavailable_Time, tc)
        for c in conf[:5]:
            issues.append(
                _issue(
                    jxbid,
                    kcm,
                    yxmc,
                    "时间偏好",
                    "一级",
                    c,
                    "调整偏好或禁止时间使其不重叠；建立偏好填写 SOP",
                )
            )
        if len(conf) > 5:
            issues.append(
                _issue(
                    jxbid,
                    kcm,
                    yxmc,
                    "时间偏好",
                    "一级",
                    f"另有 {len(conf) - 5} 处偏好与禁止时段重叠",
                    "整体检查 Prefer_Time 与 unavailable_Time",
                )
            )

        bad_fragments = _unparsed_preference_fragments(head.Prefer_Time, head.unavailable_Time)
        for frag in bad_fragments[:8]:
            issues.append(
                _issue(
                    jxbid,
                    kcm,
                    yxmc,
                    "时间偏好",
                    "一级",
                    f"无法解析的片段: {frag}",
                    "按 tryloadlimit 支持格式填写（如 周一（1-4节）、周二全天、全周（1-2节））",
                )
            )

        pref_format_error = False
        try:
            patterns = new_generate_day_patterns(head.ZXS, num_days, tc, flag_reschedule=False)
        except ValueError as e:
            pref_format_error = True
            issues.append(
                _issue(
                    jxbid,
                    kcm,
                    yxmc,
                    "时间偏好",
                    "一级",
                    f"偏好组格式错误: {e}",
                    "偏好组 [ ] 内每天须为单日列表且 periods 为单一连续块，且周学时总和等于 ZXS",
                )
            )
            patterns = []
        if not patterns and not pref_format_error:
            issues.append(
                _issue(
                    jxbid,
                    kcm,
                    yxmc,
                    "时间偏好",
                    "一级",
                    "在周学时与偏好约束下无法生成任何上课日模式（含 preferred_groups 与周学时不匹配等情况）",
                    "放宽偏好、修正偏好组总学时或调整 ZXS",
                )
            )

        strict_jas = filter_suitable_classrooms(list_of_jas, head, relax_constraints=False)
        relaxed_jas = filter_suitable_classrooms(list_of_jas, head, relax_constraints=True)
        if not relaxed_jas:
            issues.append(
                _issue(
                    jxbid,
                    kcm,
                    yxmc,
                    "教室静态可行性",
                    "二级",
                    "放宽条件后仍无可用教室（容量/校区/类型/指定教室等）",
                    "核对 SKXQ、KRL、JASLXMC、JXLDM、JASDM/LSJASDM 与教室资源表",
                )
            )
        elif not strict_jas:
            issues.append(
                _issue(
                    jxbid,
                    kcm,
                    yxmc,
                    "教室静态可行性",
                    "二级",
                    "严格条件下无教室，放宽指定教室/教学楼后可匹配",
                    "若可接受非指定教室，可清空或调整指定教室字段",
                )
            )

        xf = credit_by_jxb.get(jxbid)
        if xf is not None and zxs_int > 0:
            if xf > 0 and (zxs_int / xf) > 8:
                issues.append(
                    _issue(
                        jxbid,
                        kcm,
                        yxmc,
                        "学时学分",
                        "三级",
                        f"周学时/学分比异常: ZXS={zxs_int}, 学分={xf}",
                        "核对培养方案中学分与周学时是否录入一致",
                    )
                )

    df_issues = pd.DataFrame(issues)
    df_dup = pd.DataFrame(dup_name_rows)

    out = output_path or os.path.join("排课结果", "排课前检查报告.xlsx")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        df_issues.to_excel(writer, sheet_name="检查明细", index=False)
        df_dup.to_excel(writer, sheet_name="同名教师预警", index=False)

    return df_issues


def main():
    parser = argparse.ArgumentParser(description="排课前需求合理性检查")
    parser.add_argument("--root", default="智能排课基础数据", help="基础数据目录")
    parser.add_argument("--course", default=None, help="课程表 xlsx")
    parser.add_argument("--teacher", default=None, help="教师表 xlsx")
    parser.add_argument("--classroom", default=None, help="教室表 xlsx")
    parser.add_argument("--banji", default=None, help="班级表 xlsx")
    parser.add_argument("--combinations", default="筛选条件组合.xlsx", help="筛选条件组合，不存在则按全局筛选")
    parser.add_argument("--weeks", type=int, default=17)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--periods", type=int, default=11)
    parser.add_argument("--output", default=None, help="输出 Excel 路径")
    args = parser.parse_args()

    root = args.root
    course = args.course or os.path.join(root, "课程表2025-2026-1.xlsx")
    teacher = args.teacher or os.path.join(root, "更新后的教师名单2025-2026-1.xlsx")
    classroom = args.classroom or os.path.join(root, "更新后的教室表.xlsx")
    banji = args.banji or os.path.join(root, "班级汇总2025-2026-1.xlsx")

    combo = args.combinations if os.path.isfile(args.combinations) else None

    df = run_preflight(
        course,
        teacher,
        classroom,
        banji,
        args.weeks,
        args.days,
        args.periods,
        combinations_path=combo,
        output_path=args.output,
    )
    path = args.output or os.path.join("排课结果", "排课前检查报告.xlsx")
    print(f"排课前检查完成，共 {len(df)} 条记录，已写入: {path}")


if __name__ == "__main__":
    main()
