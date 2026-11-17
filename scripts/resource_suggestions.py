"""Read-only resource scenarios with exact evidence, same-campus rooms and bounded trials."""
from collections import Counter
from copy import deepcopy
from dataclasses import replace,asdict
from hashlib import sha256
import json
import math
import time


def _positive_count(value):
    try:
        n=float(value)
        return int(n) if math.isfinite(n) and n>0 and n.is_integer() else None
    except (TypeError,ValueError):return None


def capacity_evidence(data,c,supplemental=None):
    supplemental=supplemental or {};evidence=[]
    if c.current_enrollment is not None:
        if not 0<c.current_enrollment<c.capacity:return []
        return [{'capacity':c.current_enrollment,'basis':'current_source_actual_enrollment',
                 'label':'当前源表实际人数（不覆盖源上限）','source_field':'current_enrollment',
                 'source_hashes':dict(data.source_hashes)}]
    matched_history=False
    for record in supplemental.get('original_courses',{}).get(c.id,[]):
        values=record['values'];name=str(values.get('课程名称','')).strip()
        declared=_positive_count(values.get('人数上限'))
        # A historical row is evidence for a hypothesis, never verified current enrollment.
        campus=str(values.get('校区','')).strip()
        if name!=c.name.strip() or declared!=c.capacity or (campus and campus!=c.campus):continue
        actual=_positive_count(values.get('实际人数'))
        if actual:matched_history=True
        if actual and actual<c.capacity:
            evidence.append({'capacity':actual,'basis':'historical_actual_enrollment',
                             'label':'历史源表实际人数（当前人数待核实）','source_records':[record]})
    # Never replace a matched historical actual count with a smaller peer-class count.
    if matched_history:return evidence
    key=c.id.split('@')[0].split('.')[0]
    peers=[p for p in data.courses.values() if p.id!=c.id and p.campus==c.campus and p.name.strip()==c.name.strip()
           and p.id.split('@')[0].split('.')[0]==key and _positive_count(p.capacity)]
    for count in sorted({int(p.capacity) for p in peers if p.capacity<c.capacity},reverse=True)[:4]:
        matches=[{'course_id':p.id,'course_name':p.name,'campus':p.campus,'capacity':p.capacity} for p in peers if p.capacity==count]
        evidence.append({'capacity':count,'basis':'same_course_peer_capacity','label':'同校区同课程其他教学班容量（仅假设）','peer_courses':matches})
    return evidence


