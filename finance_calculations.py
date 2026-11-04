"""Shared read-only financial calculations for debt and reconciliation views.

Amounts are Decimal values, without changing/rounding historical database rows.
Caller supplies a connection and, for a consistent concurrent snapshot, an
existing read transaction. This module never starts/commits a transaction.

Legacy payments have only a customer name, not a sale/phone reference. Where
an exact audited name/phone alias resolves to one stable customer ID, use the
current card identity. Name-only legacy payments are allocated only if that
name uniquely resolves and the resulting group has one reliable phone and
worker owner. Then use the existing reporting FIFO: obligation date, opening
debt before sale on a tie, then row ID. FIFO is a reporting allocation, NOT
proof of the historical payment destination; it is never written to the
database. Ambiguous aliases, multiple phone groups, worker owners, or no
obligations: keep legacy money unallocated and report the ambiguity. Do not
infer identity from similar names or change dates.
"""

from decimal import Decimal
from collections import defaultdict
import re

from date_conversions import dmy_to_date, iso_to_date


_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_DMY_DATE = re.compile(r"\d{2}-\d{2}-\d{4}\Z")


def _identity(name, phone):
    raw = phone or ''
    digits = re.sub(r'\D', '', raw)
    if len(digits) == 9:
        digits = '998' + digits
    normalized = digits if len(digits) == 12 and digits.startswith('998') else raw
    return (name, normalized)


def _reliable_phone(phone):
    digits = re.sub(r'\D', '', phone or '')
    return (len(digits) == 9 or
            (len(digits) == 12 and digits.startswith('998')))


