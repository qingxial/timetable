# -*- coding: utf-8 -*-
"""生成中期答辩 PPT：docs/中期进展报告.pptx
配色沿用 HTML 报告（藏青 #23547E + 墨绿 #0E7C69）。
"""
import os
from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.enum.shapes import MSO_SHAPE
from pptx.dml.color import RGBColor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIG = os.path.join(ROOT, "docs", "figures")

# ---- 调色板 ----
NAVY   = RGBColor(0x23, 0x54, 0x7E)
NAVYD  = RGBColor(0x16, 0x2C, 0x44)   # 深底
TEAL   = RGBColor(0x0E, 0x7C, 0x69)
INK    = RGBColor(0x16, 0x20, 0x2E)
MUTED  = RGBColor(0x5D, 0x6B, 0x7E)
SOFT   = RGBColor(0xF1, 0xF5, 0xFA)
LINE   = RGBColor(0xD8, 0xDF, 0xEA)
WHITE  = RGBColor(0xFF, 0xFF, 0xFF)
GOLD   = RGBColor(0x9A, 0x7B, 0x1F)
WARN   = RGBColor(0xB1, 0x63, 0x1C)

CN = "PingFang SC"     # 中文
ENB = "PingFang SC"

EMU_W, EMU_H = Inches(13.333), Inches(7.5)

prs = Presentation()
prs.slide_width = EMU_W
prs.slide_height = EMU_H
BLANK = prs.slide_layouts[6]


def slide(bg=WHITE):
    s = prs.slides.add_slide(BLANK)
    rect = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, EMU_W, EMU_H)
    rect.fill.solid(); rect.fill.fore_color.rgb = bg
    rect.line.fill.background()
    rect.shadow.inherit = False
    return s


def textbox(s, x, y, w, h, lines, align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP):
    """lines: list of dict(text,size,bold,color,font,space_after,bullet)"""
    tb = s.shapes.add_textbox(x, y, w, h)
    tf = tb.text_frame; tf.word_wrap = True
    tf.vertical_anchor = anchor
    tf.margin_left = 0; tf.margin_right = 0; tf.margin_top = 0; tf.margin_bottom = 0
    for i, ln in enumerate(lines):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = ln.get("align", align)
        if ln.get("space_after") is not None:
            p.space_after = Pt(ln["space_after"])
        if ln.get("space_before") is not None:
            p.space_before = Pt(ln["space_before"])
        if ln.get("line"):
            p.line_spacing = ln["line"]
        runs = ln["text"] if isinstance(ln["text"], list) else [ln]
        for rdef in runs:
            r = p.add_run(); r.text = rdef["text"]
            f = r.font
            f.size = Pt(rdef.get("size", ln.get("size", 16)))
            f.bold = rdef.get("bold", ln.get("bold", False))
            f.name = rdef.get("font", ln.get("font", CN))
            f.color.rgb = rdef.get("color", ln.get("color", INK))
    return tb


def shape(s, kind, x, y, w, h, fill=None, line=None, line_w=None):
    sp = s.shapes.add_shape(kind, x, y, w, h)
    if fill is None:
        sp.fill.background()
    else:
        sp.fill.solid(); sp.fill.fore_color.rgb = fill
    if line is None:
        sp.line.fill.background()
    else:
        sp.line.color.rgb = line
        sp.line.width = Pt(line_w or 1)
    sp.shadow.inherit = False
    return sp


def title_bar(s, num, title, sub=None):
    """内容页标题：左侧小竖条 + 标题（不用下划线）"""
    shape(s, MSO_SHAPE.ROUNDED_RECTANGLE, Inches(0.6), Inches(0.5),
          Inches(0.14), Inches(0.5), fill=TEAL)
    lines = [{"text": [
        {"text": f"{num}  ", "size": 26, "bold": True, "color": TEAL},
        {"text": title, "size": 26, "bold": True, "color": NAVY},
    ]}]
    textbox(s, Inches(0.85), Inches(0.46), Inches(11.6), Inches(0.7), lines,
            anchor=MSO_ANCHOR.MIDDLE)
    if sub:
        textbox(s, Inches(0.87), Inches(1.12), Inches(11.6), Inches(0.4),
                [{"text": sub, "size": 13, "color": MUTED}])


