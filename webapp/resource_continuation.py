"""Continue failed accepted resource choices using only this operation's authorization."""
from copy import deepcopy
import time


def operation_context(context,request):
    from scripts.timetable_agent_loop import _options
    result=deepcopy(context);req=result['request']
    opts=_options({'agent_options':request.get('agent_options',req.get('agent_options',{}))})
    if request.get('movement_limit'):
        opts.update(max_moved_courses=request['movement_limit'],allow_select_movable=True)
    req['agent_options']=opts
    if 'authorization' in request:
        auth=request['authorization']
        if not isinstance(auth,dict) or set(auth)-{'allow_weekend','allow_evening'} or any(type(v) is not bool for v in auth.values()):raise ValueError('时段授权无效')
        old=req.get('authorization',{});req['authorization']={**old,**auth}
        cfg=req.setdefault('tool_arguments',{}).setdefault('config',{})
        days=set(cfg.get('days',result['policy'].get('days',range(5))))
        periods=set(cfg.get('periods',result['policy'].get('periods',range(1,9))))
        if 'allow_weekend' in auth:days=(days|{5,6}) if auth['allow_weekend'] else days&set(range(5))
        if 'allow_evening' in auth:periods=(periods|{9,10,11}) if auth['allow_evening'] else periods&set(range(1,9))
        cfg.update(days=sorted(days),periods=sorted(periods))
        # These are the new operation's search bounds, not inherited waivers.
        result['policy'].update(days=sorted(days),periods=sorted(periods))
    return result


