"""Accept an exact, hash-bound subset of a time-relaxation proposal."""
from copy import deepcopy
from pathlib import Path
import json
import re
from scripts.timetable_state import StateStore, file_hash
from scripts.timetable_agent_loop import _dataset, _validate_proposal


def accept_time_review(data_root, run_id, proposal_hash, accepted_ids):
    if not isinstance(run_id,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}',run_id):
        raise ValueError('运行ID无效')
    run=Path(data_root)/'agent-runs'/run_id
    path=run/'pending-time.json'
    if not path.is_file():raise ValueError('该轮没有有效的禁排候选')
    if not isinstance(proposal_hash,str) or file_hash(path)!=proposal_hash:raise ValueError('候选已改变，请重新读取')
    if not isinstance(accepted_ids,list) or any(not isinstance(v,str) for v in accepted_ids):raise ValueError('确认课程列表无效')
    saved=json.loads(path.read_text());proposal=deepcopy(saved['proposal'])
    pending=set(proposal['requires_time_confirmation_ids'])
    accepted=set(accepted_ids)
    if not accepted<=pending:raise ValueError('确认范围超出待确认课程')
    # Idempotent replay uses the same decision only, never silently adds grants.
    receipt=run/'time-confirmed.json'
    if receipt.exists():
        result=json.loads(receipt.read_text())
        if set(result['accepted_course_ids'])!=accepted:raise ValueError('该候选已处理，请从保存版本重新试排')
        return result
    store=StateStore(Path(data_root)/'timetable-state')
    state,dataset,proposal,affected=prepare_current_time_proposal(data_root,run_id)
    if accepted & set(affected):raise ValueError('部分候选受新安排影响，请只重试受影响课程：'+','.join(sorted(accepted & set(affected))))
    original={r['course_id'] for r in state['placements']}
    inserted={r['course_id'] for r in proposal['changes']} - (pending-accepted)
    proposal['placements']=[r for r in proposal['placements'] if r['course_id'] in original|inserted]
    proposal['changes']=[r for r in proposal['changes'] if r['course_id'] in inserted]
    config=proposal['config'];config['time_relaxation_levels']={cid:level for cid,level in config.get('time_relaxation_levels',{}).items() if cid in inserted};config.update(target_ids=sorted(inserted),movable_ids=[],max_moved_courses=0,relax_course_unavailable=bool(accepted))
    report,inserted,moved=_validate_proposal(dataset,state,proposal,config,original,set(),0)
    new=store.commit(state['state_id'],proposal['placements'],sorted(set(state['remaining_course_ids'])-inserted),
          {'engine':'time_review','tool':'accept_time_review','run_id':run_id,'parameters':{**config,'time_policy':'modern_repair'},
           'time_relaxations':proposal['changes'],'confirmed_course_unavailable_ids':sorted(accepted),
           'rule_pack_spec':saved.get('rule_pack_spec'),'confirmed_one_off_weeks':deepcopy(state.get('operation',{}).get('confirmed_one_off_weeks',[])),
           'room_review_request':json.loads((run/'request.json').read_text())},report) if inserted else state
    result={'ok':True,'status':'completed','input_state_id':state['state_id'],'output_state_id':new['state_id'],
            'pending_source_run_id':run_id,'accepted_course_ids':sorted(accepted),'summary':{'inserted':len(inserted),'remaining':len(new['remaining_course_ids']),'moved':0},
            'time_relaxations':proposal['changes'],'validation':report}
    receipt.write_text(json.dumps(result,ensure_ascii=False,indent=2))
    return result


def prepare_current_time_proposal(data_root,run_id):
    """Reuse unchanged candidate placements; only validate their impact against saved reviews."""
    from dataclasses import asdict
    from run_binding import current_run_state
    run=Path(data_root)/'agent-runs'/run_id;saved=json.loads((run/'pending-time.json').read_text());proposal=deepcopy(saved['proposal'])
    store=StateStore(Path(data_root)/'timetable-state');original=store.load(saved['parent_state_id']);state=store.load(current_run_state(data_root,run_id))
    data,_=_dataset(store,state,saved.get('rule_pack_spec'),run)
    if state['state_id']==original['state_id']:return state,data,proposal,[]
    old_data,_=_dataset(store,original,saved.get('rule_pack_spec'),run);remaining=set(state['remaining_course_ids']);safe=[];affected=[]
    for row in proposal['changes']:
        cid=row['course_id']
        if cid not in remaining:continue
        if asdict(data.courses[cid])!=asdict(old_data.courses[cid]):affected.append(cid);continue
        config=deepcopy(proposal['config']);config['target_ids']=[cid];config['time_relaxation_levels']={k:v for k,v in config.get('time_relaxation_levels',{}).items() if k==cid}
        candidate={**proposal,'placements':state['placements']+row['after'],'source_hashes':data.source_hashes}
        try:_validate_proposal(data,state,candidate,config,{r['course_id'] for r in state['placements']},set(),0)
        except (ValueError,RuntimeError):affected.append(cid);continue
        safe.append(row)
    proposal['changes']=safe;proposal['placements']=state['placements']+[p for row in safe for p in row['after']]
    proposal['source_hashes']=data.source_hashes;ids={row['course_id'] for row in safe};proposal['config']['target_ids']=sorted(ids)
    proposal['config']['time_relaxation_levels']={cid:level for cid,level in proposal['config'].get('time_relaxation_levels',{}).items() if cid in ids}
    proposal['requires_time_confirmation_ids']=sorted(set(proposal['requires_time_confirmation_ids']) & ids)
    return state,data,proposal,affected
