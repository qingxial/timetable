"""Owned process groups for cancellable local jobs; credentials travel by pipe only."""
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading

RUN_ID = re.compile(r'[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}')


def group_members(pgid):
    """Read identities only; no command arguments or credentials enter diagnostics."""
    result=subprocess.run(['ps','-axo','pid=,pgid=,stat=,uid='],capture_output=True,text=True,check=True)
    return [int(parts[0]) for line in result.stdout.splitlines()
            if len(parts:=line.split())==4 and int(parts[1])==pgid and not parts[2].startswith('Z')]


def signal_owned_group(process, sig):
    members=group_members(process.pid)
    if not members:return
    try:
        os.killpg(process.pid,sig)
    except ProcessLookupError:
        return
    except PermissionError:
        # Some macOS execution contexts reject group signalling. Recheck each
        # owned PID before falling back, and never mistake rejection for success.
        for pid in members:
            try:
                if os.getpgid(pid)==process.pid:os.kill(pid,sig)
            except ProcessLookupError:
                pass


class JobController:
    def __init__(self, runner=None):
        self.runner = Path(runner) if runner else Path(__file__).with_name("job_runner.py")
        self.lock = threading.RLock()
        self.running = {}

    def start(self, request, client, project_root, data_root):
        from initial_schedule import write_json
        rid = request['run_id']
        if not RUN_ID.fullmatch(rid):
            raise ValueError('无效的运行编号')
        data_root = Path(data_root)
        queue = data_root/'jobs'; queue.mkdir(parents=True, exist_ok=True)
        write_json(queue/(rid+'.json'), {'kind': request.get('intent','agent'),
                   'status': 'queued', 'request': request})
        credentials = None if client is None else {key: getattr(client, key) for key in
                       ('api_key', 'base_url', 'model', 'stream', 'enable_thinking')}
        payload = {'request': request, 'client': credentials,
                   'project_root': str(project_root), 'data_root': str(data_root)}
        with self.lock:
            if rid in self.running:
                raise ValueError('任务已启动')
            process = subprocess.Popen([sys.executable, str(self.runner)],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, env={**os.environ, 'PYTHONDONTWRITEBYTECODE':'1'})
            self.running[rid] = process
            try:
                process.stdin.write(json.dumps(payload).encode()); process.stdin.close()
            except Exception:
                os.killpg(process.pid, signal.SIGKILL); process.wait()
                self.running.pop(rid, None)
                raise
        threading.Thread(target=self._watch, args=(rid, process, data_root), daemon=True).start()

    def _watch(self, rid, process, data_root):
        process.wait()
        with self.lock:
            if self.running.get(rid) is not process:
                return
            self.running.pop(rid, None)
            if not (Path(data_root)/'agent-runs'/rid/'result.json').exists():
                from recovery import recover_interrupted
                recover_interrupted(data_root, run_id=rid)

    def can_cancel(self, rid):
        with self.lock:
            return rid in self.running

    def cancel(self, rid, data_root):
        from recovery import recover_interrupted, read_json
        from initial_schedule import write_json
        if not RUN_ID.fullmatch(rid):
            raise ValueError('无效的运行编号')
        folder = Path(data_root)/'agent-runs'/rid
        with self.lock:
            result = read_json(folder/'result.json')
            if result:
                return result
            process = self.running.get(rid)
            if process is None:
                raise ValueError('该任务不在当前服务的可中止任务中，请刷新运行状态')
            write_json(Path(data_root)/'jobs'/(rid+'.cancel.json'), {'run_id':rid,'status':'cancelling'})
            try:
                signal_owned_group(process, signal.SIGTERM)
                try:process.wait(timeout=2)
                except subprocess.TimeoutExpired:pass
                signal_owned_group(process, signal.SIGKILL)
                process.wait(timeout=5)
                if group_members(process.pid):
                    raise ValueError('任务仍有计算子进程存活，请再次中止')
            except (OSError,subprocess.SubprocessError) as error:
                if process.poll() is None or group_members(process.pid):
                    write_json(Path(data_root)/'jobs'/(rid+'.cancel.json'),{'run_id':rid,'status':'cancel_failed','error_type':type(error).__name__})
                    raise ValueError('系统拒绝中止当前进程，任务控制记录已保留，可重试；不会将仍运行的任务标记为已停止') from None
            self.running.pop(rid,None)
            # A normal completion may win the race; never replace its accepted result.
            result = read_json(folder/'result.json')
            if not result:
                recovered = recover_interrupted(data_root, run_id=rid)
                result = recovered[0]
                result['status'] = 'cancelled'
                result['error'] = {'message': '任务已中止。'+(
                    '已保留最后验收保存的课表版本。' if result.get('output_state_id') else
                    '中止前尚无可读取的已保存版本，需要重新提交。')+'未保存的本轮搜索结果已丢弃。'}
                result['cancellation'] = {'process_group_stopped':True, 'uncommitted_search_discarded':True}
                write_json(folder/'result.json', result)
                write_json(folder/'progress.json', {**read_json(folder/'progress.json'),'status':'cancelled','message':result['error']['message']})
            write_json(Path(data_root)/'jobs'/(rid+'.cancel.json'), {'run_id':rid,'status':result['status']})
            return result
