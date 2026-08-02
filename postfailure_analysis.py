"""
排课/调课后失败分析：读取失败课程清单，按与 schedule_one_round 一致的逻辑做归因与建议；
可选加载 pkl 增强教师占用描述；用户确认后可子进程重新执行排课脚本。

默认失败清单：若存在「排课结果/调课失败的排课失败课程.xlsx」（调课后仍失败），优先用它；
否则用「排课结果/排课失败课程_全部.xlsx」（仅初次排课失败汇总，条数通常更多）。
默认 pkl：优先 reschedule_timetables.pkl（调课后状态），否则 saved_timetables.pkl。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from utils1 import (
    get_consecutive_jxbid,
    get_teaching_weeks,
    trans_week_flags,
    get_teacher_instances,
    filter_suitable_classrooms,
    get_indices,
    get_class_instances1,
)
from tryloadlimit import build_preferences, new_generate_day_patterns, new_check_period_availability


_REJECT_LABEL_ZH = {
    "outside_pref_window": "偏好窗口外",
    "unavailable_overlap": "禁止时段",
    "classroom_occupied": "教室占用",
    "teacher_occupied": "教师占用",
    "class_occupied": "班级占用",
    "segment_boundary": "分段/连排边界",
    "pattern_group_day_mismatch": "日模式与偏好组不一致",
}

# 枚举拒绝累计拆分：用于区分「时间规则」与「资源占用」（教室/教师/班级）
_TIME_RULE_REJECT_KEYS = frozenset(
    {"outside_pref_window", "unavailable_overlap", "pattern_group_day_mismatch", "segment_boundary"}
)
_RESOURCE_REJECT_KEYS = frozenset({"classroom_occupied", "teacher_occupied", "class_occupied"})


def _format_reject_breakdown(merged: Dict[str, int]) -> Tuple[str, str]:
    """返回 (首要类型中文, 构成摘要)。按枚举被拒绝次数降序。"""
    if not merged:
        return "", ""
    items = sorted(merged.items(), key=lambda x: (-x[1], x[0]))
    top_key = items[0][0]
    primary = _REJECT_LABEL_ZH.get(top_key, top_key)
    parts = [f"{_REJECT_LABEL_ZH.get(k, k)}×{v}" for k, v in items[:8]]
    summary = "；".join(parts)
    return primary, summary


def _format_time_preference_summary(jxb: Any, tc: Optional[Dict[str, Any]]) -> str:
    """时间侧约束可读摘要：原始偏好/禁止 + 解析后的分组与不可用块（凡与时间网格有关的限制）。"""
    pt = getattr(jxb, "Prefer_Time", None)
    ut = getattr(jxb, "unavailable_Time", None)
    raw_pt = "(空)" if pt is None or (isinstance(pt, str) and not str(pt).strip()) else str(pt).strip()
    raw_ut = "(空)" if ut is None or (isinstance(ut, str) and not str(ut).strip()) else str(ut).strip()
    parts = [f"Prefer_Time={raw_pt}", f"unavailable_Time={raw_ut}"]
    if tc is not None:
        pg = tc.get("preferred_groups") or []
        po = tc.get("preferred_options") or []
        pu = tc.get("unavailable") or []
        parts.append(f"解析后 preferred_groups={len(pg)}组")
        parts.append(f"preferred_options={len(po)}条")
        parts.append(f"禁止时段解析块={len(pu)}段")
        zxs = getattr(jxb, "ZXS", None)
        if zxs is not None:
            parts.append(f"ZXS={zxs}")
    else:
        parts.append("(偏好尚未解析)")
    return " | ".join(parts)


def _norm_str(v: Any) -> str:
    if v is None:
        return ""
    return str(v).strip()


def _lookup_classroom_by_jasdm(classrooms: List[Any], jasdm: str) -> Optional[Any]:
    key = _norm_str(jasdm)
    if not key:
        return None
    for c in classrooms:
        if _norm_str(getattr(c, "JASDM", "")) == key:
            return c
    return None


def _campus_label_from_room(c: Any) -> str:
    """教室表校区展示：优先校区名称 MC，不附带校区代码括号。"""
    mc = _norm_str(getattr(c, "MC", ""))
    xq = _norm_str(getattr(c, "XXXQDM", ""))
    if mc:
        return mc
    return xq or "(校区空)"


def _format_location_preference_summary(jxb: Any, classrooms_lookup: Optional[List[Any]] = None) -> str:
    """地点侧约束：课程行原始字段 +（仅对明确的 JASDM/JXLDM）教室表核对校区等。

    - 指定教室代码 JASDM：查教室表补校区、教学楼、教室类型、教室名称、教室容量(SKZWS)；不与课程行 LSJASDM 联动。
    - 仅教学楼代码 JXLDM（且无明确 JASDM）：按教学楼汇总教室表中的校区。
    - 历年教室 LSJASDM：不在此列输出（不参与教室表核对）。
    """
    rooms = classrooms_lookup or []
    pairs = [
        ("SKXQ", "课程行校区"),
        ("KRL", "课容量"),
        ("JASLXDM", "教室类型代码"),
        ("JASLXMC", "教室类型名称"),
        ("JXLDM", "教学楼代码"),
        ("JASDM", "指定教室代码"),
    ]
    chunks = []
    for attr, label in pairs:
        v = getattr(jxb, attr, None)
        if v is None or (isinstance(v, str) and not str(v).strip()):
            continue
        chunks.append(f"{label}={v}")

    base = " | ".join(chunks) if chunks else "(课程行未填写额外地点/教室约束)"

    jasdm_explicit = _norm_str(getattr(jxb, "JASDM", None))
    jxldm_explicit = _norm_str(getattr(jxb, "JXLDM", None))

    extra: List[str] = []
    if rooms:
        # 明确指定教室：教室表 → 校区 + 教学楼 + 教室类型 + 容量（及名称）
        if jasdm_explicit:
            hit = _lookup_classroom_by_jasdm(rooms, jasdm_explicit)
            if hit:
                jxlmc = _norm_str(getattr(hit, "JXLMC", ""))
                jxldm_tbl = _norm_str(getattr(hit, "JXLDM", ""))
                jxl_show = f"{jxlmc}({jxldm_tbl})" if jxlmc and jxldm_tbl else (jxlmc or jxldm_tbl or "(教学楼空)")
                jslx = _norm_str(getattr(hit, "JASLX", "")) or "(教室类型空)"
                jsmc = _norm_str(getattr(hit, "JASMC", ""))
                skzws = getattr(hit, "SKZWS", None)
                cap_txt = ""
                if skzws is not None and str(skzws).strip() != "":
                    cap_txt = f" | 教室容量(上课座位数)={skzws}"
                seg = (
                    f"[教室表·指定教室] 校区={_campus_label_from_room(hit)}"
                    f" | 教学楼={jxl_show} | 教室类型={jslx}"
                    f"{cap_txt}"
                )
                if jsmc:
                    seg += f" | 教室名称={jsmc}"
                if jxldm_explicit and jxldm_tbl != jxldm_explicit:
                    seg += f" | 提示:课程行教学楼≠教室表({jxldm_explicit} vs {jxldm_tbl})"
                extra.append(seg)
            else:
                extra.append(f"[教室表·指定教室] 代码 {jasdm_explicit} 未在教室表中匹配")

        # 仅明确教学楼、且无指定教室：教室表看校区
        elif jxldm_explicit:
            matches = [c for c in rooms if _norm_str(getattr(c, "JXLDM", "")) == jxldm_explicit]
            if not matches:
                extra.append(f"[教室表·教学楼] 代码 {jxldm_explicit} 未在教室表中匹配")
            else:
                campus_labels: List[str] = []
                seen = set()
                for c in matches:
                    lbl = _campus_label_from_room(c)
                    if lbl not in seen:
                        seen.add(lbl)
                        campus_labels.append(lbl)
                if len(campus_labels) == 1:
                    extra.append(f"[教室表·教学楼] 教学楼 {jxldm_explicit} 对应校区={campus_labels[0]}")
                else:
                    extra.append(
                        f"[教室表·教学楼] 教学楼 {jxldm_explicit} 在教室表中出现多校区: "
                        f"{' ; '.join(campus_labels)}"
                    )

    if extra:
        return base + " || " + " || ".join(extra)
    return base


def _aggregate_reject_stats(
    jxbs: List[Any],
    list_of_jas: List[Any],
    jasdm_dict: Dict[str, int],
    teachers,
    classes,
    courses,
    num_days: int,
    relax_constraints: bool,
) -> Dict[str, int]:
    """对放宽教室下的所有候选教室 × 日模式 × 调用 new_check_period_availability，合并 reject_stats。"""
    list_of_jxbs = jxbs
    teaching_weeks = get_teaching_weeks(list_of_jxbs)
    list_of_JSHs = [jxb.JSH for jxb in list_of_jxbs]
    list_of_teachers = get_teacher_instances(list_of_JSHs, teachers)
    list_of_teacher_week = [
        trans_week_flags(jxb.RWJSZCDM) if hasattr(jxb, "RWJSZCDM") and jxb.RWJSZCDM else []
        for jxb in list_of_jxbs
    ]
    teacher_week_map = {
        teacher.JSH: (teacher, weeks)
        for teacher, weeks in zip(list_of_teachers, list_of_teacher_week)
        if teacher is not None and weeks
    }
    merged: Dict[str, int] = {}
    if not teacher_week_map or not teaching_weeks:
        return merged

    list_of_classes = get_class_instances1(list_of_jxbs[0], classes, courses)
    sp_jas = filter_suitable_classrooms(list_of_jas, list_of_jxbs[0], relax_constraints=relax_constraints)
    time_constraints = build_preferences(list_of_jxbs[0].Prefer_Time, list_of_jxbs[0].unavailable_Time)
    day_patterns = new_generate_day_patterns(list_of_jxbs[0].ZXS, num_days, time_constraints, flag_reschedule=True)

    for jas in sp_jas:
        for pattern in day_patterns:
            days_list, hours_list = pattern
            stats: Dict[str, int] = {}
            new_check_period_availability(
                days_list,
                hours_list,
                jas,
                teacher_week_map,
                list_of_classes,
                teaching_weeks,
                time_constraints,
                reject_stats=stats,
            )
            for k, v in stats.items():
                merged[k] = merged.get(k, 0) + v
    return merged


def _normalize_failed_df(df: pd.DataFrame) -> pd.DataFrame:
    if "教学班ID" not in df.columns:
        for alt in ("JXBID", "jxbid"):
            if alt in df.columns:
                df = df.rename(columns={alt: "教学班ID"})
                break
    if "课程名称" not in df.columns:
        if "KCM" in df.columns:
            df = df.rename(columns={"KCM": "课程名称"})
        elif "name" in df.columns:
            # reschedule_by_adjusting 写入的调课仍失败清单
            df = df.rename(columns={"name": "课程名称"})
    if "教学班ID" not in df.columns:
        raise ValueError("失败清单须包含列「教学班ID」或可识别的 JXBID 列")
    return df


def resolve_default_failed_excel(results_dir: str = "排课结果") -> str:
    """调课后清单优先：存在「调课失败的排课失败课程.xlsx」则用它，否则用初次排课的「排课失败课程_全部.xlsx」。"""
    after_reschedule = os.path.join(results_dir, "调课失败的排课失败课程.xlsx")
    initial = os.path.join(results_dir, "排课失败课程_全部.xlsx")
    if os.path.isfile(after_reschedule):
        return after_reschedule
    return initial


def resolve_default_pickle(results_dir: str = "排课结果") -> Optional[str]:
    """调课会写出 reschedule_timetables.pkl，分析「调课后」占用时优先于 saved_timetables.pkl。"""
    reschedule_pkl = os.path.join(results_dir, "reschedule_timetables.pkl")
    saved_pkl = os.path.join(results_dir, "saved_timetables.pkl")
    if os.path.isfile(reschedule_pkl):
        return reschedule_pkl
    if os.path.isfile(saved_pkl):
        return saved_pkl
    return None


def _teacher_busy_summary(teacher_week_map: Dict, teaching_weeks: List[int]) -> str:
    parts = []
    for jsh, (teacher, weeks) in teacher_week_map.items():
        rel = sorted(set(weeks).intersection(teaching_weeks))
        if not rel:
            continue
        busy = []
        for w in rel[:3]:
            slots = []
            for d in range(teacher.timetable.shape[1]):
                for p in range(teacher.timetable.shape[2]):
                    if str(teacher.timetable[w, d, p]["course"]).strip():
                        slots.append(f"周{w + 1}-d{d + 1}-节{p + 1}")
            if slots:
                busy.append(f"第{w + 1}教学周已占{len(slots)}节")
        if busy:
            parts.append(f"{teacher.XM}({jsh}): " + "; ".join(busy[:2]))
    text = " | ".join(parts[:4])
    if len(parts) > 4:
        text += f" …共{len(parts)}名教师"
    return text or ""


def _count_feasible_slots(
    jxbs: List[Any],
    list_of_jas: List[Any],
    jasdm_dict: Dict[str, int],
    teachers,
    classes,
    courses,
    num_days: int,
    relax_constraints: bool,
) -> Tuple[int, bool]:
    """与 schedule_one_round 一致：对每间候选教室累加方案数，返回最大值及是否调用放宽教室。"""
    list_of_jxbs = jxbs
    teaching_weeks = get_teaching_weeks(list_of_jxbs)
    list_of_JSHs = [jxb.JSH for jxb in list_of_jxbs]
    list_of_teachers = get_teacher_instances(list_of_JSHs, teachers)
    list_of_teacher_week = [
        trans_week_flags(jxb.RWJSZCDM) if hasattr(jxb, "RWJSZCDM") and jxb.RWJSZCDM else []
        for jxb in list_of_jxbs
    ]
    teacher_week_map = {
        teacher.JSH: (teacher, weeks)
        for teacher, weeks in zip(list_of_teachers, list_of_teacher_week)
        if teacher is not None and weeks
    }
    if not teacher_week_map or not teaching_weeks:
        return 0, relax_constraints

    list_of_classes = get_class_instances1(list_of_jxbs[0], classes, courses)
    sp_jas = filter_suitable_classrooms(list_of_jas, list_of_jxbs[0], relax_constraints=relax_constraints)
    time_constraints = build_preferences(list_of_jxbs[0].Prefer_Time, list_of_jxbs[0].unavailable_Time)
    day_patterns = new_generate_day_patterns(list_of_jxbs[0].ZXS, num_days, time_constraints, flag_reschedule=True)

    list_of_jasdms = list(jasdm_dict.keys())
    tem_resource_list = [0 for _ in range(len(list_of_jasdms))]

    for jas in sp_jas:
        num_slots = 0
        for pattern in day_patterns:
            days_list, hours_list = pattern
            arrangements = new_check_period_availability(
                days_list,
                hours_list,
                jas,
                teacher_week_map,
                list_of_classes,
                teaching_weeks,
                time_constraints,
            )
            num_slots += len(arrangements)
        idx = get_indices(jas.JASDM, jasdm_dict)
        if isinstance(idx, int) and idx >= 0:
            tem_resource_list[idx] = num_slots

    return max(tem_resource_list) if tem_resource_list else 0, relax_constraints


def diagnose_jxbid(
    jxbid: str,
    course_name_hint: str,
    courses: List[Any],
    teachers: List[Any],
    classes: List[Any],
    list_of_jas: List[Any],
    jasdm_dict: Dict[str, int],
    num_days: int,
    classrooms_full: Optional[List[Any]] = None,
) -> Dict[str, Any]:
    cr_rooms = classrooms_full or []
    jxbs = get_consecutive_jxbid(jxbid, courses)
    kcm = course_name_hint
    if jxbs:
        kcm = jxbs[0].KCM or course_name_hint

    out: Dict[str, Any] = {
        "教学班ID": jxbid,
        "课程名称": kcm,
        "时间偏好": "",
        "地点偏好": "",
        "原因类别": "",
        "优先级": "",
        "详细说明": "",
        "修改建议": "",
        "教师占用摘要": "",
        "首要枚举冲突": "",
        "枚举拒绝构成": "",
    }

    if not jxbs:
        out["原因类别"] = "数据缺失"
        out["优先级"] = "二级"
        out["详细说明"] = "课程表中找不到该教学班连续记录"
        out["修改建议"] = "核对教学班 ID 与课程表排序（同班连续行）"
        return out

    teaching_weeks = get_teaching_weeks(jxbs)
    if not teaching_weeks:
        out["地点偏好"] = _format_location_preference_summary(jxbs[0], cr_rooms)
        out["时间偏好"] = _format_time_preference_summary(jxbs[0], None)
        out["原因类别"] = "上课周次代码"
        out["优先级"] = "二级"
        out["详细说明"] = "SKZCDM 解析后无有效上课周"
        out["修改建议"] = "修正 SKZCDM 位串；明确单双周编码"
        return out

    list_of_JSHs = [jxb.JSH for jxb in jxbs]
    list_of_teachers = get_teacher_instances(list_of_JSHs, teachers)
    list_of_teacher_week = [
        trans_week_flags(jxb.RWJSZCDM) if hasattr(jxb, "RWJSZCDM") and jxb.RWJSZCDM else []
        for jxb in jxbs
    ]
    teacher_week_map = {
        teacher.JSH: (teacher, weeks)
        for teacher, weeks in zip(list_of_teachers, list_of_teacher_week)
        if teacher is not None and weeks
    }

    missing_jsh = [jsh for jsh, t in zip(list_of_JSHs, list_of_teachers) if t is None]
    empty_rw = [jxb.JSH for jxb, w in zip(jxbs, list_of_teacher_week) if not w]

    if not teacher_week_map:
        detail_parts = []
        if missing_jsh:
            detail_parts.append(f"教师号不存在于教师库: {','.join(set(missing_jsh))}")
        if empty_rw:
            detail_parts.append(f"RWJSZCDM 无有效周: {','.join(set(empty_rw))}")
        out["原因类别"] = "教师匹配或任务周次"
        out["优先级"] = "三级"
        out["地点偏好"] = _format_location_preference_summary(jxbs[0], cr_rooms)
        out["时间偏好"] = _format_time_preference_summary(jxbs[0], None)
        out["详细说明"] = "; ".join(detail_parts) or "教师周次映射为空"
        out["修改建议"] = "核对 JSH；补全 RWJSZCDM 或与 SKZCDM 对齐"
        return out

    out["教师占用摘要"] = _teacher_busy_summary(teacher_week_map, teaching_weeks)

    tc = build_preferences(jxbs[0].Prefer_Time, jxbs[0].unavailable_Time)
    out["地点偏好"] = _format_location_preference_summary(jxbs[0], cr_rooms)
    out["时间偏好"] = _format_time_preference_summary(jxbs[0], tc)
    try:
        day_patterns = new_generate_day_patterns(jxbs[0].ZXS, num_days, tc, flag_reschedule=True)
    except ValueError as e:
        out["原因类别"] = "时间偏好格式"
        out["优先级"] = "一级"
        out["详细说明"] = str(e)
        out["修改建议"] = "按偏好组格式修正（单日单连续块；组内学时和等于 ZXS）"
        return out

    if not day_patterns:
        out["原因类别"] = "时间偏好与周学时"
        out["优先级"] = "一级"
        out["详细说明"] = "无法生成任何上课日模式（偏好组与 ZXS 不匹配或 preferred_options 过滤过严）"
        out["修改建议"] = "放宽偏好或调整 ZXS / 偏好组总学时"
        return out

    strict_relaxed = filter_suitable_classrooms(list_of_jas, jxbs[0], relax_constraints=False)
    relaxed_only = filter_suitable_classrooms(list_of_jas, jxbs[0], relax_constraints=True)
    if not relaxed_only:
        out["原因类别"] = "教室静态不可行"
        out["优先级"] = "二级"
        out["详细说明"] = "校区/容量/类型/指定教室导致无任何候选教室"
        out["修改建议"] = "调整 SKXQ、KRL、教室类型或指定教室字段"
        return out

    max_strict, _ = _count_feasible_slots(
        jxbs, list_of_jas, jasdm_dict, teachers, classes, courses, num_days, relax_constraints=False
    )
    max_relaxed, _ = _count_feasible_slots(
        jxbs, list_of_jas, jasdm_dict, teachers, classes, courses, num_days, relax_constraints=True
    )

    if max_strict == 0 and max_relaxed > 0:
        out["原因类别"] = "指定教室过严"
        out["优先级"] = "二级"
        out["详细说明"] = "放宽教室筛选后存在理论可行方案，严格条件下方案数为 0"
        out["修改建议"] = "放宽 JASDM/LSJASDM/JXLDM 或接受其它教学楼教室"
        return out

    if max_relaxed == 0:
        out["原因类别"] = "资源冲突或偏好过窄"
        out["优先级"] = "一级"
        merged = _aggregate_reject_stats(
            jxbs, list_of_jas, jasdm_dict, teachers, classes, courses, num_days, relax_constraints=True
        )
        primary_zh, breakdown = _format_reject_breakdown(merged)
        out["首要枚举冲突"] = primary_zh
        out["枚举拒绝构成"] = breakdown
        sum_time = sum(merged.get(k, 0) for k in _TIME_RULE_REJECT_KEYS)
        sum_res = sum(merged.get(k, 0) for k in _RESOURCE_REJECT_KEYS)
        res_only = {k: merged[k] for k in _RESOURCE_REJECT_KEYS if merged.get(k)}
        _, res_breakdown_only = _format_reject_breakdown(res_only)

        detail = (
            "在当前全校占用与偏好下，枚举时段方案数为 0（教师/班级/教室时间重叠或偏好窗口无法容纳连排）。"
            "下列「枚举拒绝构成」为在所有候选教室×日模式下试探候选节次时各拒绝原因的累计次数，用于判断矛盾主要来自哪类约束（非唯一因果）。"
        )
        if breakdown:
            detail += f" 首要矛盾（单项次数最多）：{primary_zh}。"
        detail += (
            " 【枚举顺序说明】对每个候选起点依次判定：偏好窗口/禁止时段 → 教室空闲 → 教师空闲 → 班级空闲。"
            "只有通过前几步的候选才会计入后续拒绝类型；故「首要枚举冲突」不等于业务上的唯一根因。"
            f" 时间规则类合计={sum_time}（偏好窗口外/禁止/日模式不匹配/分段边界）；"
            f"资源占用类合计={sum_res}（教室/教师/班级）。"
        )
        if sum_res > sum_time:
            detail += " 累计资源拒绝更高，更说明在现行时间规则下栅格冲突偏紧（教室或师生占用）。"
        elif sum_time > sum_res:
            detail += (
                " 累计时间规则拒绝更高；但若「教师占用摘要」显示多位教师已近乎满课，仍常为偏好窗口内无共同空闲。"
                "不宜仅凭「偏好窗口外」字样断定仅需改 Prefer_Time。"
            )
        else:
            detail += " 两类合计接近，请结合「时间偏好」「地点偏好」列与教师占用摘要判断。"
        if res_breakdown_only:
            detail += f" 资源侧枚举构成：{res_breakdown_only}。"
        if out["教师占用摘要"]:
            detail += " " + out["教师占用摘要"]
        out["详细说明"] = detail
        out["修改建议"] = (
            "对照枚举构成：教室占用高→扩候选教室/错峰教室资源；教师占用高→减任务或协调其它课；班级占用高→调整班级课表；"
            "偏好窗口外/禁止高→放宽 Prefer_Time 或 unavailable_Time；日模式不匹配→核对偏好组与 ZXS。"
            "多教师团队课需结合「教师占用摘要」一起看。"
        )
        return out

    out["原因类别"] = "启发式未选中或其它"
    out["优先级"] = "二级"
    out["详细说明"] = (
        f"静态枚举最大方案数>0（严格 {max_strict} / 放宽 {max_relaxed}），仍失败可能与排课顺序、批量终止等有关"
    )
    out["修改建议"] = "调整排课顺序、分批策略或运行调课；结合排课前检查模块复核偏好"
    return out


def load_context(
    pickle_path: Optional[str],
    course_excel: Optional[str],
    teacher_excel: Optional[str],
    classroom_excel: Optional[str],
    banji_excel: Optional[str],
    num_weeks: int,
    num_days: int,
    num_periods: int,
):
    if pickle_path and os.path.isfile(pickle_path):
        import pickle

        with open(pickle_path, "rb") as f:
            data = pickle.load(f)
        courses = data["courses"]
        teachers = data["teachers"]
        classrooms = data["classrooms"]
        classes = data["classes"]
        return courses, teachers, classrooms, classes

    if not all([course_excel, teacher_excel, classroom_excel, banji_excel]):
        raise ValueError("未提供 pickle 时必须指定课程/教师/教室/班级 Excel 路径")

    from Basic_Data import load_courses, load_teachers, load_classrooms, load_classes

    courses = load_courses(course_excel, weeks=num_weeks, days=num_days, periods=num_periods)
    teachers = load_teachers(teacher_excel, weeks=num_weeks, days=num_days, periods=num_periods)
    classrooms = load_classrooms(classroom_excel, weeks=num_weeks, days=num_days, periods=num_periods)
    classes = load_classes(banji_excel, weeks=num_weeks, days=num_days, periods=num_periods)
    return courses, teachers, classrooms, classes


def run_postfailure_analysis(
    failed_excel: str,
    output_path: str,
    pickle_path: Optional[str] = None,
    course_excel: Optional[str] = None,
    teacher_excel: Optional[str] = None,
    classroom_excel: Optional[str] = None,
    banji_excel: Optional[str] = None,
    num_weeks: int = 17,
    num_days: int = 7,
    num_periods: int = 11,
) -> pd.DataFrame:
    df_fail = pd.read_excel(failed_excel)
    df_fail = _normalize_failed_df(df_fail)

    courses, teachers, classrooms, classes = load_context(
        pickle_path,
        course_excel,
        teacher_excel,
        classroom_excel,
        banji_excel,
        num_weeks,
        num_days,
        num_periods,
    )

    list_of_jas = [c for c in classrooms if str(c.SFYXPK).strip() in ("1", "1.0")]
    unique_jasdms = list(dict.fromkeys([c.JASDM for c in list_of_jas]))
    jasdm_dict = {jasdm: i for i, jasdm in enumerate(unique_jasdms)}

    rows = []
    for _, row in df_fail.iterrows():
        jxbid = str(row["教学班ID"]).strip()
        name = row["课程名称"] if "课程名称" in df_fail.columns and pd.notna(row.get("课程名称")) else ""
        rows.append(
            diagnose_jxbid(
                jxbid, str(name), courses, teachers, classes, list_of_jas, jasdm_dict, num_days, classrooms
            )
        )

    out_df = pd.DataFrame(rows)
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    out_df.to_excel(output_path, index=False)
    return out_df


def main():
    parser = argparse.ArgumentParser(description="排课失败后归因与建议")
    parser.add_argument(
        "--failed",
        default=None,
        help="失败课程 Excel；默认优先 调课失败的排课失败课程.xlsx，否则 排课失败课程_全部.xlsx",
    )
    parser.add_argument(
        "--output",
        default=os.path.join("排课结果", "失败课程归因与建议.xlsx"),
        help="输出报告路径",
    )
    parser.add_argument("--pickle", default=None, help="saved_timetables.pkl（含占用时间表）")
    parser.add_argument("--root", default="智能排课基础数据")
    parser.add_argument("--course", default=None)
    parser.add_argument("--teacher", default=None)
    parser.add_argument("--classroom", default=None)
    parser.add_argument("--banji", default=None)
    parser.add_argument("--weeks", type=int, default=17)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--periods", type=int, default=11)
    parser.add_argument("--rerun-script", default="test.py", help="确认后执行的排课脚本")
    parser.add_argument("--yes-rerun", action="store_true", help="跳过交互直接重跑（慎用）")
    args = parser.parse_args()

    root = args.root
    course = args.course or os.path.join(root, "课程表2025-2026-1.xlsx")
    teacher = args.teacher or os.path.join(root, "更新后的教师名单2025-2026-1.xlsx")
    classroom = args.classroom or os.path.join(root, "更新后的教室表.xlsx")
    banji = args.banji or os.path.join(root, "班级汇总2025-2026-1.xlsx")

    pickle_path = args.pickle
    if not pickle_path:
        pickle_path = resolve_default_pickle("排课结果")

    failed_path = args.failed if args.failed else resolve_default_failed_excel("排课结果")
    if args.failed is None:
        after_xlsx = os.path.join("排课结果", "调课失败的排课失败课程.xlsx")
        if not os.path.isfile(after_xlsx) and "排课失败课程_全部" in failed_path:
            print(
                "提示: 未找到「调课失败的排课失败课程.xlsx」，"
                "现使用初次排课的「排课失败课程_全部.xlsx」（条数通常多于调课后仍失败）。"
            )

    kw = dict(
        pickle_path=pickle_path,
        course_excel=course,
        teacher_excel=teacher,
        classroom_excel=classroom,
        banji_excel=banji,
        num_weeks=args.weeks,
        num_days=args.days,
        num_periods=args.periods,
    )
    if pickle_path:
        kw["course_excel"] = None
        kw["teacher_excel"] = None
        kw["classroom_excel"] = None
        kw["banji_excel"] = None

    df = run_postfailure_analysis(failed_path, args.output, **kw)
    print(f"失败清单: {failed_path}")
    print(f"失败分析完成，共 {len(df)} 条，已写入: {args.output}")

    if args.yes_rerun:
        do = True
    else:
        try:
            ans = input("修改数据后是否重新执行排课脚本？[y/N]: ").strip().lower()
        except EOFError:
            ans = "n"
        do = ans == "y"

    if do:
        script = args.rerun_script
        if not os.path.isfile(script):
            print(f"未找到脚本: {script}", file=sys.stderr)
            sys.exit(1)
        cwd = os.path.dirname(os.path.abspath(script)) or os.getcwd()
        subprocess.run([sys.executable, os.path.abspath(script)], cwd=cwd, check=False)


if __name__ == "__main__":
    main()
