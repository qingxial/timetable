"""Isolated first scheduling job; publishes a validated root to the shared store."""
from __future__ import annotations
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import time


# 原调课引擎结果的验收口径（用户确认）：偏好时间是软约束，偏离只记警告；
# 原引擎多次失败后会放宽班级冲突，验收不查班级。教师、教室、校区、禁排仍是硬约束。
LEGACY_ACCEPTANCE = {
    'preference_as_soft': True,
    'check_class_conflicts': False,
    'class_conflict_exemption_evidence': '用户确认：原调课引擎多次失败后放宽班级冲突，原调课结果验收不检查班级冲突。',
}


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def validate_output(dataset, rows, targets, config, weeks, *, original_contract=False):
    from dataclasses import asdict
    from scripts.timetable_acceptance import validate_transition
    return validate_transition(dataset, [], rows, targets, sorted(set(targets) - {r['course_id'] for r in rows}),
                               {**asdict(config), **({'time_policy': 'test_for_school'} if original_contract else {})}, weeks=weeks, full=True)


def worker(spec):
    sys.path.insert(0, spec['project_root'])
    from openpyxl import Workbook
    from scripts.repair_data import _read_table, _text, merge_room_rows
    import Basic_Data as basic
    import utils1 as engine
    from scripts.teacher_time_constraints import placements_from_result
    root = Path(spec['run_dir'])
    inputs = spec['sources']
    empty = root / 'empty.xlsx'
    book = Workbook(); book.active.append(['教学班ID', '教室代码', '上课周次', '星期', '节次']); book.save(empty); book.close()
    policy = spec['parameters']
    # The CLI's own loader and pre_selection own eligibility. Do not run the
    # modern repair model's coverage/special-text/hour gates before this engine.
    courses = basic.load_courses(inputs['courses'], policy['weeks'], 7, 11)
    if not courses:
        raise ValueError('课程表没有可识别的教学班')
    # Reporting scope only; every loaded row is still passed to the original
    # pre_selection unchanged, including rows that explicitly need no schedule.
    excluded = {str(c.JXBID) for c in courses if str(c.SFXYPK).strip() in ('0', '0.0')}
    required = {str(c.JXBID) for c in courses if str(c.SFXYPK).strip() not in ('0', '0.0')}
    excluded -= required
    skipped = [{'course_id': cid, 'reason': '源表明确不需排课'} for cid in sorted(excluded)]
    targets = list(dict.fromkeys(str(c.JXBID) for c in courses if str(c.JXBID) not in excluded))
    # Duplicate IDs are one physical room, as previously requested by the owner.
    # Contradictory room facts cannot be resolved by guessing.
    room_rows = merge_room_rows(_read_table(Path(inputs['rooms']), True))
    room_meta = {_text(row.get('JASDM')): row for _, row in room_rows}
    rooms = []; seen_rooms = set()
    for room in basic.load_classrooms(inputs['rooms'], policy['weeks'], 7, 11):
        rid = _text(room.JASDM)
        if rid in seen_rooms: continue
        room.JASDM = rid
        room.JASMC = room_meta[rid]['JASMC']
        rooms.append(room); seen_rooms.add(rid)
    teachers=basic.load_teachers(inputs['teachers'],policy['weeks'],7,11)
    classes=basic.load_classes(inputs['classes'],policy['weeks'],7,11)
    engine.set_placement_strategy(policy['strategy'], policy['seed'])
    # Reuse the original combination -> cluster -> batch -> dynamic MCF orchestration.
    # Only explicit snapshots are read; never use the CLI's hard-coded source paths.
    import pandas as pd
    import test_for_school as legacy
    combinations_path = inputs.get('combinations')
    clustering_path = inputs.get('clustering')
    if combinations_path:
        combinations = pd.read_excel(combinations_path)
        if not {'开课单位', '课程类别'} <= set(combinations.columns):
            raise ValueError('筛选组合表须包含开课单位、课程类别')
    else:
        combinations = pd.DataFrame([{'开课单位': None, '课程类别': None}])
    if clustering_path:
        cluster_table = pd.read_excel(clustering_path)
        if not {'JXBID', '类别ID', '类别名称'} <= set(cluster_table.columns):
            raise ValueError('聚类结果表须包含 JXBID、类别ID、类别名称')
    else:
        clustering_path = str(root / 'no-clustering-supplied.xlsx')
    original_round = legacy.schedule_one_round
    original_cluster = legacy.schedule_cluster
    original_batch = legacy.schedule_batch
    import inspect
    batch_signature = inspect.signature(original_batch)
    completed_batches = 0
    cluster_name = None
    manifest_path = root / "manifest.json"
    checkpoint_sources = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    attempted, selection_order, batches = set(), [], []
    from scripts.timetable_progress import emit
    processed=set(); scanned_at=[0.0]
    def course_progress(cid, outcome, message):
        processed.add(cid)
        emit(root,'initial',message,completed=len(processed),total=len(targets),course_id=cid,outcome=outcome)
    emit(root,'initial','开始按原批次首排；百分比为处理进度，不是成功率',completed=len(processed),total=len(targets))
    def observe(cid, phase):
        now=time.monotonic()
        if phase=='assessing' and now-scanned_at[0]<1:return
        scanned_at[0]=now
        emit(root,'initial',f'{cid}：'+('正在评估候选时段' if phase=='assessing' else '开始放置课程'),
             completed=len(processed),total=len(targets),course_id=cid,append=phase!='assessing')

    def selected_round(ids, *args, **kwargs):
        emit(root,'initial',f'批内选课：剩余 {len(ids)} 门，正在计算可行方案',completed=len(processed),total=len(targets))
        result = original_round(ids, *args, **kwargs, progress_callback=observe)
        success, cid, _, _, _ = result
        if cid:
            attempted.add(cid)
            selection_order.append({'course_id': cid, 'success': bool(success)})
            course_progress(cid,'placed' if success else 'unplaced',f'{cid}：'+('已生成安排，待统一验收' if success else '本轮未找到可行安排'))
        else:
            attempted.update(ids)
            for missing in ids:
                if missing not in processed:course_progress(missing,"unplaced",f"{missing}：本批次未找到可行安排")
        return result

    def checkpoint_batch(*args, **kwargs):
        nonlocal completed_batches
        result = original_batch(*args, **kwargs)
        bound = batch_signature.bind(*args, **kwargs).arguments
        placements = []
        for cid, room, arrangement, teaching_weeks in bound['all_scheduling_results']:
            placements.extend(placements_from_result(cid, room, arrangement, teaching_weeks))
        placed = {row['course_id'] for row in placements}
        snapshot = {
            'schema': 'initial_batch_checkpoint.v1', 'saved_at': time.time(),
            'completed_batch_count': completed_batches + 1,
            'last_batch': {'cluster': cluster_name, 'department': str(bound['department']),
                           'course_type': str(bound['course_type']),
                           'course_ids': list(bound['batch_jxbids'])},
            'placements': placements,
            'remaining_course_ids': sorted(set(targets) - placed),
            'processed_course_ids': sorted(processed),
            'pending_course_ids': sorted(set(targets) - processed),
            'failed_course_ids': sorted({str(row[0]) for row in bound['all_failed_courses']}),
            'selection_order': list(selection_order),
            'random_state': engine._PLACEMENT_RNG.getstate(),
            'sources': inputs, 'parameters': policy,
            'engine_source_hashes': checkpoint_sources.get('engine_source_hashes', {}),
            'code_hashes': checkpoint_sources.get('code_hashes', {}),
            'validation': {'status': 'pending', 'published': False},
        }
        # Replace one complete file atomically. Never delete the previous good
        # checkpoint before the new snapshot has been written successfully.
        checkpoint_path = root / 'batch-checkpoint.json'
        try:
            write_json(checkpoint_path, snapshot)
        finally:
            checkpoint_path.with_suffix('.tmp').unlink(missing_ok=True)
        completed_batches += 1
        emit(root, 'initial', f'第 {completed_batches} 批检查点已保存（仅保留最新一份）',
             completed=len(processed), total=len(targets))
        return result

    def record_cluster(name, ids, *args, **kwargs):
        nonlocal cluster_name
        cluster_name = str(name)
        for start in range(0, len(ids), policy['batch_size']):
            batches.append({'cluster': str(name), 'course_ids': ids[start:start + policy['batch_size']]})
        return original_cluster(name, ids, *args, **kwargs)

    legacy.schedule_one_round = selected_round
    legacy.schedule_cluster = record_cluster
    legacy.schedule_batch = checkpoint_batch
    try:
        results, failed_courses, scheduled_count, elapsed = legacy.process_combinations(
            combinations, courses, teachers, classes, rooms, clustering_path,
            str(root / 'legacy-results'), policy['batch_size'], 7)
        # Preserve the same native artifacts as test_for_school.main, in this
        # isolated run only. They are evidence, never an implicit next input.
        legacy.save_final_results(results, failed_courses, scheduled_count, elapsed, courses, str(root / 'legacy-results'))
        legacy.save_timetables(courses, teachers, rooms, classes, str(root / 'legacy-results' / 'saved_timetables.pkl'))
    finally:
        legacy.schedule_one_round = original_round
        legacy.schedule_cluster = original_cluster
        legacy.schedule_batch = original_batch
    placements = []
    for cid, room, arrangement, teaching_weeks in results:
        placements.extend(placements_from_result(cid, room, arrangement, teaching_weeks))
    placed = {row['course_id'] for row in placements}
    for cid in targets:
        if cid not in processed:
            course_progress(cid,'unplaced',f'{cid}：未满足原引擎预筛选条件或未被筛选组合覆盖')
    emit(root,'initial','首排课程处理结束，正在统一验收；100% 不代表全部排入',completed=len(processed),total=len(targets))
    statuses = [{'course_id': cid, 'status': 'placed' if cid in placed else 'remaining',
                 **({} if cid in placed else {'reason': '原引擎未找到可行安排' if cid in attempted else '未满足原引擎预筛选条件或未被筛选组合覆盖'})}
                for cid in targets]
    write_json(root / 'batch-plan.json', {'batch_size': policy['batch_size'], 'batches': batches,
               'selection_order': selection_order, 'automatic_relaxation': True, 'time_policy': 'test_for_school', 'precheck': 'file_contract_only'})
    validation = {'status': 'pending', 'message': '原引擎计算完成，随后独立验收；验收不参与选课或搜索'}
    remaining = sorted(set(targets) - {r['course_id'] for r in placements})
    write_json(root / 'candidate.json', {'placements': placements, 'remaining_course_ids': remaining,
               'courses': statuses, 'skipped': skipped, 'validation': validation,
               'summary': {'requested': len(targets), 'inserted': len(targets)-len(remaining), 'remaining': len(remaining), 'skipped': len(skipped)}})