def pagenum(s, n):
    textbox(s, Inches(12.4), Inches(7.0), Inches(0.7), Inches(0.3),
            [{"text": f"{n:02d}", "size": 11, "color": MUTED, "align": PP_ALIGN.RIGHT}])


# ============ 1 封面 ============
s = slide(NAVYD)
shape(s, MSO_SHAPE.RECTANGLE, 0, Inches(2.75), EMU_W, Inches(0.03), fill=TEAL)
textbox(s, Inches(1.0), Inches(0.9), Inches(11.3), Inches(0.4),
        [{"text": "硕士研究生学位论文 · 中期进展报告", "size": 15, "bold": True,
          "color": RGBColor(0x8F, 0xB9, 0xD9), "align": PP_ALIGN.CENTER}])
textbox(s, Inches(1.0), Inches(1.55), Inches(11.3), Inches(1.5),
        [{"text": "LLM 增强的启发式贪心智能排课算法", "size": 38, "bold": True,
          "color": WHITE, "align": PP_ALIGN.CENTER, "space_after": 6},
         {"text": "—— 以西安交通大学排课为例", "size": 22,
          "color": RGBColor(0xCA, 0xDC, 0xFC), "align": PP_ALIGN.CENTER}])
textbox(s, Inches(1.0), Inches(3.05), Inches(11.3), Inches(0.4),
        [{"text": "统计学 · 数学与统计学院", "size": 16,
          "color": RGBColor(0xAF,0xC0,0xD0), "align": PP_ALIGN.CENTER}])
meta = [("姓名", "李清霞"), ("学号", "3124107086"), ("导师", "张讲社"), ("汇报日期", "2026 年 6 月")]
mline = []
for i,(k,v) in enumerate(meta):
    mline.append({"text": f"{k} ", "size": 15, "color": RGBColor(0x8F,0xB9,0xD9)})
    mline.append({"text": v + ("        " if i<len(meta)-1 else ""), "size": 15, "bold": True, "color": WHITE})
textbox(s, Inches(1.0), Inches(4.3), Inches(11.3), Inches(0.5),
        [{"text": mline, "align": PP_ALIGN.CENTER}])


# ============ 2 目录 ============
s = slide()
title_bar(s, "目录", "Outline")
items = [
    ("01", "选题的科学依据", "NP-hard · 两大现实痛点"),
    ("02", "国内外研究动态", "算法谱系 · LLM 三角色 · 研究空白"),
    ("03", "研究目标与研究内容", "解析—优化—反馈 五阶段闭环"),
    ("04", "研究进展情况", "理论 · 框架完成度 · 已完成实验"),
    ("05", "下一步工作计划", "LLM 升级 · 消融 · 基准接入"),
    ("06", "参考文献", "13 篇核心文献"),
]
cols = 2
cw, ch = Inches(5.7), Inches(1.45)
x0, y0 = Inches(0.85), Inches(1.7)
gx, gy = Inches(0.55), Inches(0.32)
for i,(n,t,d) in enumerate(items):
    r,c = divmod(i, cols)
    x = x0 + c*(cw+gx); y = y0 + r*(ch+gy)
    card = shape(s, MSO_SHAPE.ROUNDED_RECTANGLE, x, y, cw, ch, fill=SOFT, line=LINE, line_w=1)
    textbox(s, x+Inches(0.25), y, Inches(1.0), ch,
            [{"text": n, "size": 34, "bold": True, "color": TEAL}], anchor=MSO_ANCHOR.MIDDLE)
    textbox(s, x+Inches(1.3), y, cw-Inches(1.5), ch,
            [{"text": t, "size": 19, "bold": True, "color": NAVY, "space_after": 4},
             {"text": d, "size": 12.5, "color": MUTED}], anchor=MSO_ANCHOR.MIDDLE)
pagenum(s, 2)


