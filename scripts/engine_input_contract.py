"""Template-faithful engine handoff. Templates supply format, never old facts."""
from copy import copy
from dataclasses import dataclass
from hashlib import sha256
import json
import math
from pathlib import Path
import re

from openpyxl import load_workbook

FILES={'courses':('课程表.xlsx',2),'rooms':('教室表.xlsx',2),
       'teachers':('教师表.xlsx',1),'classes':('班级表.xlsx',1)}
PREFIXES=(('理论学时','LLXS'),('是否检查教室冲突','IF_ROOM_CONFICT'),('是否检查班级冲突','IF_CLASS_CONFICT'))
REQUIRED={'courses':{'JXBID','JSH','SKXQ','SKZCDM','RWJSZCDM','ZXS','KRL','SFXYPK','SFXYJAS'},
          'rooms':{'JASDM','MC','SKZWS','SFYXPK'},'teachers':{'JSH','XM'},'classes':{'BJMC'}}
EXTENSIONS={'max_block','block_template','available_Time'}
NUMERIC={'LLXS','IF_ROOM_CONFICT','IF_CLASS_CONFICT','SKZWS','KSZWS'}
IDS={'JXBID','JSH','JASDM','LSJASDM','KCH','KKDWDM','JXLDM','XXXQDM','XNXQDM','SKZCDM','RWJSZCDM'}


