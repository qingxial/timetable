# -*- coding: utf-8 -*-
"""小数周学时「前后分段」拆分（构造层）。B 优先、A 兜底，无损可逆。

模型（与用户敲定）：把一门小数 ZXS 课按【周次前后】切成两个虚班，各自整块排：
  @A = 前 a 周，每周 H 节（前段满）
  @B = 后 b 周，每周 L 节（H > L）
  a + b = N（原上课周数），且 H·a + L·b = T（理论学时 LLXS，整数）→ 零残差。

拆分规则：
  B（优先，全 2 节块）：H、L 均为偶数、gap=2 → a=(T−L·N)/2；仅 T 为偶数可解（≈520 门）
  A（兜底，带余除法）：H=L+1、L=⌊T/N⌋、a=r=T mod N → 对任意 T、N 恒有整数解
  不拆：H>8（ZXS>8 短周次集中课）或 ZXS 为整数 → 原样保留

无损保证（round-trip 自检）：
  1. @A、@B 复制原行的【全部字段】，仅改 JXBID 后缀 / ZXS / SKZCDM / RWJSZCDM 四项；
  2. @A 周次 ⊎ @B 周次 == 原上课周次（不重不漏，严格划分）；
  3. RWJSZCDM 同法按同一前后边界切分，教师维度不丢；
  4. 多教师行：每条教师行各自裂成 @A/@B（教师×段 的笛卡尔积全部保留）；
  5. 合并侧 merge：去 @A/@B 后缀 + 周次并集，可完全复原原教学班。
"""
import os, sys, argparse, shutil, time
from openpyxl import load_workbook

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SRC = "智能排课基础数据/提取的基础数据表_converted/课程表_split_merged.xlsx"

# 列索引（0-based）——与 load_courses 一致
C_KCM, C_JXBID, C_LLXS, C_SKZCDM, C_ZXS, C_RWJSZCDM, C_SFXYPK = 2, 7, 12, 14, 16, 22, 28
MASK_LEN = 23   # 周次代码位串长度


def _teaching_weeks(mask):
    """周次掩码 -> 上课周的 0-based 下标列表（升序）。"""
    s = str(mask or "")
    return [i for i, ch in enumerate(s) if ch == '1']


def _mask_from_weeks(weeks, length):
    """由周下标集合还原为定长位串。"""
    b = ['0'] * length
    for w in weeks:
        if 0 <= w < length:
            b[w] = '1'
    return ''.join(b)


