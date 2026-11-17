"""Explicit, hash-bound pending time choices and missing one-off teaching weeks."""
from pathlib import Path
from copy import deepcopy
import json,re,shutil
from tempfile import TemporaryDirectory
from data_completion import resolve_basis,read,write
from scripts.timetable_state import StateStore,file_hash


def inspect_pending(data_root,run_id):
    if not isinstance(run_id,str) or not re.fullmatch(r'[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}',run_id):raise ValueError('运行编号无效')
    folder=Path(data_root)/'agent-runs'/run_id;result=read(folder/'result.json');report=result.get('agent_report',{})
    context=resolve_basis(data_root,{'run_id':run_id})
    from scripts.repair_data import load_dataset
    parent=context['parent'];store=StateStore(Path(data_root)/'timetable-state')
    data=load_dataset(context['sources']['courses'],context['sources']['rooms'],store.states/parent['state_id']/parent['artifacts']['schedule'],context['sources']['classes'])
    courses=[]
    for cid,row in (report.get('coverage') or {}).get('courses',{}).items():
        if row['status']!='waiting_data' or cid not in parent['remaining_course_ids']:continue
        c=data.courses[cid]
        if c.hours>0 and any(a.get('course_id')==cid for a in parent.get('operation',{}).get('confirmed_one_off_weeks',[])):continue
        eligible=(not c.issues and c.hours==0 and c.total_hours is not None and c.total_hours==int(c.total_hours)
                  and 0<c.total_hours<=c.max_block and not c.block_template and '@' not in cid)
        courses.append({'course_id':cid,'course_name':c.name,'weekly_hours':c.hours,'total_hours':c.total_hours,
            'allowed_weeks':sorted(c.weeks),'kind':'one_off_week' if eligible else 'other_data',
            'message':f'源周课时为0、总课时为{c.total_hours:g}；请确认只在哪一周上课。' if eligible else row.get('reason','基础资料待核实')})
    time_processed=(folder/'time-confirmed.json').exists();pending=report.get('pending_time_relaxation');affected=[]
    if pending and not time_processed:
        if file_hash(folder/'pending-time.json')!=pending['proposal_hash']:raise ValueError('禁排候选依据发生变化')
        from time_review import prepare_current_time_proposal
        _,_,candidate,affected=prepare_current_time_proposal(data_root,run_id)
        pending={**pending,'parent_state_id':parent['state_id'],'courses':candidate['changes']}
    else:pending=None
    rows=(report.get('coverage') or {}).get('courses',{});remaining=set(parent['remaining_course_ids'])
    resources=sorted(cid for cid,row in rows.items() if row['status']=='waiting_resource' and cid in remaining)
    counts={'waiting_data':len(courses),'waiting_resource':len(resources),'waiting_time':sum(bool(row.get('requires_time_confirmation')) for row in (pending or {}).get('courses',[]))}
    handled=not any(counts.values()) and not affected
    return context,data,{'run_id':run_id,'state_id':parent['state_id'],'basis_token':context['basis_token'],
        'data_courses':courses,'pending_time':pending,'resource_course_ids':resources,'counts':counts,'remaining':len(remaining),
        'affected_time_ids':affected,'handled':handled,'followup_state_id':parent['state_id'],
        'message':'该轮已确认的安排已保留；剩余待办已接续，无需重新运行完整队列。'}



