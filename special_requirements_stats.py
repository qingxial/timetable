"""
教学班「时间/地点等特殊要求」统计 + 与调课后整体结果对照。

教学班范围：与 utils1.pre_selection 完全一致（课程表行级筛选 + JXBID 去重顺序保留）。
特殊要求：Prefer_Time、unavailable_Time、JASDM、JXLDM、
          中至少一项非空（与排课约束字段一致；不含 RWJSZCDM）。
成功：教学班 ID 出现在 排课结果/调课后的整体结果.xlsx。

课程表默认：智能排课基础数据/课程表2025-2026-1.xlsx
教室表默认：智能排课基础数据/更新后的教室表.xlsx（pre_selection 需要）

示例：
  python special_requirements_stats.py --no-show
"""
from __future__ import annotations

import argparse
import os
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import matplotlib
import matplotlib.pyplot as plt
import pandas as pd

from Basic_Data import Course, load_classrooms, load_courses
from utils1 import _has_non_empty, pre_selection

CHART_SUBDIR_DEFAULT = "图表展示"

# 特殊要求字段（「不全为空」指下列至少一项有有效值）
SPECIAL_KEYS: Tuple[Tuple[str, str], ...] = (
    ("偏好时间", "Prefer_Time"),
    ("禁止时间", "unavailable_Time"),
    ("指定教室", "JASDM"),
    ("教学楼代码", "JXLDM"),
)

SPECIAL_KEYS_CHART_LABELS_EN = (
    "Prefer time",
    "Forbidden time",
    "Room code",
    "Building",
)


