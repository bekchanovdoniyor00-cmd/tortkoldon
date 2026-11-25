"""Read-only worker dashboard, inventory and customer search services.

No app.py imports. Dependencies are injected by existing Flask endpoint wrappers.
Unauthorized-role checks remain in app.py in their original order.
"""

from decimal import Decimal
import re


def build_worker_context(
    *, user, endpoint, db_factory, paginate_rows, paginate_items,
    calculate_financial_snapshot, customer_identity_context,
    worker_snapshot_obligations, customer_display, worker_channel_balances,
    expense_display, snapshot_has_ambiguous_legacy, _business_date,
    WORKER_PRODUCTS, ROLES, PAYMENT_METHODS, BAG_OPTIONS, LOOSE_PRODUCTS,
    debt_query="",
):
    with db_factory() as con:
        con.execute('BEGIN')
        sale_select = """SELECT s.*, CASE WHEN c.sale_id IS NULL THEN 0 ELSE 1 END cancelled,
            COALESCE((SELECT SUM(r.amount) FROM worker_receipts r WHERE r.sale_id=s.id),0) receipts
            FROM sales s LEFT JOIN sale_cancellations c ON c.sale_id=s.id
            WHERE s.user_id=? ORDER BY s.id DESC"""
        if endpoint == 'worker_archive':
            rows,rows_pagination=paginate_rows(con,sale_select,
                'SELECT COUNT(*) FROM sales WHERE user_id=?',(user['id'],))
        else:
            rows=con.execute(sale_select,(user['id'],)).fetchall()
            rows_pagination=None
        active_count=con.execute("""SELECT COUNT(*) FROM sales s
            WHERE s.user_id=? AND NOT EXISTS(
                SELECT 1 FROM sale_cancellations c WHERE c.sale_id=s.id)""",
            (user['id'],)).fetchone()[0]
        financial = calculate_financial_snapshot(con)
        customer_context = customer_identity_context(con)
        obligations = worker_snapshot_obligations(financial, user['id'])
        obligations_by_sale = {item['id']: item for item in obligations}
        display_rows = []
        for row in rows:
            current_name, current_phone = customer_display(
                customer_context, row['customer'], row['customer_phone'])
            display_rows.append(dict(row, customer=current_name, customer_phone=current_phone,
                financial_balance=obligations_by_sale.get(row['id'], {}).get('balance', Decimal('0')),
                legacy_estimated_amount=obligations_by_sale.get(row['id'], {}).get('legacy_applied', Decimal('0'))))
        rows = display_rows
        channel_balances = worker_channel_balances(con,user['id'])
        balance = round(sum(channel_balances.values()), 2)
        recent_receipts = con.execute("""SELECT r.date, r.amount, r.payment_method, s.customer,
                        s.customer_phone, s.product
                        FROM worker_receipts r JOIN sales s ON s.id=r.sale_id
                        WHERE r.worker_id=? ORDER BY r.id DESC LIMIT 30""", (user['id'],)).fetchall()
        opening_recent = con.execute("""SELECT r.date,r.amount,r.payment_method,d.customer,d.customer_phone,
                'Eski qarz' product FROM opening_debt_receipts r
                JOIN opening_debts d ON d.id=r.opening_debt_id
                WHERE r.worker_id=? AND r.received_by='worker' ORDER BY r.id DESC LIMIT 30""", (user['id'],)).fetchall()
        recent_receipts = [
            dict(row, customer=customer_display(
                customer_context, row['customer'], row['customer_phone'])[0])
            for row in list(recent_receipts) + list(opening_recent)
        ]
        recent_receipts.sort(key=lambda row: _business_date(row['date'])[0],reverse=True)
        recent_receipts = recent_receipts[:30]
        expenses = [expense_display(row) for row in con.execute("""SELECT e.*,
                        entered.username AS entered_by_name, source.role AS source_role
                        FROM expenses e
                        LEFT JOIN users entered ON entered.id=COALESCE(e.entered_by,e.user_id)
                        LEFT JOIN users source ON source.id=e.user_id
                        WHERE e.user_id=? ORDER BY e.id DESC LIMIT 30""", (user['id'],))]
        transfers = con.execute('SELECT * FROM worker_transfers WHERE worker_id=? ORDER BY id DESC LIMIT 30',(user['id'],)).fetchall()
    active = [r for r in rows if not r['cancelled']]
    total_debt = sum((item['balance'] for item in obligations), Decimal('0'))
    groups = {}
    for item in obligations:
        if item['balance'] <= Decimal('0.009'):
            continue
        key = (item['customer'].strip(), item['phone'])
        group = groups.setdefault(key, {'debt': Decimal('0'), 'legacy_estimated': Decimal('0')})
        group['debt'] += item['balance']
        group['legacy_estimated'] += item['legacy_applied']
    debt_groups = [{
        'customer': name, 'phone': phone, 'debt': round(amounts['debt'], 2),
        'legacy_estimated_amount': round(amounts['legacy_estimated'], 2),
        'payment_blocked': snapshot_has_ambiguous_legacy(financial, name),
    } for (name, phone), amounts in sorted(groups.items())]
    legacy_payment_warning = any(item['payment_blocked'] for item in debt_groups)
    if endpoint == 'worker_debts':
        from customer_autocomplete_service import _name_key, _phone_digits
        needle = str(debt_query or '').strip()[:80]
        if len(needle) >= 2:
            nk, digits = _name_key(needle), _phone_digits(needle)
            numeric = len(digits) >= 2 and not any(c.isalpha() for c in needle)
            debt_groups = [g for g in debt_groups if (
                digits in _phone_digits(g['phone']) if numeric
                else nk in _name_key(g['customer'])
            )]
        debt_groups,debt_groups_pagination=paginate_items(
            debt_groups,'debt_page','debt_per_page')
    else:
        debt_groups_pagination=None
    legacy_estimated_total = sum((item['legacy_applied'] for item in obligations), Decimal('0'))
    return dict(rows=rows, products=WORKER_PRODUCTS[user['role']], role_name=ROLES[user['role']],
                 balance=balance, channel_balances=channel_balances, payment_methods=PAYMENT_METHODS,
                 recent_receipts=recent_receipts, expenses=expenses, transfers=transfers, bag_options=BAG_OPTIONS,
                loose_products=LOOSE_PRODUCTS, active_count=active_count, total_debt=total_debt,
                debt_groups=debt_groups, legacy_estimated_total=legacy_estimated_total,
                legacy_payment_warning=legacy_payment_warning,
                rows_pagination=rows_pagination,
                debt_groups_pagination=debt_groups_pagination)



def load_worker_stock_status(
    *, user, db_factory, product_stocks, bagged_product_stocks, worker_products,
):
    with db_factory() as con:
        stocks=product_stocks(con)
        bag_stocks=bagged_product_stocks(con)
    products=worker_products[user['role']]
    return {
        'stocks': {p: round(float(stocks.get(p,0)), 4) for p in products},
        'bag_stocks': {p:bag_stocks[p] for p in products if p in bag_stocks},
    }



def search_worker_customers(q, *, db_factory):
    q=q.strip()[:80]
    if len(q)<2: return {'customers': []}
    digits=re.sub(r'\D','',q)
    with db_factory() as con:
        if len(digits)>=3 and len(digits)>=len(q.replace(' ', '').replace('+','').replace('-','')):
            rows=con.execute('SELECT id,name,phone FROM customers WHERE phone_key LIKE ? ORDER BY name LIMIT 12',('%'+digits+'%',)).fetchall()
        else:
            rows=con.execute('SELECT id,name,phone FROM customers WHERE name LIKE ? COLLATE NOCASE ORDER BY name LIMIT 12',('%'+q+'%',)).fetchall()
    return {'customers':[dict(r) for r in rows]}
