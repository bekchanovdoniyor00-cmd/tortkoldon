"""Excel endpoint body for unified_excel_report; unchanged calculations and workbook layout.
The Flask route and authorization remain in app.py.
"""


def export_unified_excel_report(_app_dependencies):
    BASE_DIR = _app_dependencies['BASE_DIR']
    Decimal = _app_dependencies['Decimal']
    Path = _app_dependencies['Path']
    ROLES = _app_dependencies['ROLES']
    abort = _app_dependencies['abort']
    calculate_financial_snapshot = _app_dependencies['calculate_financial_snapshot']
    customer_display = _app_dependencies['customer_display']
    customer_identity_context = _app_dependencies['customer_identity_context']
    date = _app_dependencies['date']
    db = _app_dependencies['db']
    request = _app_dependencies['request']
    resolve_customer_identity = _app_dependencies['resolve_customer_identity']
    resolve_customer_name = _app_dependencies['resolve_customer_name']
    sqlite3 = _app_dependencies['sqlite3']
    from io import BytesIO
    from datetime import datetime
    from flask import send_file
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    from date_sql import mixed_date_to_iso

    start = request.args.get('start', '').strip()
    end = request.args.get('end', '').strip()
    try:
        start = mixed_date_to_iso(start) if start else ''
        end = mixed_date_to_iso(end) if end else ''
        if start and end and start > end:
            raise ValueError()
    except ValueError:
        abort(400, 'Sana oralig‘i noto‘g‘ri; YYYY-MM-DD yoki DD-MM-YYYY formatidan foydalaning.')

    def date_key(value):
        return mixed_date_to_iso(value)

    cutoff = end or date.today().isoformat()

    def in_period(value):
        key = date_key(value)
        return (not start or key >= start) and key <= cutoff

    def date_value(value):
        return datetime.strptime(date_key(value), '%Y-%m-%d') if value else None

    def period_text():
        if not start and not end:
            return 'Barcha sanalar'
        return f'{date_value(start).strftime("%d.%m.%Y") if start else "Boshlanishi"} — {date_value(end).strftime("%d.%m.%Y") if end else "Bugungi holat"}'

    zero = Decimal('0')
    balance_epsilon = Decimal('0.000001')

    def amount(value):
        return Decimal(str(value or 0))

    def report_balance(value):
        return zero if abs(value) <= balance_epsilon else value

    def payment_method(value):
        return {'naqd': 'Naqd', 'plastik': 'Plastik', 'hisob': 'Bank'}.get(
            value, value or 'Aniqlanmagan')

    def status_for(balance, paid):
        balance = report_balance(balance)
        if balance <= zero:
            return 'To‘langan'
        return 'Qisman to‘langan' if paid > zero else 'Qarzdor'

    try:
        con = db()
        try:
            con.execute('BEGIN')
            customer_context = customer_identity_context(con)
            users = {
                row['id']: ROLES.get(row['role'], row['username'])
                for row in con.execute('SELECT id,username,role FROM users')
            }
            all_sales = [dict(row) for row in con.execute("""
                SELECT s.*, i.number AS invoice_number
                FROM sales s LEFT JOIN invoices i ON i.sale_id=s.id
                ORDER BY business_date_key(s.date),s.id
            """)]
            cancellation_rows = [dict(row) for row in con.execute(
                'SELECT sale_id,cancelled_at FROM sale_cancellations')]
            cancelled_at_cutoff = {
                row['sale_id'] for row in cancellation_rows
                if not row['cancelled_at']
                or date_key(str(row['cancelled_at'])[:10]) <= cutoff
            }
            active_period_sales = [
                row for row in all_sales
                if row['id'] not in cancelled_at_cutoff and in_period(row['date'])
            ]
            period_sales = [row for row in all_sales if in_period(row['date'])]
            receipts = [dict(row) for row in con.execute(
                'SELECT * FROM worker_receipts ORDER BY id')]
            legacy_payments = [dict(row) for row in con.execute(
                'SELECT * FROM payments ORDER BY id')]
            direct_receipts = {
                row['receipt_id']: dict(row)
                for row in con.execute('SELECT * FROM accountant_worker_receipts')
            }
            batch_by_sale = {
                row['sale_id']: row['batch_id']
                for row in con.execute('SELECT sale_id,batch_id FROM worker_sale_batch_items')
            }
            batch_channels = {}
            for row in con.execute(
                    'SELECT batch_id,payment_method,amount FROM worker_sale_batch_payments ORDER BY id'):
                batch_channels.setdefault(row['batch_id'], []).append(
                    f"{payment_method(row['payment_method'])}: {amount(row['amount']):,.0f}")

            def financial_as_of(connection, cutoff_date):
                snapshot = sqlite3.connect(':memory:')
                snapshot.row_factory = sqlite3.Row
                try:
                    connection.backup(snapshot)
                    as_of_tables = (
                        ('sales', 'date'),
                        ('opening_debts', 'date'),
                        ('opening_debt_receipts', 'date'),
                        ('worker_receipts', 'date'),
                        ('accountant_worker_receipts', 'payment_date'),
                        ('payments', 'date'),
                    )
                    for table, date_column in as_of_tables:
                        expired = [
                            row['id'] for row in snapshot.execute(
                                f'SELECT id,{date_column} FROM {table}')
                            if date_key(row[date_column]) > cutoff_date
                        ]
                        snapshot.executemany(
                            f'DELETE FROM {table} WHERE id=?',
                            ((row_id,) for row_id in expired))
                    expired_cancellations = []
                    for row in snapshot.execute(
                            'SELECT sale_id,cancelled_at FROM sale_cancellations'):
                        cancellation_date = str(row['cancelled_at'] or '')[:10]
                        if cancellation_date and date_key(cancellation_date) > cutoff_date:
                            expired_cancellations.append((row['sale_id'],))
                    snapshot.executemany(
                        'DELETE FROM sale_cancellations WHERE sale_id=?',
                        expired_cancellations)
                    return calculate_financial_snapshot(snapshot)
                finally:
                    snapshot.close()

            historical = financial_as_of(con, cutoff)
            sales_debt_balance = sum(
                (report_balance(obligation['balance'])
                 for group in historical['customers'].values()
                 for obligation in group['obligations']
                 if obligation['kind'] == 'sale'),
                zero,
            )
            opening_debt_balance = sum(
                (report_balance(obligation['balance'])
                 for group in historical['customers'].values()
                 for obligation in group['obligations']
                 if obligation['kind'] == 'opening'),
                zero,
            )
            prior_period_debt = sum(
                (report_balance(obligation['balance'])
                 for group in historical['customers'].values()
                 for obligation in group['obligations']
                 if start and date_key(obligation['date']) < start),
                zero,
            )
            allocation = {
                item['id']: item
                for group in historical['customers'].values()
                for item in group['obligations']
                if item['kind'] == 'sale'
            }
            unallocated_legacy_ids = {
                row['id'] for row in historical['unallocated_legacy']
            }
            def display_customer(name, phone):
                return customer_display(customer_context, name or '', phone or '')

            def invoice_label(row):
                sale_id = f"S-{row['id']}"
                return (
                    f"{sale_id} / № {row['invoice_number']}"
                    if row.get('invoice_number') else sale_id
                )

            # Payment rows intentionally use source events; accountant records classify
            # linked receipts and are never added as an additional receipt.
            events = []
            sales_by_id = {row['id']: row for row in all_sales}
            for sale in all_sales:
                paid = amount(sale['paid'])
                if paid <= zero or not in_period(sale['date']):
                    continue
                recipient_id = sale.get('initial_payment_recipient_id') or sale.get('user_id')
                note = sale.get('note') or ''
                batch_id = batch_by_sale.get(sale['id'])
                method = payment_method(sale.get('payment_method'))
                if batch_id is not None:
                    method = 'Aniqlanmagan'
                    batch_note = (
                        f" | Paket {batch_id}: avans sotuvlar orasida taqsimlangan; "
                        f"kanallar qayta qo‘shilmaydi ({'; '.join(batch_channels.get(batch_id, []))})."
                    )
                    note = (note + batch_note).strip()
                if sale['id'] in cancelled_at_cutoff:
                    note = (note + ' | Bekor qilingan sotuvdagi to‘lov; qaytarilgan deb hisoblanmadi.').strip()
                events.append({
                    'date': sale['date'], 'id': f"S-{sale['id']}-AVANS",
                    'customer': display_customer(sale['customer'], sale['customer_phone'])[0],
                    'sale_id': sale['id'], 'amount': paid,
                    'method': method,
                    'recipient': users.get(recipient_id, 'Aniqlanmagan'),
                    'owner': users.get(sale.get('user_id'), 'Aniqlanmagan'),
                    'note': note, 'kind': 'initial',
                })
            for row in legacy_payments:
                if not in_period(row['date']):
                    continue
                card = resolve_customer_name(customer_context, row['customer'])
                customer_name = card['name'] if card else row['customer']
                note = row.get('note') or ''
                if row['id'] in unallocated_legacy_ids:
                    note = (
                        note + ' | Eski to‘lov ishonchli majburiyat/xodimga '
                        'bog‘lanmagani uchun qarzdan chegirilmadi.'
                    ).strip()
                events.append({
                    'date': row['date'], 'id': f"P-{row['id']}",
                    'customer': customer_name, 'sale_id': '',
                    'amount': amount(row['amount']),
                    'method': 'Aniqlanmagan', 'recipient': 'Aniqlanmagan',
                    'owner': 'Aniqlanmagan', 'note': note,
                    'kind': 'legacy',
                })
            for receipt in receipts:
                if not in_period(receipt['date']):
                    continue
                sale = sales_by_id.get(receipt['sale_id'])
                direct = direct_receipts.get(receipt['id'])
                recipient_id = direct['accountant_id'] if direct else receipt['worker_id']
                note = direct.get('note', '') if direct else ''
                if not sale or receipt['sale_id'] in cancelled_at_cutoff:
                    note = (note + ' | Faol sotuvga bog‘lanmagan to‘lov.').strip()
                if direct and (
                        direct['amount'] != receipt['amount']
                        or direct['worker_id'] != receipt['worker_id']
                        or direct['payment_method'] != receipt.get('payment_method')):
                    note = (note + ' | Buxgalter belgilashi asosiy to‘lovga mos emas.').strip()
                name, phone = display_customer(
                    sale.get('customer', '') if sale else '',
                    sale.get('customer_phone', '') if sale else '')
                events.append({
                    'date': receipt['date'], 'id': f"T-{receipt['id']}",
                    'customer': name, 'sale_id': receipt['sale_id'],
                    'amount': amount(receipt['amount']),
                    'method': payment_method(receipt.get('payment_method')),
                    'recipient': users.get(recipient_id, 'Aniqlanmagan'),
                    'owner': users.get(sale.get('user_id'), 'Aniqlanmagan') if sale else 'Aniqlanmagan',
                    'note': note, 'kind': 'linked',
                })
            events.sort(key=lambda row: (date_key(row['date']), row['kind'], row['id']))
            period_paid = sum((row['amount'] for row in events), zero)
            period_sales_total = sum(
                (amount(row['qty_kg']) * amount(row['price_per_kg'])
                 for row in active_period_sales), zero)

            debt_rows = []
            debt_by_customer_id = {}
            for group in historical['customers'].values():
                if group['debt'] <= zero:
                    continue
                customer_id = group.get('customer_id')
                card = customer_context['cards'].get(customer_id)
                obligation_ids = []
                workers = set()
                for obligation in group['obligations']:
                    if obligation['balance'] <= zero:
                        continue
                    if obligation['kind'] == 'sale':
                        obligation_ids.append(f"S-{obligation['id']}")
                        sale = sales_by_id.get(obligation['id'])
                        if sale and sale.get('user_id') is not None:
                            workers.add(users.get(sale['user_id'], 'Aniqlanmagan'))
                    else:
                        obligation_ids.append(f"Q-{obligation['id']}")
                if len(workers) == 1:
                    owner = next(iter(workers))
                elif workers:
                    owner = 'Aralash: ' + ', '.join(sorted(workers))
                else:
                    owner = 'Aniqlanmagan'
                last_dates = []
                sale_obligation_ids = {
                    item['id'] for item in group['obligations']
                    if item['kind'] == 'sale'
                }
                for sale_id in sale_obligation_ids:
                    sale = sales_by_id.get(sale_id)
                    if sale and amount(sale['paid']) > zero:
                        last_dates.append(sale['date'])
                for receipt in historical['receipts']:
                    if receipt['sale_id'] in sale_obligation_ids:
                        last_dates.append(receipt['date'])
                legacy_ids = set(group.get('legacy_payment_ids', []))
                last_dates.extend(
                    row['date'] for row in legacy_payments
                    if row['id'] in legacy_ids)
                last_payment = max(last_dates, key=date_key) if last_dates else ''
                customer_name = card['name'] if card else group['customer']
                customer_phone = card['phone'] if card else group['phone']
                debt_rows.append({
                    'customer_id': customer_id,
                    'name': customer_name, 'phone': customer_phone,
                    'owner': owner, 'total': group['sales_revenue'] + group['opening_debt'],
                    'paid': group['received'], 'debt': group['debt'],
                    'prior_debt': sum(
                        (report_balance(item['balance']) for item in group['obligations']
                         if (start and date_key(item['date']) < start)
                         or (not start and item['kind'] == 'opening')),
                        zero,
                    ),
                    'last_payment': last_payment,
                    'status': status_for(group['debt'], group['received']),
                    'ids': ', '.join(obligation_ids),
                })
                if customer_id is not None:
                    prior = debt_by_customer_id.get(customer_id, zero)
                    debt_by_customer_id[customer_id] = prior + group['debt']
            debt_rows.sort(key=lambda row: (row['name'].casefold(), row['phone']))

            cards = [dict(row) for row in con.execute(
                'SELECT id,name,phone FROM customers ORDER BY name,id')]
            period_sales_by_customer = {}
            period_paid_by_customer = {}
            last_sale_by_customer = {}
            for sale in all_sales:
                if sale['id'] in cancelled_at_cutoff:
                    continue
                key = date_key(sale['date'])
                card = resolve_customer_identity(
                    customer_context, sale['customer'], sale['customer_phone'])
                if not card:
                    continue
                if key <= cutoff:
                    previous = last_sale_by_customer.get(card['id'])
                    if previous is None or key > previous[0]:
                        last_sale_by_customer[card['id']] = (key, sale['date'])
                if in_period(sale['date']):
                    period_sales_by_customer[card['id']] = (
                        period_sales_by_customer.get(card['id'], zero)
                        + amount(sale['qty_kg']) * amount(sale['price_per_kg']))
            for event in events:
                customer_id = None
                if event['kind'] == 'initial':
                    sale = sales_by_id.get(event['sale_id'])
                    if sale:
                        card = resolve_customer_identity(
                            customer_context, sale['customer'], sale['customer_phone'])
                        customer_id = card['id'] if card else None
                elif event['kind'] == 'linked':
                    sale = sales_by_id.get(event['sale_id'])
                    if sale:
                        card = resolve_customer_identity(
                            customer_context, sale['customer'], sale['customer_phone'])
                        customer_id = card['id'] if card else None
                elif event['kind'] == 'legacy':
                    card = resolve_customer_name(customer_context, event['customer'])
                    customer_id = card['id'] if card else None
                if customer_id is not None:
                    period_paid_by_customer[customer_id] = (
                        period_paid_by_customer.get(customer_id, zero) + event['amount'])

            customer_rows = []
            for card in cards:
                customer_id = card['id']
                sales_total = period_sales_by_customer.get(customer_id, zero)
                paid_total = period_paid_by_customer.get(customer_id, zero)
                debt = debt_by_customer_id.get(customer_id, zero)
                if not (sales_total or paid_total or debt):
                    continue
                historical_group = next(
                    (group for group in historical['customers'].values()
                     if group.get('customer_id') == customer_id), None)
                cumulative_paid = historical_group['received'] if historical_group else paid_total
                customer_rows.append({
                    'id': customer_id, 'name': card['name'], 'phone': card['phone'],
                    'sales': sales_total, 'paid': paid_total, 'debt': debt,
                    'last_sale': last_sale_by_customer.get(customer_id, (None, ''))[1],
                    'status': status_for(debt, cumulative_paid),
                })
            customer_rows.sort(key=lambda row: (row['name'].casefold(), row['id']))

            wb = Workbook()
            summary = wb.active
            summary.title = 'JAMLANMA'
            sales_ws = wb.create_sheet('SOTUVLAR')
            debt_ws = wb.create_sheet('QARZLAR')
            payments_ws = wb.create_sheet('TO‘LOVLAR')
            customers_ws = wb.create_sheet('MIJOZLAR')
            wb.properties.title = 'TO‘RTKO‘LDONMAHSULOTI yagona moliyaviy hisoboti'
            wb.properties.subject = f'Yagona hisobot: {period_text()}'

            dark_green = '075B3B'
            mid_green = '128451'
            pale_green = 'EAF5EE'
            white = 'FFFFFF'
            light_border = Side(style='thin', color='D8E5DC')
            money_format = '#,##0.## "so‘m"'
            number_format = '#,##0.##'
            date_format = 'dd.mm.yyyy'

            def paint_table(ws, title, headers, rows, widths, status_index=None,
                            total_values=None, header_row=4, title_row=1,
                            subtitle_row=2, period_note=None, money_columns=()):
                last_column = len(headers)
                ws.merge_cells(start_row=title_row, start_column=1,
                               end_row=title_row, end_column=last_column)
                title_cell = ws.cell(title_row, 1, title)
                title_cell.fill = PatternFill('solid', fgColor=dark_green)
                title_cell.font = Font(name='Aptos Display', size=16, bold=True, color=white)
                title_cell.alignment = Alignment(vertical='center')
                ws.row_dimensions[title_row].height = 30
                ws.merge_cells(start_row=subtitle_row, start_column=1,
                               end_row=subtitle_row, end_column=last_column)
                subtitle = period_note or (
                    f"Hisobot davri: {period_text()}. Sotuv/to‘lov aylanmasi davr bo‘yicha; "
                    "qarz qoldig‘i tanlangan davr oxirida, oldingi qarzlar bilan."
                )
                subtitle_cell = ws.cell(subtitle_row, 1, subtitle)
                subtitle_cell.font = Font(size=10, italic=True, color='45564B')
                subtitle_cell.alignment = Alignment(wrap_text=True, vertical='center')
                ws.row_dimensions[subtitle_row].height = 30
                for column, heading in enumerate(headers, 1):
                    cell = ws.cell(header_row, column, heading)
                    cell.fill = PatternFill('solid', fgColor=dark_green)
                    cell.font = Font(bold=True, color=white)
                    cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
                    cell.border = Border(bottom=Side(style='medium', color=mid_green))
                ws.row_dimensions[header_row].height = 32
                for row_index, values in enumerate(rows, header_row + 1):
                    for column, value in enumerate(values, 1):
                        cell = ws.cell(row_index, column, value)
                        cell.fill = PatternFill(
                            'solid', fgColor=white if (row_index - header_row) % 2 else pale_green)
                        cell.alignment = Alignment(vertical='top', wrap_text=True)
                        cell.border = Border(bottom=light_border)
                        if isinstance(value, str):
                            cell.data_type = 's'
                        elif isinstance(value, datetime):
                            cell.number_format = date_format
                        elif isinstance(value, Decimal):
                            cell.number_format = (
                                money_format if column in money_columns else number_format)
                            cell.alignment = Alignment(horizontal='right', vertical='top')
                        elif isinstance(value, (int, float)) and not isinstance(value, bool):
                            cell.number_format = number_format
                            cell.alignment = Alignment(horizontal='right', vertical='top')
                    if status_index is not None:
                        status_cell = ws.cell(row_index, status_index)
                        color = {
                            'To‘langan': 'D9EFD9',
                            'Qisman to‘langan': 'FFF0C2',
                            'Qarzdor': 'FCE0E0',
                            'Bekor qilingan': 'E8E8E8',
                        }.get(status_cell.value)
                        if color:
                            status_cell.fill = PatternFill('solid', fgColor=color)
                            status_cell.font = Font(bold=True)
                last_data_row = header_row + len(rows)
                if total_values is not None:
                    total_row = last_data_row + 1
                    for column, value in enumerate(total_values, 1):
                        cell = ws.cell(total_row, column, value)
                        cell.fill = PatternFill('solid', fgColor='DCEFE2')
                        cell.font = Font(bold=True, color='163D2B')
                        cell.border = Border(top=Side(style='medium', color=mid_green))
                        if isinstance(value, Decimal):
                            cell.number_format = (
                                money_format if column in money_columns else number_format)
                            cell.alignment = Alignment(horizontal='right')
                    last_data_row = total_row
                ws.auto_filter.ref = (
                    f'A{header_row}:{get_column_letter(last_column)}'
                    f'{max(header_row, header_row + len(rows))}'
                )
                ws.freeze_panes = f'A{header_row + 1}'
                ws.sheet_view.showGridLines = False
                ws.page_setup.orientation = 'landscape'
                ws.page_setup.paperSize = ws.PAPERSIZE_A4
                ws.page_setup.fitToWidth = 1
                ws.page_setup.fitToHeight = 0
                ws.sheet_properties.pageSetUpPr.fitToPage = True
                ws.print_title_rows = f'{header_row}:{header_row}'
                ws.print_area = f'A1:{get_column_letter(last_column)}{last_data_row}'
                for column, width in enumerate(widths, 1):
                    ws.column_dimensions[get_column_letter(column)].width = width
                return last_data_row

            summary_headers = [
                'Sana', 'Hujjat №', 'Xaridor', 'Telefon', 'Mahsulot',
                'Sotuv summasi', 'To‘langan summa', 'Qolgan qarz', 'Holat',
            ]
            summary_rows = []
            for sale in active_period_sales:
                item = allocation.get(sale['id'])
                if not item:
                    continue
                name, phone = display_customer(sale['customer'], sale['customer_phone'])
                paid_to_date = item['initial_paid'] + item['linked_paid'] + item['legacy_applied']
                summary_rows.append([
                    date_value(sale['date']), invoice_label(sale), name, phone,
                    sale['product'], item['amount'], paid_to_date,
                    report_balance(item['balance']),
                    status_for(item['balance'], paid_to_date),
                ])

            summary.merge_cells('C1:I1')
            summary.merge_cells('C2:I2')
            summary.merge_cells('C3:I3')
            summary['C1'] = 'TO‘RTKO‘LDONMAHSULOTI MCHJ'
            summary['C1'].font = Font(name='Aptos Display', size=20, bold=True, color=dark_green)
            summary['C2'] = 'Yagona hisobot (Sotuvlar • Qarzlar • To‘lovlar)'
            summary['C2'].font = Font(size=13, bold=True, color='233C30')
            summary['C3'] = f'Hisobot davri: {period_text()}'
            summary['C3'].font = Font(size=10, italic=True, color='526458')
            for cell in ('C1', 'C2', 'C3'):
                summary[cell].alignment = Alignment(vertical='center')
            summary.row_dimensions[1].height = 34
            summary.row_dimensions[2].height = 26
            summary.row_dimensions[3].height = 22
            logo_path = Path(BASE_DIR) / 'static' / 'company_logo.png'
            if not logo_path.is_file():
                abort(500, 'Korxona logotipi topilmadi; yagona hisobot yaratilmadi.')
            cards = [
                ('📈 JAMI SOTUV', period_sales_total, 'DDF1E3', '17643C'),
                ('💳 JAMI TO‘LOV', period_paid, 'E8F5EA', '2A7B4B'),
                ('⚠ JAMI QARZ (DAVR OXIRI)', historical['totals']['debt'], 'FCE6E6', 'B62828'),
                ('👥 QARZDORLAR SONI', Decimal(len(debt_rows)), 'E4F1FA', '17608A'),
            ]
            card_ranges = [('A4:B4', 'A5:B5'), ('C4:D4', 'C5:D5'),
                           ('E4:F4', 'E5:F5'), ('G4:I4', 'G5:I5')]
            for (label, value, fill, color), (label_range, value_range) in zip(cards, card_ranges):
                summary.merge_cells(label_range)
                summary.merge_cells(value_range)
                label_cell = summary[label_range.split(':')[0]]
                value_cell = summary[value_range.split(':')[0]]
                label_cell.value = label
                value_cell.value = value
                for ref in (label_range, value_range):
                    for row in summary[ref]:
                        for cell in row:
                            cell.fill = PatternFill('solid', fgColor=fill)
                            cell.border = Border(
                                left=light_border, right=light_border,
                                top=light_border, bottom=light_border)
                label_cell.font = Font(size=11, bold=True, color=color)
                value_cell.font = Font(size=18, bold=True, color=color)
                value_cell.number_format = '0 "ta"' if 'QARZDORLAR SONI' in label else money_format
                label_cell.alignment = Alignment(horizontal='center', vertical='center')
                value_cell.alignment = Alignment(horizontal='center', vertical='center')
            summary.row_dimensions[4].height = 30
            summary.row_dimensions[5].height = 44
            summary.merge_cells('A6:I6')
            debt_note = summary['A6']
            debt_note.value = (
                f"Qarz tarkibi — sotuvlar qoldig‘i: {sales_debt_balance:,.0f} so‘m; "
                f"boshlang‘ich qarzlar qoldig‘i: {opening_debt_balance:,.0f} so‘m; "
                f"umumiy qarz: {historical['totals']['debt']:,.0f} so‘m."
            )
            if start:
                debt_note.value += (
                    f" Davr boshlanishigacha bo‘lgan qoldiq: "
                    f"{prior_period_debt:,.0f} so‘m."
                )
            debt_note.font = Font(size=10, italic=True, color='45564B')
            debt_note.fill = PatternFill('solid', fgColor='F1F6F2')
            debt_note.alignment = Alignment(vertical='center', wrap_text=True)
            summary.row_dimensions[6].height = 30
            summary_headers_row = 7
            for column, heading in enumerate(summary_headers, 1):
                cell = summary.cell(summary_headers_row, column, heading)
                cell.fill = PatternFill('solid', fgColor=dark_green)
                cell.font = Font(bold=True, color=white)
                cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
                cell.border = Border(bottom=Side(style='medium', color=mid_green))
            summary.row_dimensions[summary_headers_row].height = 32
            for row_index, values in enumerate(summary_rows, summary_headers_row + 1):
                for column, value in enumerate(values, 1):
                    cell = summary.cell(row_index, column, value)
                    cell.fill = PatternFill(
                        'solid', fgColor=white if (row_index - summary_headers_row) % 2 else pale_green)
                    cell.alignment = Alignment(vertical='top', wrap_text=True)
                    cell.border = Border(bottom=light_border)
                    if isinstance(value, str):
                        cell.data_type = 's'
                    elif isinstance(value, datetime):
                        cell.number_format = date_format
                    elif isinstance(value, Decimal):
                        cell.number_format = (
                            money_format if column in (6, 7, 8) else number_format)
                        cell.alignment = Alignment(horizontal='right')
                status_cell = summary.cell(row_index, 9)
                status_cell.fill = PatternFill('solid', fgColor={
                    'To‘langan': 'D9EFD9', 'Qisman to‘langan': 'FFF0C2',
                    'Qarzdor': 'FCE0E0',
                }[status_cell.value])
                status_cell.font = Font(bold=True)
            summary_total_row = summary_headers_row + len(summary_rows) + 1
            summary_totals = [
                'JAMI', '', '', '', '',
                sum((row[5] for row in summary_rows), zero),
                sum((row[6] for row in summary_rows), zero),
                sum((row[7] for row in summary_rows), zero), '',
            ]
            for column, value in enumerate(summary_totals, 1):
                cell = summary.cell(summary_total_row, column, value)
                cell.fill = PatternFill('solid', fgColor='DCEFE2')
                cell.font = Font(bold=True, color='163D2B')
                cell.border = Border(top=Side(style='medium', color=mid_green))
                if isinstance(value, Decimal):
                    cell.number_format = (
                        money_format if column in (6, 7, 8) else number_format)
                    cell.alignment = Alignment(horizontal='right')
            summary.auto_filter.ref = (
                f'A7:I{max(7, 7 + len(summary_rows))}')
            summary.freeze_panes = 'A8'
            summary.sheet_view.showGridLines = False
            summary.page_setup.orientation = 'landscape'
            summary.page_setup.paperSize = summary.PAPERSIZE_A4
            summary.page_setup.fitToWidth = 1
            summary.page_setup.fitToHeight = 0
            summary.sheet_properties.pageSetUpPr.fitToPage = True
            summary.print_title_rows = '7:7'
            summary.print_area = f'A1:I{summary_total_row}'
            for column, width in enumerate((16, 23, 32, 22, 28, 21, 21, 21, 22), 1):
                summary.column_dimensions[get_column_letter(column)].width = width

            sales_headers = [
                'Sana', 'Sotuv ID / hujjat №', 'Xaridor nomi', 'Telefon',
                'Mahsulot', 'Qop og‘irligi (kg)', 'Qop soni', 'Jami kg',
                'Birlik narxi', 'Sotuv summasi', 'Sotuv xodimi',
                'Sotuv vaqtidagi to‘lov', 'Keyingi to‘lovlar (davr oxirigacha)',
                'Eski to‘lovlardan FIFO', 'Qolgan qarz', 'Holat',
            ]
            sales_data = []
            active_sales_amount = active_sales_initial = active_sales_linked = zero
            active_sales_legacy = active_sales_debt = zero
            historical_receipts = {
                row['id']: row for row in historical['receipts']
            }
            for sale in period_sales:
                item = allocation.get(sale['id'])
                initial = amount(sale['paid'])
                if sale['id'] in cancelled_at_cutoff:
                    linked = sum(
                        (row['amount'] for row in historical_receipts.values()
                         if row['sale_id'] == sale['id']), zero)
                    legacy_applied = zero
                    balance = zero
                    label = 'Bekor qilingan'
                    sale_amount = amount(sale['qty_kg']) * amount(sale['price_per_kg'])
                else:
                    if not item:
                        continue
                    linked = item['linked_paid']
                    legacy_applied = item['legacy_applied']
                    balance = report_balance(item['balance'])
                    label = status_for(balance, initial + linked + legacy_applied)
                    sale_amount = item['amount']
                    active_sales_amount += sale_amount
                    active_sales_initial += initial
                    active_sales_linked += linked
                    active_sales_legacy += legacy_applied
                    active_sales_debt += balance
                name, phone = display_customer(sale['customer'], sale['customer_phone'])
                sales_data.append([
                    date_value(sale['date']), invoice_label(sale), name, phone,
                    sale['product'], sale.get('bag_kg'), sale.get('bag_count'),
                    amount(sale['qty_kg']), amount(sale.get('unit_price') or sale['price_per_kg']),
                    sale_amount, users.get(sale.get('user_id'), 'Aniqlanmagan'),
                    initial, linked, legacy_applied, balance, label,
                ])
            sales_total = [
                'JAMI', '', '', '', '', '', '', None, None,
                active_sales_amount, '', active_sales_initial, active_sales_linked,
                active_sales_legacy, active_sales_debt, '',
            ]
            paint_table(
                sales_ws, 'SOTUVLAR — sotuvlar va ularning to‘lov holati',
                sales_headers, sales_data,
                [13, 19, 25, 18, 23, 16, 12, 14, 17, 18, 18, 19, 23, 20, 18, 20],
                status_index=16, total_values=sales_total,
                money_columns=(9, 10, 12, 13, 14, 15),
            )

            debt_headers = [
                'Xaridor nomi', 'Telefon', 'Mas’ul xodim',
                'Sotuvlar / eski qarzlar summasi', 'To‘langan jami',
                'Qolgan qarz', 'Oxirgi to‘lov sanasi', 'Holat',
                'Bog‘langan sotuv/qarz IDlari',
                ('Davrdan oldingi qarz qoldig‘i' if start
                 else 'Boshlang‘ich qarz qoldig‘i'),
            ]
            debt_data = [[
                row['name'], row['phone'], row['owner'], row['total'],
                row['paid'], row['debt'], date_value(row['last_payment']),
                row['status'], row['ids'], row['prior_debt'],
            ] for row in debt_rows]
            debt_total = [
                'JAMI', '', '', sum((row['total'] for row in debt_rows), zero),
                sum((row['paid'] for row in debt_rows), zero),
                sum((row['debt'] for row in debt_rows), zero), '', '', '',
                sum((row['prior_debt'] for row in debt_rows), zero),
            ]
            paint_table(
                debt_ws, 'QARZLAR — davr oxiridagi ochiq majburiyatlar',
                debt_headers, debt_data,
                [25, 18, 22, 25, 20, 20, 19, 20, 32, 27],
                status_index=8, total_values=debt_total,
                money_columns=(4, 5, 6, 10),
            )

            payment_headers = [
                'To‘lov sanasi', 'To‘lov ID', 'Xaridor',
                'Bog‘langan sotuv ID', 'To‘lov summasi', 'To‘lov turi',
                'Pulni qabul qilgan shaxs', 'Sotuv uchun mas’ul xodim', 'Izoh',
            ]
            payment_data = [[
                date_value(row['date']), row['id'], row['customer'],
                f"S-{row['sale_id']}" if row['sale_id'] else '',
                row['amount'], row['method'], row['recipient'], row['owner'],
                row['note'],
            ] for row in events]
            payment_total = [
                'JAMI', '', '', '', period_paid, '', '', '',
                'Kassa ichidagi o‘tkazmalar va accountant_worker_receipts dublikatlari kiritilmagan.',
            ]
            paint_table(
                payments_ws, 'TO‘LOVLAR — davrda haqiqatan qabul qilingan mablag‘lar',
                payment_headers, payment_data,
                [16, 19, 26, 20, 20, 19, 24, 24, 55],
                total_values=payment_total,
                money_columns=(5,),
            )

            customer_headers = [
                'Xaridor ID', 'Hozirgi to‘g‘ri nomi', 'Telefon',
                'Jami xarid summasi (davr)', 'Jami to‘langan summa (davr)',
                'Qolgan qarz (davr oxiri)', 'Oxirgi sotuv sanasi', 'Holat',
            ]
            customer_data = [[
                row['id'], row['name'], row['phone'], row['sales'], row['paid'],
                row['debt'], date_value(row['last_sale']), row['status'],
            ] for row in customer_rows]
            customer_total = [
                'JAMI', '', '',
                sum((row['sales'] for row in customer_rows), zero),
                sum((row['paid'] for row in customer_rows), zero),
                sum((row['debt'] for row in customer_rows), zero), '', '',
            ]
            paint_table(
                customers_ws, 'MIJOZLAR — customers.id bo‘yicha jamlangan',
                customer_headers, customer_data,
                [14, 28, 19, 25, 26, 25, 19, 20],
                status_index=8, total_values=customer_total,
                money_columns=(4, 5, 6),
            )

            for ws in wb.worksheets:
                ws.sheet_properties.pageSetUpPr.fitToPage = True
                ws.oddFooter.center.text = 'TO‘RTKO‘LDONMAHSULOTI MCHJ — maxfiy ichki hisobot'
                ws.oddFooter.right.text = 'Sahifa &[Page] / &[Pages]'
            output = BytesIO()
            wb.save(output)
            import xml.etree.ElementTree as ET
            from zipfile import ZIP_DEFLATED, ZipFile

            spreadsheet_ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
            office_rel_ns = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
            package_rel_ns = 'http://schemas.openxmlformats.org/package/2006/relationships'
            content_ns = 'http://schemas.openxmlformats.org/package/2006/content-types'
            drawing_ns = 'http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing'
            drawingml_ns = 'http://schemas.openxmlformats.org/drawingml/2006/main'

            def serialize_xml(root, default_namespace, prefixes=()):
                ET.register_namespace('', default_namespace)
                for prefix, namespace in prefixes:
                    ET.register_namespace(prefix, namespace)
                return ET.tostring(root, encoding='utf-8', xml_declaration=True)

            replacements = {}
            additions = {}
            with ZipFile(BytesIO(output.getvalue()), 'r') as source:
                content_types = ET.fromstring(source.read('[Content_Types].xml'))
                if not any(
                        node.tag == f'{{{content_ns}}}Default'
                        and node.get('Extension') == 'png'
                        for node in content_types):
                    ET.SubElement(content_types, f'{{{content_ns}}}Default', {
                        'Extension': 'png', 'ContentType': 'image/png',
                    })
                drawing_name = 'xl/drawings/drawing1.xml'
                if not any(
                        node.tag == f'{{{content_ns}}}Override'
                        and node.get('PartName') == '/xl/drawings/drawing1.xml'
                        for node in content_types):
                    ET.SubElement(content_types, f'{{{content_ns}}}Override', {
                        'PartName': '/xl/drawings/drawing1.xml',
                        'ContentType': 'application/vnd.openxmlformats-officedocument.drawing+xml',
                    })
                sheet_name = 'xl/worksheets/sheet1.xml'
                sheet_root = ET.fromstring(source.read(sheet_name))
                relation_name = 'xl/worksheets/_rels/sheet1.xml.rels'
                if relation_name in source.namelist():
                    sheet_relations = ET.fromstring(source.read(relation_name))
                else:
                    sheet_relations = ET.Element(f'{{{package_rel_ns}}}Relationships')
                existing_ids = {node.get('Id') for node in sheet_relations}
                relation_number = 1
                while f'rId{relation_number}' in existing_ids:
                    relation_number += 1
                drawing_relation_id = f'rId{relation_number}'
                ET.SubElement(sheet_relations, f'{{{package_rel_ns}}}Relationship', {
                    'Id': drawing_relation_id,
                    'Type': f'{office_rel_ns}/drawing',
                    'Target': '../drawings/drawing1.xml',
                })
                drawing_element = ET.Element(
                    f'{{{spreadsheet_ns}}}drawing',
                    {f'{{{office_rel_ns}}}id': drawing_relation_id},
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
                replacements[sheet_name] = serialize_xml(
                    sheet_root, spreadsheet_ns, (('r', office_rel_ns),))
                serialized_sheet_relations = serialize_xml(
                    sheet_relations, package_rel_ns)
                if relation_name in source.namelist():
                    replacements[relation_name] = serialized_sheet_relations
                else:
                    additions[relation_name] = serialized_sheet_relations

                drawing_root = ET.Element(f'{{{drawing_ns}}}wsDr')
                anchor = ET.SubElement(drawing_root, f'{{{drawing_ns}}}oneCellAnchor')
                origin = ET.SubElement(anchor, f'{{{drawing_ns}}}from')
                for name, value in (
                        ('col', '0'), ('colOff', '0'), ('row', '0'), ('rowOff', '0')):
                    ET.SubElement(origin, f'{{{drawing_ns}}}{name}').text = value
                ET.SubElement(anchor, f'{{{drawing_ns}}}ext', {
                    'cx': '914400', 'cy': '914400',
                })
                picture = ET.SubElement(anchor, f'{{{drawing_ns}}}pic')
                non_visual = ET.SubElement(picture, f'{{{drawing_ns}}}nvPicPr')
                ET.SubElement(non_visual, f'{{{drawing_ns}}}cNvPr', {
                    'id': '1', 'name': 'TO‘RTKO‘LDONMAHSULOTI logotipi',
                })
                ET.SubElement(
                    ET.SubElement(non_visual, f'{{{drawing_ns}}}cNvPicPr'),
                    f'{{{drawingml_ns}}}picLocks', {'noChangeAspect': '1'})
                fill = ET.SubElement(picture, f'{{{drawing_ns}}}blipFill')
                ET.SubElement(fill, f'{{{drawingml_ns}}}blip', {
                    f'{{{office_rel_ns}}}embed': 'rId1',
                })
                stretch = ET.SubElement(fill, f'{{{drawingml_ns}}}stretch')
                ET.SubElement(stretch, f'{{{drawingml_ns}}}fillRect')
                shape = ET.SubElement(picture, f'{{{drawing_ns}}}spPr')
                geometry = ET.SubElement(shape, f'{{{drawingml_ns}}}prstGeom', {
                    'prst': 'rect',
                })
                ET.SubElement(geometry, f'{{{drawingml_ns}}}avLst')
                ET.SubElement(anchor, f'{{{drawing_ns}}}clientData')
                additions[drawing_name] = serialize_xml(
                    drawing_root, drawing_ns, (('a', drawingml_ns), ('r', office_rel_ns)))
                drawing_relations = ET.Element(f'{{{package_rel_ns}}}Relationships')
                ET.SubElement(drawing_relations, f'{{{package_rel_ns}}}Relationship', {
                    'Id': 'rId1', 'Type': f'{office_rel_ns}/image',
                    'Target': '../media/unified_report_logo.png',
                })
                additions['xl/drawings/_rels/drawing1.xml.rels'] = serialize_xml(
                    drawing_relations, package_rel_ns)
                additions['xl/media/unified_report_logo.png'] = logo_path.read_bytes()
                replacements['[Content_Types].xml'] = serialize_xml(
                    content_types, content_ns)
                embedded = BytesIO()
                with ZipFile(embedded, 'w', ZIP_DEFLATED) as destination:
                    for item in source.infolist():
                        destination.writestr(
                            item, replacements.get(item.filename, source.read(item.filename)))
                    for name, data in additions.items():
                        destination.writestr(name, data)
            output = BytesIO(embedded.getvalue())
            output.seek(0)
            return send_file(
                output, as_attachment=True,
                download_name='tortkuldon_yagona_hisobot.xlsx',
                mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                max_age=0,
            )
        finally:
            con.close()
    except ValueError as exc:
        abort(400, f'Yagona hisobotni yaratib bo‘lmadi: {exc}')

