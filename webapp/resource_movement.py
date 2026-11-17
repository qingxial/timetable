"""Continue exact accepted resource choices through occupancy diagnosis and joint repair."""
from collections import Counter
from dataclasses import asdict
from pathlib import Path
import json, re, time


def accepted_retry_request(data_root, run_id, max_moves):
    if not isinstance(run_id,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}',run_id):
        raise ValueError('已接受方案运行ID无效')
    if type(max_moves) is not int or not 1<=max_moves<=30:
        raise ValueError('本次移动上限必须为1–30门')
    folder=Path(data_root)/'agent-runs'/run_id
    previous=json.loads((folder/'request.json').read_text())
    result=json.loads((folder/'result.json').read_text())
    if previous.get('decision') not in ('accept','accept_batch') or not result.get('room_review'):
        raise ValueError('只能接续已明确接受过的资源方案')
    failed={row['course_id'] for row in result['room_review'].get('failures',[])}
    selections=previous.get('selections') or [{k:previous[k] for k in ('course_id','option_id','room_id') if previous.get(k)}]
    selections=[row for row in selections if row['course_id'] in failed]
    if not selections:raise ValueError('该次接受没有待接续的失败课程')
    from remaining_review import inspect_remaining
    from resource_acceptance import validate_selections
    context,data,review=inspect_remaining(data_root,previous['basis'])
    if previous.get('basis_token')!=review['basis_token']:
        frozen=result.get('input_state_id') or result.get('output_state_id')
        if not frozen:raise ValueError('已接受方案缺少冻结版本')
        _,_,old_review=inspect_remaining(data_root,{'state_id':frozen})
        if previous.get('basis_token')!=old_review['basis_token']:raise ValueError('已接受方案的原依据不匹配')
        old_choices=validate_selections(old_review,selections);current_choices=validate_selections(review,selections)
        if any(a[1]['proposed']!=b[1]['proposed'] for a,b in zip(old_choices,current_choices)):raise ValueError('已接受的资源内容已变化，请重新确认')
        if old_review['search_days']!=review['search_days'] or old_review['search_periods']!=review['search_periods']:raise ValueError('授权时段变化，请重新确认')
    parent=(context.get('parent') or {}).get('state_id')
    original_choices=validate_selections(review,selections)
    basis=previous['basis']
    saved_output=result.get('output_state_id');already_newer=False
    if saved_output and saved_output!=parent:
        from scripts.timetable_state import StateStore
        store=StateStore(Path(data_root)/'timetable-state');node=store.load(parent)
        while node:
            if node['state_id']==saved_output:already_newer=True;break
            node=store.load(node['parent_state_id']) if node.get('parent_state_id') else None
    if saved_output and saved_output!=parent and not already_newer:
        basis={'state_id':result['output_state_id']}
        context,data,new_review=inspect_remaining(data_root,basis)
        refreshed=validate_selections(new_review,selections)
        if any(old[1]['proposed']!=new[1]['proposed'] for old,new in zip(original_choices,refreshed)):
            raise ValueError('已接受的资源内容发生变化，请重新确认')
        if new_review['search_days']!=review['search_days'] or new_review['search_periods']!=review['search_periods']:
            raise ValueError('后续版本时段变化，请重新确认时间授权')
        review=new_review;parent=result['output_state_id']
    return {'intent':'room_review','basis':basis,'basis_token':review['basis_token'],
            'decision':'accept_batch','selections':selections,'movement_limit':max_moves,
            'accepted_from_run_id':run_id,'expected_state_id':parent,
            'movement_already_ids':result['room_review'].get('cumulative_moved_ids',[])}


