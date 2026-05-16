"""
教室「静态不可行」时的 LLM 辅助：根据课程行教室相关字段，生成可执行的放宽建议（对应 Excel 修改或 relax_constraints）。

依赖：pip install openai
环境变量：OPENAI_API_KEY；可选 OPENAI_BASE_URL、OPENAI_MODEL（默认 gpt-4o-mini）
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from typing import Any, Callable, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Prompt（与 filter_suitable_classrooms 逻辑对齐，便于模型推理）
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """你是高校排课系统的顾问。用户遇到「教室静态不可行」：即在允许排课的教室列表中，
经过 filter_suitable_classrooms（严格条件）筛选后候选教室数量为 0。

根据上下文，分析可能出问题的部分，然后进行修改，比如当前课容量大于所有可用教室的课容量，或者当前指定教学楼、指定教室所在校区与课程校区不匹配，或者指定教室和教室类型不匹配，前者修改课容量大小，后者忽略指定教室

你必须输出**合法 JSON**（不要 Markdown 代码块），结构见用户消息中的 schema。
建议的 actions 要具体、可落到 Excel 字段或「开启 relax_constraints」，并按 priority 升序排列（1 最先尝试）。
tools 只能从用户给出的枚举中选择；若无合适工具，actions 可为空数组并在 summary 中说明。"""


def build_user_prompt(
    course_context: Dict[str, Any],
    strict_candidate_count: int,
    relaxed_candidate_count: int,
) -> str:
    ctx = json.dumps(course_context, ensure_ascii=False, indent=2)
    allowed_tools = "\n".join(f"- {t}" for t in REMEDIATION_TOOL_IDS)
    schema_hint = """{
  "summary": "string，简要说明为何可能候选为空",
  "actions": [
    {
      "tool": "工具枚举 id",
      "reason": "为何推荐这一步",
      "priority": 1,
      "excel_hint": "建议在课程表 Excel 中如何改（列名+操作）"
    }
  ]
}"""
    return f"""## 课程行上下文（JSON）
{ctx}

## 候选教室数量
- 严格条件 strict: {strict_candidate_count}
- 放宽条件 relaxed: {relaxed_candidate_count}

## 允许使用的 tool id（只能从这些里选）
{allowed_tools}

## 输出 JSON Schema
{schema_hint}