def continue_with_legacy(result, store, state, dataset, candidate, parameters, authorization, run_dir, code_hashes):
    from scripts.legacy_reschedule_adapter import run_legacy_reschedule, _cells
    from scripts.repair_courses import RepairConfig
    from scripts.timetable_state import file_hash
    initial_id = state['state_id']
    result['initial_state_id'] = initial_id
    if not state['remaining_course_ids']:
        result['automatic_reschedule'] = {'status': 'skipped', 'reason': '首排已无剩余课程', 'input_state_id': initial_id}
        return
    result['plan']['selected_tools'].append('legacy_reschedule')
    result['tool_calls'] += 1
    movable = sorted({p['course_id'] for p in state['placements']})
    config = {'days': parameters['days'], 'periods': parameters['periods'], 'weeks': parameters['weeks'],
              'movable_ids': movable, 'max_moved_courses': len(movable), **LEGACY_ACCEPTANCE}
    auth = {**authorization, 'auto_legacy_reschedule': True}
    audit = Path(run_dir) / 'legacy-reschedule'; audit.mkdir()
    write_json(audit / 'request.json', {'input_state_id': initial_id, 'config': config, 'authorization': auth})
    write_json(Path(run_dir) / 'initial-result.json', result)
    from scripts.timetable_progress import emit
    emit(run_dir,'legacy','首排版本已保存，正在启动原调课引擎',completed=0,total=len(state['remaining_course_ids']))
    output = None
    try:
        output = run_legacy_reschedule(store, initial_id, config, auth, artifact_dir=audit)
        write_json(audit / 'candidate.json', {k: v for k, v in output.items() if k != 'legacy_cache'})
        targets = set(state['remaining_course_ids']) | {p['course_id'] for p in state['placements']}
        after_ids = {p['course_id'] for p in output['placements']}
        if not {p['course_id'] for p in state['placements']} <= after_ids:
            raise ValueError('自动调课结果丢失已排课程，保留首排版本')
        if set(output['remaining_course_ids']) != targets - after_ids:
            raise ValueError('自动调课剩余课程对账失败，保留首排版本')
        validation = output.get('engine_validation', {})
        if code_hashes != {path: file_hash(path) for path in code_hashes}:
            raise ValueError('自动调课期间代码改变，保留首排版本')
        before_cells, after_cells = _cells(state['placements']), _cells(output['placements'])
        moved = sum(before_cells[cid] != after_cells[cid] for cid in before_cells)
        child = store.commit(initial_id, output['placements'], output['remaining_course_ids'],
                    {'engine': 'legacy', 'tool': 'reschedule_by_adjusting.reschedule',
                     'parameters': {'input_state_id': initial_id, 'config': config, 'authorization': auth},
                     'run_id': Path(run_dir).name, 'automatic_after_initial': True, 'code_hashes': code_hashes},
                    validation, output.get('legacy_cache'))
        final = {**candidate, 'placements': output['placements'], 'remaining_course_ids': output['remaining_course_ids'],
                 'validation': validation, 'summary': {**candidate['summary'],
                    'inserted': len(after_ids), 'remaining': len(output['remaining_course_ids']), 'moved': moved},
                 'courses': [{'course_id': cid, 'status': 'placed' if cid in after_ids else 'remaining'} for cid in sorted(targets)]}
        try:
            from .recovery import checkpoint
        except ImportError:
            from recovery import checkpoint
        checkpoint(run_dir, child, input_state_id=initial_id, phase='legacy_saved')
        result.update(output_state_id=child['state_id'], final_response={'ok': True, 'result': final})
        result['automatic_reschedule'] = {'status': 'completed', 'input_state_id': initial_id,
                                         'output_state_id': child['state_id'], 'moved': moved}
    except Exception as error:
        # A failed second stage cannot discard or replace the validated root.
        result['status'] = 'needs_attention'
        result['automatic_reschedule'] = {'status': 'failed', 'input_state_id': initial_id,
                                         'preserved_state_id': initial_id, 'error': {'message': str(error)}}
    finally:
        if output and output.get('legacy_cache'):
            Path(output['legacy_cache']).unlink(missing_ok=True)
    write_json(audit / 'result.json', result['automatic_reschedule'])


