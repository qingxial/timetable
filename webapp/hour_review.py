"""Explicit user decisions on measured weekly-plan differences, bound to one snapshot."""
from copy import deepcopy
from dataclasses import fields
from pathlib import Path
from tempfile import TemporaryDirectory
import json,shutil,sys
from data_completion import resolve_basis,write,read,rebuild_legacy_cache,acceptance_failure


def inspect_hours(data_root,basis):
    from scripts.timetable_state import StateStore
    from scripts.repair_data import load_dataset
    from scripts.repair_courses import RepairConfig
    from scripts.timetable_acceptance import hour_plan_differences,validate_transition
    context=resolve_basis(data_root,basis);candidate=context['candidate'];policy=context['policy']
    with TemporaryDirectory(prefix='hour-review-') as tmp:
        schedule=Path(tmp)/'schedule.xlsx';StateStore._write_schedule(None,schedule,candidate['placements'])
        data=load_dataset(context['sources']['courses'],context['sources']['rooms'],schedule,context['sources']['classes'])
    differences=hour_plan_differences(data,candidate['placements'])
    config={k:v for k,v in policy.items() if k in {f.name for f in fields(RepairConfig)}|{'time_policy'}}
    # This probes hard constraints with hour decisions deferred. It is never a
    # publish operation and must not be described as user acceptance.
    try:
        hard=validate_transition(data,[],candidate['placements'],context['scope'],candidate['remaining_course_ids'],
            {**config,'accepted_hour_differences':differences},weeks=int(policy.get('weeks',20)),full=True)
        hard.pop('accepted_hour_differences',None)
    except (ValueError,KeyError,TypeError) as e:hard=acceptance_failure(e)
    report={'basis':basis,'basis_token':context['basis_token'],'difference_count':len(differences),'differences':differences,
            'hard_validation':hard,'accepted':False,'LLXS_is_blocking':False,
            'placed':len({p['course_id'] for p in candidate['placements']}),'remaining':len(candidate['remaining_course_ids']),
            'scope':'source weekly hours and source teaching weeks; LLXS ignored for acceptance'}
    return context,data,report


def publish_snapshot(store,context,data,candidate,folder,policy,validation,decision):
    from scripts.timetable_state import StateStore,file_hash
    folder=Path(folder);store._write_schedule(folder/'schedule.xlsx',candidate['placements'])
    store._write_remaining(folder/'remaining.xlsx',candidate['remaining_course_ids'],data)
    store._write_export({'placements':candidate['placements']},data,folder/'schedule-export.xlsx')
    cache=rebuild_legacy_cache(context['sources'],candidate['placements'],folder/'legacy-cache.pkl',int(policy.get('weeks',20)))
    write(folder/'cache-audit.json',cache)
    old=context['parent'];operation=deepcopy(old.get('operation',{}) if old else {})
    operation.update(engine='hour_review',tool='accept_hour_plan',parameters=policy,hour_review=decision,
                     source_lineage=context['lineage'],run_id=folder.name)
    with TemporaryDirectory(prefix='hour-accept-',dir=store.root) as tmp:
        staging=StateStore(tmp)
        new=staging.create_baseline({**context['sources'],'schedule':folder/'schedule.xlsx','failed':folder/'remaining.xlsx'},
                                   operation=operation,validation=validation)
        new['parent_state_id']=old['state_id'] if old else None;new['validation']=validation
        dest=store.datasets/new['dataset_id']
        if dest.exists():
            if any(file_hash(dest/name)!=new['source_hashes'][k] for k,name in new['source_files'].items()):raise ValueError('源快照哈希不匹配')
        else:shutil.copytree(staging.datasets/new['dataset_id'],dest)
        new=store._publish(new,data,folder/'legacy-cache.pkl')
    write(folder/'checkpoint.json',{'run_id':folder.name,'state_id':new['state_id'],'input_state_id':old['state_id'] if old else None,'phase':'hour_review_saved'})
    return new


