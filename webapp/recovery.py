"""Recover committed version pointers; never restore an interrupted search stack."""
from copy import deepcopy
import json
from pathlib import Path
import re
import time
import math

try:
    from .initial_schedule import write_json
except ImportError:
    from initial_schedule import write_json
from scripts.timetable_state import StateStore

RUN_ID = re.compile(r'[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}')


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {} if default is None else default


def checkpoint(run_dir, state, input_state_id=None, phase='running', **details):
    write_json(Path(run_dir)/'checkpoint.json', {'run_id': Path(run_dir).name,
        'state_id': state['state_id'], 'input_state_id': input_state_id,
        'phase': phase, **details})


def recover_interrupted(data_root, run_id=None):
    data_root = Path(data_root)
    store = StateStore(data_root/'timetable-state')
    # A process may stop after atomic publication but before its checkpoint.
    owned = {}
    for path in store.states.glob('*/state.json'):
        body = read_json(path)
        owner_id = body.get('operation', {}).get('run_id')
        if owner_id:
            owned.setdefault(owner_id, []).append(path.parent.name)
    folders = {p for p in (data_root/'agent-runs').glob('*') if p.is_dir() and
               ((p/'worker.json').is_file() or (p/'checkpoint.json').is_file())}
    folders.update(data_root/'agent-runs'/p.stem for p in (data_root/'jobs').glob('*.json') if RUN_ID.fullmatch(p.stem))
    if run_id is not None:
        if not RUN_ID.fullmatch(run_id):
            raise ValueError("无效的运行编号")
        folders = {data_root/"agent-runs"/run_id}
    recovered = []
    for folder in sorted(folders):
        if (folder/'result.json').is_file():
            continue
        folder.mkdir(parents=True, exist_ok=True)
        request = read_json(folder/'request.json') or read_json(data_root/'jobs'/(folder.name+'.json')).get('request', {})
        cp = read_json(folder/'checkpoint.json')
        initial = read_json(folder/'initial-result.json')
        ids = set(owned.get(folder.name, []))
        ids.update(v for v in (cp.get('state_id'), initial.get('output_state_id'), request.get('state_id')) if v)
        valid, invalid = {}, []
        for sid in ids:
            try:
                valid[sid] = store.load(sid)
            except (OSError, ValueError, KeyError):
                invalid.append(sid)
        parents = {state.get('parent_state_id') for state in valid.values()}
        leaves = sorted(set(valid)-parents)
        # Never silently choose a different branch by filesystem timestamp.
        state = valid[leaves[0]] if len(leaves) == 1 else None
        planner = request.get('planner')
        intent = 'agent' if planner == 'agent' else request.get('intent', 'initial_schedule')
        plan = read_json(folder/'plan.json') or {'intent': intent, 'selected_tools': []}
        resume_action = ('agent' if planner == 'agent' else 'legacy_reschedule'
                         if intent in ('initial_schedule','legacy_reschedule') and
                         (intent == 'legacy_reschedule' or request.get('tool_arguments', {}).get('initial_parameters', {}).get('auto_reschedule'))
                         else 'repair')
        if state:
            message = '任务因服务重启中断，已找回最后成功保存的课表版本，可从该版本继续。'
        elif len(leaves) > 1:
            message = '任务存在多个已保存分支，请明确选择版本后继续；系统未自动选择。'
        else:
            message = '任务中断前尚无可读取的已保存版本，需要重新提交；未保存的搜索过程无法恢复。'
        result = {'ok': False, 'status': 'interrupted', 'plan': plan,
                  'run_dir': str(folder), 'publication_ready': False,
                  'error': {'message': message}, 'tool_calls': 0,
                  'recovery': {'valid_state_ids': sorted(valid), 'invalid_state_ids': invalid,
                               'uncommitted_search_restored': False},
                  'resume': {'allowed': bool(state and state['remaining_course_ids'] and request and RUN_ID.fullmatch(folder.name)),
                             'action': resume_action, 'source_run_id': folder.name}}
        if state:
            result.update(input_state_id=cp.get('input_state_id') or request.get('state_id'),
                          output_state_id=state['state_id'], preserved_state_id=state['state_id'],
                          initial_state_id=initial.get('initial_state_id') or initial.get('output_state_id'),
                          source_hashes=state['source_hashes'])
            assigned = {row['course_id'] for row in state['placements']}
            result['final_response'] = {'ok': True, 'result': {'summary': {
                'inserted': len(assigned), 'remaining': len(state['remaining_course_ids']),
                'recovered_snapshot': True}}}
        if request and not (folder/'request.json').exists():
            write_json(folder/'request.json', request)
        write_json(folder/'result.json', result)
        write_json(folder/'progress.json', {**read_json(folder/'progress.json'),'status':'interrupted','message':message})
        recovered.append(result)
    return recovered