def run_initial(request, project_root, data_root):
    sys.path.insert(0, str(project_root))
    from scripts.timetable_state import StateStore, file_hash
    from scripts.repair_data import load_dataset
    from scripts.repair_courses import RepairConfig
    run_dir = Path(data_root) / 'agent-runs' / request['run_id']
    run_dir.mkdir(parents=True, exist_ok=True)
    plan = {'intent': 'initial_schedule', 'selected_tools': ['initial_schedule'], 'planner': 'deterministic'}
    write_json(run_dir / 'request.json', request); write_json(run_dir / 'plan.json', plan)
    result = {'ok': False, 'status': 'needs_attention', 'run_dir': str(run_dir), 'plan': plan, 'tool_calls': 1}
    try:
        if request.get('state_id'):
            raise ValueError('首次排课创建根版本，不能传入已有 state_id')
        args = request['tool_arguments']
        parameters = args['initial_parameters']
        # This entry explicitly selects the original script's calendar policy.
        # Record its effective scope, including for cancellation/restart resume.
        requested_auth = request.get('requested_authorization', request['authorization'])
        auth = {**request['authorization'], 'allow_evening': True, 'allow_weekend': True,
                'time_policy': 'test_for_school'}
        request = {**request, 'requested_authorization': requested_auth, 'authorization': auth}
        write_json(run_dir / 'request.json', request)
        if type(parameters['weeks']) is not int or not 1 <= parameters['weeks'] <= 53:
            raise ValueError('学期周数须为 1–53')
        if parameters['strategy'] not in ('first', 'random', 'balanced') or type(parameters['seed']) is not int:
            raise ValueError('首轮策略或种子无效')
        parameters = {**parameters, 'auto_reschedule': parameters.get('auto_reschedule', False), 'batch_size': 50, 'days': list(range(7)), 'periods': list(range(1, 12)),
                      'time_policy': 'test_for_school'}
        sources = {}
        input_dir = run_dir / 'sources'; input_dir.mkdir()
        for key, argument in {'courses': 'course_path', 'teachers': 'teacher_path', 'rooms': 'room_path', 'classes': 'class_path'}.items():
            target = input_dir / f'{key}.xlsx'
            shutil.copyfile(args[argument], target); sources[key] = str(target.resolve())
        for key, argument in {'combinations': 'combinations_path', 'clustering': 'clustering_path'}.items():
            if args.get(argument):
                target = input_dir / f'{key}.xlsx'
                shutil.copyfile(args[argument], target); sources[key] = str(target.resolve())
        hashes = {key: file_hash(path) for key, path in sources.items()}
        result['source_hashes'] = hashes
        from scripts.engine_input_contract import prepare_engine_inputs
        result['format_audit']=str(run_dir/'engine-inputs'/'format-audit.json')
        engine_sources,format_audit=prepare_engine_inputs(sources,run_dir/'engine-inputs',project_root)
        engine_hashes={key:file_hash(path) for key,path in engine_sources.items()}
        code_paths = [Path(__file__), *[Path(project_root) / name for name in ('test_for_school.py', 'Basic_Data.py', 'utils1.py', 'tryloadlimit.py', 'scripts/repair_data.py', 'scripts/engine_input_contract.py', 'scripts/repair_courses.py', 'scripts/timetable_state.py', 'scripts/timetable_acceptance.py', 'scripts/timetable_progress.py', 'scripts/teacher_time_constraints.py', 'scripts/legacy_reschedule_adapter.py', 'utils_for_reschedule.py', 'reschedule_by_adjusting.py')]]
        code_hashes = {str(p): file_hash(p) for p in code_paths}
        spec = {'run_dir': str(run_dir.resolve()), 'project_root': str(Path(project_root).resolve()), 'sources': engine_sources, 'parameters': parameters}
        write_json(run_dir / 'worker.json', spec)
        write_json(run_dir / 'manifest.json', {'source_hashes': hashes, 'code_hashes': code_hashes, 'parameters': parameters, 'engine_source_hashes':engine_hashes, 'format_audit':format_audit})
        write_json(run_dir / 'progress.json', {'status': 'running', 'message': '正在核对四张基础表'})
        with (run_dir / 'engine.log').open('w') as log:
            subprocess.run([sys.executable, "-u", str(Path(__file__).resolve()), str(run_dir / 'worker.json')],
                           cwd=run_dir, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}, stdout=log, stderr=log,
                           check=True)
        candidate = json.loads((run_dir / 'candidate.json').read_text())
        if hashes != {key: file_hash(path) for key, path in sources.items()} or code_hashes != {str(p): file_hash(p) for p in code_paths} or engine_hashes != {key:file_hash(path) for key,path in engine_sources.items()}:
            raise ValueError('运行期间输入或求解代码改变，拒绝提交')
        dataset = load_dataset(engine_sources['courses'], engine_sources['rooms'], run_dir / 'empty.xlsx', engine_sources['classes'])
        config = RepairConfig(days=parameters['days'], periods=parameters['periods'], time_scope='strict')
        targets = {row['course_id'] for row in candidate['courses']}
        try:
            validation = validate_output(dataset, candidate['placements'], targets, config, parameters['weeks'], original_contract=True)
        except Exception as error:
            write_json(run_dir / 'acceptance.json', {'valid': False, 'message': str(error), 'candidate_preserved': True})
            result['final_response'] = {'ok': False, 'result': candidate}
            raise ValueError('原引擎已完成计算，原始结果保留在 legacy-results/ 和 candidate.json；保存版本时验收未通过：' + str(error)) from error
        write_json(run_dir / 'acceptance.json', validation)
        candidate['validation'] = validation
        write_json(run_dir / 'candidate.json', candidate)
        if set(candidate['remaining_course_ids']) != targets - {r['course_id'] for r in candidate['placements']}:
            raise ValueError('首轮剩余课程对账失败')
        store = StateStore(Path(data_root) / 'timetable-state')
        # Create and finish the root privately, then publish its directory once.
        with TemporaryDirectory(prefix='initial-', dir=store.root) as temporary:
            staging = StateStore(temporary)
            schedule, failed = Path(temporary) / 'schedule.xlsx', Path(temporary) / 'remaining.xlsx'
            staging._write_schedule(schedule, candidate['placements'])
            staging._write_remaining(failed, candidate['remaining_course_ids'], dataset)
            operation = {'engine': 'legacy_initial', 'tool': 'test_for_school.process_combinations', 'parameters': parameters,
                         'authorization': auth, 'run_id': request['run_id'], 'code_hashes': code_hashes, 'skipped': candidate['skipped'], 'input_format_contract':format_audit}
            state = staging.create_baseline({**engine_sources, 'schedule': schedule, 'failed': failed},
                                            operation=operation, strict=True, validation=validation)
            destination = store.datasets / state['dataset_id']
            if destination.exists():
                for key, filename in state['source_files'].items():
                    if file_hash(destination / filename) != state['source_hashes'][key]:
                        raise ValueError('已有源快照哈希不匹配')
            else:
                shutil.copytree(staging.datasets / state['dataset_id'], destination)
            (staging.states / state['state_id']).rename(store.states / state['state_id'])
        result.update(ok=True, status='completed', output_state_id=state['state_id'], input_state_id=None,
                      publication_ready=False, final_response={'ok': True, 'result': candidate})
        try:
            from .recovery import checkpoint
        except ImportError:
            from recovery import checkpoint
        checkpoint(run_dir, state, phase='initial_saved')
        if parameters['auto_reschedule']:
            continue_with_legacy(result, store, state, dataset, candidate, parameters, auth, run_dir, code_hashes)
    except subprocess.CalledProcessError:
        error_file = run_dir / 'worker-error.json'
        error = json.loads(error_file.read_text()) if error_file.exists() else {'message': '首轮引擎运行失败，请查看本次 engine.log。'}
        result['error'] = error
    except Exception as error:
        result['error'] = {'message': str(error)}
    write_json(run_dir / 'result.json', result)
    from scripts.timetable_progress import finish
    finish(run_dir,result['status'],'首轮任务已结束')
    return result


if __name__ == '__main__':
    spec = json.loads(Path(sys.argv[1]).read_text())
    try:
        worker(spec)
    except Exception as error:
        write_json(Path(spec['run_dir']) / 'worker-error.json', {'message': str(error)})
        raise