def adjust_selected(context,candidate,ids,folder):
    """Use the user's original scheduler only for selected courses; freeze others."""
    import utils1 as engine
    from scripts.teacher_time_constraints import placements_from_result
    import numpy as np
    objects=rebuild_legacy_cache(context['sources'],candidate['placements'],Path(folder)/'search-cache.pkl',int(context['policy'].get('weeks',20)),return_objects=True)
    policy=context['policy'];engine.set_placement_strategy(policy.get('strategy','balanced'),int(policy.get('seed',2026)))
    domain={(d,p) for d in policy.get('days',range(5)) for p in policy.get('periods',range(1,9))}
    residual=[p for p in candidate['placements'] if p['course_id'] not in ids]
    results={};failures=[]
    for cid in sorted(ids):
        cs=[c for c in objects['courses'] if str(c.JXBID)==cid]
        teacher_ids={str(c.JSH) for c in cs};room_ids={p['room_id'] for p in candidate['placements'] if p['course_id']==cid}
        related=[t for t in objects['teachers'] if str(t.JSH) in teacher_ids]+[r for r in objects['classrooms'] if str(r.JASDM) in room_ids]
        if cs and engine.check_class_conflict(cs[0]):related+=engine.get_class_instances1(cs[0],objects['classes'],objects['courses'])
        saved_courses=[(c,c.timetable.copy(),set(c.scheduled_days),c.IF_scheduled) for c in cs];saved_resources=[]
        for c in cs:c.timetable[:]='';c.scheduled_days=set();c.IF_scheduled=False
        for obj in related:
            arr=obj.timetable;mask=arr['course']==cid if arr.dtype.names else arr==cid
            saved_resources.append((obj,mask,arr[mask].copy()))
            if arr.dtype.names:arr['course'][mask]='';arr['classroom'][mask]=''
            else:arr[mask]=''
        for relax in (False,True):
            room,arr,weeks=engine.schedule_class(cid,objects['courses'],objects['teachers'],objects['classes'],objects['classrooms'],True,7,
                                                 relax_constraints=relax,time_domain=domain)
            if room and arr:
                results[cid]=placements_from_result(cid,room,arr,weeks);break
        if cid not in results:
            failures.append(cid)
            for c,table,days,scheduled in saved_courses:c.timetable[:]=table;c.scheduled_days=days;c.IF_scheduled=scheduled
            for obj,mask,cells in saved_resources:obj.timetable[mask]=cells
    after=deepcopy(candidate)
    after['placements']=[p for p in candidate['placements'] if p['course_id'] not in results]+[p for cid in sorted(results) for p in results[cid]]
    assert [p for p in after['placements'] if p['course_id'] not in ids]==residual
    return after,{'target_ids':sorted(ids),'rescheduled_ids':sorted(results),'failed_ids_original_retained':failures,
                  'other_course_arrangements_preserved':True,'remaining_unchanged':True}


def continue_original(context,state,result,folder,client):
    prior=context['request'];root=Path(folder).parent;policy=state['effective_policy']
    if prior.get('intent')=='initial_schedule' and prior.get('tool_arguments',{}).get('initial_parameters',{}).get('auto_reschedule'):
        from initial_schedule import continue_with_legacy
        from scripts.timetable_state import StateStore
        store=StateStore(state['_store_root']) if '_store_root' in state else StateStore(root.parent/'timetable-state')
        candidate=deepcopy(context['candidate']);candidate.setdefault('summary',{'placed':len({p['course_id'] for p in state['placements']}),'remaining':len(state['remaining_course_ids'])})
        # The published state is authoritative for this continuation.
        candidate.update(placements=state['placements'],remaining_course_ids=state['remaining_course_ids'])
        from scripts.repair_data import load_dataset
        files=store.dataset_files(state);data=load_dataset(files['courses'],files['rooms'],store.states/state['state_id']/state['artifacts']['schedule'],files['classes'])
        continue_with_legacy(result,store,state,data,candidate,policy,prior.get('authorization',{}),folder,{})
    else:
        from scripts.timetable_repair_agent import run_agent
        from scripts.repair_courses import RepairConfig
        allowed={f.name for f in fields(RepairConfig)}
        following={k:deepcopy(v) for k,v in prior.items() if k in {'request','intent','planner','authorization','agent_options','repair_mode','rule_pack'}}
        following.update(run_id=Path(folder).name+'-continue',state_id=state['state_id'],state_root=str(root.parent/'timetable-state'),
                         tool_arguments={'config':{'allowed_changes':[],**{k:v for k,v in prior.get('tool_arguments',{}).get('config',{}).items() if k in allowed}}})
        if following.get('planner')=='agent':
            if client is None:result['status']='needs_input';result['continuation_note']='已保存新版本；配置AI后可继续原Agent任务。';return
            following.pop('intent',None);following['agent_options']={**following.get('agent_options',{}),'resume_run_id':''}
        output=run_agent(following,root,llm_client=client)
        result['continuation']=output
        if output.get('final_response'):result['final_response']=output['final_response']
        result['output_state_id']=output.get('output_state_id') or state['state_id'];result['status']=output['status'];result['ok']=output['ok']


