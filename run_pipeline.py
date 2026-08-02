"""
一键全流程排课流水线：python run_pipeline.py

按顺序串联（对应中期报告的"排课—分析—反馈"闭环）：
  1. fix-teachers    教师表按 JSH 去重 + 从课程表补全缺失教师（幂等）
  2. check-data      校验基础数据表格式；ZXS 异常课程自动导出待教务确认清单
  3. preflight       规则版排课前检查
  4. llm-preflight   LLM 排课前合理性复核（无 API Key 时自动跳过）
  5. schedule        首轮贪心排课（test_for_school.py，耗时最长，约 1-3 小时）
  6. reschedule      变邻域调课（reschedule_by_adjusting.py）
  7. postfailure     规则版失败归因
  8. llm-failure     LLM 失败归因与建议（无 API Key 时自动跳过）
  9. visualize       利用率等可视化图表
 10. report          综合分析报告

常用用法：
  python run_pipeline.py                          # 跑全流程
  python run_pipeline.py --no-llm                 # 跳过两个 LLM 环节
  python run_pipeline.py --from-stage reschedule  # 从调课开始（已有首轮结果时）
  python run_pipeline.py --only llm-failure       # 只跑某一个环节
  python run_pipeline.py --skip visualize --skip report
  python run_pipeline.py --list                   # 查看所有环节

各阶段以子进程运行（python <脚本>），输出实时透传到终端；
每次运行在 logs/ 下生成 pipeline_<时间戳>_summary.txt 记录各阶段耗时与结果。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime
from typing import List, Optional

ROOT = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable


def _llm_available() -> bool:
    try:
        import llm_api
        return llm_api.has_api_key()
    except Exception:
        return False


class Stage:
    def __init__(self, name: str, desc: str, cmd: List[str],
                 needs_llm: bool = False, stdin_devnull: bool = False):
        self.name = name
        self.desc = desc
        self.cmd = cmd
        self.needs_llm = needs_llm
        self.stdin_devnull = stdin_devnull


def build_stages(weeks: int, days: int, periods: int, strategy: str = "first") -> List[Stage]:
    base = os.path.join("智能排课基础数据", "提取的基础数据表_converted")
    dims = ["--weeks", str(weeks), "--days", str(days), "--periods", str(periods)]
    return [
        Stage("fix-teachers", "教师表按 JSH 去重并补全课程表中缺失的教师",
              [PY, os.path.join("scripts", "补全教师表.py")]),
        Stage("check-data", "校验基础数据表格式",
              [PY, os.path.join("scripts", "validate_converted_data.py")] + dims),
        Stage("preflight", "规则版排课前检查",
              [PY, "preflight_schedule_checks.py",
               "--course", os.path.join(base, "课程表.xlsx"),
               "--teacher", os.path.join(base, "教师表.xlsx"),
               "--classroom", os.path.join(base, "教室表.xlsx"),
               "--banji", os.path.join(base, "班级表.xlsx")] + dims),
        Stage("llm-preflight", "LLM 排课前合理性复核",
              [PY, "llm_preflight_check.py"] + dims, needs_llm=True),
        Stage("schedule", "首轮贪心排课（耗时最长）",
              [PY, "test_for_school.py", "--strategy", strategy]),
        Stage("reschedule", "变邻域调课",
              [PY, "reschedule_by_adjusting.py"]),
        Stage("postfailure", "规则版失败归因",
              [PY, "postfailure_analysis.py"] + dims, stdin_devnull=True),
        Stage("llm-failure", "LLM 失败归因与建议",
              [PY, "llm_failure_analysis.py"] + dims, needs_llm=True),
        Stage("visualize", "可视化图表",
              [PY, "scheduling_visualization.py", "all", "--charts-subdir", "图表展示"]),
        Stage("report", "综合分析报告",
              [PY, "analysis_report.py"]),
    ]


def main():
    parser = argparse.ArgumentParser(
        description="排课全流程一键运行", formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    parser.add_argument("--weeks", type=int, default=20)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--periods", type=int, default=11)
    parser.add_argument("--strategy", choices=["first", "random", "balanced"], default="first",
                        help="schedule 环节的放置策略（消融实验用，默认 first=原始方案）")
    parser.add_argument("--skip", action="append", default=[], metavar="STAGE",
                        help="跳过某环节（可多次使用）")
    parser.add_argument("--only", action="append", default=[], metavar="STAGE",
                        help="只运行指定环节（可多次使用）")
    parser.add_argument("--from-stage", default=None, metavar="STAGE",
                        help="从指定环节开始运行")
    parser.add_argument("--no-llm", action="store_true", help="跳过所有 LLM 环节")
    parser.add_argument("--keep-going", action="store_true",
                        help="某环节失败后继续运行后续环节（默认失败即停止）")
    parser.add_argument("--list", action="store_true", help="列出所有环节后退出")
    args = parser.parse_args()

    stages = build_stages(args.weeks, args.days, args.periods, args.strategy)
    names = [s.name for s in stages]

    if args.list:
        for s in stages:
            tag = "（需 LLM API）" if s.needs_llm else ""
            print(f"  {s.name:<14} {s.desc}{tag}")
        return

    for n in args.skip + args.only + ([args.from_stage] if args.from_stage else []):
        if n not in names:
            parser.error(f"未知环节: {n}（可用: {', '.join(names)}）")

    selected = list(stages)
    if args.only:
        selected = [s for s in selected if s.name in args.only]
    if args.from_stage:
        idx = names.index(args.from_stage)
        selected = [s for s in selected if names.index(s.name) >= idx]
    selected = [s for s in selected if s.name not in args.skip]

    llm_ok = (not args.no_llm) and _llm_available()
    if not llm_ok:
        why = "--no-llm" if args.no_llm else "未配置 DASHSCOPE_API_KEY"
        skipped_llm = [s.name for s in selected if s.needs_llm]
        if skipped_llm:
            print(f"提示：{why}，将跳过 LLM 环节: {', '.join(skipped_llm)}")
        selected = [s for s in selected if not s.needs_llm]

    if not selected:
        print("没有需要运行的环节。")
        return

    os.makedirs("logs", exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    summary_path = os.path.join("logs", f"pipeline_{ts}_summary.txt")
    summary: List[str] = [f"排课流水线运行记录  {ts}",
                          f"参数: weeks={args.weeks} days={args.days} periods={args.periods}",
                          f"环节: {', '.join(s.name for s in selected)}", ""]

    print("=" * 60)
    print(f"排课流水线启动，共 {len(selected)} 个环节: " + " → ".join(s.name for s in selected))
    print("=" * 60)

    t_total = time.time()
    failed_stage: Optional[str] = None
    for i, stage in enumerate(selected, 1):
        print(f"\n{'=' * 60}\n[{i}/{len(selected)}] {stage.name} — {stage.desc}")
        print("命令: " + " ".join(stage.cmd))
        print("=" * 60)
        t0 = time.time()
        proc = subprocess.run(
            stage.cmd, cwd=ROOT,
            stdin=subprocess.DEVNULL if stage.stdin_devnull else None,
        )
        dt = time.time() - t0
        status = "成功" if proc.returncode == 0 else f"失败(exit={proc.returncode})"
        line = f"[{stage.name}] {status}，耗时 {dt / 60:.1f} 分钟"
        print(f"\n>>> {line}")
        summary.append(line)
        if proc.returncode != 0:
            failed_stage = stage.name
            if not args.keep_going:
                summary.append(f"流水线在 {stage.name} 处中止（未加 --keep-going）")
                break

    total_line = f"总耗时 {(time.time() - t_total) / 60:.1f} 分钟"
    summary.append(total_line)
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write("\n".join(summary) + "\n")

    print("\n" + "=" * 60)
    print("\n".join(summary[3:]))
    print(f"运行记录已写入: {summary_path}")
    if failed_stage:
        print(f"\n注意：环节 {failed_stage} 失败，修复后可用 "
              f"`python run_pipeline.py --from-stage {failed_stage}` 续跑")
        sys.exit(1)


if __name__ == "__main__":
    main()
