"""Exports based on sanitized layouts from the supplied documents."""
from copy import copy
from io import BytesIO
from pathlib import Path
import re
from xml.sax.saxutils import escape
from zipfile import ZipFile

from openpyxl import load_workbook
from openpyxl.comments import Comment

import rop_forms


TEMPLATES = Path(__file__).resolve().parent/'export_templates'
OUTPUT_COLUMNS = ('H','J','K','L','M','N')
METRICS = ('calculated_pct','calculated_kg','actual_pct','actual_kg','difference_pct','difference_kg')
OUTPUT_KEYS = ('flour','flour','bran','feed','waste','moisture')
TOKEN = re.compile(rb'\[\[([a-z0-9_]+)\]\]')


def form34_context(values, *, period_id, ledger_kg, status):
    total, _, balance = rop_forms.cleaning(values)
    result = dict(values, period_id=period_id, status=status, ledger_kg=ledger_kg,
                  total_waste=total, balance=balance)
    for key, _ in rop_forms.WASTE:
        main, grain = values.get(key+'_main'), values.get(key+'_grain')
        if main is not None and grain is not None:
            result[key+'_grain_total'] = round(main+grain,3)
    return result


def form34_download(values):
    out = BytesIO()
    with ZipFile(TEMPLATES/'rop_form34_template.docx') as source, ZipFile(out,'w') as target:
        for item in source.infolist():
            data = source.read(item)
            if item.filename == 'word/document.xml':
                def substitute(match):
                    key=match[1].decode()
                    value=values.get(key)
                    if value is None:
                        value='____________' if key.endswith(('_name','_signature')) else '—'
                    if isinstance(value,float):
                        value=format(value,'.12g')
                    return escape(str(value)).encode()
                data = TOKEN.sub(substitute, data)
            target.writestr(item, data)
    out.seek(0)
    return out


def form117_workbook(values, quality34, report, status, measured_clean):
    book = load_workbook(TEMPLATES/'rop_form117_template.xlsx')
    sheet = book.active
    visible = {}
    def set_cell(addr, value):
        cell = sheet[addr]
        cell.value = value
        if isinstance(value,str):
            cell.data_type = 's'
        visible[addr] = value
    def formula(addr, expression, displayed=None):
        sheet[addr] = expression
        visible[addr] = displayed
    for addr, value in {
        'A1':values.get('organization'), 'G1':values.get('organization_code'),
        'E4':values.get('report_month'), 'A8':status, 'L3':signature(values,'director','Rahbar'),
        'A7': f"75% un tortish · tozalangan bug‘doy: {measured_clean if measured_clean is not None else '—'} kg",
        'A55': 'Tarozi tizimi, sig‘imi va holati · '+values.get('scale','____________'),
        'A56': 'Donli chiqindi topshirish · '+values.get('waste_note','____________'),
        'A57': values.get('remarks'),
        'A58':values.get('document'), 'A59':values.get('document_date'),
        'R10':measured_clean, 'M4':values.get('approval_date'), 'R13':values.get('form117_document'),
        'R14':values.get('override_basis'),
    }.items():
        set_cell(addr,value)
    for addr,(role,label) in zip(('C58','F58','L58','C59','F59','L59'),rop_forms.SIGNS[117][1:]):
        set_cell(addr,signature(values,role,label))
    for addr in ('A8','L3','C58','F58','L58','C59','F59','L59','A55','A56','A57'):
        align=copy(sheet[addr].alignment)
        align.wrap_text=True
        sheet[addr].alignment=align
    for row in (58,59):
        sheet.row_dimensions[row].height=max(sheet.row_dimensions[row].height or 0,27)
    for row in (13,14,15,16,17,18,19):
        set_cell(f'C{row}',values.get(f'calc_{row}'))
    for addr in ('H16','J16','K16','L16','H19','J19'):
        value=values.get('calc_'+addr)
        if addr in ('H16','H19') and value is None:
            value=values.get('calc_J'+addr[-2:])
        set_cell(addr,value)
    # The originals do not place the two admixture measurements in consistently
    # named rows. Preserve the 117 laboratory cells as documented inputs.
    for row,key in ((37,'moisture'),(39,'nature')):
        for col,side in (('C','raw'),('D','clean')):
            key34=f'{side}_{key}'
            set_cell(f'{col}{row}',values.get(f'cell_{col}{row}',quality34.get(key34)))
    for cell,target in (('D37','B13'),('D39','B17'),('D49','B18'),('D38','B19')):
        set_cell(target,values.get('cell_'+target,visible.get(cell,values.get('cell_'+cell))))
    for key,value in values.items():
        if key.startswith('cell_'):
            addr=key[5:]
            if addr not in visible:
                set_cell(addr,value)
    corrections = report.get('corrections',{}) if report else {}
    for row,key in ((13,'f13'),(14,'f14'),(15,'f15')):
        set_cell(f'F{row}',corrections.get(key,values.get(f'form117_{key}')))
    rows=report.get('rows',{}) if report else {}
    for column,key in zip(OUTPUT_COLUMNS,OUTPUT_KEYS):
        result=rows.get(key,{})
        set_cell(column+'24',result.get('actual_kg',values.get('form117_'+key)))
        if values.get('actual_basis_'+key):
            sheet[column+'24'].comment=Comment(values['actual_basis_'+key],'Manba')
    if not report:
        return book,visible
    for column in ('H','J','K'):
        formula(column+'13',f'=F13*{column}12/96.5',corrections['f13']*sheet[column+'12'].value/96.5)
        formula(column+'14',f'=-F14*{column}12/96.5',-corrections['f14']*sheet[column+'12'].value/96.5)
    for addr, expression, value in (
        ('N13','=-F13',-corrections['f13']), ('L14','=F14',corrections['f14']),
        ('H15','=-F15',-corrections['f15']), ('J15','=-F15',-corrections['f15']),
        ('K15','=F15',corrections['f15']),
    ):
        formula(addr,expression,value)
    for col,key in zip(OUTPUT_COLUMNS,OUTPUT_KEYS):
        result=rows[key]
        if col in 'HJ':
            formula(col+'20',f'={col}13+{col}14+{col}15+{col}16+{col}19',result['calculated_pct']-75)
        elif col in 'KLN':
            formula(col+'20',{'K':'=K13+K14+K15+K16','L':'=L14+L16','N':'=N13'}[col],
                    result['calculated_pct']-({'K':21.5,'L':2.8,'N':0}[col]))
        exprs={21: f'={col}12+{col}20',22:f'={col}21*$R$10/100',
               23:f'={col}24/$R$10*100',25:f'={col}21-{col}23',26:f'={col}24-{col}22'}
        exprs[21]={'N':'=N20','M':'=M12'}.get(col,exprs[21])
        for row, metric in zip((21,22,23,24,25,26),METRICS):
            if row==24:
                continue
            if metric in result.get('manual',[]):
                set_cell(col+str(row),result[metric])
                sheet[col+str(row)].comment=Comment(report['override_basis'],'Qo‘lda kiritilgan')
            else:
                formula(col+str(row),exprs[row],result[metric])
    for row,metric in zip((21,22,23,24),METRICS):
        if metric in report.get('total_overrides',{}):
            set_cell('O'+str(row),report['total_overrides'][metric])
        else:
            formula('O'+str(row),'='+'+'.join(f'{col}{row}' for col in 'JKLMN'),
                    sum(v[metric] for v in rows.values()))
    for row,metric in ((25,'difference_pct'),(26,'difference_kg')):
        if metric in report.get('total_overrides',{}):
            set_cell(f'O{row}',report['total_overrides'][metric])
        elif not report.get('override_basis'):
            set_cell(f'O{row}',0)
    formula('P24','=O24-J24-K24-L24-M24-N24',0)
    formula('Q24','=O22-O24',0)
    for addr in ('P24','Q24'):
        sheet[addr].number_format='0.###;-0.###;'
    for row in (22,23):
        if f'cell_R{row}' in values and f'cell_S{row}' in values:
            formula(f'T{row}',f'=R{row}-S{row}',values[f'cell_R{row}']-values[f'cell_S{row}'])
    if 'T22' in visible and visible.get('T23'):
        formula('U22','=T22/T23%',visible['T22']*100/visible['T23'])
    for start in (29,37):
        rates = [values.get(f'cell_T{row}') for row in (start,start+1)]
        if None not in rates:
            weights = (rows['flour']['actual_kg'], rows['bran']['actual_kg'])
            weighted = 0
            for row,col,weight,rate in zip((start,start+1),('J','K'),weights,rates):
                formula(f'S{row}',f'={col}24',weight)
                formula(f'U{row}',f'=T{row}*S{row}%',weight*rate/100)
                weighted += weight*rate/100
            formula(f'S{start+2}',f'=SUM(S{start}:S{start+1})',sum(weights))
            formula(f'U{start+2}',f'=SUM(U{start}:U{start+1})',weighted)
            if sum(weights):
                formula(f'T{start+2}',f'=U{start+2}/S{start+2}%',100*weighted/sum(weights))
    return book,visible


