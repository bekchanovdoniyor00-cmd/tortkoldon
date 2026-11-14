"""Audited, immutable-ID targeted identity corrections for debtors (local candidate).

Does not create schema, alter financial amounts, or move receipts. The caller must
hold a BEGIN IMMEDIATE transaction and commit only after operation audit is saved.
"""
import hashlib
import json

from customer_helpers import phone_key
from finance_calculations import (
    calculate_financial_snapshot, customer_identity_context, resolve_customer_identity,
)


class Conflict(ValueError):
    pass


def _snapshot_signature(snapshot):
    """Ignore display-name/group keys; preserve amounts, owners and allocations."""
    obligations = sorted(
        (item['kind'], item['id'], item['user_id'], str(item['amount']),
         str(item['initial_paid']), str(item['linked_paid']),
         str(item['legacy_applied']), str(item['balance']))
        for group in snapshot['customers'].values()
        for item in group['obligations']
    )
    return (
        obligations,
        {key: str(value) for key, value in snapshot['totals'].items()},
        sorted(((r['id'], r.get('kind'), r.get('opening_debt_id'),
                 r.get('sale_id'), str(r['amount']), r.get('recipient'))
                for r in snapshot['receipts']), key=repr),
        sorted((r['id'], str(r['amount'])) for r in snapshot['unallocated_legacy']),
    )