def build_resource_plans(data,remaining,decisions=None,supplemental=None):
    from .repair_courses import room_rejections
    from .resource_similarity import similarity_context,room_similarity
    plans=[];decisions=decisions or {}
    for cid in remaining:
        c=data.courses.get(cid)
        if c is None:continue
        matching=[r for r in data.rooms.values() if not room_rejections(c,r,set())]
        if matching:continue
        def label(rid):
            r=data.rooms.get(rid)
            return f'{r.name} ({rid})' if r and r.name else rid
        original={'campus':c.campus,'building':'、'.join(sorted(c.buildings)),'room_type':c.room_type,
                  'room':'、'.join(label(rid) for rid in sorted(c.explicit_rooms)),
                  'historical_room':'、'.join(label(rid) for rid in sorted(c.historical_rooms)),
                  'capacity':c.capacity,'weekly_hours':c.hours}
        same=[r for r in data.rooms.values() if r.enabled and r.campus==c.campus and c.campus]
        evidence=capacity_evidence(data,c,supplemental)
        actual_floor=c.current_enrollment or 0
        history_floor=max((e['capacity'] for e in evidence if e['basis']=='historical_actual_enrollment'),default=0)
        protected_floor=max(actual_floor,history_floor)
        capacities=[(c.capacity,None)]+[(e['capacity'],e) for e in evidence if e['capacity']!=c.capacity]
        capacities=[(n,b) for n,b in capacities if protected_floor<=n<=c.capacity]
        context=similarity_context(data,c)
        similarities={r.id:room_similarity(data,c,r,context) for r in same}
        seen=set();options=[];omitted=0
        for capacity,basis in capacities:
            if capacity in seen:continue
            seen.add(capacity)
            eligible=[r for r in same if math.isfinite(r.capacity) and r.capacity>=max(capacity,actual_floor) and (not c.room_type or r.type)]
            scenario=replace(c,capacity=capacity)
            eligible.sort(key=lambda r:(not similarities[r.id]['auto_trial_eligible'],-similarities[r.id]['score'],len(room_rejections(scenario,r,set())),r.capacity,r.id))
            # Keep a useful mix of types, not fifteen rooms of the first type only.
            chosen=[];by_type=Counter()
            for r in eligible:
                if by_type[r.type]<4 and len(chosen)<20:chosen.append(r);by_type[r.type]+=1
            omitted+=len(eligible)-len(chosen)
            for r in chosen:
                proposed={'campus':c.campus,'building':r.building,'room_type':r.type,'room':label(r.id),
                          'historical_room':'','capacity':capacity,'weekly_hours':c.hours}
                option_id=sha256(json.dumps([cid,r.id,capacity],ensure_ascii=False).encode()).hexdigest()[:20]
                changes=[k for k in original if original[k]!=proposed[k]]
                options.append({'similarity':similarities[r.id], 'auto_trial_eligible':similarities[r.id]['auto_trial_eligible'], 'option_id':option_id,'room_id':r.id,'room_capacity':r.capacity,'proposed':proposed,
                                'changes':changes,'reasons':room_rejections(scenario,r,set()),
                                'capacity_evidence':basis,'conditional_only':basis is not None,
                                'requires_enrollment_confirmation':basis is not None,
                                'requires_room_type_confirmation':bool(c.room_type and c.room_type!=r.type)})
        options.sort(key=resource_option_rank)
        inventory={}
        for r in data.rooms.values():
            if r.enabled:
                item=inventory.setdefault(r.campus,{'enabled_rooms':0,'max_capacity':0,'room_types':set()})
                item['enabled_rooms']+=1;item['max_capacity']=max(item['max_capacity'],r.capacity);item['room_types'].add(r.type)
        for item in inventory.values():item['room_types']=sorted(item['room_types'])
        conflicts=[{'source_campus':r['values'].get('校区'), 'source_record':r} for r in (supplemental or {}).get('original_courses',{}).get(cid,[])
                   if r['values'].get('课程名称','').strip()==c.name.strip() and r['values'].get('校区') and r['values']['校区'].strip()!=c.campus]
        maximum=max((r.capacity for r in same),default=0)
        plans.append({'course_id':cid,'course_name':c.name,'original':original,'issues':list(c.issues),
                      'class_source_values':list(c.original_class_values),'class_issues':[],
                      'static_matching_rooms':0,'same_campus_max_capacity':maximum,'capacity_shortfall':max(0,c.capacity-maximum),
                      'source_capacity_upper_bound':c.capacity,'historical_count_policy':'use_only_if_strictly_below_source_upper_bound',
                      'protected_current_enrollment':actual_floor or None,'historical_actual_floor':history_floor or None,
                      'similarity_method':'structured_features_v1', 'capacity_evidence':evidence,'campus_inventory':inventory,'campus_source_conflicts':conflicts,
                      'campus_review_note':'校区来自源 SKXQ；其他校区库存只作核查线索，不能跨校区试排。若源校区有误，需要核实后修正源数据。',
                      'options':options,'options_omitted':omitted,'decision':decisions.get(cid),'teaching_weeks':sorted(c.weeks),
                      'hour_note':f'源每周 {c.hours:g} 学时，候选按 ceil 向上取整。' if math.isfinite(c.hours) and c.hours>0 and not float(c.hours).is_integer() else '',
                      'proposed_weekly_periods':math.ceil(c.hours) if math.isfinite(c.hours) and c.hours>0 else None})
    return plans



def resource_option_rank(option):
    basis=(option.get('capacity_evidence') or {}).get('basis','unchanged_source_capacity')
    priority={'unchanged_source_capacity':0,'current_source_actual_enrollment':0,'historical_actual_enrollment':1,'same_course_peer_capacity':2}.get(basis,3)
    return (not option.get('auto_trial_eligible',True),-option.get('similarity',{}).get('score',0),
            priority,len(option['reasons']),option['room_capacity'],option['room_id'])


