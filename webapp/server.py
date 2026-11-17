"""Local web adapter for the existing timetable repair agent.

Run on loopback only. Uploaded workbooks and audit packages remain under data/.
Set TIMETABLE_PROJECT_ROOT to the repository containing scripts/timetable_repair_agent.py
when it is not at the default location on this Mac.
"""
from __future__ import annotations

from datetime import datetime, timezone
from email.parser import BytesParser
from email.policy import default
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4
from urllib.parse import urlsplit, parse_qs


APP_ROOT = Path(__file__).resolve().parent
# The web application and engine live in the same repository.
PROJECT_ROOT = (Path(os.environ["TIMETABLE_PROJECT_ROOT"]).expanduser().resolve()
                if os.environ.get("TIMETABLE_PROJECT_ROOT") else APP_ROOT.parent)
DATA_ROOT = APP_ROOT / "data"
MAX_REQUEST_BYTES = 120 * 1024 * 1024
MAX_FILE_BYTES = 40 * 1024 * 1024
AI_SETTINGS_LOCK = threading.RLock()
RESUME_LOCK = threading.Lock()
from jobs import JobController
JOBS = JobController()

def _start_job(request, client):
    JOBS.start(request, client, PROJECT_ROOT, DATA_ROOT)


def _resource_chain_options(fields):
    """Capture the visible operation settings when accepting a resource choice."""
    from scripts.timetable_agent_loop import _options
    output={'auto_continue':True}
    if 'agent_max_moves' in fields:
        opts={'max_moved_courses':int(fields['agent_max_moves']),
              'allow_select_movable':fields.get('agent_choose_movable')=='true',
              'movable_ids':[s for s in re.split(r'[\s,，;；]+',fields.get('agent_movable_ids','')) if s],
              'locked_ids':[s for s in re.split(r'[\s,，;；]+',fields.get('agent_locked_ids','')) if s]}
        output['agent_options']=_options({'agent_options':opts})
    authorization={key:fields[key]=='true' for key in ('allow_weekend','allow_evening') if key in fields}
    if authorization:output['authorization']=authorization
    return output


def _cell_id(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _read_column(payload: bytes, column_name: str, header_row: int) -> set[str]:
    from openpyxl import load_workbook

    workbook = load_workbook(BytesIO(payload), read_only=True, data_only=True)
    try:
        sheet = workbook.active
        headers = [_cell_id(value) for value in next(sheet.iter_rows(
            min_row=header_row, max_row=header_row, values_only=True))]
        if column_name not in headers:
            raise ValueError(f"工作簿缺少 {column_name} 列，请检查表头格式。")
        column = headers.index(column_name)
        return {_cell_id(row[0]) for row in sheet.iter_rows(
            min_row=header_row + 1, min_col=column + 1, max_col=column + 1, values_only=True)
            if _cell_id(row[0])}
    finally:
        workbook.close()


def _validate_teacher_coverage(course_bytes: bytes, teacher_bytes: bytes) -> dict:
    course_teachers = _read_column(course_bytes, "JSH", 2)
    teacher_ids = _read_column(teacher_bytes, "JSH", 1)
    missing = sorted(course_teachers - teacher_ids)
    return {"course_teacher_ids": len(course_teachers), "teacher_table_ids": len(teacher_ids),
            "unmatched_teacher_ids": len(missing), "unmatched_ids_sample": missing[:10]}


def _agent_modules():
    if not (PROJECT_ROOT / "scripts" / "timetable_repair_agent.py").is_file():
        raise RuntimeError("找不到排课项目。请设置 TIMETABLE_PROJECT_ROOT 指向项目目录。")
    sys.dont_write_bytecode = True
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from scripts import timetable_repair_agent  # noqa: PLC0415
    return timetable_repair_agent


def _create_ai_client(fields):
    """Request-local credentials; never mutate environment or audit the key."""
    _agent_modules()
    import llm_api
    with AI_SETTINGS_LOCK:
        environment = {name: os.environ.get(name, "").strip() for name in
                       ("DASHSCOPE_BASE_URL", "DASHSCOPE_MODEL", "DASHSCOPE_API_KEY")}
    base_url = fields.get("dashscope_base_url", "").strip() or environment["DASHSCOPE_BASE_URL"] or llm_api.DEFAULT_BASE_URL
    model = fields.get("dashscope_model", "").strip() or environment["DASHSCOPE_MODEL"] or llm_api.DEFAULT_MODEL
    api_key = fields.get("dashscope_api_key", "").strip() or environment["DASHSCOPE_API_KEY"]
    if len(base_url) > 2048 or any(char.isspace() for char in base_url):
        raise ValueError("Base URL 格式无效。")
    parsed = urlsplit(base_url)
    if (not parsed.hostname or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment
            or (parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}))):
        raise ValueError("Base URL 需为 HTTPS 地址（本机可用 HTTP），且不能含账号、密码、查询参数或片段。")
    try:
        parsed.port
    except ValueError:
        raise ValueError("Base URL 端口无效。") from None
    if not model or len(model) > 200 or any(char.isspace() for char in model):
        raise ValueError("模型名格式无效。")
    if not api_key:
        raise ValueError("请填写 API Key，或在服务端设置 DASHSCOPE_API_KEY。")
    if len(api_key) > 4096 or any(char.isspace() for char in api_key):
        raise ValueError("API Key 格式无效。")
    return llm_api.LLMClient(api_key=api_key, base_url=base_url, model=model)


