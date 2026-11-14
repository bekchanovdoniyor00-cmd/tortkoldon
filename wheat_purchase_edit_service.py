"""DC-001A: transactional, audited wheat-purchase metadata edits only.

This module never changes kilograms, dates, payment records or inventory.
"""
import hashlib
import hmac
import json

OPENING_METHOD = 'Boshlang‘ich zaxira'
SNAPSHOT_FIELDS = ('id', 'date', 'supplier', 'gross', 'tare', 'net',
                   'price_per_kg', 'purchase_method', 'note')


def snapshot_hash(row):
    data = {key: row[key] for key in SNAPSHOT_FIELDS}
    encoded = json.dumps(data, sort_keys=True, ensure_ascii=False,
                         separators=(',', ':'), allow_nan=False).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def validate_metadata(supplier, note):
    supplier = (supplier or '').strip()
    note = (note or '').strip()
    if len(supplier) > 200:
        raise ValueError('Yetkazuvchi nomi 200 belgidan oshmasin.')
    if len(note) > 500:
        raise ValueError('Izoh 500 belgidan oshmasin.')
    return supplier, note


def update_purchase_metadata(con, receipt_id, actor_id, expected_hash,
                             supplier, note):
    """Caller must hold BEGIN IMMEDIATE; returns 'updated' or 'unchanged'."""
    record = con.execute('SELECT * FROM wheat_receipts WHERE id=?',
                         (receipt_id,)).fetchone()
    if record is None:
        raise ValueError('Bug‘doy xaridi topilmadi.')
    if record['purchase_method'] == OPENING_METHOD:
        raise ValueError('Boshlang‘ich bug‘doy zaxirasini bu yerda tahrirlash mumkin emas.')
    if not isinstance(expected_hash, str) or not hmac.compare_digest(
            snapshot_hash(record), expected_hash):
        raise ValueError('Xarid ma’lumotlari o‘zgargan. Sahifani yangilab qayta urinib ko‘ring.')
    supplier, note = validate_metadata(supplier, note)
    if record['supplier'] == supplier and record['note'] == note:
        return 'unchanged'
    con.execute("""CREATE TABLE IF NOT EXISTS wheat_purchase_edit_audit (
        id INTEGER PRIMARY KEY,
        wheat_receipt_id INTEGER NOT NULL,
        actor_user_id INTEGER NOT NULL,
        old_supplier TEXT,
        new_supplier TEXT,
        old_note TEXT,
        new_note TEXT,
        changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(wheat_receipt_id) REFERENCES wheat_receipts(id),
        FOREIGN KEY(actor_user_id) REFERENCES users(id))""")
    con.execute("""UPDATE wheat_receipts SET supplier=?, note=? WHERE id=?""",
                (supplier, note, receipt_id))
    con.execute("""INSERT INTO wheat_purchase_edit_audit
        (wheat_receipt_id, actor_user_id, old_supplier, new_supplier,
         old_note, new_note) VALUES(?,?,?,?,?,?)""",
        (receipt_id, actor_id, record['supplier'], supplier, record['note'], note))
    return 'updated'
