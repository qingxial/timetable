"""Minimal per-course block limits justified by source load and specified windows."""
from copy import deepcopy
from dataclasses import asdict
import math
from scripts.repair_data import parse_time_windows


def assess_concentrated_course(course, config):
    from scripts.repair_courses import blocks_for,required_week_load,time_options,room_rejections
    result={'course_id':course.id,'eligible':False,'reason':'not_a_default_limit_shortfall'}
    try:
        loads=required_week_load(course);required=max(loads.values());blocks=blocks_for(course,77)
    except ValueError as error:
        return {**result,'reason':'invalid_source_load','detail':str(error)}
    if course.issues:return {**result,'reason':'source_data_issue'}
    if required<5:return {**result,'reason':'weekly_load_below_five','required_weekly_periods':required}
    strict=deepcopy(config);strict.time_scope='strict';strict.relax_course_unavailable=False
    strict.time_relaxation_levels={};strict.course_block_limits={}
    rules=course.teacher_time_rules
    from scripts.teacher_time_constraints import needs_scoped_time
    if rules and needs_scoped_time(rules):
        domain={(d,p) for d in config.days for p in config.periods}-parse_time_windows('周二(5-8节)')-parse_time_windows(config.additional_forbidden)
        if course.special.strip()=='仅周六周日排课':domain={cell for cell in domain if cell[0] in (5,6)}
    else:domain=time_options(course,strict)[0]
    domains=[]
    for week in sorted(course.weeks):
        cells=set(domain)
        for row in rules:
            from scripts.teacher_time_constraints import flags,value,text
            if week-1 not in flags(value(row,'RWJSZCDM')) & flags(value(row,'SKZCDM')):continue
            for key in ('Prefer_Time','available_Time'):
                raw=text(value(row,key))
                if raw:cells &= parse_time_windows(raw)
            cells -= parse_time_windows(text(value(row,'unavailable_Time')))
        domains.append(cells)
    designated=bool(course.prefer or course.special.strip()=='仅周六周日排课' or any(r.get('Prefer_Time') or r.get('available_Time') for r in rules))
    if not designated and required<=config.max_weekly_hours:
        return {**result,'reason':'no_designated_time_or_high_weekly_load'}
    # Count legal nonoverlapping block starts in each day, without occupancy.
    # The exact engine below still validates mixed blocks, weeks and resources.
    day_caps=[]
    for cells in domains:
        caps={}
        for day in config.days:
            used=set();count=0
            for length in sorted(blocks,reverse=True):
                step=1 if length==1 and config.single_period_starts=='any' else 2
                for start in range(1,12,step):
                    ps=set(range(start,start+length))
                    if all((day,p) in cells for p in ps) and not ps&used:
                        used|=ps;count+=1;break
            caps[day]=count
        day_caps.append(caps)
    if not day_caps or any(sum(c.values())<len(blocks) for c in day_caps):
        return {**result,'reason':'source_window_too_small_even_without_daily_limit'}
    baseline_capacity=min(sum(min(config.max_blocks_per_day,n) for n in caps.values()) for caps in day_caps)
    daily=config.max_blocks_per_day
    if baseline_capacity<len(blocks):
        daily=next((q for q in range(config.max_blocks_per_day+1,12)
                    if all(sum(min(q,n) for n in caps.values())>=len(blocks) for caps in day_caps)),None)
        if daily is None:return {**result,'reason':'source_window_too_small_even_without_daily_limit'}
    weekly=max(config.max_weekly_hours,required)
    if daily==config.max_blocks_per_day and weekly==config.max_weekly_hours:return result
    return {**result,'eligible':True,'reason':'specified_window_block_shortfall' if baseline_capacity<len(blocks) else 'source_high_weekly_load',
            'designated_time':designated,'source_weekly_hours':course.hours,'required_weekly_periods':required,'required_blocks':len(blocks),
            'baseline_blocks_in_source_window':baseline_capacity,
            'before':{'max_weekly_hours':config.max_weekly_hours,'max_blocks_per_day':config.max_blocks_per_day},
            'after':{'max_weekly_hours':weekly,'max_blocks_per_day':daily},
            'source_preference_preserved':True,'source_weeks_preserved':True}


def prepare_concentrated_policy(dataset, targets, config):
    from scripts.repair_courses import room_rejections
    policy=deepcopy(config)
    if not policy.auto_concentrated_teaching:return policy
    assigned={p.course_id for p in dataset.placements}
    for cid in targets:
        if cid in assigned or cid not in dataset.courses:continue
        c=dataset.courses[cid];audit=assess_concentrated_course(c,policy)
        if not audit['eligible']:continue
        if not any(not room_rejections(c,r,set()) for r in dataset.rooms.values()):continue
        policy.course_block_limits[cid]=audit['after']
    return policy


def repair_concentrated_courses(dataset, targets, config):
    from scripts.repair_courses import repair,RepairConfig
    cfg=asdict(config) if isinstance(config,RepairConfig) else deepcopy(config)
    if cfg.get('max_moved_courses',0) or cfg.get('movable_ids'):raise ValueError('Concentrated repair preserves existing courses')
    cfg.update(auto_concentrated_teaching=True,time_scope='strict',relax_course_unavailable=False,time_relaxation_levels={},filter_fixed_occupancy=True)
    proposal=repair(dataset,targets,cfg)
    policy=RepairConfig(**cfg)
    proposal['concentrated_teaching_audit']=[assess_concentrated_course(dataset.courses[cid],policy) for cid in (policy.target_ids or targets)]
    return proposal