def _ai_connection_error(error):
    """Report useful categories without returning provider bodies or credentials."""
    chain, seen = [], set()
    current = error
    while current is not None and id(current) not in seen and len(chain) < 10:
        chain.append(current); seen.add(id(current))
        current = current.__cause__ or current.__context__
    if any(isinstance(item, ModuleNotFoundError) and item.name == "openai" for item in chain):
        return {"code": "missing_sdk", "message": "当前服务的 Python 未安装 openai SDK。请在同一 Python 环境运行：python3 -m pip install openai，然后重启服务。"}
    statuses = [getattr(item, "status_code", None) for item in chain]
    status = next((value for value in statuses if type(value) is int), None)
    if status in (401, 403):
        code, message = ("authentication_failed", "接口拒绝鉴权（HTTP 401）。请检查 Key 是否有效，以及它是否属于当前 Base URL 对应的平台/地域。") if status == 401 else ("permission_denied", "接口拒绝访问（HTTP 403）。请检查账号权限、模型开通状态和接口地域。")
    elif status == 404:
        code, message = "not_found", "接口或模型不存在（HTTP 404）。请核对模型名和 Base URL；Base URL 不应包含 /chat/completions。"
    elif status == 429:
        code, message = "quota_or_rate_limit", "请求受限（HTTP 429）。请检查账户额度、余额及请求频率。"
    elif status == 400:
        code, message = "invalid_model_request", "接口拒绝请求参数（HTTP 400）。请检查模型是否支持 Chat Completions 及 enable_thinking=false 参数。"
    elif status and status >= 500:
        code, message = "provider_unavailable", "模型服务端错误，请稍后重试。"
    elif any("timeout" in type(item).__name__.lower() for item in chain):
        code, message = "timeout", "连接或响应超时（单次上限 30 秒）。请检查网络、代理和 Base URL。"
    elif any(type(item).__name__ in {"APIConnectionError", "ConnectError", "SSLError"} for item in chain):
        code, message = "network_error", "无法连接模型服务。请检查域名、网络、代理和 HTTPS 证书。"
    else:
        code, message = "ai_request_failed", "模型调用失败；请检查接口配置和服务运行依赖。"
    return {"code": code, "message": message, "http_status": status}


def _apply_ai_environment(client):
    """Caller holds AI_SETTINGS_LOCK; already-running clients keep their snapshot."""
    os.environ.update(DASHSCOPE_BASE_URL=client.base_url,
                      DASHSCOPE_MODEL=client.model, DASHSCOPE_API_KEY=client.api_key)
    import llm_api
    llm_api._default_client = None  # Lazy module clients must see the new defaults.


def _save_ai_settings(fields):
    with AI_SETTINGS_LOCK:
        client = _create_ai_client(fields)
        settings = {"DASHSCOPE_BASE_URL": client.base_url,
                    "DASHSCOPE_MODEL": client.model, "DASHSCOPE_API_KEY": client.api_key}
        DATA_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(DATA_ROOT, 0o700)
        target = DATA_ROOT / "ai-settings.json"
        temporary = DATA_ROOT / (".ai-settings-" + uuid4().hex + ".tmp")
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(settings, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        _apply_ai_environment(client)


def _load_ai_settings():
    target = DATA_ROOT / "ai-settings.json"
    if not target.exists():
        return False
    with AI_SETTINGS_LOCK:
        try:
            settings = json.loads(target.read_text(encoding="utf-8"))
            names = {"DASHSCOPE_BASE_URL", "DASHSCOPE_MODEL", "DASHSCOPE_API_KEY"}
            if not isinstance(settings, dict) or set(settings) != names or any(
                    not isinstance(settings[name], str) or not settings[name].strip() for name in names):
                raise ValueError()
            client = _create_ai_client({"dashscope_base_url": settings["DASHSCOPE_BASE_URL"],
                                        "dashscope_model": settings["DASHSCOPE_MODEL"],
                                        "dashscope_api_key": settings["DASHSCOPE_API_KEY"]})
            os.chmod(target, 0o600)
            _apply_ai_environment(client)
        except (OSError, ValueError):
            raise ValueError("已保存的 AI 配置无法读取或格式无效，请重新保存配置。") from None
    return True


def _parse_multipart(content_type: str, body: bytes) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
    message = BytesParser(policy=default).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("ascii") + body
    )
    if not message.is_multipart():
        raise ValueError("请求格式错误，请重新提交表单。")
    fields: dict[str, str] = {}
    uploads: dict[str, tuple[str, bytes]] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        payload = part.get_payload(decode=True) or b""
        filename = part.get_filename()
        if filename:
            uploads[name] = (Path(filename).name, payload)
        else:
            fields[name] = payload.decode("utf-8", errors="replace")
    return fields, uploads


