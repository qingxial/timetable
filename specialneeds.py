"""
排课要求文本（PKYQMS 列）解析模块（直接运行，需配置 DASHSCOPE_API_KEY）。

调用 LLM 从教师填写的自由文本中抽取结构化约束：
时间偏好、禁止时段、教学楼/教室要求等，输出到 智能排课基础数据/timelimit.xlsx。
"""
import argparse
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

import openpyxl



API_KEY = os.getenv("DASHSCOPE_API_KEY","sk-910f8ece9acf485eb00358b7a15d8dc2")
BASE_URL = os.getenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
MODEL = os.getenv("DASHSCOPE_MODEL", "qwen3.5-35b-a3b") #qwen-plus

DEFAULT_INPUT_FILE = "./智能排课基础数据/课程表2025-2026-1_pipeline.xlsx"
DEFAULT_CLASSROOM_FILE = "./智能排课基础数据/更新后的教室表.xlsx"
DEFAULT_OUTPUT_FILE = "./智能排课基础数据/timelimit.xlsx"

TEXT_COLUMN = "PKYQMS"
WEEKLY_HOURS_COLUMN = "ZXS"
CAMPUS_COLUMN = "SKXQ"
CAPACITY_COLUMN = "KRL"

OUTPUT_CODE_COLUMNS = ["Prefer_Time", "unavailable_Time", "JXLDM", "JASDM"]
NOTE_COLUMN = "special_need_note"
BATCH_SIZE = 10
MAX_BUILDINGS_IN_PROMPT = 120
MAX_CLASSROOMS_IN_PROMPT = 160


SYSTEM_PROMPT = """你是高校排课需求结构化抽取助手。
你的任务是从教师填写的排课要求中抽取时间约束、教学楼约束、教室约束。
必须严格依据用户提供的教学楼/教室候选清单输出代码；不能编造代码。
只输出 JSON，不要输出 markdown、解释或额外文字。"""


