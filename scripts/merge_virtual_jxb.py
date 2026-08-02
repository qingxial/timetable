# -*- coding: utf-8 -*-
"""后处理：把排课结果里 JXBID 的 @W/@B 后缀去掉，合并回原 JXBID。
- 输入：排课结果/排课结果_全部.xlsx、调课后的整体结果.xlsx、排课失败课程_全部.xlsx、
        调课失败的排课失败课程.xlsx、调课成功的排课失败课程.xlsx
- 输出：同目录下 *_merged.xlsx（保留原文件以便对比）
- 同班的两条时段记录（@W 和 @B）保留为独立行（同学生不感知拆分）

用法：
  .venv/bin/python scripts/merge_virtual_jxb.py             # 默认 排课结果/
  .venv/bin/python scripts/merge_virtual_jxb.py --dir 排课结果4
"""
import argparse, os
import pandas as pd

DEFAULT_DIR = "排课结果"
# 目标文件列表（不存在则跳过）+ 该文件里教学班 ID 列名
TARGETS = [
    ("排课结果_全部.xlsx", "教学班ID"),
    ("调课后的整体结果.xlsx", "教学班ID"),
    ("排课失败课程_全部.xlsx", "教学班ID"),
    ("调课成功的排课失败课程.xlsx", "教学班ID"),
    ("调课失败的排课失败课程.xlsx", "jxbid"),
    ("全部调整结果.xlsx", "教学班ID"),
    ("调课时调整的已排课程.xlsx", "教学班ID"),
    ("失败课程归因与建议.xlsx", "教学班ID"),
    ("全部_全部/排课结果.xlsx", "教学班ID"),
    ("全部_全部/排课失败课程.xlsx", "教学班ID"),
]


def strip_suffix(s):
    """'202520261@W' → '202520261'；其他原样返回。"""
    if not isinstance(s, str):
        return s
    if "@" in s:
        base, suf = s.rsplit("@", 1)
        if suf in ("W", "B"):
            return base
    return s


def process_one(path, id_col):
    df = pd.read_excel(path)
    if id_col not in df.columns:
        print(f"  [skip] {path} 缺列 {id_col}（实际列：{list(df.columns)[:5]}...）")
        return None
    before_n = df[id_col].nunique()
    df[id_col] = df[id_col].astype(str).map(strip_suffix)
    after_n = df[id_col].nunique()
    return df, before_n, after_n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=DEFAULT_DIR)
    args = ap.parse_args()

    print(f"处理目录：{args.dir}")
    print()
    for fname, id_col in TARGETS:
        path = os.path.join(args.dir, fname)
        if not os.path.isfile(path):
            continue
        try:
            r = process_one(path, id_col)
            if r is None:
                continue
            df, before_n, after_n = r
            out_name = fname.replace(".xlsx", "_merged.xlsx")
            out_path = os.path.join(args.dir, out_name)
            os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
            df.to_excel(out_path, index=False)
            virtual = before_n - after_n
            print(f"  ✓ {fname:40s} | 唯一{id_col} {before_n} → {after_n}（合并虚班 {virtual} 对）→ {out_name}")
        except Exception as e:
            print(f"  ✗ {fname} 处理失败：{e}")

    print()
    print("完成。所有 *_merged.xlsx 已保存，原始文件未动。")


if __name__ == "__main__":
    main()
