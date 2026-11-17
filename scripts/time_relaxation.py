"""Operation-scoped, monotone time relaxation; no workbook/state writes."""
from copy import deepcopy
from dataclasses import asdict
import time
from scripts.repair_courses import RepairConfig, Occupancy, repair, validate_changes, placement_dict
from scripts.repair_data import Placement


def repair_with_time_relaxation(dataset, target_ids, config):
    policy=RepairConfig(**deepcopy(config)) if isinstance(config,dict) else deepcopy(config)
    policy.validate()
    if policy.max_moved_courses or policy.movable_ids:
        raise ValueError('Time relaxation preserves all existing courses; movement is a separate operation')
    requested=list(dict.fromkeys(policy.target_ids or target_ids))
    current=deepcopy(dataset);before=Occupancy(dataset)
    remaining=requested[:];changes={};outcomes={};runs=[];nodes=0;started=time.monotonic()
    stages=[('strict',False),('same_day',False),('weekdays',False)]
    if policy.relax_course_unavailable:stages.append(('weekdays',True))
    for index,(scope,unavailable) in enumerate(stages):
        left=policy.time_limit_seconds-(time.monotonic()-started)
        if left<=0 or not remaining:break
        stage='course_unavailable' if unavailable else scope
        args=asdict(policy)
        args.update(time_scope=scope,relax_course_unavailable=unavailable,time_relaxation_levels={},target_ids=remaining,
                    time_limit_seconds=max(.001,left/(len(stages)-index)),filter_fixed_occupancy=True)
        proposal=repair(current,remaining,args)
        nodes+=proposal['summary']['search_nodes']
        for row in proposal['results']:outcomes[row['course_id']]={**row,'attempt_stage':stage}
        for row in proposal['changes']:changes[row['course_id']]={**row,'attempt_stage':stage}
        current.placements=[Placement(r['course_id'],frozenset(r['weeks']),r['day'],tuple(r['periods']),r['room_id']) for r in proposal['placements']]
        runs.append({'stage':stage,'summary':proposal['summary'],'config':args})
        remaining=[cid for cid in remaining if outcomes[cid]['status'] not in ('placed','already_scheduled')]
    final=Occupancy(current)
    assert all(final.placements[cid]==rows for cid,rows in before.placements.items())
    for cid,row in changes.items():
        check=deepcopy(policy);check.time_relaxation_levels={};check.time_scope='strict' if row['attempt_stage']=='strict' else ('same_day' if row['attempt_stage']=='same_day' else 'weekdays')
        check.relax_course_unavailable=row['attempt_stage']=='course_unavailable'
        errors=validate_changes(current,before.placements,final,{cid},check)
        if errors:raise RuntimeError(str(errors))
    final_config=asdict(policy);final_config.update(target_ids=requested,time_scope='weekdays',time_relaxation_levels={cid:r['attempt_stage'] for cid,r in changes.items()})
    pending=sorted(cid for cid,r in changes.items() if r['requires_time_confirmation'])
    return {'schema_version':1,'mode':'proposal','config':final_config,'source_hashes':dataset.source_hashes,
            'summary':{'requested':len(requested),'inserted':len(changes),'moved':0,'baseline_scheduled':len(before.placements),
                       'proposed_scheduled':len(final.placements),'search_nodes':nodes,'elapsed_seconds':round(time.monotonic()-started,3)},
            'placements':[placement_dict(e) for cid in sorted(final.placements) for e in final.placements[cid]],
            'changes':[changes[cid] for cid in sorted(changes)],
            'results':[outcomes.get(cid,{'course_id':cid,'status':'search_limit'}) for cid in requested],
            'validation':{'new_conflicts':0,'hours_and_weeks_preserved':True,'prior_placements_unchanged':True},
            'requires_time_confirmation_ids':pending,'workflow':{'name':'time_relaxation','stages':runs},'data_issues':dataset.issues}
