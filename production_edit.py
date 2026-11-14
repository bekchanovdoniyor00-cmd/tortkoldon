"""Corrections to open-period ROP production, within the caller's write transaction."""
import json

from bulk_flour_service import bunker_balance
from business_dates import _business_date_key
from inventory_sales_helpers import product_stocks


PRODUCT_COLUMNS = {
    'Qora un': 'black_kg',
    'Kepak': 'bran_kg',
    'Muchka': 'muchka_kg',
    'Telashka atxod': 'telashka_kg',
}


def record(con, production_id):
    return con.execute('''SELECT p.id, p.date, p.wheat_kg, p.flour_kg, p.bran_kg,
        p.muchka_kg, p.telashka_kg, p.note, r.period_id, period.stop_day,
        COALESCE(b.qty_kg,0) AS bulk_kg, COALESCE(f.qty_kg,0) AS black_kg,
        (SELECT COALESCE(MAX(id),0) FROM rop_production_edits WHERE production_id=p.id) AS revision
        FROM rop_production r JOIN production p ON p.id=r.production_id
        JOIN rop_periods period ON period.id=r.period_id
        LEFT JOIN bulk_flour_batches b ON b.production_id=p.id
        LEFT JOIN production_flour_types f ON f.production_id=p.id AND f.product='Qora un'
        WHERE p.id=?''', (production_id,)).fetchone()


def values(row):
    return {key: row[key] for key in
            ('wheat_kg', 'bulk_kg', 'black_kg', 'bran_kg', 'muchka_kg', 'telashka_kg', 'note')}


def minimum_bunker(con, day):
    return con.execute('''WITH movements AS (
        SELECT business_date_key(stock_date) AS day, qty_kg AS qty FROM bunker_openings
        UNION ALL SELECT business_date_key(p.date), b.qty_kg
            FROM bulk_flour_batches b JOIN production p ON p.id=b.production_id
        UNION ALL SELECT business_date_key(date), -withdrawn_kg FROM flour_packaging
        UNION ALL SELECT ?, 0
    ), daily AS (
        SELECT day, SUM(qty) AS qty FROM movements GROUP BY day
    ), balances AS (
        SELECT day, SUM(qty) OVER (ORDER BY day) AS qty FROM daily
    ) SELECT MIN(qty) FROM balances WHERE day>=?''', (day, day)).fetchone()[0]


def minimum_product(con, product, day):
    # A dated opening resets the ledger. A cancellation restores stock from its own day.
    column = PRODUCT_COLUMNS[product]
    if column == 'black_kg':
        production = """SELECT business_date_key(p.date) AS day, f.qty_kg AS qty
            FROM production_flour_types f JOIN production p ON p.id=f.production_id
            WHERE f.product=:product
            UNION ALL SELECT business_date_key(p.date), l.bag_kg*l.bag_count
                FROM flour_packaging_lines l JOIN flour_packaging p ON p.id=l.packaging_id
                WHERE l.product=:product"""
    else:
        production = f'SELECT business_date_key(date) AS day, {column} AS qty FROM production WHERE {column}<>0'
    return con.execute(f'''WITH opening AS (
        SELECT business_date_key(stock_date) AS day, qty_kg AS qty
        FROM opening_product_stocks WHERE product=:product
    ), sales_days AS (
        SELECT s.qty_kg, business_date_key(s.date) AS day,
            CASE WHEN c.sale_id IS NOT NULL THEN business_date_key(SUBSTR(c.cancelled_at,1,10)) END AS cancelled
        FROM sales s LEFT JOIN sale_cancellations c ON c.sale_id=s.id WHERE s.product=:product
    ), movements AS (
        {production}
        UNION ALL SELECT day, -qty_kg FROM sales_days WHERE cancelled IS NULL OR cancelled>day
        UNION ALL SELECT cancelled, qty_kg FROM sales_days WHERE cancelled>day
            AND (day >= (SELECT day FROM opening) OR cancelled < (SELECT day FROM opening)
                 OR NOT EXISTS (SELECT 1 FROM opening))
        UNION ALL SELECT day, qty FROM opening
        UNION ALL SELECT :edit_day, 0
    ), daily AS (
        SELECT day, SUM(qty) AS qty FROM movements GROUP BY day
    ), balances AS (
        SELECT day, SUM(qty) OVER (
            PARTITION BY CASE WHEN day>=(SELECT day FROM opening) THEN 1 ELSE 0 END ORDER BY day
        ) AS qty FROM daily
    ) SELECT MIN(qty) FROM balances WHERE day>=:edit_day
        AND (NOT EXISTS (SELECT 1 FROM opening) OR :edit_day>=(SELECT day FROM opening)
             OR day<(SELECT day FROM opening))''', {'product': product, 'edit_day': day}).fetchone()[0]


