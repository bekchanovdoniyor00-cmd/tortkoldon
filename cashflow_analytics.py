"""Read-only cash-flow reconciliation over the existing ERP ledgers."""

from datetime import date, datetime
from decimal import Decimal

from date_conversions import dmy_to_date, iso_to_date
from finance_calculations import calculate_financial_snapshot


ZERO = Decimal('0')
CHANNELS = ('naqd', 'plastik', 'hisob', 'noma_lum')
ACCOUNT_ROLES = ('umid', 'maxmud', 'buxgalter')
ACCOUNT_LABELS = {
    'umid': 'Umid',
    'maxmud': 'Maxmud',
    'buxgalter': 'Buxgalter',
}
CHANNEL_LABELS = {
    'naqd': 'Naqd',
    'plastik': 'Plastik',
    'hisob': 'Bank',
    'noma_lum': 'Aniqlanmagan kanal',
}
FUND_SOURCE_ROLES = {
    'umid': 'umid',
    'Umid kassasi': 'umid',
    'maxmud': 'maxmud',
    'Maxmud kassasi': 'maxmud',
    'buxgalter': 'buxgalter',
    'Buxgalter kassasi': 'buxgalter',
}


def _business_day(value):
    if value in (None, ''):
        return None
    text = str(value).strip()
    if len(text) >= 10 and text[4:5] == '-':
        text = text[:10]
    try:
        if len(text) == 10 and text[4] == '-':
            return iso_to_date(text)
        if len(text) == 10 and text[2] == '-':
            return dmy_to_date(text)
    except ValueError:
        return None
    return None


def _amount(value):
    result = Decimal(str(value or 0))
    if not result.is_finite() or result < ZERO:
        raise ValueError('Pul yoki savdo yozuvida yaroqsiz summa mavjud.')
    return result


def _columns(con, table):
    exists = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if not exists:
        return set()
    return {row['name'] for row in con.execute(f'PRAGMA table_info("{table}")')}


def _account(role, user=None):
    return {
        'role': role,
        'name': (user or {}).get('username') or ACCOUNT_LABELS[role],
        'sales_kg': ZERO,
        'sales_amount': ZERO,
        'initial_paid': ZERO,
        'sales_initial_linked': ZERO,
        'sales_later_linked': ZERO,
        'debt_receipts': ZERO,
        'expenses': ZERO,
        'cash_additions': ZERO,
        'transfers_in': ZERO,
        'transfers_out': ZERO,
        'period_receipts': ZERO,
        'period_expenses': ZERO,
        'period_cash_additions': ZERO,
        'period_cash_additions_by_channel': {channel: ZERO for channel in CHANNELS},
        'period_transfers_in': ZERO,
        'period_transfers_out': ZERO,
        'closing_channels': {channel: ZERO for channel in CHANNELS},
        'period_receipts_by_channel': {channel: ZERO for channel in CHANNELS},
        'period_expenses_by_channel': {channel: ZERO for channel in CHANNELS},
        'transfers_in_by_channel': {channel: ZERO for channel in CHANNELS},
        'transfers_out_by_channel': {channel: ZERO for channel in CHANNELS},
        'debt': ZERO,
        'legacy_fifo_estimated': ZERO,
        'debt_before_legacy_estimate': ZERO,
    }


