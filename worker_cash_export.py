"""Read-only worker cash XLSX presentation, using the approved v3 styles."""
from copy import copy
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO
import math
import re
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
from excel_dates import excel_date


def build_worker_cash_workbook(template_path, workers, transfers, directs, report_date):
    methods = {'naqd': 'Naqd', 'plastik': 'Plastik', 'hisob': 'Hisobdan o‘tkazish'}
    statuses = {'approved': 'Qabul qilingan', 'pending': 'Kutilmoqda', 'rejected': 'Rad etilgan'}

    def amount(value):
        try:
            value = Decimal(str(value))
            if not value.is_finite() or value < 0 or not math.isfinite(float(value)):
                raise ValueError()
            return value
        except (InvalidOperation, ValueError, OverflowError):
            raise ValueError('Eksportda yaroqsiz yoki cheksiz summa bor.') from None

    def day(value):
        try:
            result = excel_date(value)
            if isinstance(result, datetime): return result.date()
            if isinstance(result, date): return result
            raise ValueError()
        except (ValueError, TypeError):
            raise ValueError('Eksport yozuvida sana noto‘g‘ri yoki mavjud emas.') from None

    def safe(value):
        if isinstance(value, str) and (len(value) > 32767 or re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]', value)):
            raise ValueError('Eksport matnida yaroqsiz XML belgisi yoki juda uzun matn bor.')
        if isinstance(value, (int, float, Decimal)): amount(value)
        return value

    worker_map = {w['id']: w for w in workers}
    zero = Decimal(0)
    totals = {wid: [zero] * 6 for wid in worker_map}
    events = []
    seen = set()
    for source, records in (('transfer', transfers), ('direct', directs)):
        for row in records:
            key = (source, row['id'])
            if key in seen: raise ValueError('Eksportda takror manba yozuvi bor.')
            seen.add(key)
            wid = row['worker_id']
            if wid not in worker_map:
                raise ValueError('Pul harakati Umid/Maxmud xodimlar ro‘yxatiga mos emas.')
            method = row['payment_method']
            if method not in methods: raise ValueError('Pul harakatining to‘lov kanali noma’lum.')
            value = amount(row['amount'])
            status = row['status'] if source == 'transfer' else 'approved'
            if status not in statuses: raise ValueError('Pul harakatining holati noma’lum.')
            event_date = day(row['created_at'] if source == 'transfer' else row['payment_date'])
            name = worker_map[wid]['username']
            recipient = row['accountant'] or ''
            customer = row['customer'] if source == 'direct' else '—'
            note = (row['note'] or '') if source == 'direct' else ''
            if source == 'transfer' and row['decided_at']:
                note = 'Qaror sanasi: ' + day(row['decided_at']).strftime('%d.%m.%y')
            # Validate pending/rejected rows as well: they are exported history.
            values = [event_date, name, 'Xodim kassasidan' if source == 'transfer' else 'Buxgalter bevosita', value,
                      methods[method], recipient, statuses[status], customer, note]
            for item in values: safe(item)
            events.append((event_date, source, row['id'], values))
            if status == 'approved':
                t = totals[wid]
                t[0 if source == 'transfer' else 1] += value
                t[2] += value
                t[3 + list(methods).index(method)] += value
    for t in totals.values():
        for value in t: amount(value)
        if t[2] != sum(t[3:], zero): raise ValueError('Kanal summalari jami bilan mos emas.')
    grand = [sum((t[c] for t in totals.values()), zero) for c in range(6)]
    for value in grand: amount(value)
    if grand[2] != sum((e[3][3] for e in events if e[3][6] == 'Qabul qilingan'), zero):
        raise ValueError('Tarix va umumiy hisobot summalari mos emas.')

    design = load_workbook(template_path)
    wb = Workbook(); wb.remove(wb.active)
    wb.properties.title = 'Xodimlar kassasi hisoboti'
    wb.properties.creator = 'TO‘RTKO‘LDONMAXSULOTI'

    def put(ws, row, col, value, style_cell):
        cell = ws.cell(row, col, safe(value))
        cell.font = copy(style_cell.font)
        cell.fill = copy(style_cell.fill)
        cell.border = copy(style_cell.border)
        cell.alignment = copy(style_cell.alignment)
        cell.protection = copy(style_cell.protection)
        cell.number_format = style_cell.number_format
        if isinstance(value, str): cell.data_type = 's'
        if isinstance(value, (date, datetime)): cell.number_format = 'dd.mm.yy'
        font = copy(cell.font); font.name = 'Times New Roman'; font.sz = 14; cell.font = font
        return cell

    for source in design:
        ws = wb.create_sheet(source.title)
        ws.sheet_view.showGridLines = False
        ws.sheet_view.zoomScale = source.sheet_view.zoomScale
        ws.sheet_view.topLeftCell = 'A1'
        ws.sheet_properties.pageSetUpPr = copy(source.sheet_properties.pageSetUpPr)
        ws.page_setup = copy(source.page_setup)
        ws.page_margins = copy(source.page_margins)
        ws.print_options = copy(source.print_options)
        ws.print_title_rows = source.print_title_rows
        for key, dim in source.column_dimensions.items(): ws.column_dimensions[key] = copy(dim)
        for row in source.iter_rows(min_row=1, max_row=6):
            for cell in row: put(ws, cell.row, cell.column, cell.value, cell)
        for row in range(1, 7): ws.row_dimensions[row].height = source.row_dimensions[row].height
        for region in source.merged_cells.ranges:
            if region.max_row <= 6: ws.merge_cells(str(region))
        ws['C3'] = day(report_date); ws['C3'].number_format = 'dd.mm.yy'
        date_cell = 'I3' if source.title == 'Umumiy hisobot' else 'J3'
        ws[date_cell] = day(report_date); ws[date_cell].number_format = 'dd.mm.yy'

    s, h = wb.worksheets; st, ht = design.worksheets
    # Clear sample table content by building fresh sheets; only styles/headers
    # above are copied. Row counts and every amount are derived from records.
    for row, worker in enumerate(workers, 7):
        values = [row-6, worker['username'], *totals[worker['id']], '']
        for col, value in enumerate(values, 1): put(s, row, col, value, st.cell(7, col))
        s.row_dimensions[row].height = st.row_dimensions[7].height
    total_row = 7 + len(workers)
    for col in range(1, 10): put(s, total_row, col, None, st.cell(9, col))
    s.merge_cells(start_row=total_row, start_column=1, end_row=total_row, end_column=2)
    put(s, total_row, 1, 'JAMI', st['A9'])
    for col, value in enumerate(grand, 3): put(s, total_row, col, value, st.cell(9, col))
    s.row_dimensions[total_row].height = st.row_dimensions[9].height
    for row, label, value in [(7, methods['naqd'], grand[3]), (8, methods['plastik'], grand[4]), (9, methods['hisob'], grand[5]), (10, 'JAMI', grand[2])]:
        put(s, row, 11, label, st.cell(row, 11)); put(s, row, 12, value, st.cell(row, 12))
        s.row_dimensions[row].height = max(s.row_dimensions[row].height or 0, st.row_dimensions[row].height or 0)
    note_row = max(total_row, 10) + 2
    s.merge_cells(start_row=note_row, start_column=1, end_row=note_row+1, end_column=12)
    put(s, note_row, 1, 'Faqat «Qabul qilingan» operatsiyalar JAMIga kiradi; kutilayotgan va rad etilgan summalar chiqariladi.\nBuxgalter bevosita olgan qarz to‘lovi bir marta hisoblanadi.\nBu hisobot sotuv daromadi yoki kassadagi qoldiq emas.', st['A12'])
    s.row_dimensions[note_row].height = 40; s.row_dimensions[note_row+1].height = 40
    s.print_area = f'A1:L{note_row+1}'

    for row, event in enumerate(sorted(events, key=lambda e:e[:3]), 7):
        for col, value in enumerate([row-6, *event[3]], 1): put(h, row, col, value, ht.cell(7, col))
        h.row_dimensions[row].height = max(56, 22 * max((len(str(v))//24 + 1 for v in event[3] if isinstance(v, str)), default=1))
    last = 6 + len(events)
    h.auto_filter.ref = f'A6:J{max(6,last)}'
    if events: h.auto_filter.add_sort_condition(f'B7:B{last}', descending=False)
    h.freeze_panes = 'D7'
    total_row = last + 2
    for col in range(1, 11): put(h, total_row, col, None, ht.cell(14, col))
    h.merge_cells(start_row=total_row,start_column=1,end_row=total_row,end_column=4)
    h.merge_cells(start_row=total_row,start_column=6,end_row=total_row,end_column=10)
    put(h,total_row,1,'JAMI TASDIQLANGAN',ht['A14'])
    put(h,total_row,5,grand[2],ht['E14'])
    put(h,total_row,6,'Kutilmoqda va rad etilgan summalar kiritilmagan',ht['F14'])
    h.row_dimensions[total_row].height = ht.row_dimensions[14].height
    note_row = total_row+2
    h.merge_cells(start_row=note_row,start_column=1,end_row=note_row+1,end_column=10)
    put(h,note_row,1,'Sanalar kun.oy.yil ko‘rinishida. Xodim topshirishida so‘rov yaratilgan sana, bevosita to‘lovda to‘lov sanasi ko‘rsatiladi.\nSummalar eksport paytidagi tasdiqlangan yozuvlardan hisoblangan.',ht['A16'])
    h.row_dimensions[note_row].height=30;h.row_dimensions[note_row+1].height=30
    h.print_area=f'A1:J{note_row+1}'
    wb.active=0
    output=BytesIO();wb.save(output)
    return output.getvalue()
