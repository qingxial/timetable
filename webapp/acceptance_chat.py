"""Read-only acceptance chat and explicit weekly-hours review choices."""
from hour_review import inspect_hours

CHECKS=[('范围对账','已排与未排清单不重复、不丢课。'),
        ('周学时与周次','以源表周学时及授课周为准；实际安排差异列为待选择项，不根据LLXS要求额外补课。'),
        ('教师','教师编号、责任周及各自适用的禁排、可用时间和偏好。'),
        ('资源冲突','检查教师和教室冲突；按用户要求不校验行政班标识，也不检查班级撞课。'),
        ('教室与校区','校区永久禁止放宽；其他教室条件沿用当前引擎规则。'),
        ('课块与时段','合法周次、时段、自重叠及既有课块、周负荷上限。'),
        ('版本与改动范围','只处理明确版本和获准课程，保留原安排和审计信息。')]


def is_acceptance_question(prompt):
    text=str(prompt).strip()
    if not any(k in text for k in ('验收','校验','验证')):return False
    if any(k in text for k in ('跳过验收','绕过验收','关闭校验','强制通过','改成通过')):return False
    return text in {'验收','校验','验证'} or any(k in text for k in ('什么','怎么','为什么','哪些','哪里','如何','解释','查看','说明','不通过','失败','结果','问题','一下','重新','运行','检查','标准','规则'))


def recheck_requested(prompt):return any(k in prompt for k in ('重新验收','运行验收','验收一下','重新校验','校验一下','检查验收'))


def explain_acceptance(data_root,basis=None,prompt='验收什么'):
    lines=['验收按源表周学时和授课周核对；LLXS与这个计算不一致，不再作为阻塞，也不自动更改源表。',
           '检查内容：'+'；'.join(name for name,_ in CHECKS)+'。']
    result={'read_only':True,'checks':[{'name':a,'description':b} for a,b in CHECKS],
            'basis':basis,'source_files_modified':False,'solver_started':False}
    if basis:
        context,data,review=inspect_hours(data_root,basis)
        pending=[d for d in review['differences'] if d not in context['policy'].get('accepted_hour_differences',[])]
        review['pending_count']=len(pending);review['already_accepted']=not pending and bool(context['parent'])
        result.update(basis_token=review['basis_token'],review=review,validation=review['hard_validation'],rechecked=True,
                      placed=review['placed'],remaining=review['remaining'])
        lines.append(f"当前对象：已排 {review['placed']} 门、未排 {review['remaining']} 门。按周学时/授课周统计，{review['difference_count']} 门存在安排差异，其中 {len(pending)} 门尚待选择。")
        for d in pending[:3]:lines.append(f"{d['course_id']} {d['course_name']}：源周计划合计 {d['expected_total']:g} 节，实际 {d['actual_total']} 节。")
        if not review['hard_validation'].get('valid'):
            lines.append('仍有其他约束未通过，接受课时差异也不能跳过：'+review['hard_validation'].get('message','请查看验收记录'))
        elif pending:lines.append('你可以选择“接受当前安排并继续”，保留现有课表并记录决定；也可以选择清单中的课程局部重排。不会擅自更改源表的周学时或周次。')
        elif context['parent']:lines.append('周安排差异已确认或不存在，可从当前课表版本继续排课/调课。')
        else:lines.append('其他已实现的约束检查通过。可点击“接受当前安排并继续”，把这套候选保存为可续接版本。')
        warnings=review['hard_validation'].get('source_course_warnings',{})
        if any(any(str(w).startswith('coverage:') for w in values) for values in warnings.values()):lines.append('源班级映射等覆盖限制保留在审计中，不据此声称真实学生全部无冲突。')
    else:lines.append('选中具体候选后，可查看周安排差异并选择接受或局部调整；此类询问不需要AI Key，不会自动调课。')
    result['answer']='\n\n'.join(lines);return result