def run_one_off_review(request,project_root,data_root):
    from scripts.repair_workflows import apply_data_corrections
    from scripts.repair_courses import repair
    from scripts.repair_data import load_dataset
    from scripts.timetable_acceptance import validate_transition
    from scripts.timetable_progress import emit,finish
    from resource_acceptance import review_config
    folder=Path(data_root)/'agent-runs'/request['run_id'];folder.mkdir(parents=True);write(folder/'request.json',request)
    result={'ok':False,'status':'needs_attention','run_dir':str(folder),'publication_ready':False,'plan':{'intent':'one_off_review','selected_tools':['preview_data_corrections','set_one_off_week','propose_repair','validate_timetable']}}
    try:
        context,data,view=inspect_pending(data_root,request['source_run_id'])
        if not view['data_courses']:raise ValueError('本轮已没有待补充的授课周')
        request['original_request']=context['request'];write(folder/'request.json',request)
        if request.get('basis_token')!=view['basis_token']:raise ValueError('课表依据已改变，请重新读取')
        rows=request.get('decisions');allowed={x['course_id']:x for x in view['data_courses'] if x['kind']=='one_off_week'}
        if not isinstance(rows,list) or not rows:raise ValueError('请选择并确认至少一门课程的授课周')
        actions=[];seen=set()
        for row in rows:
            if not isinstance(row,dict) or set(row)!={'course_id','teaching_week'}:raise ValueError('授课周补充格式错误')
            cid=row['course_id'];week=row['teaching_week']
            if cid in seen or cid not in allowed or type(week) is not int or week not in allowed[cid]['allowed_weeks']:raise ValueError('重复课程或授课周超出源周域')
            seen.add(cid);c=data.courses[cid]
            actions.append({'operation':'set_one_off_week','course_id':cid,'expected_weekly_hours':c.hours,'expected_total_hours':c.total_hours,
                'teaching_week':week,'weeks_confirmed':True,'evidence':'网页逐课明确确认一次性授课周；源文件保持不变'})
        _,audit=apply_data_corrections(data,actions)
        sources={};source_folder=folder/'sources';source_folder.mkdir()
        for key,path in context['sources'].items():
            dest=source_folder/(key+Path(path).suffix);shutil.copy2(path,dest);sources[key]=str(dest)
        from openpyxl import load_workbook
        from scripts.teacher_time_constraints import flags
        book=load_workbook(sources['courses']);sheet=book.active;headers={c.value:c.column for c in sheet[2]}
        for action in actions:
            cid=action['course_id'];week=action['teaching_week'];matching=[n for n in range(3,sheet.max_row+1) if str(sheet.cell(n,headers['JXBID']).value).strip()==cid]
            if not matching:raise ValueError('源表缺少教学班：'+cid)
            for n in matching:
                original=sheet.cell(n,headers['RWJSZCDM']).value
                if week-1 not in flags(original):raise ValueError('选定周不在该行教师责任周内，请先核实：'+cid)
                for field in ('SKZCDM','RWJSZCDM'):
                    length=max(week,len(str(sheet.cell(n,headers[field]).value or '')))
                    sheet.cell(n,headers[field]).value=''.join('1' if i==week-1 else '0' for i in range(length))
                sheet.cell(n,headers['ZXS']).value=action['expected_total_hours']
        book.save(sources['courses']);book.close()
        store=StateStore(Path(data_root)/'timetable-state');old=context['parent'];store._write_schedule(folder/'before.xlsx',context['candidate']['placements'])
        updated=load_dataset(sources['courses'],sources['rooms'],folder/'before.xlsx',sources['classes'])
        policy=review_config(context);policy.target_ids=sorted(seen);policy.time_limit_seconds=30
        emit(folder,'one_off_review','已记录明确授课周，正在试排',completed=0,total=len(seen))
        proposal=repair(updated,sorted(seen),policy)
        placed={row['course_id'] for row in proposal['results'] if row['status']=='placed'}
        remaining=sorted(set(context['candidate']['remaining_course_ids'])-placed)
        config={**proposal['config'],'time_policy':'modern_repair','weeks':context['policy'].get('weeks',20),
            'accepted_hour_differences':context['policy'].get('accepted_hour_differences',[])}
        validation=validate_transition(updated,old['placements'],proposal['placements'],old['remaining_course_ids'],remaining,config,weeks=config['weeks'])
        store._write_schedule(folder/'schedule.xlsx',proposal['placements']);store._write_remaining(folder/'remaining.xlsx',remaining,updated)
        operation=deepcopy(old.get('operation',{}));operation.update(engine='one_off_review',tool='set_one_off_week',run_id=folder.name,
            parameters=config,confirmed_one_off_weeks=operation.get('confirmed_one_off_weeks',[])+audit,source_lineage=context['lineage'])
        with TemporaryDirectory(prefix='one-off-',dir=store.root) as tmp:
            staging=StateStore(tmp);new=staging.create_baseline({**sources,'schedule':folder/'schedule.xlsx','failed':folder/'remaining.xlsx'},operation=operation,validation=validation)
            new['parent_state_id']=old['state_id'];dest=store.datasets/new['dataset_id']
            if not dest.exists():shutil.copytree(staging.datasets/new['dataset_id'],dest)
            new=store._publish(new,updated)
        summary={'requested':len(seen),'inserted':len(placed),'moved':0,'remaining':len(remaining)}
        explanation=f'已保存{len(seen)}门明确授课周，新排入{len(placed)}门，剩余{len(remaining)}门。原源文件未修改；其余待办已接续，无需重新运行完整队列。'
        result.update(ok=True,status='completed',input_state_id=old['state_id'],output_state_id=new['state_id'],data_basis={'state_id':new['state_id']},
            pending_source_run_id=request['source_run_id'],planning_assumptions=operation.get('planning_assumptions',{}),agent_report={'summary':summary,'explanation':explanation,'rounds':[],'questions':[]},
            one_off_review={'audit':audit,'results':proposal['results']},final_response={'ok':True,'result':{'summary':summary}})
        write(folder/'acceptance.json',validation);write(Path(data_root)/'agent-runs'/request['source_run_id']/'one-off-confirmed.json',result)
    except (ValueError,OSError,KeyError,TypeError,RuntimeError) as error:result['error']={'message':str(error)}
    write(folder/'result.json',result);finish(folder,result['status'],result.get('agent_report',{}).get('explanation','授课周补充未完成'))
    return result
