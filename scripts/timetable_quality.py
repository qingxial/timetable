"""Read-only full-timetable quality analysis. No solver or timetable writes.

Counts use actual teaching weeks, not Excel row counts. Original compliance,
accepted deviations, conditional assumptions and missing data remain distinct.
"""
from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
from itertools import combinations
import json
import math
from pathlib import Path
import re
from statistics import mean, median, pstdev

from scripts.repair_data import Placement, load_dataset, parse_time_windows, _read_table
from scripts.repair_courses import room_rejections
from scripts.timetable_acceptance import block_allowed, hour_plan_differences
from scripts.teacher_time_constraints import flags, rule_errors

SCHEMA_VERSION = 1
SCHOOL_BAN = {(1, p) for p in range(5, 9)}
STATUS_NAMES = {'violation':'违规','accepted':'已确认调整','pending':'待核实条件','unknown':'资料不足','warning':'原要求偏离'}


def ratio(numerator, denominator):
    return round(numerator / denominator * 100, 4) if denominator else None


def metric(name, numerator, denominator, definition, unit='%'):
    return {'name':name,'value':ratio(numerator,denominator),'numerator':numerator,
            'denominator':denominator,'definition':definition,'unit':unit,
            'status':'available' if denominator else 'not_applicable'}


def distribution(values):
    values=sorted(values)
    if not values:return {'count':0,'mean':None,'median':None,'p10':None,'p90':None,'max':None}
    def percentile(q):
        pos=(len(values)-1)*q;left=int(pos);right=min(left+1,len(values)-1)
        return values[left]+(values[right]-values[left])*(pos-left)
    return {'count':len(values),'mean':round(mean(values),4),'median':median(values),
            'p10':round(percentile(.1),4),'p90':round(percentile(.9),4),'max':max(values)}


def placement_signature(rows):
    return sorted((tuple(sorted(p['weeks'])),p['day'],tuple(p['periods']),p['room_id']) for p in rows)


def course_signature(course):
    """Binding fields for time/block decisions; independent of unrelated room edits."""
    return {'hours':course.hours,'weeks':sorted(course.weeks),'teachers':{k:sorted(v) for k,v in course.teachers.items()},
            'prefer':course.prefer,'unavailable':course.unavailable,'special':course.special,
            'rules':[{k:r.get(k) for k in ('JSH','RWJSZCDM','SKZCDM','Prefer_Time','unavailable_Time','available_Time','ZXS')} for r in course.teacher_time_rules]}


def grouped_rows(rows):
    result=defaultdict(list)
    for row in rows:result[row['course_id']].append(row)
    return result


def _parameters(state):
    params=state.get('operation',{}).get('parameters',{})
    return deepcopy(params.get('config',params))