# ============ 3 选题依据 ============
s = slide()
title_bar(s, "01", "选题的科学依据")
textbox(s, Inches(0.85), Inches(1.45), Inches(11.6), Inches(1.0),
        [{"text": [
            {"text": "课程排课是高校教学管理的核心难题，本质是多重硬、软约束下对教室、教师、时间段等有限资源的组合分配优化，已被证明为 ", "size": 15, "color": INK},
            {"text": "NP-hard 问题", "size": 15, "bold": True, "color": NAVY},
            {"text": "。以西安交通大学 4000 余教学班为例，整数规划模型将产生数万量级变量与约束，难以在可接受时间内求得可行解。", "size": 15, "color": INK},
        ], "line": 1.3}])
# 两痛点卡片
cards = [
    ("①  规模与可行性难以兼顾", "整数规划与遗传、退火、禁忌等元启发式在中小规模可得可行解；面对大规模、高约束密度的真实场景，求解效率与可行性难以两全，且元启发式解缺乏可解释性。"),
    ("②  自然语言需求难以解析", "排课需求大量以自然语言表述（教师偏好、教室要求、时间限制），传统系统依赖人工录入与规则维护，结构化成本高，制约系统可用性与跨场景迁移。"),
]
cw = Inches(5.75); x0=Inches(0.85); y=Inches(3.0); gx=Inches(0.5); ch=Inches(2.2)
for i,(t,d) in enumerate(cards):
    x = x0 + i*(cw+gx)
    shape(s, MSO_SHAPE.ROUNDED_RECTANGLE, x, y, cw, ch, fill=SOFT, line=LINE, line_w=1)
    shape(s, MSO_SHAPE.ROUNDED_RECTANGLE, x, y, Inches(0.12), ch, fill=TEAL)
    textbox(s, x+Inches(0.35), y+Inches(0.25), cw-Inches(0.6), ch-Inches(0.5),
            [{"text": t, "size": 17, "bold": True, "color": NAVY, "space_after": 8},
             {"text": d, "size": 13.5, "color": INK, "line":1.25}])
textbox(s, Inches(0.85), Inches(5.5), Inches(11.6), Inches(1.4),
        [{"text":[
            {"text":"切入点：","size":14,"bold":True,"color":TEAL},
            {"text":"近年大语言模型在语义理解与推理能力上的显著提升，为破解“自然语言需求解析”这一长期瓶颈提供了新的技术路径——这正是本课题的核心动机。","size":14,"color":INK},
        ],"line":1.3}])
pagenum(s, 3)


# ============ 4 国内外动态 ============
s = slide()
title_bar(s, "02", "国内外研究动态")
# 左：算法谱系
lx=Inches(0.85); ly=Inches(1.5); lw=Inches(5.7)
textbox(s, lx, ly, lw, Inches(0.4),
        [{"text":"课程排课的四类求解方法","size":16,"bold":True,"color":NAVY}])
methods=["运筹学方法（图着色 / 整数规划 / 约束满足）",
         "元启发式（遗传 / 禁忌 / 退火 / 蚁群 / 变邻域）",
         "智能方法（混合 / 模糊 / 聚类）",
         "多智能体方法"]
for i,m in enumerate(methods):
    yy=ly+Inches(0.55)+i*Inches(0.64)
    shape(s, MSO_SHAPE.ROUNDED_RECTANGLE, lx, yy, lw, Inches(0.54), fill=SOFT, line=LINE, line_w=1)
    textbox(s, lx+Inches(0.25), yy, lw-Inches(0.4), Inches(0.54),
            [{"text":m,"size":13,"color":INK}], anchor=MSO_ANCHOR.MIDDLE)
# 右：LLM 三角色
rx=Inches(6.9); rw=Inches(5.55)
textbox(s, rx, ly, rw, Inches(0.4),
        [{"text":"LLM 在调度优化中的三类角色","size":16,"bold":True,"color":NAVY}])
