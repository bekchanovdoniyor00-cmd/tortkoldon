"""Existing worker financial helpers, moved verbatim from app.py.
No app import, Flask runtime or new business logic.
"""
import math
from decimal import ROUND_DOWN
from customer_helpers import phone_key
from payment_helpers import PAYMENT_METHODS


def worker_channel_balances(con, uid):
    result = {method: 0.0 for method in PAYMENT_METHODS}
    # Legacy rows without an explicit recipient remain assigned to the seller.
    for r in con.execute("""SELECT s.payment_method method, COALESCE(SUM(s.paid),0) amount
          FROM sales s LEFT JOIN sale_cancellations c ON c.sale_id=s.id
          WHERE COALESCE(s.initial_payment_recipient_id,s.user_id)=? AND c.sale_id IS NULL
          AND NOT EXISTS (SELECT 1 FROM worker_sale_batch_items bi WHERE bi.sale_id=s.id)
          GROUP BY s.payment_method""", (uid,)):
        result[r['method']] = result.get(r['method'], 0) + float(r['amount'])
    for r in con.execute("""SELECT bp.payment_method method, SUM(bp.amount) amount
          FROM worker_sale_batch_payments bp JOIN worker_sale_batches b ON b.id=bp.batch_id
          WHERE b.worker_id=? AND NOT EXISTS (
              SELECT 1 FROM worker_sale_batch_items bi JOIN sale_cancellations c ON c.sale_id=bi.sale_id
              WHERE bi.batch_id=b.id)
          GROUP BY bp.payment_method""", (uid,)):
        result[r['method']] = result.get(r['method'], 0) + float(r['amount'])
    for r in con.execute('SELECT payment_method method, SUM(amount) amount FROM worker_receipts WHERE worker_id=? AND id NOT IN (SELECT receipt_id FROM accountant_worker_receipts) GROUP BY payment_method', (uid,)):
        result[r['method']] = result.get(r['method'], 0) + float(r['amount'])
    for r in con.execute("SELECT payment_method method, SUM(amount) amount FROM opening_debt_receipts WHERE received_by='worker' AND receiver_id=? GROUP BY payment_method", (uid,)):
        result[r['method']] = result.get(r['method'], 0) + float(r['amount'])
    for r in con.execute('SELECT payment_method method, SUM(amount) amount FROM expenses WHERE user_id=? GROUP BY payment_method', (uid,)):
        result[r['method']] = result.get(r['method'], 0) - float(r['amount'])
    for r in con.execute("SELECT payment_method method, SUM(amount) amount FROM worker_transfers WHERE worker_id=? AND status='approved' GROUP BY payment_method", (uid,)):
        result[r['method']] = result.get(r['method'], 0) - float(r['amount'])
    return {method: round(result.get(method, 0.0), 2) for method in PAYMENT_METHODS}

def worker_balance(con, uid):
    return round(sum(worker_channel_balances(con, uid).values()), 2)

def worker_snapshot_obligations(financial, worker_id, customer=None, phone=None):
    normalized_phone = phone_key(phone) or (phone or '')
    obligations = []
    for group in financial['customers'].values():
        if customer is not None and group['customer'] != customer:
            continue
        if phone is not None and group['phone'] != normalized_phone:
            continue
        for item in group['obligations']:
            if item['kind'] in ('sale','opening') and item.get('user_id') == worker_id:
                obligations.append(dict(item, customer=group['customer'],
                                        phone=group['phone']))
    return sorted(obligations, key=lambda item: (item['date'],
        0 if item['kind']=='opening' else 1, item['id']))

def snapshot_has_ambiguous_legacy(financial, customer):
    return any(warning['code'] == 'legacy_identity_ambiguous'
               and warning.get('customer') == customer
               for warning in financial['warnings'])

def balance_cents(balance):
    return int((balance * 100).to_integral_value(rounding=ROUND_DOWN))

def payment_amount_cents(raw_amount):
    amount = float(raw_amount)
    if not math.isfinite(amount) or amount <= 0:
        raise ValueError('To‘lov summasini tekshiring.')
    cents = round(amount * 100)
    if cents <= 0 or abs(amount * 100 - cents) > 0.001:
        raise ValueError('Summani tiyingacha aniq kiriting.')
    return cents