def _background_agent(request, client):
    from initial_schedule import write_json
    try:
        engine_request = dict(request)
        engine_request.pop("input_validation", None)
        if not engine_request.get('state_id'):
            from scripts.engine_input_contract import prepare_engine_inputs
            mapping={'courses':'course_path','rooms':'room_path','teachers':'teacher_path','classes':'class_path'}
            args=dict(engine_request.get('tool_arguments',{}))
            source={key:args[name] for key,name in mapping.items() if args.get(name)}
            if source:
                prepared,audit=prepare_engine_inputs(source,DATA_ROOT/'prepared-inputs'/request['run_id'],PROJECT_ROOT)
                args.update({mapping[key]:value for key,value in prepared.items() if key in mapping})
                engine_request['tool_arguments']=args
        result = _agent_modules().run_agent(engine_request, artifact_root=DATA_ROOT / "agent-runs", llm_client=client)
        if request.get('input_validation'):
            result['input_validation'] = request['input_validation']
            write_json(Path(result['run_dir'])/'result.json', result)
    except Exception as error:
        folder=DATA_ROOT / "agent-runs" / request["run_id"]
        folder.mkdir(parents=True,exist_ok=True)
        plan={"intent":"agent","planner":"agent","selected_tools":[]}
        write_json(folder / "plan.json",plan)
        write_json(folder / "result.json",{"ok":False,"status":"needs_attention","plan":plan,
                   "run_dir":str(folder),"error":{"message":str(error) if type(error).__name__=="InputContractError" else "任务执行异常（"+type(error).__name__+"），请查看运行记录。"}})