def calculate_cashflow_analytics(con, start_date=None, end_date=None):
    """Calculate period activity and cumulative balances without writing to SQLite.

    Dates are ISO strings. Closing balances are cumulative through end_date,
    using a zero calculation baseline that is explicitly not a physical opening
    balance. Missing account/channel attribution is held in a separate suspense.
    """
    start = iso_to_date(start_date) if start_date else None
    end = iso_to_date(end_date) if end_date else date.today()
    if start and start > end:
        raise ValueError('Boshlanish sanasi tugash sanasidan keyin bo‘lishi mumkin emas.')

    warnings = []
    events = []
    movement_rows = []
    users = {}
    for row in con.execute('SELECT id,username,role FROM users'):
        users[row['id']] = {'username': row['username'], 'role': row['role']}
    accounts = {
        role: _account(role, next((u for u in users.values() if u['role'] == role), None))
        for role in ACCOUNT_ROLES
    }

    def warn(code, message, **details):
        warnings.append({'code': code, 'message': message, **details})

    def event_day(value, source, source_id):
        day = _business_day(value)
        if day is None:
            warn('undated_operation', 'Sana aniqlanmagan operatsiya sana hisobiga kiritilmadi.',
                 source=source, source_id=source_id)
        return day

    def in_period(day):
        return day is not None and day <= end and (start is None or day >= start)

    def channel(value):
        return value if value in ('naqd', 'plastik', 'hisob') else 'noma_lum'

    def new_event(day, event_type, amount, source, source_id, description,
                  actor_role=None, actor_name=None, cash_delta=None, cash_role=None,
                  method=None, purpose='', actual_person='', entered_by='',
                  counterparty='', details=None):
        value = _amount(amount)
        row = {
            'date': day.isoformat() if day else 'Sana aniqlanmagan',
            '_day': day,
            'type': event_type,
            'amount': value,
            'source': source,
            'source_id': source_id,
            'description': description,
            'actor_role': actor_role or '',
            'actor': actor_name or ACCOUNT_LABELS.get(actor_role, 'Aniqlanmagan'),
            'cash_role': cash_role or '',
            'channel': CHANNEL_LABELS[channel(method)] if cash_delta is not None else '—',
            'channel_key': channel(method) if cash_delta is not None else '',
            'cash_delta': Decimal(str(cash_delta)) if cash_delta is not None else ZERO,
            'purpose': purpose or 'Aniqlanmagan',
            'actual_person': actual_person or 'Aniqlanmagan',
            'entered_by': entered_by or 'Aniqlanmagan',
            'counterparty': counterparty,
            'details': details or '',
        }
        events.append(row)
        if cash_delta is not None:
            movement_rows.append(row)
        return row

    sales_columns = _columns(con, 'sales')
    batch_columns = _columns(con, 'worker_sale_batches')
    batch_payment_columns = _columns(con, 'worker_sale_batch_payments')
    batch_item_columns = _columns(con, 'worker_sale_batch_items')
    if not sales_columns:
        raise ValueError('sales jadvali mavjud emas.')

    batches = {}
    if batch_columns:
        batches = {
            row['id']: dict(row)
            for row in con.execute('SELECT * FROM worker_sale_batches')
        }
    sale_batches = {}
    if batch_item_columns:
        sale_batches = {
            row['sale_id']: row['batch_id']
            for row in con.execute('SELECT sale_id,batch_id FROM worker_sale_batch_items')
        }
    batch_payment_sums = {}
    if batch_payment_columns:
        for row in con.execute('SELECT * FROM worker_sale_batch_payments ORDER BY id'):
            batch_id = row['batch_id']
            amount = _amount(row['amount'])
            batch_payment_sums[batch_id] = batch_payment_sums.get(batch_id, ZERO) + amount
            batch = batches.get(batch_id, {})
            worker = users.get(batch.get('worker_id'), {})
            role = worker.get('role')
            method = channel(row['payment_method'])
            day = event_day(row['created_at'], 'worker_sale_batch_payments', row['id'])
            new_event(day, 'Ko‘p mahsulotli sotuv boshlang‘ich tushumi',
                      amount, 'worker_sale_batch_payments', row['id'],
                      f"To‘plam #{batch_id}", role, worker.get('username'),
                      amount, role, method,
                      counterparty=batch.get('customer', ''),
                      details='Kanal summasi paket to‘lovidan olindi; sales.paid qayta sanalmadi.')
            if role not in accounts:
                warn('batch_payment_owner_unknown',
                     'Paket boshlang‘ich to‘lovining xodimi tanilmadi; tushum kassaga biriktirilmadi.',
                     batch_id=batch_id, payment_id=row['id'])

    receipt_columns = _columns(con, 'worker_receipts')
    accountant_receipt_columns = _columns(con, 'accountant_worker_receipts')
    direct_receipts = {}
    if accountant_receipt_columns:
        direct_receipts = {
            row['receipt_id']: dict(row)
            for row in con.execute('SELECT * FROM accountant_worker_receipts')
        }
    receipts_by_sale = {}
    receipt_row_count = 0
    if receipt_columns:
        for row in con.execute('SELECT * FROM worker_receipts ORDER BY id'):
            receipt_row_count += 1
            amount = _amount(row['amount'])
            receipts_by_sale[row['sale_id']] = receipts_by_sale.get(row['sale_id'], ZERO) + amount
            direct = direct_receipts.get(row['id'])
            worker = users.get(row['worker_id'], {})
            holder_role = 'buxgalter' if direct else worker.get('role')
            holder_name = accounts[holder_role]['name'] if holder_role in accounts else ''
            day = event_day(row['date'], 'worker_receipts', row['id'])
            method_value = row['payment_method'] if 'payment_method' in row.keys() else None
            new_event(day, 'Qarz to‘lovi', amount, 'worker_receipts', row['id'],
                      f"Sotuv #{row['sale_id']}", row['worker_id'] and worker.get('role'),
                      worker.get('username'), amount, holder_role, method_value,
                      counterparty=(direct or {}).get('customer', ''),
                      details=('Buxgalter kassasiga kirim; buxgalter_worker_receipts faqat tasnif.'
                               if direct else 'Xodim kassasiga kirim.'))
            if direct:
                if (_amount(direct.get('amount')) != amount
                        or direct.get('payment_method') != method_value
                        or direct.get('worker_id') != row['worker_id']):
                    warn('accountant_receipt_mismatch',
                         'Buxgalter tasnifi manba to‘loviga mos emas; kassa harakati worker_receipts manbasidan olindi.',
                         receipt_id=row['id'])
                if direct.get('payment_date') != row['date']:
                    warn('accountant_receipt_date_mismatch',
                         'Buxgalter tasnifidagi sana manba to‘lov sanasidan farq qiladi.',
                         receipt_id=row['id'])
            elif holder_role not in accounts:
                warn('receipt_owner_unknown',
                     'Qarz to‘lovining xodim kassasi aniqlanmadi; tushum hisobga olinmagan.',
                     receipt_id=row['id'])
            if holder_role in accounts:
                accounts[holder_role]['debt_receipts'] += amount
    if _columns(con,'opening_debt_receipts'):
        for row in con.execute('''SELECT r.*,d.customer FROM opening_debt_receipts r
                JOIN opening_debts d ON d.id=r.opening_debt_id ORDER BY r.id'''):
            amount = _amount(row['amount'])
            worker = users.get(row['worker_id'],{})
            receiver = users.get(row['receiver_id'],{})
            holder_role = 'buxgalter' if row['received_by']=='accountant' else receiver.get('role')
            day = event_day(row['date'],'opening_debt_receipts',row['id'])
            new_event(day,'Eski qarz to‘lovi',amount,'opening_debt_receipts',row['id'],
                f"Eski qarz #{row['opening_debt_id']}",worker.get('role'),
                worker.get('username'),amount,holder_role,row['payment_method'],
                counterparty=row['customer'],
                details='Eski qarz to‘lovi, sotuv emas; pul faqat qabul qiluvchi kassasida.')
            if holder_role in accounts:
                accounts[holder_role]['debt_receipts'] += amount
            else:
                warn('opening_receipt_owner_unknown',
                     'Eski qarz to‘lovi kassaga biriktirilmadi.',receipt_id=row['id'])
    if accountant_receipt_columns:
        receipt_ids = {row['id'] for row in con.execute('SELECT id FROM worker_receipts')}
        for receipt_id in direct_receipts.keys() - receipt_ids:
            warn('accountant_receipt_without_source',
                 'Buxgalter tasnifida manba worker_receipts yozuvi topilmadi; tushum qayta yaratilmagan.',
                 receipt_id=receipt_id)

    cancelled = {}
    if _columns(con, 'sale_cancellations'):
        cancelled = {
            row['sale_id']: dict(row)
            for row in con.execute('SELECT * FROM sale_cancellations')
        }
    sales = {}
    sales_by_id = {}
    sales_total = ZERO
    sales_kg = ZERO
    initial_total = ZERO
    active_sale_ids = set()
    sales_rows = con.execute('SELECT * FROM sales ORDER BY id').fetchall()
    for row in sales_rows:
        sale = dict(row)
        sale_id = sale['id']
        sales_by_id[sale_id] = sale
        batch_id = sale_batches.get(sale_id)
        batch = batches.get(batch_id, {}) if batch_id is not None else {}
        seller_id = batch.get('worker_id') if batch else sale.get('user_id')
        seller = users.get(seller_id, {})
        role = seller.get('role')
        recipient_id = sale.get('initial_payment_recipient_id')
        recipient = users.get(recipient_id, {}) if recipient_id is not None else seller
        recipient_role = recipient.get('role')
        if batch and sale.get('user_id') != seller_id:
            warn('batch_worker_mismatch',
                 'Paket xodimi va sotuv yozuvidagi user_id farq qiladi; paket xodimi sotuv egasi sifatida ishlatildi.',
                 sale_id=sale_id, batch_id=batch_id)
        try:
            revenue = _amount(sale['qty_kg']) * _amount(sale['price_per_kg'])
            qty = _amount(sale['qty_kg'])
            paid = _amount(sale.get('paid'))
        except (KeyError, TypeError):
            raise ValueError(f'Sotuv #{sale_id} summasini tekshirib bo‘lmadi.')
        is_cancelled = sale_id in cancelled
        day = event_day(sale.get('date'), 'sales', sale_id)
        receipts = receipts_by_sale.get(sale_id, ZERO)
        initial_total += paid
        if paid and batch_id is None:
            method_value = sale.get('payment_method')
            new_event(day, 'Sotuvdagi boshlang‘ich to‘lov', paid, 'sales',
                      sale_id, sale.get('product', ''), role, seller.get('username'),
                      paid, recipient_role, method_value,
                      details=('Bekor qilingan sotuvdagi qayd etilgan tushum; '
                               'qaytarim isbotlanmagan, avtomatik ayrilmadi.'
                               if is_cancelled else
                               'Sotuv boshlang‘ich to‘lovi; daromad summasiga qayta qo‘shilmaydi.'))
            if recipient_role not in accounts:
                warn('initial_payment_owner_unknown',
                     'Boshlang‘ich to‘lov qaysi kassaga tegishli ekani aniqlanmadi.',
                     sale_id=sale_id, recipient_id=recipient_id)
        elif paid and batch_id is not None and batch_id not in batch_payment_sums:
            new_event(day, 'Paket boshlang‘ich to‘lovi (kanal noma’lum)',
                      paid, 'sales', sale_id, sale.get('product', ''), role,
                      seller.get('username'), paid, role, None,
                      details='Paket kanali yozuvi topilmadi; kanal noma’lum, sales.paid bir marta sanaldi.')
            warn('batch_payment_channel_missing',
                 'Paketda sales.paid bor, ammo kanal bo‘yicha paket to‘lovlari topilmadi.',
                 sale_id=sale_id, batch_id=batch_id)
        if is_cancelled:
            cancel = cancelled[sale_id]
            unresolved = paid + receipts
            warn('cancelled_sale_refund_unconfirmed',
                 ('Sotuv bekor qilingan; pul qaytarilganini tasdiqlovchi yozuv yo‘q. '
                  'Pul qaytarildi deb hisoblanmadi.')
                 if unresolved else
                 'Sotuv bekor qilingan; qaytarim tasdiqlovchi yozuv yo‘q, qaytarim taxmin qilinmadi.',
                 sale_id=sale_id, received_amount=str(unresolved),
                 cancelled_at=cancel.get('cancelled_at', ''))
            new_event(day, 'Bekor qilingan sotuv', revenue, 'sales', sale_id,
                      sale.get('product', ''), role, seller.get('username'),
                      details='Faol sotuv summasiga kiritilmagan; pul qaytarilishi taxmin qilinmadi.')
            continue
        active_sale_ids.add(sale_id)
        sales_total += revenue
        sales_kg += qty
        if role in accounts:
            accounts[role]['sales_amount'] += revenue
            accounts[role]['sales_kg'] += qty
            accounts[role]['initial_paid'] += paid
        else:
            warn('sale_owner_unknown',
                 'Sotuv egasi Umid, Maxmud yoki Buxgalter sifatida aniqlanmadi.',
                 sale_id=sale_id, user_id=seller_id)
        new_event(day, 'Sotuv (daromad, naqd tushum emas)', revenue,
                  'sales', sale_id, sale.get('product', ''), role,
                  seller.get('username'),
                  details=f"{qty} kg; boshlang‘ich to‘lov alohida pul harakati.")
    for batch_id, batch in batches.items():
        expected = _amount(batch.get('paid'))
        recorded = batch_payment_sums.get(batch_id, ZERO)
        if expected != recorded:
            warn('batch_payment_total_mismatch',
                 'Paket paid qiymati kanal to‘lovlari yig‘indisiga teng emas; pul faqat kanal yozuvlaridan hisoblandi.',
                 batch_id=batch_id, batch_paid=str(expected), channel_payments=str(recorded))

    snapshot = calculate_financial_snapshot(con)
    for group in snapshot['customers'].values():
        for obligation in group['obligations']:
            if obligation['kind']!='opening':
                continue
            wid = obligation.get('user_id')
            role = users.get(wid,{}).get('role')
            balance = _amount(obligation['balance'])
            if role in accounts:
                accounts[role]['debt'] += balance
                accounts[role]['debt_before_legacy_estimate'] += (balance+_amount(obligation['legacy_applied']))
            elif balance:
                warn('opening_debt_owner_unknown',
                     'Mas’uli belgilanmagan eski qarz.',opening_debt_id=obligation['id'])
    obligations_by_sale = {}
    for group in snapshot['customers'].values():
        for obligation in group['obligations']:
            if obligation['kind'] == 'sale':
                obligations_by_sale[obligation['id']] = obligation
    debt_totals = ZERO
    for sale_id in active_sale_ids:
        obligation = obligations_by_sale.get(sale_id)
        if not obligation:
            warn('active_sale_missing_obligation',
                 'Faol sotuv moliyaviy snapshot majburiyatlarida topilmadi.',
                 sale_id=sale_id)
            continue
        sale = sales_by_id[sale_id]
        batch_id = sale_batches.get(sale_id)
        owner_id = batches.get(batch_id, {}).get('worker_id') if batch_id in batches else sale.get('user_id')
        role = users.get(owner_id, {}).get('role')
        balance = _amount(obligation['balance'])
        estimated = _amount(obligation['legacy_applied'])
        paid_initial = _amount(obligation['initial_paid'])
        paid_linked = _amount(obligation['linked_paid'])
        total = _amount(obligation['amount'])
        before_legacy = balance + estimated
        debt_totals += balance
        if role in accounts:
            accounts[role]['debt'] += balance
            accounts[role]['legacy_fifo_estimated'] += estimated
            accounts[role]['debt_before_legacy_estimate'] += before_legacy
        else:
            warn('debt_owner_unknown',
                 'Sotuv qarzi xodimga ishonchli biriktirilmadi.',
                 sale_id=sale_id)
        if role in accounts:
            account = accounts[role]
            account.setdefault('sales_initial_linked', ZERO)
            account['sales_initial_linked'] += paid_initial
            account.setdefault('sales_later_linked', ZERO)
            account['sales_later_linked'] += paid_linked
        if total != paid_initial + paid_linked + estimated + balance:
            excess = max(ZERO, paid_initial + paid_linked - total)
            if total != paid_initial + paid_linked + estimated + balance - excess:
                warn('sale_debt_reconciliation_mismatch',
                     'Sotuv summasi, bog‘langan to‘lovlar va snapshot qarzi tenglashmadi.',
                     sale_id=sale_id, sale_total=str(total), initial=str(paid_initial),
                     linked=str(paid_linked), legacy_estimated=str(estimated),
                     debt=str(balance), excess=str(excess))

    for row in sales_rows:
        if row['id'] in active_sale_ids:
            continue
        if row['id'] in cancelled and (row['paid'] or receipts_by_sale.get(row['id'], ZERO)):
            continue

    # Legacy payments have no reliable cash holder or channel. Keep them in suspense.
    if _columns(con, 'payments'):
        for row in con.execute('SELECT * FROM payments ORDER BY id'):
            amount = _amount(row['amount'])
            day = event_day(row['date'], 'payments', row['id'])
            new_event(day, 'Aniqlanmagan tarixiy tushum', amount, 'payments',
                      row['id'], 'Qabul qiluvchi va kanal tasdiqlanmagan.',
                      cash_delta=amount, cash_role='suspense', method=None,
                      counterparty=row['customer'],
                      details='Xodim yoki Buxgalter kassasiga taxmin bilan biriktirilmadi.')
            warn('legacy_receipt_unassigned',
                 'Eski payments yozuvi qabul qiluvchi/kanal tasdiqlanmagani uchun noma’lum tarixiy tushum sifatida ajratildi.',
                 payment_id=row['id'])

    expenses_columns = _columns(con, 'expenses')
    if expenses_columns:
        expense_rows = con.execute('SELECT * FROM expenses ORDER BY id').fetchall()
        for row in expense_rows:
            expense = dict(row)
            amount = _amount(expense['amount'])
            source_role_value = expense.get('fund_source') if 'fund_source' in expenses_columns else None
            source_role = FUND_SOURCE_ROLES.get(source_role_value)
            user_role = users.get(expense.get('user_id'), {}).get('role')
            if source_role is None and not source_role_value:
                source_role = user_role if user_role in accounts else None
            elif source_role and user_role in accounts and source_role != user_role:
                warn('expense_source_mismatch',
                     'Xarajatning fund_source va user_id egasi mos emas; kassa manbai noaniq deb qoldirildi.',
                     expense_id=expense['id'], fund_source=source_role_value,
                     user_role=user_role)
                source_role = None
            elif source_role_value and source_role is None:
                warn('expense_source_unknown',
                     'Xarajat pul manbai tanilmadi; summa kassaga taxminan ajratilmadi.',
                     expense_id=expense['id'], fund_source=source_role_value)
            method_value = expense.get('payment_method')
            day = event_day(expense.get('date'), 'expenses', expense['id'])
            entered_id = expense.get('entered_by')
            entered_name = users.get(entered_id, {}).get('username', '')
            actual_person = (expense.get('actual_person') or '').strip()
            purpose = (expense.get('purpose') or '').strip()
            row_source = expense.get('fund_source') or (
                'user_id asosida' if source_role else 'Aniqlanmagan')
            event = new_event(day, 'Xarajat', amount, 'expenses',
                              expense['id'], expense.get('category', ''),
                              source_role, accounts[source_role]['name'] if source_role else '',
                              -amount, source_role, method_value,
                              purpose=purpose, actual_person=actual_person,
                              entered_by=entered_name,
                              details=f"Pul manbai: {row_source}; izoh: {expense.get('note') or '—'}")
            event['category'] = expense.get('category') or 'Aniqlanmagan'
            event['fund_source'] = row_source
            if source_role in accounts:
                accounts[source_role]['expenses'] += amount
            else:
                warn('expense_cash_owner_unknown',
                     'Xarajat egasi aniqlanmagani uchun kassa qoldig‘i suspense orqali ko‘rsatildi.',
                     expense_id=expense['id'])

    cash_addition_columns = _columns(con, 'accountant_cash_additions')
    cash_addition_count = 0
    if cash_addition_columns:
        cash_additions = con.execute(
            'SELECT * FROM accountant_cash_additions ORDER BY id').fetchall()
        cash_addition_count = len(cash_additions)
        for row in cash_additions:
            addition = dict(row)
            amount = _amount(addition['amount'])
            method = addition.get('payment_method')
            day = event_day(addition.get('date'),
                            'accountant_cash_additions', addition['id'])
            accountant = users.get(addition.get('entered_by'), {})
            event = new_event(
                day, 'Kassaga pul qo‘shish', amount,
                'accountant_cash_additions', addition['id'],
                addition.get('source') or 'Manba ko‘rsatilmagan',
                'buxgalter', accountant.get('username'),
                amount, 'buxgalter', method,
                purpose='Buxgalter kassasini to‘ldirish',
                actual_person=addition.get('source', ''),
                entered_by=accountant.get('username', ''),
                details=addition.get('note') or '—')
            event['category'] = 'Sotuv tushumi emas'
            event['fund_source'] = addition.get('source') or 'Aniqlanmagan'
            accounts['buxgalter']['cash_additions'] += amount

    wheat_payment_columns = _columns(con, 'wheat_purchase_payments')
    wheat_payment_count = 0
    if wheat_payment_columns:
        wheat_payments = con.execute(
            'SELECT * FROM wheat_purchase_payments ORDER BY id').fetchall()
        wheat_payment_count = len(wheat_payments)
        for row in wheat_payments:
            payment = dict(row)
            amount = _amount(payment['amount'])
            method = payment.get('payment_method')
            if method not in ('naqd', 'hisob'):
                warn('wheat_purchase_payment_channel_unknown',
                     'Bug‘doy xaridi to‘lov kanali yaroqsiz; chiqim noma’lum kanalga ajratildi.',
                     payment_id=payment['id'], payment_method=method)
            day = event_day(payment.get('payment_date'),
                            'wheat_purchase_payments', payment['id'])
            accountant = users.get(payment['accountant_id'], {})
            event = new_event(
                day, 'Bug‘doy xaridi uchun to‘lov', amount,
                'wheat_purchase_payments', payment['id'],
                f"Bug‘doy kirimi #{payment['wheat_receipt_id']}",
                'buxgalter', accountant.get('username'),
                -amount, 'buxgalter', method,
                purpose='Bug‘doy xaridi',
                entered_by=accountant.get('username', ''),
                details='Bug‘doy kirimi bilan bog‘langan to‘liq to‘lov.')
            event['category'] = 'Bug‘doy xaridi'
            event['fund_source'] = 'Buxgalter kassasi'
            accounts['buxgalter']['expenses'] += amount

    transfers_columns = _columns(con, 'worker_transfers')
    transfer_totals = {'approved': ZERO, 'pending': ZERO, 'rejected': ZERO}
    transfer_out_total = ZERO
    transfer_in_total = ZERO
    transfer_row_count = 0
    if transfers_columns:
        for row in con.execute('SELECT * FROM worker_transfers ORDER BY id'):
            transfer_row_count += 1
            transfer = dict(row)
            status = transfer.get('status') or 'pending'
            amount = _amount(transfer['amount'])
            worker = users.get(transfer.get('worker_id'), {})
            worker_role = worker.get('role')
            decision_day = event_day(transfer.get('decided_at'), 'worker_transfers', transfer['id']) if status == 'approved' else _business_day(transfer.get('created_at'))
            if status in transfer_totals:
                transfer_totals[status] += amount
            if status == 'approved':
                if decision_day is None:
                    warn('approved_transfer_undated',
                         'Tasdiqlangan transfer sanasi noma’lum; kassa qoldig‘iga kiritilmadi.',
                         transfer_id=transfer['id'])
                    continue
                method_value = transfer.get('payment_method')
                new_event(decision_day, 'Tasdiqlangan transfer — xodimdan chiqim',
                          amount, 'worker_transfers', transfer['id'],
                          'Ichki harakat, yangi tushum emas.',
                          worker_role, worker.get('username'), -amount,
                          worker_role, method_value, counterparty='Buxgalter')
                new_event(decision_day, 'Tasdiqlangan transfer — Buxgalterga kirim',
                          amount, 'worker_transfers', transfer['id'],
                          'Ichki harakat, yangi tushum emas.',
                          'buxgalter', accounts['buxgalter']['name'], amount,
                          'buxgalter', method_value,
                          counterparty=worker.get('username', 'Aniqlanmagan'))
                transfer_out_total += amount
                transfer_in_total += amount
                if worker_role in accounts:
                    accounts[worker_role]['transfers_out'] += amount
                else:
                    warn('transfer_worker_unknown',
                         'Tasdiqlangan transfer xodimi tanilmadi; xodim kassasi bo‘yicha ajratilmadi.',
                         transfer_id=transfer['id'])
            elif status in ('pending', 'rejected'):
                new_event(decision_day, f"Transfer — {status}",
                          amount, 'worker_transfers', transfer['id'],
                          'Tasdiqlanmagan transfer kassa qoldig‘ini o‘zgartirmaydi.',
                          worker_role, worker.get('username'),
                          details=f"To‘lov kanali: {CHANNEL_LABELS[channel(transfer.get('payment_method'))]}")

    account_cash_transfer_count = 0
    if _columns(con, 'account_cash_transfers'):
        for row in con.execute('SELECT * FROM account_cash_transfers ORDER BY id'):
            account_cash_transfer_count += 1
            transfer = dict(row)
            amount = _amount(transfer['amount'])
            from_channel = transfer.get('from_channel')
            to_channel = transfer.get('to_channel')
            if (from_channel not in ('naqd', 'plastik', 'hisob')
                    or to_channel not in ('naqd', 'plastik', 'hisob')
                    or from_channel == to_channel):
                warn('account_cash_transfer_invalid_channels',
                     'Ichki kassa ko‘chirishining hisoblari yaroqsiz; harakat hisobga olinmadi.',
                     transfer_id=transfer['id'])
                continue
            day = event_day(transfer.get('date'), 'account_cash_transfers', transfer['id'])
            actor = users.get(transfer.get('entered_by'), {})
            note = transfer.get('note') or 'Izohsiz'
            new_event(day, 'Hisoblar orasida pul ko‘chirish — chiqim',
                      amount, 'account_cash_transfers', transfer['id'], note,
                      'buxgalter', actor.get('username'), -amount,
                      'buxgalter', from_channel,
                      counterparty=CHANNEL_LABELS[to_channel],
                      details='Ichki kassa harakati; daromad yoki xarajat emas.')
            new_event(day, 'Hisoblar orasida pul ko‘chirish — kirim',
                      amount, 'account_cash_transfers', transfer['id'], note,
                      'buxgalter', actor.get('username'), amount,
                      'buxgalter', to_channel,
                      counterparty=CHANNEL_LABELS[from_channel],
                      details='Ichki kassa harakati; daromad yoki xarajat emas.')

    # Apply all dated cash movements once; internal transfer entries cancel at company level.
    suspense_channels = {channel: ZERO for channel in CHANNELS}
    period_suspense_in = ZERO
    period_suspense_out = ZERO
    for movement in movement_rows:
        day = movement['_day']
        if day is None or day > end:
            continue
        amount = movement['cash_delta']
        method = movement['channel_key']
        role = movement['cash_role']
        holder = accounts.get(role) if role else None
        is_transfer = (
            movement['type'].startswith('Tasdiqlangan transfer')
            or movement['type'].startswith('Hisoblar orasida pul ko‘chirish'))
        if holder is None:
            suspense_channels[method] += amount
            if in_period(day) and not is_transfer:
                if amount >= ZERO:
                    period_suspense_in += amount
                else:
                    period_suspense_out += -amount
            continue
        holder['closing_channels'][method] += amount
        if in_period(day):
            if amount > ZERO and movement['type'] == 'Kassaga pul qo‘shish':
                holder['period_cash_additions'] += amount
                holder['period_cash_additions_by_channel'][method] += amount
            elif amount > ZERO and not is_transfer:
                holder['period_receipts'] += amount
                holder['period_receipts_by_channel'][method] += amount
            elif amount < ZERO and movement['type'] in (
                    'Xarajat', 'Bug‘doy xaridi uchun to‘lov'):
                holder['period_expenses'] += -amount
                holder['period_expenses_by_channel'][method] += -amount
    for movement in events:
        if movement['type'].startswith('Tasdiqlangan transfer'):
            role = movement['cash_role']
            if role in accounts and in_period(movement['_day']):
                channel_key = movement['channel_key']
                if movement['cash_delta'] > ZERO:
                    accounts[role]['period_transfers_in'] += movement['amount']
                    accounts[role]['transfers_in_by_channel'][channel_key] += movement['amount']
                else:
                    accounts[role]['period_transfers_out'] += movement['amount']
                    accounts[role]['transfers_out_by_channel'][channel_key] += movement['amount']
    for role, account in accounts.items():
        account['closing_balance'] = sum(account['closing_channels'].values(), ZERO)
        account['period_receipts_excluding_transfers'] = account['period_receipts']
        account['period_net_change'] = (
            account['period_cash_additions']
            + sum((account['period_receipts_by_channel'][channel]
                 - account['period_expenses_by_channel'][channel]
                 + account['transfers_in_by_channel'][channel]
                 - account['transfers_out_by_channel'][channel]
                 for channel in CHANNELS), ZERO))
        for metric in ('sales_kg',):
            account[metric] = Decimal(str(account[metric]))

    company_channels = {
        channel: sum((account['closing_channels'][channel] for account in accounts.values()), ZERO)
        + suspense_channels[channel]
        for channel in CHANNELS
    }
    allocated_balance = sum((account['closing_balance'] for account in accounts.values()), ZERO)
    suspense_balance = sum(suspense_channels.values(), ZERO)
    company_balance = allocated_balance + suspense_balance
    if start is None:
        start_label = 'Barcha mavjud sana'
    else:
        start_label = start.isoformat()
    period_start = start.isoformat() if start else ''
    snapshot_debt = _amount(snapshot['totals']['debt'])
    assigned_debt = sum((account['debt'] for account in accounts.values()), ZERO)
    unassigned_debt = snapshot_debt - assigned_debt
    if unassigned_debt:
        warn('debt_unassigned',
             'Qarzning bir qismi xodimga ishonchli tegishli emas (jumladan eski qarzlar).',
             amount=str(unassigned_debt))
    snapshot_warnings = snapshot['warnings']
    for item in snapshot_warnings:
        warn('financial_snapshot_' + item['code'],
             'Moliyaviy snapshot noaniqligi: ' + item['code'],
             details=str(item))
    sales_reconciliation = {
        'sales_amount': sales_total,
        'initial_paid': sum((account['sales_initial_linked'] for account in accounts.values()), ZERO),
        'linked_receipts': sum((account['sales_later_linked'] for account in accounts.values()), ZERO),
        'legacy_fifo_estimated': sum(
            (account['legacy_fifo_estimated'] for account in accounts.values()), ZERO),
        'remaining_debt': debt_totals,
        'unassigned_debt': unassigned_debt,
    }
    sales_reconciliation['difference'] = (
        sales_total - sales_reconciliation['initial_paid']
        - sales_reconciliation['linked_receipts']
        - sales_reconciliation['legacy_fifo_estimated']
        - debt_totals
    )
    account_sum_matches_company = suspense_balance == ZERO
    if not account_sum_matches_company:
        warn('cash_accounts_do_not_fully_reconcile',
             'Noma’lum manbali tushum/xarajatlar kassalarga taqsimlanmagani sabab korxona jami uchta kassa yig‘indisidan farq qiladi.',
             suspense_balance=str(suspense_balance))
    transfer_reconciles = transfer_out_total == transfer_in_total
    if not transfer_reconciles:
        warn('transfer_reconciliation_mismatch',
             'Ichki transferlarning chiqim va kirim jami teng emas.',
             transfer_out=str(transfer_out_total), transfer_in=str(transfer_in_total))

    end_events = [row for row in events if in_period(row['_day'])]
    end_events.sort(key=lambda row: (row['_day'] or date.max, row['type'], row['source'], row['source_id']))
    period_sales = ZERO
    period_sales_kg = ZERO
    for event in end_events:
        if event['type'] == 'Sotuv (daromad, naqd tushum emas)':
            period_sales += event['amount']
            for role in accounts:
                if event['actor_role'] == role:
                    accounts[role].setdefault('period_sales_amount', ZERO)
                    accounts[role]['period_sales_amount'] += event['amount']
                    sale = sales_by_id.get(event['source_id'], {})
                    accounts[role]['period_sales_kg'] = (
                        accounts[role].get('period_sales_kg', ZERO)
                        + _amount(sale.get('qty_kg', 0)))
            sale = sales_by_id.get(event['source_id'], {})
            period_sales_kg += _amount(sale.get('qty_kg', 0))
    period_known_receipts = sum(
        (account['period_receipts_excluding_transfers'] for account in accounts.values()), ZERO)
    period_expenses = (
        sum((account['period_expenses'] for account in accounts.values()), ZERO)
        + period_suspense_out)
    period_cash_additions = sum(
        (account['period_cash_additions'] for account in accounts.values()), ZERO)
    period_net_cash_flow = (
        period_known_receipts + period_suspense_in
        + period_cash_additions - period_expenses)
    company_initial = ZERO
    return {
        'period': {'start': period_start, 'start_label': start_label, 'end': end.isoformat()},
        'accounts': accounts,
        'events': end_events,
        'warnings': warnings,
        'sales': {'amount': sales_total, 'kg': sales_kg,
                  'period_amount': period_sales, 'period_kg': period_sales_kg},
        'cash': {
            'period_known_receipts': period_known_receipts,
            'period_cash_additions': period_cash_additions,
            'period_unassigned_receipts': period_suspense_in,
            'period_expenses': period_expenses,
            'period_unassigned_expenses': period_suspense_out,
            'period_net_change': period_net_cash_flow,
            'allocated_balance': allocated_balance,
            'suspense_balance': suspense_balance,
            'company_balance': company_balance,
            'company_channels': company_channels,
            'accounts_sum_matches_company': account_sum_matches_company,
            'opening_balance': company_initial,
            'opening_balance_confirmed': False,
            'balance_label': 'Hisobiy qoldiq',
            'period_total_cash_including_legacy': (
                period_known_receipts + period_suspense_in),
            'transfer_out': transfer_out_total,
            'transfer_in': transfer_in_total,
            'transfers_reconcile': transfer_reconciles,
        },
        'debt': {'snapshot_total': snapshot_debt,
                 'assigned_to_accounts': assigned_debt,
                 'unassigned': unassigned_debt,
                 'legacy_fifo_is_estimate': True},
        'sales_reconciliation': sales_reconciliation,
        'transfer_status_totals': transfer_totals,
        'source_counts': {
            'sales': len(sales_rows),
            'batches': len(batches),
            'receipts': receipt_row_count,
            'expenses': len(expense_rows) if expenses_columns else 0,
            'cash_additions': cash_addition_count,
            'wheat_purchase_payments': wheat_payment_count,
            'transfers': transfer_row_count,
            'account_cash_transfers': account_cash_transfer_count,
        },
    }