def _resource_trial(data,plan,option,policy,seconds,*,prepare_only=False):
    from .repair_courses import repair
    from .repair_workflows import apply_data_corrections
    from .repair_rule_packs import apply_rule_pack
    from .timetable_agent_tools import _planning_assumptions
    cid=plan['course_id'];c=data.courses[cid];corrections=[]
    if option['conditional_only']:
        corrections=[{'operation':'set_capacity_assumption','course_id':cid,'expected_capacity':c.capacity,
                      'capacity':option['proposed']['capacity'],'assumption_only':True,
                      'evidence':json.dumps(option['capacity_evidence'],ensure_ascii=False)}]
    # Correction/rule functions copy their input. Restrict that copy to the
    # target metadata, then merge it into a shallow, read-only full occupancy view.
    from copy import copy
    target=copy(data);target.courses={cid:c};target.placements=[p for p in data.placements if p.course_id==cid]
    corrected,audit=apply_data_corrections(target,corrections);pack=None
    if option['requires_room_type_confirmation']:
        pack={'schema_version':1,'name':'未确认教室类型条件试排','rules':[{
            'id':'trial-'+option['option_id'],'kind':'room_type_substitution','course_ids':[cid],
            'substitute_room_types':[option['proposed']['room_type']],
            'evidence':'用户要求按历史人数及相似教室生成候选；相似度不是设施确认，采用前需逐课接受。'}]}
        corrected,_=apply_rule_pack(corrected,pack)
    effective=copy(data);effective.courses=dict(data.courses);effective.source_hashes=dict(corrected.source_hashes)
    effective.courses[cid]=corrected.courses[cid]
    viewed=effective.courses[cid]
    # Bind the actual selected room. An option for R1 cannot claim success in R2.
    effective.courses[cid]=replace(viewed,explicit_rooms=frozenset({option['room_id']}),
                                   historical_rooms=frozenset(),buildings=frozenset())
    hypothesis={'course_id':cid,'option_id':option['option_id'],'expected_capacity':c.capacity,
                'capacity':option['proposed']['capacity'],'room_id':option['room_id'],
                'room_type':option['proposed']['room_type'],'similarity':option.get('similarity'),
                'room_restrictions_relaxed_for_trial':True}
    effective.source_hashes['resource_hypothesis']=sha256(json.dumps(hypothesis,sort_keys=True).encode()).hexdigest()
    if prepare_only:
        return {'course_id':cid,'option_id':option['option_id'],'correction_parameters':corrections,'input_corrections':audit,'rule_pack':pack},effective
    cfg=asdict(policy);cfg.update(target_ids=[cid],movable_ids=[],max_moved_courses=0,time_relaxation_levels={},
        filter_fixed_occupancy=True,time_limit_seconds=max(.001,seconds),max_candidates_per_course=min(policy.max_candidates_per_course,100))
    proposal=repair(effective,[cid],cfg)
    proposal.update(planning_assumptions=_planning_assumptions(corrections),resource_hypothesis=hypothesis,
                    requires_resource_confirmation=True,correction_parameters=corrections,input_corrections=audit,rule_pack=pack)
    trial={'course_id':cid,'option_id':option['option_id'],'correction_parameters':corrections,'input_corrections':audit,
           'rule_pack':pack,'similarity':option.get('similarity'),'requires_resource_confirmation':True,
           'conditional_only':option['conditional_only'],'requires_enrollment_confirmation':option['conditional_only'],
           'committed':False,'used_functions':['apply_data_corrections','repair']+(['apply_rule_pack'] if pack else []),'proposal':proposal}
    return trial,effective


