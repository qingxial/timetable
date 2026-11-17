"""One modern repair path for explicit single/batch resource confirmations."""
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
import json,shutil,time
from data_completion import write


def validate_selections(review,selections):
    if not isinstance(selections,list) or not selections or len(selections)>500:
        raise ValueError('请选择1–500门资源建议')
    plans={p['course_id']:p for p in review['course_plans']};seen=set();chosen=[]
    for selection in selections:
        if not isinstance(selection,dict) or set(selection)-{'course_id','option_id','room_id'}:raise ValueError('资源选择参数无效')
        cid=selection.get('course_id')
        if not isinstance(cid,str) or cid in seen or cid not in plans:raise ValueError('选择包含重复或不在当前清单中的课程')
        seen.add(cid);plan=plans[cid]
        option=next((o for o in plan['options'] if (selection.get('option_id') and o['option_id']==selection['option_id']) or
                    (not selection.get('option_id') and o['room_id']==selection.get('room_id') and not o['conditional_only'])),None)
        if option is None:raise ValueError('该教室不是当前同校区、容量足够的真实候选')
        if selection.get('room_id') and option['room_id']!=selection['room_id']:raise ValueError('选项与指定教室不一致')
        chosen.append((plan,option))
    return chosen


def review_config(context):
    from remaining_review import search_domain
    from scripts.repair_courses import RepairConfig
    domain=search_domain(context);supplied=context['request'].get('tool_arguments',{}).get('config',{})
    # Domain authorization comes from this request. Prior waivers are not reused.
    names={'single_period_starts','additional_forbidden','max_candidates_per_course','max_search_nodes'}
    cfg={k:v for k,v in supplied.items() if k in names}
    cfg.update(allowed_changes=[],time_scope='strict',days=sorted({d for d,p in domain}),periods=sorted({p for d,p in domain}),
               max_moved_courses=0,movable_ids=[],filter_fixed_occupancy=True,auto_concentrated_teaching=True,
               max_weekly_hours=8,max_blocks_per_day=1,time_limit_seconds=20)
    policy=RepairConfig(**cfg);policy.validate();return policy


def search_selected_resources(context,data,chosen,seconds=180):
    from scripts.resource_suggestions import _resource_trial
    from scripts.repair_data import Placement
    from scripts.repair_courses import placement_dict,Occupancy
    policy=review_config(context);current=data;before=Occupancy(data)
    successes=[];failures=[];changes=[];calls=[];limits={};deadline=time.monotonic()+seconds
    for index,(plan,option) in enumerate(chosen):
        cid=plan['course_id']
        if plan.get('issues'):
            failures.append({'course_id':cid,'course_name':plan['course_name'],'status':'data_issue','reason':'基础数据需核实：'+'；'.join(plan['issues'])});continue
        left=deadline-time.monotonic()
        if left<=0:
            failures.append({'course_id':cid,'course_name':plan['course_name'],'status':'search_limit','reason':'整批试排预算用尽，本课尚未完成试排'});continue
        share=min(20,max(.01,left/max(1,len(chosen)-index)))
        trial,effective=_resource_trial(current,plan,option,policy,share)
        proposal=trial['proposal'];outcome=next(r for r in proposal['results'] if r['course_id']==cid)
        calls.append({'course_id':cid,'tool':'propose_repair','engine':'modern_repair','option_id':option['option_id'],
                      'config':proposal['config'],'summary':proposal['summary'],'results':proposal['results']})
        if not proposal['summary']['inserted']:
            status=outcome['status'];diagnosis=outcome.get('diagnosis',{})
            reasons={'no_time_pattern':'当前选中教室及教师占用下，没有满足源时段和课块的完整安排',
                     'no_matching_room':'选中教室未满足容量、类型或同校区要求','search_limit':'搜索预算耗尽，尚不能判断是否可排',
                     'unsupported':'源课时或课块表达尚不支持','not_found_within_limits':'候选时段被占用，当前搜索范围未找到完整安排'}
            reason=reasons.get(status,diagnosis.get('detail') or '本次试排未找到完整安排')
            failures.append({'course_id':cid,'course_name':plan['course_name'],'status':status,'reason':reason,'diagnosis':diagnosis,'option_id':option['option_id']});continue
        # Adopt only complete successful courses, preserving all prior placements.
        current=effective
        current.placements=[Placement(r['course_id'],frozenset(r['weeks']),r['day'],tuple(r['periods']),r['room_id']) for r in proposal['placements']]
        successes.append({'plan':plan,'option':option,'correction_parameters':trial['correction_parameters'],
                          'input_corrections':trial['input_corrections'],'room_type_rule_audit':trial.get('rule_pack')})
        changes+=proposal['changes'];limits.update(proposal['config'].get('course_block_limits',{}))
    final=Occupancy(current)
    if any(final.placements[cid]!=rows for cid,rows in before.placements.items()):raise ValueError('资源补排改变了已有课程，已拒绝保存')
    config=asdict(policy);config.update(target_ids=[p['course_id'] for p,o in chosen],course_block_limits=limits)
    return {'data':current,'successes':successes,'failures':failures,'changes':changes,'config':config,'tool_calls':calls,
            'placements':[placement_dict(p) for cid in sorted(final.placements) for p in final.placements[cid]]}


