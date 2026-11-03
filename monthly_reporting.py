"""Period-based ERP summaries and XLSX export generation."""

from datetime import timedelta
from decimal import Decimal
from io import BytesIO
import re

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from cashflow_analytics import calculate_cashflow_analytics
from finance_calculations import (
    customer_identity_context,
    resolve_customer_identity,
    resolve_customer_name,
)


ZERO = Decimal('0')
OPENING_WHEAT_METHOD = 'Boshlang‘ich zaxira'
CHANNELS = ('naqd', 'plastik', 'hisob', 'noma_lum')
CHANNEL_LABELS = {
    'naqd': 'Naqd',
    'plastik': 'Plastik',
    'hisob': 'Bank',
    'noma_lum': 'Aniqlanmagan',
}
ROLE_LABELS = {
    'umid': 'Umid',
    'maxmud': 'Maxmud',
    'buxgalter': 'Buxgalter',
}


def _money(value):
    amount = Decimal(str(value or 0))
    if not amount.is_finite():
        raise ValueError('Hisobotda yaroqsiz sonli qiymat mavjud.')
    return amount


def _explicit_bag_sale(row, flour_products):
    if row.get('sale_mode') != 'qop' or row.get('product') not in flour_products:
        return None
    try:
        size = Decimal(str(row.get('bag_kg')))
        count = Decimal(str(row.get('bag_count')))
        quantity = Decimal(str(row.get('qty_kg')))
    except (ArithmeticError, TypeError, ValueError):
        return None
    if (not size.is_finite() or not count.is_finite()
            or not quantity.is_finite() or size not in (Decimal(25), Decimal(50))
            or count <= 0 or count != count.to_integral_value()
            or quantity != size * count):
        return None
    return row['product'], int(size), int(count)


def _warn(warnings, key, message, **details):
    warnings.append({'key': key, 'message': message, **details})


def _period_rows(con, table, date_column, start, end, parse_date, warnings):
    exists = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if not exists:
        return []
    selected = []
    for row in con.execute(f'SELECT * FROM "{table}"'):
        values = dict(row)
        raw_date = values.get(date_column)
        try:
            day = parse_date(raw_date)[0]
        except (TypeError, ValueError, IndexError):
            _warn(
                warnings, 'undated_period_record',
                'Sana noaniq yozuv tanlangan davrga taxminan biriktirilmadi.',
                table=table, record_id=values.get('id'), date=raw_date,
            )
            continue
        if start <= day <= end:
            values['_day'] = day
            selected.append(values)
    return selected