def decompose(T, N):
    """返回 (方案, H, L, a, b)。方案∈{'B','A','skip'}。a=前段周数(H节), b=后段周数(L节)。"""
    if T is None or N <= 0:
        return ('skip', None, None, None, None)
    # B 优先：H、L 偶数、gap=2
    if T % 2 == 0:
        L = 2 * (T // (2 * N))            # 最大偶数 ≤ T/N
        H = L + 2
        num = T - L * N
        if H <= 8 and num >= 0 and num % 2 == 0:
            a = num // 2
            if 0 <= a <= N:
                # a==0 或 a==N 表示其实是整数节，退化；仍走 A 判定更自然
                if 0 < a < N or (a in (0, N)):
                    return ('B', H, L, a, N - a)
    # A 兜底：带余除法，恒有解
    L = T // N
    r = T % N
    H = L + 1
    if H > 8:
        return ('skip', None, None, None, None)   # 峰值超 8 节，集中课专项
    if r == 0:
        return ('skip', None, None, None, None)    # 整除→本就是整数节，无需拆
    return ('A', H, L, r, N - r)


def split_row(row, scheme, H, L, a, b):
    """把一条原始行裂成 (@A行, @B行)。复制全部字段，仅改 JXBID/ZXS/SKZCDM/RWJSZCDM。"""
    sk = str(row[C_SKZCDM] or "")
    rw = str(row[C_RWJSZCDM] or "")
    length = max(len(sk), MASK_LEN)
    tw = _teaching_weeks(sk)                 # 课程上课周（按 SKZCDM）
    front, back = tw[:a], tw[a:]             # 前 a 周 / 后 b 周（严格划分）
    fs, bs = set(front), set(back)

    def make(suffix, weeks_set, zxs):
        r = list(row)
        r[C_JXBID] = f"{row[C_JXBID]}{suffix}"
        r[C_ZXS] = zxs
        r[C_SKZCDM] = _mask_from_weeks(weeks_set, length)
        # 教师周次同法按前后边界裁剪（只保留落在该段的教师周），教师维度不丢
        rw_weeks = set(_teaching_weeks(rw)) & weeks_set
        r[C_RWJSZCDM] = _mask_from_weeks(rw_weeks, len(rw) if rw else length)
        return r

    return make("@A", fs, H), make("@B", bs, L)


def run(src, dst, apply=False):
    wb = load_workbook(src)
    ws = wb.active
    header1 = [c.value for c in ws[1]]
    header2 = [c.value for c in ws[2]]

    out_rows = []
    stats = {'B': 0, 'A': 0, 'skip_int': 0, 'skip_peak': 0, 'unchanged': 0, 'split_rows': 0}
    # 无损/守恒自检累加
    checks = {'field_ok': 0, 'week_partition_ok': 0, 'conserve_ok': 0, 'total_split': 0}

    for r in ws.iter_rows(min_row=3, values_only=True):
        row = list(r)
        try:
            z = float(row[C_ZXS])
        except (TypeError, ValueError):
            out_rows.append(row); stats['unchanged'] += 1; continue
        is_frac = (z != int(z))
        sfxypk = str(row[C_SFXYPK]).strip() in ('1', '1.0')
        if not is_frac or not sfxypk:
            out_rows.append(row); stats['unchanged'] += 1; continue

        # T 用 LLXS（用户敲定），空则回退 学时XS（idx 11）
        T = row[C_LLXS] if row[C_LLXS] not in (None, '') else row[11]
        try:
            T = int(float(T))
        except (TypeError, ValueError):
            out_rows.append(row); stats['unchanged'] += 1; continue
        N = len(_teaching_weeks(row[C_SKZCDM]))

        scheme, H, L, a, b = decompose(T, N)
        if scheme == 'skip':
            out_rows.append(row)
            stats['skip_peak' if (T // N + 1) > 8 else 'skip_int'] += 1
            continue

        ra, rb = split_row(row, scheme, H, L, a, b)
        out_rows.append(ra); out_rows.append(rb)
        stats[scheme] += 1; stats['split_rows'] += 2

        # —— round-trip 自检 ——
        checks['total_split'] += 1
        # (1) 字段无损：除 4 个改动列外全等
        changed_idx = {C_JXBID, C_ZXS, C_SKZCDM, C_RWJSZCDM}
        field_ok = all(ra[i] == row[i] and rb[i] == row[i]
                       for i in range(len(row)) if i not in changed_idx)
        checks['field_ok'] += field_ok
        # (2) 周次严格划分：@A ⊎ @B == 原，且不重
        wa, wb = set(_teaching_weeks(ra[C_SKZCDM])), set(_teaching_weeks(rb[C_SKZCDM]))
        worig = set(_teaching_weeks(row[C_SKZCDM]))
        checks['week_partition_ok'] += (wa | wb == worig and wa & wb == set())
        # (3) 守恒：H·|A| + L·|B| == T
        checks['conserve_ok'] += (H * len(wa) + L * len(wb) == T)

    # 报告
    print("=== 拆分统计 ===")
    print(f"  B(全2节块): {stats['B']}   A(带余兜底): {stats['A']}")
    print(f"  不拆-整除: {stats['skip_int']}   不拆-峰值>8: {stats['skip_peak']}")
    print(f"  未改动原样行: {stats['unchanged']}   新增拆分行: {stats['split_rows']}")
    print(f"  输出总行数: {len(out_rows)}（原 {ws.max_row - 2}）")
    n = checks['total_split']
    print("\n=== 无损/守恒自检（应全部 == 拆分课数）===")
    print(f"  拆分课程数: {n}")
    print(f"  ①字段无损: {checks['field_ok']}/{n}")
    print(f"  ②周次严格划分(不重不漏): {checks['week_partition_ok']}/{n}")
    print(f"  ③总学时守恒 H·a+L·b==T: {checks['conserve_ok']}/{n}")
    all_ok = (checks['field_ok'] == n and checks['week_partition_ok'] == n and checks['conserve_ok'] == n)
    print(f"  → {'✅ 全部无损、零残差' if all_ok else '❌ 存在丢失/残差，需排查'}")

    if apply:
        shutil.copy(src, src + '.bak.' + time.strftime('%Y%m%d_%H%M%S'))
        # 写新文件：保留两行表头
        from openpyxl import Workbook
        nwb = Workbook(); nws = nwb.active
        nws.append(header1); nws.append(header2)
        for row in out_rows:
            nws.append(row)
        nwb.save(dst)
        print(f"\n已写出拆分课程表: {dst}")
    else:
        print("\n(dry-run，未写文件；加 --apply 生成拆分课程表)")
    return all_ok


def main():
    ap = argparse.ArgumentParser(description="小数周学时前后分段拆分（B优先A兜底，无损）")
    ap.add_argument('--src', default=DEFAULT_SRC)
    ap.add_argument('--dst', default="智能排课基础数据/提取的基础数据表_converted/课程表_frac_split.xlsx")
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()
    os.chdir(ROOT)
    ok = run(args.src, args.dst, apply=args.apply)
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
