"""Read-only daily ledger validation inside the caller's write transaction.

No schema/data repair. A rejected operation rolls its entire transaction back,
including when a legacy handler catches ValueError inside a connection context.
"""
from collections import defaultdict
from bisect import bisect_left, bisect_right
from datetime import datetime
from decimal import Decimal, InvalidOperation

from product_catalog import BAGGED_FLOUR_PRODUCTS as FLOURS
ZERO = Decimal('0')
EPS = Decimal('0.000001')


def day(value):
    text = str(value or '')[:10]
    for fmt in ('%Y-%m-%d', '%d-%m-%Y'):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    raise ValueError('Zaxira yozuvidagi sana yaroqsiz.')


def number(value):
    try:
        result = Decimal(str(value or 0))
    except InvalidOperation as exc:
        raise ValueError('Zaxira yozuvidagi miqdor yaroqsiz.') from exc
    if not result.is_finite():
        raise ValueError('Zaxira yozuvidagi miqdor yaroqsiz.')
    return result


def validate_timeline(con, from_day, *, wheat=False, bunker=False, products=()):
    """Check every affected closing date, not just the submitted date/today.

    Opening balances follow product_stocks/bagged_product_stocks semantics.
    Same-day movements are netted; cancellation restores stock on its own date.
    Products/openings outside the affected scope are left alone.
    """
    if not con.in_transaction:
        raise ValueError('Zaxira tekshiruvi yozuv tranzaksiyasi ichida bajarilsin.')
    try:
        _validate(con, day(from_day), wheat, bunker, set(products))
    except (ValueError, ArithmeticError):
        con.rollback()
        raise