def apply(con, row, new, reason, user_id):
    if row['stop_day'] is not None:
        raise ValueError('Tasdiqlangan zachiska davrida tahrirlash mumkin emas.')
    if not 1 <= len(reason) <= 500:
        raise ValueError('Tahrirlash sababini kiriting (1–500 belgi).')
    old = values(row)
    if old == new:
        raise ValueError('Tahrirlanadigan qiymat o‘zgarmagan.')
    production_id = row['id']
    if (round(row['flour_kg'] - old['bulk_kg'] - old['black_kg'], 3)
            or con.execute('''SELECT 1 FROM bagged_production_lines WHERE production_id=?
                UNION ALL SELECT 1 FROM production_flour_types WHERE production_id=? AND product<>'Qora un' ''',
                           (production_id, production_id)).fetchone()):
        raise ValueError('Yozuvdagi eski qoplash yoki un taqsimotini alohida tekshirish kerak.')

    con.execute('''UPDATE production SET wheat_kg=:wheat_kg, flour_kg=:bulk_kg+:black_kg,
        bran_kg=:bran_kg, muchka_kg=:muchka_kg, telashka_kg=:telashka_kg, note=:note WHERE id=:id''',
                dict(new, id=production_id))
    for table, column_names, placeholders, key, params in (
        ('bulk_flour_batches', 'production_id,qty_kg', '?,?', 'bulk_kg', (production_id,)),
        ('production_flour_types', 'production_id,product,qty_kg', '?,?,?', 'black_kg',
         (production_id, 'Qora un')),
    ):
        if new[key] != old[key]:
            if new[key]:
                con.execute(f'''INSERT INTO {table}({column_names}) VALUES({placeholders})
                    ON CONFLICT DO UPDATE SET qty_kg=excluded.qty_kg''', (*params, new[key]))
            else:
                condition = " AND product='Qora un'" if key == 'black_kg' else ''
                con.execute(f'DELETE FROM {table} WHERE production_id=?{condition}', (production_id,))

    day = _business_date_key(row['date'])
    if new['bulk_kg'] < old['bulk_kg']:
        if min(bunker_balance(con), minimum_bunker(con, day)) < -0.000001:
            raise ValueError('Tahrir qoplashdan keyingi bunker qoldig‘ini manfiy qiladi.')
    reduced = [product for product, key in PRODUCT_COLUMNS.items() if new[key] < old[key]]
    if reduced:
        stocks = product_stocks(con)
        for product in reduced:
            if min(stocks[product], minimum_product(con, product, day)) < -0.000001:
                raise ValueError(f'Tahrir {product} ombor qoldig‘ini manfiy qiladi.')
    return con.execute('''INSERT INTO rop_production_edits
        (production_id,user_id,reason,previous_values,new_values) VALUES(?,?,?,?,?)''',
        (production_id, user_id, reason, json.dumps(dict(old, date=row['date']), ensure_ascii=False),
         json.dumps(dict(new, date=row['date']), ensure_ascii=False))).lastrowid