def joint_resource_repairs(data,plans,config,total_time_limit_seconds=30):
    """Select ranked alternatives against growing occupancy; independent success is not additive."""
    from .repair_courses import RepairConfig,Occupancy,validate_changes,placement_dict
    from .repair_data import Placement
    from .timetable_acceptance import validate_transition
    policy=RepairConfig(**config);policy.validate()
    if policy.max_moved_courses or policy.movable_ids:raise ValueError('Resource scenarios preserve every existing course')
    current=deepcopy(data);before=Occupancy(data);selected=[];attempts=[];outcomes={};changes=[]
    started=time.monotonic();deadline=started+total_time_limit_seconds
    all_corrections=[];all_rules=[]
    for index,plan in enumerate(plans):
        cid=plan['course_id'];options=[o for o in plan['options'] if o.get('auto_trial_eligible',True)]
        if not options:
            outcomes[cid]={'course_id':cid,'status':'no_compatible_room_suggestion'};continue
        course_deadline=time.monotonic()+max(0,(deadline-time.monotonic())/max(1,len(plans)-index))
        for option in options[:8]:
            if time.monotonic()>=course_deadline:break
            left=deadline-time.monotonic()
            if left<=0:break
            share=min(left,max(.001,course_deadline-time.monotonic()))
            trial,effective=_resource_trial(current,plan,option,policy,share)
            proposal=trial['proposal'];attempts.append({'course_id':cid,'option_id':option['option_id'],'inserted':proposal['summary']['inserted']})
            if not proposal['summary']['inserted']:continue
            current=effective
            current.placements=[Placement(r['course_id'],frozenset(r['weeks']),r['day'],tuple(r['periods']),r['room_id']) for r in proposal['placements']]
            selected.append({'course_id':cid,'option':option,'resource_hypothesis':proposal['resource_hypothesis']})
            all_corrections+=trial['correction_parameters']
            all_rules+=(trial.get('rule_pack') or {}).get('rules',[])
            changes+=proposal['changes'];outcomes[cid]=next(r for r in proposal['results'] if r['course_id']==cid)
            break
        outcomes.setdefault(cid,{'course_id':cid,'status':'search_limit' if time.monotonic()>=deadline else 'no_feasible_ranked_option'})
    final=Occupancy(current);ids={r['course_id'] for r in changes}
    if any(final.placements[cid]!=rows for cid,rows in before.placements.items()):raise RuntimeError('Prior placements changed')
    args=asdict(policy);args.update(target_ids=[p['course_id'] for p in plans],movable_ids=[],max_moved_courses=0,time_relaxation_levels={})
    errors=validate_changes(current,before.placements,final,ids,RepairConfig(**args))
    if errors:raise RuntimeError(str(errors[:5]))
    rows=[placement_dict(e) for cid in sorted(final.placements) for e in final.placements[cid]]
    before_rows=[placement_dict(e) for cid in sorted(before.placements) for e in before.placements[cid]]
    # Effective metadata includes every selected conditional scenario, without source writes.
    validation=validate_transition(current,before_rows,rows,[p['course_id'] for p in plans],sorted(set(outcomes)-ids),args)
    from .timetable_agent_tools import _planning_assumptions
    assumptions=_planning_assumptions(all_corrections)
    return {'mode':'joint_resource_trial','committed':False,'requires_resource_confirmation':True,
            'selected_options':selected,'attempts':attempts,'correction_parameters':all_corrections,
            'rule_pack':{'schema_version':1,'name':'联合资源条件试排（未确认）','rules':all_rules},
            'planning_assumptions':assumptions,
            'proposal':{'config':args,'source_hashes':data.source_hashes,'placements':rows,'changes':changes,
                        'results':[outcomes[p['course_id']] for p in plans],'validation':validation,
                        'planning_assumptions':assumptions,'summary':{'requested':len(plans),'inserted':len(ids),'moved':0,
                        'remaining':len(plans)-len(ids),'elapsed_seconds':round(time.monotonic()-started,3)}}}


def probe_resource_repairs(data,targets,config,supplemental=None,max_trials=None):
    """Fair independent probes followed by a jointly validated, uncommitted scenario."""
    from .repair_courses import RepairConfig
    policy=RepairConfig(**config);policy.validate()
    started=time.monotonic();total=min(policy.time_limit_seconds,30)
    plans=build_resource_plans(data,targets,supplemental=supplemental)
    max_trials=min(60,max(12,len(plans)+10)) if max_trials is None else max_trials
    trials=[];deadline=started+total*.6
    queues=[]
    for plan in plans:
        options=sorted((o for o in plan['options'] if o.get('auto_trial_eligible',True)),key=resource_option_rank)
        queues.append((plan,options))
    ordered=[(plan,q[i]) for i in range(max((len(q) for _,q in queues),default=0)) for plan,q in queues if i<len(q)]
    for plan,option in ordered:
        if len(trials)>=max_trials or time.monotonic()>=deadline:break
        share=(deadline-time.monotonic())/max(1,min(max_trials,len(ordered))-len(trials))
        trial,_=_resource_trial(data,plan,option,policy,share);trials.append(trial)
    left=max(.001,total-(time.monotonic()-started))
    joint=joint_resource_repairs(data,plans,config,left)
    return {'mode':'resource_repair_scenarios','course_plans':plans,'trials':trials,'joint_trial':joint,'committed':False,
            'summary':{'resource_shortage_courses':len(plans),'trials':len(trials),
                       'feasible_trials':sum(t['proposal']['summary']['inserted']>0 for t in trials),
                       'feasible_courses_independently':len({t['course_id'] for t in trials if t['proposal']['summary']['inserted']}),
                       'joint_inserted':joint['proposal']['summary']['inserted']},
            'interpretation':'独立成功不可相加；joint_trial 已联合占用验收。人数依据、相似度、设施差异和资源改动均逐课记录，全部未提交且需确认；真实人数和源Excel不被覆盖。'}