def _validate(con, start_day, wheat, bunker, products):
    def rows(table):
        exists = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists:
            return []
        cur = con.execute('SELECT * FROM "' + table + '"')
        names = [d[0] for d in cur.description]
        return [dict(zip(names, tuple(r))) for r in cur]

    events = defaultdict(lambda: defaultdict(lambda: ZERO))
    dates = {start_day}

    def add(key, date_value, quantity):
        d = day(date_value)
        events[key][d] += number(quantity)
        dates.add(d)

    production = {r['id']: r for r in rows('production')}
    if wheat:
        for r in rows('wheat_receipts'):
            add('wheat', r['date'], r['net'])
        rop_ids = {r['production_id'] for r in rows('rop_production')}
        for r in production.values():
            if r['id'] not in rop_ids:
                raw = r['raw_wheat_kg'] if r.get('raw_wheat_kg') is not None else r['wheat_kg']
                add('wheat', r['date'], -number(raw))
        for r in rows('rop_receipts'):
            if r['from_warehouse']:
                add('wheat', r['day'], -number(r['qty_kg']))
    if bunker:
        for r in rows('bunker_openings'):
            add('bunker', r['stock_date'], r['qty_kg'])
        for r in rows('bulk_flour_batches'):
            if r['production_id'] in production:
                add('bunker', production[r['production_id']]['date'], r['qty_kg'])
        for r in rows('flour_packaging'):
            add('bunker', r['date'], -number(r['withdrawn_kg']))

    loose_openings = {}
    bag_openings = {}
    if products:
        for r in rows('production_flour_types'):
            if r['product'] in products:
                add(('kg', r['product']), production[r['production_id']]['date'], r['qty_kg'])
        for r in production.values():
            for p, col in (('Kepak', 'bran_kg'), ('Muchka', 'muchka_kg'), ('Telashka atxod', 'telashka_kg')):
                if p in products:
                    add(('kg', p), r['date'], r.get(col))
        for r in rows('bagged_production_lines'):
            if r['product'] in products and r['product'] in FLOURS:
                add(('bags', r['product'], int(r['bag_kg'])), production[r['production_id']]['date'], r['bag_count'])
        packaging = {r['id']: r for r in rows('flour_packaging')}
        for r in rows('flour_packaging_lines'):
            p = r['product']
            if p in products:
                d = packaging[r['packaging_id']]['date']
                add(('kg', p), d, number(r['bag_kg']) * number(r['bag_count']))
                add(('bags', p, int(r['bag_kg'])), d, r['bag_count'])
        cancels = {r['sale_id']: r for r in rows('sale_cancellations')}
        deductions = {r['sale_id']: r for r in rows('bagged_sale_deductions')}
        for r in rows('sales'):
            p = r['product']
            if p not in products:
                continue
            sold_day = day(r['date'])
            cancellation = cancels.get(r['id'])
            # Legacy undated cancellations have always been inactive.
            if cancellation and not cancellation.get('cancelled_at'):
                continue
            cancelled_day = day(cancellation['cancelled_at']) if cancellation else None
            if cancelled_day and cancelled_day <= sold_day:
                continue
            add(('kg', p), sold_day, -number(r['qty_kg']))
            deduction = deductions.get(r['id'])
            if deduction is None and r.get('sale_mode') == 'qop' and p in FLOURS:
                size, count = number(r.get('bag_kg')), number(r.get('bag_count'))
                if size in (25, 50) and count > 0 and count == int(count) and number(r['qty_kg']) == size * count:
                    deduction = {'bag_kg': int(size), 'bag_count': int(count)}
            if deduction:
                add(('bags', p, int(deduction['bag_kg'])), sold_day, -number(deduction['bag_count']))
            if cancelled_day:
                add(('kg', p), cancelled_day, r['qty_kg'])
                if deduction:
                    add(('bags', p, int(deduction['bag_kg'])), cancelled_day, deduction['bag_count'])
        for r in rows('daily_finished_stock_receipts'):
            if r['product'] in products:
                d = r['receipt_date']
                add(('kg', r['product']), d, r['qty_kg'])
                if r['product'] in FLOURS:
                    add(('bags', r['product'],25),d,r['bag_25_count'])
                    add(('bags', r['product'],50),d,r['bag_50_count'])
        for r in rows('finished_stock_adjustments'):
            if r['product'] in products:
                d=r['adjustment_date']
                add(('kg',r['product']),d,r['qty_delta_kg'])
                if r['product'] in FLOURS:
                    add(('bags',r['product'],25),d,r['bag_25_delta'])
                    add(('bags',r['product'],50),d,r['bag_50_delta'])
        for r in rows('opening_product_stocks'):
            if r['product'] in products:
                loose_openings[r['product']] = (day(r['stock_date']), number(r['qty_kg']))
                dates.add(day(r['stock_date']))
        for r in rows('opening_bagged_product_stocks'):
            if r['product'] in products:
                bag_openings[r['product']] = (day(r['stock_date']), number(r['bag_25_count']), number(r['bag_50_count']))
                dates.add(day(r['stock_date']))

    prefixes = {}
    for key, movements in events.items():
        keys = sorted(movements)
        sums = [ZERO]
        for d in keys:
            sums.append(sums[-1] + movements[d])
        prefixes[key] = (keys, sums)

    def balance(key, cutoff, since=None):
        keys, sums = prefixes.get(key, ([], [ZERO]))
        return sums[bisect_right(keys, cutoff)] - sums[bisect_left(keys, since) if since else 0]

    def require(value, label, cutoff):
        if value < -EPS:
            raise ValueError(f'{label}: {cutoff} sanasida qoldiq manfiy ({value}). Sana yoki miqdorni tekshiring.')

    for cutoff in sorted(d for d in dates if d >= start_day):
        if wheat:
            require(balance('wheat', cutoff), 'Bug‘doy', cutoff)
        if bunker:
            require(balance('bunker', cutoff), 'Bunker', cutoff)
        for p in products:
            loose = loose_openings.get(p)
            bags = bag_openings.get(p)
            loose = loose if loose and loose[0] <= cutoff else None
            bags = bags if bags and bags[0] <= cutoff else None
            if loose:
                quantity = loose[1] + (bags[1] * 25 + bags[2] * 50 if bags else ZERO)
                quantity += balance(('kg', p), cutoff, loose[0])
            elif bags:
                quantity = bags[1] * 25 + bags[2] * 50 + balance(('kg', p), cutoff, bags[0])
            else:
                quantity = balance(('kg', p), cutoff)
            require(quantity, p, cutoff)
            if p in FLOURS:
                for size, opening in ((25, bags[1] if bags else ZERO), (50, bags[2] if bags else ZERO)):
                    require(opening + balance(('bags', p, size), cutoff, bags[0] if bags else None), f'{p} {size} kg qop', cutoff)