roles=[("约束解析与建模","NL4Opt、Holy Grail 2.0、OptiMUS：自然语言→可求解模型"),
       ("启发式设计","FunSearch、EoH：LLM＋进化搜索自动发现启发式"),
       ("可解释决策","TRACE-cs：符号推理＋LLM 生成可验证解释")]
for i,(t,d) in enumerate(roles):
    yy=ly+Inches(0.55)+i*Inches(0.72)
    shape(s, MSO_SHAPE.ROUNDED_RECTANGLE, rx, yy, rw, Inches(0.62), fill=RGBColor(0xF0,0xF9,0xF6), line=RGBColor(0xBF,0xE0,0xD8), line_w=1)
    textbox(s, rx+Inches(0.25), yy, rw-Inches(0.4), Inches(0.62),
            [{"text":[{"text":t+"：","size":13,"bold":True,"color":TEAL},
                      {"text":d,"size":11.5,"color":INK}]}], anchor=MSO_ANCHOR.MIDDLE)
# 研究空白 box
gy=Inches(4.95)
shape(s, MSO_SHAPE.ROUNDED_RECTANGLE, Inches(0.85), gy, Inches(11.6), Inches(1.7),
      fill=RGBColor(0xEA,0xF2,0xFA), line=NAVY, line_w=1.5)
textbox(s, Inches(1.15), gy+Inches(0.25), Inches(11.0), Inches(1.3),
        [{"text":[{"text":"研究空白与切入点：","size":15,"bold":True,"color":NAVY},
                  {"text":"LLM 已在语义解析、启发式设计、结果解释三方面各有进展，但","size":14,"color":INK},
                  {"text":"将三者统一整合为完整排课流程的研究尚属空白","size":14,"bold":True,"color":WARN},
                  {"text":"。本课题将此三者贯通于一条“解析—优化—反馈”主线，构成核心创新。","size":14,"color":INK}],"line":1.3}])
pagenum(s, 4)


# ============ 5 五阶段流程 ============
s = slide()
title_bar(s, "03", "研究目标与研究内容", "总目标：融合 LLM 语义理解与启发式贪心求解，实现“解析—优化—反馈”协同闭环")
steps=[("1","自然语言解析","LLM 抽取教师偏好、教室与时间要求为统一结构化约束",True),
       ("2","语义聚类分批","课程属性向量化聚类，将大问题分解为低复杂度子批",False),
       ("3","贪心排课与求解策略","“最受约束优先”逐批排课，负载均衡放置确定时空",False),
       ("4","变邻域调课","对失败课程逐步松弛软约束、局部重调度，多轮提升完成率",False),
       ("5","LLM 失败归因","结合排课日志生成原因解释与调整建议，闭合反馈回路",True)]
n=len(steps); gap=Inches(0.25)
cw=(EMU_W-Inches(1.7)-gap*(n-1))/n
x0=Inches(0.85); y=Inches(2.35); ch=Inches(2.7)
for i,(no,t,d,llm) in enumerate(steps):
    x=x0+i*(cw+gap)
    fill = RGBColor(0xF0,0xF9,0xF6) if llm else SOFT
    ln = RGBColor(0xBF,0xE0,0xD8) if llm else LINE
    shape(s, MSO_SHAPE.ROUNDED_RECTANGLE, x, y, cw, ch, fill=fill, line=ln, line_w=1.2)
    circ=shape(s, MSO_SHAPE.OVAL, x+cw/2-Inches(0.28), y-Inches(0.28), Inches(0.56), Inches(0.56),
               fill=(TEAL if llm else NAVY))
    textbox(s, x+cw/2-Inches(0.28), y-Inches(0.28), Inches(0.56), Inches(0.56),
            [{"text":no,"size":18,"bold":True,"color":WHITE,"align":PP_ALIGN.CENTER}], anchor=MSO_ANCHOR.MIDDLE)
    textbox(s, x+Inches(0.18), y+Inches(0.5), cw-Inches(0.36), Inches(0.9),
            [{"text":t,"size":14.5,"bold":True,"color":(TEAL if llm else NAVY),"align":PP_ALIGN.CENTER}], anchor=MSO_ANCHOR.MIDDLE)
    textbox(s, x+Inches(0.18), y+Inches(1.35), cw-Inches(0.36), ch-Inches(1.5),
            [{"text":d,"size":11.5,"color":INK,"align":PP_ALIGN.CENTER,"line":1.25}])