def analyze_saved_timetable(state_root,state_id,baseline_state_id=None):
    """Load exact immutable versions and replay only matching historical evidence."""
    from scripts.timetable_state import StateStore
    root=Path(state_root)
    if not (root/'states').is_dir() or not (root/'datasets').is_dir():raise ValueError('课表版本库不存在')
    store=StateStore(root);current=store.load(state_id);chain=[];seen=set();node=current
    while node:
        if node['state_id'] in seen:raise ValueError('课表版本链存在循环')
        seen.add(node['state_id']);chain.append(node)
        node=store.load(node['parent_state_id']) if node.get('parent_state_id') else None
    chain.reverse();cache={}
    def dataset(state):
        spec=state.get('operation',{}).get('rule_pack_spec')
        key=(state['dataset_id'],json.dumps(spec,sort_keys=True))
        if key not in cache:
            paths=store.dataset_files(state)
            cache[key]=load_dataset(paths['courses'],paths['rooms'],store.states/state['state_id']/state['artifacts']['schedule'],paths.get('classes'))
            if spec:
                from tempfile import TemporaryDirectory
                from scripts.repair_rule_packs import load_rule_pack,apply_rule_pack
                with TemporaryDirectory(prefix='quality-rules-') as tmp:
                    rule_path=Path(tmp)/'rules.json';rule_path.write_text(json.dumps(spec,ensure_ascii=False))
                    cache[key],_=apply_rule_pack(cache[key],load_rule_pack(rule_path))
        return cache[key]
    data=deepcopy(dataset(current));data.placements=[Placement(p['course_id'],frozenset(p['weeks']),p['day'],tuple(p['periods']),p['room_id']) for p in current['placements']]
    original=dataset(chain[0]);final=grouped_rows(current['placements']);evidence={};origins={};scope=set()
    history_changes=[];evidence_gaps=[];previous_rows={}
    for state in chain:
        now=grouped_rows(state['placements']);op=state.get('operation',{});params=_parameters(state)
        source=dataset(state);run_id=op.get('run_id');saved_request={}
        if run_id and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}',str(run_id)):
            request_path=store.root.parent/'agent-runs'/run_id/'request.json'
            if request_path.exists():saved_request=json.loads(request_path.read_text())
        scope.update(now);scope.update(state.get('remaining_course_ids',[]))
        inserted=set(now)-set(previous_rows)
        moved={cid for cid in set(now)&set(previous_rows) if placement_signature(now[cid])!=placement_signature(previous_rows[cid])}
        actual_changes=inserted|moved
        auth=saved_request.get('authorization',op.get('authorization',{}))
        if not auth and isinstance(op.get('parameters'),dict):auth=op['parameters'].get('authorization',{})
        own_ids=set(op.get('confirmed_course_unavailable_ids',[]))
        time_rows={r['course_id']:r for r in op.get('time_relaxations',[])}
        hours={r['course_id']:r for r in params.get('accepted_hour_differences',[])}
        reviews={r['course_id']:r for r in op.get('human_room_reviews',[])}
        assumptions={r['course_id']:r for r in op.get('planning_assumptions',{}).get('capacity_assumptions',[])}
        oneoffs={r['course_id']:r for r in op.get('confirmed_one_off_weeks',[])}
        confirmed_by_receipt=False
        if op.get('engine')=='time_review' and run_id:
            receipt=store.root.parent/'agent-runs'/run_id/'time-confirmed.json'
            if receipt.exists():
                r=json.loads(receipt.read_text());confirmed_by_receipt=bool(r.get('ok') and r.get('output_state_id')==state['state_id'])
                own_ids &= set(r.get('accepted_course_ids',[]))
            else:evidence_gaps.append({'state_id':state['state_id'],'reason':'禁排确认凭据缺失'})
        elif op.get('engine')=='chat_accept':
            confirmed_by_receipt=bool(op.get('chat_candidate_hash') and state.get('validation',{}).get('valid'))
        for cid in now:
            if cid not in source.courses or cid not in data.courses:continue
            unchanged=placement_signature(now[cid])==placement_signature(final.get(cid,[]))
            metadata_match=course_signature(source.courses[cid])==course_signature(data.courses[cid])
            if cid in actual_changes:
                origins[cid]={'state_id':state['state_id'],'run_id':run_id,'engine':op.get('engine'),
                    'days':params.get('days',list(range(7))),'periods':params.get('periods',list(range(1,12))),
                    'allow_weekend':auth.get('allow_weekend'),'allow_evening':auth.get('allow_evening'),'is_root':not bool(state.get('parent_state_id')),
                    'additional_forbidden':params.get('additional_forbidden',''),
                    'max_blocks_per_day':params.get('max_blocks_per_day'),
                    'max_weekly_hours':params.get('max_weekly_hours')}
                # A later move invalidates a time grant unless this state carries fresh evidence.
                evidence[cid]={}
            if not unchanged or not metadata_match:continue
            ev=evidence.setdefault(cid,{})
            reference={'state_id':state['state_id'],'run_id':run_id}
            if cid in actual_changes:
                ev['origin']=origins[cid]
                if params.get('preference_as_soft') or 'time_preference' in params.get('allowed_changes',[]) or time_rows.get(cid,{}).get('relaxed_fields'):
                    ev['preference']={**reference,'reason':'保存时允许偏好偏离'}
                if cid in params.get('course_block_limits',{}):
                    ev['block_limits']={**reference,**params['course_block_limits'][cid]}
                if cid in own_ids and confirmed_by_receipt and params.get('relax_course_unavailable'):
                    after=time_rows.get(cid,{}).get('after')
                    if after is not None and placement_signature(after)==placement_signature(now[cid]):ev['course_unavailable']={**reference,'confirmed':True}
            if cid in hours:
                actual=Counter(w for p in now[cid] for w in p['weeks'] for _ in p['periods'])
                row=hours[cid]
                if row.get('actual_weekly_load')=={str(w):n for w,n in sorted(actual.items())}:ev['hour_difference']={**reference,**row}
            if cid in reviews:
                review=reviews[cid];proposed=review.get('proposed',{})
                if review.get('accepted') and all(p['room_id']==review.get('source_revision',{}).get('room_id',next(iter(source.courses[cid].explicit_rooms),None)) for p in now[cid]):
                    ev['resource']={**reference,'original':review.get('original',{}),'proposed':proposed,'option_id':review.get('option_id'),'similarity':review.get('similarity')}
            if cid in assumptions and assumptions[cid].get('capacity')==data.courses[cid].capacity:ev['capacity_assumption']={**reference,**assumptions[cid]}
            if cid in oneoffs:ev['one_off_week']={**reference,**oneoffs[cid]}
        if state.get('parent_state_id'):
            cap=params.get('max_moved_courses')
            locks=set(params.get('locked_ids',[]));movable=set(params.get('movable_ids',[]))
            violations=[]
            if moved & locks:violations.append('移动了锁定课程')
            if cap is not None and len(moved)>min(30,cap):violations.append('实际移动超过本次上限')
            if moved and 'movable_ids' in params and moved-movable:violations.append('移动超出可动清单')
            lost=set(previous_rows)-set(now)
            if lost:violations.append('旧课在后续版本中丢失')
            history_changes.append({'state_id':state['state_id'],'parent_state_id':state['parent_state_id'],'run_id':run_id,
                'inserted_ids':sorted(inserted),'moved_ids':sorted(moved),'lost_ids':sorted(lost),'movement_limit':cap,
                'violations':violations,'authorization_recorded':cap is not None})
        previous_rows=now
    # Human-readable metadata comes from this version, not a global workbook.
    metadata={'courses':{},'teachers':{}};files=store.dataset_files(current)
    for _,row in _read_table(files['courses'],True):
        cid=str(row.get('JXBID') or '').strip()
        if cid:
            meta=metadata['courses'].setdefault(cid,{})
            for key in ('KKDW','KKDWDM','KKDWMC','KKXY','KKXYMC','开课院系','开课单位','KCLB','课程类别'):
                if row.get(key):meta.setdefault('department' if key not in ('KCLB','课程类别') else 'category',str(row[key]))
    if files.get('teachers'):
        for _,row in _read_table(files['teachers'],False):
            tid=str(row.get('JSH') or row.get('教师号') or '').strip()
            if tid:metadata['teachers'][tid]={'name':str(row.get('XM') or row.get('教师姓名') or tid),
                'department':str(row.get('YXMC') or row.get('YXDM') or row.get('院系') or '')}
    baseline=store.load(baseline_state_id) if baseline_state_id else None
    report=analyze_timetable_quality(data,current['remaining_course_ids'],original=original,evidence=evidence,
        scope_ids=scope,metadata=metadata,origins=origins,history_changes=history_changes,
        baseline_rows=baseline['placements'] if baseline else None)
    report['binding']={'state_id':state_id,'state_hash':current.get('state_hash'),'dataset_id':current['dataset_id'],
        'source_hashes':current['source_hashes'],'original_state_id':chain[0]['state_id'],'lineage_state_ids':[s['state_id'] for s in chain],
        'baseline_state_id':baseline_state_id,'baseline_source_hashes':baseline.get('source_hashes') if baseline else None,
        'comparison_same_sources':baseline['source_hashes']==current['source_hashes'] if baseline else None}
    report['evidence_gaps']=evidence_gaps
    report['read_only']=True;report['solver_started']=False
    report['generated_at']=datetime.now(timezone.utc).isoformat()
    return report


