"""Excel endpoint body for excel_report; unchanged calculations and workbook layout.
The Flask route and authorization remain in app.py.
"""


def export_legacy_excel_report(_app_dependencies):
    BAGGED_FLOUR_PRODUCTS = _app_dependencies['BAGGED_FLOUR_PRODUCTS']
    BASE_DIR = _app_dependencies['BASE_DIR']
    Path = _app_dependencies['Path']
    calculate_financial_snapshot = _app_dependencies['calculate_financial_snapshot']
    customer_display = _app_dependencies['customer_display']
    customer_identity_context = _app_dependencies['customer_identity_context']
    db = _app_dependencies['db']
    debt_customers = _app_dependencies['debt_customers']
    expense_display = _app_dependencies['expense_display']
    json = _app_dependencies['json']
    product_stocks = _app_dependencies['product_stocks']
    re = _app_dependencies['re']
    request = _app_dependencies['request']
    from excel_dates import format_date_columns, display_date
    from datetime import date
    from bulk_reporting import append_bulk_sheets, build_bulk_report
    from business_dates import _monthly_parse_date
    from date_sql import (
        mixed_date_key_expression, mixed_date_to_iso,
        register_mixed_date_key_function, validate_mixed_date_column,
    )
    from io import BytesIO
    from flask import send_file, abort
    from openpyxl import Workbook
    from openpyxl import load_workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.worksheet.filters import AutoFilter
    from openpyxl.utils import get_column_letter
    from copy import copy
    from rop_service import latest_measurement, latest_period, period_totals, wheat_warehouse_used
    kinds = {'wheat':'Bug‘doy xaridi','cleaning':'Bug‘doy tozalash',
             'production':'Ishlab chiqarish','sales':'Sotuvlar',
             'debtors':'Xaridor qarzlari','payments':'To‘lovlar',
             'expenses':'Xarajatlar','stocks':'Ombor qoldiqlari'}
    kind=request.args.get('type','')
    if kind not in kinds: abort(400, 'Hisobot turini tanlang.')
    start=request.args.get('start','').strip(); end=request.args.get('end','').strip()
    try:
        if start: start = mixed_date_to_iso(start)
        if end: end = mixed_date_to_iso(end)
        if start and end and start>end: raise ValueError()
    except ValueError:
        abort(400, 'Sana oralig‘i noto‘g‘ri; YYYY-MM-DD yoki DD-MM-YYYY formatidan foydalaning.')
    if kind == 'production':
        template_path = Path(BASE_DIR) / 'static' / 'production_report_template.xlsx'
        if not template_path.is_file():
            abort(500, 'Ishlab chiqarish Excel dizayn shabloni topilmadi.')
        wb = load_workbook(template_path)
        ws = wb['Umumiy ishlab chiqarish']
    else:
        wb = Workbook()
        ws = wb.active
        ws.title = kinds[kind][:31]
    def query(con,sql,args=(),column='date',table=None):
        try:
            validate_mixed_date_column(con, table, column)
        except ValueError as exc:
            abort(400, f'Hisobotdagi sana yaroqsiz: {exc}')
        register_mixed_date_key_function(con)
        date_key = mixed_date_key_expression(column)
        filters=[]; params=list(args)
        if start: filters.append(f'{date_key}>=?'); params.append(start)
        if end: filters.append(f'{date_key}<=?'); params.append(end)
        sql, order_separator, ordering = sql.partition(' ORDER BY ')
        if order_separator:
            ordering = re.sub(
                rf'\b{re.escape(column)}\b', date_key, ordering, count=1
            )
        else:
            order_separator = ' ORDER BY '
            ordering = f'{date_key}, id'
        if filters:
            sql += (' AND ' if ' WHERE ' in sql else ' WHERE ')+' AND '.join(filters)
        if order_separator:
            sql += order_separator + ordering
        return con.execute(sql,params).fetchall()
    def financial_snapshot(con):
        try:
            return calculate_financial_snapshot(con)
        except ValueError as exc:
            abort(400, f'Moliyaviy hisobotni yaratib bo‘lmadi: {exc}')
    with db() as con:
        if kind == 'production':
            con.execute('BEGIN')
        if kind=='wheat':
            ws.append(['Sana','Xarid turi','Yetkazuvchi','Kg','Narx / kg','Jami so‘m','Izoh'])
            for r in query(con,'SELECT date,purchase_method,supplier,net,price_per_kg,note FROM wheat_receipts',table='wheat_receipts'):
                ws.append([r['date'],r['purchase_method'],r['supplier'],r['net'],r['price_per_kg'],r['net']*r['price_per_kg'],r['note']])
        elif kind in ('cleaning','production'):
            prods=query(con,"""SELECT id,date,CASE WHEN id NOT IN (SELECT production_id FROM rop_production)
                THEN COALESCE(raw_wheat_kg,wheat_kg) END AS raw,wheat_kg clean,
                flour_kg,bran_kg,muchka_kg,telashka_kg,note FROM production""",table='production')
            if kind=='cleaning':
                ws.append(['Sana','Olingan kg','Tozalangan kg','Kamaygan kg','Kamayish %'])
                for r in prods:
                    loss = r['raw'] - r['clean'] if r['raw'] is not None else None
                    loss_pct = round(100 * loss / r['raw'], 2) if r['raw'] else None
                    ws.append([r['date'], r['raw'], r['clean'], loss, loss_pct])
            else:
                from datetime import datetime
                from xml.etree import ElementTree as ET
                from zipfile import ZIP_DEFLATED, ZipFile

                bagged_products = BAGGED_FLOUR_PRODUCTS
                bunker_quantities = dict(con.execute('SELECT production_id,qty_kg FROM bulk_flour_batches'))
                detail_by_id = {}
                for r in prods:
                    types={x['product']:x['qty_kg'] for x in con.execute('SELECT product,qty_kg FROM production_flour_types WHERE production_id=?',(r['id'],))}
                    bags = {(p, size): count for p, size, count in con.execute(
                        'SELECT product,bag_kg,bag_count FROM bagged_production_lines '
                        'WHERE production_id=?', (r['id'],))}
                    bag_kg = {
                        product: sum(
                            size * bags.get((product, size), 0) for size in (25, 50)
                        )
                        for product in bagged_products
                    }
                    product_kg = {
                        product: float(types[product])
                        if product in types else float(bag_kg[product])
                        for product in bagged_products
                    }
                    classified_kg = sum(product_kg.values()) + float(types.get('Qora un', 0) or 0)
                    flour_kg = float(r['flour_kg'] or 0)
                    detail_by_id[r['id']] = {
                        'types': types, 'bags': bags, 'bag_kg': bag_kg,
                        'product_kg': product_kg,
                        'classification_gap': flour_kg - classified_kg - bunker_quantities.get(r['id'], 0),
                    }

                layout_sheet = wb['Oliy va 1-nav un']
                summary_labels = ('Jami 25 kg qop', 'Jami 50 kg qop', 'Jami qop', 'Jami un, kg')
                summary_label_styles = [
                    copy(layout_sheet.cell(15, column)._style)
                    for column in (1, 3, 5, 7)
                ]
                summary_value_styles = [
                    copy(layout_sheet.cell(16, column)._style)
                    for column in (1, 3, 5, 7)
                ]
                summary_row_heights = {
                    row: layout_sheet.row_dimensions[row].height
                    for row in (15, 16, 17, 19)
                }
                summary_note = layout_sheet['A19'].value
                summary_note_style = copy(layout_sheet['A19']._style)
                period_cells = {
                    'Umumiy ishlab chiqarish': 'I1',
                    'Oliy va 1-nav un': 'G1',
                    'Qora un': 'D1',
                    'Kepak': 'D1',
                    'Muchka': 'D1',
                    'Telashka atxod': 'D1',
                }
                for sheet in wb.worksheets:
                    for merged in list(sheet.merged_cells.ranges):
                        if merged.min_row >= 8:
                            sheet.unmerge_cells(str(merged))
                    for row in sheet.iter_rows(
                            min_row=8, max_row=sheet.max_row,
                            max_col=sheet.max_column):
                        for cell in row:
                            cell.value = None

                def report_date(value):
                    return datetime.strptime(mixed_date_to_iso(value), '%Y-%m-%d')

                def report_date_text(value):
                    return report_date(value).strftime('%d.%m.%Y')

                if start or end:
                    period_start = report_date_text(start) if start else 'Cheklanmagan'
                    period_end = report_date_text(end) if end else 'Cheklanmagan'
                    period_text = f'{period_start} — {period_end}'
                elif prods:
                    period_text = (
                        f'{report_date_text(prods[0]["date"])} — '
                        f'{report_date_text(prods[-1]["date"])}'
                    )
                else:
                    period_text = 'Ma’lumot yo‘q'
                for sheet in wb.worksheets:
                    sheet[period_cells[sheet.title]] = f'Hisobot davri:\n{period_text}'
                    sheet.freeze_panes = 'A8'

                def copy_report_row(sheet, target_row, source_row, columns):
                    if target_row != source_row:
                        source_height = sheet.row_dimensions[source_row].height
                        if source_height is not None:
                            sheet.row_dimensions[target_row].height = source_height
                    for column in range(1, columns + 1):
                        target = sheet.cell(target_row, column)
                        source = sheet.cell(source_row, column)
                        if target_row != source_row and source.has_style:
                            target._style = copy(source._style)
                        target.value = None

                def write_report_row(sheet, target_row, values, style_row,
                                     date_columns=(), text_columns=()):
                    copy_report_row(sheet, target_row, style_row, len(values))
                    for column, value in enumerate(values, 1):
                        cell = sheet.cell(target_row, column)
                        if column in date_columns and value:
                            cell.value = report_date(value)
                            cell.number_format = 'dd.mm.yyyy'
                        else:
                            cell.value = value
                        if column in text_columns and isinstance(cell.value, str):
                            cell.data_type = 's'

                def set_report_table(sheet, headers, last_data_row):
                    for column, heading in enumerate(headers, 1):
                        sheet.cell(7, column).value = heading
                        sheet.cell(7, column).data_type = 's'
                    table_ref = (
                        f'A7:{get_column_letter(len(headers))}'
                        f'{max(7, last_data_row)}'
                    )
                    for table_name in sheet.tables:
                        table = sheet.tables[table_name]
                        if len(table.tableColumns) != len(headers):
                            abort(500, f'{sheet.title} varag‘idagi jadval ustunlari mos emas.')
                        table.ref = table_ref
                        if table.autoFilter is None:
                            table.autoFilter = AutoFilter(ref=table_ref)
                        else:
                            table.autoFilter.ref = table_ref
                        for column, heading in zip(table.tableColumns, headers):
                            column.name = heading

                general = wb['Umumiy ishlab chiqarish']
                general_headers = [
                    'Sana', 'Olingan bug‘doy (kg)', 'Tozalangan bug‘doy (kg)',
                    'Kamayish (kg)', 'Kamayish (%)', 'Jami un (kg)',
                    'Kepak (kg)', 'Muchka (kg)', 'Telashka (kg)', 'Izoh',
                ]
                production_notes = {}
                for r in prods:
                    detail = detail_by_id[r['id']]
                    note_parts = [str(r['note'])] if r['note'] else []
                    if bunker_quantities.get(r['id']):
                        note_parts.append(f"Bunkerga {bunker_quantities[r['id']]:g} kg")
                    gap = detail['classification_gap']
                    if abs(gap) > 0.000001:
                        if gap > 0:
                            note_parts.append(f'Tasniflanmagan un: {gap:g} kg')
                        else:
                            note_parts.append(f'Tasniflangan un farqi: {gap:g} kg')
                    for product in bagged_products:
                        bag_gap = detail['bag_kg'][product] - detail['product_kg'][product]
                        if abs(bag_gap) > 0.000001:
                            note_parts.append(f'{product} qop/kg farqi: {bag_gap:+g} kg')
                    production_notes[r['id']] = '; '.join(note_parts)
                for index, r in enumerate(prods):
                    row_number = 8 + index
                    raw = r['raw']
                    cleaned = float(r['clean'] or 0)
                    loss = raw - cleaned if raw is not None else None
                    write_report_row(
                        general, row_number,
                        [r['date'], raw, cleaned, loss,
                         loss / raw if raw else None,
                         float(r['flour_kg'] or 0), float(r['bran_kg'] or 0),
                         float(r['muchka_kg'] or 0), float(r['telashka_kg'] or 0),
                         production_notes[r['id']]],
                        8, date_columns=(1,), text_columns=(10,),
                    )
                general_total_row = 8 + len(prods)
                copy_report_row(general, general_total_row, 11, 10)
                general_raw = sum(float(r['raw'] or 0) for r in prods)
                general_cleaned = sum(float(r['clean'] or 0) for r in prods)
                general_loss = sum(r['raw'] - r['clean'] for r in prods if r['raw'] is not None)
                general_totals = [
                    'JAMI', general_raw, general_cleaned, general_loss,
                    general_loss / general_raw if general_raw else 0,
                    sum(float(r['flour_kg'] or 0) for r in prods),
                    sum(float(r['bran_kg'] or 0) for r in prods),
                    sum(float(r['muchka_kg'] or 0) for r in prods),
                    sum(float(r['telashka_kg'] or 0) for r in prods), None,
                ]
                for column, value in enumerate(general_totals, 1):
                    cell = general.cell(general_total_row, column)
                    cell.value = value
                    if column == 1:
                        cell.data_type = 's'
                set_report_table(general, general_headers, 7 + len(prods))

                white = wb['Oliy va 1-nav un']
                white_headers = [
                    'Sana', 'Ishlab chiqarish ID', 'Un mahsuloti',
                    '25 kg qop soni', '50 kg qop soni', 'Jami qop soni',
                    'Jami kg', 'Izoh',
                ]
                white_rows = []
                white_bag_25 = white_bag_50 = 0
                for r in prods:
                    detail = detail_by_id[r['id']]
                    for product in bagged_products:
                        count_25 = int(detail['bags'].get((product, 25), 0))
                        count_50 = int(detail['bags'].get((product, 50), 0))
                        product_kg = detail['product_kg'][product]
                        if not product_kg and not count_25 and not count_50:
                            continue
                        note_parts = []
                        if r['note']:
                            note_parts.append(str(r['note']))
                        bag_gap = detail['bag_kg'][product] - product_kg
                        if abs(bag_gap) > 0.000001:
                            note_parts.append(f'Qop kilogrammi va mahsulot yozuvi farqi: {bag_gap:+g} kg')
                        if product not in detail['types'] and (count_25 or count_50):
                            note_parts.append('Mahsulot kg yozuvi yo‘q; kg qop hisobidan ko‘rsatildi')
                        white_rows.append([
                            r['date'], r['id'], product, count_25, count_50,
                            count_25 + count_50, product_kg, '; '.join(note_parts) or None,
                        ])
                        white_bag_25 += count_25
                        white_bag_50 += count_50
                    gap = detail['classification_gap']
                    if gap > 0.000001:
                        white_rows.append([
                            r['date'], r['id'], 'Eski tasniflanmagan un',
                            0, 0, 0, gap,
                            'Qop o‘lchami noma’lum yoki mahsulot turi tasniflanmagan.',
                        ])
                    elif gap < -0.000001:
                        white_rows.append([
                            r['date'], r['id'], 'Hisob farqi (jami un - tasnif)',
                            0, 0, 0, gap,
                            f'Tasniflangan jami un umumiy undan {abs(gap):g} kg ko‘p.',
                        ])
                for packaged in query(con, 'SELECT id,date FROM flour_packaging', table='flour_packaging'):
                    for line in con.execute('''SELECT product,
                        SUM(CASE WHEN bag_kg=25 THEN bag_count ELSE 0 END) AS bags_25,
                        SUM(CASE WHEN bag_kg=50 THEN bag_count ELSE 0 END) AS bags_50
                        FROM flour_packaging_lines WHERE packaging_id=? GROUP BY product ORDER BY product''',
                        (packaged['id'],)):
                        count_25, count_50 = line['bags_25'], line['bags_50']
                        white_rows.append([packaged['date'], f"Q-{packaged['id']}", line['product'],
                                           count_25, count_50, count_25 + count_50,
                                           count_25 * 25 + count_50 * 50, 'Qoplash kirimi'])
                        white_bag_25 += count_25
                        white_bag_50 += count_50
                white_rows.sort(key=lambda row: report_date(row[0]))
                for index, values in enumerate(white_rows):
                    source_row = 8 if values[2] == 'Eski tasniflanmagan un' else 9
                    write_report_row(
                        white, 8 + index, values, source_row,
                        date_columns=(1,), text_columns=(3, 8),
                    )
                white_total_row = 8 + len(white_rows)
                copy_report_row(white, white_total_row, 13, 8)
                white_kg_total = sum(float(row[6] or 0) for row in white_rows)
                white_totals = [
                    'JAMI', None, None, white_bag_25, white_bag_50,
                    white_bag_25 + white_bag_50, white_kg_total, None,
                ]
                for column, value in enumerate(white_totals, 1):
                    cell = white.cell(white_total_row, column)
                    cell.value = value
                    if column == 1:
                        cell.data_type = 's'
                set_report_table(white, white_headers, 7 + len(white_rows))

                summary_label_row = white_total_row + 2
                summary_value_row = summary_label_row + 1
                for target_row, source_row in (
                        (summary_label_row, 15), (summary_value_row, 16),
                        (summary_value_row + 1, 17), (summary_label_row + 4, 19)):
                    if summary_row_heights[source_row] is not None:
                        white.row_dimensions[target_row].height = summary_row_heights[source_row]
                summary_columns = (1, 3, 5, 7)
                summary_values = (
                    white_bag_25, white_bag_50,
                    white_bag_25 + white_bag_50, white_kg_total,
                )
                for index, column in enumerate(summary_columns):
                    white.merge_cells(
                        start_row=summary_label_row, start_column=column,
                        end_row=summary_label_row, end_column=column + 1,
                    )
                    label_cell = white.cell(summary_label_row, column)
                    label_cell._style = copy(summary_label_styles[index])
                    label_cell.value = summary_labels[index]
                    label_cell.data_type = 's'
                    white.merge_cells(
                        start_row=summary_value_row, start_column=column,
                        end_row=summary_value_row + 1, end_column=column + 1,
                    )
                    value_cell = white.cell(summary_value_row, column)
                    value_cell._style = copy(summary_value_styles[index])
                    value_cell.value = summary_values[index]
                note_row = summary_label_row + 4
                white.merge_cells(
                    start_row=note_row, start_column=1,
                    end_row=note_row, end_column=8,
                )
                note_cell = white.cell(note_row, 1)
                note_cell._style = copy(summary_note_style)
                note_cell.value = summary_note
                note_cell.data_type = 's'

                def fill_product_sheet(sheet_name, header, rows, total_label='JAMI'):
                    sheet = wb[sheet_name]
                    for index, values in enumerate(rows):
                        write_report_row(
                            sheet, 8 + index, values, 8,
                            date_columns=(1,), text_columns=(4,),
                        )
                    total_row = 8 + len(rows)
                    copy_report_row(sheet, total_row, 10, len(header))
                    total_values = [None] * len(header)
                    total_values[0] = total_label
                    total_values[2] = sum(float(row[2] or 0) for row in rows)
                    for column, value in enumerate(total_values, 1):
                        sheet.cell(total_row, column).value = value
                    sheet.merge_cells(
                        start_row=total_row, start_column=1,
                        end_row=total_row, end_column=2,
                    )
                    sheet.cell(total_row, 1).data_type = 's'
                    set_report_table(sheet, header, 7 + len(rows))

                by_product = {
                    'Qora un': [],
                    'Kepak': [],
                    'Muchka': [],
                    'Telashka atxod': [],
                }
                for r in prods:
                    detail = detail_by_id[r['id']]
                    for product in by_product:
                        if product == 'Qora un':
                            amount = float(detail['types'].get(product, 0) or 0)
                        elif product == 'Kepak':
                            amount = float(r['bran_kg'] or 0)
                        elif product == 'Muchka':
                            amount = float(r['muchka_kg'] or 0)
                        else:
                            amount = float(r['telashka_kg'] or 0)
                        if amount:
                            by_product[product].append([
                                r['date'], r['id'], amount, r['note'],
                            ])
                product_headers = ['Sana', 'Ishlab chiqarish ID', 'Ishlab chiqarilgan kg', 'Izoh']
                for product in ('Qora un', 'Kepak', 'Muchka', 'Telashka atxod'):
                    fill_product_sheet(product, product_headers, by_product[product])
        elif kind=='sales':
            con.execute('BEGIN')
            financial = financial_snapshot(con)
            customer_context=customer_identity_context(con)
            allocation = {item['id']: item for group in financial['customers'].values()
                          for item in group['obligations'] if item['kind']=='sale'}
            ws.append(['Sana','Xaridor','Telefon','Mahsulot','Sotuv turi','Qop kg','Qop soni','Jami kg','Birlik narxi','Jami so‘m','Sotuvda to‘langan','Keyingi to‘lovlar (worker_receipts)','Joriy qoldiq qarz','Qarz muddati','Eski to‘lovlardan FIFO','Jami to‘langan'])
            for r in query(con,'SELECT * FROM sales WHERE id NOT IN (SELECT sale_id FROM sale_cancellations) ORDER BY date,id',table='sales'):
                item = allocation[r['id']]
                customer,phone=customer_display(
                    customer_context,r['customer'],r['customer_phone'])
                ws.append([r['date'],customer,phone,r['product'],r['sale_mode'],r['bag_kg'],r['bag_count'],r['qty_kg'],r['unit_price'],item['amount'],item['initial_paid'],item['linked_paid'],item['balance'],r['due_date'] if item['balance']>0 else '',item['legacy_applied'],item['initial_paid']+item['linked_paid']+item['legacy_applied']])
            notes = wb.create_sheet('Izoh va ogohlantirishlar')
            notes.append(['Mavzu','Tushuntirish'])
            notes.append(['Sotuv sanasi filtri',f'{display_date(start)} — {display_date(end)}'])
            notes.append(['Qarz holati','To‘lovlar va qarz eksport paytidagi bazaning joriy holatidir. Sana filtri faqat sotuv sanasiga tegishli; davrdan tashqaridagi barcha qayd etilgan to‘lovlar ham hisoblanadi. Bu davr oxiridagi tarixiy qarz hisoboti emas.'])
            notes.append(['FIFO','Avval aniq bog‘langan to‘lovlar chegiriladi. Eski payments faqat bitta xaridor/telefon guruhiga ishonchli mos bo‘lsa, barcha sotuvlar va eski qarzlar orasida sana, eski qarz ustuvorligi va ID bo‘yicha FIFO taqsimlanadi. Bu tarixiy bog‘lanish isboti emas.'])
            notes.append(['Jami to‘langan','Boshlang‘ich + worker_receipts + ushbu sotuvga FIFO. Buxgalter qabul qilgan worker_receipts bir marta sanaladi. Ortiqcha to‘lov boshqa sotuvga ko‘chirilmaydi; qoldiq manfiy bo‘lmaydi.'])
            notes.append(['Ogohlantirishlar doirasi','Quyidagi ogohlantirishlar butun baza hisobiga tegishli, faqat tanlangan davrga emas. Eski qarzlar sotuv summasiga kiritilmagan.'])
            messages = {'legacy_identity_ambiguous':'Bir xil ism, turli telefon: eski to‘lov taqsimlanmadi',
                        'legacy_without_obligation':'Majburiyatga bog‘lanmagan eski to‘lov',
                        'legacy_fifo_not_explicit_link':'Eski to‘lov FIFO hisobida, aniq sotuv bog‘lanishi yo‘q',
                        'sale_overpaid':'Sotuvda ortiqcha to‘lov',
                        'receipt_without_active_sale':'Faol sotuvga bog‘lanmagan keyingi to‘lov',
                        'cancelled_sale_initial_payment':'Bekor qilingan sotuvda boshlang‘ich to‘lov',
                        'accountant_receipt_mismatch':'Buxgalter to‘lovi bilan manba yozuvi mos emas',
                        'accountant_receipt_without_source':'Buxgalter to‘lovining manba yozuvi yo‘q'}
            for warning in financial['warnings']:
                notes.append([messages.get(warning['code'],warning['code']),json.dumps(warning,ensure_ascii=False,default=str)])
            for row in financial['unallocated_legacy']:
                notes.append(['Taqsimlanmagan eski to‘lov',json.dumps(row,ensure_ascii=False,default=str)])
            for group in financial['customers'].values():
                if group['legacy_excess']:
                    notes.append(['Taqsimlanmagan ortiqcha eski to‘lov',json.dumps({'customer':group['customer'],'phone':group['phone'],'amount':group['legacy_excess']},ensure_ascii=False,default=str)])
            notes.freeze_panes='A2'; notes.auto_filter.ref=notes.dimensions
            notes.column_dimensions['A'].width=40; notes.column_dimensions['B'].width=100
            for row in notes:
                for cell in row:
                    cell.data_type='s'; cell.alignment=Alignment(wrap_text=True)
            for cell in notes[1]:
                cell.font=Font(bold=True,color='FFFFFF'); cell.fill=PatternFill('solid',fgColor='175B42')
        elif kind=='debtors':
            con.execute('BEGIN')
            financial = financial_snapshot(con)
            debtors = debt_customers(con, financial=financial)
            ws.append(['Xaridor','Telefon','Jami sotuv va eski qarz so‘m','To‘langan so‘m','Qolgan qarz so‘m','Eng yaqin muddat','Faol sotuvlar summasi','Eski qarz','Boshlang‘ich to‘lov','Keyingi to‘lov (worker_receipts)','Eski FIFO to‘lovi','Ortiqcha to‘lov'])
            for r in debtors:
                ws.append([r['customer'],'; '.join(phone or 'Ko‘rsatilmagan' for phone in r['phones']),r['total'],r['paid'],r['debt'],r['due_date'],r['sales_revenue'],r['opening_debt'],r['initial_paid'],r['linked_paid'],r['legacy_applied'],r['excess']])
            notes = wb.create_sheet('Izoh va ogohlantirishlar')
            debt_total = sum(r['debt'] for r in debtors)
            notes.append(['Mavzu','Qiymat yoki tushuntirish'])
            notes.append(['Eksportdagi jami qarz',debt_total])
            notes.append(['Umumiy moduldagi jami qarz',financial['totals']['debt']])
            notes.append(['Qarz farqi',financial['totals']['debt']-debt_total])
            notes.append(['Davr','Joriy qarzdorlar va barcha qayd etilgan to‘lovlar hisoblanadi. Sana filtri bu eksportga qo‘llanmaydi; bu tarixiy sana holatidagi qarz hisoboti emas.'])
            notes.append(['To‘langan so‘m','Qarzdorlar sahifasidagi kabi boshlang‘ich + worker_receipts + xaridorga ishonchli bog‘langan eski payments. Eski FIFO ustuni faqat majburiyatlarga ajratilgan qism. Ortiqcha pul ham qabul qilingan pul tarkibida, ammo boshqa majburiyat yoki xaridor qarzidan chegirilmaydi.'])
            notes.append(['Hisoblash','Eski qarz faol sotuv daromadiga kirmaydi. Buxgalter qabul qilgan worker_receipts bir marta hisoblanadi. Nol qoldiqli xaridorlar asosiy varaqda yo‘q.'])
            notes.append(['FIFO','Eski to‘lov faqat bitta ism/telefon guruhiga mos bo‘lsa, avval aniq bog‘langan to‘lovlar chegirilib, sana, eski qarz ustuvorligi va ID tartibida taqsimlanadi. FIFO tarixiy sotuv bog‘lanishining isboti emas.'])
            messages = {'legacy_identity_ambiguous':'Bir xil ism, turli telefon: eski to‘lov taqsimlanmadi',
                        'legacy_without_obligation':'Majburiyatga bog‘lanmagan eski to‘lov',
                        'legacy_fifo_not_explicit_link':'Eski to‘lov FIFO hisobida, aniq sotuv bog‘lanishi yo‘q',
                        'sale_overpaid':'Sotuvda ortiqcha to‘lov',
                        'receipt_without_active_sale':'Faol sotuvga bog‘lanmagan keyingi to‘lov',
                        'cancelled_sale_initial_payment':'Bekor qilingan sotuvda boshlang‘ich to‘lov',
                        'accountant_receipt_mismatch':'Buxgalter to‘lovi bilan manba yozuvi mos emas',
                        'accountant_receipt_without_source':'Buxgalter to‘lovining manba yozuvi yo‘q'}
            for warning in financial['warnings']:
                notes.append([messages.get(warning['code'],warning['code']),json.dumps(warning,ensure_ascii=False,default=str)])
            for row in financial['unallocated_legacy']:
                notes.append(['Taqsimlanmagan eski to‘lov',json.dumps(row,ensure_ascii=False,default=str)])
            phones_by_name = {}
            for (name, phone), group in financial['customers'].items():
                phones_by_name.setdefault(name,set()).add(phone)
                if group['legacy_excess'] or group['linked_excess']:
                    notes.append(['Ortiqcha to‘lov (nol qarzli xaridorlar ham)',json.dumps({'customer':name,'phone':phone,'legacy_excess':group['legacy_excess'],'linked_excess':group['linked_excess']},ensure_ascii=False,default=str)])
            for name, phones in phones_by_name.items():
                if len(phones)>1:
                    notes.append(['Bir xil ismli xaridorlar alohida hisoblandi',json.dumps({'customer':name,'phones':sorted(phones)},ensure_ascii=False)])
            notes.freeze_panes='A2'; notes.auto_filter.ref=notes.dimensions
            notes.column_dimensions['A'].width=40; notes.column_dimensions['B'].width=100
            for row in notes:
                for cell in row:
                    if isinstance(cell.value,str): cell.data_type='s'
                    cell.alignment=Alignment(wrap_text=True)
            for cell in notes[1]:
                cell.font=Font(bold=True,color='FFFFFF'); cell.fill=PatternFill('solid',fgColor='175B42')
        elif kind=='payments':
            from decimal import Decimal
            con.execute('BEGIN')
            financial = financial_snapshot(con)
            customer_context=customer_identity_context(con)
            users = {r['id']:r['username'] for r in con.execute('SELECT id,username FROM users')}
            sales = {r['id']:dict(r) for r in con.execute('SELECT * FROM sales')}
            cancelled = {r['sale_id'] for r in con.execute('SELECT sale_id FROM sale_cancellations')}
            direct = {r['receipt_id']:dict(r) for r in con.execute('SELECT * FROM accountant_worker_receipts')}
            batches = {r['sale_id']:r['batch_id'] for r in con.execute('SELECT * FROM worker_sale_batch_items')}
            batch_channels = {}
            for r in con.execute('SELECT * FROM worker_sale_batch_payments ORDER BY id'):
                batch_channels.setdefault(r['batch_id'],[]).append(dict(r))
            ws.append(['Sana','Xaridor','Telefon','Qabul qilingan summa','Izoh','To‘lov turi','Manba','Manba ID','Qabul qiluvchi / yozuvdagi foydalanuvchi','Tegishli xodim','Kanal (yozuv bo‘yicha)'])
            events = []
            for r in query(con,'SELECT * FROM sales WHERE paid>0 ORDER BY date,id',table='sales'):
                note = r['note'] or ''
                channel = r['payment_method']
                if r['id'] in batches:
                    batch_id = batches[r['id']]
                    channel = ''
                    note += ' | Paket '+str(batch_id)+': sales.paid — taqsimlangan boshlang‘ich summa; kanal sotuvga aniq bog‘lanmagan. Paket kanallari (qayta qo‘shilmaydi): '+json.dumps(batch_channels.get(batch_id,[]),ensure_ascii=False)
                if r['id'] in cancelled: note += ' | Bekor qilingan sotuvdagi pul yozuvi; avtomatik qaytarilgan deb hisoblanmadi.'
                if not users.get(r['user_id']): note += ' | Qabul qiluvchi aniqlanmagan.'
                customer,phone=customer_display(
                    customer_context,r['customer'],r['customer_phone'])
                events.append([r['date'],customer,phone,Decimal(str(r['paid'])),note,'Boshlang‘ich sotuv to‘lovi','sales.paid',r['id'],users.get(r['user_id'],''),users.get(r['user_id'],''),channel])
            for r in query(con,'SELECT * FROM payments ORDER BY date,id',table='payments'):
                customer,_=customer_display(customer_context,r['customer'])
                events.append([r['date'],customer,'',Decimal(str(r['amount'])),r['note'] or '','Eski to‘lov','payments',r['id'],'','',''])
            for r in query(con,'SELECT * FROM worker_receipts ORDER BY date,id',table='worker_receipts'):
                sale = sales.get(r['sale_id'],{})
                accountant = direct.get(r['id'])
                recipient = accountant['accountant_id'] if accountant else r['worker_id']
                note = accountant['note'] if accountant else ''
                if not sale or r['sale_id'] in cancelled: note += ' | Faol sotuvga bog‘lanmagan pul yozuvi.'
                if not users.get(recipient): note += ' | Qabul qiluvchi aniqlanmagan.'
                if accountant and (accountant['payment_date']!=r['date'] or accountant['amount']!=r['amount'] or accountant['payment_method']!=r['payment_method'] or accountant['worker_id']!=r['worker_id'] or accountant['customer']!=sale.get('customer') or accountant['customer_phone']!=sale.get('customer_phone')):
                    note += ' | Buxgalter belgilashi manbaga mos emas; sana, summa, kanal worker_receipts yozuvidan olindi.'
                customer,phone=customer_display(
                    customer_context,sale.get('customer',''),sale.get('customer_phone',''))
                events.append([r['date'],customer,phone,Decimal(str(r['amount'])),note,'Buxgalter olgan qarz to‘lovi' if accountant else 'Xodim olgan qarz to‘lovi','worker_receipts',r['id'],users.get(recipient,''),users.get(r['worker_id'],''),r['payment_method']])
            for r in query(con,'SELECT * FROM opening_debt_receipts ORDER BY date,id',
                           table='opening_debt_receipts'):
                debt=con.execute('SELECT customer,customer_phone FROM opening_debts WHERE id=?',
                                 (r['opening_debt_id'],)).fetchone()
                if debt is None:
                    continue
                customer,phone=customer_display(customer_context,debt['customer'],debt['customer_phone'])
                events.append([r['date'],customer,phone,Decimal(str(r['amount'])),
                    (r['note'] or '')+' | Eski qarz #'+str(r['opening_debt_id']),
                    'Eski qarz to‘lovi','opening_debt_receipts',r['id'],
                    users.get(r['receiver_id'],''),users.get(r['worker_id'],''),r['payment_method']])
            for row in sorted(events,key=lambda r:(mixed_date_to_iso(r[0]),r[6],r[7])): ws.append(row)
            notes = wb.create_sheet('Izoh va ogohlantirishlar')
            notes.append(['Mavzu','Qiymat yoki tushuntirish'])
            notes.append(['Tanlangan davrda jami qabul qilingan pul',sum((r[3] for r in events),Decimal(0))])
            notes.append(['Sana mezoni',f'{display_date(start)} — {display_date(end)}. Boshlang‘ich to‘lov: sales.date (alohida to‘lov sanasi yo‘q); eski to‘lov: payments.date; keyingi to‘lov: worker_receipts.date. Chegaralar kiritiladi.'])
            notes.append(['Pul va daromad','Jami qabul qilingan pul sotuv daromadi emas. Ichki worker_transfers, accountant_worker_receipts va paket kanal summalari yangi pul sifatida qo‘shilmaydi.'])
            notes.append(['Yozuv birligi','Manba + Manba ID noyob. Bitta haqiqiy to‘lov bir necha sotuvga bo‘lingan bo‘lsa, bazada umumiy hodisa ID yo‘qligi sabab satrlar taxmin bilan birlashtirilmaydi. Paket sales.paid summasi taqsimlangan qismdir.'])
            notes.append(['Kanal aniqligi','naqd / plastik / hisob (bank). Eski yozuvlarga migratsiya naqd qiymatini bergan bo‘lishi mumkin; kanal ustuni bazadagi belgini ko‘rsatadi, tarixiy tasdiq emas. Paketlarda sotuvga kanal taqsimlanmaydi. Bo‘sh yoki noma’lum kanal taxmin bilan to‘ldirilmaydi.'])
            notes.append(['Qabul qiluvchi','Boshlang‘ich to‘lovda sales.user_id — yozuvdagi foydalanuvchi. Keyingi to‘lovda worker_id yoki accountant_id. Eski payments uchun qabul qiluvchi tasdiqlanmagan.'])
            notes.append(['Eski payments','Telefon, qabul qiluvchi va kanal aniq bog‘lanmaganligi sabab bo‘sh qoldirildi. Boshqa xaridor yozuvidan taxminan olinmadi; asl izoh saqlandi.'])
            notes.append(['Barcha sanalar: modul tushumi',financial['totals']['received']])
            if not start and not end:
                notes.append(['Eksport va modul farqi',financial['totals']['received']-sum((r[3] for r in events),Decimal(0))])
            notes.append(['Ogohlantirishlar doirasi','Quyidagi modul ogohlantirishlari barcha sanalarga tegishli. Bekor qilingan sotuvdagi qayd etilgan pul yo‘qolmaydi; qaytarish taxmin qilinmaydi.'])
            for warning in financial['warnings']:
                notes.append(['Modul ogohlantirishi',json.dumps(warning,ensure_ascii=False,default=str)])
            for row in ws.iter_rows(min_row=2):
                for cell in row:
                    if isinstance(cell.value,str): cell.data_type='s'
            notes.freeze_panes='A2'; notes.auto_filter.ref=notes.dimensions
            notes.column_dimensions['A'].width=40; notes.column_dimensions['B'].width=100
            for row in notes:
                for cell in row:
                    if isinstance(cell.value,str): cell.data_type='s'
                    cell.alignment=Alignment(wrap_text=True)
            for cell in notes[1]:
                cell.font=Font(bold=True,color='FFFFFF'); cell.fill=PatternFill('solid',fgColor='175B42')
        elif kind=='expenses':
            ws.append(['Sana','Tizimga kiritgan shaxs','Toifa','Summa',
                       'To‘lov kanali','Pul manbai','Izoh'])
            expense_rows=query(con,'''SELECT e.*, entered.username AS entered_by_name,
                    source.role AS source_role
                    FROM expenses e
                    LEFT JOIN users entered ON entered.id=COALESCE(e.entered_by,e.user_id)
                    LEFT JOIN users source ON source.id=e.user_id
                    ORDER BY date,e.id''',table='expenses')
            for r in expense_rows:
                item=expense_display(r)
                ws.append([item['date'],item['entered_by_display'],
                           item['category'],item['amount'],
                           item['payment_method_display'],item['fund_source_display'],item['note']])
        elif kind=='stocks':
            ws.append(['Mahsulot','Joriy qoldiq kg'])
            purchased=con.execute('SELECT COALESCE(SUM(net),0) FROM wheat_receipts').fetchone()[0]
            used=wheat_warehouse_used(con)
            ws.append(['Bug‘doy',purchased-used])
            period = latest_period(con)
            measurement = latest_measurement(con, period)
            measurement_label = ('ROP oxirgi hujjatli fizik sanoqi · '
                                 + (measurement['day'] if measurement else 'noma’lum'))
            ws.append([measurement_label, measurement['qty_kg'] if measurement else None])
            if period and not period['stop_day']:
                rop_totals = period_totals(con, period)
                ws.append(['ROP davr kirimi', rop_totals['received']])
                ws.append(['ROP davr tozalangan bug‘doyi', rop_totals['cleaned']])
                ws.append(['ROP hisobiy farqi', rop_totals['calculated_difference']])
            for name,qty in product_stocks(con).items(): ws.append([name,qty])
        if kind == 'production':
            try:
                bulk = build_bulk_report(
                    con, date.fromisoformat(start) if start else date.min,
                    date.fromisoformat(end) if end else date.max,
                    _monthly_parse_date, lambda day: day.strftime('%d.%m.%Y'))
            except ValueError as exc:
                abort(400, str(exc))
            append_bulk_sheets(wb, bulk)
    if kind != 'production':
        # Explicit text columns: preserve user input verbatim, never as a formula.
        text_columns = {'wheat': (1,2,3,7), 'cleaning': (1,), 'sales': (1,2,3,4,5,14),
                        'debtors': (1,2,6), 'payments': (1,2,3,5),
                        'expenses': (1,2,3,5,6,7), 'stocks': (1,)}
        for row in ws.iter_rows(min_row=2):
            for column in text_columns[kind]:
                cell = row[column-1]
                if isinstance(cell.value, str):
                    cell.data_type = 's'
        ws.freeze_panes='A2'; ws.auto_filter.ref=ws.dimensions
        for cell in ws[1]:
            cell.font=Font(bold=True,color='FFFFFF');cell.fill=PatternFill('solid',fgColor='175B42');cell.alignment=Alignment(wrap_text=True)
        for col in ws.columns:
            letter=get_column_letter(col[0].column)
            ws.column_dimensions[letter].width=min(40,max(15,max(len(str(c.value or '')) for c in list(col)[:150])+2))
        date_columns = {'wheat': (1,), 'cleaning': (1,), 'sales': (1,14),
                        'debtors': (6,), 'payments': (1,), 'expenses': (1,), 'stocks': ()}
        try: format_date_columns(ws, date_columns[kind])
        except ValueError as exc: abort(400, str(exc))
    output=BytesIO();wb.save(output);output.seek(0)
    if kind == 'production':
        logo_path = Path(BASE_DIR) / 'static' / 'production_report_logo.png'
        if not logo_path.is_file():
            abort(500, 'Ishlab chiqarish Excel logotipi topilmadi.')
        logo_bytes = logo_path.read_bytes()
        spreadsheet_ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
        office_rel_ns = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
        package_rel_ns = 'http://schemas.openxmlformats.org/package/2006/relationships'
        content_ns = 'http://schemas.openxmlformats.org/package/2006/content-types'
        drawing_ns = 'http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing'
        drawingml_ns = 'http://schemas.openxmlformats.org/drawingml/2006/main'

        def xlsx_xml(root, default_namespace, prefixes=()):
            ET.register_namespace('', default_namespace)
            for prefix, namespace in prefixes:
                ET.register_namespace(prefix, namespace)
            return ET.tostring(root, encoding='utf-8', xml_declaration=True)

        def add_logo_to_workbook(workbook_bytes, image_bytes):
            replacements = {}
            additions = {}
            with ZipFile(BytesIO(workbook_bytes), 'r') as source:
                content_types = ET.fromstring(source.read('[Content_Types].xml'))
                if not any(
                        element.tag == f'{{{content_ns}}}Default'
                        and element.get('Extension') == 'png'
                        for element in content_types):
                    ET.SubElement(content_types, f'{{{content_ns}}}Default', {
                        'Extension': 'png', 'ContentType': 'image/png',
                    })
                for sheet_number in range(1, 7):
                    drawing_name = f'xl/drawings/drawing{sheet_number}.xml'
                    ET.SubElement(content_types, f'{{{content_ns}}}Override', {
                        'PartName': f'/{drawing_name}',
                        'ContentType': 'application/vnd.openxmlformats-officedocument.drawing+xml',
                    })
                    sheet_name = f'xl/worksheets/sheet{sheet_number}.xml'
                    sheet_root = ET.fromstring(source.read(sheet_name))
                    rel_name = f'xl/worksheets/_rels/sheet{sheet_number}.xml.rels'
                    if rel_name in source.namelist():
                        sheet_rels = ET.fromstring(source.read(rel_name))
                    else:
                        sheet_rels = ET.Element(f'{{{package_rel_ns}}}Relationships')
                    used_ids = {item.get('Id') for item in sheet_rels}
                    rel_id = 1
                    while f'rId{rel_id}' in used_ids:
                        rel_id += 1
                    drawing_rel_id = f'rId{rel_id}'
                    ET.SubElement(sheet_rels, f'{{{package_rel_ns}}}Relationship', {
                        'Id': drawing_rel_id,
                        'Type': f'{office_rel_ns}/drawing',
                        'Target': f'../drawings/drawing{sheet_number}.xml',
                    })
                    drawing_element = ET.Element(
                        f'{{{spreadsheet_ns}}}drawing',
                        {f'{{{office_rel_ns}}}id': drawing_rel_id},
                    )
                    insert_before = {
                        'legacyDrawing', 'legacyDrawingHF', 'picture', 'oleObjects',
                        'controls', 'webPublishItems', 'tableParts', 'extLst',
                    }
                    insert_at = next(
                        (index for index, child in enumerate(list(sheet_root))
                         if child.tag.rsplit('}', 1)[-1] in insert_before),
                        len(sheet_root),
                    )
                    sheet_root.insert(insert_at, drawing_element)
                    replacements[sheet_name] = xlsx_xml(
                        sheet_root, spreadsheet_ns, (('r', office_rel_ns),)
                    )
                    replacements[rel_name] = xlsx_xml(sheet_rels, package_rel_ns)

                    drawing_root = ET.Element(f'{{{drawing_ns}}}wsDr')
                    anchor = ET.SubElement(drawing_root, f'{{{drawing_ns}}}oneCellAnchor')
                    origin = ET.SubElement(anchor, f'{{{drawing_ns}}}from')
                    for name, value in (('col', '0'), ('colOff', '0'), ('row', '0'), ('rowOff', '0')):
                        ET.SubElement(origin, f'{{{drawing_ns}}}{name}').text = value
                    ET.SubElement(anchor, f'{{{drawing_ns}}}ext', {
                        'cx': '1047750', 'cy': '1114425',
                    })
                    picture = ET.SubElement(anchor, f'{{{drawing_ns}}}pic')
                    non_visual = ET.SubElement(picture, f'{{{drawing_ns}}}nvPicPr')
                    ET.SubElement(non_visual, f'{{{drawing_ns}}}cNvPr', {
                        'id': '1', 'name': f'Production report logo {sheet_number}',
                    })
                    ET.SubElement(
                        ET.SubElement(non_visual, f'{{{drawing_ns}}}cNvPicPr'),
                        f'{{{drawingml_ns}}}picLocks',
                        {'noChangeAspect': '1'},
                    )
                    fill = ET.SubElement(picture, f'{{{drawing_ns}}}blipFill')
                    ET.SubElement(fill, f'{{{drawingml_ns}}}blip', {
                        f'{{{office_rel_ns}}}embed': 'rId1',
                    })
                    stretch = ET.SubElement(fill, f'{{{drawingml_ns}}}stretch')
                    ET.SubElement(stretch, f'{{{drawingml_ns}}}fillRect')
                    shape_properties = ET.SubElement(picture, f'{{{drawing_ns}}}spPr')
                    geometry = ET.SubElement(shape_properties, f'{{{drawingml_ns}}}prstGeom', {
                        'prst': 'rect',
                    })
                    ET.SubElement(geometry, f'{{{drawingml_ns}}}avLst')
                    ET.SubElement(anchor, f'{{{drawing_ns}}}clientData')
                    additions[drawing_name] = xlsx_xml(
                        drawing_root, drawing_ns,
                        (('a', drawingml_ns), ('r', office_rel_ns)),
                    )
                    drawing_rel_root = ET.Element(f'{{{package_rel_ns}}}Relationships')
                    ET.SubElement(drawing_rel_root, f'{{{package_rel_ns}}}Relationship', {
                        'Id': 'rId1',
                        'Type': f'{office_rel_ns}/image',
                        'Target': '../media/production_report_logo.png',
                    })
                    additions[
                        f'xl/drawings/_rels/drawing{sheet_number}.xml.rels'
                    ] = xlsx_xml(drawing_rel_root, package_rel_ns)
                replacements['[Content_Types].xml'] = xlsx_xml(
                    content_types, content_ns
                )
                additions['xl/media/production_report_logo.png'] = image_bytes
                result = BytesIO()
                with ZipFile(result, 'w', ZIP_DEFLATED) as destination:
                    for item in source.infolist():
                        destination.writestr(
                            item, replacements.get(item.filename, source.read(item.filename))
                        )
                    for name, data in additions.items():
                        destination.writestr(name, data)
            return result.getvalue()

        output = BytesIO(add_logo_to_workbook(output.getvalue(), logo_bytes))
        output.seek(0)
    return send_file(output,as_attachment=True,download_name=f'tortkuldon_{kind}.xlsx',
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

