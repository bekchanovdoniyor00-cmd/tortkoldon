"""Additive daily finished-stock receipts. No ROP/production mutations.

Migration is intentionally separate; never create tables on importing this module.
"""
import math
from datetime import date
from decimal import Decimal, InvalidOperation
from business_dates import _business_date, _business_date_key, _business_date_storage
from product_catalog import PRODUCTS, BAGGED_FLOUR_PRODUCTS
from temporal_stock import validate_timeline

MAX_KG_PER_ENTRY = 300_000


def _bags(value):
    value = str(value or '0').strip()
    if not value.isascii() or not value.isdecimal():
        raise ValueError('Qop soni manfiy bo‘lmagan butun son bo‘lsin.')
    result = int(value)
    if result > 12_000:
        raise ValueError('Bir kirimda 12 000 tadan ko‘p qop mumkin emas.')
    return result


def _kg(value):
    try:
        v = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError('Kilogramm miqdori noto‘g‘ri.') from exc
    if not v.is_finite() or v <= 0 or v > MAX_KG_PER_ENTRY or v != v.quantize(Decimal('0.01')):
        raise ValueError('Miqdorni 0.01 kg aniqlikda musbat kiriting (maksimum 300 000 kg).')
    return float(v)


def normalized_receipt(form, today=None):
    product = str(form.get('product','')).strip()
    if product not in PRODUCTS:
        raise ValueError('Mahsulotni tanlang.')
    raw_date = str(form.get('date','')).strip()
    day, _ = _business_date(raw_date)
    if day > (today or date.today()):
        raise ValueError('Kelajak sanasiga kirim kiritib bo‘lmaydi.')
    note = str(form.get('note','')).strip()
    if len(note) > 500:
        raise ValueError('Izoh 500 belgidan oshmasin.')
    if product in BAGGED_FLOUR_PRODUCTS:
        count25, count50 = _bags(form.get('bag_25_count')), _bags(form.get('bag_50_count'))
        amount = 25*count25 + 50*count50
        if amount <= 0 or amount > MAX_KG_PER_ENTRY:
            raise ValueError('Kamida bitta qop kiriting; maksimal 300 000 kg.')
    else:
        count25, count50 = 0, 0
        amount = _kg(form.get('qty_kg',''))
    return {'product':product,'date':_business_date_storage(raw_date),
            'day':day.isoformat(),'qty_kg':amount,'bag_25_count':count25,
            'bag_50_count':count50,'note':note}


def _production_same_day(con, product, day):
    if product in BAGGED_FLOUR_PRODUCTS or product == 'Qora un':
        for sql in (
            "SELECT 1 FROM production_flour_types t JOIN production p ON p.id=t.production_id WHERE t.product=? AND t.qty_kg>0 AND business_date_key(p.date)=? LIMIT 1",
            "SELECT 1 FROM flour_packaging_lines l JOIN flour_packaging p ON p.id=l.packaging_id WHERE l.product=? AND business_date_key(p.date)=? LIMIT 1",
            "SELECT 1 FROM bagged_production_lines l JOIN production p ON p.id=l.production_id WHERE l.product=? AND business_date_key(p.date)=? LIMIT 1",
        ):
            if con.execute(sql,(product,day)).fetchone():
                return True
        return False
    column = {'Kepak':'bran_kg','Muchka':'muchka_kg','Telashka atxod':'telashka_kg'}[product]
    return con.execute(
        f'SELECT 1 FROM production WHERE COALESCE({column},0)>0 AND business_date_key(date)=? LIMIT 1', (day,)
    ).fetchone() is not None


def record_daily_receipt(con, form, user_id, operation_token, *, today=None):
    """Caller owns connection. Use one atomic transaction; reject duplicate tokens."""
    data=normalized_receipt(form,today)
    if not operation_token or len(operation_token)>512:
        raise ValueError('Kirim tokeni yaroqsiz. Sahifani yangilang.')
    con.execute('BEGIN IMMEDIATE')
    try:
        # Fail closed if controlled migration was not applied.
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='daily_finished_stock_receipts'").fetchone():
            raise ValueError('Kunlik kirim jadvali hali sozlanmagan. Administratorga murojaat qiling.')
        if con.execute('SELECT 1 FROM daily_finished_stock_receipts WHERE operation_token=?',(operation_token,)).fetchone():
            raise ValueError('Ushbu kirim allaqachon saqlangan. Qayta yubormang.')
        for table in ('opening_product_stocks','opening_bagged_product_stocks'):
            opening=con.execute(f'SELECT stock_date FROM {table} WHERE product=?',(data['product'],)).fetchone()
            if opening and data['day'] <= _business_date_key(opening['stock_date']):
                raise ValueError('Kirim sanasi boshlang‘ich qoldiq sanasidan keyin bo‘lsin.')
        if _production_same_day(con,data['product'],data['day']):
            raise ValueError('Shu mahsulot va sana uchun ishlab chiqarish/qoplash kirimi mavjud. Ikki marta kirim qilinmaydi.')
        con.execute('''INSERT INTO daily_finished_stock_receipts
            (receipt_date,product,qty_kg,bag_25_count,bag_50_count,note,entered_by,operation_token)
            VALUES(?,?,?,?,?,?,?,?)''',
            (data['date'],data['product'],data['qty_kg'],data['bag_25_count'],
             data['bag_50_count'],data['note'],user_id,operation_token))
        validate_timeline(con,data['day'],products=(data['product'],))
        con.commit()
    except Exception:
        con.rollback()
        raise
    return data