若 relaxed > 0 而 strict == 0，应优先建议「数据上对齐指定教室/教学楼」或「clean 指定码」；也可说明系统侧已可尝试 relax_constraints。"""


REMEDIATION_TOOL_IDS: Tuple[str, ...] = (
    "relax_constraints_true",  # 排课/诊断时开启 filter_suitable_classrooms(..., True)
    "lower_KRL",  # 减小课容量 KRL（需教务同意）
    "clear_JASDM",  # 清空指定教室代码 JASDM
    "clear_LSJASDM",  # 清空历年教室 LSJASDM（解除 code_set 钉死）
    "clear_JXLDM",  # 清空教学楼偏好 JXLDM
    "clear_JASLXMC",  # 清空教室类型名称要求 JASLXMC（及必要时 JASLXDM）
    "change_SKXQ",  # 修改上课校区 SKXQ 与教室表一致
    "widen_JASDM_codes",  # 扩充 JASDM 分号列表，加入更多可替代教室码（需对照教室表）
    "verify_codes_in_classroom_table",  # 核对 JASDM/LSJASDM 是否在教室表、SFYXPK 是否允许排课
)


# ---------------------------------------------------------------------------
# 本地「工具」：将 LLM 建议映射为可复制字段修改（不调用远程）
# ---------------------------------------------------------------------------

def course_row_dict_from_jxb(jxb: Any) -> Dict[str, Any]:
    """从 Course 实例抽取与教室静态筛选相关的字段。"""
    keys = (
        "JXBID",
        "KCM",
        "SKXQ",
        "KRL",
        "JASDM",
        "LSJASDM",
        "JXLDM",
        "JASLXDM",
        "JASLXMC",
    )
    out: Dict[str, Any] = {}
    for k in keys:
        v = getattr(jxb, k, None)
        if v is not None and not (isinstance(v, float) and str(v) == "nan"):
            out[k] = v
    return out


def apply_remediation_tool(
    row: Dict[str, Any],
    tool: str,
    *,
    new_KRL: Optional[int] = None,
    new_SKXQ: Optional[str] = None,
    extra_JASDM_codes: Optional[str] = None,
) -> Dict[str, Any]:
    """
    返回**拷贝**后的课程行字典，模拟执行某一工具（用于预览，不写 Excel）。
    未提供的参数则不做对应修改。
    """
    r = deepcopy(row)
    if tool == "clear_JASDM":
        r["JASDM"] = None
    elif tool == "clear_LSJASDM":
        r["LSJASDM"] = None
    elif tool == "clear_JXLDM":
        r["JXLDM"] = None
    elif tool == "clear_JASLXMC":
        r["JASLXMC"] = None
        r.pop("JASLXDM", None)
    elif tool == "lower_KRL" and new_KRL is not None:
        r["KRL"] = new_KRL
    elif tool == "change_SKXQ" and new_SKXQ is not None:
        r["SKXQ"] = new_SKXQ
    elif tool == "widen_JASDM_codes" and extra_JASDM_codes:
        cur = r.get("JASDM") or ""
        r["JASDM"] = ";".join(
            [x.strip() for x in str(cur).split(";") if x.strip()]
            + [x.strip() for x in str(extra_JASDM_codes).split(";") if x.strip()]
        )
    # relax_constraints_true / verify_* 无 row 字段变更
    return r


# ---------------------------------------------------------------------------
# LLM 调用
# ---------------------------------------------------------------------------

def call_llm_classroom_static_remediation(
    course_context: Dict[str, Any],
    strict_candidate_count: int = 0,
    relaxed_candidate_count: int = 0,
    *,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
) -> Dict[str, Any]:
    """
    调用大模型，返回解析后的 JSON dict：summary + actions[]。
    """
    try:
        from openai import OpenAI
    except ImportError as e:
        raise ImportError("请先安装: pip install openai") from e

    key = api_key or os.environ.get("OPENAI_API_KEY")
    if not key:
        raise ValueError("未设置 OPENAI_API_KEY，也未传入 api_key")

    client_kw: Dict[str, Any] = {"api_key": key}
    burl = base_url or os.environ.get("OPENAI_BASE_URL")
    if burl:
        client_kw["base_url"] = burl

    client = OpenAI(**client_kw)
    model_name = model or os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

    user_content = build_user_prompt(
        course_context, strict_candidate_count, relaxed_candidate_count
    )

    resp = client.chat.completions.create(
        model=model_name,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        response_format={"type": "json_object"},
        temperature=0.2,
    )
    text = resp.choices[0].message.content or "{}"
    return json.loads(text)


def suggest_with_optional_preflight(
    jxb: Any,
    list_of_jas: List[Any],
    *,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
) -> Dict[str, Any]:
    """
    组合：从 Course 实例统计 strict/relaxed 候选数 + 调 LLM。
    需传入与排课一致的允许排课教室列表 list_of_jas。
    """
    from utils1 import filter_suitable_classrooms

    strict_j = filter_suitable_classrooms(list_of_jas, jxb, relax_constraints=False)
    relaxed_j = filter_suitable_classrooms(list_of_jas, jxb, relax_constraints=True)
    ctx = course_row_dict_from_jxb(jxb)
    return call_llm_classroom_static_remediation(
        ctx,
        len(strict_j or []),
        len(relaxed_j or []),
        model=model,
        api_key=api_key,
        base_url=base_url,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli() -> None:
    import argparse

    p = argparse.ArgumentParser(description="教室静态不可行：LLM 修改建议")
    p.add_argument("--jxbid", default="", help="教学班 ID；若给则需 --course-excel")
    p.add_argument("--course-excel", default="", help="课程表 xlsx 路径")
    p.add_argument("--classroom-excel", default="", help="教室表 xlsx（数候选）")
    p.add_argument("--weeks", type=int, default=17)
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--periods", type=int, default=11)
    p.add_argument("--json-context", default="", help="直接传入 JSON 文件路径，跳过读课")
    p.add_argument("--strict", type=int, default=0)
    p.add_argument("--relaxed", type=int, default=0)
    args = p.parse_args()

    if args.json_context:
        with open(args.json_context, "r", encoding="utf-8") as f:
            ctx = json.load(f)
        out = call_llm_classroom_static_remediation(ctx, args.strict, args.relaxed)
    elif args.jxbid and args.course_excel:
        from Basic_Data import load_courses, load_classrooms
        from utils1 import get_consecutive_jxbid

        courses = load_courses(
            args.course_excel, weeks=args.weeks, days=args.days, periods=args.periods
        )
        jxbs = get_consecutive_jxbid(args.jxbid.strip(), courses)
        if not jxbs:
            raise SystemExit("未找到教学班")
        classrooms = load_classrooms(
            args.classroom_excel or os.path.join("智能排课基础数据", "更新后的教室表.xlsx"),
            weeks=args.weeks,
            days=args.days,
            periods=args.periods,
        )
        list_of_jas = [c for c in classrooms if c.SFYXPK == "1"]
        out = suggest_with_optional_preflight(jxbs[0], list_of_jas)
    else:
        demo = {
            "JXBID": "DEMO",
            "KCM": "示例课",
            "SKXQ": "兴庆校区",
            "KRL": 200,
            "JASDM": "999999",
            "LSJASDM": None,
            "JXLDM": "仅存在教学楼代码",
            "JASLXMC": "实验室",
        }
        out = call_llm_classroom_static_remediation(demo, 0, 0)
        print("(演示上下文，未读 Excel)", file=__import__("sys").stderr)

    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _cli()