def legacy_fingerprint(con, debt_id):
    row = con.execute('SELECT * FROM opening_debts WHERE id=?', (debt_id,)).fetchone()
    if row is None:
        raise LookupError('Eski qarz topilmadi.')
    version = con.execute(
        'SELECT COALESCE(MAX(id), 0) FROM opening_debt_identity_audit WHERE opening_debt_id=?',
        (debt_id,)).fetchone()[0]
    raw = json.dumps([dict(row), version], ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def legacy_identity_edit(con, debt_id, actor_user_id, digest, name, phone,
                         confirm_existing_card=False):
    if not con.in_transaction:
        raise RuntimeError('BEGIN IMMEDIATE transaction required')
    if legacy_fingerprint(con, debt_id) != digest:
        raise Conflict('Eski qarz yozuvi o‘zgargan. Sahifani yangilang.')
    row = con.execute('SELECT * FROM opening_debts WHERE id=?', (debt_id,)).fetchone()
    ctx = customer_identity_context(con)
    if resolve_customer_identity(ctx, row['customer'], row['customer_phone']) is not None:
        raise Conflict('Bu yozuv xaridor kartasiga bog‘langan. Kartani tahrirlang.')
    name, phone = name.strip(), phone.strip()
    if not name or len(name) > 120 or not phone or len(phone) > 40:
        raise ValueError('Ism va telefon to‘ldirilishi kerak.')
    if (name, phone) == (row['customer'], row['customer_phone']):
        raise ValueError('O‘zgartirish kiritilmadi.')
    if phone != row['customer_phone'] and not phone_key(phone):
        raise ValueError('Yangi telefon to‘liq va to‘g‘ri bo‘lsin.')

    # Name-only historical payments cannot be reassigned by a name edit.
    old_new_names = {row['customer'].strip().casefold(), name.casefold()}
    for payment in con.execute('SELECT customer FROM payments'):
        if (payment['customer'] or '').strip().casefold() in old_new_names:
            raise Conflict('Bu ismga eski to‘lov bog‘langan: alohida solishtirish zarur.')
    # A correction can point to an existing customer card ONLY if an accountant
    # deliberately confirms that it is the same person. Never infer identity
    # solely from a matching phone; do not change a name during this operation.
    # The card must be unique via an exact name/phone identity. Historical
    # payments, old obligations, worker assignments and amounts remain untouched.
    new_phone_key = phone_key(phone)
    linked_card = resolve_customer_identity(ctx, name, phone)
    if linked_card is not None:
        if not confirm_existing_card:
            raise Conflict('Bu telefon mavjud xaridor kartasida. Bir shaxs ekanini belgilab tasdiqlang.')
        if (not new_phone_key or
                name != row['customer'] or
                name != linked_card['name'] or
                phone_key(linked_card['phone']) != new_phone_key):
            raise Conflict('Ism yoki karta mos emas; avtomatik birlashtirish taqiqlanadi.')
        # This must truly be a previously observed sale for this same identity,
        # not merely a newly created customer card. No sale record is edited.
        related_sale = any(
            sale['customer'] == linked_card['name'] and
            phone_key(sale['customer_phone']) == new_phone_key
            for sale in con.execute('SELECT customer,customer_phone FROM sales'))
        if not related_sale:
            raise Conflict('Bu telefon bilan shu nomdagi sotuv topilmadi; alohida tekshirish kerak.')
        # Another card's historical audit alias must not claim this pair.
        if (name, new_phone_key) in ctx['ambiguous_identities']:
            raise Conflict('Bu ism/telefon bir nechta xaridor kartasiga bog‘liq.')
    if new_phone_key:
        for other in con.execute('SELECT id,customer,customer_phone FROM sales'):
            if phone_key(other['customer_phone']) == new_phone_key:
                if not (linked_card is not None and other['customer'] == linked_card['name']):
                    raise Conflict('Telefon boshqa sotuvga bog‘langan. Avtomatik birlashtirish taqiqlanadi.')
        # DC63: only a spelling-case correction on the SAME original phone may
        # coexist with another opening debt bearing that phone and casefolded
        # name. This is not a customer merge: source debt IDs, owners, payments,
        # amounts and due dates stay untouched; the old name remains in audit.
        # All other identity collisions are rejected exactly as before.
        case_only_same_phone = (
            phone == row['customer_phone']
            and name.casefold() == row['customer'].strip().casefold()
        )
        for other in con.execute('SELECT id,customer,customer_phone FROM opening_debts WHERE id<>?', (debt_id,)):
            if phone_key(other['customer_phone']) == new_phone_key:
                if (case_only_same_phone
                        and other['customer'].strip().casefold() == name.casefold()):
                    continue
                if not (linked_card is not None and other['customer'] == linked_card['name']):
                    raise Conflict('Telefon boshqa eski qarzga bog‘langan. Avtomatik birlashtirish taqiqlanadi.')
        for card in con.execute('SELECT id,phone FROM customers'):
            if phone_key(card['phone']) == new_phone_key:
                if linked_card is None or card['id'] != linked_card['id']:
                    raise Conflict('Telefon mavjud xaridor kartasida ishlatilgan.')
    else:
        # Unreliable phone unchanged: do not collide with another identity's exact pair.
        if con.execute('SELECT 1 FROM opening_debts WHERE id<>? AND customer=? AND customer_phone=? LIMIT 1',
                       (debt_id, name, phone)).fetchone():
            raise Conflict('Yangi ism va telefon boshqa eski qarz yozuvida bor.')

    before = _snapshot_signature(calculate_financial_snapshot(con))
    con.execute('UPDATE opening_debts SET customer=?,customer_phone=? WHERE id=?',
                (name, phone, debt_id))
    after = _snapshot_signature(calculate_financial_snapshot(con))
    if before != after:
        raise Conflict('Qarz va to‘lov taqsimoti o‘zgardi. Tahrirlash bekor qilindi.')
    return con.execute('''INSERT INTO opening_debt_identity_audit
        (opening_debt_id,actor_user_id,old_name,new_name,old_phone,new_phone)
        VALUES(?,?,?,?,?,?)''',
        (debt_id, actor_user_id, row['customer'], name, row['customer_phone'], phone)).lastrowid


def card_fingerprint(con, customer_id):
    card = con.execute('SELECT * FROM customers WHERE id=?', (customer_id,)).fetchone()
    if card is None:
        raise LookupError('Xaridor kartasi topilmadi.')
    version = con.execute('SELECT COALESCE(MAX(id),0) FROM customer_audit WHERE customer_id=?',
                          (customer_id,)).fetchone()[0]
    return hashlib.sha256(json.dumps([dict(card),version],sort_keys=True,default=str).encode()).hexdigest()


def card_identity_edit(con, customer_id, actor_user_id, digest, name, phone):
    if not con.in_transaction:
        raise RuntimeError('BEGIN IMMEDIATE transaction required')
    if card_fingerprint(con, customer_id) != digest:
        raise Conflict('Xaridor kartasi o‘zgargan. Sahifani yangilang.')
    row = con.execute('SELECT * FROM customers WHERE id=?',(customer_id,)).fetchone()
    name,phone=name.strip(),phone.strip()
    key=phone_key(phone)
    if not name or len(name)>120 or not phone or len(phone)>40 or not key:
        raise ValueError('Ism va to‘liq telefon raqami majburiy.')
    if (name,phone)==(row['name'],row['phone']):
        raise ValueError('O‘zgartirish kiritilmadi.')
    if con.execute('SELECT 1 FROM customers WHERE id<>? AND phone_key=?',(customer_id,key)).fetchone():
        raise Conflict('Bu telefon boshqa xaridorga tegishli.')
    # A new phone must never claim another, previously-unrelated historic row.
    previous_key = phone_key(row['phone'])
    if key != previous_key:
        ctx = customer_identity_context(con)
        for table in ('sales','opening_debts'):
            for source in con.execute('SELECT customer,customer_phone FROM ' + table):
                if phone_key(source['customer_phone']) == key:
                    owner = resolve_customer_identity(ctx,source['customer'],source['customer_phone'])
                    if owner is None or owner['id'] != customer_id:
                        raise Conflict('Yangi telefon boshqa tarixiy yozuvga tegishli bo‘lishi mumkin.')
    before = _snapshot_signature(calculate_financial_snapshot(con))
    con.execute('UPDATE customers SET name=?,phone=?,phone_key=? WHERE id=?',
                (name,phone,key,customer_id))
    audit_id=con.execute('''INSERT INTO customer_audit
        (customer_id,actor_user_id,old_name,new_name,old_phone,new_phone)
        VALUES(?,?,?,?,?,?)''',
        (customer_id,actor_user_id,row['name'],name,row['phone'],phone)).lastrowid
    if before != _snapshot_signature(calculate_financial_snapshot(con)):
        raise Conflict('Qarz va to‘lov taqsimoti o‘zgardi. Tahrirlash bekor qilindi.')
    return audit_id
