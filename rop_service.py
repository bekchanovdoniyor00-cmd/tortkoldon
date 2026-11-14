"""ROP movements and immutable stop-date reconciliation."""
import json
from datetime import date

from bulk_flour_service import amount, bunker_balance
from product_catalog import BAGGED_FLOUR_PRODUCTS, PRODUCTS
import form117
import rop_forms


ROP_TABLES = ('rop_periods', 'rop_receipts', 'rop_production', 'rop_measurements',
              'rop_transfer_documents', 'rop_production_edits',
              'rop_grain_waste', 'rop_grain_waste_audit', 'rop_form_documents')
LOSS_FIELDS = {
    'rop': 'ROP',
    'production': 'Ishlab chiqarish',
    'packaging': 'Qoplash',
}
ROP_ADJUSTMENTS = {
    'other_in': 'Boshqa hujjatli kirim',
    'other_out': 'Boshqa hujjatli chiqim',
    'cleaning_waste': 'Tozalash chiqindisi',
    'moisture_loss': 'Namlik sabab vazn o‘zgarishi',
}
DATED_INVENTORY_TABLES = {
    'sales', 'sale_cancellations', 'production', 'flour_packaging',
    'opening_product_stocks', 'opening_bagged_product_stocks',
}


def ensure_schema(con):
    con.execute('''CREATE TABLE IF NOT EXISTS rop_form_documents (
        period_id INTEGER NOT NULL REFERENCES rop_periods(id),
        number INTEGER NOT NULL CHECK(number IN (34,117)),
        revision INTEGER NOT NULL CHECK(revision > 0), data TEXT NOT NULL,
        updated_by INTEGER NOT NULL REFERENCES users(id),
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(period_id,number))''')
    con.execute("""CREATE TABLE IF NOT EXISTS rop_periods (
        id INTEGER PRIMARY KEY, start_day TEXT NOT NULL,
        opening_kg REAL NOT NULL CHECK(opening_kg >= 0),
        packaging_floor INTEGER NOT NULL, stop_day TEXT UNIQUE,
        closing_kg REAL CHECK(closing_kg >= 0),
        approved_by INTEGER REFERENCES users(id), approved_at TEXT,
        reconciliation TEXT)""")
    con.execute("""CREATE UNIQUE INDEX IF NOT EXISTS rop_one_open_period
        ON rop_periods((1)) WHERE stop_day IS NULL""")
    con.execute("""CREATE TABLE IF NOT EXISTS rop_receipts (
        id INTEGER PRIMARY KEY, period_id INTEGER NOT NULL REFERENCES rop_periods(id),
        day TEXT NOT NULL, qty_kg REAL NOT NULL CHECK(qty_kg >= 0),
        from_warehouse INTEGER NOT NULL CHECK(from_warehouse IN (0,1)),
        note TEXT NOT NULL, user_id INTEGER NOT NULL REFERENCES users(id),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    con.execute("""CREATE TABLE IF NOT EXISTS rop_production (
        production_id INTEGER PRIMARY KEY REFERENCES production(id),
        period_id INTEGER NOT NULL REFERENCES rop_periods(id))""")
    con.execute("""CREATE TABLE IF NOT EXISTS rop_transfer_documents (
        receipt_id INTEGER PRIMARY KEY REFERENCES rop_receipts(id),
        document_ref TEXT NOT NULL,
        document_key TEXT NOT NULL UNIQUE)""")
    con.execute("""CREATE TABLE IF NOT EXISTS rop_measurements (
        id INTEGER PRIMARY KEY, period_id INTEGER NOT NULL REFERENCES rop_periods(id),
        day TEXT NOT NULL, qty_kg REAL NOT NULL CHECK(qty_kg >= 0),
        document_ref TEXT NOT NULL, movement_state TEXT NOT NULL,
        user_id INTEGER NOT NULL REFERENCES users(id),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    con.execute('CREATE INDEX IF NOT EXISTS rop_receipts_period ON rop_receipts(period_id, day)')
    con.execute('CREATE INDEX IF NOT EXISTS rop_production_period ON rop_production(period_id)')
    con.execute('CREATE INDEX IF NOT EXISTS rop_measurements_period ON rop_measurements(period_id, day, id)')
    con.execute('''CREATE TABLE IF NOT EXISTS rop_production_edits (
        id INTEGER PRIMARY KEY, production_id INTEGER NOT NULL REFERENCES rop_production(production_id),
        user_id INTEGER NOT NULL REFERENCES users(id), reason TEXT NOT NULL,
        previous_values TEXT NOT NULL, new_values TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
    con.execute('CREATE INDEX IF NOT EXISTS rop_production_edits_record ON rop_production_edits(production_id, id)')
    con.execute('''CREATE TABLE IF NOT EXISTS rop_grain_waste (
        id INTEGER PRIMARY KEY, period_id INTEGER NOT NULL REFERENCES rop_periods(id),
        day TEXT NOT NULL, document_ref TEXT NOT NULL, qty_kg REAL NOT NULL CHECK(qty_kg > 0),
        note TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1)''')
    con.execute('''CREATE TABLE IF NOT EXISTS rop_grain_waste_audit (
        id INTEGER PRIMARY KEY, receipt_id INTEGER NOT NULL REFERENCES rop_grain_waste(id),
        user_id INTEGER NOT NULL REFERENCES users(id), document_key TEXT NOT NULL,
        previous_values TEXT, new_values TEXT NOT NULL, reason TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
    con.execute('CREATE INDEX IF NOT EXISTS rop_grain_waste_period ON rop_grain_waste(period_id, day)')
    con.execute('CREATE INDEX IF NOT EXISTS rop_grain_waste_documents ON rop_grain_waste_audit(document_key)')


def save_grain_waste(con, period, day, form, user_id, *, receipt_id=None, revision=None):
    if period is None or period['stop_day'] is not None:
        raise ValueError('Donli chiqindi kirimi uchun ochiq ROP davri kerak.')
    check_day(day, period)
    reference = document_reference(form.get('document_ref'), 'Nakladnoy')
    key = ' '.join(reference.split()).casefold()
    qty = amount(form.get('qty_kg'), 'Зерно отход', positive=True)
    note = form.get('note', '').strip()
    previous = None
    reason = ''
    if receipt_id is not None:
        previous = con.execute('SELECT * FROM rop_grain_waste WHERE id=? AND period_id=?',
                               (receipt_id, period['id'])).fetchone()
        if previous is None or previous['revision'] != revision:
            raise ValueError('Kirim o‘zgargan. Formani qayta oching.')
        reason = form.get('reason', '').strip()
        if not 1 <= len(reason) <= 500:
            raise ValueError('Tuzatish sababini kiriting.')
    if con.execute('''SELECT 1 FROM rop_grain_waste_audit
        WHERE document_key=? AND receipt_id<>?''', (key, receipt_id or 0)).fetchone():
        raise ValueError('Bu nakladnoy avval qayd etilgan.')
    if previous is None:
        receipt_id = con.execute('''INSERT INTO rop_grain_waste
            (period_id,day,document_ref,qty_kg,note) VALUES(?,?,?,?,?)''',
            (period['id'], day, reference, qty, note)).lastrowid
    else:
        con.execute('''UPDATE rop_grain_waste SET day=?,document_ref=?,qty_kg=?,note=?,
            revision=revision+1 WHERE id=?''', (day, reference, qty, note, receipt_id))
    current = con.execute('SELECT * FROM rop_grain_waste WHERE id=?', (receipt_id,)).fetchone()
    con.execute('''INSERT INTO rop_grain_waste_audit
        (receipt_id,user_id,document_key,previous_values,new_values,reason) VALUES(?,?,?,?,?,?)''',
        (receipt_id, user_id, key, json.dumps(dict(previous), ensure_ascii=False) if previous else None,
         json.dumps(dict(current), ensure_ascii=False), reason))
    return receipt_id


def grain_waste_summary(con, period_id, cutoff):
    return con.execute('''SELECT COUNT(*) AS entries, ROUND(SUM(qty_kg),3) AS kg
        FROM rop_grain_waste WHERE period_id=? AND day<=?''', (period_id, cutoff)).fetchone()


def wheat_warehouse_used(con):
    """ROP issues are tracked in their own ledger. Legacy issues still use raw weight."""
    return con.execute('''SELECT
        (SELECT COALESCE(SUM(COALESCE(raw_wheat_kg,wheat_kg)),0)
         FROM production WHERE id NOT IN (SELECT production_id FROM rop_production))
        + (SELECT COALESCE(SUM(qty_kg),0) FROM rop_receipts WHERE from_warehouse=1)''').fetchone()[0]


def latest_period(con):
    return con.execute('SELECT * FROM rop_periods ORDER BY id DESC LIMIT 1').fetchone()


def period_totals(con, period):
    received = con.execute('SELECT COALESCE(SUM(qty_kg),0) FROM rop_receipts WHERE period_id=?',
                           (period['id'],)).fetchone()[0]
    production = con.execute('''SELECT COALESCE(SUM(wheat_kg),0) AS cleaned,
        COALESCE(SUM(flour_kg + bran_kg + muchka_kg + telashka_kg),0) AS output
        FROM production p JOIN rop_production r ON r.production_id=p.id
        WHERE r.period_id=?''', (period['id'],)).fetchone()
    packaging = con.execute('''SELECT COALESCE(SUM(withdrawn_kg-packaged_kg),0)
        FROM flour_packaging WHERE id>?''', (period['packaging_floor'],)).fetchone()[0]
    return {
        'opening': period['opening_kg'], 'received': round(received, 3),
        'cleaned': round(production['cleaned'], 3), 'output': round(production['output'], 3),
        'calculated_difference': round(period['opening_kg'] + received - production['cleaned'], 3),
        'production_gap': round(production['cleaned'] - production['output'], 3),
        'packaging_gap': round(packaging, 3),
    }


def latest_measurement(con, period):
    if period is None:
        return None
    return con.execute('''SELECT * FROM rop_measurements WHERE period_id=?
        ORDER BY day DESC, id DESC LIMIT 1''', (period['id'],)).fetchone()


def document_reference(value, label):
    reference = (value or '').strip()
    if not 1 <= len(reference) <= 250:
        raise ValueError(f'{label} hujjatining raqami yoki havolasini kiriting.')
    return reference


def report_month_start(value, stop_day):
    """Normalize an explicit reporting month, falling back for legacy records."""
    month = stop_day[:7] if value is None else value
    try:
        parsed = date.fromisoformat(f'{month}-01')
    except ValueError as exc:
        raise ValueError('Hisobot oyini YYYY-MM ko‘rinishida kiriting.') from exc
    if parsed.isoformat()[:7] != month:
        raise ValueError('Hisobot oyini YYYY-MM ko‘rinishida kiriting.')
    return parsed


def movement_state(con, period):
    state = con.execute('''SELECT
        (SELECT COALESCE(MAX(id),0) FROM rop_receipts WHERE period_id=?),
        (SELECT COALESCE(MAX(production_id),0) FROM rop_production WHERE period_id=?),
        (SELECT COALESCE(MAX(id),0) FROM flour_packaging WHERE id>?)''',
        (period['id'], period['id'], period['packaging_floor'])).fetchone()
    last_edit = con.execute('''SELECT MAX(e.id) FROM rop_production_edits e
        JOIN rop_production p ON p.production_id=e.production_id WHERE p.period_id=?''',
                            (period['id'],)).fetchone()[0]
    values = list(state)
    if last_edit is not None:
        values.append(last_edit)
    waste_revision = con.execute('''SELECT MAX(a.id) FROM rop_grain_waste_audit a
        JOIN rop_grain_waste w ON w.id=a.receipt_id WHERE w.period_id=?''', (period['id'],)).fetchone()[0]
    if waste_revision is not None:
        values.append({'grain_waste': waste_revision})
    return json.dumps(values)


def measure(con, day, quantity, document, user_id):
    period = latest_period(con)
    if period is None or period['stop_day'] is not None:
        raise ValueError('Fizik sanoq uchun ochiq ROP davri kerak.')
    check_day(day, period)
    qty = amount(quantity, 'Haqiqiy ROP sanoqi')
    reference = document_reference(document, 'Fizik sanoq')
    return con.execute('''INSERT INTO rop_measurements
        (period_id,day,qty_kg,document_ref,movement_state,user_id)
        VALUES(?,?,?,?,?,?)''',
        (period['id'], day, qty, reference, movement_state(con, period), user_id)).lastrowid


def check_day(day, period):
    if date.fromisoformat(day) > date.today():
        raise ValueError('Kelajak sanasini kiritib bo‘lmaydi.')
    if period is not None and day < period['start_day']:
        raise ValueError('Sana ROP davri boshlanishidan oldin.')


def receive(con, day, qty, source, note, user_id, *, transfer_document=None):
    qty = amount(qty, 'ROP kirimi')
    if source not in ('direct', 'warehouse'):
        raise ValueError('ROP kirimi manbasini tanlang.')
    period = latest_period(con)
    check_day(day, period if period is not None and period['stop_day'] is None else None)
    if source == 'warehouse':
        reference = document_reference(transfer_document, 'O‘tkazma')
        document_key = reference.casefold()
        if con.execute('SELECT 1 FROM rop_transfer_documents WHERE document_key=?',
                       (document_key,)).fetchone():
            raise ValueError('Ushbu hujjat bilan o‘tkazma avval qayd etilgan.')
        available = con.execute('''WITH movements AS (
            SELECT business_date_key(date) AS day, net AS delta FROM wheat_receipts
            UNION ALL SELECT business_date_key(date), -COALESCE(raw_wheat_kg,wheat_kg)
                FROM production WHERE id NOT IN (SELECT production_id FROM rop_production)
            UNION ALL SELECT day, -qty_kg FROM rop_receipts WHERE from_warehouse=1
            UNION ALL SELECT ?, 0
        ), daily AS (SELECT day, SUM(delta) AS net FROM movements GROUP BY day),
        balances AS (SELECT day, SUM(net) OVER (ORDER BY day) AS qty FROM daily)
        SELECT MIN(qty) FROM balances WHERE day>=?''', (day, day)).fetchone()[0]
        if not qty or qty > round(available, 3):
            raise ValueError('Bug‘doy omborida o‘tkazma uchun yetarli qoldiq yo‘q.')
    if period is None or period['stop_day'] is not None:
        if not qty and not bunker_balance(con):
            raise ValueError('Davrni ochish uchun ROP kirimi yoki o‘tuvchi bunker qoldig‘i kerak.')
        if period is not None and day <= period['stop_day']:
            raise ValueError('Yangi davr sanasi oxirgi zachiskadan keyin bo‘lishi kerak.')
        latest_legacy_day = con.execute('''SELECT MAX(day) FROM (
            SELECT MAX(business_date_key(date)) AS day FROM production
            UNION ALL SELECT MAX(business_date_key(date)) FROM flour_packaging
            UNION ALL SELECT MAX(business_date_key(stock_date)) FROM bunker_openings
        )''').fetchone()[0]
        if latest_legacy_day and day < latest_legacy_day:
            raise ValueError('ROP davri avvalgi ishlab chiqarish yoki bunker yozuvidan oldin boshlanmaydi.')
        period_id = con.execute('''INSERT INTO rop_periods(start_day,opening_kg,packaging_floor)
            VALUES(?,0,(SELECT COALESCE(MAX(id),0) FROM flour_packaging))''',
            (day,)).lastrowid
    else:
        if not qty:
            raise ValueError('Qo‘shimcha kirim uchun musbat miqdor kiriting.')
        period_id = period['id']
    receipt_id = con.execute('''INSERT INTO rop_receipts(period_id,day,qty_kg,from_warehouse,note,user_id)
        VALUES(?,?,?,?,?,?)''', (period_id, day, qty, source == 'warehouse', note, user_id)).lastrowid
    if source == 'warehouse':
        con.execute('''INSERT INTO rop_transfer_documents(receipt_id,document_ref,document_key)
            VALUES(?,?,?)''', (receipt_id, reference, document_key))
    return receipt_id


def production_period(con, day):
    period = latest_period(con)
    if period is None or period['stop_day'] is not None:
        raise ValueError('Avval ROP hisob davrini oching.')
    check_day(day, period)
    return period['id']


def check_packaging_day(con, day, action):
    period = latest_period(con)
    if period is None:
        if action == 'opening':
            return
        raise ValueError('Qoplash uchun avval ROP hisob davrini oching.')
    if period['stop_day'] is not None:
        raise ValueError('Qoplash uchun yangi ROP davrini oching.')
    check_day(day, period)
    if action == 'opening':
        raise ValueError('Boshlang‘ich bunker qoldig‘ini ROP davridan oldin kiriting.')
    latest_bunker_day = con.execute('''SELECT MAX(day) FROM (
        SELECT MAX(business_date_key(p.date)) AS day FROM bulk_flour_batches b
        JOIN production p ON p.id=b.production_id
        UNION ALL SELECT MAX(business_date_key(date)) FROM flour_packaging
    )''').fetchone()[0]
    if latest_bunker_day and day < latest_bunker_day:
        raise ValueError('Qoplash sanasi oxirgi bunker harakatidan oldin bo‘lmasin.')


def validate_stop(con, period, day):
    if period is None or period['stop_day'] is not None:
        raise ValueError('Zachiska uchun ochiq ROP davri kerak.')
    check_day(day, period)
    latest_day = con.execute('''SELECT MAX(day) FROM (
        SELECT MAX(day) AS day FROM rop_receipts WHERE period_id=?
        UNION ALL SELECT MAX(business_date_key(p.date)) FROM production p
            JOIN rop_production r ON r.production_id=p.id WHERE r.period_id=?
        UNION ALL SELECT MAX(business_date_key(date)) FROM flour_packaging WHERE id>?
        UNION ALL SELECT MAX(day) FROM rop_grain_waste WHERE period_id=?
    )''', (period['id'], period['id'], period['packaging_floor'], period['id'])).fetchone()[0]
    if latest_day and day < latest_day:
        raise ValueError('To‘xtash sanasidan keyin ushbu davrda harakatlar bor.')


def finished_counts(stocks, bags):
    counts = {}
    for product in PRODUCTS:
        stock = round(stocks[product], 3)
        if product in BAGGED_FLOUR_PRODUCTS:
            sizes = bags[product]
            counts[product] = {'25': sizes[25], '50': sizes[50],
                               'kg': round(stock - sizes[25] * 25 - sizes[50] * 50, 3)}
        else:
            counts[product] = {'kg': stock}
    counts['Un (eski yozuv)'] = {'kg': round(stocks.get('Un (eski yozuv)', 0), 3)}
    return counts


def documented_adjustments(form, waste_kg=None):
    """Positive moisture means water removed. Negative means water added."""
    adjustments = {}
    other = 0
    for key, label in ROP_ADJUSTMENTS.items():
        value = (form.get(key) or '').strip()
        if key == 'cleaning_waste' and waste_kg is not None:
            if value and amount(value, label):
                raise ValueError('Nakladnoydagi donli chiqindini qo‘lda takror kiritmang.')
            other = amount(form.get('other_cleaning_waste') or '0', 'Boshqa tozalash chiqindisi')
            adjustments[key] = round(waste_kg + other, 3)
            continue
        if key == 'moisture_loss':
            adjustments[key] = form117.signed_amount(value, label)
        else:
            adjustments[key] = amount(value, label)
    documents = {
        'other_movements': document_reference(form.get('movement_document'), 'ROP harakatlari'),
        'cleaning_waste': ('ROP nakladnoylari' if waste_kg is not None else
                           document_reference(form.get('cleaning_document'), 'Tozalash chiqindisi')),
        'moisture': document_reference(form.get('moisture_document'), 'Namlik o‘zgarishi'),
    }
    if waste_kg is not None and other:
        documents['other_cleaning_waste'] = document_reference(
            form.get('cleaning_document'), 'Boshqa tozalash chiqindisi')
    return adjustments, documents


def approve(con, period, day, form, expected, user_id, *, report_month):
    totals = period_totals(con, period)
    stop_document = document_reference(form.get('stop_document'), 'Zavod to‘xtashi')
    balance_document = document_reference(form.get('balance_document'), 'Moddiy balans')
    if form.get('documents_checked') != 'yes':
        raise ValueError('To‘xtash, bo‘shlik va moddiy balans hujjatlarini tekshirganingizni tasdiqlang.')
    measurement = latest_measurement(con, period)
    if measurement is None or measurement['day'] != day or measurement['qty_kg'] != 0:
        raise ValueError('To‘xtash sanasida ROPning 0 kg fizik sanoqi va hujjati kerak.')
    if measurement['movement_state'] != movement_state(con, period):
        raise ValueError('Sanoqdan keyin davr harakatlari o‘zgardi. Bo‘shlik sanoqini qayta tasdiqlang.')
    waste = grain_waste_summary(con, period['id'], day)
    doc34, doc117, yield_report = rop_forms.closing_values(
        con, period, day, totals['cleaned'], waste['kg'], report_month)
    if doc34:
        if any(amount(form.get(key) or '0', 'Tozalash chiqindisi')
               for key in ('cleaning_waste', 'other_cleaning_waste')):
            raise ValueError('34-shakldagi chiqindini qo‘lda takror kiritmang.')
        clean_total, _, _ = rop_forms.cleaning(doc34)
        adjustments, adjustment_documents = documented_adjustments(form, clean_total)
        adjustment_documents['cleaning_waste'] = doc34['document']
    else:
        adjustments, adjustment_documents = documented_adjustments(form, waste['kg'])
    if not doc117:
        yield_report = form117.from_form(form, totals['cleaned'])
    physical_bunker = amount(form.get('bunker_kg'), 'Haqiqiy bunker qoldig‘i')
    declared = {key: (form117.signed_amount if key == 'production' else amount)(
        form.get(f'loss_{key}') or '0', f'{label} farqi') for key, label in LOSS_FIELDS.items()}
    reasons = {key: (form.get(f'reason_{key}') or '').strip() for key in LOSS_FIELDS}
    differences = {
        'rop': totals['calculated_difference'] + adjustments['other_in'] - adjustments['other_out']
               - adjustments['cleaning_waste'] - adjustments['moisture_loss'],
        'production': totals['production_gap'], 'packaging': totals['packaging_gap'],
    }
    for key, difference in differences.items():
        unexplained = round(difference - declared[key], 3)
        if unexplained:
            raise ValueError(f'{LOSS_FIELDS[key]} bo‘yicha izohlanmagan tafovut: {unexplained:,.3f} kg.')
        if declared[key] and not 5 <= len(reasons[key]) <= 500:
            raise ValueError(f'{LOSS_FIELDS[key]} yo‘qotishi uchun 5–500 belgili asos kiriting.')
    if declared['rop']:
        adjustment_documents['other_loss'] = document_reference(
            form.get('other_loss_document'), 'Boshqa ROP yo‘qotishi')
    expected_bunker = bunker_balance(con)
    if physical_bunker != expected_bunker:
        raise ValueError(f'Bunker tafovuti: {round(expected_bunker - physical_bunker, 3):,.3f} kg.')
    for product, values in expected.items():
        for unit, book_value in values.items():
            actual = amount(form.get(f'count_{product}_{unit}'), f'{product} ({unit})')
            if unit != 'kg' and not actual.is_integer():
                raise ValueError(f'{product} ({unit} kg) qop soni butun bo‘lishi kerak.')
            if actual != book_value:
                label = 'kg' if unit == 'kg' else f'{unit} kg qop'
                raise ValueError(f'{product} tafovuti: {round(book_value - actual, 3):,.3f} {label}.')
    reconciliation = json.dumps({
        **totals, 'physical_rop': 0, 'bunker_kg': physical_bunker,
        'report_month': report_month,
        'finished': expected, 'losses': declared, 'reasons': reasons,
        'rop_adjustments': adjustments, 'rop_unexplained_kg': 0,
        **({'form34_grain_waste_kg': waste['kg']} if waste['entries'] else {}),
        **({'form117_yield': yield_report} if yield_report is not None else {}),
        'documents': {'stop': stop_document, 'empty': measurement['document_ref'],
                      'balance': balance_document, 'measurement_id': measurement['id'],
                      **adjustment_documents},
    }, ensure_ascii=False)
    con.execute('''UPDATE rop_periods SET stop_day=?,closing_kg=?,approved_by=?,
        approved_at=CURRENT_TIMESTAMP,reconciliation=? WHERE id=?''',
        (day, 0, user_id, reconciliation, period['id']))
    return period['id']
