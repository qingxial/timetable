"""Small structured live events; never include credentials or provider responses."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from uuid import uuid4


def emit(directory, stage, message, *, completed=None, total=None, course_id=None,
         outcome=None, status='running', append=True):
    if not directory:
        return
    root=Path(directory);root.mkdir(parents=True,exist_ok=True)
    path=root/'progress.json'
    try:prior=json.loads(path.read_text())
    except (OSError,ValueError):prior={}
    event={'timestamp':datetime.now(timezone.utc).isoformat(), 'stage':stage,
           'message':str(message), 'status':status}
    if completed is not None and total is not None:
        event.update(completed=max(0,int(completed)),total=max(0,int(total)))
        event['percent']=round(100*min(event['completed'],event['total'])/event['total'],1) if event['total'] else 100.0
    if course_id is not None:event['course_id']=str(course_id)
    if outcome is not None:event['outcome']=outcome
    snapshot={**(prior if prior.get('stage')==stage else {}),**event}
    temporary=root/('progress-'+uuid4().hex+'.tmp')
    temporary.write_text(json.dumps(snapshot,ensure_ascii=False));temporary.replace(path)
    if append:
        with (root/'progress-events.jsonl').open('a',encoding='utf-8') as stream:
            stream.write(json.dumps(event,ensure_ascii=False)+'\n');stream.flush()


def coverage_progress(coverage, status):
    counts=coverage['counts'];total=len(coverage['courses']);completed=total-counts['queued']
    waiting=sum(counts[k] for k in ('waiting_resource','waiting_data','waiting_time'))
    if status=='needs_attention':message='本轮检查需处理错误，已保留成功安排'
    elif counts['queued']:message=f"本轮暂停，尚有{counts['queued']}门有检查步骤待执行"
    elif waiting:message=f"本轮自动检查已完成，{waiting}门待你确认或补充（不表示已排入）"
    else:message='本轮自动检查已完成'
    return {'completed':completed,'total':total,'percent':round(100*completed/total,1) if total else 100.0,'message':message}


def live_directory():
    return os.environ.get('TIMETABLE_PROGRESS_DIR') or None


def read_live(directory, cursor=0):
    root=Path(directory)
    try:progress=json.loads((root/'progress.json').read_text())
    except (OSError,ValueError):progress={}
    events=[];offset=0;truncated=False
    path=root/'progress-events.jsonl'
    if path.exists():
        with path.open('rb') as stream:
            size=os.fstat(stream.fileno()).st_size
            start=max(0,min(int(cursor),size))
            if size-start>256*1024:
                start=size-256*1024;truncated=True
            stream.seek(start)
            if truncated:stream.readline()
            while len(events)<1000:
                line=stream.readline()
                if not line:break
                if not line.endswith(b'\n'):
                    stream.seek(-len(line),1);break
                try:events.append(json.loads(line))
                except (ValueError,UnicodeError):pass
            offset=stream.tell()
    result_file=root/'result.json'
    if result_file.exists():
        try:
            result=json.loads(result_file.read_text());coverage=result.get('agent_report',{}).get('coverage')
            if coverage:
                progress.update(coverage_progress(coverage,result.get('status')))
                if events and events[-1].get('status')!='running':events[-1].update(coverage_progress(coverage,result.get('status')))
        except (OSError,ValueError,KeyError,TypeError):pass
    return {'progress':progress,'logs':events,'log_cursor':offset,'logs_truncated':truncated}


def finish(directory, status, message):
    root=Path(directory)
    try:prior=json.loads((root/'progress.json').read_text())
    except (OSError,ValueError):prior={}
    emit(root,prior.get('stage','task'),message,status=status,
         completed=prior.get('completed'),total=prior.get('total'))
