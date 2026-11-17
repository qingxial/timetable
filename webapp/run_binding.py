"""Resolve saved review decisions without changing historical run results."""
from pathlib import Path
import json,re


def current_run_state(data_root,run_id,result=None):
    if not isinstance(run_id,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}',run_id):raise ValueError('运行编号无效')
    folder=Path(data_root)/'agent-runs'/run_id
    result=result if result is not None else json.loads((folder/'result.json').read_text())
    original=result.get('output_state_id');selected=original;selected_ancestors=set()
    if not original:return None
    if not any((folder/name).exists() for name in ('time-confirmed.json','one-off-confirmed.json','resource-confirmed.json','chat-confirmed.json')):return selected
    from scripts.timetable_state import StateStore
    store=StateStore(Path(data_root)/'timetable-state')
    for name in ('time-confirmed.json','one-off-confirmed.json','resource-confirmed.json','chat-confirmed.json'):
        path=folder/name
        if not path.exists():continue
        receipt=json.loads(path.read_text());candidate=receipt.get('output_state_id')
        if not receipt.get('ok') or not candidate:continue
        ancestors=[];state=store.load(candidate)
        while True:
            ancestors.append(state['state_id'])
            if state['state_id']==original:break
            parent=state.get('parent_state_id')
            if not parent:raise ValueError('确认后的版本不属于该次运行，拒绝自动切换')
            state=store.load(parent)
        if receipt.get('input_state_id') not in ancestors:raise ValueError('确认记录的父版本不在课表链中')
        if selected in ancestors:selected=candidate;selected_ancestors=set(ancestors)
        elif candidate not in selected_ancestors:raise ValueError('确认记录存在分叉，请明确选择版本')
    return selected


def present_run_result(data_root,run_id,result):
    selected=current_run_state(data_root,run_id,result)
    last_review=Path(data_root)/'agent-runs'/run_id/'last-resource-review.json'
    if last_review.exists():
        review=json.loads(last_review.read_text())
        if review.get('room_review',{}).get('failures'):
            result={**result,'room_review':review['room_review'],'accepted_resource_run_id':Path(review['run_dir']).name}
    if not selected or selected==result.get('output_state_id'):return result
    from scripts.timetable_state import StateStore
    store=StateStore(Path(data_root)/'timetable-state');original=store.load(result['output_state_id']);current=store.load(selected)
    old={p['course_id'] for p in original['placements']};new={p['course_id'] for p in current['placements']}
    from copy import deepcopy
    coverage=deepcopy(result.get('agent_report',{}).get('coverage'))
    if coverage:
        remaining=set(current['remaining_course_ids']);confirmed={a['course_id'] for a in current.get('operation',{}).get('confirmed_one_off_weeks',[])}
        for cid,row in coverage['courses'].items():
            if cid not in remaining:row['status']='scheduled'
            elif row['status']=='waiting_data' and cid in confirmed:row.update(status='failed',reason='授课周已补齐，本次试排暂未排入')
            elif row['status']=='waiting_time' and (Path(data_root)/'agent-runs'/run_id/'time-confirmed.json').exists():row.update(status='failed',reason='该轮未接受此禁排候选，继续保留未排')
        coverage['counts']={name:sum(row['status']==name for row in coverage['courses'].values()) for name in ('scheduled','waiting_resource','waiting_data','waiting_time','failed','queued')}
        coverage['remaining']=len(remaining);coverage['unfinished']=coverage['counts']['queued'];coverage['unattempted']=sum(not row.get('attempts') and row['status']=='queued' for row in coverage['courses'].values())
    return {**result,'continuation_state_id':selected,'continuation_coverage':coverage,'continuation_summary':{'inserted':len(new-old),'remaining':len(current['remaining_course_ids']),'moved':0}}