class Handler(BaseHTTPRequestHandler):
    server_version = "TimetableLocal/1.0"

    def log_message(self, _format: str, *_args) -> None:
        # Do not write filenames, prompts, form contents, or API details to the console.
        return

    def _send_json(self, value: dict, status: int = 200) -> None:
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def _local_origin_only(self) -> bool:
        host = self.headers.get("Host", "").lower()
        origin = self.headers.get("Origin")
        allowed_hosts = {"127.0.0.1:8765", "localhost:8765"}
        if host not in allowed_hosts:
            return False
        return origin is None or origin.lower() in {"http://127.0.0.1:8765", "http://localhost:8765"}

    def do_GET(self) -> None:  # noqa: N802
        quality_download=re.fullmatch(r'/api/quality-reports/([0-9]{8}T[0-9]{6}Z-[a-f0-9]{8})/(quality\.html|quality\.json|issues\.xlsx)',urlsplit(self.path).path)
        if quality_download:
            if not self._local_origin_only():self._send_json({'ok':False},403);return
            try:
                folder=DATA_ROOT/'quality-reports'/quality_download.group(1)
                if not (folder/'manifest.json').is_file():raise ValueError('质量报告尚未生成完成')
                name=quality_download.group(2);payload=(folder/name).read_bytes()
                mime={'quality.html':'text/html; charset=utf-8','quality.json':'application/json; charset=utf-8','issues.xlsx':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'}[name]
                self.send_response(200);self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(payload)));self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
                if name=='quality.html':self.send_header('Content-Security-Policy',"default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; frame-ancestors 'self'")
                else:self.send_header('Content-Disposition',f'attachment; filename="{name}"')
                self.end_headers();self.wfile.write(payload)
            except (OSError,ValueError):self._send_json({'ok':False,'error':{'message':'质量报告不存在或尚未完成'}},404)
            return
        if urlsplit(self.path).path == '/api/remaining-review':
            if not self._local_origin_only():
                self._send_json({'ok':False,'error':{'message':'仅允许本机页面检查数据'}},403);return
            try:
                _agent_modules()
                from remaining_review import inspect_remaining
                query=parse_qs(urlsplit(self.path).query)
                basis={k:v[0] for k,v in query.items() if k in {'state_id','run_id'}}
                _,_,review=inspect_remaining(DATA_ROOT,basis)
                self._send_json({'ok':True,'review':review})
            except (ValueError,OSError,KeyError,TypeError) as error:self._send_json({'ok':False,'error':{'message':str(error)}},400)
            return
        if urlsplit(self.path).path == '/api/pending-review':
            if not self._local_origin_only():self._send_json({'ok':False},403);return
            try:
                _agent_modules()
                from pending_review import inspect_pending
                query=parse_qs(urlsplit(self.path).query)
                _,_,view=inspect_pending(DATA_ROOT,query.get('run_id',[''])[0])
                self._send_json({'ok':True,'review':view})
            except (ValueError,OSError,KeyError,TypeError) as error:self._send_json({'ok':False,'error':{'message':str(error)}},400)
            return
        if urlsplit(self.path).path == '/api/data-check':
            if not self._local_origin_only():
                self._send_json({'ok':False,'error':{'message':'仅允许本机页面检查数据'}},403);return
            try:
                _agent_modules()
                from data_completion import inspect_basis
                query=parse_qs(urlsplit(self.path).query)
                basis={k:v[0] for k,v in query.items() if k in {'state_id','run_id'}}
                self._send_json({'ok':True,'inspection':inspect_basis(DATA_ROOT,basis)})
            except (ValueError,OSError,KeyError) as error:self._send_json({'ok':False,'error':{'message':str(error)}},400)
            return
        download=re.fullmatch(r'/api/completions/([0-9]{8}T[0-9]{6}Z-[a-f0-9]{8})/(schedule|remaining|courses|teachers)\.xlsx',urlsplit(self.path).path)
        if download:
            try:
                folder=DATA_ROOT/'agent-runs'/download.group(1)
                result=json.loads((folder/'result.json').read_text())
                if not result.get('data_completion',{}).get('saved'):raise ValueError('此运行没有已保存的补齐数据')
                names={'schedule':folder/'schedule-export.xlsx','remaining':folder/'remaining.xlsx','courses':folder/'sources/courses.xlsx','teachers':folder/'sources/teachers.xlsx'}
                payload=names[download.group(2)].read_bytes()
                self.send_response(200);self.send_header('Content-Type','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet');self.send_header('Content-Length',str(len(payload)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(payload)
            except (OSError,ValueError):self._send_json({'ok':False,'error':{'message':'补齐结果尚不可下载'}},404)
            return
        match = re.fullmatch(r"/api/jobs/([0-9]{8}T[0-9]{6}Z-[a-f0-9]{8})", urlsplit(self.path).path)
        if match:
            folder = DATA_ROOT / "agent-runs" / match.group(1)
            from scripts.timetable_progress import read_live
            try:cursor=max(0,int(parse_qs(urlsplit(self.path).query).get('after',['0'])[0]))
            except ValueError:cursor=0
            live=read_live(folder,cursor)
            if (folder / "result.json").is_file():
                self._send_json({"ok": True, "done": True, **live, "result": json.loads((folder / "result.json").read_text())})
            elif (folder / "request.json").is_file():
                progress = json.loads((folder / "progress.json").read_text()) if (folder / "progress.json").exists() else {}
                self._send_json({"ok": True, "done": False, **live, "can_cancel": JOBS.can_cancel(match.group(1))})
            elif (DATA_ROOT / "jobs" / (match.group(1)+".json")).is_file():
                self._send_json({"ok":True,"done":False,**live,"can_cancel":JOBS.can_cancel(match.group(1))})
            else:
                self._send_json({"ok": False, "error": {"message": "未找到该任务"}}, status=404)
            return
        result_match=re.fullmatch(r"/api/runs/([A-Za-z0-9][A-Za-z0-9._-]{0,79})",self.path)
        if result_match:
            _agent_modules()
            path=DATA_ROOT / "agent-runs" / result_match.group(1) / "result.json"
            if path.is_file():
                from run_binding import present_run_result
                self._send_json(present_run_result(DATA_ROOT,result_match.group(1),json.loads(path.read_text(encoding='utf-8'))))
            else:self._send_json({"ok":False,"error":{"message":"该运行尚无结果"}},status=404)
            return
        if self.path == "/api/health":
            try:
                _agent_modules()
                import llm_api
                self._send_json({"ok": True, "engine_available": True,
                                 "llm_available": bool(os.environ.get("DASHSCOPE_API_KEY", "").strip()),
                                 "ai_settings_saved": (DATA_ROOT / "ai-settings.json").is_file(),
                                 "ai_defaults": {"base_url": os.environ.get("DASHSCOPE_BASE_URL") or llm_api.DEFAULT_BASE_URL,
                                                 "model": os.environ.get("DASHSCOPE_MODEL") or llm_api.DEFAULT_MODEL}})
            except Exception as error:
                self._send_json({"ok": False, "engine_available": False,
                                 "message": str(error)}, status=503)
            return
        if self.path == "/api/runs":
            _agent_modules()
            records = []
            root = DATA_ROOT / "agent-runs"
            if root.is_dir():
                for folder in sorted(root.iterdir(), key=lambda item: item.stat().st_mtime, reverse=True):
                    if not folder.is_dir():
                        continue
                    try:
                        result = json.loads((folder / "result.json").read_text(encoding="utf-8"))
                        plan = json.loads((folder / 'plan.json').read_text(encoding='utf-8')) if (folder/'plan.json').exists() else result.get('plan',{})
                        records.append({"run_id": folder.name, "status": result.get("status"),
                                        "ok": result.get("ok"), "intent": plan.get("intent"),
                                        "tools": plan.get("selected_tools", []),
                                        "tool_calls": result.get("tool_calls", 0),
                                        "input_state_id": result.get("input_state_id"),
                                        "output_state_id": result.get("output_state_id"),
                                        "continuation_state_id": __import__("run_binding").current_run_state(DATA_ROOT,folder.name,result),
                                        "source_hashes": result.get("source_hashes", {})})
                    except (OSError, ValueError):
                        continue
                    if len(records) >= 20:
                        break
            self._send_json({"ok": True, "runs": records})
            return
        match = re.fullmatch(r"/api/states/([a-f0-9]{32})/(schedule|remaining)\.xlsx", self.path)
        if match:
            try:
                agent = _agent_modules()
                store = agent.StateStore(DATA_ROOT / "timetable-state")
                state = store.load(match.group(1))
                output = (store.export_excel(match.group(1)) if match.group(2) == "schedule" else
                          store.states / state["state_id"] / state["artifacts"]["remaining"])
                payload = output.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                self.send_header("Content-Disposition", f'attachment; filename="{match.group(2)}-{match.group(1)[:12]}.xlsx"')
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(payload)
            except (OSError, ValueError) as error:
                self._send_json({"ok": False, "error": {"message": str(error)}}, status=404)
            return
        if urlsplit(self.path).path not in ("/", "/index.html"):
            self.send_error(404)
            return
        page = (APP_ROOT / "index.html").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(page)

    def do_POST(self) -> None:  # noqa: N802
        if self.path not in {"/api/run", "/api/resume", "/api/cancel", "/api/ai/test", "/api/ai/settings", "/api/teacher-completion", "/api/acceptance/explain", "/api/hour-review", "/api/room-review", "/api/room-review-batch", "/api/room-review-repair", "/api/time-review", "/api/one-off-review", "/api/schedule/chat", "/api/chat-candidate", "/api/quality"}:
            self.send_error(404)
            return
        if not self._local_origin_only():
            self._send_json({"ok": False, "error": {"message": "仅允许本机页面提交任务。"}}, status=403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_REQUEST_BYTES:
                raise ValueError("上传总量超过限制或请求为空（单个文件上限 40 MB）。")
            body = self.rfile.read(length)
            fields, uploads = _parse_multipart(self.headers.get("Content-Type", ""), body)
            if self.path == '/api/quality':
                _agent_modules()
                from quality_review import run_quality_review
                basis=json.loads(fields.get('basis','null'))
                result=run_quality_review(DATA_ROOT,basis,fields.get('baseline_state_id') or None)
                self._send_json(result);return
            if self.path == '/api/room-review-repair':
                _agent_modules()
                from resource_movement import accepted_retry_request
                if fields.get('confirmed')!='true':raise ValueError('需要明确授权本次移动上限')
                request=accepted_retry_request(DATA_ROOT,fields.get('accepted_run_id'),int(fields.get('max_moved_courses','0')))
                request['run_id']=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-')+uuid4().hex[:8]
                request['auto_continue']=True
                _start_job(request,None);self._send_json({'ok':True,'status':'running','job_id':request['run_id']},202);return
            if self.path == '/api/room-review-batch':
                _agent_modules()
                from remaining_review import inspect_remaining
                from resource_acceptance import validate_selections
                basis=json.loads(fields.get('basis','null'));_,_,review=inspect_remaining(DATA_ROOT,basis)
                if fields.get('basis_token')!=review['basis_token']:raise ValueError('版本已改变，请重新查看对比')
                if fields.get('confirmed')!='true':raise ValueError('需要明确接受本批假设')
                selections=json.loads(fields.get('selections','null'));validate_selections(review,selections)
                rid=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-')+uuid4().hex[:8]
                request={'intent':'room_review','run_id':rid,'basis':basis,'basis_token':review['basis_token'],'decision':'accept_batch','selections':selections,**_resource_chain_options(fields)}
                _start_job(request,None);self._send_json({'ok':True,'status':'running','job_id':rid},202);return
            if self.path == '/api/room-review':
                _agent_modules()
                from remaining_review import inspect_remaining
                basis=json.loads(fields.get('basis','null'));_,_,review=inspect_remaining(DATA_ROOT,basis)
                if fields.get('basis_token')!=review['basis_token']:raise ValueError('版本已改变，请重新查看对比')
                if fields.get('decision') not in {'accept','reject'}:raise ValueError('请选择接受或不接受')
                rid=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-')+uuid4().hex[:8]
                request={'intent':'room_review','run_id':rid,'basis':basis,'basis_token':review['basis_token'],
                         'course_id':fields.get('course_id'),'room_id':fields.get('room_id'),'option_id':fields.get('option_id'),'decision':fields['decision'],**_resource_chain_options(fields)}
                _start_job(request,None);self._send_json({'ok':True,'status':'running','job_id':rid},202);return
            if self.path == '/api/one-off-review':
                _agent_modules()
                if fields.get('confirmed')!='true':raise ValueError('需要明确确认逐课授课周')
                from pending_review import inspect_pending
                source_run_id=fields.get('source_run_id');_,_,view=inspect_pending(DATA_ROOT,source_run_id)
                if fields.get('basis_token')!=view['basis_token']:raise ValueError('课表依据已改变')
                rid=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-')+uuid4().hex[:8]
                request={'intent':'one_off_review','run_id':rid,'source_run_id':source_run_id,'basis_token':view['basis_token'],'decisions':json.loads(fields.get('decisions','[]'))}
                _start_job(request,None);self._send_json({'ok':True,'status':'running','job_id':rid},202);return
            if self.path == '/api/time-review':
                _agent_modules()
                from time_review import accept_time_review
                if fields.get('confirmed')!='true':raise ValueError('需要明确确认本次选择')
                with RESUME_LOCK:
                    result=accept_time_review(DATA_ROOT,fields.get('run_id'),fields.get('proposal_hash'),json.loads(fields.get('accepted_ids','[]')))
                self._send_json(result);return
            if self.path == '/api/hour-review':
                _agent_modules()
                from hour_review import inspect_hours
                basis=json.loads(fields.get('basis','null'));context,_,report=inspect_hours(DATA_ROOT,basis)
                if fields.get('basis_token')!=report['basis_token']:raise ValueError('课表版本已改变，请重新查看差异')
                decision=fields.get('decision');ids=json.loads(fields.get('course_ids','[]'))
                if decision not in {'accept','adjust'} or not isinstance(ids,list) or any(not isinstance(v,str) for v in ids):raise ValueError('请选择接受或局部调整，并指定差异课程')
                if fields.get('confirmed')!='true':raise ValueError('需要明确提交本次选择')
                client=None
                if context['request'].get('planner')=='agent':
                    try:client=_create_ai_client(fields)
                    except ValueError:pass
                rid=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-')+uuid4().hex[:8]
                request={'intent':'hour_review','run_id':rid,'basis':basis,'basis_token':report['basis_token'],'decision':decision,'course_ids':ids,'resume':True}
                _start_job(request,client);self._send_json({'ok':True,'status':'running','job_id':rid},202);return
            if self.path == '/api/schedule/chat':
                _agent_modules()
                basis=json.loads(fields.get('basis','null'))
                if not basis:raise ValueError('请先选择当前课表版本，再询问具体课程')
                from data_completion import resolve_basis
                context=resolve_basis(DATA_ROOT,basis)
                question=fields.get('request','').strip()
                if not question or len(question)>6000:raise ValueError('请填写6000字以内的问题')
                opts={'max_rounds':int(fields.get('agent_max_rounds','6')),'max_moved_courses':int(fields.get('agent_max_moves','0')),
                    'allow_select_movable':fields.get('agent_choose_movable')=='true',
                    'movable_ids':[cid for cid in re.split(r'[\s,，;；]+',fields.get('agent_movable_ids','')) if cid],
                    'locked_ids':[cid for cid in re.split(r'[\s,，;；]+',fields.get('agent_locked_ids','')) if cid],
                    'quality_weights':{key:float(fields.get('agent_weight_'+key,default)) for key,default in {'time_preference':'1','evening':'2','weekend':'4'}.items()}}
                from scripts.timetable_agent_loop import _options
                _options({'agent_options':opts})
                client=None
                try:client=_create_ai_client(fields)
                except ValueError:pass
                rid=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-')+uuid4().hex[:8]
                request={'intent':'scheduling_chat','run_id':rid,'basis':basis,'question':question,'agent_options':opts,
                    'previous_chat_run_id':fields.get('previous_chat_run_id',''),
                    'authorization':{'allow_evening':fields.get('allow_evening')=='true','allow_weekend':fields.get('allow_weekend')=='true'}}
                _start_job(request,client);self._send_json({'ok':True,'status':'running','job_id':rid},202);return
            if self.path == '/api/chat-candidate':
                _agent_modules()
                from scheduling_chat import accept_chat_candidate
                if fields.get('confirmed')!='true':raise ValueError('需要明确确认保存此候选')
                with RESUME_LOCK:
                    result=accept_chat_candidate(DATA_ROOT,fields.get('run_id'),fields.get('candidate_id'),fields.get('candidate_hash'),fields.get('state_id'),json.loads(fields.get('accepted_unavailable_ids','[]')))
                self._send_json(result);return
            if self.path == '/api/acceptance/explain':
                _agent_modules()
                from acceptance_chat import explain_acceptance
                basis=json.loads(fields.get('basis','null'))
                self._send_json({'ok':True,'report':explain_acceptance(DATA_ROOT,basis,fields.get('request','验收什么'))})
                return
            if self.path == '/api/teacher-completion':
                _agent_modules()
                from data_completion import resolve_basis
                from initial_schedule import write_json
                basis=json.loads(fields.get('basis','{}'));answers=json.loads(fields.get('answers','[]'))
                context=resolve_basis(DATA_ROOT,basis)
                if fields.get('basis_token')!=context['basis_token']:raise ValueError('版本已改变，请重新检查')
                if not isinstance(answers,list) or not 1<=len(answers)<=100:raise ValueError('需提供1–100条明确的教师信息')
                client=None
                if context['request'].get('planner')=='agent':
                    try:client=_create_ai_client(fields)
                    except ValueError:pass  # Preserve facts even when AI is not configured.
                rid=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-')+uuid4().hex[:8]
                request={'run_id':rid,'intent':'data_completion','basis':basis,'basis_token':context['basis_token'],'answers':answers}
                _start_job(request,client)
                self._send_json({'ok':True,'status':'running','job_id':rid},202);return
            if self.path == "/api/cancel":
                result = JOBS.cancel(fields.get("run_id", "").strip(), DATA_ROOT)
                self._send_json({"ok": True, "result": result})
                return
            if self.path == "/api/resume":
                from recovery import make_resume_request
                from initial_schedule import write_json
                with RESUME_LOCK:
                    source_id = fields.get('run_id','').strip()
                    new_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-')+uuid4().hex[:8]
                    request = make_resume_request(DATA_ROOT, source_id, new_id)
                    prior = DATA_ROOT/'agent-runs'/source_id/'resumed-by.json'
                    if prior.exists():
                        old = json.loads(prior.read_text())['run_id']
                        previous_result = DATA_ROOT/'agent-runs'/old/'result.json'
                        if previous_result.exists():
                            self._send_json(json.loads(previous_result.read_text()))
                        else:
                            self._send_json({'ok':True,'status':'running','job_id':old},status=202)
                        return
                    client = _create_ai_client({}) if request['planner']=='agent' else None
                    queue=DATA_ROOT/'jobs';queue.mkdir(parents=True,exist_ok=True)
                    write_json(queue/(new_id+'.json'),{'kind':request.get('intent','agent'),'status':'queued','request':request,'resumed_from':source_id})
                    write_json(prior,{'run_id':new_id,'input_state_id':request['state_id']})
                    _start_job(request, client)
                    self._send_json({'ok':True,'status':'running','job_id':new_id},status=202)
                return
            if self.path == "/api/ai/settings":
                _save_ai_settings(fields)
                self._send_json({"ok": True, "message": "配置已保存并应用到服务端环境变量；刷新页面或重启服务后仍可使用。"})
                return
            if self.path == "/api/ai/test":
                client = _create_ai_client(fields)
                try:
                    client.chat("Reply with OK only.", "Connection test.", temperature=0,
                                max_retries=1, stream=False)
                except Exception as error:
                    self._send_json({"ok": False, "error": _ai_connection_error(error)}, status=422)
                    return
                self._send_json({"ok": True, "message": "AI 连接成功；此配置可用于 AI 多轮排课助手。"})
                return
            prompt = fields.get("request", "").strip()
            if not prompt:
                raise ValueError("请描述这次排课任务。")
            action = fields.get("action", "initial_schedule")
            if action not in {"repair", "diagnose", "legacy_reschedule", "initial_schedule"}:
                raise ValueError("任务类型无效。")
            state_id = fields.get("state_id", "").strip() or None
            _agent_modules()
            from selection import guard_selection
            guard_selection(DATA_ROOT,fields,action,state_id)
            if action == "initial_schedule" and state_id:
                raise ValueError("首次排课创建独立根版本，不能继续已有 state_id。")
            required = {"proposal"} if action == "diagnose" else ({"course", "teacher", "room", "class_path"} if action == "initial_schedule" else set() if state_id else {
                "course", "teacher", "room", "class_path", "schedule", "failed"}
            )
            optional = {'combinations', 'clustering'} & uploads.keys() if action == 'initial_schedule' else set()
            missing = required - uploads.keys()
            if missing:
                raise ValueError("缺少必需文件：" + "、".join(sorted(missing)))
            for key, (filename, payload) in uploads.items():
                allowed_suffixes = (".json",) if key == "proposal" else (".xlsx", ".xlsm")
                if key in required | optional and not filename.lower().endswith(allowed_suffixes):
                    raise ValueError(f"{key} 文件格式不符合要求。")
                if len(payload) > MAX_FILE_BYTES:
                    raise ValueError("单个文件超过 40 MB。")
            if fields.get("planner", "deterministic") not in {"deterministic", "llm", "agent"}:
                raise ValueError("规划模式无效。")
            ai_client = (_create_ai_client(fields) if action == "repair"
                         and fields.get("planner") in {"llm", "agent"} else None)
            allow_evening = fields.get("allow_evening") == "true"
            allow_weekend = fields.get("allow_weekend") == "true"
            run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:8]
            run_home = DATA_ROOT / "uploads" / run_id
            input_dir = run_home / "input"
            input_dir.mkdir(parents=True, mode=0o700)
            os.chmod(run_home, 0o700)
            os.chmod(input_dir, 0o700)
            paths = {}
            keys_to_save = set(required) | optional
            for key in keys_to_save:
                filename, payload = uploads[key]
                path = input_dir / f"{key}-{Path(filename).name}"
                path.write_bytes(payload)
                paths[key] = str(path.resolve())

            input_validation = None
            if action in {"repair", "legacy_reschedule"} and not state_id:
                input_validation = _validate_teacher_coverage(
                    uploads["course"][1], uploads["teacher"][1])
                (input_dir / "input-manifest.json").write_text(json.dumps({
                    "files": {key: {"filename": uploads[key][0],
                                    "sha256": sha256(uploads[key][1]).hexdigest(),
                                    "bytes": len(uploads[key][1])}
                              for key in sorted(required)},
                    "teacher_coverage_check": input_validation,
                    "class_registry": "上传并保留；引擎用于对照课程中的班级标识",
                }, ensure_ascii=False, indent=2), encoding="utf-8")

            if action == "diagnose":
                request = {"request": prompt, "intent": "diagnose", "planner": "deterministic",
                           "tool_arguments": {"proposal_path": paths["proposal"]}, "run_id": run_id}
            else:
                days = list(range(7 if allow_weekend else 5))
                periods = list(range(1, 12 if allow_evening else 9))
                request = {
                    "request": prompt,
                    "planner": fields.get("planner", "deterministic"),
                    "authorization": {
                        "allow_evening": allow_evening,
                        "allow_weekend": allow_weekend,
                        "publication": False,
                    },
                    "tool_arguments": {"config": {"allowed_changes": [], "days": days, "periods": periods}},
                    "run_id": run_id,
                    "state_root": str(DATA_ROOT / "timetable-state"),
                }
                if state_id:
                    request["state_id"] = state_id
                else:
                    request["tool_arguments"].update({
                        "course_path": paths["course"], "teacher_path": paths["teacher"],
                        "room_path": paths["room"], "class_path": paths["class_path"],
                        **({"schedule_path": paths["schedule"], "failed_path": paths["failed"]} if action != "initial_schedule" else {}),
                    })
                if action == "initial_schedule":
                    request["tool_arguments"].update({key + "_path": paths[key] for key in optional})
                if action == "legacy_reschedule":
                    request["intent"] = "legacy_reschedule"
                    # 原调课引擎靠挪动已排课程腾时段。默认授权移动当前版本已排的课，
                    # 否则任何一次挪动都会让整次结果因“未授权移动”而作废。
                    if state_id and fields.get("legacy_allow_move", "true") == "true":
                        from scripts.timetable_state import StateStore
                        placed = sorted({p["course_id"] for p in
                                         StateStore(DATA_ROOT / "timetable-state").load(state_id)["placements"]})
                        request["tool_arguments"]["config"].update(
                            movable_ids=placed, max_moved_courses=len(placed))
                    # 原调课的验收口径（用户确认）：偏好时间是软约束；原引擎多次失败后会放宽班级，
                    # 验收不查班级冲突。教师、教室、校区、禁排仍是硬约束。
                    from initial_schedule import LEGACY_ACCEPTANCE  # 首排后自动调课共用同一口径
                    request["tool_arguments"]["config"].update(LEGACY_ACCEPTANCE)
                    # 原始调课只由规则执行；选择 AI 时之前会被转去 AI 多轮流程。
                    request["planner"] = "deterministic"
                elif fields.get("planner", "deterministic") == "deterministic":
                    request["intent"] = "repair"
            if action == "repair" and fields.get("planner") == "agent":
                from initial_schedule import write_json
                request["planner"]="agent"
                request.pop("intent",None)
                request["agent_options"]={
                    "max_rounds":int(fields.get("agent_max_rounds","6")),
                    "process_all_remaining":fields.get("agent_process_all")=="true",
                    "max_moved_courses":int(fields.get("agent_max_moves","0")),
                    "allow_select_movable":fields.get("agent_choose_movable")=="true",
                    "movable_ids":[cid for cid in re.split(r"[\s,，;；]+",fields.get("agent_movable_ids", "")) if cid],
                    "locked_ids":[cid for cid in re.split(r"[\s,，;；]+",fields.get("agent_locked_ids", "")) if cid],
                    "quality_weights":{key:float(fields.get("agent_weight_"+key,default)) for key,default in
                                       {"time_preference":"1","evening":"2","weekend":"4"}.items()},
                    "context":fields.get("agent_context","")[:6000],
                    "resume_run_id":fields.get("agent_resume_run_id","").strip(),
                }
                from scripts.timetable_agent_loop import _options
                _options(request)
                queue=DATA_ROOT / "jobs"
                queue.mkdir(parents=True,exist_ok=True)
                write_json(queue / (run_id+".json"),{"kind":"agent","status":"queued"})
                _start_job(request, ai_client)
                self._send_json({"ok":True,"status":"running","job_id":run_id},status=202)
                return
            if action == "initial_schedule":
                from initial_schedule import run_initial, write_json
                request["intent"] = "initial_schedule"
                request["planner"] = "deterministic"
                parameters = {"auto_reschedule": True, "weeks": int(fields.get("weeks", "20")),
                              "strategy": fields.get("strategy", "balanced"), "seed": int(fields.get("seed", "2026"))}
                if not 1 <= parameters["weeks"] <= 53 or parameters["strategy"] not in {"first", "random", "balanced"}:
                    raise ValueError("学期周数或首轮策略无效。")
                request["tool_arguments"]["initial_parameters"] = parameters
                folder = DATA_ROOT / "agent-runs" / run_id
                folder.mkdir(parents=True)
                write_json(folder / "request.json", request)
                _start_job(request, None)
                self._send_json({"ok": True, "status": "running", "job_id": run_id}, status=202)
                return
            if input_validation is not None:
                request['input_validation'] = input_validation
            _start_job(request, ai_client)
            self._send_json({'ok': True, 'status': 'running', 'job_id': run_id}, status=202)

        except (ValueError, OSError) as error:
            self._send_json({"ok": False, "error": {"message": str(error)}}, status=400)
        except Exception as error:
            # Keep provider exceptions and stack traces out of the browser response.
            self._send_json({"ok": False, "error": {"message": f"服务运行失败（{type(error).__name__}）。请查看本机服务状态。"}}, status=500)


def main() -> None:
    if not PROJECT_ROOT.is_dir():
        raise SystemExit(f"找不到排课项目目录：{PROJECT_ROOT}\n请设置 TIMETABLE_PROJECT_ROOT 后重试。")
    DATA_ROOT.mkdir(exist_ok=True)
    os.chmod(DATA_ROOT, 0o700)
    try:
        _load_ai_settings()
    except ValueError:
        print("已保存的 AI 配置无效，当前使用启动环境；请从网页重新保存。")
    from recovery import recover_interrupted
    recover_interrupted(DATA_ROOT)
    server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    print("排课协同台已启动：http://127.0.0.1:8765")
    print("仅本机可访问；关闭此窗口即可停止服务。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n排课协同台已停止。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
