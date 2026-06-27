"""
Scheduling visualization: classroom / teacher / class time utilization, space utilization, and summary charts.

Data:
  - Time grids: 排课结果/reschedule_timetables.pkl (via utils_for_reschedule.load_timetables)
  - Space + KPI inputs: 排课结果/排课结果_全部.xlsx, 智能排课基础数据 classroom workbook

Outputs go under: 排课结果/图表展示/ (configurable via --charts-subdir).

Examples:
  python scheduling_visualization.py quick --no-show
  python scheduling_visualization.py all --charts-subdir 图表展示
  ##这个不能merge到服务器上，有问题，不能merger

"""
from __future__ import annotations

import argparse
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
import matplotlib
import matplotlib.pyplot as plt

from utils_for_reschedule import load_timetables

CHART_SUBDIR_DEFAULT = "图表展示"


@dataclass
class VizConfig:
    results_dir: str = "排课结果"
    charts_subdir: str = CHART_SUBDIR_DEFAULT
    root_path: str = "智能排课基础数据"
    num_weeks: int = 20    # 实际数据为 20 教学周（旧默认 17 会漏算后 3 周）
    num_days: int = 7
    num_periods: int = 11   # 上午4+下午4+晚上3=11（旧默认 12 偏大）

    reschedule_pkl: str = field(init=False)
    scheduled_xlsx: str = field(init=False)
    classroom_excel: str = field(init=False)

    def __post_init__(self) -> None:
        self.reschedule_pkl = os.path.join(self.results_dir, "reschedule_timetables.pkl")
        self.scheduled_xlsx = os.path.join(self.results_dir, "排课结果_全部.xlsx")
        # 教室座位表必须用与排课同源的转换后教室表（教室代码 CLS#### 一致）；
        # 旧的「更新后的教室表.xlsx」教室代码体系不同，交集为 0，会导致座位全部回退成 50。
        self.classroom_excel = os.path.join(self.root_path, "提取的基础数据表_converted", "教室表.xlsx")

    @property
    def charts_dir(self) -> str:
        return os.path.join(self.results_dir, self.charts_subdir)


def configure_matplotlib() -> None:
    matplotlib.rcParams["font.sans-serif"] = ["DejaVu Sans", "Arial", "Helvetica", "SimHei"]
    matplotlib.rcParams["axes.unicode_minus"] = False


def _infer_periods(sample_timetable: Any, fallback: int) -> int:
    if sample_timetable is not None and hasattr(sample_timetable, "shape") and len(sample_timetable.shape) >= 3:
        return int(sample_timetable.shape[2])
    return fallback


def timetable_cell_busy(cell: Any) -> bool:
    if cell is None:
        return False
    dt = getattr(cell, "dtype", None)
    names = getattr(dt, "names", None) if dt is not None else None
    if names and "course" in names:
        try:
            return bool(str(cell["course"]).strip())
        except (TypeError, IndexError, ValueError):
            return False
    try:
        return bool(str(cell).strip())
    except Exception:
        return False


def _count_used_slots(
    timetable: Any,
    *,
    week_range: Sequence[int],
    day_count: int,
    period_start: int,
    period_end: int,
) -> int:
    used = 0
    if timetable is None:
        return 0
    for week in week_range:
        for day in range(day_count):
            for period in range(period_start, period_end):
                try:
                    if timetable_cell_busy(timetable[week, day, period]):
                        used += 1
                except IndexError:
                    continue
    return used