textbox(s, Inches(0.85), Inches(5.5), Inches(11.6), Inches(0.5),
        [{"text":"▲ 绿色为 LLM 增强环节，蓝色为优化求解环节","size":12,"color":MUTED,"align":PP_ALIGN.CENTER}])
textbox(s, Inches(0.85), Inches(6.0), Inches(11.6), Inches(1.0),
        [{"text":[{"text":"创新点：","size":13.5,"bold":True,"color":TEAL},
                  {"text":"(1) 以 LLM 统一处理需求解析与失败归因两端，贯通“语义—优化—解释”全链路；(2) 语义聚类实现大规模问题可控分解；(3) 贪心放置引入近似最小约束值策略，缓解资源分布偏倚。","size":13.5,"color":INK}],"line":1.3}])
pagenum(s, 5)


# ============ 6 理论进展①形式化 ============
s = slide()
title_bar(s, "04 · 理论进展", "问题形式化")
textbox(s, Inches(0.85), Inches(1.45), Inches(11.6), Inches(1.2),
        [{"text":[{"text":"排课即求映射 σ：","size":15,"bold":True,"color":NAVY},
                  {"text":"为每个教学班 c 指定教室、教师、班级、连续上课时段 (d,s,h) 与授课周；硬约束要求教室/教师/班级三类资源在任意时刻唯一占用，软约束涵盖时段偏好、校区与教室类型匹配、历史教室倾向。","size":14,"color":INK}],"line":1.3}])
s.shapes.add_picture(os.path.join(FIG,"f_sigma.png"), Inches(1.4), Inches(2.7), width=Inches(10.5))
textbox(s, Inches(0.85), Inches(3.9), Inches(11.6), Inches(0.5),
        [{"text":"优化目标按字典序：先最大化排课完成率，再最大化软约束累计满足度","size":14,"bold":True,"color":NAVY}])
s.shapes.add_picture(os.path.join(FIG,"f_obj.png"), Inches(1.9), Inches(4.5), width=Inches(9.5))
pagenum(s, 6)


# ============ 7 理论进展②核心环节 ============
s = slide()
title_bar(s, "04 · 理论进展", "三个核心环节")
blocks=[("核心一　最受约束优先（MCF）贪心",
         "每轮选取可行方案数最少（资源最紧张）的教学班优先安排，避免后期资源耗尽。",
         "f_mcf.png"),
        ("核心二　求解策略：近似最小约束值（LCV）",
         "将放置视为约束满足中的“值排序”：以时段占用率 ρ、容量浪费率 ω、晚间占比 ε 三项软导向择优，缓解周初堆积、大教室错配与晚间堆课。",
         "f_lcv.png"),
        ("核心三　变邻域调课 + LLM 归因",
         "对失败集合按轮次迭代收缩、逐步松弛软约束并局部重调度；对残余失败由 LLM 结合日志生成解释与建议，闭合“归因—调整—重排”回路。",
         "f_llm.png")]
y=Inches(1.5)
for t,d,img in blocks:
    shape(s, MSO_SHAPE.ROUNDED_RECTANGLE, Inches(0.85), y, Inches(11.6), Inches(1.72), fill=SOFT, line=LINE, line_w=1)
    shape(s, MSO_SHAPE.ROUNDED_RECTANGLE, Inches(0.85), y, Inches(0.12), Inches(1.72), fill=TEAL)
    textbox(s, Inches(1.15), y+Inches(0.16), Inches(11.0), Inches(0.4),
            [{"text":t,"size":15,"bold":True,"color":NAVY}])
    textbox(s, Inches(1.15), y+Inches(0.6), Inches(11.0), Inches(0.55),
            [{"text":d,"size":11.5,"color":INK,"line":1.18}])
    s.shapes.add_picture(os.path.join(FIG,img), Inches(1.3), y+Inches(1.16), height=Inches(0.46))
    y += Inches(1.92)
