"""Version-bound quality artifacts shared by the web button and chat entry."""
from pathlib import Path
from datetime import datetime, timezone
from hashlib import sha256
from html import escape
import json
import re
from uuid import uuid4
from openpyxl import Workbook

from data_completion import resolve_basis, write
from scripts.timetable_quality import analyze_saved_timetable, STATUS_NAMES


def run_quality_review(data_root,basis,baseline_state_id=None):
    context=resolve_basis(data_root,basis)
    state=context['parent']
    if not state:raise ValueError('请先选择已保存的课表版本，再做整表质量检查')
    report=analyze_saved_timetable(Path(data_root)/'timetable-state',state['state_id'],baseline_state_id)
    rid=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-')+uuid4().hex[:8]
    folder=Path(data_root)/'quality-reports'/rid;folder.mkdir(parents=True)
    write(folder/'quality.json',report)
    export_issues(report,folder/'issues.xlsx')
    (folder/'quality.html').write_text(render_quality_report(report),encoding='utf-8')
    links={key:f'/api/quality-reports/{rid}/{filename}' for key,filename in {'report':'quality.html','data':'quality.json','details':'issues.xlsx'}.items()}
    result={'ok':True,'read_only':True,'solver_started':False,'report_id':rid,'state_id':state['state_id'],
        'summary':report['summary'],'metrics':report['metrics'],'downloads':links,
        'answer':quality_answer(report),'tool':'analyze_timetable_quality'}
    write(folder/'manifest.json',{'report_id':rid,'state_id':state['state_id'],'state_hash':state.get('state_hash'),
        'files':{p.name:sha256(p.read_bytes()).hexdigest() for p in folder.iterdir() if p.is_file()}})
    return result


def quality_answer(report):
    s=report['summary'];counts=s['course_counts_by_status'];metrics=report['metrics']
    def value(key):
        v=metrics[key]['value'];return '资料不足/不适用' if v is None else f'{v:.2f}%'
    return '\n\n'.join([f"当前版本 {report['binding']['state_id'][:12]}：有安排 {s['recorded_courses']} 门，逐周完整 {s['complete_courses']} 门，未排 {s['remaining_courses']} 门。",
        f"整表检查：违规涉及 {counts['violation']} 门，已确认调整涉及 {counts['accepted']} 门，待核实条件涉及 {counts['pending']} 门，资料不足涉及 {counts['unknown']} 门。各类可能重叠，不能简单相加。",
        f"周计划差异 {s['weekly_plan_differences']} 门，已确认 {s['confirmed_hour_differences']} 门，待核对 {s['pending_hour_differences']} 门，其中仅节次取整差异 {s['rounding_only_differences']} 门。",
        f"原偏好完全满足率 {value('preference_courses')}；晚间课节占比 {value('evening')}；周末课节占比 {value('weekend')}。",
        '完整报告包含原要求、已确认调整、实际安排和逐条证据，可筛选具体课程、教师、校区及授课周。质量检查未修改课表，也未自动确认任何差异。'])


def _cell(value):
    if isinstance(value,(list,dict,tuple,set)):value=json.dumps(value,ensure_ascii=False,default=lambda x:sorted(x) if isinstance(x,(set,frozenset)) else str(x))
    # Source text is data, including text that resembles an Excel formula.
    if isinstance(value,str) and value.startswith(('=','+','-','@')):return "'"+value
    return value


