"""Persistent inspect -> human answer -> source revision -> recheck -> resume loop."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import re
import shutil
from tempfile import TemporaryDirectory
from uuid import uuid4


def read(path):return json.loads(Path(path).read_text(encoding='utf-8'))

def write(path,data):
    path=Path(path);tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8');tmp.replace(path)


def resolve_basis(data_root,basis):
    from scripts.timetable_state import StateStore,file_hash
    root=Path(data_root).resolve();store=StateStore(root/'timetable-state')
    if not isinstance(basis,dict) or len(basis)!=1 or set(basis)-{'state_id','run_id'}:raise ValueError('必须指定一个课表版本或已完成运行')
    parent=None
    if 'state_id' in basis:
        parent=store.load(basis['state_id']);sources=store.dataset_files(parent)
        candidate={'placements':parent['placements'],'remaining_course_ids':parent['remaining_course_ids']}
        req={'planner':'agent','request':'补齐教师身份后继续原排课任务','agent_options':{},'authorization':parent.get('operation',{}).get('authorization',{}),'tool_arguments':{'config':parent['effective_policy']}}
        policy=store.resolve_policy(parent)
        lineage={'parent_state_id':parent['state_id'],'state_hash':parent['state_hash']}
    else:
        rid=basis['run_id']
        if not isinstance(rid,str) or not re.fullmatch(r'[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}',rid):raise ValueError('无效的运行编号')
        folder=root/'agent-runs'/rid
        if not (folder/'result.json').is_file():raise ValueError('运行尚未完成，请先等待或中止当前任务')
        result=read(folder/'result.json')
        if result.get('output_state_id'):
            from run_binding import current_run_state
            resolved=resolve_basis(root,{'state_id':current_run_state(root,rid,result)})
            if (folder/'request.json').is_file():
                original=read(folder/'request.json')
                if original.get('intent') in {'data_completion','hour_review','one_off_review'}:original=original.get('original_request',original)
                resolved['request']=original
            return resolved | {'original_basis':basis}
        spec=read(folder/'worker.json');sources={k:Path(v) for k,v in spec['sources'].items()}
        expected=read(folder/'manifest.json').get('engine_source_hashes')
        if not expected or any(file_hash(v)!=expected.get(k) for k,v in sources.items()):raise ValueError('冻结源数据哈希不匹配，拒绝补齐')
        candidate=read(folder/'candidate.json');req=read(folder/'request.json')
        if req.get('intent') in {'data_completion','hour_review'}:req=req.get('original_request',req)
        policy=spec.get('parameters',{})
        lineage={'parent_run_id':rid,'candidate_hash':file_hash(folder/'candidate.json')}
    for p in sources.values():
        if not Path(p).resolve().is_relative_to(root):raise ValueError('源数据不在本服务管理目录内')
    if not {'courses','teachers','rooms','classes'}<=sources.keys():raise ValueError('版本未绑定完整四张基础表，无法安全补齐')
    placements=candidate['placements'];remaining=candidate['remaining_course_ids'];scope={p['course_id'] for p in placements}|set(remaining)
    binding={'source_hashes':{k:file_hash(v) for k,v in sources.items()},'lineage':lineage,
             'placements':placements,'remaining_course_ids':remaining,'policy':policy}
    token=sha256(json.dumps(binding,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    return {'basis':basis,'sources':sources,'candidate':candidate,'scope':scope,'parent':parent,'policy':policy,
            'request':req,'lineage':lineage,'basis_token':token}


def inspect_basis(data_root,basis):
    from scripts.teacher_identity_completion import inspect_teacher_identities
    context=resolve_basis(data_root,basis)
    scan=inspect_teacher_identities(context['sources']['courses'],context['sources']['teachers'],context['scope'])
    return {**scan,'basis':basis,'basis_token':context['basis_token'],
            'questions':[i['question'] for i in scan['issues']],
            'note':'仅使用你明确提供的教师身份；所有问题均扫描，不受Agent单轮60门目标限制。'}


def rebuild_legacy_cache(sources,placements,output,weeks,*,return_objects=False):
    """Rebuild exclusively from the explicit JSON version and corrected frozen sources."""
    import pickle,os
    import Basic_Data as basic
    import utils1 as engine
    from scripts.teacher_time_constraints import teacher_week_map
    from scripts.repair_data import _read_table,merge_room_rows
    courses=basic.load_courses(sources['courses'],weeks,7,11);basic.initialize_virtual_class_pairs(courses)
    teachers=basic.load_teachers(sources['teachers'],weeks,7,11)
    classes=basic.load_classes(sources['classes'],weeks,7,11)
    canonical={str(r['JASDM']):r for _,r in merge_room_rows(_read_table(Path(sources['rooms']),True))}
    rooms={}
    for r in basic.load_classrooms(sources['rooms'],weeks,7,11):
        rid=str(r.JASDM).strip()
        if rid not in rooms:r.JASDM=rid;r.JASMC=canonical[rid]['JASMC'];rooms[rid]=r
    grouped={}
    for c in courses:grouped.setdefault(str(c.JXBID),[]).append(c)
    # Every responsibility week's true teacher occupancy must be representable.
    actual={};class_maps={};teacher_maps={}
    for p in placements:
        cid=p['course_id'];cs=grouped[cid];r=rooms[p['room_id']]
        if str(cs[0].SKXQ).strip()!=str(r.MC).strip():raise ValueError('课表存在跨校区安排，拒绝生成缓存')
        if cid not in teacher_maps:
            teacher_maps[cid]=teacher_week_map(cs,teachers)
            class_maps[cid]=engine.get_class_instances1(cs[0],classes,courses) if engine.check_class_conflict(cs[0]) else []
        for w in p['weeks']:
            for per in p['periods']:
                ix=(w-1,p['day'],per-1)
                for c in cs:c.timetable[ix]=r.JASDM;c.scheduled_days.add(p['day']);c.IF_scheduled=True
                if engine.check_room_conflict(cs[0]):
                    if r.timetable[ix] not in ('',cid):raise ValueError('教室占用冲突，缓存未生成')
                    r.timetable[ix]=cid
                for tid,(t,active) in teacher_maps[cid].items():
                    if w-1 in active:
                        key=(tid,*ix)
                        if key in actual and actual[key]!=cid:raise ValueError(f'教师 {tid} 在第{w}周的安排冲突：{actual[key]} / {cid}')
                        actual[key]=cid;t.timetable[ix]['course']=cid;t.timetable[ix]['classroom']=r.JASMC
                for cl in class_maps[cid]:
                    if cl.timetable[ix]['course'] not in ('',cid):raise ValueError('班级占用冲突，缓存未生成')
                    cl.timetable[ix]['course']=cid;cl.timetable[ix]['classroom']=r.JASMC
    if return_objects:return {'courses':courses,'teachers':teachers,'classrooms':list(rooms.values()),'classes':classes}
    target=Path(output);tmp=target.with_suffix('.tmp')
    try:
        with tmp.open('wb') as f:pickle.dump({'courses':courses,'teachers':teachers,'classrooms':list(rooms.values()),'classes':classes},f,protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp,target)
    finally:tmp.unlink(missing_ok=True)
    if placements:
        results=[(p['course_id'],rooms[p['room_id']],[(p['day'],min(p['periods'])-1,len(p['periods']))],[w-1 for w in p['weeks']]) for p in placements]
        engine.save_scheduling_results(results,courses,str(target.with_name('schedule-export.xlsx')))
    return {'status':'rebuilt_from_corrected_snapshot','teacher_occupied_cells':len(actual)}


def acceptance_failure(error):
    raw=str(error);issues=[]
    if raw.startswith('independent acceptance failed: '):
        try:issues=json.loads(raw.split(': ',1)[1])
        except (ValueError,TypeError):pass
    labels={'hours_or_weeks_changed':'授课周缺失或逐周课时低于源表要求','total_hours_changed':'累计课时低于源表最低要求',
            'teacher_conflict':'教师占用冲突','room_conflict':'教室占用冲突'}
    if issues:
        examples='；'.join(str(i.get('course_id',''))+'：'+labels.get(i.get('reason'),'约束未满足') for i in issues[:3])
        message='整表仍有约束未通过。示例：'+examples+'。完整证据保存在本次验收记录。'
    elif 'metadata' in raw:message='仍有课程基础数据缺失或矛盾，需要继续核实。'
    elif 'responsibility weeks' in raw:message='任课教师的责任周未覆盖课程授课周，需要核实对应源数据行。'
    else:message='整表验收未通过，修正草稿已保留；请查看本次验收记录中的具体约束。'
    return {'valid':False,'scope':'complete_root','message':message,'issues':issues,'diagnostic_detail':raw}


def run_completion(request,project_root,data_root,client=None):
    """Cancellable job. Invalid full acceptance retains a draft, never an accepted state."""
    import sys
    sys.path.insert(0,str(project_root))
    from scripts.teacher_identity_completion import apply_teacher_answers,inspect_teacher_identities
    from scripts.timetable_state import StateStore,file_hash
    from scripts.repair_data import load_dataset
    from scripts.timetable_acceptance import validate_transition
    from scripts.timetable_progress import emit,finish
    root=Path(data_root);rid=request['run_id'];folder=root/'agent-runs'/rid;folder.mkdir(parents=True,exist_ok=True)
    write(folder/'request.json',request);plan={'intent':'data_completion','planner':'agent','selected_tools':['inspect_data_quality','complete_teacher_identity','validate_timetable']};write(folder/'plan.json',plan)
    result={'ok':False,'status':'needs_attention','run_dir':str(folder),'plan':plan,'publication_ready':False,'tool_calls':0}
    try:
        context=resolve_basis(root,request['basis'])
        request['original_request']=context['request'];write(folder/'request.json',request)
        if request['basis_token']!=context['basis_token']:raise ValueError('课表或数据版本已改变，请重新检查后提交')
        scan=inspect_teacher_identities(context['sources']['courses'],context['sources']['teachers'],context['scope'])
        emit(folder,'data_completion','正在保存你补充的教师身份',completed=0,total=3)
        sources,audit=apply_teacher_answers(context['sources'],request['answers'],scan['source_hashes'],folder/'sources',context['scope'])
        audit.update(context['lineage']);write(folder/'teacher-identity-audit.json',audit)
        candidate=deepcopy(context['candidate']);write(folder/'candidate.json',candidate)
        policy=deepcopy(context['policy']);weeks=int(policy.get('weeks',20))
        if not 1<=weeks<=53:raise ValueError('学期周数无效')
        # Keep the original engine's calendar/room contract, never turn it into a modern policy implicitly.
        config={k:v for k,v in policy.items() if k in {'days','periods','time_scope','allowed_changes','time_policy','accepted_hour_differences'}}
        if policy.get('time_policy')=='test_for_school':config.update(days=list(range(7)),periods=list(range(1,12)),time_policy='test_for_school')
        store=StateStore(root/'timetable-state');schedule=folder/'schedule.xlsx';remaining=folder/'remaining.xlsx'
        store._write_schedule(schedule,candidate['placements'])
        dataset=load_dataset(sources['courses'],sources['rooms'],schedule,sources.get('classes'))
        store._write_remaining(remaining,candidate['remaining_course_ids'],dataset)
        store._write_export({'placements':candidate['placements']},dataset,folder/'schedule-export.xlsx')
        write(folder/'worker.json',{'sources':sources,'parameters':policy})
        write(folder/'manifest.json',{'engine_source_hashes':{k:file_hash(v) for k,v in sources.items()},'lineage':context['lineage']})
        after=inspect_teacher_identities(sources['courses'],sources['teachers'],context['scope']);write(folder/'data-quality.json',after)
        emit(folder,'data_completion','已保存新数据快照，正在复查整表约束',completed=1,total=3)
        validation={'valid':False,'scope':'complete_root'}
        if after['issues']:validation['message']='仍有教师身份需要补齐'
        else:
            try:
                validation=validate_transition(dataset,[],candidate['placements'],context['scope'],candidate['remaining_course_ids'],config,weeks=weeks,full=True)
            except (ValueError,KeyError,TypeError) as e:validation=acceptance_failure(e)
        write(folder/'acceptance.json',validation);candidate['validation']=validation;write(folder/'candidate.json',candidate)
        cache={'status':'not_built','reason':'仍有教师身份问题，避免生成缺教师占用的缓存'}
        if not after['issues']:
            try:cache=rebuild_legacy_cache(sources,candidate['placements'],folder/'legacy-cache.pkl',weeks)
            except (ValueError,KeyError,IndexError) as e:cache={'status':'blocked','reason':str(e)};validation={'valid':False,'scope':'complete_root','message':str(e)};write(folder/'acceptance.json',validation)
        candidate['validation']=validation;write(folder/'candidate.json',candidate)
        write(folder/'cache-audit.json',cache)
        result.update(ok=True,status='needs_input',source_hashes={k:file_hash(v) for k,v in sources.items()},tool_calls=3,
                      data_completion={'saved':True,'audit':audit,'validation':validation,'cache':cache},
                      final_response={'ok':True,'result':{'summary':{'placed':len({p['course_id'] for p in candidate['placements']}),'remaining':len(candidate['remaining_course_ids'])}}})
        questions=after['issues'];explanation='教师身份已保存并复查。'
        if validation.get('valid') and cache['status']=='rebuilt_from_corrected_snapshot':
            # Stage the new source dataset, then publish a child/root only after full acceptance.
            with TemporaryDirectory(prefix='teacher-completion-',dir=store.root) as tmp:
                staging=StateStore(tmp)
                old=context['parent'];operation=deepcopy(old.get('operation',{}) if old else {})
                operation.update(engine='data_completion',tool='complete_teacher_identity',parameters=policy,human_identity_corrections=deepcopy(operation.get('human_identity_corrections',[]))+audit['human_answers'],source_lineage=context['lineage'],run_id=rid)
                if operation.get('corrections') or operation.get('correction_history'):raise ValueError('该版本带有尚不能重放的其他更正；已保存补齐草稿，需核实后续接')
                new=staging.create_baseline({**sources,'schedule':schedule,'failed':remaining},operation=operation,validation=validation)
                new['parent_state_id']=old['state_id'] if old else None;new['validation']=validation
                dest=store.datasets/new['dataset_id']
                if dest.exists():
                    if any(file_hash(dest/name)!=new['source_hashes'][k] for k,name in new['source_files'].items()):raise ValueError('源数据快照冲突')
                else:shutil.copytree(staging.datasets/new['dataset_id'],dest)
                new=store._publish(new,dataset,folder/'legacy-cache.pkl')
            result['output_state_id']=new['state_id'];result['input_state_id']=old['state_id'] if old else None
            write(folder/'checkpoint.json',{'run_id':rid,'state_id':new['state_id'],'input_state_id':result['input_state_id'],'phase':'teacher_identity_saved'})
            result['status']='completed';explanation+='整表验收通过，已生成新课表版本。'
            # Continue only the original authorized planner, with unchanged movement grants.
            prior=context['request']
            if prior.get('planner')=='agent' and client is not None:
                following=deepcopy(prior);following.update(run_id=rid+'-continue',state_id=new['state_id'],state_root=str(store.root))
                following.pop('intent',None);from dataclasses import fields
                from scripts.repair_courses import RepairConfig
                permitted={f.name for f in fields(RepairConfig)}
                requested_config=prior.get('tool_arguments',{}).get('config',config)
                following['tool_arguments']={'config':{k:v for k,v in requested_config.items() if k in permitted}}
                following['agent_options']={**following.get('agent_options',{}),'resume_run_id':'','context':'教师身份补齐并整表复验通过。继续原任务：'+prior.get('request','')}
                from scripts.timetable_agent_loop import run_agent_loop
                continued=run_agent_loop(following,root/'agent-runs',llm_client=client)
                result['continuation']=continued;result['output_state_id']=continued.get('output_state_id') or new['state_id'];result['status']=continued['status']
                result['ok']=continued['ok']
                explanation+=('已自动续接原Agent任务。' if continued['ok'] else '原Agent续接未完成：'+(continued.get('error') or continued.get('final_response',{}).get('error') or {}).get('message','请查看续接记录'))
                questions=continued.get('agent_report',{}).get('data_questions',[])
                result['continuation_questions']=continued.get('agent_report',{}).get('questions',[])
            elif prior.get('planner')=='agent':
                result['status']='needs_input';explanation+='尚未配置可用AI连接，请配置后从此版本继续。'
            elif prior.get('intent')=='initial_schedule' and prior.get('tool_arguments',{}).get('initial_parameters',{}).get('auto_reschedule'):
                from initial_schedule import continue_with_legacy
                parameters={**policy,'weeks':weeks}
                continue_with_legacy(result,store,new,dataset,candidate,parameters,prior.get('authorization',{}),folder,{})
                explanation+='已按原任务设置续接旧引擎调课。'
            elif prior.get('intent') in {'repair','legacy_reschedule'}:
                from scripts.timetable_repair_agent import run_agent
                following={k:deepcopy(v) for k,v in prior.items() if k in {'request','intent','planner','authorization','repair_mode','rule_pack'}}
                following.update(run_id=rid+'-continue',state_id=new['state_id'],state_root=str(store.root),tool_arguments={'config':{'allowed_changes':[],**prior.get('tool_arguments',{}).get('config',{})}})
                continued=run_agent(following,root/'agent-runs')
                result['continuation']=continued;result['output_state_id']=continued.get('output_state_id') or new['state_id'];result['ok']=continued['ok'];result['status']=continued['status']
                explanation+=('已续接原任务。' if continued['ok'] else '原任务续接未完成：'+(continued.get('error') or continued.get('final_response',{}).get('error') or {}).get('message','请查看续接记录'))
        else:explanation+='整表仍未通过验收，修正草稿已保留，未启动排课或调课：'+validation.get('message',cache.get('reason',''))
        write(folder/'data-quality.json',after)
        result['agent_report']={'goal':'补齐教师信息后复查并续接原任务','questions':[i['question'] for i in questions]+result.get('continuation_questions',[]),
                                'data_questions':questions,'explanation':explanation,'rounds':[{'tool':t} for t in plan['selected_tools']],
                                'summary':result['final_response']['result']['summary']}
        (folder/'tool-calls.jsonl').write_text(''.join(json.dumps({'sequence':n,'tool':tool,'result':value},ensure_ascii=False)+'\n' for n,(tool,value) in enumerate([('inspect_data_quality',scan),('complete_teacher_identity',{'audit':audit,'cache':cache}),('validate_timetable',validation)],1)),encoding='utf-8')
        result['data_basis']={'run_id':rid}
        write(folder/'report.json',result['agent_report'])
    except (ValueError,OSError,KeyError,TypeError) as e:
        result.update(ok=False,status='needs_attention',error={'message':str(e)})
        if (folder/'teacher-identity-audit.json').is_file() and (folder/'worker.json').is_file():
            result.setdefault('data_completion',{'saved':True,'audit':read(folder/'teacher-identity-audit.json'),'validation':{'valid':False,'message':str(e)}})
            result['data_basis']={'run_id':rid}
    write(folder/'result.json',result);finish(folder,result['status'],'教师信息补齐任务结束');return result