pagenum(s, 7)


# ============ 8 框架完成度 ============
s = slide()
title_bar(s, "04 · 框架完成度", "系统六大模块实现情况")
rows=[("① 自然语言需求解析","已完成（规则＋LLM）","LLM 版完成，对复杂、隐含需求实现语义级结构化抽取",True),
      ("② 语义嵌入聚类分批","已完成","国产开源嵌入模型向量化，已与贪心引擎完整耦合",True),
      ("③ 贪心排课与求解策略","已完成","硬/软约束统一决策，MCF 分批；放置策略可配置切换",True),
      ("④ 变邻域迭代调课","已完成","逐步松弛软约束、局部重调度，多轮提升完成率",True),
      ("⑤ LLM 失败归因与反馈","规则版完成 · LLM 完善中","规则版输出归因；LLM 版生成自然语言解释与建议",False),
      ("⑥ 端到端流程与可视化","已完成","断点续算、状态持久化、多维利用率可视化与分析报告",True)]
tx=Inches(0.85); ty=Inches(1.6); tw=Inches(11.6)
cols=[Inches(3.4), Inches(2.8), Inches(5.4)]
hh=Inches(0.5)
# 表头
hx=tx
for j,htxt in enumerate(["模块","状态","实现情况"]):
    shape(s, MSO_SHAPE.RECTANGLE, hx, ty, cols[j], hh, fill=NAVY)
    textbox(s, hx+Inches(0.15), ty, cols[j]-Inches(0.2), hh,
            [{"text":htxt,"size":13.5,"bold":True,"color":WHITE}], anchor=MSO_ANCHOR.MIDDLE)
    hx+=cols[j]
ry=ty+hh
rh=Inches(0.78)
for i,(m,st,desc,done) in enumerate(rows):
    bg = WHITE if i%2==0 else SOFT
    hx=tx
    for j,val in enumerate([m,st,desc]):
        shape(s, MSO_SHAPE.RECTANGLE, hx, ry, cols[j], rh, fill=bg, line=LINE, line_w=0.75)
        if j==0:
            textbox(s, hx+Inches(0.15), ry, cols[j]-Inches(0.2), rh,
                    [{"text":val,"size":12.5,"bold":True,"color":INK}], anchor=MSO_ANCHOR.MIDDLE)
        elif j==1:
            col = TEAL if done else WARN
            textbox(s, hx+Inches(0.15), ry, cols[j]-Inches(0.2), rh,
                    [{"text":val,"size":11.5,"bold":True,"color":col}], anchor=MSO_ANCHOR.MIDDLE)
        else:
            textbox(s, hx+Inches(0.15), ry, cols[j]-Inches(0.25), rh,
                    [{"text":val,"size":11,"color":INK,"line":1.15}], anchor=MSO_ANCHOR.MIDDLE)
        hx+=cols[j]
    ry+=rh
pagenum(s, 8)


# ============ 9 实验：整体性能 ============
s = slide()
title_bar(s, "04 · 已完成实验", "真实数据集端到端验证")
stats=[("99.1%","XJTU 4000+ 班\n调课后完成率"),
       ("98.09%","某高校 4670 班\nbalanced 调课后"),
       ("2","真实高校数据集\n端到端验证"),
       ("≤3h","万级规模排课\n调课 12–30 min")]
n=len(stats); gap=Inches(0.4)
cw=(EMU_W-Inches(1.7)-gap*(n-1))/n; x0=Inches(0.85); y=Inches(1.8); ch=Inches(1.9)
for i,(num,lab) in enumerate(stats):
    x=x0+i*(cw+gap)
    shape(s, MSO_SHAPE.ROUNDED_RECTANGLE, x, y, cw, ch, fill=SOFT, line=LINE, line_w=1)
    textbox(s, x, y+Inches(0.3), cw, Inches(0.9),
            [{"text":num,"size":40,"bold":True,"color":TEAL,"align":PP_ALIGN.CENTER}])
    textbox(s, x, y+Inches(1.25), cw, Inches(0.6),
            [{"text":lab.replace("\n","  "),"size":12,"color":MUTED,"align":PP_ALIGN.CENTER,"line":1.15}])
