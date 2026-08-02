# -*- coding: utf-8 -*-
"""把中期报告关键公式渲染为透明 PNG，供 PPT 使用。输出到 docs/figures/。"""
import os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs", "figures")
os.makedirs(OUT, exist_ok=True)
plt.rcParams["mathtext.fontset"] = "cm"

FORMULAS = {
    "f_sigma": r"$\sigma:\ c\ \mapsto\ (r_c,\ T_c,\ B_c,\ \{(d,s,h)\},\ \{W_t\}_{t\in T_c})$",
    "f_obj":  r"$\max\ \left(\ |\{c\in C:\ c\ \mathrm{scheduled}\}|,\quad \sum_{c}\sum_{k}\lambda_k\, s_k(c)\ \right)$",
    "f_mcf":  r"$\Omega(c)=\sum_{r\in R(c)}\sum_{\pi\in\Pi(c)}|\Theta(c,r,\pi)|,\qquad c^*=\arg\min_{c}\ \Omega(c)$",
    "f_lcv":  r"$(r^*,a^*)=\arg\min_{(r,a)\in A(c^*)}\left[\,w_\rho\,\rho(a)+w_\omega\,\omega(r,c^*)+w_\varepsilon\,\varepsilon(a)\,\right]$",
    "f_llm":  r"$\mathrm{LLM}(F_K,\ \mathcal{L})\ \rightarrow\ (\mathrm{cause},\ \mathrm{suggestion})$",
}

for name, tex in FORMULAS.items():
    fig = plt.figure(figsize=(10, 1.2))
    fig.text(0.5, 0.5, tex, ha="center", va="center", fontsize=26, color="#16202e")
    path = os.path.join(OUT, f"{name}.png")
    fig.savefig(path, dpi=220, transparent=True, bbox_inches="tight", pad_inches=0.12)
    plt.close(fig)
    print("Saved:", path)
