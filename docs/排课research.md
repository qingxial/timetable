# 排课 research：顶会与权威期刊调研 & 可借鉴 idea

> 调研日期：2026-06-26
> 目的：从 AAAI / IJCAI / NeurIPS / PATAT / Journal of Scheduling / Annals of OR
> 等渠道梳理课程排课（University Course Timetabling, UCTT）近五年的主流做法，
> 提炼对本毕设（**LLM 增强的启发式贪心智能排课，西交大数据**）有借鉴价值的 idea，
> 并给出"对本项目如何落地最划算"的建议。
> 项目当前状态摘要：贪心 + reschedule_by_adjusting 调课，参数 20 周 / 7 天 / 11 节，
> 已实现 first / random / balanced 三种放置策略对比，论文方向是
> "LLM 增强 + 消融实验 + ITC 基准实验"，所以本调研重点关心
> **(1) 与启发式贪心兼容、(2) 与 LLM 模块互补、(3) ITC 基准可复现** 的方法。

---

## 1. 调研范围与背景

UCTT 是 NP-hard 经典问题，国际公认基准是 **ITC-2007**（curriculum-based）和
**ITC-2019**（含 student sectioning，源自 UniTime 实际部署数据）。
顶会层面，UCTT 本身近年很少直接出现在 AAAI/IJCAI/NeurIPS 主会（这些会偏好通用
组合优化 / ML4CO 方向），但 **PATAT**（Practice and Theory of Automated
Timetabling，2024 年 8 月在 DTU 召开第 14 届）和
**Journal of Scheduling / Annals of Operations Research / EJOR** 是该问题的核心阵地。
ML/LLM × 调度交叉则在 NeurIPS、KR、ICML 工作坊和 arXiv 上活跃。

---

## 2. 主流方法地图（按本项目可借鉴度排序）

### 2.1 Adaptive Large Neighborhood Search（ALNS）—— **最值得借鉴**

- **代表文献**：Lindahl, Sørensen, Stidsen, *"Adaptive large neighborhood search
  for the curriculum-based course timetabling problem"*, **Annals of Operations
  Research**, 2018（在 ITC-2007 上刷新 5 个最佳已知解，至今是经典 baseline）。
- **核心思想**：反复"destroy（部分撤销）+ repair（用 CP/贪心修复）"，
  并维护多个 destroy / repair 算子的权重，按近期成功率自适应抽样。
- **2024 进展**：Sugimori et al., *"Large Neighborhood Prioritized Search for
  Combinatorial Optimization with ASP"*, **KR 2024** —— 把 ASP（Answer Set
  Programming）的优先级语义当 LNS 邻域，在 ITC-CB-CTT 上显著优于纯 ASP。
- **对本项目的 idea**：
  我们的 `reschedule_by_adjusting` 本质是**单课时的局部调课**，邻域过小、容易卡在
  局部最优（balanced 策略的"晚间课时占比 21.8%"就是典型征兆）。
  可以加一层 **ALNS 外壳**：
  1. destroy：随机/规则地撤销一整门课、一个教室周一全部安排、或冲突最严重的 5%
     课时；
  2. repair：复用现有 utils1 的贪心放置（first/random/balanced 都能当 repair 算子）；
  3. 算子权重按"是否带来完成率/熵提升"自适应。
  这一层与 LLM 模块完全解耦，可作为**消融实验"w/o ALNS"**的一组对比，
  也能直接用于打 ITC-2007/2019 benchmark。

### 2.2 Matheuristic：Fix-and-Optimize / 分解 + MIP

- **代表文献**：
  - Lindahl, Stidsen, Sørensen, *"A fix-and-optimize matheuristic for university
    timetabling"*, **Journal of Heuristics**, 2018.
  - Holm et al., *"A matheuristic for customized multi-level multi-criteria
    university timetabling"*, **EJOR**, 2023.
  - Holm et al., *"Real-world university course timetabling at ITC 2019"*,
    **Journal of Scheduling**, 2025.（ITC-2019 官方综述，MIP-based 方法在中等
    规模实例上最具竞争力）
- **核心思想**：把启发式找到的可行解里"大部分变量固定"，只对一小窗（如某天、
  某教室群）放开，交给 CP-SAT / Gurobi 做小规模精确优化；滚动窗口推进。
- **对本项目的 idea**：
  我们目前没有任何 MIP/CP 兜底，调课失败基本靠 LLM 给建议。可以**加一个
  OR-Tools CP-SAT 的窗口式精修阶段**：贪心跑完后，对"未排上 + 冲突高"的 5%
  课时，连同它们所在教室/时段的邻居，建一个 ≤500 变量的 CP-SAT 子问题求解。
  - 优势：完成率天花板能稳稳逼近 100%，是论文里很硬的卖点；
  - 工程量：1～2 周（CP-SAT 建模 + 与 utils1 的数据来回桥接）；
  - 论文上：天然成为消融实验里**"w/o Matheuristic"**的对比组。