def build_monthly_report(
        con, start, end, flour_products, byproducts, historical_snapshot,
        parse_date, format_date):
    """Build period activity and as-of balances using existing ERP ledgers."""
    warnings = []
    customer_context = customer_identity_context(con)
    users = {
        row['id']: dict(row)
        for row in con.execute('SELECT id,username,role FROM users')
    }

    wheat_rows = _period_rows(
        con, 'wheat_receipts', 'date', start, end, parse_date, warnings)
    opening_wheat_rows = [
        row for row in wheat_rows
        if row.get('purchase_method') == OPENING_WHEAT_METHOD
    ]
    wheat_rows = [
        row for row in wheat_rows
        if row.get('purchase_method') != OPENING_WHEAT_METHOD
    ]
    production_rows = _period_rows(
        con, 'production', 'date', start, end, parse_date, warnings)
    sale_rows = _period_rows(
        con, 'sales', 'date', start, end, parse_date, warnings)
    opening_debt_rows = _period_rows(
        con, 'opening_debts', 'date', start, end, parse_date, warnings)
    receipt_rows = _period_rows(
        con, 'worker_receipts', 'date', start, end, parse_date, warnings)
    legacy_payment_rows = _period_rows(
        con, 'payments', 'date', start, end, parse_date, warnings)
    expense_rows = _period_rows(
        con, 'expenses', 'date', start, end, parse_date, warnings)
    wheat_payment_rows = _period_rows(
        con, 'wheat_purchase_payments', 'payment_date', start, end,
        parse_date, warnings)
    all_wheat_payments = []
    if con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='wheat_purchase_payments'").fetchone():
        all_wheat_payments = [
            dict(row) for row in con.execute(
                'SELECT wheat_receipt_id,payment_method,payment_date '
                'FROM wheat_purchase_payments')
        ]
    _period_rows(
        con, 'sale_cancellations', 'cancelled_at', start, end,
        parse_date, warnings)

    cancellations = {}
    if con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='sale_cancellations'").fetchone():
        for row in con.execute('SELECT sale_id,cancelled_at FROM sale_cancellations'):
            try:
                cancellation_day = parse_date(row['cancelled_at'])[0]
            except (TypeError, ValueError, IndexError):
                _warn(
                    warnings, 'uncertain_sale_cancellation',
                    'Bekor qilish sanasi noaniq; sotuvning sanali yozuvi saqlandi, '
                    'bekor qilinganlik holati esa tasdiqlanmadi.',
                    sale_id=row['sale_id'], date=row['cancelled_at'],
                )
                continue
            if cancellation_day <= end:
                cancellations[row['sale_id']] = cancellation_day

    period_sales = [
        row for row in sale_rows
        if row['id'] not in cancellations
    ]
    production_ids = {row['id'] for row in production_rows}
    period_production_types = {}
    if production_ids:
        for row in con.execute(
                'SELECT production_id,product,qty_kg FROM production_flour_types'):
            if row['production_id'] in production_ids:
                by_product = period_production_types.setdefault(
                    row['production_id'], {})
                by_product[row['product']] = _money(row['qty_kg'])

    bagged_production = {}
    production_bag_counts = {
        product: {25: 0, 50: 0} for product in flour_products
    }
    if production_ids:
        for row in con.execute(
                'SELECT production_id,product,bag_kg,bag_count '
                'FROM bagged_production_lines'):
            if row['production_id'] not in production_ids:
                continue
            key = (row['production_id'], row['product'], row['bag_kg'])
            bagged_production[key] = row['bag_count']
            if row['product'] in production_bag_counts and row['bag_kg'] in (25, 50):
                production_bag_counts[row['product']][row['bag_kg']] += row['bag_count']

    product_totals = {
        product: ZERO for product in (*flour_products, *byproducts, 'Un (eski yozuv)')
    }
    production_detail = []
    raw_wheat = cleaned_wheat = ZERO
    for row in production_rows:
        raw = _money(row.get('raw_wheat_kg') or row.get('wheat_kg'))
        cleaned = _money(row.get('wheat_kg'))
        raw_wheat += raw
        cleaned_wheat += cleaned
        types = period_production_types.get(row['id'], {})
        for product, amount in types.items():
            product_totals[product] = product_totals.get(product, ZERO) + amount
        legacy_flour = max(ZERO, _money(row.get('flour_kg')) - sum(types.values(), ZERO))
        product_totals['Un (eski yozuv)'] += legacy_flour
        for product, column in (
                ('Kepak', 'bran_kg'), ('Muchka', 'muchka_kg'),
                ('Telashka atxod', 'telashka_kg')):
            product_totals[product] += _money(row.get(column))
        per_product_bags = {
            product: {
                size: bagged_production.get((row['id'], product, size), 0)
                for size in (25, 50)
            }
            for product in flour_products
        }
        production_detail.append({
            'date': format_date(row['_day']),
            'id': row['id'],
            'raw_wheat_kg': raw,
            'cleaned_wheat_kg': cleaned,
            'loss_kg': raw - cleaned,
            'flour_kg': _money(row.get('flour_kg')),
            'products': {product: types.get(product, ZERO)
                         for product in flour_products},
            'legacy_flour_kg': legacy_flour,
            'bran_kg': _money(row.get('bran_kg')),
            'black_flour_kg': types.get('Qora un', ZERO),
            'muchka_kg': _money(row.get('muchka_kg')),
            'telashka_kg': _money(row.get('telashka_kg')),
            'bag_counts': per_product_bags,
            'bag_25': sum(
                count for (pid, _product, size), count in bagged_production.items()
                if pid == row['id'] and size == 25),
            'bag_50': sum(
                count for (pid, _product, size), count in bagged_production.items()
                if pid == row['id'] and size == 50),
        })

    sale_batch_workers = {}
    if con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='worker_sale_batches'").fetchone():
        sale_batch_workers = {
            row['sale_id']: row['worker_id']
            for row in con.execute(
                'SELECT i.sale_id,b.worker_id FROM worker_sale_batch_items i '
                'JOIN worker_sale_batches b ON b.id=i.batch_id')
        }
    sales_by_id = {}
    seller_totals = {
        role: {'sales_count': 0, 'qty_kg': ZERO, 'revenue': ZERO,
               'initial_paid': ZERO, 'initial_debt': ZERO}
        for role in ('umid', 'maxmud')
    }
    sales_detail = []
    for row in period_sales:
        seller_id = sale_batch_workers.get(row['id'], row.get('user_id'))
        seller = users.get(seller_id, {})
        role = seller.get('role', '')
        amount = _money(row.get('qty_kg')) * _money(row.get('price_per_kg'))
        initial = _money(row.get('paid'))
        unpaid_at_sale = max(ZERO, amount - initial)
        sales_by_id[row['id']] = row
        if role in seller_totals:
            seller_totals[role]['sales_count'] += 1
            seller_totals[role]['qty_kg'] += _money(row.get('qty_kg'))
            seller_totals[role]['revenue'] += amount
            seller_totals[role]['initial_paid'] += initial
            seller_totals[role]['initial_debt'] += unpaid_at_sale
        sales_detail.append({
            'date': format_date(row['_day']),
            'sale_id': row['id'],
            'seller': seller.get('username') or 'Biriktirilmagan',
            'seller_role': role,
            'customer': row.get('customer', ''),
            'phone': row.get('customer_phone', ''),
            'product': row.get('product', ''),
            'qty_kg': _money(row.get('qty_kg')),
            'sale_amount': amount,
            'initial_paid': initial,
            'debt_at_sale': unpaid_at_sale,
            'payment_method': row.get('payment_method') or 'Noma’lum',
            'bag_kg': row.get('bag_kg') or '',
            'bag_count': row.get('bag_count') or '',
        })

    bagged_sale_counts = {
        product: {25: 0, 50: 0} for product in flour_products
    }
    recorded_bagged_sale_ids = set()
    if period_sales:
        period_sale_ids = {row['id'] for row in period_sales}
        if con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='bagged_sale_deductions'").fetchone():
            for row in con.execute(
                    'SELECT sale_id,product,bag_kg,bag_count '
                    'FROM bagged_sale_deductions'):
                if row['sale_id'] in period_sale_ids:
                    recorded_bagged_sale_ids.add(row['sale_id'])
                if (row['sale_id'] in period_sale_ids
                        and row['product'] in bagged_sale_counts
                        and row['bag_kg'] in (25, 50)):
                    bagged_sale_counts[row['product']][row['bag_kg']] += row['bag_count']
    for row in period_sales:
        if row['id'] in recorded_bagged_sale_ids:
            continue
        bagged_sale = _explicit_bag_sale(row, flour_products)
        if bagged_sale is None:
            continue
        product, size, count = bagged_sale
        bagged_sale_counts[product][size] += count
        _warn(
            warnings, 'bagged_sale_deduction_missing',
            f"Sotuv ID {row['id']} da qop turi va soni aniq, lekin ombor "
            'qop chiqimi yozuvi yo‘q; sotuv tafsilotidan hisoblandi, '
            'qoldiq avtomatik o‘zgartirilmadi.',
            sale_id=row['id'], product=product, bag_kg=size, bag_count=count,
        )

    total_sales_amount = sum(
        (_money(row['sale_amount']) for row in sales_detail), ZERO)
    sale_qty = sum((_money(row['qty_kg']) for row in sales_detail), ZERO)
    initial_sales_paid = sum((_money(row['initial_paid']) for row in sales_detail), ZERO)
    sales_unpaid_at_sale = sum((_money(row['debt_at_sale']) for row in sales_detail), ZERO)

    receipts_by_sale = {}
    receipt_total = ZERO
    accountant_receipt_ids = {
        row['receipt_id'] for row in con.execute(
            'SELECT receipt_id FROM accountant_worker_receipts')
    } if con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='accountant_worker_receipts'").fetchone() else set()
    receipt_detail = []
    customer_activity = {}

    def customer_key(name, phone):
        card = resolve_customer_identity(customer_context, name, phone)
        if card is None and not phone:
            card = resolve_customer_name(customer_context, name)
        if card is not None:
            name, phone = card['name'], card['phone']
        raw_phone = phone or ''
        digits = re.sub(r'\D', '', raw_phone)
        if len(digits) == 9:
            digits = '998' + digits
        normalized_phone = (
            digits if len(digits) == 12 and digits.startswith('998')
            else raw_phone)
        return (
            name or 'Noma’lum xaridor',
            normalized_phone or 'Telefon ko‘rsatilmagan',
        )

    for row in receipt_rows:
        amount = _money(row.get('amount'))
        receipt_total += amount
        receipts_by_sale[row['sale_id']] = (
            receipts_by_sale.get(row['sale_id'], ZERO) + amount)
        sale = sales_by_id.get(row['sale_id'])
        if sale is None:
            sale = con.execute(
                'SELECT customer,customer_phone FROM sales WHERE id=?',
                (row['sale_id'],)).fetchone()
        if sale:
            key = customer_key(sale['customer'], sale['customer_phone'])
            activity = customer_activity.setdefault(key, {
                'new_debt': ZERO, 'opening_debt_added': ZERO,
                'debt_payments': ZERO})
            activity['debt_payments'] += amount
        else:
            key = customer_key('Sotuv topilmadi', '')
            activity = customer_activity.setdefault(key, {
                'new_debt': ZERO, 'opening_debt_added': ZERO,
                'debt_payments': ZERO})
            activity['debt_payments'] += amount
        recipient = 'Buxgalter' if row['id'] in accountant_receipt_ids else 'Xodim'
        receipt_detail.append({
            'date': format_date(row['_day']),
            'type': 'Qarz to‘lovi',
            'customer': sale['customer'] if sale else 'Sotuv topilmadi',
            'phone': sale['customer_phone'] if sale else '',
            'amount': amount,
            'recipient': recipient,
            'sale_id': row['sale_id'],
            'channel': row.get('payment_method') or 'Noma’lum',
            'note': '',
        })

    legacy_receipt_total = sum(
        (_money(row.get('amount')) for row in legacy_payment_rows), ZERO)
    for row in legacy_payment_rows:
        receipt_detail.append({
            'date': format_date(row['_day']),
            'type': 'Eski to‘lov (xaridor/telefon to‘liq aniqlanmagan)',
            'customer': row.get('customer', ''),
            'phone': '',
            'amount': _money(row.get('amount')),
            'recipient': 'Aniqlanmagan',
            'sale_id': '',
            'channel': 'Noma’lum',
            'note': row.get('note') or '',
        })
    if legacy_receipt_total:
        _warn(
            warnings, 'legacy_debt_payments_unassigned',
            'Eski to‘lovlar telefon yoki aniq sotuvga bog‘lanmagani sababli '
            'qarzdorlarga taxminan taqsimlanmadi.',
            amount=str(legacy_receipt_total),
        )

    new_debt_gross = ZERO
    opening_debt_amount = ZERO
    for row in period_sales:
        debt = max(
            ZERO,
            _money(row.get('qty_kg')) * _money(row.get('price_per_kg'))
            - _money(row.get('paid')),
        )
        new_debt_gross += debt
        key = customer_key(row.get('customer'), row.get('customer_phone'))
        customer_activity.setdefault(
            key, {
                'new_debt': ZERO, 'opening_debt_added': ZERO,
                'debt_payments': ZERO,
            })['new_debt'] += debt
    for row in opening_debt_rows:
        amount = _money(row.get('amount'))
        opening_debt_amount += amount
        key = customer_key(row.get('customer'), row.get('customer_phone'))
        customer_activity.setdefault(
            key, {
                'new_debt': ZERO, 'opening_debt_added': ZERO,
                'debt_payments': ZERO,
            })['opening_debt_added'] += amount

    total_debt_receipts = receipt_total + legacy_receipt_total

    wheat_payments_by_channel = {method: ZERO for method in ('naqd', 'hisob')}
    for row in wheat_payment_rows:
        method = row.get('payment_method')
        if method not in wheat_payments_by_channel:
            _warn(
                warnings, 'unknown_wheat_payment_channel',
                'Bug‘doy xaridi to‘lovining kanali noma’lum; alohida Naqd/Bankga '
                'biriktirilmadi.',
                payment_id=row.get('id'), method=method,
            )
            continue
        wheat_payments_by_channel[method] += _money(row.get('amount'))

    expense_total = sum((_money(row.get('amount')) for row in expense_rows), ZERO)
    wheat_purchase_total = sum(
        (_money(row.get('net')) * _money(row.get('price_per_kg'))
         for row in wheat_rows), ZERO)
    wheat_period_used = sum(
        (_money(row.get('raw_wheat_kg') or row.get('wheat_kg'))
         for row in production_rows), ZERO)

    start_snapshot = historical_snapshot(start - timedelta(days=1), warnings)
    end_snapshot = historical_snapshot(end, warnings)
    opening_stocks = start_snapshot['stocks']
    closing_stocks = end_snapshot['stocks']
    opening_bags = start_snapshot['bags']
    closing_bags = end_snapshot['bags']
    effective_opening_stock_rows = _period_rows(
        con, 'opening_product_stocks', 'stock_date', start, start,
        parse_date, warnings)
    for row in effective_opening_stock_rows:
        opening_stocks[row['product']] = (
            _money(opening_stocks.get(row['product'], 0))
            + _money(row.get('qty_kg')))
    effective_opening_bag_rows = _period_rows(
        con, 'opening_bagged_product_stocks', 'stock_date', start, start,
        parse_date, warnings)
    for row in effective_opening_bag_rows:
        product = row['product']
        size25 = int(row.get('bag_25_count') or 0)
        size50 = int(row.get('bag_50_count') or 0)
        opening_stocks[product] = (
            _money(opening_stocks.get(product, 0)) + size25 * 25 + size50 * 50)
        product_bags = opening_bags.setdefault(product, {25: 0, 50: 0})
        product_bags[25] += size25
        product_bags[50] += size50
    stock_products = []
    stock_product_names = list(dict.fromkeys(
        [*flour_products, *byproducts, 'Un (eski yozuv)']))
    for product in stock_product_names:
        opening = _money(opening_stocks.get(product, 0))
        produced = _money(product_totals.get(product, 0))
        sold = sum(
            (_money(row['qty_kg']) for row in period_sales
             if ('Un (eski yozuv)' if row.get('product') == 'Un'
                 else row.get('product')) == product), ZERO)
        closing = _money(closing_stocks.get(product, 0))
        bag_open = opening_bags.get(product, {25: 0, 50: 0})
        bag_close = closing_bags.get(product, {25: 0, 50: 0})
        bag_produced = production_bag_counts.get(product, {25: 0, 50: 0})
        bag_sold = bagged_sale_counts.get(product, {25: 0, 50: 0})
        stock_products.append({
            'product': product,
            'opening_kg': opening,
            'produced_kg': produced,
            'sold_kg': sold,
            'closing_kg': closing,
            'opening_25': bag_open.get(25, 0),
            'opening_50': bag_open.get(50, 0),
            'produced_25': bag_produced.get(25, 0),
            'produced_50': bag_produced.get(50, 0),
            'sold_25': bag_sold.get(25, 0),
            'sold_50': bag_sold.get(50, 0),
            'closing_25': bag_close.get(25, 0),
            'closing_50': bag_close.get(50, 0),
        })
        if abs(opening + produced - sold - closing) > Decimal('0.01'):
            _warn(
                warnings, 'stock_reconciliation_difference',
                'Mahsulot kg qoldig‘i kirim/chiqim bilan to‘liq tenglashmadi; '
                'tarixiy yoki tasniflanmagan yozuvlarni tekshiring.',
                product=product, difference=str(opening + produced - sold - closing),
            )
        for size in (25, 50):
            if (bag_open.get(size, 0) + bag_produced.get(size, 0)
                    - bag_sold.get(size, 0) != bag_close.get(size, 0)):
                _warn(
                    warnings, 'bag_stock_reconciliation_difference',
                    'Qop qoldig‘i kirim/chiqim bilan tenglashmadi; eski kg-only '
                    'yozuvlar qopga aylantirilmagan.',
                    product=product, bag_kg=size,
                )

    wheat_opening = start_snapshot['wheat_stock']
    wheat_closing = end_snapshot['wheat_stock']
    opening_wheat_added = sum(
        (_money(row.get('net')) for row in opening_wheat_rows), ZERO)
    wheat_reconciled = (
        wheat_opening
        + sum((_money(row.get('net')) for row in wheat_rows), ZERO)
        + opening_wheat_added
        - wheat_period_used
    )
    if abs(wheat_reconciled - wheat_closing) > Decimal('0.01'):
        _warn(
            warnings, 'wheat_stock_reconciliation_difference',
            'Bug‘doy qoldig‘i kirim va ishlab chiqarish sarfi bilan tenglashmadi.',
            difference=str(wheat_reconciled - wheat_closing),
        )

    period_cash = calculate_cashflow_analytics(
        con, start.isoformat(), end.isoformat())
    for warning in period_cash['warnings']:
        if warning not in warnings:
            warnings.append(warning)

    closing_channels = period_cash['cash']['company_channels']
    period_channel_delta = {channel: ZERO for channel in CHANNELS}
    period_account_delta = {
        role: {channel: ZERO for channel in CHANNELS}
        for role in period_cash['accounts']
    }
    for event in period_cash['events']:
        channel = event.get('channel_key') or 'noma_lum'
        delta = _money(event.get('cash_delta'))
        period_channel_delta[channel] += delta
        role = event.get('cash_role')
        if role in period_account_delta:
            period_account_delta[role][channel] += delta
    # Analytics balances are cumulative through period end; remove in-period
    # movements to reconstruct the opening balances without a second full scan.
    opening_channels = {
        channel: closing_channels[channel] - period_channel_delta[channel]
        for channel in CHANNELS
    }
    cash_accounts = []
    for role, account in period_cash['accounts'].items():
        cash_accounts.append({
            'role': role,
            'name': account['name'],
            'opening': {
                channel: account['closing_channels'][channel]
                - period_account_delta[role][channel]
                for channel in CHANNELS
            },
            'closing': account['closing_channels'],
        })

    cash_events = []
    flow_totals = {
        'sales_and_debt_receipts': ZERO,
        'external_additions': ZERO,
        'wheat_payments': ZERO,
        'other_expenses': ZERO,
        'transfers_in': ZERO,
        'transfers_out': ZERO,
    }
    flow_channels = {
        key: {channel: ZERO for channel in CHANNELS}
        for key in flow_totals
    }
    for event in period_cash['events']:
        amount = _money(event.get('amount'))
        cash_delta = _money(event.get('cash_delta'))
        event_type = event.get('type', '')
        channel = event.get('channel_key') or 'noma_lum'
        flow_key = None
        flow_amount = ZERO
        if event_type == 'Kassaga pul qo‘shish':
            flow_key, flow_amount = 'external_additions', amount
        elif event_type == 'Bug‘doy xaridi uchun to‘lov':
            flow_key, flow_amount = 'wheat_payments', amount
        elif event_type == 'Xarajat':
            flow_key, flow_amount = 'other_expenses', amount
        elif (event_type.startswith('Hisoblar orasida pul ko‘chirish')
              or event_type.startswith('Tasdiqlangan transfer —')):
            if cash_delta >= ZERO:
                flow_key, flow_amount = 'transfers_in', amount
            else:
                flow_key, flow_amount = 'transfers_out', amount
        elif cash_delta > ZERO:
            flow_key, flow_amount = 'sales_and_debt_receipts', amount
        if flow_key:
            flow_totals[flow_key] += flow_amount
            flow_channels[flow_key][channel] += flow_amount
        if cash_delta or event_type == 'Sotuv (daromad, naqd tushum emas)':
            cash_events.append({
                'date': event.get('date', ''),
                'type': event_type,
                'account': event.get('actor', ''),
                'counterparty': event.get('counterparty', ''),
                'purpose': event.get('purpose', ''),
                'channel': event.get('channel', ''),
                'amount': amount,
                'cash_delta': cash_delta,
                'source': event.get('source', ''),
                'source_id': event.get('source_id', ''),
                'note': event.get('details', ''),
            })

    confirmed_wheat_opening = False
    for row in con.execute(
            'SELECT date FROM wheat_receipts WHERE purchase_method=?',
            (OPENING_WHEAT_METHOD,)):
        try:
            if parse_date(row['date'])[0] <= start:
                confirmed_wheat_opening = True
                break
        except (TypeError, ValueError, IndexError):
            continue
    if not confirmed_wheat_opening:
        _warn(
            warnings, 'wheat_opening_balance_unconfirmed',
            'Bug‘doy uchun alohida fizik boshlang‘ich qoldiq daftari yo‘q; '
            'qoldiq mavjud bug‘doy kirimlari va ishlab chiqarish sarfidan '
            'nol boshlang‘ich mezonida hisoblandi.',
        )
    recorded_opening_products = set()
    for table in ('opening_product_stocks', 'opening_bagged_product_stocks'):
        exists = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,)).fetchone()
        if not exists:
            continue
        for row in con.execute(f'SELECT product,stock_date FROM "{table}"'):
            try:
                opening_day = parse_date(row['stock_date'])[0]
            except (TypeError, ValueError, IndexError):
                continue
            if opening_day <= start:
                recorded_opening_products.add(row['product'])
    missing_opening_products = set((*flour_products, *byproducts)) - recorded_opening_products
    if missing_opening_products:
        _warn(
            warnings, 'unconfirmed_initial_stock',
            'Quyidagi mahsulotlarda davr boshigacha boshlang‘ich saldo yozuvi '
            'yo‘q; ular mavjud kirim/chiqimdan nol boshlang‘ich mezonida '
            'hisoblandi.',
            products=', '.join(sorted(missing_opening_products)),
        )

    for row in wheat_rows:
        matching_payments = [
            payment for payment in all_wheat_payments
            if payment['wheat_receipt_id'] == row['id']
        ]
        if not matching_payments:
            _warn(
                warnings, 'wheat_payment_method_unavailable',
                'Bug‘doy xaridi uchun to‘lov yozuvi topilmadi; summa Naqd yoki '
                'Bankga taxminan ajratilmadi.',
                receipt_id=row['id'],
            )
    for row in wheat_payment_rows:
        if not any(
                wheat['id'] == row['wheat_receipt_id']
                for wheat in wheat_rows):
            linked = con.execute(
                'SELECT date FROM wheat_receipts WHERE id=?',
                (row['wheat_receipt_id'],)).fetchone()
            if linked is None:
                _warn(
                    warnings, 'wheat_payment_without_receipt',
                    'Bug‘doy to‘lovi manba kirimisiz qoldi; alohida tekshiring.',
                    payment_id=row['id'],
                )

    debt_opening_total = (
        start_snapshot['financial']['totals']['debt']
        if start_snapshot['financial'] else None)
    debt_closing_total = (
        end_snapshot['financial']['totals']['debt']
        if end_snapshot['financial'] else None)
    if debt_opening_total is None or debt_closing_total is None:
        _warn(
            warnings, 'debt_snapshot_unavailable',
            'Tarixiy qarz snapshotini sana yoki majburiyat yozuvi xatosi sabab '
            'ishonchli hisoblab bo‘lmadi.',
        )
    else:
        for snapshot in (start_snapshot, end_snapshot):
            for item in snapshot['financial']['warnings']:
                _warn(
                    warnings, 'debt_snapshot_' + item['code'],
                    'Qarz snapshotida tarixiy tasnif yoki bog‘lanish noaniqligi bor.',
                    details=str(item),
                )

    debt_details = []
    opening_financial = start_snapshot['financial']
    closing_financial = end_snapshot['financial']
    customer_snapshots = {}
    for label, financial in (
            ('opening', opening_financial), ('closing', closing_financial)):
        if not financial:
            continue
        for group in financial['customers'].values():
            key = customer_key(group['customer'], group['phone'])
            item = customer_snapshots.setdefault(key, {
                'customer': key[0], 'phone': key[1],
                'opening_debt': ZERO, 'closing_debt': ZERO,
            })
            item['opening_debt' if label == 'opening' else 'closing_debt'] = group['debt']
    for key, item in customer_snapshots.items():
        activity = customer_activity.get(key, {})
        item['new_debt'] = activity.get('new_debt', ZERO)
        item['opening_debt_added'] = activity.get(
            'opening_debt_added', ZERO)
        item['debt_payments'] = activity.get('debt_payments', ZERO)
        debt_details.append(item)
    for key, activity in customer_activity.items():
        if key not in customer_snapshots:
            debt_details.append({
                'customer': key[0], 'phone': key[1],
                'opening_debt': ZERO,
                'new_debt': activity['new_debt'],
                'opening_debt_added': activity['opening_debt_added'],
                'debt_payments': activity['debt_payments'],
                'closing_debt': ZERO,
            })
    if legacy_receipt_total:
        debt_details.append({
            'customer': 'Taqsimlanmagan eski to‘lovlar',
            'phone': '',
            'opening_debt': ZERO,
            'new_debt': ZERO,
            'opening_debt_added': ZERO,
            'debt_payments': legacy_receipt_total,
            'closing_debt': ZERO,
        })
    debt_details.sort(key=lambda item: (item['customer'].casefold(), item['phone']))

    return {
        'start': start,
        'end': end,
        'start_label': format_date(start),
        'end_label': format_date(end),
        'warnings': warnings,
        'production': {
            'raw_wheat_kg': raw_wheat,
            'cleaned_wheat_kg': cleaned_wheat,
            'loss_kg': raw_wheat - cleaned_wheat,
            'loss_percent': (raw_wheat - cleaned_wheat) * 100 / raw_wheat
            if raw_wheat else ZERO,
            'products': product_totals,
            'bag_counts': production_bag_counts,
            'rows': production_detail,
        },
        'inventory': {'products': stock_products},
        'sales': {
            'qty_kg': sale_qty,
            'amount': total_sales_amount,
            'initial_paid': initial_sales_paid,
            'debt_at_sale': sales_unpaid_at_sale,
            'by_seller': [
                dict(seller_totals[role], role=role, label=ROLE_LABELS[role])
                for role in ('umid', 'maxmud')
            ],
            'rows': sales_detail,
        },
        'debt': {
            'opening': debt_opening_total,
            'new_sales_debt': new_debt_gross,
            'new_opening_debt': opening_debt_amount,
            'opening_debt_added': opening_debt_amount,
            'new_debt': new_debt_gross,
            'linked_payments': receipt_total,
            'legacy_payments': legacy_receipt_total,
            'payments': total_debt_receipts,
            'closing': debt_closing_total,
            'customers': debt_details,
            'payment_rows': receipt_detail,
        },
        'cash': {
            'opening_channels': opening_channels,
            'closing_channels': closing_channels,
            'accounts': cash_accounts,
            'flows': flow_totals,
            'flow_channels': flow_channels,
            'events': cash_events,
            'baseline_confirmed': False,
            'warning_count': len(period_cash['warnings']),
        },
        'wheat': {
            'opening_kg': wheat_opening,
            'purchased_kg': sum((_money(row.get('net')) for row in wheat_rows), ZERO),
            'opening_stock_added_kg': opening_wheat_added,
            'used_kg': wheat_period_used,
            'closing_kg': wheat_closing,
            'purchase_value': wheat_purchase_total,
            'payments_by_channel': wheat_payments_by_channel,
            'initial_stock_entries': [{
                'date': format_date(row['_day']),
                'id': row['id'],
                'qty_kg': _money(row.get('net')),
                'note': row.get('note') or '',
            } for row in opening_wheat_rows],
            'receipts': [{
                'date': format_date(row['_day']),
                'id': row['id'],
                'supplier': row.get('supplier') or '',
                'gross_kg': _money(row.get('gross')),
                'tare_kg': _money(row.get('tare')),
                'net_kg': _money(row.get('net')),
                'unit_price': _money(row.get('price_per_kg')),
                'value': _money(row.get('net')) * _money(row.get('price_per_kg')),
                'purchase_method': row.get('purchase_method') or 'Noma’lum',
                'payment_status': (
                    'To‘lov shu davrda'
                    if any(payment['wheat_receipt_id'] == row['id']
                           for payment in wheat_payment_rows)
                    else (
                        'To‘lov boshqa davrda'
                        if any(payment['wheat_receipt_id'] == row['id']
                               for payment in all_wheat_payments)
                        else 'To‘lov yozuvi topilmadi'
                    )
                ),
            } for row in wheat_rows],
            'payments': [{
                'date': format_date(row['_day']),
                'receipt_id': row.get('wheat_receipt_id'),
                'channel': CHANNEL_LABELS.get(row.get('payment_method'), 'Noma’lum'),
                'amount': _money(row.get('amount')),
            } for row in wheat_payment_rows],
            'unclassified_purchase_value': sum(
                (_money(row.get('net')) * _money(row.get('price_per_kg'))
                 for row in wheat_rows
                 if not any(payment['wheat_receipt_id'] == row['id']
                            for payment in all_wheat_payments)),
                ZERO),
        },
    }