def analyze_timetable_quality(dataset,remaining_course_ids,*,original=None,evidence=None,scope_ids=None,metadata=None,
                              origins=None,history_changes=(),baseline_rows=None,room_openings=None):
    """Compute all issues and eight quality dimensions using one occupancy index.

    room_openings, when supplied, maps a room to actual (week,day,period) cells.
    Otherwise report reference-calendar occupancy with explicit unknown openings.
    """
    original=original or dataset;evidence=evidence or {};metadata=metadata or {};origins=origins or {}
    rows=defaultdict(list);course_cells=defaultdict(set);raw_cells=Counter();teacher_index=defaultdict(set);room_index=defaultdict(set)
    teacher_slots=defaultdict(set);room_slots=defaultdict(set);issues=[];courses=[];relaxations=[];time_cache={};window_cache={}
    school=set(SCHOOL_BAN);scope=set(scope_ids if scope_ids is not None else set(p.course_id for p in dataset.placements)|set(remaining_course_ids))
    def issue(cid,rule,status,message,**details):
        row={'course_id':cid,'course_name':dataset.courses[cid].name if cid in dataset.courses else '',
             'rule':rule,'status':status,'status_label':STATUS_NAMES[status],'message':message,**details}
        issues.append(row);return row
    def windows(raw,cid,rule):
        key=str(raw or '')
        if not key:return set()
        if key not in window_cache:
            try:window_cache[key]=parse_time_windows(key)
            except ValueError:window_cache[key]=None
        result=window_cache[key]
        if result is None:issue(cid,rule,'unknown','时间表达暂不能解析',source=key)
        return result
    for p in dataset.placements:
        cid=p.course_id;rows[cid].append(p)
        if cid not in dataset.courses:issue(cid,'course_metadata','violation','安排中的教学班不存在于源数据');continue
        c=dataset.courses[cid]
        if p.day not in range(7) or not p.weeks or not p.periods or any(type(w) is not int or not 1<=w<=53 for w in p.weeks) or any(type(k) is not int or not 1<=k<=11 for k in p.periods) or list(p.periods)!=list(range(min(p.periods),max(p.periods)+1)):
            issue(cid,'malformed_block','violation','安排有非法周次、星期或不连续节次',placement={'weeks':sorted(p.weeks),'day':p.day,'periods':list(p.periods),'room_id':p.room_id});continue
        for w in p.weeks:
            for period in p.periods:
                cell=(w,p.day,period);course_cells[cid].add(cell);raw_cells[(cid,*cell)]+=1
                for tid,weeks in c.teachers.items():
                    if w in weeks:teacher_index[(tid,*cell)].add(cid);teacher_slots[tid].add(cell)
                if c.check_room:room_index[(p.room_id,*cell)].add(cid);room_slots[p.room_id].add(cell)
    remaining=set(remaining_course_ids)
    if len(remaining)!=len(remaining_course_ids):issue('','scope','violation','未排清单存在重复ID')
    for cid in sorted(scope-set(rows)-remaining):issue(cid,'scope','violation','教学班从已排及未排清单中消失')
    for cid in sorted(set(rows)&remaining):issue(cid,'scope','violation','教学班同时出现在已排和未排清单')
    for cid in sorted(set(rows)-scope):issue(cid,'scope','violation','安排包含范围外教学班')
    duplicate_by=defaultdict(list)
    for (cid,w,d,p),n in raw_cells.items():
        if n>1:duplicate_by[cid].append({'week':w,'day':d,'period':p,'count':n})
    for cid,cells in duplicate_by.items():issue(cid,'duplicate_or_self_overlap','violation','同一教学班课节重复或自身重叠',cells=cells)
    conflicts=[]
    for kind,index in [('teacher',teacher_index),('room',room_index)]:
        for (rid,w,d,p),ids in sorted(index.items()):
            if len(ids)>1:
                detail={'resource_type':kind,'resource_id':rid,'week':w,'day':d,'period':p,'course_ids':sorted(ids),'pair_count':len(ids)*(len(ids)-1)//2}
                conflicts.append(detail)
                for cid in sorted(ids):issue(cid,kind+'_conflict','violation','教师同一课节承担多门课' if kind=='teacher' else '教室同一课节被多门课占用',**detail)
    completeness=[];hour_differences=[];preference_total=preference_good=preference_cells=pref_fit_cells=0
    all_cells=evening=weekend=intersection=0;capacity_rows=[];stability=[];days_load=Counter();heat=Counter();block_hist=Counter()
    fractional=[];relaxed_seen=set();preferred_courses=0;preferred_unknown=0
    for cid in sorted(scope|set(rows)):
        c=dataset.courses.get(cid)
        if c is None:issue(cid,'course_metadata','unknown','缺少源教学班信息');continue
        base=original.courses.get(cid,c);ev=evidence.get(cid,{});entries=rows[cid];cells=course_cells[cid]
        if not entries:
            issue(cid,'unassigned','pending','尚无课程安排');completeness.append({'course_id':cid,'complete':False,'reason':'unassigned'});continue
        start=len(issues);tags=[];actual=Counter(w for w,d,p in cells)
        if c.capacity!=base.capacity and not ev.get('capacity_assumption') and not ev.get('resource'):
            issue(cid,'capacity_change_evidence','unknown','当前人数需求与原源不同，需核对数据更正或人数确认依据',original_demand=base.capacity,current_demand=c.capacity)
        source_expected=c.weekly_loads or {w:c.hours for w in c.weeks}
        integer_expected=c.weekly_loads or ({w:math.ceil(c.hours) for w in c.weeks} if c.hours>0 else {})
        actual_dict={str(w):n for w,n in sorted(actual.items())};source_dict={str(w):n for w,n in sorted(source_expected.items())}
        known=bool(integer_expected);complete=known and dict(actual)==integer_expected
        if c.hours>0 and not float(c.hours).is_integer():fractional.append(cid)
        source_diff=set(source_expected)!=set(actual) or any(abs(actual.get(w,0)-n)>1e-5 for w,n in source_expected.items())
        exact_hour_accept=ev.get('hour_difference',{}).get('actual_weekly_load')==actual_dict and ev.get('hour_difference',{}).get('expected_weekly_load')==source_dict
        if source_diff:
            delta={'course_id':cid,'course_name':c.name,'expected_weekly_load':source_dict,'actual_weekly_load':actual_dict,
                   'expected_total':sum(source_expected.values()),'actual_total':sum(actual.values()),
                   'confirmed':exact_hour_accept,'rounding_only':complete and cid in fractional}
            hour_differences.append(delta)
            issue(cid,'weekly_plan_difference','accepted' if exact_hour_accept else 'pending','源周计划与实际节次有差异',**{k:v for k,v in delta.items() if k not in ('course_id','course_name')})
        if not known:issue(cid,'weekly_plan','unknown','源周课时为0或缺少有效周计划，需要确认实际授课周与课时')
        if known:
            short={str(w):n-actual.get(w,0) for w,n in integer_expected.items() if actual.get(w,0)<n}
            extra={str(w):actual.get(w,0)-integer_expected.get(w,0) for w in actual if actual[w]>integer_expected.get(w,0)}
            if short or extra:
                if exact_hour_accept:complete=True
                issue(cid,'hours_or_weeks','accepted' if exact_hour_accept else 'violation','实际逐周课时与有效课节计划不一致',shortage=short,extra=extra,evidence=ev.get('hour_difference'))
        completeness.append({'course_id':cid,'complete':complete,'known':known,'source_weekly_load':source_dict,'effective_integer_load':{str(w):n for w,n in integer_expected.items()},'actual_weekly_load':actual_dict})
        covered=set().union(*c.teachers.values()) if c.teachers else set()
        if not c.weeks<=covered:issue(cid,'teacher_responsibility','violation','教师责任周未覆盖课程授课周',weeks=sorted(c.weeks-covered))
        if c.issues:
            relevant=[x for x in c.issues if not str(x).startswith(('coverage:','metadata: inconsistent TJBJ','metadata: inconsistent IF_CLASS_CONFICT','metadata: inconsistent LLXS')) and 'invalid LLXS:' not in str(x)]
            if relevant:issue(cid,'source_data','unknown','源课程存在待核实字段',source_issues=relevant)
        origin=origins.get(cid,ev.get('origin',{}));additional=windows(origin.get('additional_forbidden',''),cid,'additional_forbidden')
        forbidden_school=[{'week':w,'day':d,'period':p} for w,d,p in sorted(cells) if (d,p) in school]
        if forbidden_school:issue(cid,'school_unavailable','violation','进入校级周二5–8节禁排',cells=forbidden_school)
        if additional:
            hit=[{'week':w,'day':d,'period':p} for w,d,p in sorted(cells) if (d,p) in additional]
            if hit:issue(cid,'additional_forbidden','violation','进入当次额外禁排时段',cells=hit)
        by_week=defaultdict(list)
        for entry in entries:
            if entry.periods:
                for w in entry.weeks:by_week[w].append((entry.day,min(entry.periods)-1,len(entry.periods)))
        source_rules=base.teacher_time_rules or ({'JXBID':cid,'JSH':'','ZXS':base.hours,'Prefer_Time':base.prefer,'unavailable_Time':base.unavailable,
            'available_Time':'','RWJSZCDM':'1'*53,'SKZCDM':'1'*53},)
        applicable_pref=set();bad_pref=set();unavailable_hits=set();original_pref_unknown=False
        for rule in source_rules:
            active=(flags(rule.get('RWJSZCDM'))&flags(rule.get('SKZCDM')))
            pref=rule.get('Prefer_Time') or '';ban=windows(rule.get('unavailable_Time',''),cid,'course_unavailable');available_raw=rule.get('available_Time') or '';avail=windows(available_raw,cid,'available_time')
            pref_window=windows(pref,cid,'time_preference') if not any(x in pref for x in ('[','【','［')) else None
            for w,plan in by_week.items():
                if w-1 not in active:continue
                wcells={(w,d,p+1) for d,start,n in plan for p in range(start,start+n)}
                if ban is not None:unavailable_hits |= {cell for cell in wcells if (cell[1],cell[2]) in ban}
                if available_raw and avail is not None:
                    outside=sorted(cell for cell in wcells if (cell[1],cell[2]) not in avail)
                    if outside:issue(cid,'available_time','violation','超出明确可用时段',teacher_id=rule.get('JSH'),cells=outside)
                if pref:
                    applicable_pref |= wcells
                    if pref_window is not None:bad_pref |= {cell for cell in wcells if (cell[1],cell[2]) not in pref_window}
                    key=(pref,rule.get('ZXS'),tuple(sorted(set(plan))))
                    try:
                        if key not in time_cache:time_cache[key]=[r for r,_ in rule_errors({**rule,'unavailable_Time':'','available_Time':''},list(sorted(set(plan))),w-1)]
                        if 'outside_preferred' in time_cache[key]:
                            if pref_window is None or not any(cell in bad_pref for cell in wcells):bad_pref |= wcells
                    except (ValueError,TypeError,KeyError,AttributeError):original_pref_unknown=True
        if unavailable_hits:
            confirmed=bool(ev.get('course_unavailable',{}).get('confirmed'))
            issue(cid,'course_unavailable','accepted' if confirmed else 'violation','实际进入原课程禁排时段',cells=[list(x) for x in sorted(unavailable_hits)],evidence=ev.get('course_unavailable'))
            tags.append('课程禁排变更')
        if applicable_pref:
            preferred_courses+=1
            if original_pref_unknown:preferred_unknown+=1
            else:
                preference_total+=1;preference_good+=not bad_pref;preference_cells+=len(applicable_pref);pref_fit_cells+=len(applicable_pref-bad_pref)
            if bad_pref:
                issue(cid,'time_preference','accepted' if ev.get('preference') else 'warning','偏离原时间偏好',deviation_cells=len(bad_pref),applicable_cells=len(applicable_pref),deviation_percent=ratio(len(bad_pref),len(applicable_pref)),evidence=ev.get('preference'))
                tags.append('时间偏好偏离')
        if ev.get('one_off_week'):issue(cid,'one_off_week','accepted','已逐课确认一次性授课周',evidence=ev['one_off_week']);tags.append('授课周确认')
        assigned_rooms=set();room_buildings=set();weekly_blocks=Counter((w,e.day) for e in entries for w in e.weeks)
        default_daily=origin.get('max_blocks_per_day');default_weekly=origin.get('max_weekly_hours');limits=ev.get('block_limits',{})
        daily_cap=limits.get('max_blocks_per_day',default_daily);weekly_cap=limits.get('max_weekly_hours',default_weekly)
        if daily_cap and any(n>daily_cap for n in weekly_blocks.values()):issue(cid,'daily_block_limit','violation','超过本课适用的每日课块数',cap=daily_cap,loads=[{'week':w,'day':d,'blocks':n} for (w,d),n in weekly_blocks.items() if n>daily_cap])
        if weekly_cap and any(n>weekly_cap for n in actual.values()):issue(cid,'weekly_hours_limit','violation','超过本课适用的每周节次上限',cap=weekly_cap)
        if limits:issue(cid,'concentrated_teaching','accepted','使用已有依据的逐课集中教学参数',evidence=limits);tags.append('集中教学')
        for entry in entries:
            if not entry.periods:continue
            if not block_allowed(c,entry.day,entry.periods,entry.weeks):issue(cid,'block_length','violation','课块长度不符合源规则',periods=list(entry.periods),day=entry.day,weeks=sorted(entry.weeks))
            room=dataset.rooms.get(entry.room_id);assigned_rooms.add(entry.room_id)
            if room is None:issue(cid,'room_metadata','unknown','教室代码缺少源资源记录',room_id=entry.room_id);continue
            room_buildings.add(room.building)
            rejections=room_rejections(c,room,set())
            if not c.campus or not room.campus:issue(cid,'campus','unknown','课程或教室校区缺失',room_id=room.id)
            elif room.campus!=c.campus:issue(cid,'campus','violation','课程与教室不在同校区',course_campus=c.campus,room_campus=room.campus)
            for reason in rejections:
                if reason=='campus':continue
                if reason in ('capacity','room_disabled','explicit_room','room_type'):issue(cid,reason,'unknown' if reason=='capacity' and (not room.capacity or not c.capacity) else 'violation','教室条件不满足：'+reason,room_id=room.id,demand=c.capacity,seats=room.capacity)
                else:issue(cid,reason,'warning','教室原偏好未满足：'+reason,room_id=room.id)
            load=len(entry.weeks)*len(entry.periods)
            capacity_rows.append({'course_id':cid,'course_name':c.name,'campus':c.campus,'room_id':room.id,'room_name':room.name,'room_type':room.type,
                'demand':c.capacity,'current_enrollment':c.current_enrollment,'original_demand':base.capacity,'seats':room.capacity,'teaching_cells':load,
                'match_percent':ratio(c.capacity,room.capacity) if c.capacity>0 and room.capacity>0 else None,
                'basis':'historical_assumption' if ev.get('capacity_assumption') else 'current_actual' if c.current_enrollment is not None else 'source_demand',
                'requires_confirmation':bool(ev.get('capacity_assumption'))})
            block_hist[len(entry.periods)]+=len(entry.weeks)
        if ev.get('capacity_assumption'):
            issue(cid,'capacity_assumption','pending','当前安排仍依赖未核实人数假设',evidence=ev['capacity_assumption']);tags.append('人数假设')
            if c.current_enrollment is not None and c.capacity<c.current_enrollment:issue(cid,'actual_enrollment_floor','violation','人数假设低于当前源表实际人数',demand=c.capacity,current_enrollment=c.current_enrollment)
        resource=ev.get('resource',{});oldtype=resource.get('original',{}).get('room_type',base.room_type)
        if oldtype and oldtype!=c.room_type:
            issue(cid,'room_type_replacement','accepted' if resource else 'unknown','教室类型发生变更',original_type=oldtype,current_type=c.room_type,evidence=resource or None)
            issue(cid,'facility_suitability','pending','已接受类型替代方案，具体设备适用性仍需业务核实',original_type=oldtype,current_type=c.room_type)
            tags.append('教室类型替代')
        ecells=sum(p>=9 for w,d,p in cells);wcells=sum(d>=5 for w,d,p in cells);icells=sum(d>=5 and p>=9 for w,d,p in cells)
        all_cells+=len(cells);evening+=ecells;weekend+=wcells;intersection+=icells
        for w,d,p in cells:days_load[(w,d)]+=1;heat[(d,p)]+=1
        for label,amount,grant in [('晚间',ecells,origin.get('allow_evening')),('周末',wcells,origin.get('allow_weekend'))]:
            if amount:
                tags.append(label)
                if grant is False:issue(cid,'operation_domain','violation','实际安排超出保存时明确关闭的'+label+'范围',cells=amount)
        meetings=sorted((w,e.day,min(e.periods),e.room_id) for e in entries if e.periods for w in e.weeks)
        switches=sum(a[3]!=b[3] for a,b in zip(meetings,meetings[1:]))
        stability.append({'course_id':cid,'course_name':c.name,'rooms':sorted(assigned_rooms),'room_count':len(assigned_rooms),'additional_rooms':max(0,len(assigned_rooms)-1),'room_switches':switches,'building_count':len(room_buildings)})
        statuses=sorted({i['status'] for i in issues[start:]});dept=metadata.get('courses',{}).get(cid,{}).get('department','未知院系')
        source_window=windows(base.prefer,cid,'time_preference') if base.prefer and not any(x in base.prefer for x in ('[','【','［')) else None
        source_covered=bool(source_window) and all((d,p) in source_window for w,d,p in cells)
        segment_origin='source_window' if source_covered else 'later_introduced' if origin and not origin.get('is_root') else 'baseline_or_unspecified'
        item={'course_id':cid,'course_name':c.name,'campus':c.campus,'department':dept,'teacher_ids':sorted(c.teachers),
            'weeks':sorted(actual),'teaching_cells':len(cells),'complete':complete,'statuses':statuses,'tags':sorted(set(tags)),
            'original_preference':base.prefer,'original_unavailable':base.unavailable,'original_capacity':base.capacity,'effective_capacity':c.capacity,
            'evening_cells':ecells,'weekend_cells':wcells,'both_cells':icells,'segment_origin':segment_origin,'preference_cells':len(applicable_pref),
            'preference_deviation_cells':len(bad_pref),'evidence':ev,
            'placements':[{'weeks':sorted(e.weeks),'day':e.day,'periods':list(e.periods),'room_id':e.room_id,'room_name':dataset.rooms[e.room_id].name if e.room_id in dataset.rooms else e.room_id} for e in entries]}
        courses.append(item)
        if tags or statuses:relaxations.append(item)
    # Teacher workloads use responsibility weeks and do not multiply course counts for co-teaching.
    teacher_days=[];teacher_summary=[];teacher_names=metadata.get('teachers',{})
    for tid,slots in sorted(teacher_slots.items()):
        by_day=defaultdict(set);weekly=defaultdict(list)
        for w,d,p in slots:by_day[(w,d)].add(p)
        for (w,d),periods in sorted(by_day.items()):
            ordered=sorted(periods);runs=[];run=0;previous=None
            for p in ordered:
                if previous is None or p!=previous+1 or p in (5,9):
                    if run:runs.append(run)
                    run=1
                else:run+=1
                previous=p
            if run:runs.append(run)
            # Gaps count instructional periods only; fixed clock-time lunch is not a numbered slot.
            gaps=max(ordered)-min(ordered)+1-len(ordered)
            row={'teacher_id':tid,'teacher_name':teacher_names.get(tid,{}).get('name',tid),'week':w,'day':d,'periods':ordered,
                'teaching_cells':len(periods),'longest_consecutive_periods':max(runs),'first_period':min(ordered),'last_period':max(ordered),
                'period_span':max(ordered)-min(ordered)+1,'gap_periods':gaps,'single_block_day':len(runs)==1,
                'evening_cells':sum(p>=9 for p in periods),'weekend_cells':len(periods) if d>=5 else 0}
            teacher_days.append(row);weekly[w].append(row)
        teacher_summary.append({'teacher_id':tid,'teacher_name':teacher_names.get(tid,{}).get('name',tid),
            'department':teacher_names.get(tid,{}).get('department') or '未知院系','teaching_cells':len(slots),
            'evening_cells':sum(p>=9 for w,d,p in slots),'weekend_cells':sum(d>=5 for w,d,p in slots),
            'undesirable_cells':sum(d>=5 or p>=9 for w,d,p in slots),
            'evening_percent':ratio(sum(p>=9 for w,d,p in slots),len(slots)),
            'weekend_percent':ratio(sum(d>=5 for w,d,p in slots),len(slots)),
            'teaching_days_by_week':{str(w):len(weekly.get(w,[])) for w in sorted(set().union(*(c.teachers.get(tid,frozenset()) for cid,c in dataset.courses.items() if cid in scope)))},
            'longest_consecutive_periods':max(r['longest_consecutive_periods'] for ds in weekly.values() for r in ds),
            'single_block_days':sum(r['single_block_day'] for ds in weekly.values() for r in ds),
            'gap_periods':sum(r['gap_periods'] for ds in weekly.values() for r in ds)})
    # Calendar-normalized reference rates remain separate from unknown real opening hours.
    weeks=sorted({w for cid in scope if cid in dataset.courses for w in dataset.courses[cid].weeks}|{w for slots in room_slots.values() for w,d,p in slots})
    room_details=[];room_groups=defaultdict(lambda:{'used':0,'available':0,'rooms':0})
    for rid,room in sorted(dataset.rooms.items()):
        if not room.enabled:continue
        occupied=room_slots.get(rid,set());opening=room_openings.get(rid) if room_openings is not None else None
        denominators={'weekday_daytime':{(w,d,p) for w in weeks for d in range(5) for p in range(1,9) if (d,p) not in school},
            'evening':{(w,d,p) for w in weeks for d in range(7) for p in range(9,12)},
            'weekend':{(w,d,p) for w in weeks for d in (5,6) for p in range(1,12)}}
        row={'room_id':rid,'room_name':room.name,'campus':room.campus,'room_type':room.type,'capacity':room.capacity,'occupied_cells':len(occupied),
             'actual_opening_known':opening is not None,'actual_utilization_percent':ratio(len(occupied&set(opening)),len(opening)) if opening is not None else None,
             'outside_opening_cells':len(occupied-set(opening)) if opening is not None else None,'reference_rates':{}}
        for name,domain in denominators.items():
            row['reference_rates'][name]={'used':len(occupied&domain),'available':len(domain),'percent':ratio(len(occupied&domain),len(domain))}
            g=room_groups[(room.campus,room.type,name)];g['used']+=len(occupied&domain);g['available']+=len(domain);g['rooms']+=1
        room_details.append(row)
    group_rows=[{'campus':campus,'room_type':kind,'segment':seg,**v,'percent':ratio(v['used'],v['available'])} for (campus,kind,seg),v in sorted(room_groups.items())]
    usable=[r for r in capacity_rows if r['match_percent'] is not None]
    weighted=sum(r['match_percent']*r['teaching_cells'] for r in usable);weight=sum(r['teaching_cells'] for r in usable)
    fairness=[]
    for level,groups in [('teacher',[(t['teacher_id'],[t]) for t in teacher_summary]),
                          ('department',[(dept,[t for t in teacher_summary if t['department']==dept]) for dept in sorted({t['department'] for t in teacher_summary})])]:
        for name,group in groups:
            total=sum(t['teaching_cells'] for t in group);ev=sum(t['evening_cells'] for t in group);we=sum(t['weekend_cells'] for t in group)
            fairness.append({'level':level,'group':name,'teachers':len(group),'teaching_cells':total,'evening_cells':ev,'weekend_cells':we,
                'evening_percent':ratio(ev,total),'weekend_percent':ratio(we,total)})
    for change in history_changes:
        for message in change['violations']:issue('','movement_authorization','violation',message,history=change)
    statuses_by=defaultdict(set)
    for row in issues:
        if row['course_id']:statuses_by[row['course_id']].add(row['status'])
    for row in courses:row['statuses']=sorted(statuses_by[row['course_id']])
    unique_status={status:sorted({i['course_id'] for i in issues if i['status']==status and i['course_id']}) for status in STATUS_NAMES}
    baseline_comparison=None
    if baseline_rows is not None:
        before=grouped_rows(baseline_rows);after=grouped_rows([{'course_id':p.course_id,'weeks':sorted(p.weeks),'day':p.day,'periods':list(p.periods),'room_id':p.room_id} for p in dataset.placements])
        changed=[cid for cid in set(before)&set(after) if placement_signature(before[cid])!=placement_signature(after[cid])]
        before_even=sum(len(p['weeks'])*sum(k>=9 for k in p['periods']) for p in baseline_rows)
        before_weekend=sum(len(p['weeks'])*len(p['periods']) for p in baseline_rows if p['day']>=5)
        baseline_comparison={'inserted_ids':sorted(set(after)-set(before)),'removed_ids':sorted(set(before)-set(after)),'changed_ids':sorted(changed),
            'evening_cells_before':before_even,'evening_cells_after':evening,'weekend_cells_before':before_weekend,'weekend_cells_after':weekend,
            'note':'跨多个操作的累计变化不受单次移动上限直接约束；源数据或规则变化另列，不宣称算法优劣。'}
    metrics={
        'complete_courses':metric('逐周完整排课率',sum(r['complete'] for r in completeness),len(scope),'逐周符合有效课节计划（含明确确认差异）的教学班数/有效任务范围数'),
        'preference_courses':metric('原偏好完全满足率',preference_good,preference_total,'完全满足原偏好的已排教学班/有可评价原偏好的已排教学班'),
        'preference_cells':metric('原偏好课节满足率',pref_fit_cells,preference_cells,'满足该周适用原偏好的课节/有适用原偏好的课节'),
        'evening':metric('晚间课节占比',evening,all_cells,'第9–11节实际教学节次/全部实际教学节次'),
        'weekend':metric('周末课节占比',weekend,all_cells,'周六日实际教学节次/全部实际教学节次'),
        'evening_or_weekend':metric('晚间或周末课节占比',evening+weekend-intersection,all_cells,'晚间与周末并集/全部教学节次；交集只计一次'),
        'room_stability':metric('单教室稳定率',sum(r['room_count']==1 for r in stability),len(stability),'只使用一间教室的教学班/可评价已排教学班'),
        'capacity_weighted':{'name':'按课节加权需求容量匹配率','value':round(weighted/weight,4) if weight else None,'numerator':round(weighted,4),'denominator':weight,
            'definition':'各安排需求人数/座位数×实际教学节次数，再按教学节次加权平均；不是实际上座率','unit':'%','status':'available' if weight else 'not_applicable'},
        'capacity_coverage':metric('容量数据覆盖率',len(usable),len(capacity_rows),'需求和座位均有效的安排条目/全部安排条目'),
        'room_opening_coverage':metric('真实开放时段覆盖率',sum(r['actual_opening_known'] for r in room_details),len(room_details),'已知真实开放时段的启用教室/全部启用教室')}
    return {'schema_version':SCHEMA_VERSION,'read_only':True,'summary':{'scope_courses':len(scope),'recorded_courses':len(rows),'complete_courses':sum(r['complete'] for r in completeness),
        'remaining_courses':len(remaining),'teaching_cells':all_cells,'teachers':len(teacher_slots),'used_rooms':len(room_slots),'enabled_rooms':len(room_details),
        'issue_counts':dict(Counter(i['status'] for i in issues)),'course_counts_by_status':{k:len(v) for k,v in unique_status.items()},
        'status_sets_overlap':True,'hard_constraints_passed':not any(i['status']=='violation' for i in issues),'fully_verified':not any(i['status'] in ('violation','pending','unknown') for i in issues),
        'weekly_plan_differences':len(hour_differences),'confirmed_hour_differences':sum(r['confirmed'] for r in hour_differences),
        'pending_hour_differences':sum(not r['confirmed'] for r in hour_differences),'rounding_only_differences':sum(r['rounding_only'] for r in hour_differences),
        'evening_weekend_intersection_cells':intersection},'metrics':metrics,'issues':issues,'courses':courses,'relaxations':relaxations,
        'completeness':completeness,'hour_differences':hour_differences,'conflicts':conflicts,'capacity_details':capacity_rows,
        'teacher_days':teacher_days,'teachers':teacher_summary,'room_details':room_details,'room_groups':group_rows,'room_stability':stability,
        'fairness':fairness,'history_changes':list(history_changes),'baseline_comparison':baseline_comparison,
        'segment_origins':[{"origin":origin,"courses":sum(c['segment_origin']==origin for c in courses),"evening_cells":sum(c['evening_cells'] for c in courses if c['segment_origin']==origin),"weekend_cells":sum(c['weekend_cells'] for c in courses if c['segment_origin']==origin)} for origin in ('source_window','later_introduced','baseline_or_unspecified')],
        'charts':{'heatmap':[{'day':d,'period':p,'teaching_cells':heat[(d,p)]} for d in range(7) for p in range(1,12)],
                  'daily_load':[{'week':w,'day':d,'teaching_cells':n} for (w,d),n in sorted(days_load.items())],
                  'block_lengths':[{'length':n,'meetings':count} for n,count in sorted(block_hist.items())]},
        'distributions':{'teacher_daily_load':distribution([r['teaching_cells'] for r in teacher_days]),
            'teacher_daily_gaps':distribution([r['gap_periods'] for r in teacher_days]),
            'teacher_weekly_teaching_days':distribution([days for t in teacher_summary for days in t['teaching_days_by_week'].values()]),
            'teacher_weekend_percent':distribution([t['weekend_percent'] for t in teacher_summary]),
            'teacher_evening_percent':distribution([t['evening_percent'] for t in teacher_summary])},
        'coverage':{'preferred_courses':preferred_courses,'unparsed_preference_courses':preferred_unknown,
            'teacher_department_known':sum(t['department']!='未知院系' for t in teacher_summary),'teacher_count':len(teacher_summary)},
        'limitations':['按用户决定不检查班级或学生冲突。','缺真实选课人数时仅报告需求容量匹配，不报告实际上座率。',
            '教室开放未知时仅给参考日历占用率，不声称真实时间利用率。','教师连续、跨度和空档按节次统计，午休与夜间分段；缺实际时刻不推算分钟或课间步行可达性。',
            '已确认调整与待核实条件可能属于同一课程，各状态课程数不可简单相加。','本报告不自动确认差异或修改课表。']}
