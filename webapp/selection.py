"""Bind web operations to the explicitly viewed result; never fall back from a draft."""
import json
from pathlib import Path
import re


def guard_selection(data_root,fields,action,state_id):
    if action not in {'repair','legacy_reschedule'}:return
    draft=fields.get('draft_run_id','').strip()
    viewed=fields.get('viewed_run_id','').strip()
    rid=draft or viewed
    if not rid:return
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}',rid):raise ValueError('当前页面的运行编号无效，请重新打开运行记录')
    folder=Path(data_root)/'agent-runs'/rid
    try:result=json.loads((folder/'result.json').read_text(encoding='utf-8'))
    except (OSError,ValueError):raise ValueError('无法核对当前页面的课表，请重新打开运行记录') from None
    from run_binding import current_run_state
    selected=current_run_state(data_root,rid,result)
    if draft or not selected:
        counts=''
        try:
            candidate=json.loads((folder/'candidate.json').read_text(encoding='utf-8'))
            counts=f"（已排 {len({p['course_id'] for p in candidate['placements']})} 门、未排 {len(candidate['remaining_course_ids'])} 门）"
        except (OSError,ValueError,KeyError,TypeError):pass
        raise ValueError(f'当前选择的是候选 {rid}{counts}，尚未生成验收通过的课表版本，不能直接补排或调课。请先处理该候选的验收问题；系统不会改用旧版本。')
    if state_id!=selected:raise ValueError('页面显示的课表与提交的版本不一致，未启动任务。请重新打开目标运行记录。')
