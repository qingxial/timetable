"""Explainable, offline room similarity. Scores rank suggestions, not permissions."""
from difflib import SequenceMatcher

_TYPES = {
    '固定桌椅多媒体':('general',{'multimedia','fixed'}),
    '活动桌椅多媒体':('general',{'multimedia','movable'}),
    '普通教室':('general',set()),'活动桌椅教室':('general',{'movable'}),
    '录播教室':('general',{'multimedia','recording'}),
    '语音教室':('language',{'language'}),'同声传译教室':('language',{'language','interpretation'}),
    '实验室':('laboratory',{'laboratory'}),'体育类专用':('sport',{'sport'}),
    '艺术类专用':('art',{'art'}),'LECTURE':('general',set()),'LAB':('laboratory',{'laboratory'}),
}
_FEATURE_LABELS={'multimedia':'多媒体','fixed':'固定桌椅','movable':'活动桌椅','recording':'录播',
                 'language':'语音','interpretation':'同声传译','laboratory':'实验设施','sport':'体育设施','art':'艺术设施'}
_SPORTS=('足球','篮球','排球','网球','乒乓','田径','武术','健美操','游泳','羽毛球')


def similarity_context(data, course):
    anchors=[data.rooms[rid] for rid in sorted(course.explicit_rooms|course.historical_rooms) if rid in data.rooms and data.rooms[rid].campus==course.campus]
    if not anchors:
        peers={c.id for c in data.courses.values() if c.id!=course.id and c.name.strip()==course.name.strip() and c.campus==course.campus}
        ids={p.room_id for p in data.placements if p.course_id in peers}
        anchors=[data.rooms[rid] for rid in sorted(ids) if rid in data.rooms and data.rooms[rid].campus==course.campus]
    source_types=sorted({course.room_type} if course.room_type else {r.type for r in anchors if r.type})
    return {'anchors':anchors,'source_types':source_types}


def room_similarity(data, course, room, context=None):
    context=context or similarity_context(data,course)
    anchors=context['anchors'];source_types=context['source_types']
    family,features=_TYPES.get(room.type,('unknown',set()))
    comparisons=[]
    for source_type in source_types:
        sf,sfeatures=_TYPES.get(source_type,('unknown',set()))
        exact=source_type==room.type
        comparable=exact or (sf!='unknown' and sf==family)
        missing=sfeatures-features-{'fixed'}  # Furniture changes remain visible for review.
        missing_labels=sorted(_FEATURE_LABELS[k] for k in missing)
        if exact:score=1.0
        elif comparable:
            score=.55+.35*len(features&sfeatures)/max(1,len(features|sfeatures))
            if 'multimedia' in missing or 'laboratory' in missing or 'interpretation' in missing:score*=.4
        else:score=.15*SequenceMatcher(None,source_type,room.type).ratio()
        reasons=['相同教室类型' if exact else '同类功能空间' if comparable else '功能类别不同或信息不足']
        if missing_labels:reasons.append('需要核实的功能差异：'+'、'.join(missing_labels))
        comparisons.append((score,comparable,missing_labels,source_type,reasons))
    if comparisons:
        score,comparable,missing,source_type,reasons=max(comparisons,key=lambda x:x[0])
        basis='source_room_type' if course.room_type else 'same_course_historical_room_types'
    else:
        # Empty type is not permission to replace a lecture with a specialized venue.
        score=.5 if family=='general' else .15;comparable=family=='general';missing=[];source_type=''
        reasons=['源类型为空，优先通用教学空间；设备需求待确认'];basis='unspecified_type_general_room_prior'
    sport=next((token for token in _SPORTS if token in course.name),None)
    if sport and not any(token in course.name for token in ('鉴赏','理论','概论','史')):
        if family!='sport' or sport not in room.name:
            score=0;comparable=False;reasons.append('课程运动项目与场地用途未匹配')
    if course.campus!=room.campus or not course.campus:
        score=0;comparable=False;reasons.append('校区不匹配，不可替代')
    anchor_score=max((SequenceMatcher(None,a.name,room.name).ratio() for a in anchors),default=0)
    if comparable and score<1 and anchors:score=min(.99,score+.04*anchor_score)
    return {'method':'structured_features_v1','score':round(score,4),'source_type':source_type,'candidate_type':room.type,
            'basis':basis,'anchor_room_ids':[r.id for r in anchors], 'missing_features':missing,
            'reasons':reasons,'auto_trial_eligible':comparable and not any(f in missing for f in ('多媒体','实验设施','同声传译')),
            'requires_facility_confirmation':not bool(course.room_type and course.room_type==room.type),
            'interpretation':'功能相似度仅用于推荐排序，不构成设备适用性或替代授权。'}
