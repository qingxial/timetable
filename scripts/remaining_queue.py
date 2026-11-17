"""Whole-remaining work queue. Batches are implementation details, not task scope."""
from copy import deepcopy
from scripts.repair_courses import RepairConfig, room_rejections
from scripts.concentrated_teaching import assess_concentrated_course


class RemainingQueue:
    ORDER = ('concentrated', 'time', 'resource', 'occupancy_check', 'explain', 'optimize', 'unavailable')

    def __init__(self, dataset, remaining, config, saved=None):
        self.rows = {}
        prior = (saved or {}).get('courses', {})
        for cid in sorted(remaining):
            if cid in prior:
                self.rows[cid] = deepcopy(prior[cid])
                continue
            course = dataset.courses.get(cid)
            row = {'course_id': cid, 'attempts': [], 'status': 'queued'}
            if course is None or course.issues:
                row.update(status='waiting_data', reason='；'.join(course.issues) if course else '课程元数据缺失')
            elif not any(not room_rejections(course, room, set()) for room in dataset.rooms.values()):
                row['stage'] = 'resource'
            else:
                assessment = assess_concentrated_course(course, RepairConfig(**config))
                row['stage'] = 'concentrated' if assessment.get('eligible') else 'time'
            self.rows[cid] = row

    def next_action(self, remaining, allowed_movable, movement_cap):
        wanted = set(remaining)
        for stage in self.ORDER:
            ids = [cid for cid, row in self.rows.items() if cid in wanted and row['status'] == 'queued' and row.get('stage') == stage][:60]
            if not ids:
                continue
            tools = {'concentrated': 'repair_concentrated_courses', 'time': 'repair_with_time_relaxation',
                     'resource': 'probe_resource_repairs', 'occupancy_check': 'propose_repair', 'explain': 'explain_conflict',
                     'optimize': 'optimize_local_repair', 'unavailable': 'repair_with_time_relaxation'}
            args = {'target_ids': ids, 'time_limit_seconds': 25}
            if stage in ('explain', 'optimize'):
                # Use the real occupancy core; releasing it still requires complete reinsertion.
                blockers = set().union(*(set(self.rows[cid].get('blockers', [])) for cid in ids))
                movable = sorted(blockers & allowed_movable)[:60] if movement_cap else []
                args.update(movable_ids=movable, max_moved_courses=movement_cap if movable else 0)
            return {'tool': tools[stage], 'arguments': args, 'reason': '全部未排队列：按失败原因处理本批，随后自动接续其他课程。', 'questions': []}
        return None

    def observe(self, action, response):
        result = response.get('result', {})
        proposal = result.get('proposal', {})
        outcomes = {r['course_id']: r for r in proposal.get('results', [])}
        plans = {r['course_id']: r for r in result.get('course_plans', [])}
        blockers = {r.get('course_id') for r in result.get('core', []) if r.get('course_id')}
        blockers |= {s.removeprefix('occupancy:') for s in result.get('core_ids', []) if s.startswith('occupancy:')}
        for cid in action['arguments'].get('target_ids', []):
            row = self.rows[cid]
            stage = row['stage']
            outcome = outcomes.get(cid, {})
            reason = outcome.get('status', 'not_found_within_limits')
            row['attempts'].append({'tool': action['tool'], 'stage': stage, 'result': reason if response.get('ok') else 'tool_failed'})
            if not response.get('ok'):
                row.update(status='failed', reason=str(response.get('error', '工具执行失败')))
            elif reason in ('placed', 'already_scheduled'):
                row.update(status='scheduled', reason=reason)
            elif stage == 'resource':
                row.update(status='waiting_resource' if plans.get(cid, {}).get('options') else 'failed', reason='人数或教室替代待确认' if plans.get(cid, {}).get('options') else '未找到资源替代建议')
            elif stage == 'concentrated':
                row.update(stage='time', reason=reason)
            elif stage == 'time':
                if reason == 'no_matching_room': row['stage'] = 'resource'
                elif reason in ('no_time_pattern', 'no_joint_teacher_time_pattern'): row['stage'] = 'occupancy_check'
                elif reason in ('not_found_within_limits', 'search_limit'): row['stage'] = 'explain'
                else: row.update(status='waiting_data' if reason == 'unsupported' else 'failed', reason=reason)
            elif stage == 'occupancy_check':
                # Fixed-occupancy pruning can report no_time_pattern even when the source domain exists.
                row.update(stage='unavailable' if reason in ('no_time_pattern','no_joint_teacher_time_pattern','unsupported') else 'explain', reason=reason)
            elif stage == 'explain':
                row.update(stage='optimize', blockers=sorted(blockers), reason=result.get('status', 'unknown'))
            elif stage == 'optimize':
                row.update(stage='unavailable', reason=reason)
            else:
                row.update(status='failed', reason=reason)

    def snapshot(self, remaining, pending=()):
        remaining = set(remaining)
        rows = deepcopy(self.rows)
        for cid, row in rows.items():
            if cid not in remaining: row['status'] = 'scheduled'
            elif cid in pending: row['status'] = 'waiting_time'
            elif row['status'] == 'scheduled': row.update(status='queued', reason='候选尚未保存')
        counts = {name: sum(row['status'] == name for row in rows.values()) for name in ('scheduled', 'waiting_resource', 'waiting_data', 'waiting_time', 'failed', 'queued')}
        return {'courses': rows, 'counts': counts, 'remaining': len(remaining),
                'unattempted': sum(row['status'] == 'queued' and not row['attempts'] for row in rows.values()),
                'unfinished': counts['queued'], 'all_processed': counts['queued'] == 0}