PROMPT_TEMPLATE = """
请从“排课要求描述”中抽取结构化排课约束。

【课程上下文】
- 排课要求描述: {requirement}
- 课内周学时 ZXS: {weekly_hours}
- 上课校区 SKXQ: {campus}
- 课容量 KRL: {capacity}

【可用教学楼候选】
候选项格式为: 教学楼名称 | 教学楼代码 | 校区 | 别名
{building_context}

【可用教室候选】
候选项格式为: 教室名称 | 教室代码 | 教学楼名称 | 教学楼代码 | 校区 | 容量 | 类型
{classroom_context}

【输出字段】
返回 JSON 对象，字段必须完整：
{{
  "preferred_slots": [],
  "unavailable_slots": [],
  "JXLDM": [],
  "JASDM": [],
  "warnings": []
}}

【时间抽取规则】
0. 时间偏好主要有三类格式：全周某时段、某天全天、某天某节。例如：
   - 全周（1-2节）
   - 周五全天
   - 周三（3-4节）
   同一类格式要写在一起，用顿号 `、` 分隔。不同格式用`,`分开。
   示例：
   输入：一次排一个班，不超过35人。排课时间请参考18-19第一学期时间排课，能排在周内最好，晚上尽量不排。
   输出：
   {{
     "preferred_slots": [],
     "unavailable_slots": ["周六全天、周日全天", "全周（9-10节）"]
   }}
1. 如果只提到节次没提周几，默认表示“全周”该节次。
2. 注意否定性描述，如“不排课”“请避免”“不建议”“尽量不”“不能”“不要”等，放入 unavailable_slots。
3. 【绑定时间的定义】：中括号 `[]` 仅用于"直接指定上课时间段"的情安排课程，明确要求课程必须排在这些时间段，不能更改。
   - 示例：`[周一（9-10节）、周三（9-10节）]` 表示课程必须同时排在周一9-10节和周三9-10节
   - 示例：`[周二（5-8节）、周四（5-8节）]` 表示课程必须排在周二以及周四的5-8节
   - 示例：若输入为'周二上午3-4、周四上午3-4节；或者周二下午5-6、周四下午5-6节。',那么时间应该为'[周二（3-4 节）、周四（3-4 节）];[周二（5-6 节）、周四（5-6 节）]'
   - **不要过度使用中括号**。对于"排在周二、周四下午"这样的表述，应按以下方式处理：
     - 若确实是绑定（必须这两天都上），写入 `[周二（5-8节）、周四（5-8节）]` 到 preferred_slots
     - 若是"可选择周二或周四"，按规则 4 处理，直接列出 `周二（5-8节）、周四（5-8节）` 
4. 若为可选择的时间段（不绑定、不固定），直接列出，无需括号。
   - 示例：`周二全天`，`全周（3-4节）`，`周二（5-8节）、周四（5-8节）`
5. 若绑定组和可选项同时存在，整合成列表：
   - 示例：
     {{
       "preferred_slots": ["[周五（3-4节）、周一（3-4节）]", "周二全天", "全周（3-4节）"]
     }}
6. 节次含义：上午是1-4节，下午是5-8节，晚上是9-10节。例如："周五上午不排课" -> `周五（1-4节）`；"周六" -> `周六（1-8节）`。
7. 若老师没有说明任何时间偏好，则相应字段留空列表：
   {{
     "preferred_slots": [],
     "unavailable_slots": []
   }}
8. “希望、最好、优先、尽量排、只能、固定”等正向表达，放入 preferred_slots。
9. 用课内周学时 ZXS 检查 preferred_slots 是否足够容纳一次课程：
   - 一个“周X（a-b节）”容量为 b-a+1。
   - “周X全天”容量按 10 节。
   - “全周（a-b节）”表示一周 5 个工作日均可选，容量按 5*(b-a+1)。
   - 绑定组如 [周一（1-2节）、周三（1-2节）] 的容量为组内所有节次数之和。
   - 如果 preferred_slots 明显少于 ZXS，不要删除原始偏好，仍保留 preferred_slots，同时在 warnings 中加入 "偏好时间段小于周学时: 原始偏好=...; ZXS=..."。

【教学楼/教室抽取和映射规则】
1. JXLDM 输出教学楼代码列表；JASDM 输出教室代码列表。注意输出代码，不输出名称。
2. 先从排课要求描述中识别教学楼名称、教学楼简称、教室名称或房间号，再用候选清单映射为代码。
   - 特殊映射：文本中的"管院"或"管理学院"指的是"文管楼"。
3. 【唯一映射原则】：尽量选出唯一的教室代码或教学楼代码。
   - 如果名称能唯一映射到一个候选代码，输出该代码。
   - 如果名称一对多映射（如"主楼"对应多个候选代码），无法唯一确定时，输出对应的最相关的多个候选代码，并在 warnings 中说明无法唯一映射的理由。
   - 只能使用上方候选清单中的代码；不能编造代码。
4. 如果文本明确指定教室，如"主楼B-103""西2西-321""教2-100"，优先输出 JASDM。
5. 如果文本只指定教学楼，如"主楼B""西二楼""创新港涵英楼"，输出 JXLDM，不要臆造 JASDM。
6. 如果文本只说"多媒体教室、实验室、机房、体育场地"等类型，不要填 JXLDM/JASDM。
7. 【关键区分】：老师的住处与上课地点是两个概念。
   - 若文本提到"住创新港""住在某地"但这不是上课需求，应忽略。
   - 只关注"排在...""尽量安排在...""上课地点..."等与实际上课相关的地点信息。
   - 例如"住创新港，希望课不排在早上1-2节"，"住创新港"可忽略，只提取时间偏好。
8. 如果文本中的校区和课程 SKXQ 冲突，仍按文本匹配候选，但在 warnings 说明校区可能冲突。
9. 如果没有地点信息，JXLDM/JASDM 输出 []。

【示例】
输入描述: 早上1-2节不排，尽量安排在主楼B
输出:
{{
  "preferred_slots": [],
  "unavailable_slots": ["全周（1-2节）"],
  "JXLDM": ["主楼B对应的候选代码"],
  "JASDM": [],
  "warnings": []
}}

输入描述: 周一3-4节，主楼B-103
输出:
{{
  "preferred_slots": ["周一（3-4节）"],
  "unavailable_slots": [],
  "JXLDM": [],
  "JASDM": ["主楼B-103对应的候选代码"],
  "warnings": []
}}

输入描述: 排在周二、周四下午
输出（假设可选择，非绑定）:
{{
  "preferred_slots": ["周二（5-8节）、周四（5-8节）"],
  "unavailable_slots": [],
  "JXLDM": [],
  "JASDM": [],
  "warnings": []
}}

输入描述: 住创新港，希望课不排在早上1-2节，安排在文管楼
输出:
{{
  "preferred_slots": [],
  "unavailable_slots": ["全周（1-2节）"],
  "JXLDM": ["文管楼对应的候选代码"],
  "JASDM": [],
  "warnings": ["住处信息已忽略，仅提取上课地点与时间约束"]
}}

现在请处理本条排课要求，只输出 JSON。
"""


