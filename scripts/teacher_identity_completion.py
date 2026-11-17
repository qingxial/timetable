"""Human supplied teacher identities, scoped to a frozen course source row.

The LLM may inspect problems and ask questions. Only the web's structured human
answer can authorize a source edit; arbitrary model text is never a correction.
"""
from copy import copy
import hashlib
import json
from pathlib import Path
import re
import shutil
from openpyxl import load_workbook


def text(value):
    if value is None:return ''
    if isinstance(value,float) and value.is_integer():return str(int(value))
    return str(value).strip()


def file_hash(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for part in iter(lambda:f.read(1024*1024),b''):h.update(part)
    return h.hexdigest()


def inspect_teacher_identities(course_path,teacher_path,scope_ids=None):
    """Scan every source row in scheduled AND remaining scope, no 60-row truncation."""
    hashes={'courses':file_hash(course_path),'teachers':file_hash(teacher_path)}
    book=load_workbook(teacher_path,read_only=True,data_only=True)
    try:
        rows=iter(book.active.values);header=[text(x) for x in next(rows,())]
        if not {'JSH','XM'}<=set(header):raise ValueError('教师表缺少 JSH/XM 列')
        registry={}
        for row in rows:
            d=dict(zip(header,row));tid=text(d.get('JSH'))
            if tid:registry.setdefault(tid,set()).add(text(d.get('XM')))
    finally:book.close()
    book=load_workbook(course_path,read_only=True,data_only=True);issues=[]
    try:
        rows=iter(book.active.values);next(rows,None);header=[text(x) for x in next(rows,())]
        if not {'JXBID','JSH','SKZCDM','RWJSZCDM'}<=set(header):raise ValueError('课程表缺少教学班、教师或责任周标准列')
        checked=0
        for n,row in enumerate(rows,3):
            d=dict(zip(header,row));cid=text(d.get('JXBID'))
            if not cid or (scope_ids is not None and cid not in scope_ids):continue
            if scope_ids is None and text(d.get('SFXYPK')) in ('0','0.0'):continue
            checked+=1;tid=text(d.get('JSH'));name=text(d.get('XM'))
            kind=('missing_teacher_id' if not tid else 'teacher_not_registered' if tid not in registry else
                  'teacher_name_mismatch' if len(registry[tid])!=1 or (name and name not in registry[tid]) else '')
            if not kind:continue
            active=[i+1 for i,(a,b) in enumerate(zip(text(d.get('SKZCDM')),text(d.get('RWJSZCDM')))) if a==b=='1']
            item={'kind':kind,'course_id':cid,'course_name':text(d.get('KCM')),'source_row':n,
                  'teacher_id':tid,'teacher_name':name,'responsibility_weeks':active,
                  'course_week_mask':text(d.get('SKZCDM')),'teacher_week_mask':text(d.get('RWJSZCDM')),
                  'registry_names':sorted(registry.get(tid,set()))}
            item['issue_id']=hashlib.sha256(json.dumps([hashes,item],ensure_ascii=False,sort_keys=True).encode()).hexdigest()
            item['question']=f"{cid} {item['course_name']}（源表第{n}行，责任周{','.join(map(str,active))}）：请核实教师编号、姓名及是否需登记到教师表。"
            issues.append(item)
    finally:book.close()
    return {'tool':'inspect_data_quality','scope':'all_scheduled_and_remaining_source_rows',
            'source_hashes':hashes,'checked_source_rows':checked,'issues':issues,'issue_count':len(issues)}


def apply_teacher_answers(sources,answers,expected_hashes,destination,scope_ids=None):
    """Copy-on-write source snapshot; never overwrite an old source or guess an ID."""
    if not isinstance(answers,list) or not 1<=len(answers)<=100:raise ValueError('每次需补齐1–100条教师信息')
    scan=inspect_teacher_identities(sources['courses'],sources['teachers'],scope_ids)
    if scan['source_hashes']!=expected_hashes:raise ValueError('数据版本已改变，请刷新检查结果后再补充')
    issues={i['issue_id']:i for i in scan['issues']};seen=set();validated=[]
    tb=load_workbook(sources['teachers']);ts=tb.active;th={text(c.value):c.column for c in ts[1]}
    cb=load_workbook(sources['courses']);cs=cb.active;ch={text(c.value):c.column for c in cs[2] if text(c.value)}
    if 'XM' not in ch:
        col=cs.max_column+1;cs.cell(1,col,'教师姓名');cs.cell(2,col,'XM');ch['XM']=col
    registry={}
    for row in ts.iter_rows(min_row=2):
        tid=text(row[th['JSH']-1].value)
        if tid:registry.setdefault(tid,set()).add(text(row[th['XM']-1].value))
    try:
        for a in answers:
            if not isinstance(a,dict) or set(a)!={'issue_id','teacher_id','teacher_name','register_teacher'}:raise ValueError('教师补充字段不完整或含未知字段')
            if any(not isinstance(a[k],str) for k in ('issue_id','teacher_id','teacher_name')):raise ValueError('问题编号、教师编号和姓名必须为文本')
            issue=issues.get(a['issue_id'])
            if issue is None or a['issue_id'] in seen:raise ValueError('问题已失效、超出范围或重复提交')
            seen.add(a['issue_id']);tid=text(a['teacher_id']);name=text(a['teacher_name'])
            if not re.fullmatch(r'[A-Za-z0-9_.@-]{1,80}',tid) or not name or len(name)>100 or any(ord(c)<32 for c in name):raise ValueError('教师编号或姓名格式无效')
            if type(a['register_teacher'])is not bool:raise ValueError('是否新增教师必须明确选择')
            # A known nonempty identity is not silently replaced by an unrelated teacher.
            if issue['teacher_id'] and issue['teacher_id']!=tid:raise ValueError('现有教师编号不能由缺失信息补齐工具替换，请单独核实更正')
            if issue['teacher_name'] and issue['teacher_name']!=name:raise ValueError('姓名与当前课程记录不一致，请核实后使用专门的身份更正流程')
            if tid in registry:
                if registry[tid]!={name}:raise ValueError('编号与教师表姓名不一致或该编号有矛盾记录，请先核实教师身份')
                if a['register_teacher']:raise ValueError('该编号已登记，请取消新增教师')
                added=False
            else:
                if not a['register_teacher']:raise ValueError('教师表没有此编号，请明确选择新增教师登记')
                ts.append([None]*ts.max_column);n=ts.max_row
                if n>2:
                    for col in range(1,ts.max_column+1):ts.cell(n,col)._style=copy(ts.cell(2,col)._style)
                ts.cell(n,th['JSH'],tid).number_format='@';ts.cell(n,th['XM'],name)
                registry[tid]={name};added=True
            row=issue['source_row'];assert text(cs.cell(row,ch['JXBID']).value)==issue['course_id']
            cs.cell(row,ch['JSH'],tid).number_format='@';cs.cell(row,ch['XM'],name)
            validated.append({**issue,'operation':'fill_teacher_identity','supplied_teacher_id':tid,'supplied_teacher_name':name,
                              'teacher_registry_added':added,'evidence':'explicit_structured_human_answer'})
        dest=Path(destination);dest.mkdir(parents=True,exist_ok=True);paths={}
        for key,path in sources.items():
            target=dest/(key+Path(path).suffix);shutil.copy2(path,target);paths[key]=str(target.resolve())
        cb.save(paths['courses']);tb.save(paths['teachers'])
    finally:cb.close();tb.close()
    if scan['source_hashes']!={'courses':file_hash(sources['courses']),'teachers':file_hash(sources['teachers'])}:raise ValueError('运行期间源数据发生改变，拒绝保存')
    return paths,{'tool':'complete_teacher_identity','human_answers':validated,'source_hashes_before':scan['source_hashes'],
                  'source_hashes_after':{k:file_hash(v) for k,v in paths.items()},'source_files_unchanged':True}