def make_resume_request(data_root, source_run_id, new_run_id):
    if not RUN_ID.fullmatch(source_run_id) or not RUN_ID.fullmatch(new_run_id):
        raise ValueError('无效的运行编号')
    root = Path(data_root); folder = root/'agent-runs'/source_run_id
    result = read_json(folder/'result.json')
    if result.get('status') not in ('interrupted', 'cancelled') or not result.get('resume', {}).get('allowed'):
        raise ValueError('此任务没有可续接的中断版本，请选择已有课表版本继续')
    original = read_json(folder/'request.json')
    store = StateStore(root/'timetable-state'); state = store.load(result['output_state_id'])
    action = result['resume']['action']
    authorization = {k:v for k,v in original.get('authorization', {}).items()
                     if k in ('allow_evening','allow_weekend','publication')}
    request = {'request': original.get('request') or '从中断前保存的版本继续处理',
               'run_id':new_run_id, 'state_id':state['state_id'], 'state_root':str(store.root),
               'authorization':authorization, 'planner':'agent' if action=='agent' else 'deterministic'}
    raw_config = original.get('tool_arguments', {}).get('config', {})
    if action == 'legacy_reschedule':
        auto = read_json(folder/'legacy-reschedule'/'request.json')
        config = deepcopy(auto.get('config') or raw_config)
        if original.get('intent') == 'initial_schedule' and not auto:
            # The initial workflow explicitly authorized moving its initial placements.
            config = {'movable_ids':sorted({r['course_id'] for r in state['placements']}),
                      'max_moved_courses':len({r['course_id'] for r in state['placements']})}
        # A bounded legacy grant also belongs to the interrupted task, not
        # to each continuation independently. Unlimited-within-list grants can
        # safely reuse that same fixed list without increasing distinct moves.
        granted = set(config.get('movable_ids', []))
        cap = int(config.get('max_moved_courses', 0))
        if cap < len(granted):
            input_id = original.get('state_id') or read_json(folder/'checkpoint.json').get('input_state_id')
            if input_id:
                used, cursor, visited = set(), state, set()
                while cursor['state_id'] != input_id and cursor['state_id'] not in visited:
                    visited.add(cursor['state_id'])
                    used.update(set(cursor.get('validation', {}).get('moved_ids', [])) & granted)
                    parent = cursor.get('parent_state_id')
                    if not parent: break
                    cursor = store.load(parent)
                config['max_moved_courses'] = max(0, cap-len(used))
                config['movable_ids'] = sorted(granted-used)
        resolved = store.resolve_policy(state, config)
        request.update(intent='legacy_reschedule',tool_arguments={'config':resolved})
    else:
        from scripts.repair_courses import RepairConfig
        from dataclasses import fields
        resolved = store.resolve_policy(state, raw_config)
        names = {f.name for f in fields(RepairConfig)}
        config = {k:v for k,v in resolved.items() if k in names}
        config['target_ids'] = []
        request['tool_arguments'] = {'config':config}
        if action == 'agent':
            options = deepcopy(original.get('agent_options', {}))
            input_id = read_json(folder/'checkpoint.json').get('input_state_id') or original.get('state_id')
            baseline = store.load(input_id) if input_id else None
            original_ids = {r['course_id'] for r in baseline['placements']} if baseline else set()
            moved, cursor, visited = set(), state, set()
            while cursor['state_id'] != input_id and cursor['state_id'] not in visited:
                visited.add(cursor['state_id'])
                moved.update(set(cursor.get('validation', {}).get('moved_ids', [])) & original_ids)
                parent = cursor.get('parent_state_id')
                if not parent: break
                cursor = store.load(parent)
            options['max_moved_courses'] = max(0, options.get('max_moved_courses',0)-len(moved))
            options['locked_ids'] = sorted(set(options.get('locked_ids',[])) | moved)
            options['movable_ids'] = sorted(set(options.get('movable_ids',[]))-moved)
            decisions = read_json(folder/'decisions.json', [])
            observed = [int(p.stem.split('-')[-1]) for p in folder.glob('observation-*.json') if p.stem.split('-')[-1].isdigit()]
            consumed = max([0]+observed+[d.get('round',0) for d in decisions])
            remaining_rounds = options.get('max_rounds',6)-consumed
            if remaining_rounds < 1:
                raise ValueError('原任务决策轮数已用完；已保留课表版本，请设置新预算后另行继续')
            options['max_rounds'] = remaining_rounds
            started_at = read_json(folder/'checkpoint.json').get('started_at')
            if started_at is not None:
                seconds = options.get('total_time_limit_seconds',240)-math.ceil(max(0,time.time()-started_at))
                if seconds < 10:
                    raise ValueError('原任务时间预算已用完；课表版本已保留，请设置新预算后继续')
                options['total_time_limit_seconds'] = seconds
            options['resume_run_id'] = ''  # Interrupted candidates are regenerated for this parent.
            options['context'] = (options.get('context','')+'\n从服务中断前的最后已保存版本继续；已完成步骤不要重复。')[-6000:]
            request['agent_options'] = options
            if original.get('rule_pack') is not None: request['rule_pack']=original['rule_pack']
        else:
            request['intent'] = 'repair'
    return request