def normalize_jxbid(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    return str(value).strip()


def group_courses_by_jxbid(courses: Sequence[Course]) -> Dict[str, List[Course]]:
    g: Dict[str, List[Course]] = defaultdict(list)
    for c in courses:
        jid = normalize_jxbid(c.JXBID)
        if jid:
            g[jid].append(c)
    return dict(g)


def row_has_special_flags(rows: Sequence[Course]) -> Tuple[bool, Dict[str, bool]]:
    flags: Dict[str, bool] = {}
    for label, attr in SPECIAL_KEYS:
        flags[label] = any(_has_non_empty(getattr(r, attr, None)) for r in rows)
    has_any = any(flags.values())
    return has_any, flags


def first_nonempty(rows: Sequence[Course], attr: str) -> str:
    for r in rows:
        v = getattr(r, attr, None)
        if _has_non_empty(v):
            return str(v).strip()
    return ""


def build_detail_rows(
    grouped: Dict[str, List[Course]],
    scheduled_set: set,
    *,
    universe_jxbids: set,
) -> pd.DataFrame:
    rows_out = []
    for jxbid, rows in grouped.items():
        if jxbid not in universe_jxbids:
            continue
        head = rows[0]
        has_sp, flags = row_has_special_flags(rows)
        summary_parts = [k for k, v in flags.items() if v]
        summary = "；".join(summary_parts) if summary_parts else ""

        rows_out.append(
            {
                "教学班ID": jxbid,
                "课程名称": getattr(head, "KCM", "") or "",
                "开课单位": getattr(head, "YXMC", "") or "",
                "课程表行数": len(rows),
                "有特殊要求": "是" if has_sp else "否",
                "特殊要求摘要": summary,
                **{f"要求_{k}": ("是" if flags[k] else "否") for k in flags},
                "偏好时间原文": first_nonempty(rows, "Prefer_Time"),
                "禁止时间原文": first_nonempty(rows, "unavailable_Time"),
                "指定教室原文": first_nonempty(rows, "JASDM"),
                "教学楼原文": first_nonempty(rows, "JXLDM"),
                "是否成功排课": "是" if jxbid in scheduled_set else "否",
            }
        )
    df = pd.DataFrame(rows_out)
    if df.empty:
        return df
    return df.sort_values(["有特殊要求", "是否成功排课", "教学班ID"], ascending=[False, True, True])


def load_scheduled_jxbids(scheduled_xlsx: str) -> set:
    if not os.path.isfile(scheduled_xlsx):
        print(f"警告：未找到结果文件 {scheduled_xlsx}，将全部标记为未成功。")
        return set()
    sdf = pd.read_excel(scheduled_xlsx)
    col = "教学班ID" if "教学班ID" in sdf.columns else None
    if not col:
        print("警告：结果表中没有「教学班ID」列。")
        return set()
    out = set()
    for v in sdf[col].tolist():
        jid = normalize_jxbid(v)
        if jid:
            out.add(jid)
    return out


def configure_matplotlib() -> None:
    matplotlib.rcParams["font.sans-serif"] = ["DejaVu Sans", "Arial", "Helvetica", "SimHei"]
    matplotlib.rcParams["axes.unicode_minus"] = False


def plot_summary_bars(
    *,
    n_total: int,
    n_special: int,
    n_special_scheduled: int,
    outfile: str,
    show: bool,
    title_note: str,
) -> None:
    configure_matplotlib()
    cats = ["Sections\n(pre_selection)", "With special\nconstraints", "Special &\nscheduled"]
    vals = [n_total, n_special, n_special_scheduled]
    plt.figure(figsize=(8, 5))
    colors = ["#6baed6", "#fd8d3c", "#74c476"]
    plt.bar(cats, vals, color=colors)
    m = max(vals) if vals else 1
    for i, v in enumerate(vals):
        plt.text(i, v + m * 0.02, str(v), ha="center", fontsize=12)
    plt.ylabel("Count (sections)")
    plt.title(f"Special requirements vs schedule OK — {title_note}", fontsize=12, fontweight="bold")
    plt.grid(True, axis="y", alpha=0.3)
    plt.tight_layout()
    os.makedirs(os.path.dirname(outfile) or ".", exist_ok=True)
    plt.savefig(outfile, dpi=300, bbox_inches="tight")
    print(f"Saved: {outfile}")
    if show:
        plt.show()
    else:
        plt.close()


def plot_special_mix(df_detail: pd.DataFrame, outfile: str, show: bool) -> None:
    sub = df_detail[df_detail["有特殊要求"] == "是"]
    if sub.empty:
        return
    counts = {}
    for label, _ in SPECIAL_KEYS:
        col = f"要求_{label}"
        if col in sub.columns:
            counts[label] = int((sub[col] == "是").sum())
    counts = {k: v for k, v in counts.items() if v > 0}
    if not counts:
        return
    configure_matplotlib()
    label_zh = list(counts.keys())
    en_map = {SPECIAL_KEYS[i][0]: SPECIAL_KEYS_CHART_LABELS_EN[i] for i in range(len(SPECIAL_KEYS))}
    labels = [en_map.get(k, k) for k in label_zh]
    vals = list(counts.values())
    plt.figure(figsize=(max(8, len(labels) * 0.45), 5))
    plt.bar(range(len(labels)), vals, color="steelblue")
    plt.xticks(range(len(labels)), labels, rotation=30, ha="right")
    plt.ylabel("Sections (multi-select)")
    plt.title("Constraint flags among sections with special requirements", fontsize=12, fontweight="bold")
    plt.grid(True, axis="y", alpha=0.3)
    plt.tight_layout()
    os.makedirs(os.path.dirname(outfile) or ".", exist_ok=True)
    plt.savefig(outfile, dpi=300, bbox_inches="tight")
    print(f"Saved: {outfile}")
    if show:
        plt.show()
    else:
        plt.close()


@dataclass
class StatsConfig:
    root_path: str = "智能排课基础数据"
    results_dir: str = "排课结果"
    charts_subdir: str = CHART_SUBDIR_DEFAULT
    course_excel_name: str = "课程表2025-2026-1.xlsx"
    classroom_excel_name: str = "更新后的教室表.xlsx"
    scheduled_xlsx_name: str = "调课后的整体结果.xlsx"
    num_weeks: int = 17
    num_days: int = 7
    num_periods: int = 11

    @property
    def course_excel(self) -> str:
        return os.path.join(self.root_path, self.course_excel_name)

    @property
    def classroom_excel(self) -> str:
        return os.path.join(self.root_path, self.classroom_excel_name)

    @property
    def scheduled_xlsx(self) -> str:
        return os.path.join(self.results_dir, self.scheduled_xlsx_name)

    @property
    def charts_dir(self) -> str:
        return os.path.join(self.results_dir, self.charts_subdir)


def summarize_sheet(
    df_detail: pd.DataFrame,
    *,
    universe_label: str,
    n_course_table_unique_jxbid: int,
    n_pre_selection: int,
) -> pd.DataFrame:
    total = len(df_detail)
    sp = df_detail[df_detail["有特殊要求"] == "是"]
    sp_ok = sp[sp["是否成功排课"] == "是"]

    rows = [
        ("统计说明", universe_label),
        ("课程表唯一教学班数（全部行聚合）", n_course_table_unique_jxbid),
        ("utils1.pre_selection 筛后教学班数（去重）", n_pre_selection),
        ("本表明细行数（应等于上一行）", total),
        ("有特殊要求的教学班数", len(sp)),
        ("有特殊要求且出现在调课后整体结果中的教学班数", len(sp_ok)),
        ("有特殊要求且未出现在结果表中的教学班数", len(sp[sp["是否成功排课"] == "否"])),
    ]
    if len(sp):
        rows.append(("有特殊要求教学班成功率(%)", round(len(sp_ok) / len(sp) * 100, 2)))
    else:
        rows.append(("有特殊要求教学班成功率(%)", ""))

    out = pd.DataFrame(rows, columns=["指标", "数值"])
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="教学班特殊要求（pre_selection 范围内）与调课后结果对照")
    parser.add_argument("--root", default="智能排课基础数据")
    parser.add_argument("--results-dir", default="排课结果")
    parser.add_argument("--charts-subdir", default=CHART_SUBDIR_DEFAULT)
    parser.add_argument("--course-excel", default="", help="课程表 xlsx 路径")
    parser.add_argument("--classroom-excel", default="", help="教室表 xlsx 路径（pre_selection 用）")
    parser.add_argument("--scheduled-xlsx", default="", help="调课后整体结果 xlsx")
    parser.add_argument("--no-show", action="store_true")
    args = parser.parse_args()

    cfg = StatsConfig(
        root_path=args.root,
        results_dir=args.results_dir,
        charts_subdir=args.charts_subdir,
    )
    course_path = args.course_excel or cfg.course_excel
    classroom_path = args.classroom_excel or cfg.classroom_excel
    scheduled_path = args.scheduled_xlsx or cfg.scheduled_xlsx

    if not os.path.isfile(course_path):
        raise SystemExit(f"找不到课程表：{course_path}")
    if not os.path.isfile(classroom_path):
        raise SystemExit(f"找不到教室表（pre_selection 必需）：{classroom_path}")

    courses = load_courses(
        course_path,
        weeks=cfg.num_weeks,
        days=cfg.num_days,
        periods=cfg.num_periods,
    )
    classrooms = load_classrooms(
        classroom_path,
        weeks=cfg.num_weeks,
        days=cfg.num_days,
        periods=cfg.num_periods,
    )

    grouped = group_courses_by_jxbid(courses)
    n_course_table_unique_jxbid = len(grouped)

    jxbid_index, _, _ = pre_selection(courses, classrooms)
    universe = {normalize_jxbid(j) for j in jxbid_index.keys()}
    n_pre = len(universe)

    scheduled_set = load_scheduled_jxbids(scheduled_path)
    df_detail = build_detail_rows(grouped, scheduled_set, universe_jxbids=universe)

    if df_detail.empty:
        print("pre_selection 未筛出任何教学班，请检查课程表/教室表与 utils1.pre_selection 条件。")
        return

    sp_mask = df_detail["有特殊要求"] == "是"
    n_total = len(df_detail)
    n_special = int(sp_mask.sum())
    n_special_scheduled = int((sp_mask & (df_detail["是否成功排课"] == "是")).sum())

    uni_label = "教学班范围 = utils1.pre_selection(courses, classrooms)；成功 = 调课后的整体结果.xlsx"
    df_summary = summarize_sheet(
        df_detail,
        universe_label=uni_label,
        n_course_table_unique_jxbid=n_course_table_unique_jxbid,
        n_pre_selection=n_pre,
    )
    note_row = pd.DataFrame(
        [
            (
                "特殊要求口径",
                "Prefer_Time、unavailable_Time、JASDM、JXLDM至少一项非空。",
            ),
            ("成功口径", f"教学班 ID ∈「{os.path.basename(scheduled_path)}」"),
        ],
        columns=["指标", "数值"],
    )
    df_summary = pd.concat([note_row, df_summary], ignore_index=True)

    os.makedirs(cfg.charts_dir, exist_ok=True)
    out_xlsx = os.path.join(cfg.charts_dir, "special_requirements_vs_schedule.xlsx")
    with pd.ExcelWriter(out_xlsx, engine="openpyxl") as w:
        df_summary.to_excel(w, sheet_name="汇总", index=False)
        df_detail.to_excel(w, sheet_name="教学班明细", index=False)

    print(f"Saved table: {out_xlsx}")

    print("\n=== 汇总（控制台）===")
    print(df_summary.to_string(index=False))

    show = not args.no_show
    plot_summary_bars(
        n_total=n_total,
        n_special=n_special,
        n_special_scheduled=n_special_scheduled,
        outfile=os.path.join(cfg.charts_dir, "special_requirements_summary.png"),
        show=show,
        title_note="",
    )
    plot_special_mix(
        df_detail,
        outfile=os.path.join(cfg.charts_dir, "special_requirements_breakdown.png"),
        show=show,
    )


if __name__ == "__main__":
    main()
