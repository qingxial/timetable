# -*- coding: utf-8 -*-
"""画两张「特殊要求」相关的柱状图（中文，XJTU 最新数据）：
1) 总教学班 vs 有特殊要求 vs 成功排课的特殊要求
2) 四类约束（偏好时间段/禁止排课时间段/指定教室/指定教学楼）班数（多选可累加）
输出到 排课结果/图表展示/
数据口径与 utils1.pre_selection 一致；"指定教室"只看 JASDM 字段（不含历史教室 LSJASDM）。
"""
import os, sys, json
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager as fm

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

# 中文字体注册（macOS 全覆盖 CJK）
for fp in ["/Library/Fonts/Arial Unicode.ttf",
           "/System/Library/Fonts/STHeiti Medium.ttc",
           "/System/Library/Fonts/PingFang.ttc"]:
    if os.path.exists(fp):
        try:
            fm.fontManager.addfont(fp)
            plt.rcParams["font.sans-serif"] = [fm.FontProperties(fname=fp).get_name()]
            break
        except Exception:
            pass
plt.rcParams["axes.unicode_minus"] = False

OUT = os.path.join("排课结果", "图表展示")
os.makedirs(OUT, exist_ok=True)


def compute_numbers():
    """重新统计（与 pre_selection 同口径），避免依赖临时 json。"""
    import pandas as pd
    from Basic_Data import load_courses, load_classrooms
    from utils1 import pre_selection

    courses = load_courses("智能排课基础数据/课程表2025-2026-1.xlsx",
                           weeks=20, days=7, periods=11)
    classrooms = load_classrooms("智能排课基础数据/更新后的教室表.xlsx",
                                 weeks=20, days=7, periods=11)
    jx, _, _ = pre_selection(courses, classrooms)
    valid = set(jx.keys())

    def ne(v):
        if v is None:
            return False
        try:
            if pd.isna(v):
                return False
        except Exception:
            pass
        return str(v).strip() not in ("", "nan", "None", "0", "0.0")

    prefer = set(); forbid = set(); room = set(); building = set()
    for j in courses:
        if j.JXBID not in valid:
            continue
        if ne(getattr(j, "Prefer_Time", None)):      prefer.add(j.JXBID)
        if ne(getattr(j, "unavailable_Time", None)): forbid.add(j.JXBID)
        if ne(getattr(j, "JASDM", None)):            room.add(j.JXBID)
        if ne(getattr(j, "JXLDM", None)):            building.add(j.JXBID)
    special = prefer | forbid | room | building
    ok = set(pd.read_excel("排课结果/调课后的整体结果.xlsx")["教学班ID"]
             .astype(str).unique())
    return dict(
        total=len(valid),
        special=len(special),
        special_ok=len(special & ok),
        prefer=len(prefer),
        forbid=len(forbid),
        room=len(room),
        building=len(building),
    )


def fig1_overview(n):
    labels = ["全部教学班\n（pre_selection 后）",
              "有特殊要求的\n教学班",
              "成功排课的\n特殊要求教学班"]
    values = [n["total"], n["special"], n["special_ok"]]
    colors = ["#5B9BD5", "#ED7D31", "#70AD47"]  # 蓝 / 橙 / 绿

    fig, ax = plt.subplots(figsize=(7.2, 5.0))
    bars = ax.bar(labels, values, color=colors, width=0.55,
                  edgecolor="white", linewidth=1.5, zorder=3)
    ax.set_ylim(0, max(values) * 1.18)
    ax.set_ylabel("教学班数量", fontsize=12)
    ax.set_title("特殊要求教学班 与 成功排课情况（西安交通大学）",
                 fontsize=13.5, fontweight="bold", pad=14)
    ax.grid(axis="y", alpha=0.3, zorder=0)
    ax.spines["top"].set_visible(False); ax.spines["right"].set_visible(False)
    for b, v in zip(bars, values):
        ax.text(b.get_x() + b.get_width() / 2, v + max(values) * 0.018,
                f"{v:,}", ha="center", va="bottom",
                fontsize=13, fontweight="bold")
    # 成功率小标注
    if n["special"]:
        rate = n["special_ok"] / n["special"] * 100
        ax.annotate(f"成功率 {rate:.1f}%",
                    xy=(2, values[2]), xytext=(2, values[2] + max(values) * 0.08),
                    ha="center", fontsize=10.5, color="#1E6B2B", fontweight="bold")
    plt.tight_layout()
    out = os.path.join(OUT, "special_req_overview.png")
    plt.savefig(out, dpi=180, bbox_inches="tight")
    plt.close()
    print("Saved:", out)


def fig2_breakdown(n):
    labels = ["偏好时间段", "禁止排课时间段", "指定教室", "指定教学楼"]
    values = [n["prefer"], n["forbid"], n["room"], n["building"]]
    fig, ax = plt.subplots(figsize=(7.2, 4.6))
    bars = ax.bar(labels, values, color="#4F81BD", width=0.55,
                  edgecolor="white", linewidth=1.5, zorder=3)
    ax.set_ylim(0, max(values) * 1.20)
    ax.set_ylabel("教学班数量（多选可累加）", fontsize=12)
    ax.set_title("特殊要求类型分布（西安交通大学）",
                 fontsize=13.5, fontweight="bold", pad=14)
    ax.grid(axis="y", alpha=0.3, zorder=0)
    ax.spines["top"].set_visible(False); ax.spines["right"].set_visible(False)
    ax.tick_params(axis="x", labelsize=11)
    for b, v in zip(bars, values):
        ax.text(b.get_x() + b.get_width() / 2, v + max(values) * 0.018,
                f"{v}", ha="center", va="bottom",
                fontsize=12, fontweight="bold")
    plt.tight_layout()
    out = os.path.join(OUT, "special_req_breakdown.png")
    plt.savefig(out, dpi=180, bbox_inches="tight")
    plt.close()
    print("Saved:", out)


if __name__ == "__main__":
    nums = compute_numbers()
    print("统计:", json.dumps(nums, ensure_ascii=False))
    fig1_overview(nums)
    fig2_breakdown(nums)
