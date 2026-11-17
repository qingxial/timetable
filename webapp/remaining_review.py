"""Evidence-based, per-course room review against immutable timetable snapshots."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import math
import json
import shutil
from openpyxl import load_workbook
from data_completion import resolve_basis, read, write, rebuild_legacy_cache


def build_review(data, remaining, decisions=None, supplemental=None):
    from scripts.resource_suggestions import build_resource_plans
    return build_resource_plans(data,remaining,decisions,supplemental)


def search_domain(context):
    """Current candidate request narrows search without reinterpreting old arrangements."""
    import re
    req=context['request'];config=req.get('tool_arguments',{}).get('config',{})
    authorization=req.get('authorization',{})
    days=set(config.get('days',range(5))) & set(context['policy'].get('days',range(7)))
    periods=set(config.get('periods',range(1,9))) & set(context['policy'].get('periods',range(1,12)))
    if authorization.get('allow_weekend') is False:days &= set(range(5))
    if authorization.get('allow_evening') is False:periods &= set(range(1,9))
    goal=req.get('request','')
    if re.search(r'(不安排|不要|禁止|不允许|不排).{0,4}周末',goal):days &= set(range(5))
    if re.search(r'(不安排|不要|禁止|不允许|不排).{0,4}(晚间|晚上|夜间)',goal):periods &= set(range(1,9))
    if not days or not periods:raise ValueError('本次候选请求的可用时段为空')
    return {(d,p) for d in days for p in periods}


def inspect_remaining(data_root, basis):
    from scripts.repair_data import load_dataset
    from scripts.timetable_state import StateStore
    context = resolve_basis(data_root, basis)
    if 'run_id' in basis:
        run = Path(data_root)/'agent-runs'/basis['run_id']
        result = read(run/'result.json')
        intent = result.get('plan',{}).get('intent')
        if intent not in {'repair','agent','room_review','one_off_review'} or (intent not in {'room_review','one_off_review','time_review'} and result.get('plan',{}).get('planner') not in {'agent','llm'}):
            raise ValueError('逐课资源建议仅用于生成未排课程候选，不用于首排或正常调课')
    elif not (context['parent'] and context['parent'].get('operation',{}).get('engine') in {'room_review','one_off_review','time_review'}):
        raise ValueError('请从生成未排课程候选的运行记录打开逐课建议')
    with TemporaryDirectory(prefix='remaining-review-') as tmp:
        path = Path(tmp)/'schedule.xlsx'
        StateStore._write_schedule(None, path, context['candidate']['placements'])
        data = load_dataset(context['sources']['courses'], context['sources']['rooms'], path, context['sources']['classes'])
    from scripts.repair_action_plans import load_action_plan_supplemental
    from hashlib import sha256
    reference=Path(__file__).resolve().parents[1]/'排课要求和基础数据_v1.0.xlsx'
    supplemental=load_action_plan_supplemental(source_path=reference) if reference.is_file() else {}
    context['basis_token']=sha256((context['basis_token']+json.dumps(supplemental.get('source_hashes',{}),sort_keys=True)).encode()).hexdigest()
    path = Path(data_root)/'room-decisions'/(context['basis_token']+'.json')
    decisions = deepcopy((context['parent'] or {}).get('operation',{}).get('room_review_rejections',{})) | (read(path) if path.exists() else {})
    prior_request=(context['parent'] or {}).get('operation',{}).get('room_review_request')
    if prior_request:context['request']=deepcopy(prior_request)
    domain=search_domain(context)
    return context, data, {'basis': basis, 'basis_token': context['basis_token'],
                          'search_days':sorted({d for d,p in domain}), 'search_periods':sorted({p for d,p in domain}),
                          'course_plans': build_review(data, context['candidate']['remaining_course_ids'], decisions, supplemental)}


def change_room_sources(context,data,successes,folder):
    """Apply only successful, explicitly accepted choices to a fresh source snapshot."""
    target=Path(folder)/'sources';target.mkdir()
    sources={}
    for key,path in context['sources'].items():
        output=target/(key+Path(path).suffix);shutil.copy2(path,output);sources[key]=str(output)
    book=load_workbook(sources['courses']);sheet=book.active
    headers={str(c.value).strip():c.column for c in sheet[2] if c.value is not None}
    audits=[]
    for item in successes:
        cid=item['plan']['course_id'];option=item['option'];room=data.rooms[option['room_id']]
        updates={'JXLDM':room.building,'JASLXMC':room.type,'JASDM':room.id,'LSJASDM':''}
        if option['conditional_only']:updates['KRL']=option['proposed']['capacity']
        if data.courses[cid].room_type!=room.type:updates['JASLXDM']=''
        for key in updates:
            if key not in headers:
                headers[key]=sheet.max_column+1;sheet.cell(1,headers[key],key);sheet.cell(2,headers[key],key)
        rows=[]
        for row in range(3,sheet.max_row+1):
            if str(sheet.cell(row,headers['JXBID']).value).strip()==cid:
                rows.append(row)
                for key,value in updates.items():sheet.cell(row,headers[key]).value=value or None
        if not rows:raise ValueError('源表没有此教学班，不能保存决定')
        audits.append({'course_id':cid,'room_id':room.id,'source_rows':rows,'updated_fields':updates})
    book.save(sources['courses']);book.close()
    return sources,audits


def change_room_source(context,data,cid,rid,folder,option=None):
    option=option or {'room_id':rid,'conditional_only':False}
    sources,audits=change_room_sources(context,data,[{'plan':{'course_id':cid},'option':option}],folder)
    return sources,audits[0]


def try_moving_for_review(context,sources,cid,folder):
    from scripts.timetable_agent_tools import dispatch_tool
    from scripts.timetable_state import StateStore
    req=context['request'];opts=req.get('agent_options',{});cap=opts.get('max_moved_courses',0)
    if type(cap) is not int or not 0<=cap<=30:raise ValueError('移动上限必须为0–30')
    op=(context['parent'] or {}).get('operation',{})
    prior=set(op.get('room_review_moved_ids',[]));remaining=cap-len(prior)
    if remaining<=0:return None
    assigned={r['course_id'] for r in context['candidate']['placements']}
    movable=assigned if opts.get('allow_select_movable') else set(opts.get('movable_ids',[]))
    frozen={a['course_id'] for a in op.get('human_room_reviews',[])}
    movable=(movable&assigned)-set(opts.get('locked_ids',[]))-prior-frozen
    if not movable:return None
    baseline=Path(folder)/'baseline.xlsx';failed=Path(folder)/'target.xlsx'
    StateStore._write_schedule(None,baseline,context['candidate']['placements'])
    from openpyxl import Workbook
    book=Workbook();book.active.append(['教学班ID']);book.active.append([cid]);book.save(failed);book.close()
    domain=search_domain(context)
    cfg={k:v for k,v in req.get('tool_arguments',{}).get('config',{}).items() if k in {'allowed_changes','time_scope','single_period_starts','max_weekly_hours','max_blocks_per_day'}}
    cfg.update(allowed_changes=cfg.get('allowed_changes',[]),target_ids=[cid],movable_ids=sorted(movable),
               max_moved_courses=remaining,locked_ids=sorted(set(opts.get('locked_ids',[]))|prior|frozen),
               days=sorted({d for d,p in domain}),periods=sorted({p for d,p in domain}),time_limit_seconds=30)
    weights=opts.get('quality_weights',{'time_preference':1,'evening':2,'weekend':4})
    response=dispatch_tool('optimize_local_repair',{'course_path':sources['courses'],'room_path':sources['rooms'],
        'class_path':sources['classes'],'schedule_path':str(baseline),'failed_path':str(failed),'config':cfg,
        'optimizer_config':{'quality_weights':weights,'time_limit_seconds':30,'candidate_limit_per_course':100}})
    write(Path(folder)/'local-repair-tool.json',response)
    if not response.get('ok'):return None
    proposal=response['result']['proposal']
    if not any(r['course_id']==cid for r in proposal['placements']):return None
    moved=[r['course_id'] for r in proposal['changes'] if r['operation']=='move']
    return {'placements':proposal['placements'],'movable_ids':sorted(movable),'movement_limit':remaining,'moved_ids':moved}


def run_room_review(request,project_root,data_root):
    if request.get('decision') in {'accept','accept_batch'}:
        from resource_acceptance import run_resource_acceptance
        return run_resource_acceptance(request,project_root,data_root)
    from scripts.timetable_progress import finish
    folder=Path(data_root)/'agent-runs'/request['run_id'];folder.mkdir(parents=True)
    write(folder/'request.json',request)
    plan={'intent':'room_review','planner':'deterministic','selected_tools':['inspect_remaining','review_room']}
    write(folder/'plan.json',plan)
    result={'ok':False,'status':'needs_attention','run_dir':str(folder),'plan':plan,'publication_ready':False}
    try:
        context,data,review=inspect_remaining(data_root,request['basis'])
        if request['basis_token']!=review['basis_token']:raise ValueError('课表版本已改变，请重新查看对比')
        cid=request['course_id']
        if request.get('decision')!='reject' or not any(p['course_id']==cid for p in review['course_plans']):raise ValueError('请选择有效课程并接受或不接受')
        rejected=Path(data_root)/'room-decisions';rejected.mkdir(exist_ok=True)
        path=rejected/(review['basis_token']+'.json');decisions=read(path) if path.exists() else {}
        decisions[cid]={'decision':'rejected','run_id':request['run_id']};write(path,decisions)
        result.update(ok=True,status='completed',room_review={'course_id':cid,'accepted':False,'decision':'rejected'},data_basis=request['basis'],
                      agent_report={'goal':'逐课教室建议验收','questions':[],'rounds':[],'explanation':'已记录不接受；本课继续未排，已有课表保持。'})
    except (ValueError,OSError,KeyError,TypeError) as error:result['error']={'message':str(error)}
    write(folder/'result.json',result);finish(folder,result['status'],'逐课教室处理结束')
    return result