textbox(s, Inches(0.85), Inches(4.1), Inches(11.6), Inches(2.4),
        [{"text":[{"text":"整体性能　","size":15,"bold":True,"color":NAVY},
                  {"text":"系统已在两套真实高校数据上完成端到端实验。西安交大 4000 余教学班经贪心与变邻域调课后完成率达 99.1%，剩余 36 个失败案例均获结构化归因；某高校 4670 教学班 balanced 策略首轮 88.44%、调课后提升至 98.09%，最终 89 个失败教学班归为“主讲教师信息缺失、无满足条件教室、教师时间窗已占满”三类。","size":14,"color":INK}],"line":1.4,"space_after":10},
         {"text":[{"text":"结论　","size":15,"bold":True,"color":TEAL},
                  {"text":"两套数据均验证了系统的稳定性、跨院校适用性与时间高效性，且失败案例可被结构化解释。","size":14,"color":INK}],"line":1.4}])
pagenum(s, 9)


# ============ 10 实验：策略对比 ============
def chart_slide(num, title, img, takeaway_title, takeaway, page, img_h=Inches(4.0)):
    s = slide()
    title_bar(s, num, title)
    pic = s.shapes.add_picture(os.path.join(ROOT,"docs","figures",img), 0, 0, height=img_h)
    # 居中
    pic.left = int((EMU_W - pic.width)/2)
    pic.top = Inches(1.55)
    by = Inches(1.55)+img_h+Inches(0.15)
    shape(s, MSO_SHAPE.ROUNDED_RECTANGLE, Inches(0.85), by, Inches(11.6), Inches(7.5)-by-Inches(0.35),
          fill=RGBColor(0xFD,0xFA,0xF0), line=GOLD, line_w=1.3)
    textbox(s, Inches(1.15), by+Inches(0.12), Inches(11.0), Inches(7.5)-by-Inches(0.55),
            [{"text":[{"text":takeaway_title,"size":13.5,"bold":True,"color":GOLD},
                      {"text":takeaway,"size":13,"color":INK}],"line":1.3}], anchor=MSO_ANCHOR.MIDDLE)
    pagenum(s, page)
    return s

chart_slide("04 · 实验", "放置策略三方对比（图 1）", "strategy_compare_report.png",
            "实验结论：",
            "原始 first 策略周一课时为周五约 4 倍、偏倚显著；random 去偏却把 21.8% 课时推向晚间。本研究 balanced 在取得同等完成率与周分布均衡的同时，将晚间占比降至 3.7%、教室容量利用率由 61% 提升至 85%，可行性、资源适配、作息友好三方面均优。",
            10, img_h=Inches(3.5))

# ============ 11 实验：热力图 ============
chart_slide("04 · 实验", "课时分布热力图（图 2）", "heatmap_report_weekday_period.png",
            "可视化印证：",
            "balanced 策略调课后，周一至周五课时分布均衡（无周初早间堆积），晚间 9–11 节占用极低（≤0.6%），周末基本空闲——符合高校作息规律，验证了放置策略的有效性。",
            11, img_h=Inches(3.9))

# ============ 12 实验：调课提升 ============
s = slide()
title_bar(s, "04 · 实验", "变邻域调课的增益（图 3）")
pic = s.shapes.add_picture(os.path.join(FIG,"reschedule_gain_report.png"), 0, Inches(1.6), height=Inches(4.5))
pic.left = Inches(0.9)
rx = Inches(6.6)
textbox(s, rx, Inches(2.0), Inches(6.0), Inches(4.0),
        [{"text":"本次实验","size":13,"bold":True,"color":TEAL,"space_after":6},
         {"text":"基于当前最优的 balanced 首轮结果运行变邻域调课：","size":14,"color":INK,"line":1.3,"space_after":14},
         {"text":[{"text":"首轮完成率　","size":15,"color":MUTED},{"text":"88.44%","size":18,"bold":True,"color":NAVY}],"space_after":8},
         {"text":[{"text":"调课后完成率　","size":15,"color":MUTED},{"text":"98.09%","size":18,"bold":True,"color":TEAL}],"space_after":8},
         {"text":[{"text":"净提升　","size":15,"color":MUTED},{"text":"+9.65 个百分点","size":16,"bold":True,"color":WARN}],"space_after":14},
         {"text":"4670 个教学班，最终 89 个失败并全部获得结构化归因，可进一步交由 LLM 生成自然语言解释与调整建议。","size":13.5,"color":INK,"line":1.35}])