def _sheet_header(ws, title, subtitle, headers):
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(headers))
    ws.cell(1, 1, title)
    ws.cell(1, 1).font = Font(bold=True, size=16, color='FFFFFF')
    ws.cell(1, 1).fill = PatternFill('solid', fgColor='175B42')
    ws.cell(1, 1).alignment = Alignment(vertical='center')
    ws.row_dimensions[1].height = 27
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=len(headers))
    ws.cell(2, 1, subtitle)
    ws.cell(2, 1).font = Font(italic=True, color='52665E')
    for index, value in enumerate(headers, 1):
        cell = ws.cell(4, index, value)
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = PatternFill('solid', fgColor='247A58')
        cell.alignment = Alignment(wrap_text=True, vertical='center')
    ws.freeze_panes = 'A5'


def _append_rows(ws, rows, number_columns=()):
    for values in rows:
        ws.append(values)
        for index, value in enumerate(values, 1):
            cell = ws.cell(ws.max_row, index)
            if isinstance(value, str):
                cell.data_type = 's'
            if index in number_columns and isinstance(value, (int, float, Decimal)):
                cell.number_format = '#,##0.00;[Red]-#,##0.00;0'
            cell.alignment = Alignment(vertical='top', wrap_text=isinstance(value, str))


def _finish_sheet(ws, widths):
    ws.auto_filter.ref = f'A4:{ws.cell(ws.max_row, len(widths)).coordinate}'
    for index, width in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(index)].width = width
    ws.sheet_view.showGridLines = False