def digest(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def text(value):
    if value is None:return ''
    if isinstance(value,float) and value.is_integer():return str(int(value))
    return str(value).strip()


def headers(sheet, count):
    result=[]
    for column in range(1,sheet.max_column+1):
        key=text(sheet.cell(count,column).value)
        if not key and count==2:
            chinese=text(sheet.cell(1,column).value)
            key=next((k for prefix,k in PREFIXES if chinese.startswith(prefix)), '')
        result.append(key)
    return result


class InputContractError(ValueError):pass


def prepare_engine_inputs(sources, directory, project_root, *, allow_extensions=True):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    templates=Path(project_root)/'智能排课基础数据'/'提取的基础数据表_converted'
    report={'schema':'engine_input_contract.v1','template_root':str(templates.resolve()),
            'template_data_reused':False,'files':{},'issues':[]}
    outputs=dict(sources)
    for kind,(filename,count) in FILES.items():
        if kind not in sources:continue
        source=Path(sources[kind]);template=templates/filename
        if not template.is_file():raise InputContractError(f'标准格式文件不存在：{template}')
        output=directory/filename
        if output.resolve() in (source.resolve(),template.resolve()):
            raise InputContractError('格式交接目录不能覆盖源表或标准模板')
        source_hash=digest(source);template_hash=digest(template)
        book=load_workbook(template);original=load_workbook(source)
        try:
            sheet=book.active;incoming=original.active
            if len(book.worksheets)!=1:
                raise InputContractError(f'{filename} 标准工作簿有多个工作表，需要明确引擎使用的模板')
            keys=headers(sheet,count);source_keys=headers(incoming,count)
            source_map={k:i+1 for i,k in enumerate(source_keys) if k}
            duplicates=sorted({k for k in source_keys if k and source_keys.count(k)>1})
            issues=[f'{filename}：重复字段 {key}' for key in duplicates]
            missing=sorted(REQUIRED[kind]-set(source_map))
            issues.extend(f'{filename}：缺少必需字段 {key}'+('（理论学时，不能自动以 XS/SKXS 替代）' if key=='LLXS' else '') for key in missing)
            extra_parameters=sorted(EXTENSIONS.intersection(source_map)-set(keys))
            if extra_parameters and not allow_extensions:
                issues.append(f'{filename}：标准模板不含参数列 {", ".join(extra_parameters)}，为避免丢弃参数，未交给引擎；请明确允许扩展或更新标准模板')
            template_columns=sheet.max_column
            if allow_extensions:
                for key in extra_parameters:
                    column=sheet.max_column+1
                    for r in range(1,count+1):
                        sheet.cell(r,column,key)._style=copy(sheet.cell(r,template_columns)._style)
                    keys.append(key)
            template_last=sheet.max_row
            input_last=incoming.max_row; input_columns=incoming.max_column
            profiles=[]
            for column,key in enumerate(keys,1):
                sample=next((sheet.cell(r,column) for r in range(count+1,template_last+1)
                             if sheet.cell(r,column).value is not None),sheet.cell(count+1,column))
                types={sheet.cell(r,column).data_type for r in range(count+1,template_last+1)
                       if sheet.cell(r,column).value is not None}
                dtype='s' if not types else 'mixed' if {'s','n'}<=types else sample.data_type
                profiles.append((copy(sample._style),dtype))
            # Blank template data in place: keep styles, widths, header cells and
            # layout, but remove every old value, formula, comment and hyperlink.
            old_last=sheet.max_row
            for row in sheet.iter_rows(min_row=count+1):
                for cell in row:
                    cell.value=None;cell.hyperlink=None;cell.comment=None
            next_row=count+1;conversions=0;row_map=[]
            for source_row in range(count+1,input_last+1):
                if not any(incoming.cell(source_row,c).value is not None for c in range(1,input_columns+1)):continue
                row_map.append({'source_row':source_row,'engine_row':next_row})
                for column,key in enumerate(keys,1):
                    target=sheet.cell(next_row,column)
                    style,dtype=profiles[column-1]
                    if next_row>old_last:target._style=copy(style)
                    source_column=source_map.get(key)
                    if not source_column:continue
                    cell=incoming.cell(source_row,source_column);value=cell.value
                    if value is None or value=='':continue
                    if cell.data_type=='f':
                        issues.append(f'{filename} {cell.coordinate}：字段 {key} 是公式，请先提供已确认的值，避免列重排后公式引用变化')
                        continue
                    desired='n' if key in NUMERIC else 's' if key in IDS or key in EXTENSIONS else dtype
                    try:
                        if key in {'SFXYPK','SFXYJAS','IF_ROOM_CONFICT','IF_CLASS_CONFICT','SFYXPK'} and text(value) not in ('0','0.0','1','1.0'):
                            raise ValueError('开关必须为明确的 0/1')
                        if desired=='mixed':
                            converted=value
                        elif desired=='n':
                            number=float(value)
                            if not math.isfinite(number):raise ValueError()
                            converted=int(number) if number.is_integer() else number
                        else:
                            converted=text(value)
                            if key in IDS and isinstance(value,(int,float)) and re.fullmatch(r'0+',cell.number_format or ''):
                                converted=converted.zfill(len(cell.number_format))
                        conversions+=int(type(converted)!=type(value) or converted!=value)
                        target.value=converted
                        if isinstance(converted,str):target.data_type='s'
                    except (ValueError,TypeError,OverflowError):
                        issues.append(f'{filename} {cell.coordinate}：字段 {key} 不能转换为标准单元格类型 {desired}')
                next_row+=1
            if old_last>=next_row:sheet.delete_rows(next_row,old_last-next_row+1)
            output=directory/filename;book.save(output)
            outputs[kind]=str(output.resolve())
            report['files'][kind]={'source_path':str(source.resolve()),'source_hash':source_hash,
                 'template_path':str(template.resolve()),'template_hash':template_hash,
                 'engine_path':str(output.resolve()),'engine_hash':digest(output),
                 'standard_columns':template_columns,'output_columns':len(keys),'data_rows':len(row_map),
                 'cell_conversions':conversions,'row_mapping':row_map,
                 'omitted_source_columns':sorted(set(source_map)-set(keys)),
                 'extension_columns':extra_parameters if allow_extensions else [],'issues':issues}
            if digest(source)!=source_hash or digest(template)!=template_hash:
                issues.append(f'{filename}：转换期间源表或模板变化，请重新提交')
            report['issues'].extend(issues)
        finally:
            original.close();book.close()
    report['valid']=not report['issues']
    (directory/'format-audit.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    if report['issues']:
        raise InputContractError('引擎输入格式不符合标准：'+'；'.join(report['issues'][:12])+f'。完整检查记录：{directory/"format-audit.json"}')
    return outputs,report
