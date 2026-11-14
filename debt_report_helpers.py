"""Read-only customer debt reporting helpers moved from app.py."""
from business_dates import _business_date, _business_date_after
from finance_calculations import (calculate_financial_snapshot, customer_identity_context)
from customer_helpers import customer_display


def customer_debt_details(con, customer):
    """Oldest-first payment allocation across sales and opening debts."""
    obligations = []
    for r in con.execute("""SELECT id, date, due_date, qty_kg*price_per_kg-paid-COALESCE((SELECT SUM(wr.amount) FROM worker_receipts wr WHERE wr.sale_id=sales.id),0) balance
                            FROM sales WHERE customer=? AND id NOT IN (SELECT sale_id FROM sale_cancellations)""", (customer,)):
        due = r['due_date'] or _business_date_after(r['date'],25)
        _business_date(due)
        if r['balance'] > 0.009:
            obligations.append({'kind':'sale', 'id':r['id'], 'date':r['date'], 'due_date':due, 'balance':float(r['balance'])})
    for r in con.execute('''SELECT d.id,d.date,d.due_date,d.amount,
             COALESCE((SELECT SUM(r.amount) FROM opening_debt_receipts r WHERE r.opening_debt_id=d.id),0) paid
             FROM opening_debts d WHERE d.customer=?''', (customer,)):
        _business_date(r['date'])
        _business_date(r['due_date'])
        remaining = float(r['amount'])-float(r['paid'])
        if remaining>0.009:
            obligations.append({'kind':'opening', 'id':r['id'], 'date':r['date'], 'due_date':r['due_date'], 'balance':remaining})
    obligations.sort(key=lambda r: (_business_date(r['date'])[0], 0 if r['kind']=='opening' else 1, r['id']))
    remaining_payments = float(con.execute('SELECT COALESCE(SUM(amount),0) FROM payments WHERE customer=?', (customer,)).fetchone()[0] or 0)
    items = []
    for obligation in obligations:
        applied = min(obligation['balance'], max(0.0, remaining_payments))
        remaining_payments -= applied
        balance = obligation['balance'] - applied
        if balance > 0.009:
            items.append({'kind': obligation['kind'], 'id':obligation['id'], 'balance':balance, 'due_date':obligation['due_date']})
    return items

def debt_customers(con, financial=None, per_obligation=False):
    # Debt pages and exports retain customer totals; control can classify each due separately.
    if financial is None:
        financial=calculate_financial_snapshot(con)
    if financial is not None:
        raw_phones = {}
        customer_context=customer_identity_context(con)
        if not per_obligation:
            for table, kind in (('sales', 'sale'), ('opening_debts', 'opening')):
                for row in con.execute('SELECT id,customer,customer_phone FROM '+table):
                    raw_phones[(kind,row['id'])] = customer_display(
                        customer_context,row['customer'],row['customer_phone'])[1]
        result = []
        for (name, phone), totals in financial['customers'].items():
            if totals['debt'] <= 0:
                continue
            unpaid = [item for item in totals['obligations'] if item['balance'] > 0]
            if per_obligation:
                for item in unpaid:
                    due = item['due_date'] or _business_date_after(item['date'], 25)
                    card = customer_context['cards'].get(totals['customer_id'])
                    result.append({'customer': name,
                        'phone': card['phone'] if card else phone,
                        'debt': item['balance'], 'due_date': due,
                        'kind': item['kind'], 'obligation_id': item['id']})
                continue
            due = min(
                (item['due_date'] or _business_date_after(item['date'],25) for item in unpaid),
                key=lambda value: _business_date(value)[0],
            )
            phones = sorted({raw_phones[(item['kind'],item['id'])] for item in totals['obligations']})
            result.append({'customer':name,'phone':phone,'phones':phones,
                'total':totals['sales_revenue']+totals['opening_debt'],
                'sales_revenue':totals['sales_revenue'],'opening_debt':totals['opening_debt'],
                'initial_paid':totals['initial_paid'],'linked_paid':totals['linked_paid'],
                'legacy_received':totals['legacy_received'],'legacy_applied':totals['legacy_applied'],
                'paid':totals['received'],'excess':totals['linked_excess']+totals['legacy_excess'],
                'debt':totals['debt'],'due_date':due})
        return sorted(result,key=lambda r:(r['customer'].casefold(),r['phone']))