def continue_resource_search(context,search,chosen,*,seconds=120,already_moved=(),progress=None):
    from resource_acceptance import review_config
    from scripts.resource_suggestions import _resource_trial
    from scripts.repair_courses import Occupancy,placement_dict,repair,structured_college_time
    from scripts.repair_data import Placement
    from scripts.time_relaxation import repair_with_time_relaxation
    from scripts.repair_explanations import explain_conflict
    from resource_movement import search_accepted_with_movement
    deadline=time.monotonic()+seconds;current=search['data'];prepared={};failures={r['course_id']:r for r in search['failures']}
    original=set(Occupancy(current).placements)-{s['plan']['course_id'] for s in search['successes']}
    policy=review_config(context);opts=context['request']['agent_options'];cap=opts['max_moved_courses']
    allowed=(original if opts['allow_select_movable'] else set(opts['movable_ids'])&original)-set(opts['locked_ids'])
    cumulative=set(already_moved)|set(search.get('moved_ids',[]));changed={r['course_id']:r for r in search['changes']}
    cfg=deepcopy(search['config']);cfg.update(allowed_changes=['time_preference'],time_scope='weekdays',relax_course_unavailable=False)
    diagnostics=[];stages=[]
    for plan,option in chosen:
        cid=plan['course_id']
        if cid not in failures or plan.get('issues'):continue
        trial,current=_resource_trial(current,plan,option,policy,0,prepare_only=True)
        prepared[cid]={'plan':plan,'option':option,**trial}
    cfg['reviewed_soft_time_ids']=sorted({*cfg.get('reviewed_soft_time_ids',[]),*(cid for cid in prepared if structured_college_time(current.courses[cid]))})
    def pending():return [cid for cid in prepared if cid in failures]
    def emit_stage(tool,ids):
        if progress:progress(tool,ids)
    def adopt(proposal,stage):
        nonlocal current
        before=Occupancy(current)
        current.placements=[Placement(p['course_id'],frozenset(p['weeks']),p['day'],tuple(p['periods']),p['room_id']) for p in proposal['placements']]
        after=Occupancy(current);inserted=set(after.placements)-set(before.placements)
        for cid in inserted:
            if any(p.room_id!=prepared[cid]['option']['room_id'] for p in after.placements[cid]):raise ValueError('自动接续更换了已接受教室')
            search['successes'].append(prepared[cid]);failures.pop(cid,None)
        for row in proposal['changes']:
            cid=row['course_id']
            if row.get('requires_time_confirmation'):raise ValueError('未确认课程禁排结果不得自动保存')
            changed[cid]={**row,'attempt_stage':stage}
        if stage in ('evening','weekend','configured'):cfg['time_scope']='configured_days'
        for key in ('course_block_limits','time_relaxation_levels'):
            cfg.setdefault(key,{}).update(proposal['config'].get(key,{}))
        for cid in inserted:
            if stage in ('evening','weekend','configured'):cfg.get('time_relaxation_levels',{}).pop(cid,None)
        for outcome in proposal['results']:
            cid=outcome['course_id']
            if cid in failures:failures[cid].update(status=outcome['status'],diagnosis=outcome.get('diagnosis',{}))
    # Diagnosis is an actual tool call, before selecting any further repair.
    for start in range(0,len(pending()),60):
        if time.monotonic()>=deadline:break
        ids=pending()[start:start+60];args={**cfg,'target_ids':ids,'filter_fixed_occupancy':False}
        emit_stage('explain_conflict',ids)
        evidence=explain_conflict(current,ids,args,{'time_limit_seconds':min(3,max(.01,deadline-time.monotonic())),
            'max_candidates_per_course':50,'max_nodes':20000,'max_oracle_calls':20,'allow_scoped_proof':False})
        search['tool_calls'].append({'tool':'explain_conflict','target_ids':ids,'result':evidence});diagnostics.append(evidence)
    scopes=['weekdays']
    if any(p>=9 for p in cfg['periods']):scopes.append('evening')
    if any(d>=5 for d in cfg['days']):scopes.append('weekend')
    for scope in scopes:
        ids=pending()
        for start in range(0,len(ids),60):
            batch=[cid for cid in ids[start:start+60] if cid in failures];left=deadline-time.monotonic()
            if not batch or left<=0:break
            args={**deepcopy(cfg),'target_ids':batch,'filter_fixed_occupancy':True,'movable_ids':[],'max_moved_courses':0,
                'time_relaxation_levels':{},'time_limit_seconds':min(20,max(.01,left/2))}
            if scope=='evening':args.update(time_scope='configured_days',periods=[p for p in cfg['periods'] if p>=9])
            elif scope=='weekend':args.update(time_scope='configured_days',days=[d for d in cfg['days'] if d>=5])
            tool='repair_with_time_relaxation' if scope=='weekdays' else 'propose_repair'
            emit_stage(tool,batch)
            proposal=repair_with_time_relaxation(current,batch,args) if scope=='weekdays' else repair(current,batch,args)
            search['tool_calls'].append({'tool':tool,'stage':scope,'target_ids':batch,'config':proposal['config'],'summary':proposal['summary'],'results':proposal['results']})
            adopt(proposal,scope);stages.append(scope)
        if not pending():break
    # Preserve every already successful insertion. Only original, explicitly movable courses can move.
    if pending() and cap>len(cumulative) and allowed and time.monotonic()<deadline:
        ids=pending()
        for start in range(0,len(ids),60):
            batch=[cid for cid in ids[start:start+60] if cid in failures];left=deadline-time.monotonic()
            if not batch or left<=0 or cap<=len(cumulative):break
            emit_stage('optimize_local_repair',batch)
            movement=search_accepted_with_movement(context,current,[(prepared[cid]['plan'],prepared[cid]['option']) for cid in batch],cap,
                seconds=min(60,left),already_moved=cumulative,allowed_movable_ids=allowed,policy_override={**cfg,'time_relaxation_levels':{}})
            search['tool_calls']+=movement['tool_calls'];search.setdefault('movement_diagnostics',[]).extend(movement['movement_diagnostics'])
            for item in movement['successes']:search['successes'].append(item);failures.pop(item['plan']['course_id'],None)
            current=movement['data'];cumulative.update(movement['moved_ids'])
            for row in movement['changes']:changed[row['course_id']]={**row,'attempt_stage':'local_movement'}
            cfg['course_block_limits'].update(movement['config'].get('course_block_limits',{}))
            cfg['movable_ids']=sorted(set(cfg.get('movable_ids',[]))|set(movement['config']['movable_ids']))
            cfg['max_moved_courses']=max(cfg.get('max_moved_courses',0),movement['config']['max_moved_courses'])
            for failure in movement['failures']:failures[failure['course_id']]=failure
            stages.append('local_movement')
    for cid,failure in failures.items():
        if cid not in prepared:continue
        facts=[];course=current.courses[cid]
        if course.hours==0 and course.total_hours:facts.append('源周课时为0，需明确实际授课周')
        if cap==0:facts.append('当前移动上限为0，未授权挪动旧课')
        elif not allowed:facts.append('当前没有可移动的已排课程，或阻塞课程已锁定')
        if not any(d>=5 for d in cfg['days']):facts.append('未授权周末试排')
        if not any(p>=9 for p in cfg['periods']):facts.append('未授权晚间试排')
        failure['reason']='已保留所选教室并诊断，自动尝试授权内后续策略，暂未排入。'+'；'.join(facts)
        if time.monotonic()>=deadline:
            failure['status']='search_limit';failure['reason']='自动接续的搜索预算耗尽，尚有策略未完成；不能据此认定不可排。'+'；'.join(facts)
        failure['next_conditions']=facts
    final=Occupancy(current);search.update(data=current,placements=[placement_dict(p) for cid in sorted(final.placements) for p in final.placements[cid]],
        changes=list(changed.values()),failures=list(failures.values()),config=cfg,moved_ids=sorted(cumulative-set(already_moved)))
    search['continuation']={'attempted_stages':stages,'diagnostics':diagnostics,'max_moved_courses':cap,
        'days':cfg['days'],'periods':cfg['periods'],'budget_exhausted':time.monotonic()>=deadline,'completed':not failures}
    return search
