"""DC65: audited manual corrections on finished stock, no changes to production or sales.

All functions must be called by a role-authorized route, after CSRF verification.
They own the SQL transaction and fail closed when the separate migration is missing.
"""
import hashlib
import json
from datetime import date
from decimal import Decimal, InvalidOperation

from business_dates import _business_date_key, _business_date_storage
from daily_finished_stock import normalized_receipt, MAX_KG_PER_ENTRY
from product_catalog import PRODUCTS, BAGGED_FLOUR_PRODUCTS
from temporal_stock import validate_timeline


def receipt_fingerprint(row):
    data={key:row[key] for key in (
        'id','receipt_date','product','qty_kg','bag_25_count','bag_50_count','note')}
    return hashlib.sha256(json.dumps(data,sort_keys=True,ensure_ascii=False,
                                    separators=(',',':')).encode('utf-8')).hexdigest()


def _require_audit_table(con, table):
    if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():
        raise ValueError('DC65 jadvali hali sozlanmagan. Administratorga murojaat qiling.')


def _reason(raw):
    value = str(raw or '').strip()
    if len(value) < 5 or len(value) > 500:
        raise ValueError('Tuzatish sababi 5–500 belgi bo‘lsin.')
    return value


def edit_daily_receipt(con, receipt_id, form, actor_id, operation_token, expected_fingerprint, *, today=None):
    """Edit quantity/bags/note only; immutable product/date and prior audit."""
    reason=_reason(form.get('reason'))
    if not operation_token or len(operation_token)>512 or len(expected_fingerprint)!=64:
        raise ValueError('Tahrirlash tokeni yaroqsiz.')
    con.execute('BEGIN IMMEDIATE')
    try:
        _require_audit_table(con,'daily_finished_stock_receipt_edits')
        row=con.execute('SELECT * FROM daily_finished_stock_receipts WHERE id=?',(receipt_id,)).fetchone()
        if not row:
            raise ValueError('Kirim hujjati topilmadi.')
        if receipt_fingerprint(row)!=expected_fingerprint:
            raise ValueError('Kirim boshqa oynada o‘zgargan. Sahifani yangilang.')
        if con.execute('SELECT 1 FROM daily_finished_stock_receipt_edits WHERE edit_token=?',(operation_token,)).fetchone():
            raise ValueError('Bu tahrir avval bajarilgan.')
        # A client cannot change the product or the date while editing an existing entry.
        data=normalized_receipt({'product':row['product'],'date':row['receipt_date'],
                                 'bag_25_count':form.get('bag_25_count','0'),
                                 'bag_50_count':form.get('bag_50_count','0'),
                                 'qty_kg':form.get('qty_kg',''),
                                 'note':form.get('note','')},today=today)
        if (float(row['qty_kg'])==data['qty_kg']
                and row['bag_25_count']==data['bag_25_count']
                and row['bag_50_count']==data['bag_50_count']
                and row['note']==data['note']):
            raise ValueError('Hech qanday o‘zgarish kiritilmadi.')
        con.execute('''UPDATE daily_finished_stock_receipts
            SET qty_kg=?,bag_25_count=?,bag_50_count=?,note=? WHERE id=?''',
            (data['qty_kg'],data['bag_25_count'],data['bag_50_count'],data['note'],receipt_id))
        con.execute('''INSERT INTO daily_finished_stock_receipt_edits
            (receipt_id,actor_user_id,old_qty_kg,new_qty_kg,
             old_bag_25_count,new_bag_25_count,old_bag_50_count,new_bag_50_count,
             old_note,new_note,reason,edit_token)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
            (receipt_id,actor_id,row['qty_kg'],data['qty_kg'],
             row['bag_25_count'],data['bag_25_count'],row['bag_50_count'],data['bag_50_count'],
             row['note'],data['note'],reason,operation_token))
        validate_timeline(con,data['day'],products=(row['product'],))
        con.commit()
    except Exception:
        con.rollback()
        raise
    return data


def _signed_int(value):
    txt=str(value if value is not None else '0').strip()
    if txt.startswith(('+','-')):
        digits=txt[1:]
    else:
        digits=txt
    if not digits.isascii() or not digits.isdecimal():
        raise ValueError('Qop farqi butun son bo‘lishi kerak (masalan +2 yoki -1).')
    val=int(txt)
    if abs(val)>12000:
        raise ValueError('Bir tuzatishda 12 000 qopdan oshmasin.')
    return val


def _signed_kg(value):
    try:
        val=Decimal(str(value).strip())
    except (InvalidOperation,ValueError) as exc:
        raise ValueError('Qoldiq farqi noto‘g‘ri.') from exc
    if (not val.is_finite() or val==0 or abs(val)>MAX_KG_PER_ENTRY
            or val!=val.quantize(Decimal('0.01'))):
        raise ValueError('Farq + yoki − 0.01 kg aniqlikda, 300 000 kg gacha bo‘lsin.')
    return float(val)


def record_adjustment(con, form, actor_id, operation_token, *, today=None):
    """An immutable signed correction. A negative delta must pass date-ledger validation."""
    product=str(form.get('product','')).strip()
    if product not in PRODUCTS:
        raise ValueError('Mahsulotni tanlang.')
    reason=_reason(form.get('reason'))
    if not operation_token or len(operation_token)>512:
        raise ValueError('Tuzatish tokeni yaroqsiz.')
    if product in BAGGED_FLOUR_PRODUCTS:
        b25=_signed_int(form.get('bag_25_delta',0))
        b50=_signed_int(form.get('bag_50_delta',0))
        qty=25*b25+50*b50
        if (b25==0 and b50==0) or abs(qty)>MAX_KG_PER_ENTRY:
            raise ValueError('Qop farqlaridan kamida bittasi noldan boshqa bo‘lsin.')
    else:
        b25=b50=0
        qty=_signed_kg(form.get('qty_delta_kg',''))
    adjustment_date=_business_date_storage((today or date.today()).isoformat())
    adjustment_day=_business_date_key(adjustment_date)
    con.execute('BEGIN IMMEDIATE')
    try:
        _require_audit_table(con,'finished_stock_adjustments')
        if con.execute('SELECT 1 FROM finished_stock_adjustments WHERE operation_token=?',(operation_token,)).fetchone():
            raise ValueError('Ushbu tuzatish allaqachon saqlangan.')
        con.execute('''INSERT INTO finished_stock_adjustments
            (adjustment_date,product,qty_delta_kg,bag_25_delta,bag_50_delta,
             reason,entered_by,operation_token) VALUES (?,?,?,?,?,?,?,?)''',
            (adjustment_date,product,qty,b25,b50,reason,actor_id,operation_token))
        validate_timeline(con,adjustment_day,products=(product,))
        con.commit()
    except Exception:
        con.rollback()
        raise
    return {'product':product,'qty_delta_kg':qty,'bag_25_delta':b25,'bag_50_delta':b50}
