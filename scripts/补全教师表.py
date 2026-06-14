"""
教师表整理（流水线 fix-teachers 环节）：
  1. 按教师编号 JSH 去重（保留首次出现的行）；
  2. 把课程表中出现、但教师表中缺失的教师（JSH+XM）补进教师表。

用法：python scripts/补全教师表.py [--dir 数据目录] [--dry-run]
- 无需改动时不写文件（幂等，可放心在流水线中每次运行）
- 修改前自动备份教师表为 教师表.backup_<时间戳>.xlsx
- 写入时保留课程表单元格的原始值（不做类型转换）：排课引擎 get_teacher_instances
  按原始值精确匹配 JSH，文本/数字类型不一致会导致匹配失败
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402
from openpyxl import load_workbook  # noqa: E402


def ns(v) -> str:
    return "" if v is None else str(v).strip()


def main():
    parser = argparse.ArgumentParser(description="用课程表补全教师表中缺失的教师")
    parser.add_argument("--dir", default=os.path.join("智能排课基础数据", "提取的基础数据表_converted"))
    parser.add_argument("--dry-run", action="store_true", help="只统计不写入")
    args = parser.parse_args()

    course_path = os.path.join(args.dir, "课程表.xlsx")
    teacher_path = os.path.join(args.dir, "教师表.xlsx")

    # 课程表：两行表头，第2行英文字段名，逐行取 JSH/XM 原始值
    ws = load_workbook(course_path, read_only=True).active
    headers = [c.value for c in ws[2]]
    col_jsh = headers.index("JSH") + 1
    col_xm = headers.index("XM") + 1

    df_t = pd.read_excel(teacher_path)

    # 第一步：按 JSH 去重（空 JSH 行不参与去重判断，原样保留）
    keys = df_t["JSH"].map(ns)
    dup_mask = keys.duplicated(keep="first") & (keys != "")
    n_dup = int(dup_mask.sum())
    if n_dup:
        dup_jshs = sorted(set(keys[dup_mask]))
        print(f"教师表中发现重复 JSH {len(dup_jshs)} 个（共 {n_dup} 行将被去除，保留首行）："
              f"{dup_jshs[:5]}")
        df_t = df_t[~dup_mask].reset_index(drop=True)

    existing = {ns(v) for v in df_t["JSH"].dropna()}

    # 第二步：从课程表补缺失教师
    missing: dict[str, tuple] = {}  # 规范化JSH -> (原始JSH, 原始XM)
    for row in ws.iter_rows(min_row=3, values_only=False):
        raw_jsh = row[col_jsh - 1].value
        raw_xm = row[col_xm - 1].value
        key = ns(raw_jsh)
        if key and key not in existing and key not in missing:
            missing[key] = (raw_jsh, raw_xm)

    print(f"教师表现有 {len(df_t)} 人；课程表中缺失教师 {len(missing)} 人")
    if not missing and not n_dup:
        print("无需整理。")
        return

    if missing:
        no_name = [k for k, (_, xm) in missing.items() if not ns(xm)]
        if no_name:
            print(f"  其中 {len(no_name)} 人课程表里也没有姓名（XM 为空），将以空姓名补入，"
                  f"例如: {no_name[:5]}")
        sample = list(missing.items())[:5]
        print("  示例: " + "; ".join(f"{k}({ns(x[1]) or '无名'})" for k, x in sample))

    if args.dry_run:
        print("--dry-run：未写入。")
        return

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = teacher_path.replace(".xlsx", f".backup_{ts}.xlsx")
    shutil.copy2(teacher_path, backup)
    print(f"已备份教师表到: {backup}")

    out_df = df_t
    if missing:
        add_df = pd.DataFrame(
            [{"JSH": raw_jsh, "XM": raw_xm} for raw_jsh, raw_xm in missing.values()],
            columns=df_t.columns if set(df_t.columns) >= {"JSH", "XM"} else ["JSH", "XM"],
        )
        out_df = pd.concat([df_t, add_df], ignore_index=True)
    out_df.to_excel(teacher_path, index=False)
    print(f"已写入: {teacher_path}，教师表现共 {len(out_df)} 人"
          f"（去重 -{n_dup} 行，补缺 +{len(missing)} 人）")


if __name__ == "__main__":
    main()