def run_resource_acceptance(request,project_root,data_root):
    from remaining_review import inspect_remaining,change_room_sources
    from scripts.timetable_state import StateStore,file_hash
    from scripts.repair_data import load_dataset
    from scripts.timetable_acceptance import validate_transition,hour_plan_differences
    from scripts.timetable_agent_tools import _planning_assumptions
    from scripts.timetable_progress import emit,finish
    bulk=request.get('decision')=='accept_batch'
    folder=Path(data_root)/'agent-runs'/request['run_id'];folder.mkdir(parents=True)
    write(folder/'request.json',request)
    plan={'intent':'room_review','planner':'deterministic','selected_tools':['inspect_remaining','propose_repair','validate_timetable']}
    write(folder/'plan.json',plan)
    result={'ok':False,'status':'needs_attention','run_dir':str(folder),'plan':plan,'publication_ready':False,'data_basis':request['basis']}
    try:
        context,data,review=inspect_remaining(data_root,request['basis'])
        if request['basis_token']!=review['basis_token']:raise ValueError('课表版本已改变，请重新查看对比')
        selections=request.get('selections') if bulk else [{k:request[k] for k in ('course_id','option_id','room_id') if request.get(k)}]
        chosen=validate_selections(review,selections)
        emit(folder,'room_review','按已确认的资源方案联合试排；保留已有课程',completed=0,total=len(chosen))
        if request.get('auto_continue'):
            from resource_continuation import operation_context
            context=operation_context(context,request)
        movement=request.get('movement_limit',0)
        if movement:
            from resource_movement import search_accepted_with_movement
            if not request.get('accepted_from_run_id'):raise ValueError('移动试排必须绑定已接受的资源方案')
            if request.get('expected_state_id')!=(context.get('parent') or {}).get('state_id'):raise ValueError('课表版本已变化，请重新诊断')
            search=search_accepted_with_movement(context,data,chosen,movement,already_moved=request.get('movement_already_ids',[]))
            write(folder/'movement-diagnostics.json',search['movement_diagnostics'])
        else:
            search=search_selected_resources(context,data,chosen)
        if request.get('auto_continue') and search['failures']:
            from resource_continuation import continue_resource_search
            search=continue_resource_search(context,search,chosen,already_moved=request.get('movement_already_ids',[]),
                progress=lambda tool,ids:emit(folder,'room_review','试排未完成，自动接续：'+tool,completed=len(search['successes']),total=len(chosen)))
        moved_ids=search.get('moved_ids',[])
        plan['selected_tools']=['inspect_remaining']+list(dict.fromkeys(call['tool'] for call in search['tool_calls']))+['validate_timetable']
        write(folder/'plan.json',plan)
        write(folder/'resource-tool-calls.json',search['tool_calls']);write(folder/'resource-failures.json',search['failures'])
        success=search['successes'];count=len(success)
        ids={s['plan']['course_id'] for s in success};remaining=[cid for cid in context['candidate']['remaining_course_ids'] if cid not in ids]
        summary={'requested':len(chosen),'inserted':count,'failed':len(search['failures']),'moved':len(moved_ids),'remaining':len(remaining)}
        if request.get('auto_continue'):result['resource_followup']=search.get('continuation',{'completed':not search['failures'],'attempted_stages':[]})
        result.update(room_review={'bulk':bulk,'decision':'accepted' if not bulk else 'batch_accepted','accepted':bool(count),
                                  'cumulative_moved_ids':sorted(set(request.get('movement_already_ids',[]))|set(moved_ids)),
                                  'course_id':request.get('course_id'),'failures':search['failures'],'summary':summary},
                      final_response={'ok':True,'result':{'summary':summary}})
        if not success:
            reason='；'.join(f"{r['course_id']}：{r['reason']}（{r['status']}）" for r in search['failures'])
            if not bulk and not request.get('auto_continue'):raise ValueError('本课未验收通过：'+reason+'。本次没有增加移动授权。')
            result.update(ok=True,status='completed',output_state_id=(context.get('parent') or {}).get('state_id'))
            explanation='整批已试排，暂未排入课程；原课表未改变。'+reason
        else:
            sources,audits=change_room_sources(context,data,success,folder)
            store=StateStore(Path(data_root)/'timetable-state')
            candidate={'placements':search['placements'],'remaining_course_ids':remaining}
            store._write_schedule(folder/'schedule.xlsx',candidate['placements'])
            updated=load_dataset(sources['courses'],sources['rooms'],folder/'schedule.xlsx',sources['classes'])
            policy={**search['config'],'time_policy':'modern_repair','weeks':int(context['policy'].get('weeks',20)),
                    'accepted_hour_differences':context['policy'].get('accepted_hour_differences',[])}
            # Ceil construction is independently checked against source weekly minima.
            differences=[r for r in hour_plan_differences(updated,candidate['placements']) if r['course_id'] in ids]
            policy['accepted_hour_differences']+=differences
            validation=validate_transition(updated,context['candidate']['placements'],candidate['placements'],
                context['candidate']['remaining_course_ids'],remaining,policy,weeks=policy['weeks'])
            store._write_remaining(folder/'remaining.xlsx',remaining,updated);store._write_export(candidate,updated,folder/'schedule-export.xlsx')
            old=context['parent'];operation=deepcopy(old.get('operation',{}) if old else {})
            prior=operation.get('planning_assumptions',{}).get('capacity_assumptions',[])
            actions=[{'operation':'set_capacity_assumption','assumption_only':True,**a} for a in prior]
            for item in success:actions+=item['correction_parameters']
            assumptions=_planning_assumptions(actions)
            reviews=[]
            for item,audit in zip(success,audits):
                p,o=item['plan'],item['option']
                reviews.append({'course_id':p['course_id'],'basis':request['basis'],'basis_token':review['basis_token'],
                    'decision':'accept','accepted':True,'original':p['original'],'proposed':o['proposed'],'option_id':o['option_id'],
                    'source_revision':audit,'similarity':o.get('similarity'),'capacity_evidence':o.get('capacity_evidence'),
                    'input_corrections':item['input_corrections'],'correction_parameters':item['correction_parameters']})
            operation.update(engine='room_review',tool='accept_room_batch' if bulk else 'accept_room_proposal',parameters=policy,
                planning_assumptions=assumptions,run_id=folder.name,source_lineage=context['lineage'],
                room_review_moved_ids=sorted(set(operation.get('room_review_moved_ids',[]))|set(moved_ids)),
                room_review_request=deepcopy(context['request']),human_room_reviews=operation.get('human_room_reviews',[])+reviews,
                room_review_rejections={p['course_id']:p['decision'] for p in review['course_plans'] if (p.get('decision') or {}).get('decision')=='rejected' and p['course_id'] not in ids})
            with TemporaryDirectory(prefix='room-accept-',dir=store.root) as tmp:
                staging=StateStore(tmp)
                new=staging.create_baseline({**sources,'schedule':folder/'schedule.xlsx','failed':folder/'remaining.xlsx'},operation=operation,validation=validation)
                new['parent_state_id']=old['state_id'] if old else None
                dest=store.datasets/new['dataset_id']
                if not dest.exists():shutil.copytree(staging.datasets/new['dataset_id'],dest)
                new=store._publish(new,updated)
            write(folder/'acceptance.json',validation);candidate['validation']=validation;write(folder/'candidate.json',candidate)
            write(folder/'worker.json',{'sources':sources,'parameters':policy});write(folder/'manifest.json',{'engine_source_hashes':{k:file_hash(v) for k,v in sources.items()}})
            write(folder/'room-review.json',{'bulk':bulk,'accepted':True,'reviews':reviews,'failures':search['failures']})
            result.update(ok=True,status='completed',input_state_id=old['state_id'] if old else None,output_state_id=new['state_id'],
                          planning_assumptions=assumptions,data_basis={'state_id':new['state_id']})
            result['room_review']['audit']=reviews[0] if not bulk else {'reviews':reviews}
            explanation=f'已按选中的资源方案联合试排：排入{count}门，未排入{len(search["failures"])}门，移动{len(moved_ids)}门；通过独立验收后保存子版本。'
        if request.get('auto_continue'):
            names=' → '.join(dict.fromkeys(call['tool'] for call in search['tool_calls']))
            explanation+='\n实际调用：'+names
            if search['failures']:
                result['status']='needs_input'
                explanation+='\n已接受的资源方案继续保留。授权内未找到完整安排；缺少的授权或资料已逐门列出，无需再次接受相同方案。'
        result['agent_report']={'planning_assumptions':result.get('planning_assumptions',{}),'goal':'资源假设联合试排','questions':[],
                               'rounds':[{'tool':call['tool'],'outcome':'executed','reason':'保持已接受方案和授权时段'} for call in search['tool_calls']] if movement or request.get('auto_continue') else [],
                               'movement_diagnostics':search.get('movement_diagnostics',[]),'moved_ids':moved_ids,'changes':search['changes'],
                               'explanation':explanation,'resource_failures':search['failures']}
    except (ValueError,OSError,KeyError,TypeError,IndexError,RuntimeError) as error:
        result['error']={'message':str(error)}
        if 'room_review' in result:result['room_review']['accepted']=False
        result['final_response']={'ok':False,'error':result['error']}
        write(folder/'room-review.json',{'accepted':False,'error':str(error)})
    if result.get('ok') and result.get('output_state_id') and request.get('basis',{}).get('run_id'):
        source_run=request['basis']['run_id'];source_folder=Path(data_root)/'agent-runs'/source_run
        source_result=json.loads((source_folder/'result.json').read_text())
        if source_result.get('agent_report',{}).get('coverage'):
            result['pending_source_run_id']=source_run
            write(source_folder/'last-resource-review.json',result)
            if result.get('input_state_id') and result['output_state_id']!=result['input_state_id']:write(source_folder/'resource-confirmed.json',result)
    write(folder/'result.json',result);finish(folder,result['status'],'授权内策略已尝试；仍有课程需进一步处理' if result.get('resource_followup') and result.get('room_review',{}).get('failures') else '资源联合试排结束')
    return result