def signature(values, role, label):
    return label+' · '+values.get(role+'_name','________')+' · '+values.get(role+'_signature','________')


def workbook_download(book, values):
    original = BytesIO()
    book.save(original)
    out = BytesIO()
    with ZipFile(original) as source, ZipFile(out,'w') as target:
        for item in source.infolist():
            data = source.read(item)
            if item.filename=='xl/worksheets/sheet1.xml':
                def cached(match):
                    value=values.get(match[2].decode())
                    if not isinstance(value,(int,float)):
                        return match[0]
                    return match[1]+f'<v>{value}</v></c>'.encode()
                data=re.sub(rb'(<c r="([A-Z]+[0-9]+)"[^>]*><f>[^<]*</f>)(?:<v></v>|<v\s*/>)</c>',cached,data)
            target.writestr(item,data)
    out.seek(0)
    return out


def print_grid(sheet, visible):
    width = [sheet.sheet_format.defaultColWidth or 13]*16
    for dimension in sheet.column_dimensions.values():
        for index in range(dimension.min or 1,min(dimension.max or 1,16)+1):
            width[index-1]=dimension.width
    merges = {}
    for rng in sheet.merged_cells.ranges:
        if rng.max_col <= 16:
            merges[(rng.min_row,rng.min_col)] = (rng.max_row-rng.min_row+1,rng.max_col-rng.min_col+1)
    rows = []
    for row in sheet.iter_rows(min_row=1,max_row=59,max_col=16):
        result = []
        for cell in row:
            if cell.__class__.__name__ == 'MergedCell':
                continue
            coord=cell.coordinate
            value=visible.get(coord,cell.value)
            if coord=='P24' and value==0:
                value=None
            row_span,col_span=merges.get((cell.row,cell.column),(1,1))
            precision = 0 if cell.row in (22,24,26) else 2
            result.append((value,row_span,col_span,cell.font.bold,precision,
                           any(getattr(cell.border,edge) and getattr(cell.border,edge).style
                               for edge in ('top','bottom','left','right'))))
        rows.append(result)
    return rows,[round(w*100/sum(width),4) for w in width]
