"""Read-only cash and payment reporting helpers moved from app.py."""
from business_dates import _business_date_key
from payment_helpers import PAYMENT_METHODS


def sale_payment_allocations(con):
    result = {}
    names = [r[0] for r in con.execute('SELECT customer FROM sales UNION SELECT customer FROM opening_debts')]
    for customer in names:
        debts = []
        for r in con.execute('SELECT id,date,qty_kg*price_per_kg-paid AS balance FROM sales WHERE customer=? AND id NOT IN (SELECT sale_id FROM sale_cancellations)', (customer,)):
            if r['balance']>0: debts.append((r['date'],1,r['id'],r['balance']))
        for r in con.execute('SELECT id,date,amount FROM opening_debts WHERE customer=?',(customer,)):
            debts.append((r['date'],0,r['id'],r['amount']))
        left=con.execute('SELECT COALESCE(SUM(amount),0) FROM payments WHERE customer=?',(customer,)).fetchone()[0]
        for d,kind,id_,balance in sorted(debts):
            allocated=min(max(0,left),balance);left-=allocated
            if kind==1:result[id_]=allocated
    return result

def cash_receipt_totals(con, day, custodian='all'):
    # payments is the legacy accountant receipt ledger; a named recipient is not inferred.
    # Cancellation is not a cash refund. Internal transfers only change custody.
    from decimal import Decimal
    if custodian not in ('all','accountant','workers'):
        raise ValueError('Kassa turi noto‘g‘ri.')
    zero = Decimal('0')
    result = {key: zero for key in ('initial','sales','legacy','linked','expenses','additions','direct','undated','unassigned','transfers')}
    result['channels'] = {method: zero for method in PAYMENT_METHODS}
    events = con.execute("""SELECT date,paid amount,'sales' kind,payment_method,0 direct,
            (SELECT role FROM users
             WHERE id=COALESCE(sales.initial_payment_recipient_id,sales.user_id)) owner
            FROM sales WHERE paid<>0
        UNION ALL SELECT date,amount,'legacy','','0','buxgalter' FROM payments
        UNION ALL SELECT r.date,r.amount,'linked',r.payment_method,
            EXISTS(SELECT 1 FROM accountant_worker_receipts a WHERE a.receipt_id=r.id),'' FROM worker_receipts r
        UNION ALL SELECT r.date,r.amount,'linked',r.payment_method,
            CASE WHEN r.received_by='accountant' THEN 1 ELSE 0 END,
            (SELECT role FROM users WHERE id=r.receiver_id) FROM opening_debt_receipts r
        UNION ALL SELECT date,amount,'expenses','',0,(SELECT role FROM users WHERE id=expenses.user_id) FROM expenses
        UNION ALL SELECT payment_date,amount,'expenses',payment_method,0,'buxgalter'
            FROM wheat_purchase_payments
        UNION ALL SELECT date,amount,'additions',payment_method,0,'buxgalter'
            FROM accountant_cash_additions
        UNION ALL SELECT decided_at,amount,'transfers',payment_method,0,'buxgalter'
            FROM worker_transfers WHERE status='approved'""")
    for row in events:
        kind = row['kind']
        unknown_owner = False
        if kind=='transfers' and custodian=='all':
            continue
        if custodian!='all':
            if kind in ('sales','expenses','additions'):
                if row['owner']=='buxgalter':
                    if custodian=='workers': continue
                elif row['owner'] in ('umid','maxmud','ishchi'):
                    if custodian=='accountant': continue
                else:
                    unknown_owner = True
            elif kind=='legacy' and custodian=='workers':
                continue
            elif kind=='linked' and bool(row['direct']) != (custodian=='accountant'):
                continue
        amount = Decimal(str(row['amount']))
        if not amount.is_finite() or amount < zero:
            raise ValueError('Pul yozuvida yaroqsiz summa mavjud.')
        event_day = _business_date_key(str(row['date'])[:10])
        if unknown_owner:
            if event_day<=day: result['unassigned'] += amount
            continue
        delta = -amount if kind=='expenses' or (kind=='transfers' and custodian=='workers') else amount
        if event_day < day:
            result['initial'] += delta
        elif event_day == day:
            result[kind] += amount
            if kind=='linked' and row['direct']:
                result['direct'] += amount
                method = row['payment_method']
                result['channels'][method] = result['channels'].get(method,zero) + amount
    result['payments'] = result['legacy'] + result['linked']
    transfer_delta = -result['transfers'] if custodian=='workers' else result['transfers']
    result['balance'] = (
        result['initial'] + result['sales'] + result['payments']
        + result['additions'] - result['expenses'] + transfer_delta)
    return result