@dataclass
class Classroom:
    JASDM: str
    JASMC: str
    JASLX: str
    JXLDM: str
    JXLMC: str
    MC: str
    SKZWS: str
    SFYXPK: str

    @property
    def seats(self) -> Optional[float]:
        try:
            return float(self.SKZWS)
        except (TypeError, ValueError):
            return None


def norm(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() == "nan":
        return ""
    if re.fullmatch(r"\d+\.0", text):
        return text[:-2]
    return text


def get_message(prompt: str) -> str:
    if not API_KEY:
        raise RuntimeError("DASHSCOPE_API_KEY is not set.")
    from openai import OpenAI

    client = OpenAI(api_key=API_KEY, base_url=BASE_URL)
    completion = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        temperature=0,
    )
    return completion.choices[0].message.content or ""


def clean_json_text(result_text: str) -> str:
    text = result_text.strip()
    if text.startswith("```json"):
        text = text[len("```json") :].strip()
    if text.startswith("```"):
        text = text[len("```") :].strip()
    if text.endswith("```"):
        text = text[:-3].strip()
    return text


def parse_json_response(result_text: str) -> Dict[str, Any]:
    return json.loads(clean_json_text(result_text))


def list_value(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [norm(v) for v in value if norm(v)]
    text = norm(value)
    return [text] if text else []


def join_list(values: Any) -> str:
    return ";".join(list_value(values))


def find_header_row(rows: List[Tuple[Any, ...]], required_codes: Iterable[str]) -> Tuple[int, Dict[str, int]]:
    required = set(required_codes)
    for row_idx, row in enumerate(rows[:5], start=1):
        header_map = {norm(value): col_idx for col_idx, value in enumerate(row, start=1) if norm(value)}
        if required & set(header_map):
            return row_idx, header_map
    row = rows[0]
    return 1, {norm(value): col_idx for col_idx, value in enumerate(row, start=1) if norm(value)}


def cell_value(ws: openpyxl.worksheet.worksheet.Worksheet, row_idx: int, header_map: Dict[str, int], code: str) -> str:
    col_idx = header_map.get(code)
    if not col_idx:
        return ""
    return norm(ws.cell(row_idx, col_idx).value)


def ensure_code_column(
    ws: openpyxl.worksheet.worksheet.Worksheet,
    header_row: int,
    header_map: Dict[str, int],
    code: str,
    display_name: str,
) -> int:
    if code in header_map:
        return header_map[code]
    col_idx = ws.max_column + 1
    if header_row == 2:
        ws.cell(1, col_idx).value = display_name
        ws.cell(2, col_idx).value = code
    else:
        ws.cell(1, col_idx).value = code
    header_map[code] = col_idx
    return col_idx


def read_classrooms(path: str) -> List[Classroom]:
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = workbook[workbook.sheetnames[0]]
    rows = list(ws.iter_rows(values_only=True))
    header = {norm(value): idx for idx, value in enumerate(rows[0]) if norm(value)}

    def get(row: Tuple[Any, ...], name: str) -> str:
        idx = header.get(name)
        return norm(row[idx]) if idx is not None and idx < len(row) else ""

    classrooms: List[Classroom] = []
    for row in rows[1:]:
        jasdm = get(row, "教室代码")
        if not jasdm or jasdm == "JASDM":
            continue
        classrooms.append(
            Classroom(
                JASDM=jasdm,
                JASMC=get(row, "教室名称"),
                JASLX=get(row, "教室类型"),
                JXLDM=get(row, "教学楼代码"),
                JXLMC=get(row, "教学楼名称"),
                MC=get(row, "校区名称"),
                SKZWS=get(row, "上课座位数"),
                SFYXPK=get(row, "是否允许排课（1，是，0，否）"),
            )
        )
    return classrooms


def compact_alias(name: str) -> str:
    aliases = {name}
    aliases.add(re.sub(r"[\s\-－（）()]+", "", name))
    aliases.add(name.replace("教学楼", "").replace("楼", ""))
    return "/".join(sorted(alias for alias in aliases if alias))


def extract_location_tokens(text: str) -> List[str]:
    tokens = set()
    for token in re.findall(r"[\u4e00-\u9fff]+[A-Za-z]+[-－]\d+[A-Za-z0-9]*", text):
        tokens.add(token)
        tokens.add(re.split(r"[-－]", token)[0])
        suffix = re.search(r"[A-Za-z]\d*[-－]\d+[A-Za-z0-9]*", token)
        if suffix:
            tokens.add(suffix.group(0))
    for token in re.findall(r"[\u4e00-\u9fffA-Za-z0-9]+[-－]\d+[A-Za-z0-9]*", text):
        if not re.search(r"[\u4e00-\u9fffA-Za-z]", token):
            continue
        tokens.add(token)
        if "-" in token or "－" in token:
            prefix = re.split(r"[-－]", token)[0]
            if len(prefix) > 1:
                tokens.add(prefix)
            suffix = re.search(r"[A-Za-z]\d*[-－]\d+[A-Za-z0-9]*", token)
            if suffix:
                tokens.add(suffix.group(0))
    for match in re.findall(r"(?:在|到|去|安排在|排在|放在|限|指定)([\u4e00-\u9fffA-Za-z0-9\-－（）()]{2,})", text):
        tokens.add(match)
    for token in re.findall(r"[\u4e00-\u9fffA-Za-z0-9（）()\-－]{2,}", text):
        if any(key in token for key in ["楼", "教室", "室", "馆", "场", "中心", "主", "东", "西", "南", "北"]):
            tokens.add(token)
    for token in re.findall(r"[A-Za-z]\d+[A-Za-z]?(?:[-－]\d+)?|\d{3,}[A-Za-z]?(?:[-－]\d+)?", text):
        tokens.add(token)
    tokens = {token for token in tokens if len(token) > 1}
    return sorted(tokens, key=len, reverse=True)


def parse_float(text: str) -> Optional[float]:
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def filter_by_course_context(classrooms: List[Classroom], campus: str, capacity: str) -> List[Classroom]:
    result = classrooms
    if campus:
        campus_filtered = [room for room in result if room.MC == campus]
        if campus_filtered:
            result = campus_filtered
    capacity_num = parse_float(capacity)
    if capacity_num is not None:
        capacity_filtered = [room for room in result if room.seats is None or room.seats >= capacity_num]
        if capacity_filtered:
            result = capacity_filtered
    allowed_filtered = [room for room in result if room.SFYXPK in {"", "1"}]
    return allowed_filtered or result


def text_matches_room(room: Classroom, tokens: List[str]) -> bool:
    values = [room.JASMC, room.JXLMC, room.JASDM, room.JXLDM]
    compact_values = [re.sub(r"[\s\-－（）()]+", "", value) for value in values]
    for token in tokens:
        compact_token = re.sub(r"[\s\-－（）()]+", "", token)
        for value, compact_value in zip(values, compact_values):
            if token and token.lower() in value.lower():
                return True
            if compact_token and compact_token.lower() in compact_value.lower():
                return True
    return False


def related_classrooms(classrooms: List[Classroom], text: str, campus: str, capacity: str) -> List[Classroom]:
    base = filter_by_course_context(classrooms, campus, capacity)
    tokens = extract_location_tokens(text)
    if not tokens:
        return base[:MAX_CLASSROOMS_IN_PROMPT]
    matched = [room for room in classrooms if text_matches_room(room, tokens)]
    if matched:
        seen = set()
        merged = []
        for room in matched + base:
            if room.JASDM in seen:
                continue
            seen.add(room.JASDM)
            merged.append(room)
        return merged[:MAX_CLASSROOMS_IN_PROMPT]
    return base[:MAX_CLASSROOMS_IN_PROMPT]


def building_context(classrooms: List[Classroom], text: str, campus: str) -> str:
    building_map: Dict[str, Classroom] = {}
    for room in classrooms:
        if room.JXLDM and room.JXLDM not in building_map:
            building_map[room.JXLDM] = room
    buildings = list(building_map.values())
    if campus:
        campus_buildings = [room for room in buildings if room.MC == campus]
        if campus_buildings:
            buildings = campus_buildings

    tokens = extract_location_tokens(text)
    if tokens:
        matched = [room for room in buildings if text_matches_room(room, tokens)]
        if matched:
            seen = set()
            buildings = [room for room in matched + buildings if not (room.JXLDM in seen or seen.add(room.JXLDM))]

    lines = [
        f"- {room.JXLMC} | {room.JXLDM} | {room.MC} | {compact_alias(room.JXLMC)}"
        for room in buildings[:MAX_BUILDINGS_IN_PROMPT]
        if room.JXLDM or room.JXLMC
    ]
    return "\n".join(lines) if lines else "- 无候选教学楼"


def classroom_context(classrooms: List[Classroom], text: str, campus: str, capacity: str) -> str:
    candidates = related_classrooms(classrooms, text, campus, capacity)
    lines = [
        f"- {room.JASMC} | {room.JASDM} | {room.JXLMC} | {room.JXLDM} | {room.MC} | {room.SKZWS} | {room.JASLX}"
        for room in candidates
    ]
    return "\n".join(lines) if lines else "- 无候选教室"


def build_prompt(row_values: Dict[str, str], classrooms: List[Classroom]) -> str:
    requirement = row_values.get(TEXT_COLUMN, "")
    weekly_hours = row_values.get(WEEKLY_HOURS_COLUMN, "")
    campus = row_values.get(CAMPUS_COLUMN, "")
    capacity = row_values.get(CAPACITY_COLUMN, "")
    return PROMPT_TEMPLATE.format(
        requirement=requirement,
        weekly_hours=weekly_hours or "未知",
        campus=campus or "未知",
        capacity=capacity or "未知",
        building_context=building_context(classrooms, requirement, campus),
        classroom_context=classroom_context(classrooms, requirement, campus, capacity),
    )


def empty_result() -> Dict[str, List[str]]:
    return {
        "preferred_slots": [],
        "unavailable_slots": [],
        "JXLDM": [],
        "JASDM": [],
        "warnings": [],
    }


def sample_non_empty_texts(input_file: str, max_samples: int = 30) -> List[Tuple[int, str]]:
    workbook = openpyxl.load_workbook(input_file, read_only=True, data_only=True)
    ws = workbook[workbook.sheetnames[0]]
    preview_rows = list(ws.iter_rows(min_row=1, max_row=min(5, ws.max_row), values_only=True))
    header_row, header_map = find_header_row(
        preview_rows,
        required_codes=[TEXT_COLUMN, WEEKLY_HOURS_COLUMN, CAMPUS_COLUMN, CAPACITY_COLUMN],
    )
    text_col = header_map.get(TEXT_COLUMN)
    if not text_col:
        return []

    samples: List[Tuple[int, str]] = []
    for row_idx in range(header_row + 1, ws.max_row + 1):
        value = norm(ws.cell(row_idx, text_col).value)
        if value:
            samples.append((row_idx, value))
            if len(samples) >= max_samples:
                break
    return samples


def process(input_file: str, classroom_file: str, output_file: str, batch_size: int = BATCH_SIZE, max_rows: Optional[int] = None) -> None:
    classrooms = read_classrooms(classroom_file)
    workbook = openpyxl.load_workbook(input_file)
    ws = workbook[workbook.sheetnames[0]]
    preview_rows = list(ws.iter_rows(min_row=1, max_row=min(5, ws.max_row), values_only=True))
    header_row, header_map = find_header_row(
        preview_rows,
        required_codes=[TEXT_COLUMN, WEEKLY_HOURS_COLUMN, CAMPUS_COLUMN, CAPACITY_COLUMN],
    )
    data_start_row = header_row + 1

    output_columns = {
        "Prefer_Time": ensure_code_column(ws, header_row, header_map, "Prefer_Time", "偏好上课时间"),
        "unavailable_Time": ensure_code_column(ws, header_row, header_map, "unavailable_Time", "避免排课时间"),
        "JXLDM": ensure_code_column(ws, header_row, header_map, "JXLDM", "教学楼代码，名称见教室资源表"),
        "JASDM": ensure_code_column(ws, header_row, header_map, "JASDM", "教室代码，名称见教室资源表"),
        NOTE_COLUMN: ensure_code_column(ws, header_row, header_map, NOTE_COLUMN, "特殊需求备注"),
    }

    last_row = ws.max_row
    if max_rows is not None and max_rows >= 1:
        last_row = min(last_row, data_start_row + max_rows - 1)

    for start in range(data_start_row, last_row + 1, batch_size):
        end = min(start + batch_size - 1, last_row)
        print(f"处理第 {start} 到 {end} 行...")
        for row_idx in range(start, end + 1):
            row_values = {
                TEXT_COLUMN: cell_value(ws, row_idx, header_map, TEXT_COLUMN),
                WEEKLY_HOURS_COLUMN: cell_value(ws, row_idx, header_map, WEEKLY_HOURS_COLUMN),
                CAMPUS_COLUMN: cell_value(ws, row_idx, header_map, CAMPUS_COLUMN),
                CAPACITY_COLUMN: cell_value(ws, row_idx, header_map, CAPACITY_COLUMN),
            }
            if not row_values[TEXT_COLUMN]:
                result = empty_result()
            else:
                prompt = build_prompt(row_values, classrooms)
                try:
                    result = parse_json_response(get_message(prompt))
                except Exception as exc:
                    print(f"[提取失败] Excel 行 {row_idx}: {row_values[TEXT_COLUMN]}\n错误: {exc}")
                    result = empty_result()
                    result["warnings"] = [f"提取失败: {exc}"]

            ws.cell(row_idx, output_columns["Prefer_Time"]).value = join_list(result.get("preferred_slots"))
            ws.cell(row_idx, output_columns["unavailable_Time"]).value = join_list(result.get("unavailable_slots"))
            ws.cell(row_idx, output_columns["JXLDM"]).value = join_list(result.get("JXLDM"))
            ws.cell(row_idx, output_columns["JASDM"]).value = join_list(result.get("JASDM"))
            ws.cell(row_idx, output_columns[NOTE_COLUMN]).value = join_list(result.get("warnings"))

    workbook.save(output_file)
    print(f"完成，已保存到 {output_file}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract special scheduling needs with classroom/building context.")
    parser.add_argument("--input", default=DEFAULT_INPUT_FILE, help="课程表 Excel，需包含 PKYQMS/ZXS/SKXQ/KRL 等列")
    parser.add_argument("--classroom", default=DEFAULT_CLASSROOM_FILE, help="教室资源 Excel")
    parser.add_argument("--output", default=DEFAULT_OUTPUT_FILE, help="输出 Excel")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    parser.add_argument("--max-rows", type=int, default=None, help="仅处理前 N 条数据行（不含表头行）")
    parser.add_argument("--sample-texts", action="store_true", help="打印前 30 个非空 PKYQMS 文本并退出")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.sample_texts:
        samples = sample_non_empty_texts(args.input, max_samples=30)
        if not samples:
            print("未找到非空 PKYQMS 文本，或无法定位 PKYQMS 列。")
        else:
            for row_idx, text in samples:
                print(f"行 {row_idx}: {text}")
        raise SystemExit(0)
    process(args.input, args.classroom, args.output, args.batch_size, args.max_rows)
