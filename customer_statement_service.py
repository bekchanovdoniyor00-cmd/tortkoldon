"""Read-only customer statement data; original SQL and calculations preserved."""


def build_customer_statement(customer, *, db, calculate_financial_snapshot,
                             customer_identity_context, resolve_customer_name,
                             resolve_customer_identity, customer_display, _business_date):
    from decimal import Decimal
    with db() as con:
        con.execute('BEGIN')
        financial = calculate_financial_snapshot(con)
        customer_context=customer_identity_context(con)
        requested_card=resolve_customer_name(customer_context,customer)
        display_customer=requested_card['name'] if requested_card else customer
        sales = {r['id']: dict(r) for r in con.execute(
            'SELECT s.*,i.id invoice_id,i.number invoice_number FROM sales s '
            'LEFT JOIN invoices i ON i.sale_id=s.id ORDER BY business_date_key(s.date),s.id')}
        opening = {r['id']: dict(r) for r in con.execute(
            'SELECT * FROM opening_debts ORDER BY business_date_key(date),id')}
        legacy = {r['id']: dict(r) for r in con.execute(
            'SELECT * FROM payments ORDER BY business_date_key(date),id')}
        def belongs_to_statement(row):
            card=resolve_customer_identity(
                customer_context,row['customer'],row['customer_phone'])
            if requested_card is not None:
                return card is not None and card['id']==requested_card['id']
            if card is not None:
                return card['name']==display_customer
            return row['customer']==display_customer

        matching_sale_ids = {
            sid for sid,row in sales.items() if belongs_to_statement(row)
        }
        opening_receipts = [dict(r) for r in con.execute(
            'SELECT o.*,d.customer,d.customer_phone FROM opening_debt_receipts o '
            'JOIN opening_debts d ON d.id=o.opening_debt_id')]
        receipts = [r for r in financial['receipts'] if r['sale_id'] in matching_sale_ids]
        receipt_ids = {r['id'] for r in receipts}
        if matching_sale_ids:
            placeholders=','.join('?' for _ in matching_sale_ids)
            receipt_ids.update(row['receipt_id'] for row in con.execute(
                'SELECT a.receipt_id FROM accountant_worker_receipts a '
                'JOIN worker_receipts r ON r.id=a.receipt_id '
                'WHERE r.sale_id IN ('+placeholders+')',tuple(matching_sale_ids)))
        financial_warnings = [w for w in financial['warnings'] if
            w.get('customer') == display_customer or w.get('sale_id') in matching_sale_ids
            or w.get('receipt_id') in receipt_ids]
        groups = []
        active_sale_ids = set()
        for key, totals in financial['customers'].items():
            if key[0] != display_customer:
                continue
            group_sales, group_opening, history, raw_phones = [], [], [], set()
            for item in totals['obligations']:
                row = dict(sales[item['id']] if item['kind'] == 'sale' else opening[item['id']])
                current_name,current_phone=customer_display(
                    customer_context,row['customer'],row['customer_phone'])
                row['customer']=current_name
                row['customer_phone']=current_phone
                raw_phones.add(current_phone)
                row['calculation'] = item
                if item['kind'] == 'sale':
                    active_sale_ids.add(item['id'])
                    group_sales.append(row)
                    if item['initial_paid']:
                        history.append({'kind':'initial','id':item['id'],'sale_id':item['id'],
                            'date':row['date'],'amount':item['initial_paid'],'phone':row['customer_phone'],
                            'recipient':'sale','note':'Sotuvdagi boshlang‘ich to‘lov'})
                else:
                    group_opening.append(row)
            group_opening_ids = {r['id'] for r in group_opening}
            for receipt in opening_receipts:
                if receipt['opening_debt_id'] in group_opening_ids:
                    _,current_phone = customer_display(
                        customer_context,receipt['customer'],receipt['customer_phone'])
                    history.append({'kind':'receipt','id':receipt['id'],
                        'sale_id':None,'date':receipt['date'],
                        'amount':Decimal(str(receipt['amount'])),
                        'phone':current_phone,'recipient':receipt['received_by'],
                        'note':'Eski qarz bo‘yicha to‘lov #'+str(receipt['opening_debt_id'])})
            ids = {r['id'] for r in group_sales}
            for receipt in receipts:
                if receipt['sale_id'] in ids:
                    sale=sales[receipt['sale_id']]
                    _,current_phone=customer_display(
                        customer_context,sale['customer'],sale['customer_phone'])
                    history.append(dict(receipt,kind='receipt',phone=current_phone,
                        note='Keyingi qarz to‘lovi'))
            if totals['legacy_received']:
                history.extend(dict(legacy[payment_id],kind='legacy',sale_id=None,
                    phone='',recipient='legacy')
                    for payment_id in totals['legacy_payment_ids'])
            history.sort(key=lambda r:(_business_date(r['date'])[0],r['kind'],r['id']))
            groups.append({'totals':totals,'phones':sorted(raw_phones),'sales':group_sales,
                           'opening':group_opening,'history':history})
        if len(groups) > 1:
            financial_warnings.append({'code':'same_name_multiple_phones','customer':display_customer})
        exception_history = []
        for r in receipts:
            if r['sale_id'] not in active_sale_ids:
                sale=sales[r['sale_id']]
                _,current_phone=customer_display(
                    customer_context,sale['customer'],sale['customer_phone'])
                exception_history.append(dict(r,kind='receipt',phone=current_phone,
                    note='Faol sotuvga bog‘lanmagan keyingi to‘lov'))
        for sid, row in sales.items():
            if sid in matching_sale_ids and sid not in active_sale_ids and Decimal(str(row['paid'])):
                _,current_phone=customer_display(
                    customer_context,row['customer'],row['customer_phone'])
                exception_history.append({'kind':'initial','id':sid,'sale_id':sid,'date':row['date'],
                    'amount':Decimal(str(row['paid'])),'phone':current_phone,
                    'recipient':'sale','note':'Bekor qilingan sotuvdagi boshlang‘ich to‘lov'})
        customer_ids = (set(customer_context['name_to_ids'].get(customer, set()))
                        if requested_card is None else {requested_card['id']})
        relevant_unallocated = set()
        for row in financial['unallocated_legacy']:
            aliases = customer_context['name_to_ids'].get(row['customer'], set())
            if ((customer_ids and customer_ids.intersection(aliases))
                    or (not customer_ids and row['customer'] == customer)):
                relevant_unallocated.add(row['id'])
        unallocated_legacy = [legacy[r['id']] for r in financial['unallocated_legacy']
                              if r['id'] in relevant_unallocated]
    return {
        'customer': display_customer,
        'groups': groups,
        'financial_warnings': financial_warnings,
        'exception_history': exception_history,
        'unallocated_legacy': unallocated_legacy,
    }