def export_issues(report,path):
    book=Workbook();book.remove(book.active)
    def sheet(title,rows,columns):
        ws=book.create_sheet(title);ws.append([label for key,label in columns]);ws.freeze_panes='A2';ws.auto_filter.ref=f'A1:{__import__("openpyxl").utils.get_column_letter(len(columns))}1'
        for row in rows:ws.append([_cell(row.get(key)) for key,label in columns])
        for col in ws.columns:ws.column_dimensions[col[0].column_letter].width=22
    sheet('概览',[{'key':k,'value':v} for k,v in report['summary'].items()],[('key','项目'),('value','结果')])
    sheet('指标公式',report['metrics'].values(),[('name','指标'),('value','百分比'),('numerator','分子'),('denominator','分母'),('definition','计算口径'),('status','数据状态')])
    sheet('全量异常与确认',report['issues'],[('course_id','教学班'),('course_name','课程'),('rule','检查项'),('status_label','状态'),('message','说明'),('evidence','确认依据'),('cells','涉及课节'),('weeks','周次'),('history','操作记录')])
    sheet('逐课安排',report['courses'],[('course_id','教学班'),('course_name','课程'),('campus','校区'),('department','院系'),('complete','逐周完整'),('statuses','检查状态'),('tags','安排标签'),('placements','实际安排'),('original_preference','原偏好'),('original_unavailable','原禁排'),('evidence','逐课依据')])
    sheet('完整性与周计划',report['completeness'],[('course_id','教学班'),('complete','完整'),('source_weekly_load','源周计划'),('effective_integer_load','有效课节计划'),('actual_weekly_load','实际周课节')])
    sheet('资源冲突',report['conflicts'],[('resource_type','资源类别'),('resource_id','资源'),('week','周'),('day','星期0基'),('period','节次'),('course_ids','冲突教学班'),('pair_count','冲突对次数')])
    sheet('教师每日负担',report['teacher_days'],[('teacher_id','教师号'),('teacher_name','教师'),('week','周'),('day','星期0基'),('teaching_cells','教学节次数'),('longest_consecutive_periods','最长连续节次'),('period_span','首末课跨度'),('gap_periods','空档节次'),('single_block_day','单块教学日')])
    sheet('教师与公平性',report['teachers'],[('teacher_id','教师号'),('teacher_name','教师'),('department','院系'),('teaching_cells','教学节次数'),('evening_percent','晚间占比'),('weekend_percent','周末占比'),('teaching_days_by_week','每周教学到校天数'),('gap_periods','空档节次数')])
    sheet('教室利用',report['room_details'],[('room_id','教室'),('room_name','名称'),('campus','校区'),('room_type','类型'),('occupied_cells','占用格数'),('actual_opening_known','真实开放已知'),('actual_utilization_percent','真实利用率'),('reference_rates','参考日历占用率')])
    sheet('容量匹配',report['capacity_details'],[('course_id','教学班'),('room_id','教室'),('original_demand','原需求'),('demand','有效需求'),('current_enrollment','当前源实际人数'),('seats','座位'),('match_percent','需求容量匹配率'),('basis','人数口径'),('requires_confirmation','依赖待确认假设')])
    sheet('教室稳定性',report['room_stability'],[('course_id','教学班'),('room_count','使用教室数'),('room_switches','换教室次数'),('building_count','教学楼数'),('rooms','教室清单')])
    sheet('操作与范围',report['history_changes'],[('state_id','版本'),('parent_state_id','父版本'),('inserted_ids','新增'),('moved_ids','移动'),('lost_ids','丢失'),('movement_limit','当次移动上限'),('violations','范围异常')])
    # Keep complete structured records available even when a rule has extra fields.
    sheet('异常完整证据',[{'course_id':r['course_id'],'detail':r} for r in report['issues']],[('course_id','教学班'),('detail','完整证据JSON')])
    book.save(path);book.close()


def render_quality_report(report):
    """Render the concise desktop report with the complete original dataset.

    Four views summarize rules, teaching and resources. Detailed evidence remains
    available in the separate JSON/Excel exports. Built-in gzip keeps the offline
    HTML small without loading fonts, scripts or data from external services.
    """
    import base64
    import gzip
    template=Path(__file__).with_name('quality_report.html').read_text(encoding='utf-8')
    data=json.dumps(report,ensure_ascii=False,separators=(',',':'),
                    default=lambda x:sorted(x) if isinstance(x,(set,frozenset)) else str(x))
    payload=base64.b64encode(gzip.compress(data.encode('utf-8'),mtime=0)).decode('ascii')
    return template.replace('__PAYLOAD__',payload)
