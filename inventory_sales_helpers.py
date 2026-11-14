"""Existing product stock and sale validators, moved verbatim from app.py.
No app import, Flask runtime or new business logic.
"""
import math
from business_dates import _business_date_key
from temporal_stock import validate_timeline
from product_catalog import (
    PRODUCTS, FLOUR_TYPES, BAGGED_FLOUR_PRODUCTS, BAG_OPTIONS, LOOSE_PRODUCTS,
)


def product_stocks(con):
    made = {p: 0.0 for p in PRODUCTS}
    for r in con.execute('SELECT product, SUM(qty_kg) AS qty FROM production_flour_types GROUP BY product'):
        if r['product'] in made: made[r['product']] += r['qty'] or 0
    # New bagging ledger: bunker stock is not directly available for sale.
    for r in con.execute("SELECT product,SUM(bag_kg*bag_count) qty FROM flour_packaging_lines GROUP BY product"):
        if r['product'] in made: made[r['product']] += (r['qty'] or 0)
    for col, product in [('bran_kg','Kepak'), ('muchka_kg','Muchka'), ('telashka_kg','Telashka atxod')]:
        made[product] += con.execute(f'SELECT COALESCE(SUM({col}),0) FROM production').fetchone()[0]
    # Eski sinov ma'lumotlari yo'qolmasligi uchun eski "Un" alohida qoladi.
    made['Un (eski yozuv)'] = (con.execute('SELECT COALESCE(SUM(flour_kg),0) FROM production').fetchone()[0] - sum(made[p] for p in FLOUR_TYPES) - con.execute('SELECT COALESCE(SUM(qty_kg),0) FROM bulk_flour_batches').fetchone()[0] + con.execute('SELECT COALESCE(SUM(bag_kg*bag_count),0) FROM flour_packaging_lines').fetchone()[0])
    for r in con.execute('SELECT product, SUM(qty_kg) AS qty FROM sales WHERE id NOT IN (SELECT sale_id FROM sale_cancellations) GROUP BY product'):
        key = 'Un (eski yozuv)' if r['product'] == 'Un' else r['product']
        made[key] = made.get(key, 0) - (r['qty'] or 0)
    if con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='opening_product_stocks'"
    ).fetchone():
        production_columns = {
            'Kepak': 'bran_kg',
            'Muchka': 'muchka_kg',
            'Telashka atxod': 'telashka_kg',
        }
        for opening in con.execute('SELECT product,stock_date,qty_kg FROM opening_product_stocks'):
            product = opening['product']
            opening_day = _business_date_key(opening['stock_date'])
            if product in FLOUR_TYPES:
                production_after_opening = con.execute("""
                    SELECT COALESCE(SUM(pt.qty_kg),0)
                    FROM production_flour_types pt
                    JOIN production p ON p.id=pt.production_id
                    WHERE pt.product=? AND business_date_key(p.date)>=?
                """, (product, opening_day)).fetchone()[0]
                production_after_opening += con.execute('''
                    SELECT COALESCE(SUM(l.bag_kg*l.bag_count),0)
                    FROM flour_packaging_lines l JOIN flour_packaging p ON p.id=l.packaging_id
                    WHERE l.product=? AND business_date_key(p.date)>=?
                ''',(product, opening_day)).fetchone()[0]
            else:
                column = production_columns.get(product)
                if column is None:
                    continue
                production_after_opening = con.execute(
                    f'SELECT COALESCE(SUM({column}),0) FROM production '
                    'WHERE business_date_key(date)>=?', (opening_day,)
                ).fetchone()[0]
            sold_after_opening = con.execute("""
                SELECT COALESCE(SUM(qty_kg),0) FROM sales
                WHERE product=? AND business_date_key(date)>=?
                AND id NOT IN (SELECT sale_id FROM sale_cancellations)
            """, (product, opening_day)).fetchone()[0]
            made[product] = opening['qty_kg'] + production_after_opening - sold_after_opening
    for opening in con.execute("""
            SELECT product,stock_date,bag_25_count,bag_50_count
            FROM opening_bagged_product_stocks"""):
        product = opening['product']
        opening_day = _business_date_key(opening['stock_date'])
        opening_kg = opening['bag_25_count'] * 25 + opening['bag_50_count'] * 50
        existing_opening = con.execute(
            'SELECT 1 FROM opening_product_stocks WHERE product=?',
            (product,)).fetchone()
        if existing_opening:
            made[product] += opening_kg
        else:
            production_after_opening = con.execute("""
                SELECT COALESCE(SUM(pt.qty_kg),0)
                FROM production_flour_types pt
                JOIN production p ON p.id=pt.production_id
                WHERE pt.product=? AND business_date_key(p.date)>=?
            """, (product, opening_day)).fetchone()[0]
            production_after_opening += con.execute('''
                SELECT COALESCE(SUM(l.bag_kg*l.bag_count),0)
                FROM flour_packaging_lines l JOIN flour_packaging p ON p.id=l.packaging_id
                WHERE l.product=? AND business_date_key(p.date)>=?
            ''',(product, opening_day)).fetchone()[0]
            sold_after_opening = con.execute("""
                SELECT COALESCE(SUM(qty_kg),0) FROM sales
                WHERE product=? AND business_date_key(date)>=?
                AND id NOT IN (SELECT sale_id FROM sale_cancellations)
            """, (product, opening_day)).fetchone()[0]
            made[product] = opening_kg + production_after_opening - sold_after_opening
    # Additive manual daily receipts, entered separately from ROP/production.
    # Openings remain immutable snapshots, never replaced by daily movements.
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='daily_finished_stock_receipts'").fetchone():
        for r in con.execute('SELECT product,SUM(qty_kg) qty FROM daily_finished_stock_receipts GROUP BY product'):
            if r['product'] in made:
                made[r['product']] += r['qty'] or 0
    # DC65 immutable inventory adjustments (signed). No historical row is rewritten.
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='finished_stock_adjustments'").fetchone():
        for r in con.execute('SELECT product,SUM(qty_delta_kg) AS qty FROM finished_stock_adjustments GROUP BY product'):
            if r['product'] in made:
                made[r['product']] += r['qty'] or 0
    return made