def search_accepted_with_movement(context,data,chosen,max_moves,seconds=120,already_moved=(),allowed_movable_ids=None,policy_override=None):
    from resource_acceptance import review_config
    from scripts.resource_suggestions import _resource_trial
    from scripts.repair_courses import Occupancy, CandidateFactory, placement_dict, keys_for
    from scripts.repair_explanations import explain_conflict
    from scripts.repair_optimizer import optimize_local
    from scripts.concentrated_teaching import assess_concentrated_course
    if type(max_moves) is not int or not 1<=max_moves<=30:raise ValueError('移动上限必须为1–30')
    policy=review_config(context)
    if policy_override is not None:policy=type(policy)(**policy_override)
    policy.filter_fixed_occupancy=False
    policy.max_candidates_per_course=min(100,max(20,policy.max_candidates_per_course))
    before=Occupancy(data);effective=data;prepared=[];deadline=time.monotonic()+seconds
    for plan,option in chosen:
        if plan.get('issues'):raise ValueError('本课尚有基础数据问题：'+plan['course_id'])
        trial,effective=_resource_trial(effective,plan,option,policy,0,prepare_only=True)
        prepared.append({'plan':plan,'option':option,**trial})
    ids=[p['course_id'] for p,o in chosen]
    config=asdict(policy);config.update(target_ids=ids,filter_fixed_occupancy=False)
    limits={}
    for cid in ids:
        assessment=assess_concentrated_course(effective.courses[cid],policy)
        if assessment['eligible']:limits[cid]=assessment['after']
    config['course_block_limits']=limits
    locks=set(context['request'].get('agent_options',{}).get('locked_ids',[]))
    # An explicit movement retry authorizes choosing actual blockers, while preserving locks.
    allowed=set(before.placements)-locks-set(already_moved)
    if allowed_movable_ids is not None:allowed &= set(allowed_movable_ids)
    remaining_moves=max(0,max_moves-len(set(already_moved)))
    frequencies=Counter();diagnostics=[];calls=[];pattern_choices=[]
    factory=CandidateFactory(effective,type(policy)(**config),min(deadline,time.monotonic()+min(20,seconds/3)))
    occupancy=Occupancy(effective)
    for cid in ids:
        remaining=deadline-time.monotonic()
        if remaining<=0:break
        explanation=explain_conflict(effective,[cid],config,{'time_limit_seconds':min(3,max(.01,remaining/3)),
            'max_candidates_per_course':100,'max_nodes':20000,'max_oracle_calls':30,'allow_scoped_proof':False})
        calls.append({'tool':'explain_conflict','course_id':cid,'result':explanation})
        patterns=factory.get(cid)
        patterns_evidence=[];scoped_choices=[]
        for _,entries in patterns:
            blockers=occupancy.blockers(cid,entries)
            frequencies.update(blockers)
            if blockers<=allowed:
                scoped_choices.append((blockers,{key for entry in entries for key in keys_for(effective.courses[cid],entry)}))
            if len(patterns_evidence)<5:patterns_evidence.append({'placements':[placement_dict(p) for p in entries],'blocking_course_ids':sorted(blockers)})
        pattern_choices.append(scoped_choices)
        diagnostics.append({'course_id':cid,'course_name':effective.courses[cid].name,
            'generation':factory.diagnostics.get(cid,{}),'explanation_status':explanation['status'],
            'core_ids':explanation.get('core_ids',[]),'candidate_examples':patterns_evidence})
    direct=[cid for cid,_ in frequencies.most_common() if cid in allowed][:40]
    # Include concrete second-hop blockers so displaced courses can be reinserted.
    for cid in direct[:10]:
        if time.monotonic()>=deadline:break
        for _,entries in factory.get(cid)[:5]:frequencies.update(occupancy.blockers(cid,entries))
    movable=list(dict.fromkeys(direct+[cid for cid,_ in frequencies.most_common() if cid in allowed]))[:60] if remaining_moves else []
    weights=context['request'].get('agent_options',{}).get('quality_weights',{'time_preference':1,'evening':2,'weekend':4})
    # Try small concrete blocking sets before a broad neighborhood exhausts the search budget.
    neighborhoods=[]
    if len(ids)<=2 and pattern_choices and all(pattern_choices):
        from itertools import product
        combinations=[]
        for combo in product(*(choices[:20] for choices in pattern_choices)):
            if len(combo)==2 and combo[0][1]&combo[1][1]:continue
            group=set().union(*(choice[0] for choice in combo))
            if len(group)<=remaining_moves:combinations.append(group)
        for group in sorted(combinations,key=lambda group:(len(group),sorted(group))):
            group=sorted(group)
            if group not in neighborhoods:neighborhoods.append(group)
            if len(neighborhoods)>=8:break
    if movable not in neighborhoods:neighborhoods.append(movable)
    proposal=None
    for index,neighborhood in enumerate(neighborhoods):
        left=deadline-time.monotonic()
        if left<=0 and proposal is not None:break
        scoped={**config,'movable_ids':neighborhood,'max_moved_courses':remaining_moves,'locked_ids':sorted(locks),
                'time_limit_seconds':max(.01,left/max(1,len(neighborhoods)-index))}
        attempt=optimize_local(effective,ids,scoped,{'quality_weights':weights,'time_limit_seconds':scoped['time_limit_seconds'],
            'max_search_nodes':100000,'candidate_limit_per_course':100})
        calls.append({'tool':'optimize_local_repair','config':scoped,'summary':attempt['summary'],'results':attempt['results'],'solver':attempt['solver']})
        score=lambda p:(-p['summary']['inserted'],p['summary']['moved'],p['solver']['quality'])
        if proposal is None or score(attempt)<score(proposal):proposal=attempt
        if proposal['summary']['inserted']==len(ids):break
    config=proposal['config'];movable=config['movable_ids']
    final=Occupancy(effective)
    from scripts.repair_data import Placement
    final_rows=[Placement(p['course_id'],frozenset(p['weeks']),p['day'],tuple(p['periods']),p['room_id']) for p in proposal['placements']]
    from copy import copy
    final_data=copy(effective);final_data.placements=final_rows;after=Occupancy(final_data)
    signature=lambda rows:sorted(json.dumps(placement_dict(p),sort_keys=True) for p in rows)
    moved={cid for cid,rows in before.placements.items() if signature(rows)!=signature(after.placements.get(cid,[]))}
    if not set(before.placements)<=set(after.placements) or not moved<=set(movable) or len(moved|set(already_moved))>max(max_moves,len(set(already_moved))):
        raise ValueError('局部调课超出授权或未完整排回旧课')
    inserted=set(after.placements)-set(before.placements)
    outcomes={row['course_id']:row for row in proposal['results']};success=[];failures=[]
    exhausted=any(call.get('solver',{}).get('phase_1',{}).get('status')=='unknown_budget_exhausted' for call in calls)
    for entry in prepared:
        cid=entry['plan']['course_id']
        if cid in inserted:
            if any(p.room_id!=entry['option']['room_id'] for p in after.placements[cid]):raise ValueError('调课擅自更换已接受教室')
            success.append(entry)
        else:
            outcome=outcomes.get(cid,{})
            failures.append({'course_id':cid,'course_name':entry['plan']['course_name'],'option_id':entry['option']['option_id'],
                'status':outcome.get('status','search_limit'),'reason':('已按原方案尝试小范围联合调课，暂未排入；扩展范围的搜索达到预算上限，尚不能判断无解' if exhausted else '已按原资源方案诊断并联合调课，本次保留候选范围内暂未排入；不代表全局无解'),
                'diagnosis':outcome.get('diagnosis',{})})
    return {'data':final_data,'successes':success,'failures':failures,'changes':proposal['changes'],'config':proposal['config'],
        'placements':proposal['placements'],'tool_calls':calls,'movement_diagnostics':diagnostics,'moved_ids':sorted(moved)}
