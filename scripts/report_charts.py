# -*- coding: utf-8 -*-
"""为中期报告生成两张关键图：
1) heatmap_report_weekday_period.png  周×节次课时占用热力图（全部教室，展示 balanced 时段分布）
2) strategy_compare_report.png         first/random/balanced 三策略关键指标对比柱状图
输出到 排课结果/图表展示/
"""
import os, sys
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from scheduling_visualization import (
    VizConfig, load_timetables, timetable_cell_busy,
    configure_matplotlib, _infer_periods,
)

CHARTS = os.path.join("排课结果", "图表展示")
os.makedirs(CHARTS, exist_ok=True)
configure_matplotlib()

# 注册中文字体（macOS 全覆盖 CJK）
from matplotlib import font_manager as fm
for fp in ["/Library/Fonts/Arial Unicode.ttf",
           "/System/Library/Fonts/STHeiti Medium.ttc"]:
    if os.path.exists(fp):
        try:
            fm.fontManager.addfont(fp)
            plt.rcParams["font.sans-serif"] = [fm.FontProperties(fname=fp).get_name()]
            plt.rcParams["axes.unicode_minus"] = False
            break
        except Exception:
            pass


def heatmap():
    cfg = VizConfig()
    data = load_timetables(cfg.reschedule_pkl)
    if not data:
        print("无法加载课表，跳过热力图")
        return
    _, _, classrooms, _ = data
    nd = cfg.num_days
    sample = getattr(classrooms[0], "timetable", None)
    npd = _infer_periods(sample, cfg.num_periods)
    grid = np.zeros((nd, npd))
    for c in classrooms:
        tt = getattr(c, "timetable", None)
        if tt is None:
            continue
        for w in range(cfg.num_weeks):
            for d in range(nd):
                for p in range(npd):
                    try:
                        if timetable_cell_busy(tt[w, d, p]):
                            grid[d, p] += 1
                    except IndexError:
                        continue
    # 转为“占全部课时的百分比”，便于看分布
    total = grid.sum()
    rate = grid / total * 100.0 if total else grid

    plt.figure(figsize=(11, 5.2))
    im = plt.imshow(rate, aspect="auto", cmap="YlOrRd",
                    vmin=0, vmax=rate.max() * 1.05 + 1e-6)
    cbar = plt.colorbar(im)
    cbar.set_label("占全部课时比例 (%)", fontsize=12)
    day_labels = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    plt.yticks(range(nd), day_labels[:nd], fontsize=12)
    plt.xticks(range(npd), [f"第{i+1}节" for i in range(npd)], fontsize=11, rotation=30)
    for d in range(nd):
        for p in range(npd):
            v = rate[d, p]
            if v >= 0.1:
                plt.text(p, d, f"{v:.1f}", ha="center", va="center",
                         fontsize=8, color="#333")
    plt.title("课时在 周次×节次 上的分布热力图（balanced 策略，调课后）",
              fontsize=14, fontweight="bold")
    plt.tight_layout()
    out = os.path.join(CHARTS, "heatmap_report_weekday_period.png")
    plt.savefig(out, dpi=200, bbox_inches="tight")
    plt.close()
    print("Saved:", out)


def strategy_compare():
    # 数据取自 docs/实验记录_放置策略三方对比.md
    strategies = ["first\n(原始)", "random\n(去偏基线)", "balanced\n(本研究)"]
    colors = ["#9aa7b5", "#b1631c", "#0e7c69"]
    metrics = {
        "首轮完成率 (%)": [87.58, 88.42, 88.44],
        "晚间(9-11节)占比 (%)": [8.7, 21.8, 3.7],
        "平均教室容量利用率 (%)": [61.1, 60.7, 84.7],
    }
    fig, axes = plt.subplots(1, 3, figsize=(13, 4.4))
    for ax, (name, vals) in zip(axes, metrics.items()):
        bars = ax.bar(range(3), vals, color=colors, width=0.62)
        ax.set_title(name, fontsize=12.5, fontweight="bold")
        ax.set_xticks(range(3))
        ax.set_xticklabels(strategies, fontsize=10)
        ax.set_ylim(0, max(vals) * 1.25)
        for b, v in zip(bars, vals):
            ax.text(b.get_x() + b.get_width() / 2, v + max(vals) * 0.02,
                    f"{v}", ha="center", va="bottom", fontsize=11, fontweight="bold")
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.grid(axis="y", alpha=0.25)
    fig.suptitle("放置策略三方对比（某高校真实数据，首轮排课）",
                 fontsize=15, fontweight="bold", y=1.02)
    plt.tight_layout()
    out = os.path.join(CHARTS, "strategy_compare_report.png")
    plt.savefig(out, dpi=200, bbox_inches="tight")
    plt.close()
    print("Saved:", out)


def reschedule_gain():
    """调课前后完成率提升（balanced 策略，某高校近 4670 教学班）。"""
    stages = ["首轮排课\n(balanced)", "变邻域调课后"]
    rates = [88.44, 98.09]
    fig, ax = plt.subplots(figsize=(5.6, 4.6))
    bars = ax.bar(range(2), rates, color=["#7fa8c9", "#0e7c69"], width=0.55)
    ax.set_ylim(0, 108)
    ax.set_ylabel("完成率 (%)", fontsize=12)
    ax.set_xticks(range(2))
    ax.set_xticklabels(stages, fontsize=11.5)
    for b, v in zip(bars, rates):
        ax.text(b.get_x() + b.get_width() / 2, v + 1.2, f"{v}%",
                ha="center", va="bottom", fontsize=13, fontweight="bold")
    # 提升箭头标注
    ax.annotate("", xy=(1, 98.09), xytext=(0, 88.44),
                arrowprops=dict(arrowstyle="->", color="#b1631c", lw=2))
    ax.text(0.5, 94.5, "+9.65 pp", ha="center", color="#b1631c",
            fontsize=12, fontweight="bold")
    ax.set_title("变邻域调课对完成率的提升\n（4670 教学班，最终 89 个失败并获结构化归因）",
                 fontsize=12.5, fontweight="bold")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(axis="y", alpha=0.25)
    plt.tight_layout()
    out = os.path.join(CHARTS, "reschedule_gain_report.png")
    plt.savefig(out, dpi=200, bbox_inches="tight")
    plt.close()
    print("Saved:", out)


if __name__ == "__main__":
    strategy_compare()
    heatmap()
    reschedule_gain()