def bagged_product_stocks(con):
    """Return bag counts from explicit production, sale, and opening ledgers."""
    stocks = {product: {25: 0, 50: 0} for product in BAGGED_FLOUR_PRODUCTS}
    opening_dates = {}
    for row in con.execute("""
            SELECT product,stock_date,bag_25_count,bag_50_count
            FROM opening_bagged_product_stocks"""):
        if row['product'] not in stocks:
            continue
        opening_dates[row['product']] = _business_date_key(row['stock_date'])
        stocks[row['product']][25] += row['bag_25_count']
        stocks[row['product']][50] += row['bag_50_count']
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='daily_finished_stock_receipts'").fetchone():
        for r in con.execute('SELECT product,receipt_date,bag_25_count,bag_50_count FROM daily_finished_stock_receipts'):
            if r['product'] not in stocks:
                continue
            if r['product'] in opening_dates and _business_date_key(r['receipt_date']) <= opening_dates[r['product']]:
                continue
            stocks[r['product']][25] += r['bag_25_count']
            stocks[r['product']][50] += r['bag_50_count']
    # DC65: signed 25/50 kg bag adjustments are counted separately.
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='finished_stock_adjustments'").fetchone():
        for r in con.execute('SELECT product,adjustment_date,bag_25_delta,bag_50_delta FROM finished_stock_adjustments'):
            if r['product'] not in stocks:
                continue
            if r['product'] in opening_dates and _business_date_key(r['adjustment_date']) <= opening_dates[r['product']]:
                continue
            stocks[r['product']][25] += r['bag_25_delta']
            stocks[r['product']][50] += r['bag_50_delta']
    for row in con.execute("""
            SELECT b.product,b.bag_kg,b.bag_count,p.date
            FROM bagged_production_lines b
            JOIN production p ON p.id=b.production_id"""):
        if (row['product'] in stocks and row['bag_kg'] in stocks[row['product']]
                and (row['product'] not in opening_dates
                     or _business_date_key(row['date']) >= opening_dates[row['product']])):
            stocks[row['product']][row['bag_kg']] += row['bag_count']
    for row in con.execute('''
            SELECT l.product,l.bag_kg,l.bag_count,p.date
            FROM flour_packaging_lines l
            JOIN flour_packaging p ON p.id=l.packaging_id'''):
        if (row['product'] in stocks and row['bag_kg'] in stocks[row['product']]
                and (row['product'] not in opening_dates
                     or _business_date_key(row['date']) >= opening_dates[row['product']])):
            stocks[row['product']][row['bag_kg']] += row['bag_count']
    for row in con.execute("""
            SELECT d.product,d.bag_kg,d.bag_count,s.date
            FROM bagged_sale_deductions d
            JOIN sales s ON s.id=d.sale_id
            LEFT JOIN sale_cancellations c ON c.sale_id=s.id
            WHERE c.sale_id IS NULL"""):
        if (row['product'] in stocks and row['bag_kg'] in stocks[row['product']]
                and (row['product'] not in opening_dates
                     or _business_date_key(row['date']) >= opening_dates[row['product']])):
            stocks[row['product']][row['bag_kg']] -= row['bag_count']
    for row in con.execute("""
            SELECT s.product,s.bag_kg,s.bag_count,s.qty_kg,s.date
            FROM sales s
            LEFT JOIN bagged_sale_deductions d ON d.sale_id=s.id
            LEFT JOIN sale_cancellations c ON c.sale_id=s.id
            WHERE s.sale_mode='qop' AND d.sale_id IS NULL
              AND c.sale_id IS NULL"""):
        product = row['product']
        if product not in stocks:
            continue
        try:
            size = int(row['bag_kg'])
            count = int(row['bag_count'])
            quantity = float(row['qty_kg'])
        except (TypeError, ValueError, OverflowError):
            continue
        if (size not in stocks[product] or count <= 0
                or float(row['bag_kg']) != size
                or float(row['bag_count']) != count
                or quantity != size * count):
            continue
        if (product in opening_dates
                and _business_date_key(row['date']) < opening_dates[product]):
            continue
        stocks[product][size] -= count
    return stocks