### 2.3 MaxSAT / CP-SAT 直接建模

- **代表文献**：
  - Demirović & Stuckey, *"ITC 2019: University Course Timetabling with MaxSAT"*,
    **PATAT 2020 proceedings**（UniCorT，ITC-2019 五强）。
  - Lemos et al., *"Solving the Course-timetabling Problem of Cairo University
    Using Max-SAT"*, arXiv:1803.05027.
- **核心思想**：硬约束写成 CNF，软约束按权重 → MaxSAT；现代求解器（RC2、
  EvalMaxSAT、CP-SAT 的 lazy-clause）能直接吃。
- **对本项目的 idea**：
  全量 MaxSAT 不现实（西交大数据规模大，且我们的硬约束多含数值如教室容量）。
  但可借鉴**软约束权重显式化**：当前 utils1 的 balanced 评分
  `0.6×时段占用 + 0.25×容量浪费 + 0.35×晚间占比` 是手工调的，
  可以**用 MaxSAT 论文里的"层次化权重（hard ≫ critical-soft ≫ soft）"框架**
  把它写成一个清晰的目标函数表，再用一次小型 grid search / SMAC 调权重。
  论文里能写一节"目标函数设计与权重灵敏度分析"，比当前空口手调更站得住脚。

### 2.4 Hyper-heuristic（HH，选择型）

- **代表文献**：
  - Burke et al., *"Hyper-heuristics: a survey of the state of the art"*, **JORS**, 2013.
  - Siew et al., *"A Survey of Solution Methodologies for Exam Timetabling"*, 2024.
    （2012-2023 区间 HH 占 32%，是最活跃路线之一）
  - Kheiri & Keedwell 在 high school timetabling 上的 selection HH 工作。
- **核心思想**：维护一池 low-level heuristics（LLH），由一个高层选择器（强化学习
  / 多臂老虎机 / 案例推理）决定下一步用哪个 LLH，配上 move-acceptance（IE、
  Late Acceptance、Great Deluge 等）。
- **对本项目的 idea**：
  我们已经有了 first / random / balanced 三个 LLH，**只差一个选择器**。
  最轻量的做法是 **ε-greedy / UCB1** 选策略 + Late Acceptance Hill Climbing
  作为接受准则。可以替代当前"全程跑同一个策略"的设定，期望解决 balanced 容易
  陷入"晚间倾斜"的副作用。**优势是几乎不动核心代码**，只在 test_for_school.py
  外面加一层 wrapper。

### 2.5 LLM × 优化 / 调度（与本毕设主线最贴）

- **代表文献**：
  - Huang et al., *"On the Prospects of Incorporating LLMs in Automated Planning
    and Scheduling (APS)"*, arXiv:2401.02500, 2024.
  - Abgaryan et al., *"LLMs can Schedule"*, arXiv:2408.06993, 2024.
  - Liu et al., *"Discovering heuristics with LLMs for mixed-integer programs:
    Single-machine scheduling"*, **Computers & OR**, 2025.
  - Wu et al., *"Large Language Models for Combinatorial Optimization: A
    Systematic Review"*, arXiv:2507.03637, 2025.
  - Yang et al., *"ACCORD: Autoregressive Constraint-satisfying Generation for
    Combinatorial Optimization"*, arXiv:2506.11052, 2025.
- **核心趋势**：
  1. **LLM-as-Heuristic-Designer**：让 LLM 生成 Python 邻域算子或评分函数代码，
     由进化框架（FunSearch / EoH / ReEvo）打分迭代；
  2. **LLM-as-Reasoner**：在失败/卡壳时让 LLM 给"哪个约束/课/教室是瓶颈"的归因
     解释 → 指导下一轮搜索（我们当前 llm_failure_analysis.py 就是这一类，TRACE-cs
     式人工评估是标准做法）；
  3. **LLM-as-Constraint-Parser**：把"周三下午不排课""老张和老李不能同时间"等
     自然语言诉求转 IL/JSON 约束（我们 PKYQMS 文本解析就是这块）。
- **对本项目的 idea**：
  - 我们已落地 (2) 和 (3)，可在论文里**显式对标 LLMs-can-Schedule / ACCORD**
  作为相关工作；
  - 还可借鉴 **EoH（Evolution of Heuristics）** 的做法：让 qwen-plus 在
    `utils1.py` 的放置评分函数上做"代码级进化"，比手工调 0.6/0.25/0.35 更系统。
    这是一个 1~2 周可完成的小实验，能直接写进消融或扩展章节。

### 2.6 GNN / 强化学习（前沿但 ROI 较低）