def plot_usage_ranking_bar(
    df: pd.DataFrame,
    *,
    value_col: str,
    title: str,
    x_label: str,
    color: str,
    outfile: str,
    cfg: VizConfig,
    show: bool,
    y_hi: Optional[float] = None,
) -> Optional[str]:
    if df is None or df.empty:
        print(f"[skip] No data: {title}")
        return None
    configure_matplotlib()
    df_plot = df.copy()
    avg = float(df_plot[value_col].mean())
    ymax = y_hi if y_hi is not None else min(100.0, max(80.0, df_plot[value_col].max() * 1.15))

    plt.figure(figsize=(16, 8))
    x_positions = range(len(df_plot))
    plt.bar(x_positions, df_plot[value_col], color=color, alpha=0.75, width=0.8, linewidth=0)
    plt.axhline(y=avg, color="red", linestyle="--", linewidth=2, label=f"Mean: {avg:.1f}%")
    plt.xlim(-10, len(df_plot) + 10)
    plt.ylim(0, ymax)

    tick_interval = max(1, len(df_plot) // 10)
    tick_positions = list(range(0, len(df_plot), tick_interval))
    if len(df_plot) - 1 not in tick_positions:
        tick_positions.append(len(df_plot) - 1)
    plt.xticks(tick_positions, [str(i) for i in tick_positions], fontsize=10)
    plt.yticks(fontsize=10)
    plt.title(title, fontsize=16, fontweight="bold", pad=20)
    plt.xlabel(x_label, fontsize=12)
    plt.ylabel("Utilization rate (%)", fontsize=12)
    plt.grid(True, alpha=0.3, linewidth=0.5)
    plt.legend(loc="upper right", fontsize=10)
    plt.tight_layout()

    os.makedirs(cfg.charts_dir, exist_ok=True)
    path = os.path.join(cfg.charts_dir, outfile)
    plt.savefig(path, dpi=300, bbox_inches="tight")
    print(f"Saved: {path}")
    if show:
        plt.show()
    else:
        plt.close()
    return path


def _week_range(cfg: VizConfig, week_filter: Optional[Sequence[int]]) -> List[int]:
    if week_filter is None:
        return list(range(cfg.num_weeks))
    return list(week_filter)


def _period_bounds(cfg: VizConfig, period_range: Optional[Tuple[int, int]], sample_tt: Any) -> Tuple[int, int]:
    nper = _infer_periods(sample_tt, cfg.num_periods)
    if period_range is None:
        return 0, nper
    return period_range[0], min(period_range[1], nper)


def compute_classroom_time_usage(
    cfg: VizConfig,
    *,
    period_range: Optional[Tuple[int, int]] = None,
    week_filter: Optional[Sequence[int]] = None,
) -> Optional[pd.DataFrame]:
    data = load_timetables(cfg.reschedule_pkl)
    if not data:
        print("Error: cannot load timetables (need reschedule_timetables.pkl).")
        return None
    _, _, classrooms, _ = data
    pk = [c for c in classrooms if getattr(c, "SFYXPK", "") == "1"]
    if not pk:
        print("Warning: no classrooms with SFYXPK==1")
        return None

    sample_tt = getattr(pk[0], "timetable", None)
    p0, p1 = _period_bounds(cfg, period_range, sample_tt)
    weeks = _week_range(cfg, week_filter)
    total_slots = len(weeks) * cfg.num_days * (p1 - p0)

    rows = []
    for c in pk:
        tt = getattr(c, "timetable", None)
        used = _count_used_slots(tt, week_range=weeks, day_count=cfg.num_days, period_start=p0, period_end=p1)
        rate = (used / total_slots * 100) if total_slots else 0.0
        rows.append(
            {
                "教室代码": getattr(c, "JASDM", ""),
                "教室名称": getattr(c, "JASMC", ""),
                "教室类型": getattr(c, "JASLX", ""),
                "校区": getattr(c, "MC", ""),
                "容量": getattr(c, "SKZWS", 0),
                "已用时段": used,
                "总时段": total_slots,
                "利用率(%)": round(rate, 2),
            }
        )
    df = pd.DataFrame(rows).sort_values("利用率(%)", ascending=False)
    print(f"[classroom time] rooms={len(df)}, slots={total_slots}")
    return df


def compute_teacher_time_usage(
    cfg: VizConfig,
    *,
    period_range: Optional[Tuple[int, int]] = None,
    week_filter: Optional[Sequence[int]] = None,
) -> Optional[pd.DataFrame]:
    data = load_timetables(cfg.reschedule_pkl)
    if not data:
        return None
    _, teachers, _, _ = data
    if not teachers:
        return None
    sample_tt = getattr(teachers[0], "timetable", None)
    p0, p1 = _period_bounds(cfg, period_range, sample_tt)
    weeks = _week_range(cfg, week_filter)
    total_slots = len(weeks) * cfg.num_days * (p1 - p0)

    rows = []
    for t in teachers:
        tt = getattr(t, "timetable", None)
        used = _count_used_slots(tt, week_range=weeks, day_count=cfg.num_days, period_start=p0, period_end=p1)
        rate = (used / total_slots * 100) if total_slots else 0.0
        name = getattr(t, "XM", None) or getattr(t, "JSM", "") or ""
        rows.append(
            {
                "教师工号": getattr(t, "JSH", ""),
                "教师姓名": name,
                "院系代码": getattr(t, "YXDM", ""),
                "职称": getattr(t, "ZJM", ""),
                "已用时段": used,
                "总时段": total_slots,
                "利用率(%)": round(rate, 2),
            }
        )
    df = pd.DataFrame(rows).sort_values("利用率(%)", ascending=False)
    print(f"[teacher time] count={len(df)}")
    return df


def compute_class_time_usage(
    cfg: VizConfig,
    *,
    period_range: Optional[Tuple[int, int]] = None,
    week_filter: Optional[Sequence[int]] = None,
) -> Optional[pd.DataFrame]:
    data = load_timetables(cfg.reschedule_pkl)
    if not data:
        return None
    _, _, _, classes = data
    if not classes:
        return None
    sample_tt = getattr(classes[0], "timetable", None)
    p0, p1 = _period_bounds(cfg, period_range, sample_tt)
    weeks = _week_range(cfg, week_filter)
    total_slots = len(weeks) * cfg.num_days * (p1 - p0)

    rows = []
    for cobj in classes:
        tt = getattr(cobj, "timetable", None)
        used = _count_used_slots(tt, week_range=weeks, day_count=cfg.num_days, period_start=p0, period_end=p1)
        rate = (used / total_slots * 100) if total_slots else 0.0
        rows.append(
            {
                "班级代码": getattr(cobj, "BJDM", ""),
                "班级名称": getattr(cobj, "BJMC", ""),
                "院系代码": getattr(cobj, "YXDM", ""),
                "年级": getattr(cobj, "NJ", ""),
                "学生人数": getattr(cobj, "RS", 0),
                "已用时段": used,
                "总时段": total_slots,
                "利用率(%)": round(rate, 2),
            }
        )
    df = pd.DataFrame(rows).sort_values("利用率(%)", ascending=False)
    print(f"[class time] count={len(df)}")
    return df


def _save_df(df: Optional[pd.DataFrame], path: str) -> None:
    if df is None or df.empty:
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    df.to_excel(path, index=False)
    print(f"Saved table: {path}")


def run_entity_deep_charts(
    cfg: VizConfig,
    show: bool,
    *,
    compute: Callable[..., Optional[pd.DataFrame]],
    title_base: str,
    x_label: str,
    color: str,
    file_tag: str,
) -> None:
    """All-day + morning/afternoon + week ranges + sample weeks (same logic as legacy scripts)."""
    df_all = compute(cfg)
    if df_all is None:
        return
    plot_usage_ranking_bar(
        df_all,
        value_col="利用率(%)",
        title=f"{title_base} (all day)",
        x_label=x_label,
        color=color,
        outfile=f"{file_tag}_rank_all.png",
        cfg=cfg,
        show=show,
    )
    _save_df(df_all, os.path.join(cfg.charts_dir, f"{file_tag}_detail_all.xlsx"))

    for pr, sfx, title_suffix in [
        ((0, 4), "morning", "(morning, periods 1-4)"),
        ((4, 8), "afternoon", "(afternoon, periods 5-8)"),
    ]:
        df = compute(cfg, period_range=pr)
        if df is None:
            continue
        plot_usage_ranking_bar(
            df,
            value_col="利用率(%)",
            title=f"{title_base} {title_suffix}",
            x_label=x_label,
            color=color,
            outfile=f"{file_tag}_rank_{sfx}.png",
            cfg=cfg,
            show=show,
        )
        _save_df(df, os.path.join(cfg.charts_dir, f"{file_tag}_detail_{sfx}.xlsx"))

    for wrange, name_key, title_weeks in [
        (list(range(0, 8)), "weeks_1_8", "(weeks 1-8)"),
        (list(range(8, 16)), "weeks_9_16", "(weeks 9-16)"),
    ]:
        df = compute(cfg, week_filter=wrange)
        if df is None:
            continue
        plot_usage_ranking_bar(
            df,
            value_col="利用率(%)",
            title=f"{title_base} {title_weeks}",
            x_label=x_label,
            color=color,
            outfile=f"{file_tag}_rank_{name_key}.png",
            cfg=cfg,
            show=show,
        )
        _save_df(df, os.path.join(cfg.charts_dir, f"{file_tag}_detail_{name_key}.xlsx"))

    for wk in [0, 4, 8, 12]:
        df = compute(cfg, week_filter=[wk])
        if df is None:
            continue
        n = wk + 1
        plot_usage_ranking_bar(
            df,
            value_col="利用率(%)",
            title=f"{title_base} (week {n})",
            x_label=x_label,
            color=color,
            outfile=f"{file_tag}_rank_week_{n}.png",
            cfg=cfg,
            show=show,
        )
        _save_df(df, os.path.join(cfg.charts_dir, f"{file_tag}_detail_week_{n}.xlsx"))


SPACE_LEVELS = ["Low (0-50%)", "Fair (50-70%)", "Good (70-85%)", "High (85-100%)"]
SPACE_LEVEL_COLORS = {
    "Low (0-50%)": "red",
    "Fair (50-70%)": "orange",
    "Good (70-85%)": "#E6C200",
    "High (85-100%)": "green",
}


def compute_classroom_space_usage(cfg: VizConfig, verbose: bool = False) -> Optional[pd.DataFrame]:
    if not os.path.isfile(cfg.scheduled_xlsx):
        print(f"Skip space utilization: missing {cfg.scheduled_xlsx}")
        return None
    df = pd.read_excel(cfg.scheduled_xlsx)
    required = ["教学班ID", "课程名称", "课容量", "教室", "教师", "教室代码"]
    miss = [c for c in required if c not in df.columns]
    if miss:
        print(f"Scheduled result missing columns: {miss}")
        return None

    df = df.rename(columns={"课容量": "课程容量", "教室": "教室名称", "教师": "教师姓名"})

    classroom_seat_map: Dict[str, int] = {}
    if os.path.isfile(cfg.classroom_excel):
        cdf = pd.read_excel(cfg.classroom_excel)
        code_col = "教室代码" if "教室代码" in cdf.columns else cdf.columns[0]
        seat_col = None
        for cand in ("上课座位数", "座位数", "SKZWS", "容量", "教室容量"):
            if cand in cdf.columns:
                seat_col = cand
                break
        if seat_col:
            cdf = cdf[(cdf[code_col].notna()) & (cdf[code_col].astype(str).str.strip() != "") & (cdf[code_col].astype(str) != "JASDM")]
            for _, row in cdf.iterrows():
                code = str(row[code_col]).strip()
                try:
                    sc = int(float(row[seat_col]))
                    if code and sc > 0:
                        classroom_seat_map[code] = sc
                except (TypeError, ValueError):
                    continue
            if verbose:
                print(f"Seat map size: {len(classroom_seat_map)}")

    df["教室代码_clean"] = df["教室代码"].astype(str).str.strip()
    df["上课座位数"] = df["教室代码_clean"].map(classroom_seat_map)
    if df["上课座位数"].isna().any():
        if verbose:
            print("Some rooms unmatched; filling seats with 50")
        df["上课座位数"] = df["上课座位数"].fillna(50)

    df_clean = df.dropna(subset=["课程容量"])
    df_clean = df_clean[(df_clean["课程容量"] > 0) & (df_clean["上课座位数"] > 0)]
    grp = df_clean.groupby("教学班ID", as_index=False).first()
    grp["空间利用率(%)"] = (grp["课程容量"] / grp["上课座位数"]) * 100
    grp = grp[grp["空间利用率(%)"] <= 100].copy()
    grp["利用率等级"] = pd.cut(
        grp["空间利用率(%)"],
        bins=[0, 50, 70, 85, 100],
        labels=SPACE_LEVELS,
    )
    grp = grp.sort_values("空间利用率(%)", ascending=False)
    print(f"[space] sections={len(grp)}, mean={grp['空间利用率(%)'].mean():.2f}%")
    return grp


def plot_space_distribution(df: pd.DataFrame, cfg: VizConfig, show: bool) -> None:
    if df is None or df.empty:
        return
    df = df.reset_index(drop=True)
    configure_matplotlib()
    avg = float(df["空间利用率(%)"].mean())
    plt.figure(figsize=(16, 8))
    for level, color in SPACE_LEVEL_COLORS.items():
        mask = df["利用率等级"] == level
        if not mask.any():
            continue
        positions = np.flatnonzero(mask.to_numpy()).tolist()
        vals = df.loc[mask, "空间利用率(%)"].tolist()
        plt.bar(positions, vals, color=color, alpha=0.9, width=1.0, label=f"{level} (n={int(mask.sum())})")
    plt.axhline(y=avg, color="red", linestyle="--", linewidth=2, label=f"Mean: {avg:.1f}%")
    plt.xlim(-10, len(df) + 10)
    plt.ylim(0, 100)
    plt.title("Room space utilization (enrollment / seat capacity)", fontsize=16, fontweight="bold")
    plt.xlabel("Course section (sorted by utilization)")
    plt.ylabel("Space utilization (%)")
    plt.legend(loc="upper right", fontsize=9)
    plt.grid(True, alpha=0.3)
    plt.tight_layout()
    os.makedirs(cfg.charts_dir, exist_ok=True)
    path = os.path.join(cfg.charts_dir, "room_space_util_distribution.png")
    plt.savefig(path, dpi=300, bbox_inches="tight")
    print(f"Saved: {path}")
    if show:
        plt.show()
    else:
        plt.close()


def plot_space_pie(df: pd.DataFrame, cfg: VizConfig, show: bool) -> None:
    if df is None or df.empty:
        return
    configure_matplotlib()
    vc = df["利用率等级"].value_counts()
    colors = [SPACE_LEVEL_COLORS.get(str(i), "gray") for i in vc.index]
    plt.figure(figsize=(10, 8))
    plt.pie(vc.values, labels=vc.index, colors=colors, autopct="%1.1f%%", startangle=90)
    plt.title("Room space utilization — level mix", fontsize=16, fontweight="bold")
    plt.tight_layout()
    path = os.path.join(cfg.charts_dir, "room_space_util_levels_pie.png")
    plt.savefig(path, dpi=300, bbox_inches="tight")
    print(f"Saved: {path}")
    if show:
        plt.show()
    else:
        plt.close()


def plot_weekday_period_heatmap(cfg: VizConfig, show: bool) -> None:
    data = load_timetables(cfg.reschedule_pkl)
    if not data:
        return
    _, _, classrooms, _ = data
    pk = [c for c in classrooms if getattr(c, "SFYXPK", "") == "1"]
    if not pk:
        return
    nd = cfg.num_days
    sample = getattr(pk[0], "timetable", None)
    np_ = _infer_periods(sample, cfg.num_periods)
    grid = np.zeros((nd, np_))
    denom = len(pk) * cfg.num_weeks
    for c in pk:
        tt = getattr(c, "timetable", None)
        if tt is None:
            continue
        for w in range(cfg.num_weeks):
            for d in range(nd):
                for p in range(np_):
                    try:
                        if timetable_cell_busy(tt[w, d, p]):
                            grid[d, p] += 1
                    except IndexError:
                        continue
    rate = grid / denom * 100.0

    configure_matplotlib()
    plt.figure(figsize=(12, 6))
    im = plt.imshow(rate, aspect="auto", cmap="YlOrRd", vmin=0, vmax=min(100, rate.max() * 1.05 + 1e-6))
    plt.colorbar(im, label="Occupancy rate (%)")
    day_labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    plt.yticks(range(nd), day_labels[:nd])
    plt.xticks(range(np_), [f"P{i + 1}" for i in range(np_)])
    plt.xlabel("Period")
    plt.ylabel("Weekday")
    plt.title("Room occupancy heatmap (allowed rooms, weekly average)", fontsize=14, fontweight="bold")
    plt.tight_layout()
    os.makedirs(cfg.charts_dir, exist_ok=True)
    path = os.path.join(cfg.charts_dir, "heatmap_weekday_period.png")
    plt.savefig(path, dpi=300, bbox_inches="tight")
    print(f"Saved: {path}")
    if show:
        plt.show()
    else:
        plt.close()


def plot_timesegment_comparison(cfg: VizConfig, show: bool) -> None:
    segments = [((0, 4), "Morning (P1-4)"), ((4, 8), "Afternoon (P5-8)"), ((8, 12), "Evening (P9+)")]
    rates = []
    labels = []
    for (a, b), name in segments:
        df = compute_classroom_time_usage(cfg, period_range=(a, b))
        if df is None:
            continue
        rates.append(float(df["利用率(%)"].mean()))
        labels.append(name)
    if not rates:
        return
    configure_matplotlib()
    plt.figure(figsize=(8, 5))
    plt.bar(labels, rates, color=["#2ca02c", "#ff7f0e", "#9467bd"])
    plt.ylabel("Mean classroom time utilization (%)")
    plt.title("Time-of-day comparison (classrooms)", fontsize=14, fontweight="bold")
    for i, v in enumerate(rates):
        plt.text(i, v + 1, f"{v:.1f}%", ha="center", fontsize=11)
    plt.ylim(0, max(rates) * 1.25 + 5)
    plt.grid(True, axis="y", alpha=0.3)
    plt.tight_layout()
    os.makedirs(cfg.charts_dir, exist_ok=True)
    path = os.path.join(cfg.charts_dir, "timesegment_classroom_util.png")
    plt.savefig(path, dpi=300, bbox_inches="tight")
    print(f"Saved: {path}")
    if show:
        plt.show()
    else:
        plt.close()


def plot_campus_classroom_usage(cfg: VizConfig, show: bool) -> None:
    df = compute_classroom_time_usage(cfg)
    if df is None or df.empty or "校区" not in df.columns:
        return
    df_c = df.assign(校区=df["校区"].fillna("(unknown)"))
    g = df_c.groupby("校区", as_index=False)["利用率(%)"].mean().sort_values("利用率(%)", ascending=False)
    configure_matplotlib()
    plt.figure(figsize=(max(8, len(g) * 0.5), 5))
    plt.bar(range(len(g)), g["利用率(%)"], color="steelblue")
    plt.xticks(range(len(g)), g["校区"], rotation=25, ha="right")
    plt.ylabel("Mean classroom time utilization (%)")
    plt.title("By campus: mean classroom time utilization", fontsize=14, fontweight="bold")
    plt.grid(True, axis="y", alpha=0.3)
    plt.tight_layout()
    os.makedirs(cfg.charts_dir, exist_ok=True)
    path = os.path.join(cfg.charts_dir, "campus_classroom_util.png")
    plt.savefig(path, dpi=300, bbox_inches="tight")
    print(f"Saved: {path}")
    if show:
        plt.show()
    else:
        plt.close()


def plot_scheduling_overview(cfg: VizConfig, show: bool) -> None:
    scheduled = 0
    failed = 0
    rescheduled_success = 0
    if os.path.isfile(cfg.scheduled_xlsx):
        try:
            sdf = pd.read_excel(cfg.scheduled_xlsx)
            scheduled = sdf["教学班ID"].nunique() if "教学班ID" in sdf.columns else len(sdf)
        except Exception:
            pass

    reschedule_success_path = os.path.join(cfg.results_dir, "调课成功的排课失败课程.xlsx")
    if os.path.isfile(reschedule_success_path):
        try:
            rsdf = pd.read_excel(reschedule_success_path)
            id_col = "教学班ID" if "教学班ID" in rsdf.columns else "jxbid" if "jxbid" in rsdf.columns else None
            if id_col:
                rescheduled_success = rsdf[id_col].nunique()
        except Exception:
            pass

    fail_a = os.path.join(cfg.results_dir, "调课失败的排课失败课程.xlsx")
    fail_b = os.path.join(cfg.results_dir, "排课失败课程_全部.xlsx")
    fail_path = fail_a if os.path.isfile(fail_a) else fail_b if os.path.isfile(fail_b) else None
    if fail_path:
        try:
            fdf = pd.read_excel(fail_path)
            id_col = "教学班ID" if "教学班ID" in fdf.columns else "jxbid" if "jxbid" in fdf.columns else None
            if id_col:
                failed = fdf[id_col].nunique()
        except Exception:
            pass

    # 排课结果_全部.xlsx 是「首轮成功」名单（不含调课新增），故它本身就是首轮成功数，
    # 不能再减 rescheduled_success（那 451 个根本不在这 4130 里）。
    # 最终成功 = 首轮成功(4130) + 调课新增(451) = 4581。
    original_scheduled = scheduled            # 首轮成功 = 4130

    configure_matplotlib()
    plt.figure(figsize=(8, 5))
    cats = ["Scheduled\n(unique sections)", "Still failed\n(unique sections)"]
    bottom_vals = [original_scheduled, 0]
    top_vals = [rescheduled_success, 0]
    bar_positions = list(range(len(cats)))

    plt.bar(bar_positions, bottom_vals, color="#1f77b4", label="Original scheduled")
    if rescheduled_success > 0:
        plt.bar(bar_positions, top_vals, bottom=bottom_vals, color="#ff7f0e", label="Reschedule success")
    plt.bar(bar_positions[1], failed, color="#d62728", label="Still failed")

    max_val = max(scheduled, failed, 1)
    if original_scheduled > 0:
        plt.text(0, original_scheduled / 2, str(original_scheduled), ha="center", va="center", fontsize=11, color="white")
    if rescheduled_success > 0:
        plt.text(0, original_scheduled + rescheduled_success / 2, str(rescheduled_success), ha="center", va="center", fontsize=11, color="white")
    plt.text(bar_positions[1], failed + max_val * 0.02, str(failed), ha="center", fontsize=12)

    plt.xticks(bar_positions, cats)
    plt.title("Scheduling outcome overview", fontsize=14, fontweight="bold")
    plt.ylabel("Count (sections)")
    plt.grid(True, axis="y", alpha=0.3)
    plt.legend(loc="upper left", fontsize=10)
    plt.annotate(
        "Failed list prefers post-reschedule file when present",
        xy=(0.5, -0.12),
        xycoords="axes fraction",
        ha="center",
        fontsize=8,
        color="gray",
    )
    plt.tight_layout()
    os.makedirs(cfg.charts_dir, exist_ok=True)
    path = os.path.join(cfg.charts_dir, "scheduling_overview_kpi.png")
    plt.savefig(path, dpi=300, bbox_inches="tight")
    print(f"Saved: {path}")
    if show:
        plt.show()
    else:
        plt.close()


def plot_scheduling_overview_manual(cfg: VizConfig, show: bool) -> None:
    original_scheduled = 3992
    rescheduled_success = 194
    failed = 36

    configure_matplotlib()
    plt.figure(figsize=(8, 5))
    cats = ["Scheduled\n(unique sections)", "Still failed\n(unique sections)"]
    bottom_vals = [original_scheduled, 0]
    top_vals = [rescheduled_success, 0]
    bar_positions = list(range(len(cats)))

    plt.bar(bar_positions, bottom_vals, color="#1f77b4", label="Original scheduled")
    plt.bar(bar_positions, top_vals, bottom=bottom_vals, color="#ff7f0e", label="Reschedule success")
    plt.bar(bar_positions[1], failed, color="#d62728", label="Still failed")

    max_val = max(original_scheduled + rescheduled_success, failed, 1)
    plt.text(0, original_scheduled / 2, str(original_scheduled), ha="center", va="center", fontsize=11, color="white")
    plt.text(0, original_scheduled + rescheduled_success / 2, str(rescheduled_success), ha="center", va="center", fontsize=11, color="white")
    plt.text(bar_positions[1], failed + max_val * 0.02, str(failed), ha="center", fontsize=12)

    plt.xticks(bar_positions, cats)
    plt.title("Scheduling outcome overview", fontsize=14, fontweight="bold")
    plt.ylabel("Count (sections)")
    plt.grid(True, axis="y", alpha=0.3)
    plt.legend(loc="upper left", fontsize=10)
    # plt.annotate(
    #     "Manual values: 3992 original + 194 reschedule success, 36 still failed",
    #     xy=(0.5, -0.12),
    #     xycoords="axes fraction",
    #     ha="center",
    #     fontsize=8,
    #     color="gray",
    # )
    plt.tight_layout()
    os.makedirs(cfg.charts_dir, exist_ok=True)
    path = os.path.join(cfg.charts_dir, "scheduling_overview_manual.png")
    plt.savefig(path, dpi=300, bbox_inches="tight")
    print(f"Saved: {path}")
    if show:
        plt.show()
    else:
        plt.close()


def run_quick(cfg: VizConfig, show: bool) -> None:
    plot_weekday_period_heatmap(cfg, show)
    plot_timesegment_comparison(cfg, show)
    plot_campus_classroom_usage(cfg, show)
    plot_scheduling_overview(cfg, show)

    jobs = [
        ("Classroom index", compute_classroom_time_usage, "Classroom time utilization", "green", "classroom_time"),
        ("Teacher index", compute_teacher_time_usage, "Teacher time utilization", "orange", "teacher_time"),
        ("Class index", compute_class_time_usage, "Class time utilization", "blue", "class_time"),
    ]
    for x_label, compute_fn, title, color, file_tag in jobs:
        df = compute_fn(cfg)
        if df is None:
            continue
        plot_usage_ranking_bar(
            df,
            value_col="利用率(%)",
            title=f"{title} (all day)",
            x_label=x_label,
            color=color,
            outfile=f"{file_tag}_rank_all.png",
            cfg=cfg,
            show=show,
        )
        _save_df(df, os.path.join(cfg.charts_dir, f"{file_tag}_detail_all.xlsx"))

    space_df = compute_classroom_space_usage(cfg, verbose=False)
    if space_df is not None:
        plot_space_distribution(space_df, cfg, show)
        plot_space_pie(space_df, cfg, show)
        _save_df(space_df, os.path.join(cfg.charts_dir, "room_space_util_detail.xlsx"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Scheduling charts (English labels; output under 排课结果/图表展示)")
    parser.add_argument(
        "mode",
        nargs="?",
        default="quick",
        choices=["quick", "all", "classroom", "teacher", "class", "space", "extras", "manual"],
        help="quick | all | single-entity | space | extras | manual",
    )
    parser.add_argument("--results-dir", default="排课结果")
    parser.add_argument("--charts-subdir", default=CHART_SUBDIR_DEFAULT, help='Subfolder under results (default: "图表展示")')
    parser.add_argument("--root", default="智能排课基础数据")
    parser.add_argument("--no-show", action="store_true")
    args = parser.parse_args()

    cfg = VizConfig(results_dir=args.results_dir, root_path=args.root, charts_subdir=args.charts_subdir)
    show = not args.no_show

    if not os.path.isfile(cfg.reschedule_pkl) and args.mode not in ("space",):
        print(f"Warning: missing {cfg.reschedule_pkl}; time-based charts may be skipped.")

    if args.mode == "quick":
        run_quick(cfg, show)
    elif args.mode == "extras":
        plot_weekday_period_heatmap(cfg, show)
        plot_timesegment_comparison(cfg, show)
        plot_campus_classroom_usage(cfg, show)
        plot_scheduling_overview(cfg, show)
    elif args.mode == "classroom":
        run_entity_deep_charts(
            cfg,
            show,
            compute=compute_classroom_time_usage,
            title_base="Classroom time utilization",
            x_label="Classroom index (sorted)",
            color="green",
            file_tag="classroom_time",
        )
    elif args.mode == "teacher":
        run_entity_deep_charts(
            cfg,
            show,
            compute=compute_teacher_time_usage,
            title_base="Teacher time utilization",
            x_label="Teacher index (sorted)",
            color="orange",
            file_tag="teacher_time",
        )
    elif args.mode == "class":
        run_entity_deep_charts(
            cfg,
            show,
            compute=compute_class_time_usage,
            title_base="Class time utilization",
            x_label="Class index (sorted)",
            color="blue",
            file_tag="class_time",
        )
    elif args.mode == "space":
        df = compute_classroom_space_usage(cfg, verbose=True)
        if df is not None:
            plot_space_distribution(df, cfg, show)
            plot_space_pie(df, cfg, show)
            _save_df(df, os.path.join(cfg.charts_dir, "room_space_util_detail.xlsx"))
    elif args.mode == "manual":
        plot_scheduling_overview_manual(cfg, show)
    elif args.mode == "all":
        plot_weekday_period_heatmap(cfg, show)
        plot_timesegment_comparison(cfg, show)
        plot_campus_classroom_usage(cfg, show)
        plot_scheduling_overview(cfg, show)
        run_entity_deep_charts(
            cfg,
            show,
            compute=compute_classroom_time_usage,
            title_base="Classroom time utilization",
            x_label="Classroom index (sorted)",
            color="green",
            file_tag="classroom_time",
        )
        run_entity_deep_charts(
            cfg,
            show,
            compute=compute_teacher_time_usage,
            title_base="Teacher time utilization",
            x_label="Teacher index (sorted)",
            color="orange",
            file_tag="teacher_time",
        )
        run_entity_deep_charts(
            cfg,
            show,
            compute=compute_class_time_usage,
            title_base="Class time utilization",
            x_label="Class index (sorted)",
            color="blue",
            file_tag="class_time",
        )
        space_df = compute_classroom_space_usage(cfg, verbose=True)
        if space_df is not None:
            plot_space_distribution(space_df, cfg, show)
            plot_space_pie(space_df, cfg, show)
            _save_df(space_df, os.path.join(cfg.charts_dir, "room_space_util_detail.xlsx"))

    print("\nDone.")


if __name__ == "__main__":
    main()