def customer_identity_context(con):
    """Build exact, collision-safe aliases from customer cards and audit history."""
    tables = {row[0] for row in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    if 'customers' not in tables:
        return {'cards': {}, 'identity_to_id': {}, 'ambiguous_identities': set(),
                'name_to_ids': {}}

    cards = {
        row['id']: {'id': row['id'], 'name': row['name'], 'phone': row['phone']}
        for row in con.execute('SELECT id,name,phone FROM customers')
    }
    pair_candidates = defaultdict(set)
    name_candidates = defaultdict(set)

    def add_alias(customer_id, name, phone):
        if customer_id not in cards or not name:
            return
        name_candidates[name].add(customer_id)
        if phone and _reliable_phone(phone):
            pair_candidates[_identity(name, phone)].add(customer_id)

    for customer_id, card in cards.items():
        add_alias(customer_id, card['name'], card['phone'])

    if 'customer_audit' in tables:
        for row in con.execute(
                'SELECT customer_id,old_name,new_name,old_phone,new_phone FROM customer_audit'):
            add_alias(row['customer_id'], row['old_name'], row['old_phone'])
            add_alias(row['customer_id'], row['new_name'], row['new_phone'])

    identity_to_id = {
        pair: next(iter(ids)) for pair, ids in pair_candidates.items() if len(ids) == 1
    }
    return {
        'cards': cards,
        'identity_to_id': identity_to_id,
        'ambiguous_identities': {
            pair for pair, ids in pair_candidates.items() if len(ids) > 1
        },
        'name_to_ids': {name: set(ids) for name, ids in name_candidates.items()},
    }


def resolve_customer_identity(context, name, phone):
    """Resolve an exact name/phone pair only when it maps to one stable customer ID."""
    customer_id = context['identity_to_id'].get(_identity(name, phone))
    return context['cards'].get(customer_id)


def resolve_customer_name(context, name):
    """Resolve a name-only alias only when it maps to exactly one customer ID."""
    ids = context['name_to_ids'].get(name, set())
    return context['cards'].get(next(iter(ids))) if len(ids) == 1 else None


def _parse_business_date(value, field):
    if not isinstance(value, str):
        raise ValueError(f"{field} sanasi matn bo‘lishi kerak.")
    try:
        if _ISO_DATE.fullmatch(value):
            return iso_to_date(value), 'iso'
        if _DMY_DATE.fullmatch(value):
            return dmy_to_date(value), 'dmy'
        raise ValueError("YYYY-MM-DD yoki DD-MM-YYYY formati kutilgan.")
    except ValueError as exc:
        raise ValueError(f"{field} sanasi yaroqsiz ({value!r}): {exc}") from exc


def calculate_financial_snapshot(con):
    """Return obligations, customer groups, receipts, excess and warnings.

Requires sqlite3.Row-compatible rows. Receipt totals represent money received,
not cash on hand (expenses/transfers are deliberately outside this mechanism).
Accountant receipt rows classify worker_receipts; they are not extra income.
Cancelled-sale money is retained as an exception, never silently refunded.
"""
    zero = Decimal('0')

    def money(value):
        result = Decimal(str(value))
        if not result.is_finite() or result < zero:
            raise ValueError('Non-finite or negative financial amount')
        return result

    sales = {r['id']: dict(r) for r in con.execute('SELECT * FROM sales')}
    opening_rows = [dict(r) for r in con.execute('SELECT * FROM opening_debts ORDER BY id')]
    # Backward compatible with older read-only snapshots and isolated tests where
    # the additive opening-receipt table has not yet been created. Production
    # migrations must still be run explicitly before accepting new payments.
    has_opening_receipts = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='opening_debt_receipts'"
    ).fetchone() is not None
    opening_receipts = ([dict(r) for r in con.execute(
        'SELECT * FROM opening_debt_receipts ORDER BY id')]
        if has_opening_receipts else [])
    receipts = [dict(r) for r in con.execute('SELECT * FROM worker_receipts ORDER BY id')]
    accountant_rows = [dict(r) for r in con.execute('SELECT * FROM accountant_worker_receipts')]
    legacy_rows = [dict(r) for r in con.execute('SELECT * FROM payments ORDER BY id')]

    def normalize_date(value, field, allow_empty=False):
        if allow_empty and value in (None, ''):
            return ''
        parsed, _ = _parse_business_date(value, field)
        return parsed.isoformat()

    for sale_id, row in sales.items():
        row['date'] = normalize_date(row['date'], f"sales[{sale_id}].date")
        row['due_date'] = normalize_date(
            row['due_date'], f"sales[{sale_id}].due_date", allow_empty=True
        )
    for row in opening_rows:
        row['date'] = normalize_date(row['date'], f"opening_debts[{row['id']}].date")
        row['due_date'] = normalize_date(
            row['due_date'], f"opening_debts[{row['id']}].due_date"
        )
    for row in opening_receipts:
        row['date'] = normalize_date(row['date'], f"opening_debt_receipts[{row['id']}].date")
    for row in receipts:
        row['date'] = normalize_date(row['date'], f"worker_receipts[{row['id']}].date")
    for row in accountant_rows:
        row['payment_date'] = normalize_date(
            row['payment_date'],
            f"accountant_worker_receipts[{row['id']}].payment_date"
        )
    for row in legacy_rows:
        row['date'] = normalize_date(row['date'], f"payments[{row['id']}].date")

    cancelled = {r['sale_id'] for r in con.execute('SELECT sale_id FROM sale_cancellations')}
    customer_context = customer_identity_context(con)

    def canonical_identity(name, phone):
        card = resolve_customer_identity(customer_context, name, phone)
        if card is None:
            return _identity(name, phone), None
        return _identity(card['name'], card['phone']), card['id']

    accountant = {r['receipt_id']: r for r in accountant_rows}
    linked = {}
    warnings = []
    received_worker = received_accountant = received_linked = zero
    receipt_rows = []
    for row in receipts:
        amount = money(row['amount'])
        received_linked += amount
        direct = accountant.get(row['id'])
        if direct is not None:
            received_accountant += amount
            if money(direct['amount']) != amount or direct['worker_id'] != row['worker_id'] or direct['payment_method'] != row['payment_method']:
                warnings.append({'code': 'accountant_receipt_mismatch', 'receipt_id': row['id']})
        else:
            received_worker += amount
        linked[row['sale_id']] = linked.get(row['sale_id'], zero) + amount
        if row['sale_id'] not in sales or row['sale_id'] in cancelled:
            warnings.append({'code': 'receipt_without_active_sale', 'receipt_id': row['id'], 'amount': amount})
        receipt_rows.append(dict(row, amount=amount, recipient='accountant' if direct is not None else 'worker'))
    opening_paid = {}
    for row in opening_receipts:
        amount = money(row['amount'])
        received_linked += amount
        if row['received_by']=='accountant':
            received_accountant += amount
        elif row['received_by']=='worker':
            received_worker += amount
        else:
            warnings.append({'code':'opening_receipt_invalid_recipient','receipt_id':row['id']})
        opening_paid[row['opening_debt_id']] = opening_paid.get(row['opening_debt_id'],zero)+amount
        receipt_rows.append(dict(row, kind='opening', sale_id=None,
                                 opening_debt_id=row['opening_debt_id'], recipient=row['received_by']))
    receipt_ids = {r['id'] for r in receipts}
    for rid in accountant.keys() - receipt_ids:
        warnings.append({'code': 'accountant_receipt_without_source', 'receipt_id': rid})

    customers = {}
    known_identities = {}
    known_workers = {}
    for row in sales.values():
        key, _ = canonical_identity(row['customer'], row['customer_phone'])
        known_identities.setdefault(key[0], set()).add(key)
        known_workers.setdefault(key[0], set()).add(row.get('user_id'))
    for row in opening_rows:
        key, _ = canonical_identity(row['customer'], row['customer_phone'])
        known_identities.setdefault(key[0], set()).add(key)

    def group(key, customer_id=None):
        if key not in customers:
            customers[key] = {'customer': key[0], 'phone': key[1], 'obligations': [],
                              'sales_revenue': zero, 'opening_debt': zero,
                              'initial_paid': zero, 'linked_paid': zero,
                              'legacy_received': zero, 'legacy_applied': zero,
                              'legacy_excess': zero, 'linked_excess': zero, 'debt': zero,
                              'customer_id': customer_id, 'legacy_payment_ids': []}
        elif customer_id is not None:
            customers[key]['customer_id'] = customer_id
        return customers[key]

    initial_received = zero
    for sid, row in sales.items():
        initial = money(row['paid'])
        initial_received += initial
        if sid in cancelled:
            if initial:
                warnings.append({'code': 'cancelled_sale_initial_payment', 'sale_id': sid, 'amount': initial})
            continue
        total = money(row['qty_kg']) * money(row['price_per_kg'])
        paid = linked.get(sid, zero)
        key, customer_id = canonical_identity(row['customer'], row['customer_phone'])
        g = group(key, customer_id)
        g['sales_revenue'] += total
        g['initial_paid'] += initial
        g['linked_paid'] += paid
        excess = max(zero, initial + paid - total)
        g['linked_excess'] += excess
        if excess:
            warnings.append({'code': 'sale_overpaid', 'sale_id': sid, 'amount': excess})
        g['obligations'].append({'kind': 'sale', 'id': sid, 'date': row['date'],
            'user_id': row['user_id'],
            'due_date': row['due_date'], 'amount': total, 'initial_paid': initial,
            'linked_paid': paid, 'legacy_applied': zero,
            'balance': max(zero, total - initial - paid)})
    for row in opening_rows:
        amount = money(row['amount'])
        key, customer_id = canonical_identity(row['customer'], row['customer_phone'])
        g = group(key, customer_id)
        g['opening_debt'] += amount
        paid = opening_paid.get(row['id'],zero)
        g['linked_paid'] += paid
        if paid > amount:
            warnings.append({'code':'opening_debt_overpaid','opening_debt_id':row['id'],'amount':paid-amount})
            g['linked_excess'] += paid-amount
        g['obligations'].append({'kind': 'opening', 'id': row['id'], 'date': row['date'],
            'user_id': row.get('worker_id'),
            'due_date': row['due_date'], 'amount': amount, 'initial_paid': zero,
            'linked_paid': paid, 'legacy_applied': zero,
            'balance': max(zero,amount-paid)})

    legacy_by_name = {}
    unallocated = []
    for row in legacy_rows:
        card = resolve_customer_name(customer_context, row['customer'])
        if card is None and len(customer_context['name_to_ids'].get(row['customer'], set())) > 1:
            unallocated_row = dict(row, amount=money(row['amount']))
            unallocated.append(unallocated_row)
            for candidate_id in customer_context['name_to_ids'][row['customer']]:
                candidate = customer_context['cards'][candidate_id]
                warnings.append({'code': 'legacy_identity_ambiguous',
                                 'customer': candidate['name'],
                                 'legacy_customer': row['customer'],
                                 'payment_ids': [row['id']]})
            continue
        name = card['name'] if card is not None else row['customer']
        legacy_by_name.setdefault(name, []).append(dict(row, amount=money(row['amount'])))
    for name, rows in legacy_by_name.items():
        matches = [g for key, g in customers.items() if key[0] == name]
        identities = known_identities.get(name, set())
        workers = known_workers.get(name, set())
        identity_is_reliable = (len(identities) == 1 and
                                _reliable_phone(next(iter(identities))[1]) and
                                len(workers) == 1 and None not in workers)
        if len(matches) != 1 or not identity_is_reliable:
            unallocated.extend(rows)
            identity_is_ambiguous = (len(identities) > 1 or
                (len(identities) == 1 and not _reliable_phone(next(iter(identities))[1])) or
                len(workers) != 1 or None in workers)
            warnings.append({'code': 'legacy_identity_ambiguous' if identity_is_ambiguous else 'legacy_without_obligation',
                             'customer': name, 'payment_ids': [r['id'] for r in rows]})
            continue
        g = matches[0]
        left = sum((r['amount'] for r in rows), zero)
        g['legacy_received'] = left
        g['legacy_payment_ids'].extend(r['id'] for r in rows)
        warnings.append({'code': 'legacy_fifo_not_explicit_link', 'customer': name,
                         'payment_ids': [r['id'] for r in rows]})
        for item in sorted(g['obligations'], key=lambda x: (
                iso_to_date(x['date']),
                0 if x['kind'] == 'opening' else 1,
                x['id'])):
            applied = min(left, item['balance'])
            item['legacy_applied'] = applied
            item['balance'] -= applied
            g['legacy_applied'] += applied
            left -= applied
        g['legacy_excess'] = left
    for g in customers.values():
        g['debt'] = sum((x['balance'] for x in g['obligations']), zero)
        g['received'] = g['initial_paid'] + g['linked_paid'] + g['legacy_received']
    legacy_received = sum((money(r['amount']) for r in legacy_rows), zero)
    return {'customers': customers, 'receipts': receipt_rows, 'unallocated_legacy': unallocated,
            'warnings': warnings,
            'totals': {'sales_revenue': sum((g['sales_revenue'] for g in customers.values()), zero),
                       'opening_debt': sum((g['opening_debt'] for g in customers.values()), zero),
                       'debt': sum((g['debt'] for g in customers.values()), zero),
                       'initial_received': initial_received, 'linked_received': received_linked,
                       'legacy_received': legacy_received,
                       'received': initial_received + received_linked + legacy_received,
                       'worker_collected_receipts': received_worker,
                       'accountant_collected_receipts': received_accountant,
                       'excess': sum((g['linked_excess'] + g['legacy_excess'] for g in customers.values()), zero),
                       'unallocated_legacy': sum((r['amount'] for r in unallocated), zero)}}