- **代表文献**：
  - Eskandari Sabzi et al., *"Enhancing Genetic Algorithms with Graph Neural
    Networks: A Timetabling Case Study"*, Springer 2025.
  - Mahmoodjanloo et al., *"Train timetabling with multi-agent DRL"*,
    Transportation Research Part C, 2022.
  - Park et al., *"Learning to schedule job-shop problems via GNN+RL"*,
    arXiv:2106.01086.
  - Awesome-ML4CO（Thinklab-SJTU）整理的综述列表。
- **核心思想**：把课程-教室-时段建成异构图，GNN 学节点 embedding 喂给 RL
  policy 直接输出动作；或者用 GNN 当 GA 的 fitness 预测器加速。
- **对本项目的 idea**：
  **不推荐毕设阶段全力上**，原因：训练数据少（只有一所学校）、调参/复现成本高、
  对完成率提升通常 < 1pp。但**可以作为"局限性与未来工作"一节的展望**，把
  Awesome-ML4CO 引一下，显得视野完整。

### 2.7 Matheuristic + Pareto / 多目标

- **代表文献**：
  - Holm et al. (Lancaster), *"Modelling and Solving Multi-objective UCTT"*, 2024.
  - Yasari & Kheiri 在 PATAT 2024 的
    *"Matheuristic for Approximating a Frontier for a Many-objective University
    Timetabling Problem"*.
- **核心思想**：把"教师偏好满足率 / 学生时段集中度 / 教室利用率 / 晚间课比例"
  做成 4-6 个目标，给 stakeholder 看一条 Pareto 前沿而非一个解。
- **对本项目的 idea**：
  我们目前完成率 99.1%（first）/ 98.1%（balanced + 调课）已经很高，
  下一步的差异化卖点完全可以转向**多目标**：把"晚间课占比、周一/周五负载差、
  教师跨校区跳跃、教室容量浪费"这 4 个目标做 ε-constraint 扫描，输出 5~7 个
  Pareto 解供教务老师选。中期报告里"放置策略对比"那一节天然就是 2D 切片，
  扩成 4D 多目标几乎不增工作量。

---

## 3. 综合建议：本毕设最划算的吸收路径

按"性价比 × 与现有架构兼容度 × 论文卖点强度"排序，给出三档建议：

### 第一档（强烈建议做，每项 1-2 周）

| # | idea | 借鉴自 | 落地位置 | 论文贡献 |
|---|------|--------|----------|----------|
| A | **ALNS 外壳**：destroy 5% + repair 用现有贪心 | §2.1 Lindahl 2018 / Sugimori KR 2024 | 新增 `alns_wrapper.py`，包住 `run_pipeline.py` | 消融实验 "w/o ALNS"，且 ITC 基准可比 |
| B | **CP-SAT 窗口式精修**（matheuristic） | §2.2 Holm 2023/2025 | 新增 `cpsat_polish.py`，作为流水线最后一阶段 | 完成率天花板 → 100%，硬卖点 |
| C | **目标函数权重显式化 + 灵敏度分析** | §2.3 Demirović 2020 + §2.5 | 重构 `utils1.py` 评分项 | 解决"为什么是 0.6/0.25/0.35"的审稿质疑 |

### 第二档（如果时间充裕，每项 1 周）

| # | idea | 借鉴自 | 落地位置 |
|---|------|--------|----------|
| D | **ε-greedy / UCB 选择型超启发式**，在 first/random/balanced 三策略上切换 | §2.4 Burke 2013, Siew 2024 | 包一层 wrapper |
| E | **多目标 ε-constraint** 扫一条 Pareto 前沿 | §2.7 Holm 2024, Yasari PATAT 2024 | 新增 `pareto_sweep.py` |
| F | **LLM 进化放置评分函数代码**（EoH 风格） | §2.5 LLMs-for-CO Review 2025 | 复用 `llm_api.py` |

### 第三档（仅作展望，写未来工作）

- GNN-policy / 多智能体 RL：训练数据不足，毕设时间内 ROI 低。
- 全量 MaxSAT：数值约束多、规模大，编码代价不值。

---

## 4. "怎么做最好"——具体路线建议

结合现在的待办（ITC 接入 + 消融框架 + DashScope 阻塞），我建议的顺序：

1. **先做 A + C**（ALNS 壳 + 权重显式化）。
   - 这两步**不依赖 LLM 额度**（DashScope 当前欠费阻塞），是当前空档期最佳工作。
   - ALNS 壳天然产出"w/o ALNS" 的消融对比，把现在 first vs random vs balanced
     的实验数据直接复用为 repair 算子级别的对比。
2. **再做 B**（CP-SAT 精修）。
   - 等 ITC-2019 数据用 `data-convert` skill 转好之后，CP-SAT 精修在 ITC 实例上
     最能体现"matheuristic 接近最优"的论文价值。