def build_monthly_workbook(report):
    """Create the seven-sheet, complete-data workbook for one date range."""
    workbook = Workbook()
    subtitle = f"Dav davri: {report['start_label']} — {report['end_label']}"
    production = report['production']

    ws = workbook.active
    ws.title = 'Umumiy natijalar'
    headers = ('Bo‘lim', 'Ko‘rsatkich', 'Qiymat', 'Birlik', 'Izoh')
    _sheet_header(ws, 'Oylik umumiy natijalar', subtitle, headers)
    summary_rows = [
        ('Ishlab chiqarish', 'Qayta ishlangan bug‘doy', production['raw_wheat_kg'], 'kg', ''),
        ('Ishlab chiqarish', 'Tozalangan bug‘doy', production['cleaned_wheat_kg'], 'kg', ''),
        ('Ishlab chiqarish', 'Kamayish', production['loss_kg'], 'kg', f"{production['loss_percent']:.2f}%"),
        ('Sotuv', 'Jami sotuv miqdori', report['sales']['qty_kg'], 'kg', ''),
        ('Sotuv', 'Sotuv summasi', report['sales']['amount'], 'so‘m', ''),
        ('Sotuv', 'Sotuv paytidagi to‘lov', report['sales']['initial_paid'], 'so‘m', ''),
        ('Sotuv', 'Sotuv paytidagi to‘lanmagan qism', report['sales']['debt_at_sale'], 'so‘m', ''),
        ('Qarz', 'Davr boshidagi qarzdorlik', report['debt']['opening'], 'so‘m', ''),
        ('Qarz', 'Davrda yuzaga kelgan yangi sotuv qarzi', report['debt']['new_debt'], 'so‘m', ''),
        ('Qarz', 'Davrda qayd etilgan dastlabki qarzlar', report['debt']['opening_debt_added'], 'so‘m', 'Sotuv qarzi emas'),
        ('Qarz', 'Davrda qabul qilingan qarz to‘lovlari', report['debt']['payments'], 'so‘m', ''),
        ('Qarz', 'Davr oxiridagi qarzdorlik', report['debt']['closing'], 'so‘m', ''),
        ('Bug‘doy', 'Davr boshidagi qoldiq', report['wheat']['opening_kg'], 'kg', ''),
        ('Bug‘doy', 'Boshlang‘ich zaxira qo‘shildi', report['wheat']['opening_stock_added_kg'], 'kg', 'Xarid va pul harakati emas'),
        ('Bug‘doy', 'Xarid qilingan', report['wheat']['purchased_kg'], 'kg', ''),
        ('Bug‘doy', 'Ishlab chiqarishga sarflangan', report['wheat']['used_kg'], 'kg', ''),
        ('Bug‘doy', 'Davr oxiridagi qoldiq', report['wheat']['closing_kg'], 'kg', ''),
        ('Bug‘doy', 'Xarid qiymati', report['wheat']['purchase_value'], 'so‘m', ''),
        ('Pul', 'Tashqi kassa kirimi', report['cash']['flows']['external_additions'], 'so‘m', 'Daromad emas'),
        ('Pul', 'Bug‘doy xaridi to‘lovlari', report['cash']['flows']['wheat_payments'], 'so‘m', 'Xarajatlarda bir marta'),
        ('Pul', 'Boshqa xarajatlar', report['cash']['flows']['other_expenses'], 'so‘m', ''),
        ('Pul', 'Ichki transfer chiqimi', report['cash']['flows']['transfers_out'], 'so‘m', 'Korxona pulini kamaytirmaydi'),
        ('Nazorat', 'Ogohlantirishlar soni', len(report['warnings']), 'ta', ''),
    ]
    for channel in CHANNELS:
        summary_rows.append((
            'Kassa', f"Davr boshida — {CHANNEL_LABELS[channel]}",
            report['cash']['opening_channels'][channel], 'so‘m', 'Hisobiy qoldiq',
        ))
        summary_rows.append((
            'Kassa', f"Davr oxirida — {CHANNEL_LABELS[channel]}",
            report['cash']['closing_channels'][channel], 'so‘m', 'Hisobiy qoldiq',
        ))
        for flow_name, flow_key in (
                ('Tashqi kassa kirimi', 'external_additions'),
                ('Bug‘doy xaridi to‘lovi', 'wheat_payments'),
                ('Sotuv va qarz tushumi', 'sales_and_debt_receipts'),
                ('Boshqa xarajat', 'other_expenses')):
            summary_rows.append((
                'Pul oqimi',
                f'{flow_name} — {CHANNEL_LABELS[channel]}',
                report['cash']['flow_channels'][flow_key][channel],
                'so‘m', '',
            ))
    for warning in report['warnings']:
        summary_rows.append((
            'Ogohlantirish',
            warning.get('key', warning.get('code', 'Noma’lum ogohlantirish')),
            '',
            '',
            warning['message'],
        ))
    _append_rows(ws, summary_rows, (3,))
    _finish_sheet(ws, (20, 42, 20, 14, 56))

    ws = workbook.create_sheet('Ishlab chiqarish')
    flour_columns = tuple(report['production']['bag_counts'])
    bag_columns = tuple(
        f'{product} {size} kg qop'
        for product in flour_columns
        for size in (25, 50)
    )
    prod_headers = (
        'Sana', 'Ishlab chiqarish ID', 'Xom bug‘doy, kg',
        'Tozalangan bug‘doy, kg', 'Kamayish, kg',
        *flour_columns, 'Un (eski yozuv), kg',
        'Kepak, kg', 'Muchka, kg', 'Telashka atxod, kg',
        *bag_columns,
    )
    _sheet_header(ws, 'Ishlab chiqarish', subtitle, prod_headers)
    prod_rows = []
    for row in production['rows']:
        prod_rows.append((
            row['date'], row['id'], row['raw_wheat_kg'],
            row['cleaned_wheat_kg'], row['loss_kg'],
            *(row['products'].get(product, ZERO)
              for product in flour_columns),
            row['legacy_flour_kg'],
            row['bran_kg'], row['muchka_kg'], row['telashka_kg'],
            *(row['bag_counts'].get(product, {}).get(size, 0)
              for product in flour_columns
              for size in (25, 50)),
        ))
    _append_rows(ws, prod_rows, tuple(range(3, len(prod_headers) + 1)))
    _finish_sheet(ws, (14, 18, 19, 21, 17, *(15 for _ in prod_headers[5:])))

    ws = workbook.create_sheet('Ombor')
    stock_headers = (
        'Mahsulot', 'Davr boshidagi kg', 'Ishlab chiqarilgan kg',
        'Sotilgan/chiqim kg', 'Davr oxiridagi kg',
        'Boshida 25 kg', 'Boshida 50 kg', 'Kirim 25 kg', 'Kirim 50 kg',
        'Chiqim 25 kg', 'Chiqim 50 kg', 'Oxirida 25 kg', 'Oxirida 50 kg',
    )
    _sheet_header(ws, 'Ombor qoldiqlari', subtitle, stock_headers)
    _append_rows(ws, [(
        row['product'], row['opening_kg'], row['produced_kg'], row['sold_kg'],
        row['closing_kg'], row['opening_25'], row['opening_50'],
        row['produced_25'], row['produced_50'], row['sold_25'],
        row['sold_50'], row['closing_25'], row['closing_50'],
    ) for row in report['inventory']['products']], tuple(range(2, 14)))
    _finish_sheet(ws, (25, 20, 22, 22, 20, *(15 for _ in range(8))))

    ws = workbook.create_sheet('Sotuvlar')
    sales_headers = (
        'Sana', 'Sotuv ID', 'Xodim', 'Xaridor', 'Telefon', 'Mahsulot',
        'Miqdor, kg', 'Summa, so‘m', 'Boshlang‘ich to‘lov, so‘m',
        'Sotuv paytidagi qarz, so‘m', 'To‘lov turi', 'Qop, kg', 'Qop soni',
    )
    _sheet_header(ws, 'Sotuvlar', subtitle, sales_headers)
    _append_rows(ws, [(
        row['date'], row['sale_id'], row['seller'], row['customer'], row['phone'],
        row['product'], row['qty_kg'], row['sale_amount'], row['initial_paid'],
        row['debt_at_sale'], row['payment_method'], row['bag_kg'], row['bag_count'],
    ) for row in report['sales']['rows']], (7, 8, 9, 10))
    _finish_sheet(ws, (14, 13, 22, 26, 18, 22, 17, 20, 23, 24, 20, 12, 12))

    ws = workbook.create_sheet('Xaridor qarzlari')
    debt_headers = (
        'Xaridor', 'Telefon', 'Davr boshidagi qarz, so‘m',
        'Yangi sotuv qarzi, so‘m',
        'Davrda qayd etilgan dastlabki qarz, so‘m',
        'Qarz to‘lovlari, so‘m', 'Davr oxiridagi qarz, so‘m',
    )
    _sheet_header(ws, 'Xaridor qarzlari', subtitle, debt_headers)
    _append_rows(ws, [(
        row['customer'], row['phone'], row['opening_debt'], row['new_debt'],
        row['opening_debt_added'], row['debt_payments'], row['closing_debt'],
    ) for row in report['debt']['customers']], (3, 4, 5, 6, 7))
    ws.append((
        'JAMI', '', report['debt']['opening'], report['debt']['new_debt'],
        report['debt']['opening_debt_added'], report['debt']['payments'],
        report['debt']['closing'],
    ))
    debt_summary_end = ws.max_row
    for cell in ws[ws.max_row]:
        cell.font = Font(bold=True)
    if report['debt']['payment_rows']:
        ws.append(('', '', '', '', '', ''))
        ws.append(('Davr ichidagi qarz to‘lovlari', '', '', '', '', ''))
        ws.cell(ws.max_row, 1).font = Font(bold=True, color='175B42')
        ws.append((
            'Sana', 'To‘lov turi', 'Xaridor', 'Telefon', 'Summa, so‘m',
            'Qabul qiluvchi / sotuv ID / kanal / izoh',
        ))
        for cell in ws[ws.max_row]:
            cell.font = Font(bold=True, color='FFFFFF')
            cell.fill = PatternFill('solid', fgColor='247A58')
        for payment in report['debt']['payment_rows']:
            ws.append((
                payment['date'], payment['type'], payment['customer'],
                payment['phone'], payment['amount'],
                f"{payment['recipient']} / {payment['sale_id']} / "
                f"{payment['channel']} / {payment['note']}",
            ))
            for cell in ws[ws.max_row]:
                if isinstance(cell.value, str):
                    cell.data_type = 's'
        for row_index in range(debt_summary_end + 4, ws.max_row + 1):
            ws.cell(row_index, 5).number_format = '#,##0.00;[Red]-#,##0.00;0'
    _finish_sheet(ws, (28, 22, 24, 24, 30, 22, 24))
    ws.auto_filter.ref = f'A4:G{debt_summary_end}'

    ws = workbook.create_sheet('Kassa va pul aylanishi')
    cash_headers = (
        'Sana', 'Operatsiya', 'Hisob egasi', 'Qarshi tomon', 'To‘lov kanali',
        'Operatsiya summasi, so‘m', 'Kassa ta’siri, so‘m',
        'Manba', 'Manba ID', 'Izoh',
    )
    _sheet_header(ws, 'Kassa va pul aylanishi', subtitle, cash_headers)
    for channel in CHANNELS:
        ws.append((
            f"Korxona — {CHANNEL_LABELS[channel]}",
            'Davr boshidagi jami qoldiq', '', '', CHANNEL_LABELS[channel],
            '', report['cash']['opening_channels'][channel], '', '', '',
        ))
        ws.append((
            f"Korxona — {CHANNEL_LABELS[channel]}",
            'Davr oxiridagi jami qoldiq', '', '', CHANNEL_LABELS[channel],
            '', report['cash']['closing_channels'][channel], '', '', '',
        ))
    for account in report['cash']['accounts']:
        for channel in CHANNELS:
            ws.append((
                f"{account['name']} — {CHANNEL_LABELS[channel]}",
                'Davr boshidagi qoldiq', '', '', CHANNEL_LABELS[channel],
                '', account['opening'].get(channel, ZERO), '', '', '',
            ))
            ws.append((
                f"{account['name']} — {CHANNEL_LABELS[channel]}",
                'Davr oxiridagi qoldiq', '', '', CHANNEL_LABELS[channel],
                '', account['closing'].get(channel, ZERO), '', '', '',
            ))
    ws.append(('', '', '', '', '', '', '', '', '', ''))
    for row in report['cash']['events']:
        ws.append((
            row['date'], row['type'], row['account'], row['counterparty'],
            row['channel'], row['amount'], row['cash_delta'],
            row['source'], row['source_id'], row['note'],
        ))
        for cell in ws[ws.max_row]:
            if isinstance(cell.value, str):
                cell.data_type = 's'
    for row_index in range(5, ws.max_row + 1):
        for column in (6, 7):
            ws.cell(row_index, column).number_format = '#,##0.00;[Red]-#,##0.00;0'
    _finish_sheet(ws, (26, 38, 22, 25, 22, 23, 20, 28, 14, 56))

    ws = workbook.create_sheet('Bug‘doy xaridi')
    wheat_headers = (
        'Sana', 'Kirim ID', 'Yetkazib beruvchi', 'Brutto, kg', 'Tara, kg',
        'Sof kirim, kg', '1 kg narxi', 'Xarid qiymati, so‘m',
        'Xarid usuli', 'To‘lov sanasi', 'To‘lov kanali', 'To‘langan, so‘m',
    )
    _sheet_header(ws, 'Bug‘doy xaridi va sarfi', subtitle, wheat_headers)
    payments_by_receipt = {}
    for payment in report['wheat']['payments']:
        payments_by_receipt.setdefault(payment['receipt_id'], []).append(payment)
    for opening in report['wheat']['initial_stock_entries']:
        ws.append((
            opening['date'], opening['id'], '',
            opening['qty_kg'], ZERO, opening['qty_kg'], ZERO, ZERO,
            OPENING_WHEAT_METHOD, '', 'Xarid va pul harakati emas', '',
        ))
    for receipt in report['wheat']['receipts']:
        payments = payments_by_receipt.get(receipt['id'], [])
        if payments:
            for payment in payments:
                ws.append((
                    receipt['date'], receipt['id'], receipt['supplier'],
                    receipt['gross_kg'], receipt['tare_kg'], receipt['net_kg'],
                    receipt['unit_price'], receipt['value'], receipt['purchase_method'],
                    payment['date'], payment['channel'], payment['amount'],
                ))
        else:
            ws.append((
                receipt['date'], receipt['id'], receipt['supplier'],
                receipt['gross_kg'], receipt['tare_kg'], receipt['net_kg'],
                receipt['unit_price'], receipt['value'], receipt['purchase_method'],
                '', receipt['payment_status'], '',
            ))
    period_receipt_ids = {
        receipt['id'] for receipt in report['wheat']['receipts']
    }
    for payment in report['wheat']['payments']:
        if payment['receipt_id'] not in period_receipt_ids:
            ws.append((
                payment['date'], payment['receipt_id'], '',
                '', '', '', '', '', 'Oldingi davr kirimi',
                payment['date'], payment['channel'], payment['amount'],
            ))
    _finish_sheet(ws, (14, 13, 25, 16, 14, 17, 18, 23, 26, 16, 20, 20))

    for sheet in workbook.worksheets:
        for row in sheet.iter_rows(min_row=5):
            for cell in row:
                if isinstance(cell.value, str):
                    cell.data_type = 's'
    output = BytesIO()
    workbook.save(output)
    output.seek(0)
    return output