def run_hour_review(request,project_root,data_root,client=None):
    sys.path.insert(0,str(project_root))
    from scripts.timetable_state import StateStore,file_hash
    from scripts.timetable_acceptance import validate_transition,hour_plan_differences
    from scripts.timetable_progress import emit,finish
    folder=Path(data_root)/'agent-runs'/request['run_id'];folder.mkdir(parents=True,exist_ok=True)
    write(folder/'request.json',request);plan={'intent':'hour_review','planner':'deterministic','selected_tools':['inspect_hour_plan',request['decision']]};write(folder/'plan.json',plan)
    result={'ok':False,'status':'needs_attention','run_dir':str(folder),'plan':plan,'publication_ready':False,'tool_calls':2}
    try:
        context,data,report=inspect_hours(data_root,request['basis'])
        if request['basis_token']!=report['basis_token']:raise ValueError('课表版本已改变，请重新查看差异后再操作')
        known={d['course_id'] for d in report['differences']};ids=set(request.get('course_ids',[]))
        if not ids<=known:raise ValueError('所选课程不在当前差异清单中')
        decision=request['decision']
        if decision not in {'accept','adjust'}:raise ValueError('不支持的课时处理选择')
        if not report['hard_validation'].get('valid'):raise ValueError('仍有禁排、冲突、校区或其他约束未通过，接受课时差异不能跳过这些检查：'+report['hard_validation'].get('message','请查看验收证据'))
        candidate=deepcopy(context['candidate']);policy=deepcopy(context['policy'])
        emit(folder,'task','正在处理所选课时差异',completed=0,total=2)
        if decision=='adjust':
            if not ids:raise ValueError('请至少选择一个待局部调整的教学班')
            candidate,adjustment=adjust_selected(context,candidate,ids,folder);write(folder/'local-adjustment.json',adjustment)
            differences=hour_plan_differences(data,candidate['placements'])
            pending=[d for d in differences if d not in policy.get('accepted_hour_differences',[])]
        else:
            if ids!=known:raise ValueError('接受前请确认当前完整差异清单，不能漏掉未确认课程')
            differences=report['differences'];pending=[];policy['accepted_hour_differences']=differences
        request['original_request']=context['request'];write(folder/'request.json',request)
        operation={'decision':decision,'basis_token':report['basis_token'],'course_ids':sorted(ids),
                   'decisions':differences if decision=='accept' else [],'evidence':'explicit_human_hour_review_choice','source_lineage':context['lineage']}
        config={**policy,'accepted_hour_differences':differences if decision=='accept' else policy.get('accepted_hour_differences',[])}
        validation=validate_transition(data,[],candidate['placements'],context['scope'],candidate['remaining_course_ids'],
                                       {**config,'accepted_hour_differences':differences},weeks=int(policy.get('weeks',20)),full=True)
        write(folder/'hour-review.json',operation);write(folder/'acceptance.json',validation);candidate['validation']=validation;write(folder/'candidate.json',candidate)
        sources={k:str(v) for k,v in context['sources'].items()};write(folder/'worker.json',{'sources':sources,'parameters':policy})
        write(folder/'manifest.json',{'engine_source_hashes':{k:file_hash(v) for k,v in sources.items()},'source_lineage':context['lineage']})
        store=StateStore(Path(data_root)/'timetable-state')
        if pending:
            store._write_schedule(folder/'schedule.xlsx',candidate['placements']);store._write_remaining(folder/'remaining.xlsx',candidate['remaining_course_ids'],data)
            rebuild_legacy_cache(sources,candidate['placements'],folder/'legacy-cache.pkl',int(policy.get('weeks',20)))
            result.update(ok=True,status='needs_input',data_basis={'run_id':folder.name},hour_review={'pending':True,'difference_count':len(pending)})
            explanation='局部调整已保存为候选，其余课程保持原安排。仍有课时差异，可接受现安排或继续选择课程调整。'
        else:
            policy['accepted_hour_differences']=differences if decision=='accept' else policy.get('accepted_hour_differences',[])
            state=publish_snapshot(store,context,data,candidate,folder,policy,validation,operation)
            result.update(ok=True,status='completed',source_hashes=state['source_hashes'],output_state_id=state['state_id'],input_state_id=context['parent']['state_id'] if context['parent'] else None,
                          hour_review={'pending':False,'decision':decision,'accepted_course_ids':sorted(ids)})
            explanation='已按你的选择保存课表版本，源表课时和授课周保持原值。'
            emit(folder,'task','已保存版本，正在续接原任务',completed=1,total=2)
            if request.get('resume',True):continue_original(context,state,result,folder,client);explanation+='已按原任务设置续接。'+result.get('continuation_note','')
        result.setdefault('final_response',{'ok':True,'result':{'summary':{'placed':len({p['course_id'] for p in candidate['placements']}),'remaining':len(candidate['remaining_course_ids'])}}})
        result['agent_report']={'goal':'课时差异处理','questions':[],'explanation':explanation,'rounds':[],'summary':result.get('final_response',{}).get('result',{}).get('summary',{})}
    except (ValueError,OSError,KeyError,TypeError) as e:result.update(error={'message':str(e)})
    write(folder/'result.json',result);finish(folder,result['status'],'课时差异处理结束');return result
