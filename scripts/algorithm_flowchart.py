# -*- coding: utf-8 -*-
"""排课算法核心实现 · 流程图（中期答辩颗粒度）。
输出：docs/figures/algorithm_flowchart.png
配色与 build_ppt.py 一致：navy 主色、teal LLM/创新、warn 失败回路。
"""
import os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Rectangle
from matplotlib import font_manager as fm

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs", "figures")
os.makedirs(OUT, exist_ok=True)

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

NAVY  = "#23547E"
TEAL  = "#0E7C69"
WARN  = "#B1631C"
INK   = "#16202E"
MUTED = "#5D6B7E"
SOFT  = "#F1F5FA"
GREENF = "#F0F9F6"

fig, ax = plt.subplots(figsize=(13.6, 9.2))
ax.set_xlim(0, 100); ax.set_ylim(0, 100)
ax.set_aspect("equal"); ax.axis("off")


def box(x, y, w, h, title, body=None, *, fill=SOFT, edge=NAVY, text_color=NAVY,
        body_color=INK, title_size=12.5, body_size=10.3, lw=1.6):
    patch = FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.15,rounding_size=0.4",
                           facecolor=fill, edgecolor=edge, linewidth=lw, zorder=2)
    ax.add_patch(patch)
    if body:
        ax.text(x + w/2, y + h*0.74, title, ha="center", va="center",
                fontsize=title_size, fontweight="bold", color=text_color, zorder=3)
        ax.text(x + w/2, y + h*0.30, body, ha="center", va="center",
                fontsize=body_size, color=body_color, zorder=3, linespacing=1.35)
    else:
        ax.text(x + w/2, y + h/2, title, ha="center", va="center",
                fontsize=title_size, fontweight="bold", color=text_color, zorder=3)


def diamond(cx, cy, w, h, text, *, fill="#FFF6E8", edge=WARN, fs=11):
    poly = plt.Polygon([(cx, cy + h/2), (cx + w/2, cy), (cx, cy - h/2), (cx - w/2, cy)],
                       closed=True, facecolor=fill, edgecolor=edge, linewidth=1.6, zorder=2)
    ax.add_patch(poly)
    ax.text(cx, cy, text, ha="center", va="center",
            fontsize=fs, fontweight="bold", color=INK, zorder=3)


def arrow(x1, y1, x2, y2, *, color=NAVY, lw=1.8, style="-|>"):
    a = FancyArrowPatch((x1, y1), (x2, y2), arrowstyle=style,
                        mutation_scale=14, color=color, linewidth=lw, zorder=1)
    ax.add_patch(a)


def label(x, y, text, *, color=INK, fs=10, weight="normal"):
    ax.text(x, y, text, ha="center", va="center", fontsize=fs,
            color=color, fontweight=weight, zorder=4)


# ============== 标题 ==============
ax.text(50, 97, "排课算法核心实现 · 流程图",
        ha="center", va="center", fontsize=17, fontweight="bold", color=NAVY)
ax.text(50, 94, "MCF 贪心选班（外层 argmin） + Balance LCV 选落点（内层 argmin） + 变邻域调课 + LLM 失败归因",
        ha="center", va="center", fontsize=11, color=MUTED)

# ============== 第 1 行：① 数据/预筛 → ② 聚类切批 → ③ 进入主循环 ==============
box(3, 85, 24, 6.5, "① 数据加载 + 预筛选",
    "课程/教室/教师/班级表 + 语义聚类",
    fill=SOFT, edge=NAVY)
box(31, 85, 20, 6.5, "② 按聚类切批", "约 90 个子批，降低复杂度",
    fill=SOFT, edge=NAVY)
box(55, 85, 20, 6.5, "③ 对每个聚类批次", "进入贪心主循环",
    fill=GREENF, edge=TEAL, text_color=TEAL)

arrow(27, 88.25, 31, 88.25)
arrow(51, 88.25, 55, 88.25)
arrow(65, 85, 65, 80.5)   # 进入主循环

# ============== 循环背景框 ==============
loop_bg = Rectangle((4, 36), 92, 44, linewidth=1.4, linestyle="--",
                    edgecolor=NAVY, facecolor="none", zorder=0)
ax.add_patch(loop_bg)
ax.text(5.5, 78.5, "贪心主循环（批内）", fontsize=10.5, color=NAVY,
        fontweight="bold", zorder=5,
        bbox=dict(facecolor="white", edgecolor=NAVY, boxstyle="round,pad=0.25"))

# 菱形：待排集 U 非空？居中 (50, 75)
diamond(65, 75, 22, 7, "待排集 U ≠ ∅ ?")

# 是：向下进入 MCF
arrow(65, 71.5, 65, 69)
label(63, 70, "是", fs=10.5, color=NAVY, weight="bold")

# 否：向右出循环
arrow(76, 75, 84, 75)
label(80, 76.5, "否", fs=10.5, color=MUTED, weight="bold")
box(84, 72, 12, 6, "本批完成", fill=SOFT, edge=MUTED, text_color=MUTED, title_size=11.5)

