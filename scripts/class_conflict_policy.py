"""User-confirmed project policy; retain class source text but do not validate it."""
CHECK_CLASS_CONFLICTS = False
EVIDENCE = '2026-10-30 用户明确要求：行政班标识不论能否匹配都不校验；不检查班级撞课。'


def enabled():
    return CHECK_CLASS_CONFLICTS


def acceptance_policy():
    return {'check_class_conflicts': False, 'class_conflict_exemption_evidence': EVIDENCE}
