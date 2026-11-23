"""DonControl: accountant-only guarded editor for any ACTIVE single / batch invoice.

No import-time writes or migrations. The caller holds BEGIN IMMEDIATE and must
verify a signed, unchanged snapshot. Edited financial rows are committed together
and every change has a user_audit before/after record. Invoice ID/number do not move.
"""
import json
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from datetime import datetime

PAYMENT_METHODS = ('naqd', 'plastik', 'hisob')


def money(value):
    try:
        d = Decimal(str(value))
        if not d.is_finite() or d < 0 or d.quantize(Decimal('.01')) != d:
            raise ValueError('Pul qiymati noto‘g‘ri yoki tiyindan kichik aniqlikda.')
        return int(d * 100)
    except (InvalidOperation, TypeError, OverflowError) as exc:
        raise ValueError('Pul qiymati noto‘g‘ri.') from exc


def sale_total(sale):
    return int((Decimal(str(sale['qty_kg'])) * Decimal(str(sale['price_per_kg'])) * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def iso_day(value):
    try:
        if len(value) != 10: raise ValueError()
        for fmt in ('%d-%m-%Y', '%Y-%m-%d'):
            try: return datetime.strptime(value, fmt).date()
            except ValueError: pass
    except (AttributeError, TypeError): pass
    raise ValueError('Sana noto‘g‘ri.')


def load(con, invoice_id):
    invoice = con.execute('SELECT * FROM invoices WHERE id=?', (invoice_id,)).fetchone()
    if not invoice: raise ValueError('Nakladnoy topilmadi.')
    anchor = con.execute('SELECT * FROM sales WHERE id=?', (invoice['sale_id'],)).fetchone()
    if not anchor: raise ValueError('Nakladnoyga bog‘langan sotuv topilmadi.')
    link = con.execute('SELECT batch_id FROM worker_sale_batch_items WHERE sale_id=?', (anchor['id'],)).fetchone()
    batch = None
    payments = []
    if link:
        batch = con.execute('SELECT * FROM worker_sale_batches WHERE id=?', (link['batch_id'],)).fetchone()
        if not batch: raise ValueError('Nakladnoy batch yozuvi topilmadi.')
        sales = con.execute('''SELECT s.* FROM worker_sale_batch_items bi
                     JOIN sales s ON s.id=bi.sale_id WHERE bi.batch_id=? ORDER BY s.id''', (batch['id'],)).fetchall()
        payments = con.execute('SELECT * FROM worker_sale_batch_payments WHERE batch_id=? ORDER BY id', (batch['id'],)).fetchall()
        if not any(s['id'] == anchor['id'] for s in sales): raise ValueError('Nakladnoy batch bog‘lanishi buzilgan.')
        if int(batch['worker_id']) != int(anchor['user_id']): raise ValueError('Xodim bog‘lanishi mos emas.')
        if any(s['user_id'] != anchor['user_id'] for s in sales): raise ValueError('Batchda turli xodimlar bor.')
        if sum(sale_total(s) for s in sales) != money(batch['total']):
            raise ValueError('Batch savdo jami va mahsulotlar jami farqli.')
        if sum(money(p['amount']) for p in payments) != money(batch['paid']):
            raise ValueError('Batch va kanal to‘lovlari farqli.')
        if sum(money(s['paid']) for s in sales) != money(batch['paid']):
            raise ValueError('Batch va mahsulotga taqsimlangan to‘lovlar farqli.')
        if any(s['customer'] != batch['customer'] or s['customer_phone'] != batch['customer_phone'] for s in sales):
            raise ValueError('Batch va mahsulot xaridorlari mos emas.')
    else:
        sales = [anchor]
        if con.execute('SELECT 1 FROM worker_sale_batch_items WHERE sale_id=?', (anchor['id'],)).fetchone():
            raise ValueError('Batch bog‘lanishi xato.')
    if any(con.execute('SELECT 1 FROM sale_cancellations WHERE sale_id=?', (s['id'],)).fetchone() for s in sales):
        raise ValueError('Bekor qilingan nakladnoy arxiv tarixidir; uni to‘g‘ridan-to‘g‘ri tahrirlab bo‘lmaydi.')
    if any(iso_day(s['date']).strftime('%Y-%m') != invoice['month_key'] for s in sales):
        raise ValueError('Sotuv oyi va nakladnoy oyi mos emas.')
    if iso_day(invoice['issued_date']).strftime('%Y-%m') != invoice['month_key']:
        raise ValueError('Nakladnoy oyi noto‘g‘ri.')
    receipts = {}
    for s in sales:
        rs = con.execute('SELECT * FROM worker_receipts WHERE sale_id=? ORDER BY id', (s['id'],)).fetchall()
        receipts[str(s['id'])] = [dict(r) for r in rs]
    return {'invoice':dict(invoice), 'anchor':dict(anchor),
            'batch':dict(batch) if batch else None,
            'sales':[dict(s) for s in sales], 'payments':[dict(p) for p in payments],
            'receipts': receipts}


def fingerprint(ctx):
    # Includes linked later payments to reject stale forms (snapshot signed by app).
    return ctx


def old_channels(ctx):
    if ctx['batch']:
        result = {m:0 for m in PAYMENT_METHODS}
        for p in ctx['payments']:
            if p['payment_method'] not in result: raise ValueError('Noma’lum to‘lov kanali.')
            result[p['payment_method']] += money(p['amount'])
        return result
    s=ctx['sales'][0]
    result = {m:0 for m in PAYMENT_METHODS}
    if money(s['paid']):
        if s['payment_method'] not in result: raise ValueError('Noma’lum to‘lov kanali.')
        result[s['payment_method']] = money(s['paid'])
    return result


def validate_changes(ctx, form, *, products, bag_options, loose_products, worker_products):
    original = ctx['sales']
    worker = int(ctx['anchor']['user_id'])
    role = 'umid' if worker == 3 else 'maxmud' if worker == 4 else None
    if not role: raise ValueError('Nakladnoy xodimi aniqlanmadi.')
    raw = form.get('items_json', '')
    try: entries = json.loads(raw)
    except (ValueError, TypeError) as exc: raise ValueError('Mahsulotlar ro‘yxati noto‘g‘ri.') from exc
    if not isinstance(entries, list) or len(entries) != len(original):
        raise ValueError('Mahsulotlar sonini bu oynada o‘zgartirish mumkin emas.')
    by_id={int(s['id']):s for s in original}
    if len(by_id)!=len(original):raise ValueError('Takror sotuv ID topildi.')
    if {int(x['sale_id']) for x in entries} != set(by_id):
        raise ValueError('Boshqa sotuvni tahrirlashga urinish bloklandi.')
    date_iso = form.get('date','').strip()
    sale_day = iso_day(date_iso)
    if sale_day.strftime('%Y-%m') != ctx['invoice']['month_key']:
        raise ValueError('Raqamni saqlash uchun sana shu oy ichida bo‘lishi kerak.')
    new_date = sale_day.strftime('%d-%m-%Y')
    customer = form.get('customer','').strip()
    phone = form.get('customer_phone','').strip()
    reason = form.get('reason','').strip()
    if not customer or (not phone and (customer!=original[0]['customer'] or phone!=original[0]['customer_phone'])):
        raise ValueError('Xaridor nomi va telefon kerak.')
    if len(reason)<8:raise ValueError('Tahrirlash sababini kamida 8 belgi bilan yozing.')
    out=[]
    for entry in entries:
        s=by_id[int(entry['sale_id'])]
        product=str(entry.get('product',''))
        mode=str(entry.get('mode',''))
        if product not in products or product not in worker_products[role]:
            raise ValueError('Mahsulot shu xodimning yo‘nalishida bo‘lishi kerak.')
        if mode=='qop':
            bag=Decimal(str(entry.get('bag_kg','')))
            count=Decimal(str(entry.get('bag_count','')))
            if float(bag) not in bag_options[product] or count<=0 or count!=count.to_integral_value():
                raise ValueError('Qop hajmi yoki soni noto‘g‘ri.')
            qty=bag*count; units=count
        elif mode=='kg' and product in loose_products:
            bag=None;count=None
            qty=Decimal(str(entry.get('qty_kg','')))
            units=qty
        else:raise ValueError('Mahsulot uchun savdo usuli noto‘g‘ri.')
        price=Decimal(str(entry.get('unit_price','')))
        if not qty.is_finite() or not price.is_finite() or qty<=0 or price<=0:
            raise ValueError('Mahsulot miqdori/narxi musbat bo‘lishi kerak.')
        if qty.as_tuple().exponent < -2 or price.as_tuple().exponent < -18:
            raise ValueError('Miqdor yoki narx aniqligi juda katta.')
        total=(price*units).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
        if total<=0:raise ValueError('Mahsulot jami musbat bo‘lishi kerak.')
        out.append({'id':s['id'], 'product':product,'mode':mode,'bag_kg':float(bag) if bag is not None else None,
                    'bag_count':int(count) if count is not None else None, 'qty_kg':float(qty),
                    'unit_price':float(price), 'total_cents':money(total),
                    'linked_cents':sum(money(x['amount']) for x in ctx['receipts'][str(s['id'])]), 'note':str(entry.get('note','')).strip()})
    total_cents=sum(x['total_cents'] for x in out)
    old_paid=old_channels(ctx)
    if ctx['batch']:
        new_paid={m:money(form.get('pay_'+m,0) or 0) for m in PAYMENT_METHODS}
        recipient=int(ctx['anchor']['user_id'])
    else:
        new_paid={m:0 for m in PAYMENT_METHODS}
        method=form.get('payment_method','naqd')
        if method not in PAYMENT_METHODS:raise ValueError('To‘lov usuli noto‘g‘ri.')
        new_paid[method]=money(form.get('paid',0) or 0)
        # Preserve original recipient unless accountant explicitly corrects it.
        recipient_str=form.get('recipient_id','').strip()
        recipient=int(recipient_str) if recipient_str else (ctx['sales'][0].get('initial_payment_recipient_id') or worker)
    received=sum(new_paid.values())
    if received>total_cents:raise ValueError('Qabul qilingan pul sotuvdan katta bo‘lishi mumkin emas.')
    allocated=[];left=received
    for x in out:
        take=min(x['total_cents'],left);left-=take;allocated.append(take)
        if take+x['linked_cents']>x['total_cents']:
            raise ValueError('Tuzatish keyingi real to‘lovdan kam qarz hosil qildi; pulni qaytarish/avans hisobini alohida rasmiylashtiring.')
    if left:raise ValueError('To‘lov taqsimoti xatosi.')
    old_customer=original[0]['customer'];old_phone=original[0]['customer_phone']
    return {'date':new_date,'customer':customer,'phone':phone,'reason':reason,
            'lines':out,'allocated':allocated,'total_cents':total_cents,
            'old_channels':old_paid,'new_channels':new_paid,'recipient':recipient,
            'identity_changed':customer!=old_customer or phone!=old_phone}


def apply(con,ctx,proposal,*,actor, product_stocks,bagged_product_stocks,
          record_bagged_sale,validate_timeline,worker_channel_balances, invoice_groups):
    # DB preconditions: caller has BEGIN IMMEDIATE, signed snapshot compared to fresh load.
    inv=ctx['invoice']; sales=ctx['sales']; batch=ctx['batch']; new=proposal
    seller=ctx['anchor']['user_id']
    if new['identity_changed']:
        if any(ctx['receipts'][str(s['id'])] for s in sales):
            raise ValueError('Keyingi to‘lovlari bor nakladnoy xaridorini o‘zgartirish to‘lov kartalarini buzishi mumkin. Xaridor bog‘lanishini alohida to‘g‘rilash kerak.')
        old_name=sales[0]['customer'];new_name=new['customer']
        # Name-only historical payments could be silently moved by changing identity.
        names={old_name,new_name}
        for n in names:
            if con.execute('SELECT 1 FROM payments WHERE customer=? LIMIT 1',(n,)).fetchone():
                raise ValueError('Xaridorning eski bog‘lanmagan to‘lovlari bor. Ismni o‘zgartirishdan avval to‘lovlarni bog‘lash zarur.')
        from customer_helpers import phone_key
        card=con.execute('SELECT name,phone FROM customers WHERE phone_key=?', (phone_key(new['phone']),)).fetchone()
        if card is None or card['name'] != new_name:
            raise ValueError('Yangi xaridor/telefon mavjud xaridor kartasiga mos kelishi kerak.')
    # If altering non-batch payment recipient, verify accountable user.
    if not batch:
        user=con.execute('SELECT id,role FROM users WHERE id=?',(new['recipient'],)).fetchone()
        if not user or user['role'] not in ('buxgalter','umid','maxmud'):
            raise ValueError('Pulni qabul qiluvchi hisob topilmadi.')
        if user['role'] in ('umid','maxmud') and user['id']!=seller:
            raise ValueError('Pul boshqa xodimga biriktirib bo‘lmaydi.')
    if not batch:
        old_recipient=sales[0].get('initial_payment_recipient_id') or seller
        if old_recipient != seller and any(proposal['new_channels'][m] < proposal['old_channels'][m] for m in PAYMENT_METHODS):
            raise ValueError('Buxgalter qabul qilgan pulni kamaytirish uchun buxgalter kassasi bo‘yicha alohida nazorat zarur.')
    # Note old value of every cash channel and every sale row in immutable audit.
    before={'invoice':inv,'batch':batch,'sales':sales,'payments':ctx['payments']}
    original_balances={m:float(worker_channel_balances(con,seller).get(m,0)) for m in PAYMENT_METHODS}
    changed_inventory=any(
        x['product']!=s['product'] or x['qty_kg']!=s['qty_kg']
        or x['mode']!=s['sale_mode'] or (x['bag_kg'] or 0)!=(s['bag_kg'] or 0)
        or (x['bag_count'] or 0)!=(s['bag_count'] or 0)
        or iso_day(s['date']) != iso_day(new['date'])
        for x,s in zip(new['lines'], sales))
    original_stock=product_stocks(con) if changed_inventory else {}
    original_bags=bagged_product_stocks(con) if changed_inventory else {}
    current_money=proposal['new_channels']
    previous_money=proposal['old_channels']
    con.execute('SAVEPOINT dc_universal_edit')
    try:
        # Batch & nonbatch money bookkeeping are distinct. Never append a duplicate receipt.
        if batch:
            found={}
            for p in ctx['payments']:
                if p['payment_method'] in found:
                    raise ValueError('Bir xil kanal uchun bir nechta batch to‘lovi bor; qo‘lda audit talab qilinadi.')
                found[p['payment_method']]=p
            for method in PAYMENT_METHODS:
                amount=current_money[method]/100
                if method in found:
                    if current_money[method]:
                        con.execute('UPDATE worker_sale_batch_payments SET amount=? WHERE id=?',(amount, found[method]['id']))
                    else:
                        con.execute('DELETE FROM worker_sale_batch_payments WHERE id=?',(found[method]['id'],))
                elif current_money[method]:
                    # Preserve original sale-period accounting, not the correction timestamp.
                    con.execute('INSERT INTO worker_sale_batch_payments(batch_id,payment_method,amount,created_at) VALUES(?,?,?,?)',
                                (batch['id'],method,amount,batch['created_at']))
            con.execute('''UPDATE worker_sale_batches SET customer=?,customer_phone=?,sale_date=?,total=?,paid=? WHERE id=?''',
                       (new['customer'],new['phone'],new['date'],new['total_cents']/100,sum(current_money.values())/100,batch['id']))
        else:
            # Nonbatch sales have only one initial payment method.
            pass
        orig_by_id={x['id']:x for x in sales}
        for i,x in enumerate(new['lines']):
            old=orig_by_id[x['id']]
            due=old['due_date'] if old['due_date'] else ''
            remaining=x['total_cents']-new['allocated'][i]-x['linked_cents']
            if remaining>0 and not due:
                dt=iso_day(new['date'])
                from datetime import timedelta
                due=(dt+timedelta(days=25)).strftime('%d-%m-%Y')
            if remaining<=0: due=''
            if batch:
                pm='naqd'  # Existing batch convention; actual channels are stored in batch payments.
                receiver=None
            else:
                pm=next((m for m in PAYMENT_METHODS if current_money[m]),old['payment_method'])
                receiver=new['recipient'] if new['allocated'][i] else None
            con.execute('''UPDATE sales SET date=?, customer=?,customer_phone=?,due_date=?,product=?,
                  qty_kg=?,price_per_kg=?,paid=?,note=?,sale_mode=?,bag_kg=?,bag_count=?,
                  unit_price=?,payment_method=?,initial_payment_recipient_id=? WHERE id=?''',
                  (new['date'],new['customer'],new['phone'],due,x['product'],x['qty_kg'],
                   (x['total_cents']/100)/x['qty_kg'],new['allocated'][i]/100,x['note'],
                   x['mode'],x['bag_kg'],x['bag_count'],x['unit_price'],pm,receiver,x['id']))
            if changed_inventory:
                record_bagged_sale(con,x['id'],x['product'],x['mode'],x['bag_kg'],x['bag_count'])
            con.execute('''INSERT INTO sale_edit_versions(sale_id,version) VALUES(?,1)
                ON CONFLICT(sale_id) DO UPDATE SET version=version+1''',(x['id'],))
        # Keep printed invoice date inside the original month. Its immutable ID and visible M-/U-serial stay stable.
        cat=invoice_groups.get(new['lines'][0]['product'], ('boshqa','Boshqa'))[0]
        if cat!=inv['category']:
            conflicting=con.execute('''SELECT 1 FROM invoices WHERE category=? AND month_key=? AND number=? AND id<>?''',
                                    (cat,inv['month_key'],inv['number'],inv['id'])).fetchone()
            if conflicting:
                raise ValueError('Mahsulot turini bu nakladnoyga almashtirish raqam to‘qnashuviga olib keladi.')
        con.execute('UPDATE invoices SET issued_date=?,category=? WHERE id=?', (new['date'],cat,inv['id']))
        # Price/identity-only corrections must not rewrite or revalidate historical stock.
        if changed_inventory:
            products={s['product'] for s in sales}|{x['product'] for x in new['lines']}
            stock=product_stocks(con)
            bag_stocks=bagged_product_stocks(con)
            for product in products:
                before_stock=float(original_stock.get(product,0))
                after_stock=float(stock.get(product,0))
                if after_stock < min(0.0,before_stock)-0.000001:
                    raise ValueError(product+' ombor qoldig‘i yomonlashdi; o‘zgartirish saqlanmadi.')
                if product in bag_stocks:
                    for size,qty in bag_stocks[product].items():
                        old_qty=original_bags.get(product,{}).get(size,0)
                        if qty<min(0,old_qty):
                            raise ValueError(product+' qop qoldig‘i yomonlashdi.')
                earliest=min([iso_day(s['date']) for s in sales]+[iso_day(new['date'])]).strftime('%d-%m-%Y')
                validate_timeline(con,earliest,products=(product,))
        # Comparing actual balances catches accounting impact of correcting the initial payment.
        new_balances=worker_channel_balances(con,seller)
        for method in PAYMENT_METHODS:
            before_balance=original_balances[method]
            after_balance=float(new_balances.get(method,0))
            if after_balance < -0.009 and after_balance < before_balance-0.009:
                raise ValueError(method+' kassada yetarli mablag‘ yo‘q; to‘lov tuzatishi qaytarildi.')
        after=load(con, inv['id'])
        if after['invoice']['id']!=inv['id'] or after['invoice']['number']!=inv['number'] or after['invoice']['month_key']!=inv['month_key']:
            raise ValueError('Nakladnoy raqami yoki oyi o‘zgarib ketdi.')
        if batch:
            if money(after['batch']['total'])!=new['total_cents'] or money(after['batch']['paid'])!=sum(current_money.values()):
                raise ValueError('Batch qayta tekshiruv xatosi.')
        audit={'reason':new['reason'], 'actor':actor, 'invoice_id':inv['id'],
               'batch_id':batch['id'] if batch else None,'before':before,
               'after':{'invoice':after['invoice'],'batch':after['batch'],'sales':after['sales'], 'payments':after['payments']},
               'old_channels_cents':previous_money, 'new_channels_cents':current_money,
               'before_balances':original_balances,'after_balances':new_balances}
        con.execute('INSERT INTO user_audit(actor_user_id,target_user_id,action,details) VALUES(?,?,?,?)',
                    (actor,seller,'edit_invoice_accountant',json.dumps(audit,ensure_ascii=False,sort_keys=True)))
        con.execute('RELEASE SAVEPOINT dc_universal_edit')
        return audit
    except Exception:
        con.execute('ROLLBACK TO SAVEPOINT dc_universal_edit')
        con.execute('RELEASE SAVEPOINT dc_universal_edit')
        raise