# ====== ④ MCF 框（外层 argmin） ======
box(20, 56, 60, 13,
    "④ MCF · 外层 argmin（选「最难排」的班）",
    "对每个 c ∈ U：枚举 教室 R(c) × 上课天模式 Π(c)，并通过\n"
    "教室 / 教师 / 班级 / 时间 四类冲突检查\n"
    "⇒ 可行方案总数 Ω(c) = Σ_r Σ_π |Θ(c, r, π)|\n"
    "c* = arg min  Ω(c)   （资源最紧张的班优先排）",
    fill="#EAF2FA", edge=NAVY, title_size=13, body_size=10.5)

# ④ 向下到 Ω=0? 菱形
arrow(50, 56, 50, 54)

# 菱形：Ω(c*)=0?
diamond(50, 50, 22, 7, "Ω(c*) = 0 ?")

# 是 → 右：失败集
arrow(61, 50, 72, 50)
label(66.5, 51.5, "是", fs=10.5, color=WARN, weight="bold")
box(72, 47, 24, 6.5, "加入失败集 F",
    "暂搁置，进入调课处理",
    fill="#FFF3E6", edge=WARN, text_color=WARN, title_size=12, body_size=9.8)

# 否 → 下：LCV
arrow(50, 46.5, 50, 44)
label(53, 45.5, "否", fs=10.5, color=NAVY, weight="bold")

# ====== ⑤ Balance LCV ======
box(20, 36, 60, 8,
    "⑤ Balance · 内层 argmin（选「最优落点」）",
    "枚举 c* 全部硬可行落点 A(c*) = {(r, a)}（线性扫描）\n"
    "(r*, a*) = arg min [ w_ρ·ρ(a) + w_ω·ω(r,c*) + w_ε·ε(a) ]\n"
    "时段占用率 ρ · 容量浪费率 ω · 晚间占比 ε",
    fill=GREENF, edge=TEAL, text_color=TEAL, title_size=13, body_size=10.3)

# ⑤ 向下到落子
arrow(50, 36, 50, 33.7)
box(25, 27.5, 50, 6, "⑥ 落子：更新教室/教师/班级时间表；U ← U \\ {c*}",
    fill=SOFT, edge=NAVY, title_size=11.8)

# 回环：⑥ → 菱形 U 非空？
# 走左侧外环：⑥ 左边 → 下 → 上 → 进入菱形左
arrow(25, 30.5, 14, 30.5, color=NAVY, lw=1.5)
arrow(14, 30.5, 14, 75, color=NAVY, lw=1.5)
arrow(14, 75, 54, 75, color=NAVY, lw=1.5)
label(12, 53, "重新计算 Ω(c)\n继续选下一班", fs=9.8, color=NAVY)

# ============== 调课 + 归因 ==============
# 失败集 → 调课
arrow(84, 47, 84, 21)         # 失败集底部 → 调课右侧
# 本批完成 → 调课
arrow(90, 72, 90, 21)
arrow(90, 21, 78, 21)         # 汇合到调课右边

box(20, 15, 56, 8,
    "⑦ 变邻域调课（K 轮，逐步放松软约束）",
    "对失败集 F：尝试腾挪已排课、放宽偏好/教室类型 → 重排\n"
    "本研究：4670 班 88.4% → 98.1%（+9.7pp）；XJTU 4222 班 → 99.2%",
    fill="#EAF2FA", edge=NAVY, title_size=13, body_size=10.2)

arrow(48, 15, 48, 12)

box(15, 4, 70, 8,
    "⑧ LLM 失败归因（残余失败集 F_K）",
    "规则版：按拒绝类型自动归因（教室静态不可行 / 教师匹配·周次 / 资源冲突）\n"
    "LLM 版：结合排课日志生成自然语言原因解释与调整建议",
    fill=GREENF, edge=TEAL, text_color=TEAL, title_size=13, body_size=10.2)

# ============== 图例 ==============
ly = 0.4
ax.add_patch(Rectangle((6, ly), 2, 1.4, facecolor="#EAF2FA", edgecolor=NAVY, lw=1.2))
ax.text(9, ly + 0.7, "优化求解（蓝）", fontsize=9.8, color=NAVY, va="center")
ax.add_patch(Rectangle((28, ly), 2, 1.4, facecolor=GREENF, edgecolor=TEAL, lw=1.2))
ax.text(31, ly + 0.7, "LLM 增强 / 本研究创新（绿）", fontsize=9.8, color=TEAL, va="center")
ax.add_patch(Rectangle((61, ly), 2, 1.4, facecolor="#FFF3E6", edgecolor=WARN, lw=1.2))
ax.text(64, ly + 0.7, "失败回路（橙）", fontsize=9.8, color=WARN, va="center")

plt.tight_layout()
out = os.path.join(OUT, "algorithm_flowchart.png")
plt.savefig(out, dpi=200, bbox_inches="tight", facecolor="white")
plt.close()
print("Saved:", out)
