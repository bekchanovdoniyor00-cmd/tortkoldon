"""DonControl: a separate flour-bunker and bagging ledger, no Flask dependencies.

Existing historic production/bagging records are NOT migrated or rewritten. All
operations on this service must be executed in a caller-owned BEGIN IMMEDIATE.
"""
from decimal import Decimal, InvalidOperation

from product_catalog import BAGGED_FLOUR_PRODUCTS
from temporal_stock import validate_timeline

def amount(value, label, positive=False, *, multiplier=1):
    try:
        n = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f'{label}: raqamni tekshiring.') from exc
    if not n.is_finite() or n < 0 or n > Decimal('1000000000'):
        raise ValueError(f'{label}: qiymat noto‘g‘ri.')
    if positive and n == 0:
        raise ValueError(f'{label}: musbat qiymat kiriting.')
    if n.as_tuple().exponent < -3:
        raise ValueError(f'{label}: ko‘pi bilan 0.001 aniqlikda kiriting.')
    kilograms = n * multiplier
    if kilograms > Decimal('1000000000'):
        raise ValueError(f'{label}: kilogrammdagi qiymat chegaradan oshdi.')
    return float(kilograms)


def ensure_schema(con):
    con.execute('''CREATE TABLE IF NOT EXISTS bulk_flour_batches (
        production_id INTEGER PRIMARY KEY REFERENCES production(id),
        qty_kg REAL NOT NULL CHECK(qty_kg>0))''')
    con.execute('''CREATE TABLE IF NOT EXISTS bunker_openings (
        id INTEGER PRIMARY KEY CHECK(id=1), qty_kg REAL NOT NULL CHECK(qty_kg>=0),
        stock_date TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', user_id INTEGER NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
    con.execute('''CREATE TABLE IF NOT EXISTS flour_packaging (
        id INTEGER PRIMARY KEY, date TEXT NOT NULL, withdrawn_kg REAL NOT NULL CHECK(withdrawn_kg>0),
        packaged_kg REAL NOT NULL CHECK(packaged_kg>0),
        loss_kg REAL NOT NULL CHECK(loss_kg>=0),
        token TEXT NOT NULL UNIQUE, user_id INTEGER NOT NULL, payload TEXT NOT NULL,
        note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
    con.execute('''CREATE TABLE IF NOT EXISTS flour_packaging_lines (
        packaging_id INTEGER NOT NULL REFERENCES flour_packaging(id),
        product TEXT NOT NULL, bag_kg INTEGER NOT NULL CHECK(bag_kg IN (25,50)),
        bag_count INTEGER NOT NULL CHECK(bag_count>0),
        PRIMARY KEY(packaging_id, product, bag_kg))''')


def bunker_balance(con):
    opening = con.execute('SELECT COALESCE(SUM(qty_kg),0) FROM bunker_openings').fetchone()[0]
    made = con.execute('SELECT COALESCE(SUM(qty_kg),0) FROM bulk_flour_batches').fetchone()[0]
    used = con.execute('SELECT COALESCE(SUM(withdrawn_kg),0) FROM flour_packaging').fetchone()[0]
    return round(opening + made - used, 3)


def save_opening(con, qty_kg, stock_date, user_id, note):
    qty = amount(qty_kg, 'Boshlang‘ich bunker qoldig‘i')
    if con.execute('SELECT 1 FROM bunker_openings LIMIT 1').fetchone():
        raise ValueError('Bunker boshlang‘ich qoldig‘i faqat bir marta kiritiladi.')
    if con.execute('SELECT 1 FROM flour_packaging LIMIT 1').fetchone():
        raise ValueError('Qoplash boshlanganidan keyin bunker ochilish qoldig‘ini kiritish mumkin emas.')
    con.execute('INSERT INTO bunker_openings(id,qty_kg,stock_date,note,user_id) VALUES(1,?,?,?,?)',
                (qty, stock_date, note, user_id))


def save_production(con, date_value, clean, bulk, black, bran, muchka, telashka,
                    note, user_id, period_id):
    cur = con.execute('''INSERT INTO production
        (date,wheat_kg,flour_kg,bran_kg,muchka_kg,telashka_kg,note,user_id)
        VALUES(?,?,?,?,?,?,?,?)''',
        (date_value,clean,bulk+black,bran,muchka,telashka,note,user_id))
    pid = cur.lastrowid
    if black:
        con.execute('INSERT INTO production_flour_types(production_id,product,qty_kg) VALUES(?,?,?)',
                    (pid,'Qora un',black))
    if bulk:
        con.execute('INSERT INTO bulk_flour_batches(production_id,qty_kg) VALUES(?,?)',(pid,bulk))
    con.execute('INSERT INTO rop_production(production_id,period_id) VALUES(?,?)', (pid,period_id))
    return pid


def validate_lines(products, sizes, counts):
    if not (len(products)==len(sizes)==len(counts)) or not products:
        raise ValueError('Qoplash qatorlari to‘liq emas.')
    lines = {}
    for product, size_string, count_string in zip(products,sizes,counts):
        if product not in BAGGED_FLOUR_PRODUCTS:
            raise ValueError('Mahsulot noto‘g‘ri.')
        try:
            size = int(size_string)
            count = int(count_string)
        except (ValueError,TypeError,OverflowError) as exc:
            raise ValueError('Qop og‘irligi va soni butun son bo‘lishi kerak.') from exc
        if size not in (25,50) or str(size)!=str(size_string).strip() or count<=0 or str(count)!=str(count_string).strip():
            raise ValueError('Qop og‘irligi 25 yoki 50 kg, soni musbat butun bo‘lsin.')
        key=(product,size)
        lines[key]=lines.get(key,0)+count
    return lines


def save_packaging(con,date_value,withdrawn_kg,products,sizes,counts,token,payload,user_id,note,
                   *, pending_reconciliation=False):
    withdrawn=amount(withdrawn_kg,'Bunkerdan olingan un',positive=True)
    lines=validate_lines(products,sizes,counts)
    packaged=sum(size*count for (_,size),count in lines.items())
    if packaged>withdrawn:
        raise ValueError('Qoplangan un bunkerdan olingan undan ko‘p bo‘lmaydi.')
    previous=con.execute('SELECT * FROM flour_packaging WHERE token=?',(token,)).fetchone()
    if previous:
        if previous['user_id']!=user_id or previous['payload']!=payload:
            raise ValueError('Takroriy tokenning ma’lumotlari mos kelmaydi.')
        return previous['id'],False
    if withdrawn>bunker_balance(con):
        raise ValueError('Bunkerda yetarli un yo‘q.')
    loss = 0 if pending_reconciliation else round(withdrawn-packaged,3)
    cur=con.execute('''INSERT INTO flour_packaging
      (date,withdrawn_kg,packaged_kg,loss_kg,token,user_id,payload,note)
      VALUES(?,?,?,?,?,?,?,?)''',
      (date_value,withdrawn,packaged,loss,token,user_id,payload,note))
    for (product,size),count in lines.items():
        con.execute('INSERT INTO flour_packaging_lines(packaging_id,product,bag_kg,bag_count) VALUES(?,?,?,?)',
                    (cur.lastrowid,product,size,count))
    validate_timeline(con, date_value, bunker=True)
    return cur.lastrowid,True