def validate_bagged_sale(con, product, mode, bag_kg, bag_count,
                         exclude_sale_id=None):
    if product not in BAGGED_FLOUR_PRODUCTS or mode != 'qop':
        return
    try:
        size = int(bag_kg)
        count = int(bag_count)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError('Qop o‘lchami va sonini tekshiring.') from exc
    if size not in (25, 50) or float(bag_kg) != size:
        raise ValueError('Ushbu mahsulot uchun faqat 25 kg yoki 50 kg qop mavjud.')
    if count <= 0 or float(bag_count) != count:
        raise ValueError('Qop soni musbat butun son bo‘lishi kerak.')
    available = bagged_product_stocks(con)[product][size]
    if exclude_sale_id is not None:
        existing = con.execute(
            'SELECT product,bag_kg,bag_count FROM bagged_sale_deductions WHERE sale_id=?',
            (exclude_sale_id,)).fetchone()
        if existing and existing['product'] == product and existing['bag_kg'] == size:
            available += existing['bag_count']
    if count > available:
        raise ValueError(
            f'{product} uchun {size} kg qop yetarli emas. '
            'Eski kilogramm qoldiqlari qop o‘lchami bo‘yicha hisoblanmaydi.')

def record_bagged_sale(con, sale_id, product, mode, bag_kg, bag_count):
    con.execute('DELETE FROM bagged_sale_deductions WHERE sale_id=?', (sale_id,))
    if product in BAGGED_FLOUR_PRODUCTS and mode == 'qop':
        con.execute("""INSERT INTO bagged_sale_deductions
            (sale_id,product,bag_kg,bag_count) VALUES(?,?,?,?)""",
            (sale_id, product, int(bag_kg), int(bag_count)))
    sale_day = con.execute('SELECT date FROM sales WHERE id=?', (sale_id,)).fetchone()[0]
    validate_timeline(con, sale_day, products=(product,))


def product_stock_activity_exists(con, product, from_day):
    production_columns = {
        'Kepak': 'bran_kg',
        'Muchka': 'muchka_kg',
        'Telashka atxod': 'telashka_kg',
    }
    if product in FLOUR_TYPES:
        production = con.execute("""
            SELECT 1 FROM production_flour_types pt
            JOIN production p ON p.id=pt.production_id
            WHERE pt.product=? AND pt.qty_kg>0 AND business_date_key(p.date)>=?
            LIMIT 1
        """, (product, from_day)).fetchone()
    else:
        column = production_columns.get(product)
        production = con.execute(
            f'SELECT 1 FROM production WHERE COALESCE({column},0)>0 '
            'AND business_date_key(date)>=? LIMIT 1', (from_day,)
        ).fetchone() if column else None
    if production:
        return True
    if product in BAGGED_FLOUR_PRODUCTS and con.execute('''
        SELECT 1 FROM flour_packaging_lines l
        JOIN flour_packaging p ON p.id=l.packaging_id
        WHERE l.product=? AND business_date_key(p.date)>=? LIMIT 1
    ''',(product,from_day)).fetchone():
        return True
    return con.execute("""
        SELECT 1 FROM sales
        WHERE product=? AND business_date_key(date)>=?
        AND id NOT IN (SELECT sale_id FROM sale_cancellations)
        LIMIT 1
    """, (product, from_day)).fetchone() is not None

def sale_values(form):
    product = form['product']
    if product not in PRODUCTS: raise ValueError('Mahsulot noto‘g‘ri')
    mode = form.get('sale_mode', 'qop')
    if mode == 'qop':
        bag_kg = float(form['bag_kg']); count = float(form['bag_count']); unit_price = float(form['price'])
        if bag_kg not in BAG_OPTIONS[product] or count <= 0 or count != int(count): raise ValueError('Qop soni yoki hajmi noto‘g‘ri')
        qty = bag_kg * count; total = count * unit_price
    elif mode == 'kg' and product in LOOSE_PRODUCTS:
        bag_kg = None; count = None; qty = float(form['qty_kg']); unit_price = float(form['price']); total = qty * unit_price
    else: raise ValueError('Sotuv turi noto‘g‘ri')
    paid = float(form.get('paid') or 0)
    if not all(math.isfinite(x) for x in (qty,unit_price,total,paid)) or not (qty > 0 and unit_price >= 0 and 0 <= paid <= total): raise ValueError('Miqdor yoki narx noto‘g‘ri')
    return product, mode, bag_kg, count, unit_price, qty, total, paid