3. **DashScope 恢复后做 F**（LLM 进化评分函数）。
   - 把"LLM 增强"从"只做解析 + 失败归因"扩到"LLM 进化代码"，
     是对中期报告主线的自然加强。
4. **最后做 E**（多目标 Pareto）作为应用章节。
   - 把"放置策略对比"升级成"多目标决策支持系统"，写故事更顺。

> 注：D（hyper-heuristic 选择器）和 A（ALNS）功能上有重叠，二者**做一个就够**。
> 推荐 A，因为它在 UCTT 文献里地位更稳，相关工作章节好写。

---

## 5. 参考文献（按出现顺序）

1. Lindahl M., Sørensen M., Stidsen T. R. *Adaptive large neighborhood search
   for the curriculum-based course timetabling problem.* Annals of Operations
   Research, 2018. https://link.springer.com/article/10.1007/s10479-016-2151-2
2. Sugimori T., Banbara M., Inoue K. *Large Neighborhood Prioritized Search for
   Combinatorial Optimization with Answer Set Programming.* KR 2024.
   https://proceedings.kr.org/2024/72/kr2024-0072-sugimori-et-al.pdf
3. Lindahl M., Stidsen T. R., Sørensen M. *A fix-and-optimize matheuristic for
   university timetabling.* Journal of Heuristics, 2018.
   https://link.springer.com/article/10.1007/s10732-018-9371-3
4. Holm D. S. et al. *A matheuristic for customized multi-level multi-criteria
   university timetabling.* EJOR, 2023.
   https://pmc.ncbi.nlm.nih.gov/articles/PMC10080184/
5. Holm D. S. et al. *Real-world university course timetabling at ITC 2019.*
   Journal of Scheduling, 2025.
   https://link.springer.com/article/10.1007/s10951-023-00801-w
6. Demirović E., Stuckey P. J. *ITC 2019: University Course Timetabling with
   MaxSAT.* PATAT 2020.
   https://www.patatconference.org/patat2020/proceedings/papers/9.%20PATAT_2020_paper_20.pdf
7. Lemos A. et al. *Solving the Course-timetabling Problem of Cairo University
   Using Max-SAT.* arXiv:1803.05027.
8. Burke E. K. et al. *Hyper-heuristics: a survey of the state of the art.*
   Journal of the Operational Research Society, 2013.
   https://link.springer.com/article/10.1057/jors.2013.71
9. Siew N. et al. *A Survey of Solution Methodologies for Exam Timetabling*, 2024.
   https://www.graham-kendall.com/papers/siewetal2024a.pdf
10. Huang X. et al. *On the Prospects of Incorporating LLMs in Automated
    Planning and Scheduling.* arXiv:2401.02500, 2024.
    https://arxiv.org/html/2401.02500v2
11. Abgaryan H. et al. *LLMs can Schedule.* arXiv:2408.06993, 2024.
    https://arxiv.org/html/2408.06993v1
12. Liu F. et al. *Discovering heuristics with LLMs for mixed-integer programs:
    Single-machine scheduling.* Computers & OR, 2025.
    https://www.sciencedirect.com/science/article/abs/pii/S0305054825003545
13. Wu Z. et al. *Large Language Models for Combinatorial Optimization: A
    Systematic Review.* arXiv:2507.03637, 2025.
    https://arxiv.org/pdf/2507.03637
14. Yang R. et al. *ACCORD: Autoregressive Constraint-satisfying Generation for
    Combinatorial Optimization with Routing and Dynamic attention.*
    arXiv:2506.11052, 2025. https://arxiv.org/pdf/2506.11052
15. Eskandari Sabzi H. et al. *Enhancing Genetic Algorithms with Graph Neural
    Networks: A Timetabling Case Study.* Springer, 2025.
    https://link.springer.com/chapter/10.1007/978-3-032-23604-3_17
16. Park J. et al. *Learning to schedule job-shop problems: Representation and
    policy learning using GNN and RL.* arXiv:2106.01086.
17. Thinklab-SJTU. *Awesome ML4CO.*
    https://github.com/Thinklab-SJTU/awesome-ml4co
18. Yasari P., Kheiri A. *Matheuristic for Approximating a Frontier for a
    Many-objective University Timetabling Problem.* PATAT 2024 Proceedings.
    https://patat.cs.kuleuven.be/patat-conferences/patat24/proceedings
19. PATAT 2024 Proceedings (DTU, August 2024).
    https://patatconference2024.dtu.dk/-/media/websites/patatconference2024/patat/patat-2024-proceedings.pdf
20. Babaei H. et al. *Modelling and solving the university course timetabling
    problem with hybrid teaching considerations.* Journal of Scheduling, 2024.
    https://link.springer.com/article/10.1007/s10951-024-00817-w