pagenum(s, 12)


# ============ 13 下一步计划 ============
s = slide()
title_bar(s, "05", "下一步的工作计划")
plan=[("框架完善","进行中","将 LLM 合理性检查与失败归因由规则版升级为语义推理版，提升对多样表述、隐性矛盾与多重耦合冲突的覆盖与解释可读性。",WARN),
      ("自然语言解析方法对比","下一阶段","构建排课需求训练数据（真实＋合成增强），对比提示工程与 LoRA 微调在解析准确率上的差异。",NAVY),
      ("模块级消融实验","核心验证","五组消融变体（w/o LLM-Parse / Consistency / Embedding / MCF / LLM-Diag），在 ITC-2007、ITC-2019 与真实数据上从可行性、质量、效率、可解释性四维量化贡献。",NAVY),
      ("基准接入与可解释性评估","","接入 ITC 标准基准论证普适性；参照 TRACE-cs 人工评估范式，从正确性、可操作性、简洁性三维评价失败解释质量。",NAVY),
      ("论文撰写与定稿","","汇总实验结论，明确创新点与作用边界，完成学位论文撰写、修改与答辩。",TEAL)]
x=Inches(1.1); y0=Inches(1.7)
# 竖线
shape(s, MSO_SHAPE.RECTANGLE, x, y0, Pt(2.2), Inches(5.0), fill=LINE)
for i,(t,when,d,col) in enumerate(plan):
    yy=y0+i*Inches(1.02)
    shape(s, MSO_SHAPE.OVAL, x-Inches(0.1), yy, Inches(0.26), Inches(0.26), fill=col)
    line=[{"text":t,"size":15.5,"bold":True,"color":NAVY}]
    if when:
        line.append({"text":"   "+when,"size":12,"bold":True,"color":col})
    textbox(s, x+Inches(0.45), yy-Inches(0.05), Inches(11.3), Inches(0.45),
            [{"text":line}])
    textbox(s, x+Inches(0.45), yy+Inches(0.36), Inches(11.0), Inches(0.6),
            [{"text":d,"size":12.5,"color":INK,"line":1.2}])
pagenum(s, 13)


# ============ 14 结束 ============
s = slide(NAVYD)
shape(s, MSO_SHAPE.RECTANGLE, 0, Inches(3.95), EMU_W, Inches(0.03), fill=TEAL)
textbox(s, Inches(1.0), Inches(2.7), Inches(11.3), Inches(1.2),
        [{"text":"恳请各位老师批评指正","size":34,"bold":True,"color":WHITE,"align":PP_ALIGN.CENTER}])
textbox(s, Inches(1.0), Inches(4.2), Inches(11.3), Inches(0.6),
        [{"text":"LLM 增强的启发式贪心智能排课算法 · 中期进展报告","size":15,
          "color":RGBColor(0xAF,0xC0,0xD0),"align":PP_ALIGN.CENTER}])
textbox(s, Inches(1.0), Inches(4.85), Inches(11.3), Inches(0.5),
        [{"text":"李清霞　导师：张讲社　2026 年 6 月","size":13,
          "color":RGBColor(0x8F,0xB9,0xD9),"align":PP_ALIGN.CENTER}])

out = os.path.join(ROOT, "docs", "中期进展报告.pptx")
prs.save(out)
print("Saved:", out, "slides:", len(prs.slides._sldIdLst))
