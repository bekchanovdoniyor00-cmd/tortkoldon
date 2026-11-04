from flask import Flask, render_template, request, redirect, url_for, session, flash, abort, send_file, send_from_directory
import sqlite3, os, shutil, json, secrets, hmac, re
from pathlib import Path
from functools import wraps
import math
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP
from datetime import datetime, date, timedelta
from werkzeug.security import generate_password_hash, check_password_hash
from itsdangerous import URLSafeSerializer, BadSignature
from finance_calculations import (
    calculate_financial_snapshot, customer_identity_context,
    resolve_customer_identity, resolve_customer_name,
)
from date_conversions import dmy_to_date, iso_to_date
from ui_translations import DEFAULT_LANGUAGE, LANGUAGES, get_language, translate
from monthly_reporting import build_monthly_report, build_monthly_workbook

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = (
    os.environ.get('ERP_DATABASE_PATH')
    or os.environ.get('DATABASE_PATH')
    or os.environ.get('DB_PATH')
    or os.path.join(BASE_DIR, 'tortkuldon.db')
)
OPENING_WHEAT_METHOD = 'Boshlang‘ich zaxira'

def _business_date(value):
    if isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        return iso_to_date(value), 'iso'
    if isinstance(value, str) and re.fullmatch(r'\d{2}-\d{2}-\d{4}', value):
        return dmy_to_date(value), 'dmy'
    raise ValueError('Sana YYYY-MM-DD yoki DD-MM-YYYY formatida bo‘lishi kerak.')

def _business_date_after(value, days):
    parsed, date_format = _business_date(value)
    result = parsed + timedelta(days=days)
    if date_format == 'iso':
        return result.isoformat()
    return f'{result.day:02d}-{result.month:02d}-{result.year:04d}'

def _business_date_key(value):
    return _business_date(value)[0].isoformat()

def _business_date_storage(value):
    parsed, _ = _business_date(value)
    return f'{parsed.day:02d}-{parsed.month:02d}-{parsed.year:04d}'

def _html_date_storage(value):
    parsed = iso_to_date(value)
    return f'{parsed.day:02d}-{parsed.month:02d}-{parsed.year:04d}'

def _business_date_input(value):
    return _business_date(value)[0].isoformat()

def _business_date_display(value):
    parsed, _ = _business_date(value)
    return parsed.strftime('%d.%m.%Y')

def _monthly_parse_date(value):
    if isinstance(value, str) and len(value) > 10:
        value = value[:10]
    return _business_date(value)

def _monthly_historical_snapshot(con, cutoff, warnings):
    snapshot = sqlite3.connect(':memory:')
    snapshot.row_factory = sqlite3.Row
    try:
        con.backup(snapshot)
        snapshot.create_function(
            'business_date_key', 1, _business_date_key, deterministic=True)
        cutoff_text = cutoff.isoformat()
        dated_tables = {
            'sales': 'date',
            'opening_debts': 'date',
            'opening_debt_receipts': 'date',
            'worker_receipts': 'date',
            'accountant_worker_receipts': 'payment_date',
            'payments': 'date',
            'sale_cancellations': 'cancelled_at',
            'production': 'date',
            'wheat_receipts': 'date',
            'opening_product_stocks': 'stock_date',
            'opening_bagged_product_stocks': 'stock_date',
            'expenses': 'date',
            'accountant_cash_additions': 'date',
            'wheat_purchase_payments': 'payment_date',
            'account_cash_transfers': 'date',
            'worker_sale_batch_payments': 'created_at',
            'worker_sale_batches': 'created_at',
        }
        for table, date_column in dated_tables.items():
            exists = snapshot.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,)).fetchone()
            if not exists:
                continue
            columns = {
                row['name'] for row in
                snapshot.execute(f'PRAGMA table_info("{table}")')
            }
            if date_column not in columns:
                continue
            for row in snapshot.execute(
                    f'SELECT rowid AS _rowid, "{date_column}" AS _date '
                    f'FROM "{table}"').fetchall():
                try:
                    row_day = _monthly_parse_date(row['_date'])[0]
                except (TypeError, ValueError):
                    if table == 'worker_transfers':
                        continue
                    if table == 'sale_cancellations':
                        snapshot.execute(
                            'DELETE FROM sale_cancellations WHERE rowid=?',
                            (row['_rowid'],))
                    else:
                        snapshot.execute(
                            f'DELETE FROM "{table}" WHERE rowid=?',
                            (row['_rowid'],))
                    warnings.append({
                        'key': 'historical_snapshot_undated',
                        'message': (
                            'Sana aniqlanmagan tarixiy yozuv cutoff hisobiga '
                            'kiritilmadi.'),
                        'table': table,
                        'record_id': row['_rowid'],
                        'date': row['_date'],
                    })
                    continue
                if row_day > cutoff:
                    snapshot.execute(
                        f'DELETE FROM "{table}" WHERE rowid=?',
                        (row['_rowid'],))
        for table, parent_table, parent_id, child_id in (
                ('production_flour_types', 'production', 'id', 'production_id'),
                ('bagged_production_lines', 'production', 'id', 'production_id'),
                ('bagged_sale_deductions', 'sales', 'id', 'sale_id'),
                ('worker_sale_batch_items', 'sales', 'id', 'sale_id')):
            child_exists = snapshot.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,)).fetchone()
            parent_exists = snapshot.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (parent_table,)).fetchone()
            if child_exists and parent_exists:
                snapshot.execute(
                    f'DELETE FROM "{table}" WHERE "{child_id}" NOT IN '
                    f'(SELECT "{parent_id}" FROM "{parent_table}")')
        if snapshot.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='worker_transfers'").fetchone():
            for row in snapshot.execute(
                        "SELECT rowid AS _rowid,id,decided_at FROM worker_transfers "
                    "WHERE status='approved'").fetchall():
                try:
                    transfer_day = _monthly_parse_date(row['decided_at'])[0]
                except (TypeError, ValueError):
                    snapshot.execute(
                        'DELETE FROM worker_transfers WHERE rowid=?',
                        (row['_rowid'],))
                    warnings.append({
                        'key': 'historical_snapshot_undated_transfer',
                        'message': 'Tasdiqlangan transfer sanasi noaniq; '
                                   'tarixiy kassa snapshotiga kiritilmadi.',
                        'record_id': row['id'],
                    })
                    continue
                if transfer_day > cutoff:
                    snapshot.execute(
                        'DELETE FROM worker_transfers WHERE rowid=?',
                        (row['_rowid'],))

        stocks = product_stocks(snapshot)
        bags = bagged_product_stocks(snapshot)
        wheat_in = snapshot.execute(
            'SELECT COALESCE(SUM(net),0) FROM wheat_receipts').fetchone()[0]
        wheat_used = snapshot.execute(
            'SELECT COALESCE(SUM(COALESCE(raw_wheat_kg,wheat_kg)),0) '
            'FROM production').fetchone()[0]
        try:
            financial = calculate_financial_snapshot(snapshot)
        except ValueError as exc:
            financial = None
            warnings.append({
                'key': 'historical_debt_snapshot_error',
                'message': 'Tarixiy qarz snapshotini hisoblab bo‘lmadi: '
                           f'{exc}',
                'cutoff': cutoff_text,
            })
        return {
            'stocks': stocks,
            'bags': bags,
            'wheat_stock': Decimal(str(wheat_in or 0)) - Decimal(str(wheat_used or 0)),
            'financial': financial,
        }
    finally:
        snapshot.close()

app = Flask(__name__)

PAGINATION_SIZES = (20, 50, 100)


def _pagination_state(total, page_param='page', per_page_param='per_page'):
    try:
        per_page = int(request.args.get(per_page_param, '20'))
    except (TypeError, ValueError):
        per_page = 20
    if per_page not in PAGINATION_SIZES:
        per_page = 20
    try:
        requested_page = int(request.args.get(page_param, '1'))
    except (TypeError, ValueError):
        requested_page = 1
    total = max(0, int(total))
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(max(1, requested_page), pages)
    if pages <= 7:
        page_window = list(range(1, pages + 1))
    else:
        page_window = [1]
        if page > 4:
            page_window.append(None)
        page_window.extend(range(max(2, page - 1), min(pages, page + 1) + 1))
        if page < pages - 3:
            page_window.append(None)
        page_window.append(pages)
    return {
        'total': total,
        'per_page': per_page,
        'page': page,
        'pages': pages,
        'offset': (page - 1) * per_page,
        'start': (page - 1) * per_page + 1 if total else 0,
        'end': min(page * per_page, total),
        'window': page_window,
        'page_param': page_param,
        'per_page_param': per_page_param,
    }


def paginate_rows(con, select_sql, count_sql, params=(),
                  page_param='page', per_page_param='per_page'):
    total = con.execute(count_sql, params).fetchone()[0]
    pagination = _pagination_state(total, page_param, per_page_param)
    rows = con.execute(
        f'{select_sql} LIMIT ? OFFSET ?',
        (*params, pagination['per_page'], pagination['offset']),
    ).fetchall()
    return rows, pagination


def paginate_items(items, page_param='page', per_page_param='per_page'):
    pagination = _pagination_state(len(items), page_param, per_page_param)
    start = pagination['offset']
    return items[start:start + pagination['per_page']], pagination


@app.context_processor
def pagination_context():
    def pagination_url(page, pagination):
        values = request.args.to_dict(flat=False)
        values[pagination['page_param']] = str(page)
        values[pagination['per_page_param']] = str(pagination['per_page'])
        route_values = dict(request.view_args or {})
        route_values.update(values)
        return url_for(request.endpoint, **route_values)
    return {'pagination_url': pagination_url}


@app.route('/service-worker.js')
def service_worker():
    response = send_from_directory(
        app.static_folder, 'service-worker.js', mimetype='application/javascript'
    )
    response.headers['Service-Worker-Allowed'] = '/'
    response.headers['Cache-Control'] = 'no-cache'
    return response


@app.route('/robots.txt')
def robots_txt():
    return send_from_directory(app.static_folder, 'robots.txt', mimetype='text/plain')


@app.route('/sitemap.xml')
def sitemap_xml():
    return send_from_directory(app.static_folder, 'sitemap.xml', mimetype='application/xml')


# A separate persistent session secret for the isolated test installation.
SECRET_PATH = os.path.join(BASE_DIR, '.sinov_session_key')
if os.environ.get('ERP_SECRET_KEY'):
    app.secret_key = os.environ['ERP_SECRET_KEY']
elif os.environ.get('SECRET_KEY'):
    app.secret_key = os.environ['SECRET_KEY']
elif os.environ.get('ERP_SECRET_FILE') and os.path.isfile(os.environ['ERP_SECRET_FILE']):
    with open(os.environ['ERP_SECRET_FILE'], encoding='utf-8') as f:
        app.secret_key = f.read().strip()
else:
    if not os.path.exists(SECRET_PATH):
        with open(SECRET_PATH, 'w', encoding='utf-8') as f:
            f.write(secrets.token_hex(32))
    with open(SECRET_PATH, encoding='utf-8') as f:
        app.secret_key = f.read().strip()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax')

if os.environ.get('BEHIND_PROXY', 'false').lower() in ('true', '1', 'yes') or os.environ.get('ERP_MODE') == 'production':
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

ROLES = {'rahbar': 'Boshliq', 'buxgalter': 'Buxgalter', 'umid': 'Umid', 'maxmud': 'Maxmud', 'ishchi': 'Eski ishchi'}

def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.create_function('business_date_key', 1, _business_date_key, deterministic=True)
    conn.execute('PRAGMA foreign_keys = ON')
    conn.execute('PRAGMA busy_timeout=30000')
    return conn

@app.template_filter('business_date')
def business_date_filter(value):
    return _business_date_display(value) if value else ''

@app.template_filter('business_date_input')
def business_date_input_filter(value):
    return _business_date_input(value) if value else ''

def phone_key(raw):
    digits=re.sub(r'\D','',raw or '')
    if len(digits)==9: digits='998'+digits
    return digits if len(digits)==12 and digits.startswith('998') else ''

def customer_display(context, name, phone=None):
    card = (resolve_customer_identity(context, name, phone)
            if phone is not None else resolve_customer_name(context, name))
    if card is None:
        return name, phone
    return card['name'], card['phone'] if phone is not None else None

def backup_before_internal_cash_transfer_migration():
    if not os.path.isfile(DB_PATH):
        return
    uri = Path(DB_PATH).resolve().as_uri() + '?mode=ro'
    con = sqlite3.connect(uri, uri=True, timeout=30)
    try:
        existing_tables = {
            row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('account_cash_transfers','opening_product_stocks',"
                "'opening_bagged_product_stocks','bagged_production_lines',"
                "'bagged_sale_deductions','wheat_purchase_payments',"
                "'accountant_cash_additions','user_auth_versions')"
            )
        }
    finally:
        con.close()
    required_tables = {
        'account_cash_transfers', 'opening_product_stocks',
        'opening_bagged_product_stocks', 'bagged_production_lines',
        'bagged_sale_deductions', 'wheat_purchase_payments',
        'accountant_cash_additions', 'user_auth_versions',
    }
    if required_tables <= existing_tables:
        return
    backup_path = make_backup()
    if not backup_path.is_file() or backup_path.stat().st_size == 0:
        raise RuntimeError('Yangi kassa, ombor yoki bug‘doy xaridi jadvalidan oldin DB zaxirasi yaratilmadi.')


def backup_before_opening_debt_assignment_migration():
    """Additive migratsiyadan avval mavjud DB'ning nuqtaviy nusxasi."""
    if not os.path.isfile(DB_PATH):
        return
    with db() as con:
        exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='opening_debts'").fetchone()
        if not exists:
            return
        cols = {row['name'] for row in con.execute('PRAGMA table_info(opening_debts)')}
        if 'worker_id' in cols:
            return
    backup_path = make_backup()
    if not backup_path.is_file() or backup_path.stat().st_size == 0:
        raise RuntimeError('Eski qarz migratsiyasi oldidan bazaning zaxirasi olinmadi.')


def backup_before_opening_receipts_migration():
    if not os.path.isfile(DB_PATH):
        return
    with db() as con:
        old = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='opening_debts'").fetchone()
        new = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='opening_debt_receipts'").fetchone()
    if old and not new:
        path = make_backup()
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError('Eski qarz to‘lovlari migratsiyasi oldidan zaxira yaratilmadi.')


def init_db():
    backup_before_internal_cash_transfer_migration()
    backup_before_opening_debt_assignment_migration()
    backup_before_opening_receipts_migration()
    with db() as con:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, role TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS user_auth_versions (
            user_id INTEGER PRIMARY KEY,
            version INTEGER NOT NULL DEFAULT 0 CHECK(version >= 0),
            FOREIGN KEY(user_id) REFERENCES users(id));
        CREATE TABLE IF NOT EXISTS wheat_receipts(id INTEGER PRIMARY KEY, date TEXT NOT NULL, supplier TEXT, gross REAL NOT NULL, tare REAL NOT NULL, net REAL NOT NULL, price_per_kg REAL NOT NULL, vat_rate REAL NOT NULL, note TEXT, user_id INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS production(id INTEGER PRIMARY KEY, date TEXT NOT NULL, wheat_kg REAL NOT NULL, flour_kg REAL NOT NULL, bran_kg REAL NOT NULL, note TEXT, user_id INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS sales(id INTEGER PRIMARY KEY, date TEXT NOT NULL, customer TEXT NOT NULL, product TEXT NOT NULL, qty_kg REAL NOT NULL, price_per_kg REAL NOT NULL, vat_rate REAL NOT NULL, paid REAL NOT NULL DEFAULT 0, note TEXT, user_id INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS expenses(id INTEGER PRIMARY KEY, date TEXT NOT NULL, category TEXT NOT NULL, amount REAL NOT NULL, note TEXT, user_id INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS payments(id INTEGER PRIMARY KEY, date TEXT NOT NULL, customer TEXT NOT NULL, amount REAL NOT NULL, note TEXT, user_id INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        ''')
        expense_cols = {r['name'] for r in con.execute('PRAGMA table_info(expenses)')}
        for column, definition in (
                ('actual_person', 'TEXT'),
                ('entered_by', 'INTEGER REFERENCES users(id)'),
                ('purpose', 'TEXT'),
                ('fund_source', 'TEXT')):
            if column not in expense_cols:
                con.execute(f'ALTER TABLE expenses ADD COLUMN {column} {definition}')
        con.execute('''CREATE TABLE IF NOT EXISTS opening_debts (
            id INTEGER PRIMARY KEY, customer TEXT NOT NULL, customer_phone TEXT NOT NULL,
            amount REAL NOT NULL CHECK(amount > 0), date TEXT NOT NULL,
            due_date TEXT NOT NULL, note TEXT, user_id INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP)''')
        debt_cols = {r['name'] for r in con.execute('PRAGMA table_info(opening_debts)')}
        if 'worker_id' not in debt_cols:
            con.execute('ALTER TABLE opening_debts ADD COLUMN worker_id INTEGER REFERENCES users(id)')
        con.execute("""CREATE TABLE IF NOT EXISTS opening_debt_assignment_audit (
            id INTEGER PRIMARY KEY,
            opening_debt_id INTEGER NOT NULL REFERENCES opening_debts(id),
            old_worker_id INTEGER REFERENCES users(id),
            new_worker_id INTEGER NOT NULL REFERENCES users(id),
            actor_user_id INTEGER NOT NULL REFERENCES users(id),
            changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
        con.execute("""CREATE TABLE IF NOT EXISTS opening_debt_receipts (
            id INTEGER PRIMARY KEY,
            opening_debt_id INTEGER NOT NULL REFERENCES opening_debts(id),
            worker_id INTEGER NOT NULL REFERENCES users(id),
            amount REAL NOT NULL CHECK(amount>0),
            date TEXT NOT NULL,
            payment_method TEXT NOT NULL CHECK(payment_method IN ('naqd','plastik','hisob')),
            received_by TEXT NOT NULL CHECK(received_by IN ('worker','accountant')),
            receiver_id INTEGER NOT NULL REFERENCES users(id),
            operation_token TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(operation_token,opening_debt_id))""")
        con.execute('CREATE INDEX IF NOT EXISTS idx_opening_debt_receipts_debt ON opening_debt_receipts(opening_debt_id)')
        con.execute("""CREATE TABLE IF NOT EXISTS invoices (
            id INTEGER PRIMARY KEY, sale_id INTEGER NOT NULL UNIQUE,
            category TEXT NOT NULL, month_key TEXT NOT NULL,
            number INTEGER NOT NULL, issued_date TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(category, month_key, number),
            FOREIGN KEY(sale_id) REFERENCES sales(id))""")
        con.execute("""CREATE TABLE IF NOT EXISTS sale_cancellations (sale_id INTEGER PRIMARY KEY, reason TEXT NOT NULL, cancelled_at TEXT NOT NULL, user_id INTEGER)""")
        con.execute("""CREATE TABLE IF NOT EXISTS stock_alerts (product TEXT PRIMARY KEY, min_kg REAL NOT NULL DEFAULT 500)""")
        # Safe migrations: preserve existing records and add cleaning/byproduct fields.
        wheat_cols = {r['name'] for r in con.execute('PRAGMA table_info(wheat_receipts)').fetchall()}
        if 'cleaned_net' not in wheat_cols:
            con.execute('ALTER TABLE wheat_receipts ADD COLUMN cleaned_net REAL')
            con.execute('UPDATE wheat_receipts SET cleaned_net=net WHERE cleaned_net IS NULL')
        prod_cols = {r['name'] for r in con.execute('PRAGMA table_info(production)').fetchall()}
        if 'muchka_kg' not in prod_cols:
            con.execute('ALTER TABLE production ADD COLUMN muchka_kg REAL NOT NULL DEFAULT 0')
        if 'telashka_kg' not in prod_cols:
            con.execute('ALTER TABLE production ADD COLUMN telashka_kg REAL NOT NULL DEFAULT 0')
        # New purchase and cleaning fields; old rows remain unchanged.
        if 'purchase_method' not in wheat_cols:
            con.execute("ALTER TABLE wheat_receipts ADD COLUMN purchase_method TEXT NOT NULL DEFAULT 'Oldingi kirim'")
        con.execute("""CREATE TABLE IF NOT EXISTS wheat_purchase_payments (
            id INTEGER PRIMARY KEY,
            wheat_receipt_id INTEGER NOT NULL UNIQUE,
            accountant_id INTEGER NOT NULL,
            payment_method TEXT NOT NULL CHECK(payment_method IN ('naqd','hisob')),
            amount REAL NOT NULL CHECK(amount>0),
            payment_date TEXT NOT NULL,
            operation_token TEXT NOT NULL UNIQUE,
            FOREIGN KEY(wheat_receipt_id) REFERENCES wheat_receipts(id),
            FOREIGN KEY(accountant_id) REFERENCES users(id))""")
        if 'raw_wheat_kg' not in prod_cols:
            con.execute('ALTER TABLE production ADD COLUMN raw_wheat_kg REAL')
            con.execute('UPDATE production SET raw_wheat_kg=wheat_kg WHERE raw_wheat_kg IS NULL')
        # Har bir un turi va qora un uchun alohida ishlab chiqarish hisobi.
        con.execute("""CREATE TABLE IF NOT EXISTS production_flour_types (
            production_id INTEGER NOT NULL, product TEXT NOT NULL, qty_kg REAL NOT NULL DEFAULT 0,
            PRIMARY KEY(production_id, product), FOREIGN KEY(production_id) REFERENCES production(id))""")
        con.execute("""CREATE TABLE IF NOT EXISTS bagged_production_lines (
            production_id INTEGER NOT NULL,
            product TEXT NOT NULL,
            bag_kg INTEGER NOT NULL CHECK(bag_kg IN (25,50)),
            bag_count INTEGER NOT NULL CHECK(typeof(bag_count)='integer' AND bag_count>0),
            PRIMARY KEY(production_id,product,bag_kg),
            FOREIGN KEY(production_id) REFERENCES production(id))""")
        con.execute("""CREATE TABLE IF NOT EXISTS bagged_sale_deductions (
            sale_id INTEGER PRIMARY KEY,
            product TEXT NOT NULL,
            bag_kg INTEGER NOT NULL CHECK(bag_kg IN (25,50)),
            bag_count INTEGER NOT NULL CHECK(typeof(bag_count)='integer' AND bag_count>0),
            FOREIGN KEY(sale_id) REFERENCES sales(id))""")
        sale_cols2 = {r['name'] for r in con.execute('PRAGMA table_info(sales)').fetchall()}
        if 'sale_mode' not in sale_cols2:
            con.execute("ALTER TABLE sales ADD COLUMN sale_mode TEXT NOT NULL DEFAULT 'kg'")
        if 'bag_kg' not in sale_cols2:
            con.execute('ALTER TABLE sales ADD COLUMN bag_kg REAL')
        if 'bag_count' not in sale_cols2:
            con.execute('ALTER TABLE sales ADD COLUMN bag_count REAL')
        if 'unit_price' not in sale_cols2:
            con.execute('ALTER TABLE sales ADD COLUMN unit_price REAL')
        # Migrate existing databases safely; old sales remain intact.
        sale_cols = {r['name'] for r in con.execute('PRAGMA table_info(sales)').fetchall()}
        if 'customer_phone' not in sale_cols:
            con.execute("ALTER TABLE sales ADD COLUMN customer_phone TEXT NOT NULL DEFAULT ''")
        if 'due_date' not in sale_cols:
            con.execute("ALTER TABLE sales ADD COLUMN due_date TEXT NOT NULL DEFAULT ''")
        for row in con.execute(
                "SELECT id,date FROM sales WHERE due_date='' OR due_date IS NULL").fetchall():
            con.execute('UPDATE sales SET due_date=? WHERE id=?',
                        (_business_date_after(row['date'], 25), row['id']))
        cols = {r['name'] for r in con.execute('PRAGMA table_info(users)')}
        if 'phone' not in cols: con.execute("ALTER TABLE users ADD COLUMN phone TEXT NOT NULL DEFAULT ''")
        if 'active' not in cols: con.execute('ALTER TABLE users ADD COLUMN active INTEGER NOT NULL DEFAULT 1')
        con.execute("""CREATE TABLE IF NOT EXISTS user_audit (
            id INTEGER PRIMARY KEY, actor_user_id INTEGER, target_user_id INTEGER,
            action TEXT NOT NULL, details TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")

        con.execute("""CREATE TABLE IF NOT EXISTS worker_receipts (id INTEGER PRIMARY KEY, sale_id INTEGER NOT NULL, worker_id INTEGER NOT NULL, amount REAL NOT NULL CHECK(amount>0), date TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(sale_id) REFERENCES sales(id))""")
        con.execute("""CREATE TABLE IF NOT EXISTS worker_receipt_operations (
            token TEXT PRIMARY KEY, worker_id INTEGER NOT NULL, sale_id INTEGER NOT NULL,
            amount REAL NOT NULL CHECK(amount>0), payment_method TEXT NOT NULL,
            receipt_id INTEGER NOT NULL UNIQUE,
            FOREIGN KEY(receipt_id) REFERENCES worker_receipts(id))""")
        con.execute("""CREATE TABLE IF NOT EXISTS worker_group_receipt_operations (
            token TEXT PRIMARY KEY NOT NULL, worker_id INTEGER NOT NULL,
            customer TEXT NOT NULL, phone_key TEXT NOT NULL,
            amount_cents INTEGER NOT NULL CHECK(amount_cents>0), payment_method TEXT NOT NULL)""")
        con.execute("""CREATE TABLE IF NOT EXISTS worker_group_receipt_operation_items (
            token TEXT NOT NULL, receipt_id INTEGER PRIMARY KEY,
            FOREIGN KEY(token) REFERENCES worker_group_receipt_operations(token),
            FOREIGN KEY(receipt_id) REFERENCES worker_receipts(id))""")
        con.execute("""CREATE TABLE IF NOT EXISTS accountant_payment_operations (
            token TEXT PRIMARY KEY NOT NULL, accountant_id INTEGER NOT NULL,
            payload TEXT NOT NULL)""")
        con.execute("""CREATE TABLE IF NOT EXISTS worker_expense_operations (
            token TEXT PRIMARY KEY NOT NULL, worker_id INTEGER NOT NULL,
            payload TEXT NOT NULL, expense_id INTEGER NOT NULL UNIQUE,
            expense_date TEXT NOT NULL,
            FOREIGN KEY(expense_id) REFERENCES expenses(id))""")
        con.execute("""CREATE TABLE IF NOT EXISTS accountant_expense_operations (
            token TEXT PRIMARY KEY NOT NULL, accountant_id INTEGER NOT NULL,
            payload TEXT NOT NULL, expense_id INTEGER NOT NULL UNIQUE,
            expense_date TEXT NOT NULL,
            FOREIGN KEY(expense_id) REFERENCES expenses(id))""")
        con.execute("""CREATE TABLE IF NOT EXISTS user_status_versions (
            user_id INTEGER PRIMARY KEY, version INTEGER NOT NULL CHECK(version>=0),
            FOREIGN KEY(user_id) REFERENCES users(id))""")
        con.execute("""CREATE TABLE IF NOT EXISTS remaining_write_operations (
            token TEXT PRIMARY KEY NOT NULL, actor_id INTEGER NOT NULL,
            action TEXT NOT NULL, payload TEXT NOT NULL, result_id INTEGER NOT NULL)""")
        con.execute("""CREATE TABLE IF NOT EXISTS sale_edit_versions (
            sale_id INTEGER PRIMARY KEY, version INTEGER NOT NULL CHECK(version>=0),
            FOREIGN KEY(sale_id) REFERENCES sales(id))""")
        con.execute("""CREATE TABLE IF NOT EXISTS worker_single_sale_operations (
            token TEXT PRIMARY KEY NOT NULL, user_id INTEGER NOT NULL,
            payload TEXT NOT NULL, sale_id INTEGER NOT NULL UNIQUE,
            invoice_id INTEGER NOT NULL UNIQUE,
            FOREIGN KEY(sale_id) REFERENCES sales(id),
            FOREIGN KEY(invoice_id) REFERENCES invoices(id))""")
        con.execute("""CREATE TABLE IF NOT EXISTS accountant_sale_operations (
            token TEXT PRIMARY KEY NOT NULL, user_id INTEGER NOT NULL,
            payload TEXT NOT NULL, sale_id INTEGER NOT NULL UNIQUE,
            invoice_id INTEGER NOT NULL UNIQUE,
            FOREIGN KEY(sale_id) REFERENCES sales(id),
            FOREIGN KEY(invoice_id) REFERENCES invoices(id))""")
        con.execute("""CREATE TABLE IF NOT EXISTS account_cash_transfers (
            id INTEGER PRIMARY KEY,
            date TEXT NOT NULL,
            from_channel TEXT NOT NULL CHECK(from_channel IN ('naqd','plastik','hisob')),
            to_channel TEXT NOT NULL CHECK(to_channel IN ('naqd','plastik','hisob')),
            amount REAL NOT NULL CHECK(amount > 0),
            note TEXT NOT NULL DEFAULT '',
            entered_by INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            operation_token TEXT NOT NULL UNIQUE,
            payload TEXT NOT NULL,
            CHECK(from_channel <> to_channel),
            FOREIGN KEY(entered_by) REFERENCES users(id))""")
        con.execute("""CREATE TABLE IF NOT EXISTS accountant_cash_additions (
            id INTEGER PRIMARY KEY,
            date TEXT NOT NULL,
            amount REAL NOT NULL CHECK(amount > 0),
            payment_method TEXT NOT NULL CHECK(payment_method IN ('naqd','hisob')),
            source TEXT NOT NULL CHECK(length(trim(source)) > 0),
            note TEXT NOT NULL DEFAULT '',
            entered_by INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            operation_token TEXT NOT NULL UNIQUE,
            payload TEXT NOT NULL,
            FOREIGN KEY(entered_by) REFERENCES users(id))""")
        con.execute("""CREATE TABLE IF NOT EXISTS opening_product_stocks (
            product TEXT PRIMARY KEY,
            stock_date TEXT NOT NULL,
            qty_kg REAL NOT NULL CHECK(qty_kg > 0),
            note TEXT NOT NULL DEFAULT '',
            entered_by INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(entered_by) REFERENCES users(id))""")
        con.execute("""CREATE TABLE IF NOT EXISTS opening_bagged_product_stocks (
            product TEXT PRIMARY KEY,
            stock_date TEXT NOT NULL,
            bag_25_count INTEGER NOT NULL DEFAULT 0
                CHECK(typeof(bag_25_count)='integer' AND bag_25_count>=0),
            bag_50_count INTEGER NOT NULL DEFAULT 0
                CHECK(typeof(bag_50_count)='integer' AND bag_50_count>=0),
            note TEXT NOT NULL DEFAULT '',
            entered_by INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK(bag_25_count + bag_50_count > 0),
            FOREIGN KEY(entered_by) REFERENCES users(id))""")
        con.execute("""CREATE TABLE IF NOT EXISTS production_operations (
            token TEXT PRIMARY KEY NOT NULL, user_id INTEGER NOT NULL,
            payload TEXT NOT NULL, production_id INTEGER NOT NULL UNIQUE,
            FOREIGN KEY(production_id) REFERENCES production(id))""")
        con.execute("""CREATE TABLE IF NOT EXISTS accountant_payment_operation_items (
            token TEXT NOT NULL, receipt_id INTEGER PRIMARY KEY,
            accountant_receipt_id INTEGER NOT NULL UNIQUE,
            FOREIGN KEY(token) REFERENCES accountant_payment_operations(token),
            FOREIGN KEY(receipt_id) REFERENCES worker_receipts(id),
            FOREIGN KEY(accountant_receipt_id) REFERENCES accountant_worker_receipts(id))""")
        con.execute("""CREATE TABLE IF NOT EXISTS worker_transfers (id INTEGER PRIMARY KEY, worker_id INTEGER NOT NULL, amount REAL NOT NULL CHECK(amount>0), status TEXT NOT NULL DEFAULT 'pending', created_at TEXT DEFAULT CURRENT_TIMESTAMP, decided_at TEXT, decided_by INTEGER, CHECK(status IN ('pending','approved','rejected')))""")
        # 4-C: payment channels. Existing transactions remain 'naqd'; no historic sums are changed.
        for table in ('worker_receipts', 'sales', 'expenses', 'worker_transfers'):
            columns = {r['name'] for r in con.execute('PRAGMA table_info('+table+')')}
            if 'payment_method' not in columns:
                con.execute("ALTER TABLE "+table+" ADD COLUMN payment_method TEXT NOT NULL DEFAULT 'naqd'")
        sale_cash_columns = {r['name'] for r in con.execute('PRAGMA table_info(sales)')}
        if 'initial_payment_recipient_id' not in sale_cash_columns:
            con.execute('ALTER TABLE sales ADD COLUMN initial_payment_recipient_id INTEGER REFERENCES users(id)')

        con.execute("""CREATE TABLE IF NOT EXISTS accountant_worker_receipts (
            id INTEGER PRIMARY KEY, receipt_id INTEGER NOT NULL UNIQUE,
            worker_id INTEGER NOT NULL, accountant_id INTEGER NOT NULL,
            customer TEXT NOT NULL, customer_phone TEXT NOT NULL,
            payment_method TEXT NOT NULL, amount REAL NOT NULL,
            payment_date TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(receipt_id) REFERENCES worker_receipts(id))""")
        con.execute("""CREATE TABLE IF NOT EXISTS worker_sale_batches (
            id INTEGER PRIMARY KEY, worker_id INTEGER NOT NULL, customer TEXT NOT NULL,
            customer_phone TEXT NOT NULL, sale_date TEXT NOT NULL,
            total REAL NOT NULL, paid REAL NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        con.execute("""CREATE TABLE IF NOT EXISTS worker_sale_batch_items (
            batch_id INTEGER NOT NULL, sale_id INTEGER NOT NULL UNIQUE,
            PRIMARY KEY(batch_id,sale_id),
            FOREIGN KEY(batch_id) REFERENCES worker_sale_batches(id),
            FOREIGN KEY(sale_id) REFERENCES sales(id))""")
        con.execute("""CREATE TABLE IF NOT EXISTS worker_sale_batch_payments (
            id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL,
            payment_method TEXT NOT NULL CHECK(payment_method IN ('naqd','plastik','hisob')),
            amount REAL NOT NULL CHECK(amount>0), created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(batch_id) REFERENCES worker_sale_batches(id))""")
        # One-time submission lock: prevents browser Back / double-click duplicate sales.
        con.execute("""CREATE TABLE IF NOT EXISTS worker_sale_submission_locks (
            token TEXT PRIMARY KEY, worker_id INTEGER NOT NULL,
            batch_id INTEGER NOT NULL UNIQUE,
            FOREIGN KEY(batch_id) REFERENCES worker_sale_batches(id))""")
        con.execute("""CREATE TABLE IF NOT EXISTS customers (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, phone TEXT NOT NULL,
            phone_key TEXT NOT NULL UNIQUE, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        con.execute("""CREATE TABLE IF NOT EXISTS customer_audit (
            id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL, actor_user_id INTEGER NOT NULL,
            old_name TEXT, new_name TEXT, old_phone TEXT, new_phone TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        con.execute('CREATE INDEX IF NOT EXISTS idx_customers_name ON customers(name COLLATE NOCASE)')
        # Import only legacy entries with a valid, unambiguous phone. Never change historical sales/debts.
        for row in con.execute("""SELECT customer AS name, customer_phone AS phone FROM sales
                WHERE TRIM(customer_phone)<>'' UNION ALL
                SELECT customer AS name, customer_phone AS phone FROM opening_debts
                WHERE TRIM(customer_phone)<>''""").fetchall():
            key=phone_key(row['phone'])
            if key and not con.execute('SELECT id FROM customers WHERE phone_key=?',(key,)).fetchone():
                con.execute('INSERT INTO customers(name,phone,phone_key) VALUES(?,?,?)',
                            (row['name'].strip(),row['phone'].strip(),key))

# Role checks must be enforced on the server, not only in HTML navigation.
BOSS_READ_ENDPOINTS = {
    'dashboard', 'boss_statistics', 'wheat', 'production_page', 'sales_page', 'invoices_page',
    'invoice_saved', 'invoice_print', 'debtors_page', 'payments_page', 'unified_excel_report',
    'expenses_page', 'reports', 'warehouses', 'excel_report', 'customer_statement',
    'cash_page', 'control_page', 'daily_page', 'finance_page', 'worker_admin', 'combined_finance_page', 'monthly_finance_excel', 'cashflow_test_page', 'worker_batch_invoice', 'worker_transfers_excel', 'worker_sale_result'
}

def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not session.get('user_id'):
            return redirect(url_for('login'))
        user = current_user()
        if user is None or not user['active']:
            language = get_language(session.get('ui_language'))
            session.clear()
            session['ui_language'] = language
            return redirect(url_for('login'))
        if session.get('auth_version') != user['auth_version']:
            language = get_language(session.get('ui_language'))
            session.clear()
            session['ui_language'] = language
            return redirect(url_for('login'))
        role, endpoint = user['role'], request.endpoint
        if role in ('umid', 'maxmud', 'ishchi'):
            # Until financial ownership is migrated, never expose global records.
            if endpoint not in ('dashboard', 'change_password', 'worker_sales', 'worker_new_sale', 'worker_receipt', 'worker_expense', 'worker_transfer', 'worker_customer_search', 'worker_customer_edit', 'worker_sale_form', 'worker_debts', 'worker_archive', 'worker_customers_page', 'worker_cash_page', 'worker_group_receipt', 'worker_stock_status', 'worker_multi_sale', 'worker_batch_invoice', 'worker_sale_result') and not (role == 'umid' and endpoint == 'production_page'):
                abort(403, description='Xodimning shaxsiy hisobi keyingi bosqichda ishga tushadi.')
        elif role == 'rahbar':
            if endpoint == 'change_password':
                pass
            elif request.method != 'GET' or endpoint not in BOSS_READ_ENDPOINTS:
                abort(403, description='Boshliq faqat ma’lumotlarni ko‘ra oladi.')
        elif role != 'buxgalter':
            abort(403)
        if endpoint == 'production_page':
            if role not in ('umid', 'rahbar') or (role == 'rahbar' and request.method != 'GET'):
                abort(403, description='Ishlab chiqarish faqat Umidga biriktirilgan.')
        return fn(*args, **kwargs)
    return wrapped

@app.context_processor
def csrf_context():
    def csrf_token():
        if 'csrf_token' not in session:
            session['csrf_token'] = secrets.token_urlsafe(32)
        return session['csrf_token']
    return {'csrf_token': csrf_token}

@app.before_request
def check_csrf():
    if (session.get('role') == 'rahbar'
            and request.method not in ('GET', 'HEAD', 'OPTIONS')
            and request.endpoint not in ('set_language', 'logout')):
        abort(403, description='Boshliq hech qanday o‘zgartirish amalini bajara olmaydi.')
    if request.method == 'POST':
        expected = session.get('csrf_token', '')
        supplied = request.form.get('csrf_token', '')
        if not expected or not hmac.compare_digest(expected, supplied):
            abort(400, description='Sahifa eskirgan. Qayta ochib urinib ko‘ring.')

def current_user():
    if not session.get('user_id'): return None
    with db() as con:
        return con.execute('''SELECT users.*,
                COALESCE(user_auth_versions.version,0) AS auth_version
            FROM users
            LEFT JOIN user_auth_versions ON user_auth_versions.user_id=users.id
            WHERE users.id=?''', (session['user_id'],)).fetchone()

@app.context_processor
def inject_globals():
    language = get_language(session.get('ui_language', DEFAULT_LANGUAGE))
    return {
        'current_user': current_user(),
        'roles': ROLES,
        'ui_language': language,
        'ui_date_locale': 'uz-Cyrl-UZ' if language == 'uz-Cyrl' else 'uz-Latn-UZ',
        'language_options': LANGUAGES,
        't': lambda key: translate(key, language),
    }

@app.route('/language', methods=['POST'])
def set_language():
    language = request.form.get('language', '')
    if language not in {code for code, _ in LANGUAGES}:
        abort(400, description=translate('language.invalid', get_language(session.get('ui_language'))))
    session['ui_language'] = language

    from urllib.parse import urlsplit

    target = request.form.get('next', '/')
    parsed = urlsplit(target)
    if (parsed.scheme or parsed.netloc or not parsed.path.startswith('/')
            or parsed.path.startswith('//') or '\\' in target):
        target = '/'
    return redirect(target)

FLOUR_TYPES = ('Sara', 'Yevro', 'Zar Bug‘doy', 'Robiya', 'Qora un')
BAGGED_FLOUR_PRODUCTS = ('Sara', 'Yevro', 'Zar Bug‘doy', 'Robiya')
PRODUCTS = FLOUR_TYPES + ('Kepak', 'Muchka', 'Telashka atxod')
BAG_OPTIONS = {'Sara': (25, 50), 'Yevro': (25, 50), 'Zar Bug‘doy': (25, 50),
               'Robiya': (25, 50), 'Qora un': (30,), 'Kepak': (23,),
               'Muchka': (50,), 'Telashka atxod': ()}
LOOSE_PRODUCTS = ('Kepak', 'Muchka', 'Telashka atxod')

INVOICE_GROUPS = {
    'Robiya': ('oliy', 'Oliy nav un'),
    'Sara': ('birinchi', '1-nav un'),
    'Yevro': ('birinchi', '1-nav un'),
    'Zar Bug‘doy': ('birinchi', '1-nav un'),
    'Qora un': ('qora', 'Qora un'),
    'Kepak': ('kepak', 'Kepak'),
    'Muchka': ('muchka', 'Muchka'),
    'Telashka atxod': ('telashka', 'Teleshka atxod'),
}

def ensure_invoice(con, sale_id, product, sale_date):
    sale_day, _ = _business_date(sale_date)
    existing = con.execute('SELECT id FROM invoices WHERE sale_id=?', (sale_id,)).fetchone()
    if existing:
        return existing['id']
    category = INVOICE_GROUPS.get(product, ('boshqa', 'Boshqa'))[0]
    month_key = sale_day.strftime('%Y-%m')
    # BEGIN IMMEDIATE in caller prevents duplicate numbering under concurrent sales.
    next_num = con.execute('SELECT COALESCE(MAX(number),0)+1 FROM invoices WHERE category=? AND month_key=?',
                           (category, month_key)).fetchone()[0]
    return con.execute('INSERT INTO invoices(sale_id,category,month_key,number,issued_date) VALUES(?,?,?,?,?)',
                       (sale_id,category,month_key,next_num,sale_date)).lastrowid

def product_stocks(con):
    made = {p: 0.0 for p in PRODUCTS}
    for r in con.execute('SELECT product, SUM(qty_kg) AS qty FROM production_flour_types GROUP BY product'):
        if r['product'] in made: made[r['product']] += r['qty'] or 0
    for col, product in [('bran_kg','Kepak'), ('muchka_kg','Muchka'), ('telashka_kg','Telashka atxod')]:
        made[product] += con.execute(f'SELECT COALESCE(SUM({col}),0) FROM production').fetchone()[0]
    # Eski sinov ma'lumotlari yo'qolmasligi uchun eski "Un" alohida qoladi.
    made['Un (eski yozuv)'] = con.execute('SELECT COALESCE(SUM(flour_kg),0) FROM production').fetchone()[0] - sum(made[p] for p in FLOUR_TYPES)
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
            sold_after_opening = con.execute("""
                SELECT COALESCE(SUM(qty_kg),0) FROM sales
                WHERE product=? AND business_date_key(date)>=?
                AND id NOT IN (SELECT sale_id FROM sale_cancellations)
            """, (product, opening_day)).fetchone()[0]
            made[product] = opening_kg + production_after_opening - sold_after_opening
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
    for row in con.execute("""
            SELECT b.product,b.bag_kg,b.bag_count,p.date
            FROM bagged_production_lines b
            JOIN production p ON p.id=b.production_id"""):
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

@app.route('/setup', methods=['GET','POST'])
def setup():
    user = current_user()
    if user is None or not user['active'] or user['role'] != 'buxgalter':
        abort(403)
    if request.method == 'POST':
        username = request.form.get('username','').strip()
        password = request.form.get('password','')
        role = request.form.get('role','umid')
        if not username or len(password) < 12 or role not in ('rahbar', 'buxgalter', 'umid', 'maxmud'):
            flash('Login kiriting, parol kamida 12 belgidan iborat bo‘lsin.', 'error')
        else:
            try:
                with db() as con:
                    con.execute('INSERT INTO users(username,password_hash,role,phone) VALUES(?,?,?,?)',
                        (username,generate_password_hash(password),role,request.form.get('phone','').strip()))
                    con.execute('INSERT INTO user_audit(actor_user_id,target_user_id,action,details) VALUES(?,?,?,?)',
                        (session['user_id'],con.execute('SELECT last_insert_rowid()').fetchone()[0], 'create_user', json.dumps({'username':username,'role':role},ensure_ascii=False)))
                flash('Foydalanuvchi yaratildi.', 'success')
                return redirect(url_for('manage_users'))
            except sqlite3.IntegrityError:
                flash('Bu login band. Boshqa login tanlang.', 'error')
    return render_template('setup.html')

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        with db() as con:
            user = con.execute('''SELECT users.*,
                    COALESCE(user_auth_versions.version,0) AS auth_version
                FROM users
                LEFT JOIN user_auth_versions ON user_auth_versions.user_id=users.id
                WHERE users.username=? COLLATE NOCASE''',
                (request.form.get('username','').strip(),)).fetchone()
        if user and user['active'] and check_password_hash(user['password_hash'], request.form.get('password','')):
            language = get_language(session.get('ui_language'))
            session.clear()
            session['ui_language'] = language
            session.permanent = True
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['role'] = user['role']
            session['auth_version'] = user['auth_version']
            return redirect(url_for('dashboard'))
        flash(translate('login.invalid_credentials', get_language(session.get('ui_language'))), 'error')
    return render_template('login.html')

@app.route('/logout', methods=['POST'])
def logout():
    language = get_language(session.get('ui_language'))
    session.clear()
    session['ui_language'] = language
    return redirect(url_for('login'))

@app.route('/')
@login_required
def dashboard():
    if current_user()['role'] in ('umid','maxmud'):
        return redirect(url_for('worker_sales'))
    if current_user()['role'] == 'ishchi':
        return render_template('worker_pending.html')
    if current_user()['role'] == 'rahbar':
        return redirect(url_for('boss_statistics'))
    with db() as con:
        con.execute('BEGIN')
        financial = calculate_financial_snapshot(con)
        financial_warnings = list(financial['warnings'])
        phones_by_name = {}
        for name, phone in financial['customers']:
            phones_by_name.setdefault(name, set()).add(phone)
        for name, phones in phones_by_name.items():
            if len(phones) > 1:
                financial_warnings.append({'code': 'same_name_multiple_phones', 'customer': name})
        wheat_in = con.execute('SELECT COALESCE(SUM(net),0) FROM wheat_receipts').fetchone()[0]
        wheat_used = con.execute('SELECT COALESCE(SUM(COALESCE(raw_wheat_kg,wheat_kg)),0) FROM production').fetchone()[0]
        stocks = product_stocks(con)
        flour_stock = sum(stocks.get(p,0) for p in FLOUR_TYPES) + stocks.get('Un (eski yozuv)',0)
        bran_stock = stocks.get('Kepak',0)
        muchka_stock = stocks.get('Muchka',0)
        telashka_stock = stocks.get('Telashka atxod',0)
        sales_total = financial['totals']['sales_revenue']
        paid_total = financial['totals']['received']
        expense_total = con.execute('SELECT COALESCE(SUM(amount),0) FROM expenses').fetchone()[0]
        # Dashboard chart: group real sales into five mutually exclusive product groups.
        sales_groups = (
            ('un', 'Un', ('Sara', 'Yevro', 'Zar Bug‘doy', 'Robiya', 'Un')),
            ('qora-un', 'Qora un', ('Qora un',)),
            ('kepak', 'Kepak', ('Kepak',)),
            ('muchka', 'Muchka', ('Muchka',)),
            ('telashka', 'Telashka atxod', ('Telashka atxod',)),
        )
        sales_group_by_product = {
            product: group_key
            for group_key, _, products in sales_groups
            for product in products
        }
        chart_start = date.today() - timedelta(days=6)
        chart_end = date.today()
        daily_rows = con.execute("SELECT business_date_key(date) date, product, SUM(qty_kg) qty FROM sales WHERE business_date_key(date)>=? AND business_date_key(date)<=? AND id NOT IN (SELECT sale_id FROM sale_cancellations) GROUP BY business_date_key(date),product ORDER BY business_date_key(date)", (chart_start.isoformat(), chart_end.isoformat())).fetchall()
        day_map = {(r['date'],r['product']): float(r['qty']) for r in daily_rows}
        daily_sales = []
        for day_offset in range(6, -1, -1):
            chart_day = date.today() - timedelta(days=day_offset)
            day_key = chart_day.isoformat()
            day_sales = {
                'label': chart_day.strftime('%d.%m'),
                **{group_key: 0.0 for group_key, _, _ in sales_groups},
            }
            for (sale_day, product), quantity in day_map.items():
                if sale_day == day_key:
                    group_key = sales_group_by_product.get(product)
                    if group_key is not None:
                        day_sales[group_key] += quantity
            daily_sales.append(day_sales)
        max_daily = max(
            1,
            max(daily[group_key] for daily in daily_sales for group_key, _, _ in sales_groups),
        )
        max_stock = max(wheat_in-wheat_used, flour_stock, bran_stock, muchka_stock, telashka_stock, 1)
        recent_sales = con.execute('SELECT * FROM sales ORDER BY id DESC LIMIT 5').fetchall()
        recent_receipts = con.execute('SELECT * FROM wheat_receipts ORDER BY id DESC LIMIT 5').fetchall()
        debts = financial['totals']['debt']
    return render_template('dashboard.html', wheat_stock=wheat_in-wheat_used, flour_stock=flour_stock, bran_stock=bran_stock, muchka_stock=muchka_stock, telashka_stock=telashka_stock, sales_total=sales_total, paid_total=paid_total, debts=debts, financial_totals=financial['totals'], financial_warnings=financial_warnings, expense_total=expense_total, recent_sales=recent_sales, recent_receipts=recent_receipts, daily_sales=daily_sales, sales_groups=sales_groups, max_daily=max_daily, max_stock=max_stock)


WORKER_PRODUCTS = {
    'umid': ('Sara', 'Yevro', 'Zar Bug‘doy', 'Robiya'),
    'maxmud': ('Kepak', 'Qora un', 'Muchka', 'Telashka atxod'),
}

PAYMENT_METHODS = {'naqd': 'Naqd', 'plastik': 'Plastik', 'hisob': 'Hisobdan o‘tkazish'}
EXPENSE_PAYMENT_METHODS = {'naqd': 'Naqd', 'plastik': 'Plastik', 'hisob': 'Bank'}
EXPENSE_FUND_SOURCES = {
    'umid': 'Umid kassasi',
    'maxmud': 'Maxmud kassasi',
    'buxgalter': 'Buxgalter kassasi',
}


def expense_display(row):
    result = dict(row)
    result['actual_person_display'] = (result.get('actual_person') or '').strip() or 'Aniqlanmagan'
    result['entered_by_display'] = result.get('entered_by_name') or 'Aniqlanmagan'
    result['purpose_display'] = (result.get('purpose') or '').strip() or 'Aniqlanmagan'
    result['payment_method_display'] = EXPENSE_PAYMENT_METHODS.get(
        result.get('payment_method'), 'Aniqlanmagan')
    source = result.get('fund_source')
    if source not in EXPENSE_FUND_SOURCES:
        source = result.get('source_role')
    result['fund_source_display'] = EXPENSE_FUND_SOURCES.get(source, 'Aniqlanmagan')
    return result


def payment_method_from_form(form):
    method = form.get('payment_method', 'naqd')
    if method not in PAYMENT_METHODS:
        raise ValueError('To‘lov turi noto‘g‘ri.')
    return method


def worker_channel_balances(con, uid):
    result = {method: 0.0 for method in PAYMENT_METHODS}
    # Legacy rows without an explicit recipient remain assigned to the seller.
    for r in con.execute("""SELECT s.payment_method method, COALESCE(SUM(s.paid),0) amount
          FROM sales s LEFT JOIN sale_cancellations c ON c.sale_id=s.id
          WHERE COALESCE(s.initial_payment_recipient_id,s.user_id)=? AND c.sale_id IS NULL
          AND NOT EXISTS (SELECT 1 FROM worker_sale_batch_items bi WHERE bi.sale_id=s.id)
          GROUP BY s.payment_method""", (uid,)):
        result[r['method']] = result.get(r['method'], 0) + float(r['amount'])
    for r in con.execute("""SELECT bp.payment_method method, SUM(bp.amount) amount
          FROM worker_sale_batch_payments bp JOIN worker_sale_batches b ON b.id=bp.batch_id
          WHERE b.worker_id=? AND NOT EXISTS (
              SELECT 1 FROM worker_sale_batch_items bi JOIN sale_cancellations c ON c.sale_id=bi.sale_id
              WHERE bi.batch_id=b.id)
          GROUP BY bp.payment_method""", (uid,)):
        result[r['method']] = result.get(r['method'], 0) + float(r['amount'])
    for r in con.execute('SELECT payment_method method, SUM(amount) amount FROM worker_receipts WHERE worker_id=? AND id NOT IN (SELECT receipt_id FROM accountant_worker_receipts) GROUP BY payment_method', (uid,)):
        result[r['method']] = result.get(r['method'], 0) + float(r['amount'])
    for r in con.execute("SELECT payment_method method, SUM(amount) amount FROM opening_debt_receipts WHERE received_by='worker' AND receiver_id=? GROUP BY payment_method", (uid,)):
        result[r['method']] = result.get(r['method'], 0) + float(r['amount'])
    for r in con.execute('SELECT payment_method method, SUM(amount) amount FROM expenses WHERE user_id=? GROUP BY payment_method', (uid,)):
        result[r['method']] = result.get(r['method'], 0) - float(r['amount'])
    for r in con.execute("SELECT payment_method method, SUM(amount) amount FROM worker_transfers WHERE worker_id=? AND status='approved' GROUP BY payment_method", (uid,)):
        result[r['method']] = result.get(r['method'], 0) - float(r['amount'])
    return {method: round(result.get(method, 0.0), 2) for method in PAYMENT_METHODS}


def worker_balance(con, uid):
    return round(sum(worker_channel_balances(con, uid).values()), 2)


def worker_snapshot_obligations(financial, worker_id, customer=None, phone=None):
    normalized_phone = phone_key(phone) or (phone or '')
    obligations = []
    for group in financial['customers'].values():
        if customer is not None and group['customer'] != customer:
            continue
        if phone is not None and group['phone'] != normalized_phone:
            continue
        for item in group['obligations']:
            if item['kind'] in ('sale','opening') and item.get('user_id') == worker_id:
                obligations.append(dict(item, customer=group['customer'],
                                        phone=group['phone']))
    return sorted(obligations, key=lambda item: (item['date'],
        0 if item['kind']=='opening' else 1, item['id']))


def snapshot_has_ambiguous_legacy(financial, customer):
    return any(warning['code'] == 'legacy_identity_ambiguous'
               and warning.get('customer') == customer
               for warning in financial['warnings'])


def balance_cents(balance):
    return int((balance * 100).to_integral_value(rounding=ROUND_DOWN))


def payment_amount_cents(raw_amount):
    amount = float(raw_amount)
    if not math.isfinite(amount) or amount <= 0:
        raise ValueError('To‘lov summasini tekshiring.')
    cents = round(amount * 100)
    if cents <= 0 or abs(amount * 100 - cents) > 0.001:
        raise ValueError('Summani tiyingacha aniq kiriting.')
    return cents


def worker_context():
    user = current_user()
    if user['role'] not in WORKER_PRODUCTS: abort(403)
    with db() as con:
        con.execute('BEGIN')
        sale_select = """SELECT s.*, CASE WHEN c.sale_id IS NULL THEN 0 ELSE 1 END cancelled,
            COALESCE((SELECT SUM(r.amount) FROM worker_receipts r WHERE r.sale_id=s.id),0) receipts
            FROM sales s LEFT JOIN sale_cancellations c ON c.sale_id=s.id
            WHERE s.user_id=? ORDER BY s.id DESC"""
        if request.endpoint == 'worker_archive':
            rows,rows_pagination=paginate_rows(con,sale_select,
                'SELECT COUNT(*) FROM sales WHERE user_id=?',(user['id'],))
        else:
            rows=con.execute(sale_select,(user['id'],)).fetchall()
            rows_pagination=None
        active_count=con.execute("""SELECT COUNT(*) FROM sales s
            WHERE s.user_id=? AND NOT EXISTS(
                SELECT 1 FROM sale_cancellations c WHERE c.sale_id=s.id)""",
            (user['id'],)).fetchone()[0]
        financial = calculate_financial_snapshot(con)
        customer_context = customer_identity_context(con)
        obligations = worker_snapshot_obligations(financial, user['id'])
        obligations_by_sale = {item['id']: item for item in obligations}
        display_rows = []
        for row in rows:
            current_name, current_phone = customer_display(
                customer_context, row['customer'], row['customer_phone'])
            display_rows.append(dict(row, customer=current_name, customer_phone=current_phone,
                financial_balance=obligations_by_sale.get(row['id'], {}).get('balance', Decimal('0')),
                legacy_estimated_amount=obligations_by_sale.get(row['id'], {}).get('legacy_applied', Decimal('0'))))
        rows = display_rows
        channel_balances = worker_channel_balances(con,user['id'])
        balance = round(sum(channel_balances.values()), 2)
        recent_receipts = con.execute("""SELECT r.date, r.amount, r.payment_method, s.customer,
                        s.customer_phone, s.product
                        FROM worker_receipts r JOIN sales s ON s.id=r.sale_id
                        WHERE r.worker_id=? ORDER BY r.id DESC LIMIT 30""", (user['id'],)).fetchall()
        opening_recent = con.execute("""SELECT r.date,r.amount,r.payment_method,d.customer,d.customer_phone,
                'Eski qarz' product FROM opening_debt_receipts r
                JOIN opening_debts d ON d.id=r.opening_debt_id
                WHERE r.worker_id=? AND r.received_by='worker' ORDER BY r.id DESC LIMIT 30""", (user['id'],)).fetchall()
        recent_receipts = [
            dict(row, customer=customer_display(
                customer_context, row['customer'], row['customer_phone'])[0])
            for row in list(recent_receipts) + list(opening_recent)
        ]
        recent_receipts.sort(key=lambda row: _business_date(row['date'])[0],reverse=True)
        recent_receipts = recent_receipts[:30]
        expenses = [expense_display(row) for row in con.execute("""SELECT e.*,
                        entered.username AS entered_by_name, source.role AS source_role
                        FROM expenses e
                        LEFT JOIN users entered ON entered.id=COALESCE(e.entered_by,e.user_id)
                        LEFT JOIN users source ON source.id=e.user_id
                        WHERE e.user_id=? ORDER BY e.id DESC LIMIT 30""", (user['id'],))]
        transfers = con.execute('SELECT * FROM worker_transfers WHERE worker_id=? ORDER BY id DESC LIMIT 30',(user['id'],)).fetchall()
    active = [r for r in rows if not r['cancelled']]
    total_debt = sum((item['balance'] for item in obligations), Decimal('0'))
    groups = {}
    for item in obligations:
        if item['balance'] <= Decimal('0.009'):
            continue
        key = (item['customer'].strip(), item['phone'])
        group = groups.setdefault(key, {'debt': Decimal('0'), 'legacy_estimated': Decimal('0')})
        group['debt'] += item['balance']
        group['legacy_estimated'] += item['legacy_applied']
    debt_groups = [{
        'customer': name, 'phone': phone, 'debt': round(amounts['debt'], 2),
        'legacy_estimated_amount': round(amounts['legacy_estimated'], 2),
        'payment_blocked': snapshot_has_ambiguous_legacy(financial, name),
    } for (name, phone), amounts in sorted(groups.items())]
    legacy_payment_warning = any(item['payment_blocked'] for item in debt_groups)
    if request.endpoint == 'worker_debts':
        debt_groups,debt_groups_pagination=paginate_items(
            debt_groups,'debt_page','debt_per_page')
    else:
        debt_groups_pagination=None
    legacy_estimated_total = sum((item['legacy_applied'] for item in obligations), Decimal('0'))
    return dict(rows=rows, products=WORKER_PRODUCTS[user['role']], role_name=ROLES[user['role']],
                 balance=balance, channel_balances=channel_balances, payment_methods=PAYMENT_METHODS,
                 recent_receipts=recent_receipts, expenses=expenses, transfers=transfers, bag_options=BAG_OPTIONS,
                loose_products=LOOSE_PRODUCTS, active_count=active_count, total_debt=total_debt,
                debt_groups=debt_groups, legacy_estimated_total=legacy_estimated_total,
                legacy_payment_warning=legacy_payment_warning,
                rows_pagination=rows_pagination,
                debt_groups_pagination=debt_groups_pagination)

@app.route('/xodim/sotuvlar')
@login_required
def worker_sales():
    return render_template('worker_home.html', **worker_context())

@app.route('/xodim/ombor-qoldiq')
@login_required
def worker_stock_status():
    user=current_user()
    if user['role'] not in WORKER_PRODUCTS: abort(403)
    with db() as con:
        stocks=product_stocks(con)
        bag_stocks=bagged_product_stocks(con)
    products=WORKER_PRODUCTS[user['role']]
    return {
        'stocks': {p: round(float(stocks.get(p,0)), 4) for p in products},
        'bag_stocks': {p:bag_stocks[p] for p in products if p in bag_stocks},
    }


@app.route('/xodim/yangi-sotuv-sahifa')
@login_required
def worker_sale_form():
    session['worker_sale_token'] = secrets.token_urlsafe(32)
    return render_template('worker_new_page.html', sale_token=session['worker_sale_token'], **worker_context())

@app.route('/xodim/qarzlar')
@login_required
def worker_debts():
    return render_template('worker_debt_page.html', **worker_context())

@app.route('/xodim/arxiv')
@login_required
def worker_archive():
    return render_template('worker_archive_page.html', **worker_context())

@app.route('/xodim/xaridorlar')
@login_required
def worker_customers_page():
    return render_template('worker_customer_page.html', **worker_context())

@app.route('/xodim/kassa')
@login_required
def worker_cash_page():
    return render_template('worker_cash_page.html', **worker_context())


@app.route('/xodim/xaridor-qidirish')
@login_required
def worker_customer_search():
    user=current_user()
    if user['role'] not in WORKER_PRODUCTS: abort(403)
    q=request.args.get('q','').strip()[:80]
    if len(q)<2: return {'customers': []}
    digits=re.sub(r'\D','',q)
    with db() as con:
        if len(digits)>=3 and len(digits)>=len(q.replace(' ', '').replace('+','').replace('-','')):
            rows=con.execute('SELECT id,name,phone FROM customers WHERE phone_key LIKE ? ORDER BY name LIMIT 12',('%'+digits+'%',)).fetchall()
        else:
            rows=con.execute('SELECT id,name,phone FROM customers WHERE name LIKE ? COLLATE NOCASE ORDER BY name LIMIT 12',('%'+q+'%',)).fetchall()
    return {'customers':[dict(r) for r in rows]}

@app.route('/xodim/xaridor-tahrirlash', methods=['POST'])
@login_required
def worker_customer_edit():
    user=current_user()
    if user['role'] not in WORKER_PRODUCTS and user['role']!='buxgalter': abort(403)
    try:
        raw_cid=request.form.get('customer_id','').strip()
        if not raw_cid.isdecimal() or int(raw_cid)<=0:
            raise ValueError('Avval yuqoridagi qidiruvdan xaridorni tanlang.')
        cid=int(raw_cid)
        name=request.form.get('customer','').strip()
        phone=request.form.get('customer_phone','').strip()
        key=phone_key(phone)
        if not name or not key: raise ValueError('Ism va to‘g‘ri telefon raqami majburiy.')
        with db() as con:
            con.execute('BEGIN IMMEDIATE')
            old=con.execute('SELECT * FROM customers WHERE id=?',(cid,)).fetchone()
            if not old: raise ValueError('Xaridor topilmadi.')
            other=con.execute('SELECT id FROM customers WHERE phone_key=? AND id<>?',(key,cid)).fetchone()
            if other: raise ValueError('Bu telefon boshqa xaridorga biriktirilgan.')
            con.execute('UPDATE customers SET name=?,phone=?,phone_key=? WHERE id=?',(name,phone,key,cid))
            con.execute('INSERT INTO customer_audit(customer_id,actor_user_id,old_name,new_name,old_phone,new_phone) VALUES(?,?,?,?,?,?)',
                        (cid,user['id'],old['name'],name,old['phone'],phone))
        flash('Xaridor kartasi yangilandi. Tarixiy yozuvlar o‘zgartirilmaydi, lekin joriy hisobotlarda shu xaridor sifatida ko‘rinadi.','success')
    except (ValueError,sqlite3.Error) as exc: flash(str(exc),'error')
    return redirect(url_for('worker_sales') if user['role'] in WORKER_PRODUCTS else url_for('dashboard'))

@app.context_processor
def worker_single_sale_context():
    # Legacy single-sale endpoint only; the current menu uses worker_multi_sale.
    def worker_single_sale_token():
        return URLSafeSerializer(app.secret_key, salt='worker-single-sale-operation-v1').dumps({
            'action': 'worker_single_sale', 'user_id': session['user_id'],
            'nonce': secrets.token_urlsafe(32)})
    return {'worker_single_sale_token': worker_single_sale_token}


@app.route('/xodim/yangi-sotuv', methods=['POST'])
@login_required
def worker_new_sale():
    user=current_user()
    if user['role'] not in WORKER_PRODUCTS: abort(403)
    try:
        product,mode,bag_kg,count,unit_price,qty,total,paid=sale_values(request.form)
        payment_method = payment_method_from_form(request.form)
        if product not in WORKER_PRODUCTS[user['role']]: raise ValueError('Bu mahsulot sizga biriktirilmagan.')
        customer=request.form.get('customer','').strip(); phone=request.form.get('customer_phone','').strip()
        key=phone_key(phone)
        if not customer or not key: raise ValueError('Xaridor ismi va to‘g‘ri telefon raqami majburiy.')
        selected=request.form.get('customer_id','').strip()
        today=date.today().isoformat()
        with db() as con:
            con.execute('BEGIN IMMEDIATE')
            token = request.form.get('operation_token', '')
            try:
                operation = URLSafeSerializer(app.secret_key, salt='worker-single-sale-operation-v1').loads(token)
            except BadSignature:
                raise ValueError('Sotuv tokeni yaroqsiz. Formani qayta oching.')
            if (not isinstance(operation, dict) or operation.get('action') != 'worker_single_sale'
                    or operation.get('user_id') != user['id']):
                raise ValueError('Sotuv tokeni bu xodimga tegishli emas.')
            payload = json.dumps({key: request.form.getlist(key) for key in request.form
                if key not in ('csrf_token', 'operation_token')},
                ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            previous = con.execute('SELECT * FROM worker_single_sale_operations WHERE token=?', (token,)).fetchone()
            if previous:
                if previous['user_id'] != user['id'] or previous['payload'] != payload:
                    raise ValueError('Bu token boshqa sotuv mazmuni uchun ishlatilgan.')
                flash('Sotuv saqlandi. Naqd tushum shaxsiy kassangizga yozildi.','success')
                return redirect(url_for('worker_sale_form'))
            existing=con.execute('SELECT * FROM customers WHERE phone_key=?',(key,)).fetchone()
            if selected:
                card=con.execute('SELECT * FROM customers WHERE id=?',(int(selected),)).fetchone()
                if not card: raise ValueError('Xaridor kartasi topilmadi.')
                if card['phone_key']!=key or card['name']!=customer:
                    raise ValueError('Xaridor ma’lumoti o‘zgargan. Qayta tanlang yoki kartani tahrirlang.')
                customer,phone=card['name'],card['phone']
            elif existing:
                raise ValueError('Bu telefon bazada mavjud. Ro‘yxatdan xaridorni tanlang.')
            else:
                con.execute('INSERT INTO customers(name,phone,phone_key) VALUES(?,?,?)',(customer,phone,key))
            validate_bagged_sale(con,product,mode,bag_kg,count)
            if qty > product_stocks(con).get(product,0)+0.000001: raise ValueError('Omborda yetarli mahsulot yo‘q.')
            today_storage = _business_date_storage(today)
            due=_business_date_storage(_business_date_after(today,25)) if total-paid>0.009 else ''
            cur=con.execute("""INSERT INTO sales(date,customer,customer_phone,due_date,product,qty_kg,price_per_kg,vat_rate,paid,note,user_id,sale_mode,bag_kg,bag_count,unit_price,payment_method)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(today_storage,customer,phone,due,product,qty,total/qty,0,paid,request.form.get('note','').strip(),user['id'],mode,bag_kg,count,unit_price,payment_method))
            record_bagged_sale(con,cur.lastrowid,product,mode,bag_kg,count)
            invoice_id = ensure_invoice(con,cur.lastrowid,product,today_storage)
            con.execute('INSERT INTO worker_single_sale_operations(token,user_id,payload,sale_id,invoice_id) VALUES(?,?,?,?,?)',
                        (token,user['id'],payload,cur.lastrowid,invoice_id))
        flash('Sotuv saqlandi. Naqd tushum shaxsiy kassangizga yozildi.','success')
    except (ValueError,KeyError,OverflowError,sqlite3.Error) as exc: flash(str(exc) or 'Sotuvni tekshiring.','error')
    return redirect(url_for('worker_sale_form'))

@app.route('/xodim/ko-p-mahsulotli-sotuv', methods=['POST'])
@login_required
def worker_multi_sale():
    user = current_user()
    if user['role'] not in WORKER_PRODUCTS: abort(403)
    submission_token = request.form.get('submission_token', '')
    if not submission_token or not hmac.compare_digest(submission_token, session.get('worker_sale_token','')):
        flash('Bu sotuv oldin yuborilgan yoki sahifa eskirgan. Yangi sotuv sahifasini oching.', 'error')
        return redirect(url_for('worker_sales'))
    try:
        customer = request.form.get('customer','').strip()
        phone = request.form.get('customer_phone','').strip()
        key = phone_key(phone)
        if not customer or not key:
            raise ValueError('Xaridorning ismi va to‘g‘ri telefon raqami majburiy.')
        selected = request.form.get('customer_id','').strip()
        raw_items = request.form.get('items_json','')
        if len(raw_items)>30000: raise ValueError('Mahsulotlar soni juda ko‘p.')
        items = json.loads(raw_items)
        if not isinstance(items,list) or not 1<=len(items)<=15:
            raise ValueError('1 dan 15 tagacha mahsulot kiriting.')
        clean = []
        stocks_needed = {}
        bag_counts_needed = {}
        for item in items:
            if not isinstance(item,dict): raise ValueError('Mahsulot ma’lumoti noto‘g‘ri.')
            class LineForm(dict):
                pass
            row = LineForm(product=item.get('product'),sale_mode=item.get('sale_mode'),
                 bag_kg=item.get('bag_kg'),bag_count=item.get('bag_count'),
                 qty_kg=item.get('qty_kg'),price=item.get('price'),paid='0')
            product,mode,bag_kg,count,unit_price,qty,total,_ = sale_values(row)
            if product not in WORKER_PRODUCTS[user['role']]:
                raise ValueError('Bu mahsulot sizga biriktirilmagan.')
            if total<=0: raise ValueError('Mahsulot narxi musbat bo‘lsin.')
            clean.append((product,mode,bag_kg,count,unit_price,qty,total))
            stocks_needed[product]=stocks_needed.get(product,0)+qty
            if product in BAGGED_FLOUR_PRODUCTS and mode == 'qop':
                bag_key=(product,int(bag_kg))
                bag_counts_needed[bag_key]=bag_counts_needed.get(bag_key,0)+int(count)
        total_cents = sum(round(x[6]*100) for x in clean)
        if total_cents<=0: raise ValueError('Sotuv summasi noto‘g‘ri.')
        payments = {}
        for method in PAYMENT_METHODS:
            value = float(request.form.get('pay_'+method,'0') or 0)
            if not math.isfinite(value) or value<0 or abs(value*100-round(value*100))>0.001:
                raise ValueError('To‘lov summalarini tiyingacha aniq kiriting.')
            payments[method]=round(value*100)
        paid_cents=sum(payments.values())
        if paid_cents>total_cents: raise ValueError('To‘lov jami sotuvdan oshmasin.')
        today=date.today().isoformat()
        with db() as con:
            con.execute('BEGIN IMMEDIATE')
            existing=con.execute('SELECT * FROM customers WHERE phone_key=?',(key,)).fetchone()
            if selected:
                if not selected.isdecimal(): raise ValueError('Xaridorni qayta tanlang.')
                card=con.execute('SELECT * FROM customers WHERE id=?',(int(selected),)).fetchone()
                if not card or card['phone_key']!=key or card['name']!=customer:
                    raise ValueError('Xaridor kartasini qayta tanlang.')
                customer,phone=card['name'],card['phone']
            elif existing:
                raise ValueError('Bu telefon bazada mavjud. Ro‘yxatdan xaridorni tanlang.')
            else:
                con.execute('INSERT INTO customers(name,phone,phone_key) VALUES(?,?,?)',(customer,phone,key))
            stocks=product_stocks(con)
            for product,need in stocks_needed.items():
                if need>stocks.get(product,0)+0.000001:
                    raise ValueError(product+' omborida yetarli mahsulot yo‘q.')
            for (product,bag_kg),count in bag_counts_needed.items():
                validate_bagged_sale(con,product,'qop',bag_kg,count)
            today_storage = _business_date_storage(today)
            batch=con.execute('INSERT INTO worker_sale_batches(worker_id,customer,customer_phone,sale_date,total,paid) VALUES(?,?,?,?,?,?)',
                  (user['id'],customer,phone,today_storage,total_cents/100,paid_cents/100)).lastrowid
            for method,cents in payments.items():
                if cents:
                    con.execute('INSERT INTO worker_sale_batch_payments(batch_id,payment_method,amount) VALUES(?,?,?)',
                                (batch,method,cents/100))
            left=paid_cents
            anchor_id=None
            for idx,(product,mode,bag_kg,count,unit_price,qty,total) in enumerate(clean):
                item_cents=round(total*100)
                take=min(left,item_cents)
                left-=take
                due=_business_date_storage(_business_date_after(today,25)) if take<item_cents else ''
                cursor=con.execute("""INSERT INTO sales(date,customer,customer_phone,due_date,product,qty_kg,
                    price_per_kg,vat_rate,paid,note,user_id,sale_mode,bag_kg,bag_count,unit_price,payment_method)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (_business_date_storage(today),customer,phone,due,product,qty,total/qty,0,take/100,
                     request.form.get('note','').strip(),user['id'],mode,bag_kg,count,unit_price,'naqd'))
                sale_id=cursor.lastrowid
                record_bagged_sale(con,sale_id,product,mode,bag_kg,count)
                con.execute('INSERT INTO worker_sale_batch_items(batch_id,sale_id) VALUES(?,?)',(batch,sale_id))
                if anchor_id is None: anchor_id=sale_id
            if left: raise ValueError('To‘lov taqsimotida xatolik.')
            # One invoice number for all items. Legacy individual invoices remain unchanged.
            invoice_id=ensure_invoice(con,anchor_id,clean[0][0],today_storage)
            con.execute('INSERT INTO worker_sale_submission_locks(token,worker_id,batch_id) VALUES(?,?,?)',
                        (submission_token,user['id'],batch))
        flash('Sotuv saqlandi: '+str(len(clean))+' mahsulot, bitta nakladnoy.','success')
        session.pop('worker_sale_token', None)
        return redirect(url_for('worker_sale_result',invoice_id=invoice_id))
    except (ValueError,KeyError,TypeError,OverflowError,sqlite3.Error,json.JSONDecodeError) as exc:
        flash(str(exc) or 'Sotuvni tekshiring.','error')
        return redirect(url_for('worker_sale_form'))


@app.route('/xodim/sotuv-natijasi/<int:invoice_id>')
@login_required
def worker_sale_result(invoice_id):
    user = current_user()
    with db() as con:
        inv = con.execute('SELECT id, sale_id, number FROM invoices WHERE id=?', (invoice_id,)).fetchone()
        if not inv: abort(404)
        batch = con.execute("""SELECT b.* FROM worker_sale_batches b
                     JOIN worker_sale_batch_items bi ON bi.batch_id=b.id
                     WHERE bi.sale_id=?""", (inv['sale_id'],)).fetchone()
        if not batch: abort(404)
        if user['role'] in WORKER_PRODUCTS and batch['worker_id'] != user['id']: abort(403)
        if user['role'] not in WORKER_PRODUCTS and user['role'] not in ('rahbar','buxgalter'): abort(403)
    return render_template('worker_sale_result.html', invoice=inv, batch=batch)


@app.route('/xodim/yangi-nakladnoy/<int:invoice_id>')
@login_required
def worker_batch_invoice(invoice_id):
    user=current_user()
    with db() as con:
        inv=con.execute('SELECT * FROM invoices WHERE id=?',(invoice_id,)).fetchone()
        if not inv: abort(404)
        row=con.execute("""SELECT b.* FROM worker_sale_batches b
            JOIN worker_sale_batch_items bi ON bi.batch_id=b.id
            WHERE bi.sale_id=?""",(inv['sale_id'],)).fetchone()
        if not row: abort(404)
        if user['role'] in WORKER_PRODUCTS and row['worker_id']!=user['id']: abort(403)
        if user['role'] not in WORKER_PRODUCTS and user['role'] not in ('buxgalter','rahbar'): abort(403)
        items=con.execute("""SELECT s.product,s.sale_mode,s.bag_kg,s.bag_count,s.qty_kg
          FROM worker_sale_batch_items bi JOIN sales s ON s.id=bi.sale_id
          WHERE bi.batch_id=? ORDER BY s.id""",(row['id'],)).fetchall()
    return render_template('worker_batch_invoice.html',r=inv,batch=row,items=items)


@app.context_processor
def worker_receipt_context():
    def worker_receipt_token(sale):
        return URLSafeSerializer(app.secret_key, salt='worker-receipt-operation-v1').dumps({
            'action': 'worker_receipt', 'worker_id': session['user_id'],
            'sale_id': sale['id'], 'customer': sale['customer'],
            'phone': sale['customer_phone'], 'nonce': secrets.token_urlsafe(32)})
    return {'worker_receipt_token': worker_receipt_token}


@app.route('/xodim/qarz-tolovi', methods=['POST'])
@login_required
def worker_receipt():
    user=current_user()
    if user['role'] not in WORKER_PRODUCTS: abort(403)
    try:
        sale_id=int(request.form['sale_id'])
        amount_cents=payment_amount_cents(request.form['amount'])
        amount=amount_cents/100
        payment_method = payment_method_from_form(request.form)
        with db() as con:
            con.execute('BEGIN IMMEDIATE')
            token = request.form.get('operation_token', '')
            try:
                operation = URLSafeSerializer(app.secret_key, salt='worker-receipt-operation-v1').loads(token)
            except BadSignature:
                raise ValueError('To‘lov tokeni yaroqsiz. Formani qayta oching.')
            if (not isinstance(operation, dict) or operation.get('action') != 'worker_receipt'
                    or operation.get('worker_id') != user['id'] or operation.get('sale_id') != sale_id):
                raise ValueError('To‘lov tokeni bu xodim yoki sotuvga tegishli emas.')
            sale=con.execute("""SELECT s.*,COALESCE((SELECT SUM(amount) FROM worker_receipts WHERE sale_id=s.id),0) receipts FROM sales s
                LEFT JOIN sale_cancellations c ON c.sale_id=s.id WHERE s.id=? AND s.user_id=? AND c.sale_id IS NULL""",(sale_id,user['id'])).fetchone()
            if not sale: raise ValueError('Bu sotuv sizga tegishli emas.')
            if (operation.get('customer') != sale['customer'] or operation.get('phone') != sale['customer_phone']
                    or request.form.get('customer', sale['customer']) != sale['customer']
                    or request.form.get('customer_phone', sale['customer_phone']) != sale['customer_phone']):
                raise ValueError('Xaridor ma’lumoti o‘zgargan. To‘lov formasini qayta oching.')
            previous = con.execute('SELECT * FROM worker_receipt_operations WHERE token=?', (token,)).fetchone()
            if previous:
                if (previous['worker_id'] != user['id'] or previous['sale_id'] != sale_id
                        or previous['amount'] != amount or previous['payment_method'] != payment_method):
                    raise ValueError('Bu token boshqa to‘lov uchun ishlatilgan.')
                flash('Qarz to‘lovi kassangizga yozildi.','success')
                return redirect(url_for('worker_sales'))
            financial = calculate_financial_snapshot(con)
            if snapshot_has_ambiguous_legacy(financial, sale['customer']):
                raise ValueError('Eski to‘lov xaridorning telefoniga ishonchli bog‘lanmagan. Avval aniqlashtiring.')
            obligation = next((item for item in worker_snapshot_obligations(
                financial, user['id'], sale['customer'], sale['customer_phone'])
                if item['id'] == sale_id), None)
            if amount_cents > balance_cents(obligation['balance'] if obligation else Decimal('0')):
                raise ValueError('To‘lov qolgan qarzdan oshmasin.')
            receipt = con.execute('INSERT INTO worker_receipts(sale_id,worker_id,amount,date,payment_method) VALUES(?,?,?,?,?)',(sale_id,user['id'],amount,_business_date_storage(date.today().isoformat()),payment_method)).lastrowid
            con.execute('INSERT INTO worker_receipt_operations(token,worker_id,sale_id,amount,payment_method,receipt_id) VALUES(?,?,?,?,?,?)',
                        (token,user['id'],sale_id,amount,payment_method,receipt))
        flash('Qarz to‘lovi kassangizga yozildi.','success')
    except (ValueError,KeyError,sqlite3.Error) as exc: flash(str(exc) or 'To‘lov xato.','error')
    return redirect(url_for('worker_sales'))

@app.context_processor
def worker_group_receipt_context():
    def worker_group_receipt_token(customer, phone):
        return URLSafeSerializer(app.secret_key, salt='worker-group-receipt-operation-v1').dumps({
            'action': 'worker_group_receipt', 'worker_id': session['user_id'],
            'customer': customer, 'phone_key': phone_key(phone) or phone,
            'nonce': secrets.token_urlsafe(32)})
    return {'worker_group_receipt_token': worker_group_receipt_token}


@app.route('/xodim/umumiy-qarz-tolovi', methods=['POST'])
@login_required
def worker_group_receipt():
    user = current_user()
    if user['role'] not in WORKER_PRODUCTS: abort(403)
    try:
        customer = request.form.get('customer', '').strip()
        phone = request.form.get('customer_phone', '').strip()
        key = phone_key(phone) or phone
        amount_cents = payment_amount_cents(request.form.get('amount', ''))
        amount = amount_cents / 100
        payment_method = payment_method_from_form(request.form)
        if not customer or not key:
            raise ValueError('Xaridor va musbat to‘lov summasini kiriting.')
        cents = amount_cents
        with db() as con:
            con.execute('BEGIN IMMEDIATE')
            token = request.form.get('operation_token', '')
            try:
                operation = URLSafeSerializer(app.secret_key, salt='worker-group-receipt-operation-v1').loads(token)
            except BadSignature:
                raise ValueError('To‘lov tokeni yaroqsiz. Formani qayta oching.')
            if (not isinstance(operation, dict) or operation.get('action') != 'worker_group_receipt'
                    or operation.get('worker_id') != user['id']
                    or operation.get('customer') != customer or operation.get('phone_key') != key):
                raise ValueError('To‘lov tokeni bu xodim yoki xaridorga tegishli emas.')
            previous = con.execute('SELECT * FROM worker_group_receipt_operations WHERE token=?', (token,)).fetchone()
            if previous:
                if (previous['worker_id'] != user['id'] or previous['customer'] != customer
                        or previous['phone_key'] != key or previous['amount_cents'] != cents
                        or previous['payment_method'] != payment_method):
                    raise ValueError('Bu token boshqa to‘lov uchun ishlatilgan.')
                flash('To‘lov saqlandi. Pul eng eski qarzlardan boshlab taqsimlandi.','success')
                return redirect(url_for('worker_debts'))
            financial = calculate_financial_snapshot(con)
            if snapshot_has_ambiguous_legacy(financial, customer):
                raise ValueError('Eski to‘lov xaridorning telefoniga ishonchli bog‘lanmagan. Avval aniqlashtiring.')
            debts = [(item['kind'],item['id'], balance_cents(item['balance']))
                     for item in worker_snapshot_obligations(financial, user['id'], customer, phone)
                     if balance_cents(item['balance']) > 0]
            total_cents = sum(rem for _, _, rem in debts)
            if total_cents <= 0: raise ValueError('Bu xaridorning qarzi topilmadi.')
            if cents > total_cents: raise ValueError('To‘lov umumiy qarzdan oshmasin.')
            con.execute('INSERT INTO worker_group_receipt_operations(token,worker_id,customer,phone_key,amount_cents,payment_method) VALUES(?,?,?,?,?,?)',
                        (token,user['id'],customer,key,cents,payment_method))
            left = cents
            for kind, obligation_id, rem in debts:
                if left <= 0: break
                take = min(left, rem)
                receipt_day = _business_date_storage(date.today().isoformat())
                if kind == 'opening':
                    con.execute('''INSERT INTO opening_debt_receipts
                        (opening_debt_id,worker_id,amount,date,payment_method,received_by,receiver_id,operation_token)
                        VALUES(?,?,?,?,?,'worker',?,?)''',
                        (obligation_id,user['id'],take/100,receipt_day,payment_method,user['id'],token))
                else:
                    receipt = con.execute('INSERT INTO worker_receipts(sale_id,worker_id,amount,date,payment_method) VALUES(?,?,?,?,?)',
                                          (obligation_id,user['id'],take/100,receipt_day,payment_method)).lastrowid
                    con.execute('INSERT INTO worker_group_receipt_operation_items(token,receipt_id) VALUES(?,?)', (token,receipt))
                left -= take
        flash('To‘lov saqlandi. Pul eng eski qarzlardan boshlab taqsimlandi.','success')
    except (ValueError,OverflowError,sqlite3.Error) as exc:
        flash(str(exc) or 'Qarz to‘lovida xato.','error')
    return redirect(url_for('worker_debts'))


@app.context_processor
def worker_expense_context():
    def worker_expense_token():
        return URLSafeSerializer(app.secret_key, salt='worker-expense-operation-v1').dumps({
            'action': 'worker_expense', 'worker_id': session['user_id'],
            'nonce': secrets.token_urlsafe(32)})
    return {'worker_expense_token': worker_expense_token}


@app.route('/xodim/xarajat', methods=['POST'])
@login_required
def worker_expense():
    user=current_user()
    if user['role'] not in WORKER_PRODUCTS: abort(403)
    try:
        amount=float(request.form['amount']); category=request.form.get('category','').strip()
        note=request.form.get('note','').strip()
        payment_method=payment_method_from_form(request.form)
        if not math.isfinite(amount) or amount<=0 or not category:
            raise ValueError('Xarajat summasi va turini tekshiring.')
        with db() as con:
            con.execute('BEGIN IMMEDIATE')
            token=request.form.get('operation_token','')
            try:
                operation=URLSafeSerializer(app.secret_key, salt='worker-expense-operation-v1').loads(token)
            except BadSignature:
                raise ValueError('Xarajat tokeni yaroqsiz. Formani qayta oching.')
            if (not isinstance(operation,dict) or operation.get('action')!='worker_expense'
                    or operation.get('worker_id')!=user['id']):
                raise ValueError('Xarajat tokeni bu xodimga tegishli emas.')
            # This form has no editable date: first acceptance uses the server day.
            # Replays keep the committed expense date, including after midnight.
            if request.form.get('date'):
                raise ValueError('Xarajat sanasi server tomonidan avtomatik belgilanadi.')
            payload=json.dumps({'amount':amount.hex(),'payment_method':payment_method,
                'category':category,'note':note},
                ensure_ascii=False,sort_keys=True,separators=(',',':'))
            previous=con.execute('SELECT * FROM worker_expense_operations WHERE token=?',(token,)).fetchone()
            if previous:
                if previous['worker_id']!=user['id'] or previous['payload']!=payload:
                    raise ValueError('Bu token boshqa xarajat mazmuni uchun ishlatilgan.')
                flash('Xarajat kassadan ayrildi.','success')
                return redirect(url_for('worker_sales'))
            if amount>worker_channel_balances(con,user['id'])[payment_method]+0.001: raise ValueError('Tanlangan hisobda yetarli pul yo‘q.')
            expense_date=_business_date_storage(date.today().isoformat())
            expense=con.execute('''INSERT INTO expenses
                (date,category,amount,note,user_id,payment_method,entered_by,fund_source)
                VALUES(?,?,?,?,?,?,?,?)''',
                (expense_date,category,amount,note,user['id'],payment_method,
                 user['id'],user['role'])).lastrowid
            con.execute('INSERT INTO worker_expense_operations(token,worker_id,payload,expense_id,expense_date) VALUES(?,?,?,?,?)',
                        (token,user['id'],payload,expense,expense_date))
        flash('Xarajat kassadan ayrildi.','success')
    except (ValueError,sqlite3.Error) as exc: flash(str(exc) or 'Xarajat xato.','error')
    return redirect(url_for('worker_sales'))

@app.context_processor
def remaining_write_context():
    def write_operation_token(action, **state):
        return URLSafeSerializer(app.secret_key, salt='remaining-write-operation-v1').dumps({
            'actor_id': session['user_id'], 'action': action,
            'nonce': secrets.token_urlsafe(32), 'state': state})
    return {'write_operation_token': write_operation_token}


def check_remaining_operation(con, action):
    # Called inside the caller's BEGIN IMMEDIATE transaction.
    token = request.form.get('operation_token', '')
    try:
        claim = URLSafeSerializer(app.secret_key, salt='remaining-write-operation-v1').loads(token)
    except BadSignature:
        raise ValueError('Operatsiya tokeni yaroqsiz. Formani qayta oching.')
    if (not isinstance(claim, dict) or claim.get('actor_id') != session['user_id']
            or claim.get('action') != action):
        raise ValueError('Token bu foydalanuvchi yoki amalga tegishli emas.')
    payload = json.dumps({key: request.form.getlist(key) for key in request.form
        if key not in ('csrf_token', 'operation_token')},
        ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    previous = con.execute('SELECT * FROM remaining_write_operations WHERE token=?', (token,)).fetchone()
    if previous and (previous['actor_id'] != session['user_id'] or previous['action'] != action
                     or previous['payload'] != payload):
        raise ValueError('Bu token boshqa mazmundagi operatsiya uchun ishlatilgan.')
    return token, payload, previous, claim


def save_remaining_operation(con, action, token, payload, result_id):
    con.execute('INSERT INTO remaining_write_operations(token,actor_id,action,payload,result_id) VALUES(?,?,?,?,?)',
                (token,session['user_id'],action,payload,result_id))


@app.route('/xodim/pul-topshirish', methods=['POST'])
@login_required
def worker_transfer():
    user=current_user()
    if user['role'] not in WORKER_PRODUCTS: abort(403)
    try:
        amount=float(request.form['amount'])
        payment_method=payment_method_from_form(request.form)
        if not math.isfinite(amount) or amount<=0: raise ValueError('Summa musbat bo‘lsin.')
        with db() as con:
            con.execute('BEGIN IMMEDIATE')
            token, payload, previous, _ = check_remaining_operation(con, 'worker_transfer')
            if previous:
                flash('So‘rov oldin saqlangan. Takror pul topshirish yaratilmadi.', 'success')
                return redirect(url_for('worker_sales'))
            pending=con.execute("SELECT COALESCE(SUM(amount),0) FROM worker_transfers WHERE worker_id=? AND status='pending' AND payment_method=?",(user['id'],payment_method)).fetchone()[0]
            if amount+pending>worker_channel_balances(con,user['id'])[payment_method]+0.001: raise ValueError('Mavjud kassadan ortiq pul topshirib bo‘lmaydi.')
            transfer = con.execute('INSERT INTO worker_transfers(worker_id,amount,payment_method) VALUES(?,?,?)',(user['id'],amount,payment_method)).lastrowid
            save_remaining_operation(con, 'worker_transfer', token, payload, transfer)
        flash('So‘rov yuborildi. Buxgalter tasdiqlamaguncha pul kassada qoladi.','success')
    except (ValueError,sqlite3.Error) as exc: flash(str(exc) or 'So‘rov xato.','error')
    return redirect(url_for('worker_sales'))

@app.route('/xodimlar/nazorat',methods=['GET','POST'])
@login_required
def worker_admin():
    if current_user()['role'] not in ('buxgalter','rahbar'): abort(403)
    if request.method=='POST':
        if current_user()['role']!='buxgalter': abort(403)
        if request.form.get('action') == 'add_accountant_cash':
            try:
                token = request.form.get('operation_token', '')
                try:
                    claim = URLSafeSerializer(
                        app.secret_key, salt='accountant-cash-add-v1').loads(token)
                except BadSignature:
                    raise ValueError('Kassaga pul qo‘shish tokeni yaroqsiz. Formani qayta oching.')
                if (not isinstance(claim, dict)
                        or claim.get('action') != 'add_accountant_cash'
                        or claim.get('actor_id') != session['user_id']):
                    raise ValueError('Token bu foydalanuvchiga tegishli emas.')

                raw_date = request.form.get('date', '').strip()
                addition_date = _html_date_storage(raw_date)
                if _business_date(addition_date)[0] > date.today():
                    raise ValueError('Kelajak sanasiga pul kiritib bo‘lmaydi.')
                payment_method = request.form.get('payment_method', '').strip()
                if payment_method not in ('naqd', 'hisob'):
                    raise ValueError('Naqd yoki Bank turini tanlang.')
                source = request.form.get('source', '').strip()
                note = request.form.get('note', '').strip()
                if not source or len(source) > 200:
                    raise ValueError('Mablag‘ manbasini 1–200 belgi bilan kiriting.')
                if len(note) > 500:
                    raise ValueError('Izoh 500 belgidan oshmasin.')
                amount = payment_amount_cents(
                    request.form.get('amount', '')) / 100
                payload = json.dumps({
                    'date': addition_date,
                    'amount_cents': int(round(amount * 100)),
                    'payment_method': payment_method,
                    'source': source,
                    'note': note,
                }, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

                con = db()
                try:
                    con.execute('BEGIN IMMEDIATE')
                    previous = con.execute(
                        'SELECT entered_by,payload FROM accountant_cash_additions '
                        'WHERE operation_token=?', (token,)).fetchone()
                    if previous:
                        if previous['entered_by'] != session['user_id'] or previous['payload'] != payload:
                            raise ValueError('Bu token boshqa kassaga pul qo‘shish uchun ishlatilgan.')
                        flash('So‘rov oldin saqlangan. Takroriy pul kiritilmadi.', 'success')
                        return redirect(url_for('worker_admin'))
                    con.execute("""INSERT INTO accountant_cash_additions
                        (date,amount,payment_method,source,note,entered_by,operation_token,payload)
                        VALUES(?,?,?,?,?,?,?,?)""",
                        (addition_date, amount, payment_method, source, note,
                         session['user_id'], token, payload))
                    con.commit()
                finally:
                    con.close()
                flash('Pul buxgalter kassasiga kiritildi; sotuv tushumi sifatida hisoblanmadi.', 'success')
            except (ValueError, KeyError, OverflowError, sqlite3.Error) as exc:
                flash(str(exc) or 'Kassaga pul qo‘shib bo‘lmadi.', 'error')
            return redirect(url_for('worker_admin'))
        if request.form.get('action') == 'internal_cash_transfer':
            try:
                token = request.form.get('operation_token', '')
                try:
                    claim = URLSafeSerializer(
                        app.secret_key, salt='account-cash-transfer-v1').loads(token)
                except BadSignature:
                    raise ValueError('Ko‘chirish tokeni yaroqsiz. Formani qayta oching.')
                if (not isinstance(claim, dict)
                        or claim.get('action') != 'internal_cash_transfer'
                        or claim.get('actor_id') != session['user_id']):
                    raise ValueError('Ko‘chirish tokeni bu foydalanuvchiga tegishli emas.')
                payload = json.dumps({
                    key: request.form.getlist(key)
                    for key in request.form
                    if key not in ('csrf_token', 'operation_token', 'action')
                }, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
                transfer_date = _business_date_storage(
                    request.form.get('date', '').strip())
                from_channel = request.form.get('from_channel', '').strip()
                to_channel = request.form.get('to_channel', '').strip()
                if from_channel not in ('naqd', 'plastik', 'hisob'):
                    raise ValueError('Manba hisobni tanlang.')
                if to_channel not in ('naqd', 'plastik', 'hisob'):
                    raise ValueError('Qabul qiluvchi hisobni tanlang.')
                if from_channel == to_channel:
                    raise ValueError('Bir hisobdan o‘ziga pul ko‘chirish mumkin emas.')
                amount_cents = payment_amount_cents(request.form.get('amount', ''))
                amount = amount_cents / 100
                note = request.form.get('note', '').strip()
                if len(note) > 500:
                    raise ValueError('Izoh 500 belgidan oshmasin.')
                con = db()
                try:
                    con.execute('BEGIN IMMEDIATE')
                    previous = con.execute(
                        'SELECT entered_by,payload FROM account_cash_transfers WHERE operation_token=?',
                        (token,)).fetchone()
                    if previous:
                        if previous['entered_by'] != session['user_id'] or previous['payload'] != payload:
                            raise ValueError('Bu token boshqa pul ko‘chirish uchun ishlatilgan.')
                        flash('So‘rov oldin saqlangan. Takroriy ko‘chirish yaratilmadi.', 'success')
                        return redirect(url_for('worker_admin'))
                    day = _business_date_key(transfer_date)
                    from cashflow_analytics import calculate_cashflow_analytics
                    report = calculate_cashflow_analytics(con, end_date=day)
                    available = report['accounts']['buxgalter']['closing_channels'][from_channel]
                    if Decimal(str(amount)) > available:
                        raise ValueError(
                            f'Manba hisobida yetarli mablag‘ yo‘q. Hisobiy qoldiq: '
                            f'{available:,.2f} so‘m.')
                    con.execute("""INSERT INTO account_cash_transfers
                        (date,from_channel,to_channel,amount,note,entered_by,operation_token,payload)
                        VALUES(?,?,?,?,?,?,?,?)""",
                        (transfer_date, from_channel, to_channel, amount, note,
                         session['user_id'], token, payload))
                    con.commit()
                finally:
                    con.close()
                flash('Hisoblar orasidagi pul ko‘chirildi.', 'success')
            except (ValueError, KeyError, OverflowError, sqlite3.Error) as exc:
                flash(str(exc) or 'Pul ko‘chirish amalga oshmadi.', 'error')
            return redirect(url_for('worker_admin'))
        tid=request.form.get('transfer_id',type=int); action=request.form.get('action')
        if action not in ('approved','rejected'): abort(400)
        with db() as con:
            con.execute('BEGIN IMMEDIATE')
            row=con.execute("SELECT * FROM worker_transfers WHERE id=? AND status='pending'",(tid,)).fetchone()
            if not row: abort(400,'So‘rov allaqachon ko‘rib chiqilgan.')
            if action=='approved' and row['amount']>worker_channel_balances(con,row['worker_id'])[row['payment_method']]+0.001:
                abort(400,'Xodim kassasida yetarli mablag‘ yo‘q.')
            con.execute('UPDATE worker_transfers SET status=?,decided_at=?,decided_by=? WHERE id=?',(action,datetime.now().isoformat(),session['user_id'],tid))
        return redirect(url_for('worker_admin'))
    with db() as con:
        workers=con.execute("SELECT id,username,role FROM users WHERE role IN ('umid','maxmud') ORDER BY id").fetchall()
        balances=[(w,worker_balance(con,w['id'])) for w in workers]
        from cashflow_analytics import calculate_cashflow_analytics
        cashflow = calculate_cashflow_analytics(con)
        accountant_cash = cashflow['accounts']['buxgalter']['closing_channels']
        account_transfers,account_transfer_pagination = paginate_rows(con,
            """SELECT t.*,u.username AS entered_by_name
               FROM account_cash_transfers t JOIN users u ON u.id=t.entered_by
               ORDER BY business_date_key(t.date) DESC,t.id DESC""",
            """SELECT COUNT(*) FROM account_cash_transfers t
               JOIN users u ON u.id=t.entered_by""",
            page_param='account_transfer_page',
            per_page_param='account_transfer_per_page')
        accountant_cash_additions,addition_pagination=paginate_rows(con,
            """SELECT a.*,u.username AS entered_by_name
               FROM accountant_cash_additions a JOIN users u ON u.id=a.entered_by
               ORDER BY business_date_key(a.date) DESC,a.id DESC""",
            """SELECT COUNT(*) FROM accountant_cash_additions a
               JOIN users u ON u.id=a.entered_by""",
            page_param='addition_page',per_page_param='addition_per_page')
        transfers,transfer_request_pagination=paginate_rows(con,
            """SELECT t.*,u.username FROM worker_transfers t
               JOIN users u ON u.id=t.worker_id ORDER BY t.id DESC""",
            """SELECT COUNT(*) FROM worker_transfers t
               JOIN users u ON u.id=t.worker_id""",
            page_param='transfer_request_page',
            per_page_param='transfer_request_per_page')
        sums=con.execute("""SELECT worker_id,payment_method,COALESCE(SUM(amount),0) amount
                          FROM worker_transfers WHERE status='approved'
                          GROUP BY worker_id,payment_method""").fetchall()
        # Both invoice debt and opening debt collected directly by accountant.
        # UNION ALL preserves every receipt; GROUP BY summarizes each worker/channel once.
        direct_rows=con.execute("""SELECT worker_id,payment_method,COALESCE(SUM(amount),0) amount
                            FROM (
                                SELECT worker_id,payment_method,amount
                                FROM accountant_worker_receipts
                                UNION ALL
                                SELECT worker_id,payment_method,amount
                                FROM opening_debt_receipts
                                WHERE received_by='accountant'
                            ) GROUP BY worker_id,payment_method""").fetchall()
        direct={(r['worker_id'],r['payment_method']):float(r['amount']) for r in direct_rows}
        collected={(r['worker_id'],r['payment_method']):float(r['amount']) for r in sums}
        received_by_worker=[{'username':w['username'], 'worker_id':w['id'],
                            'naqd':collected.get((w['id'],'naqd'),0),
                            'plastik':collected.get((w['id'],'plastik'),0),
                            'hisob':collected.get((w['id'],'hisob'),0),
                            'direct_naqd':direct.get((w['id'],'naqd'),0),
                            'direct_plastik':direct.get((w['id'],'plastik'),0),
                            'direct_hisob':direct.get((w['id'],'hisob'),0)} for w in workers]
    return render_template('worker_admin.html',balances=balances,transfers=transfers,
                           received_by_worker=received_by_worker,payment_methods=PAYMENT_METHODS,
                           accountant_cash=accountant_cash,account_transfers=account_transfers,
                           accountant_cash_additions=accountant_cash_additions,
                           cash_channels={'naqd':'Naqd','plastik':'Plastik','hisob':'Bank'},
                           today=date.today().isoformat(),
                           account_transfer_pagination=account_transfer_pagination,
                           addition_pagination=addition_pagination,
                           transfer_request_pagination=transfer_request_pagination)

@app.context_processor
def account_cash_transfer_context():
    def account_cash_transfer_token():
        return URLSafeSerializer(app.secret_key, salt='account-cash-transfer-v1').dumps({
            'action':'internal_cash_transfer','actor_id':session['user_id'],
            'nonce':secrets.token_urlsafe(32)})
    return {'account_cash_transfer_token':account_cash_transfer_token}


@app.context_processor
def accountant_cash_add_context():
    def accountant_cash_add_token():
        return URLSafeSerializer(app.secret_key, salt='accountant-cash-add-v1').dumps({
            'action': 'add_accountant_cash', 'actor_id': session['user_id'],
            'nonce': secrets.token_urlsafe(32)})
    return {'accountant_cash_add_token': accountant_cash_add_token}

@app.route('/xodimlar/nazorat/excel')
@login_required
def worker_transfers_excel():
    if current_user()['role'] not in ('buxgalter', 'rahbar'):
        abort(403)
    from flask import Response
    from worker_cash_export import build_worker_cash_workbook

    con = db()
    try:
        con.execute('BEGIN')
        workers = [dict(r) for r in con.execute("SELECT id,username FROM users WHERE role IN ('umid','maxmud') ORDER BY id")]
        transfers = [dict(r) for r in con.execute("""SELECT t.*,a.username accountant
            FROM worker_transfers t LEFT JOIN users a ON a.id=t.decided_by
            ORDER BY t.created_at,t.id""")]
        directs = [dict(r) for r in con.execute("""SELECT r.*,a.username accountant
            FROM accountant_worker_receipts r LEFT JOIN users a ON a.id=r.accountant_id
            ORDER BY business_date_key(r.payment_date),r.id""")]
        # Export opening debt accountant receipts with the same column schema.
        # Negative in-memory IDs distinguish this source from the sales receipt IDs;
        # no database ID is changed, and the Excel deduplication remains effective.
        directs.extend(dict(r) for r in con.execute("""
            SELECT -r.id AS id, r.worker_id, r.receiver_id AS accountant_id,
                   d.customer, d.customer_phone, r.payment_method, r.amount,
                   r.date AS payment_date, r.note, r.created_at,
                   a.username AS accountant
            FROM opening_debt_receipts r
            JOIN opening_debts d ON d.id=r.opening_debt_id
            LEFT JOIN users a ON a.id=r.receiver_id
            WHERE r.received_by='accountant'
            ORDER BY business_date_key(r.date),r.id"""))
    finally:
        con.close()
    today = date.today().isoformat()
    try:
        data = build_worker_cash_workbook(
            os.path.join(BASE_DIR, 'export_templates', 'worker_cash_v3.xlsx'),
            workers, transfers, directs, date.today())
    except ValueError as exc:
        abort(400, description=str(exc))
    return Response(data, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={'Content-Disposition':'attachment; filename="xodimlar_kassasi_'+today+'.xlsx"','Cache-Control':'no-store'})

@app.route('/boshliq/statistika')
@login_required
def boss_statistics():
    if current_user()['role'] != 'rahbar':
        abort(403)
    with db() as con:
        con.execute('BEGIN')
        financial = calculate_financial_snapshot(con)
        sales = financial['totals']['sales_revenue']
        receipts = financial['totals']['received']
        debts = financial['totals']['debt']
        financial_warnings = list(financial['warnings'])
        phones_by_name = {}
        for name, phone in financial['customers']:
            phones_by_name.setdefault(name, set()).add(phone)
        for name, phones in phones_by_name.items():
            if len(phones) > 1:
                financial_warnings.append({'code': 'same_name_multiple_phones', 'customer': name})
        expenses = con.execute("SELECT COALESCE(SUM(amount),0) FROM expenses").fetchone()[0]
        purchased = con.execute('SELECT COALESCE(SUM(net),0) FROM wheat_receipts').fetchone()[0]
        used = con.execute('SELECT COALESCE(SUM(COALESCE(raw_wheat_kg,wheat_kg)),0) FROM production').fetchone()[0]
        stocks = [('Bug‘doy', purchased-used)] + list(product_stocks(con).items())
        bagged_stocks=bagged_product_stocks(con)
        from cashflow_analytics import calculate_cashflow_analytics
        cashflow = calculate_cashflow_analytics(con)
        accountant_cash = cashflow['accounts']['buxgalter']['closing_channels']
        account_transfers,transfer_pagination=paginate_rows(con,
            """SELECT t.*,u.username AS entered_by_name
               FROM account_cash_transfers t JOIN users u ON u.id=t.entered_by
               ORDER BY business_date_key(t.date) DESC,t.id DESC""",
            """SELECT COUNT(*) FROM account_cash_transfers t
               JOIN users u ON u.id=t.entered_by""",
            page_param='transfer_page',per_page_param='transfer_per_page')
        users = {r['id']:r['username'] for r in con.execute('SELECT id,username FROM users')}
        debt_details = []
        for customer in financial['customers'].values():
            for item in customer['obligations']:
                if item['balance'] <= 0:
                    continue
                if item['kind'] == 'sale':
                    r = con.execute('SELECT date,product,qty_kg,user_id FROM sales WHERE id=?',(item['id'],)).fetchone()
                    product,qty,seller = r['product'],r['qty_kg'],users.get(r['user_id'],'Belgilanmagan')
                else:
                    product,qty,seller = 'Eski qarz',None,users.get(item.get('user_id'),'Biriktirilmagan')
                debt_details.append({'customer':customer['customer'],'phone':customer['phone'],
                    'date':item['date'],
                    'product':product,'qty':qty,'seller':seller,'balance':item['balance'],
                    'due':item['due_date'] or _business_date_after(item['date'],25)})
        debt_details.sort(key=lambda x: (x['customer'].casefold(),_business_date(x['date'])[0]))
        debt_details,debt_pagination=paginate_items(
            debt_details,'debt_page','debt_per_page')
    return render_template('boss_statistics.html',sales=sales,receipts=receipts,expenses=expenses,
                           debts=debts,stocks=stocks,details=debt_details,
                           bagged_stocks=bagged_stocks,
                           financial_totals=financial['totals'],financial_warnings=financial_warnings,
                           accountant_cash=accountant_cash,account_transfers=account_transfers,
                           cash_channels={'naqd':'Naqd','plastik':'Plastik','hisob':'Bank'},
                           transfer_pagination=transfer_pagination,
                           debt_pagination=debt_pagination)

@app.route('/bugdoy', methods=['GET','POST'])
@login_required
def wheat():
    if request.method == 'POST':
        if current_user()['role'] != 'buxgalter':
            abort(403)
        try:
            date_value = request.form.get('date', '')
            if date_value:
                try:
                    purchase_date = _html_date_storage(date_value)
                except ValueError:
                    flash('Xarid sanasi YYYY-MM-DD formatida haqiqiy sana bo‘lishi kerak.', 'error')
                    return redirect(url_for('wheat'))
            else:
                purchase_date = _business_date_storage(date.today().isoformat())
            unit = request.form.get('unit', 'tonna')
            if unit not in ('kg', 'tonna'): raise ValueError()
            factor = 1000 if unit == 'tonna' else 1
            gross = float(request.form['gross']) * factor
            tare = float(request.form.get('tare', 0)) * factor
            entered_price = float(request.form['price'])
            price_unit = request.form.get('price_unit', 'kg')
            if price_unit not in ('kg', 'tonna'): raise ValueError()
            price = entered_price / 1000 if price_unit == 'tonna' else entered_price
            if not all(math.isfinite(x) for x in (gross, tare, entered_price, price)): raise ValueError()
            method = request.form.get('purchase_method', '')
            purchase_methods = {
                'naqd': ('Naqd', 'naqd'),
                'bank': ('Bank orqali (birja)', 'hisob'),
            }
            if method not in purchase_methods or gross <= tare or tare < 0 or price <= 0:
                raise ValueError()
            net = gross - tare
            total_payment = float(
                (Decimal(str(net)) * Decimal(str(price))).quantize(
                    Decimal('0.01'), rounding=ROUND_HALF_UP))
            if not math.isfinite(total_payment) or total_payment <= 0:
                raise ValueError()
            purchase_label, cash_channel = purchase_methods[method]
            with db() as con:
                con.execute('BEGIN IMMEDIATE')
                token, payload, previous, _ = check_remaining_operation(con, 'wheat')
                if previous:
                    flash('Bug‘doy xaridi oldin saqlangan. Takror kirim yaratilmadi.', 'success')
                    return redirect(url_for('wheat'))
                from cashflow_analytics import calculate_cashflow_analytics
                cashflow = calculate_cashflow_analytics(con)
                available = cashflow['accounts']['buxgalter']['closing_channels'][cash_channel]
                if Decimal(str(available)) + Decimal('0.001') < Decimal(str(total_payment)):
                    account_name = 'Naqd kassa' if cash_channel == 'naqd' else 'Bank hisobi'
                    raise ValueError(
                        f"Buxgalterning {account_name}da mablag‘ yetarli emas. "
                        f"Mavjud: {float(available):,.2f} so‘m; "
                        f"xarid summasi: {total_payment:,.2f} so‘m.")
                wheat_id = con.execute('''INSERT INTO wheat_receipts
                    (date,supplier,gross,tare,net,cleaned_net,price_per_kg,vat_rate,note,user_id,purchase_method)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                    (purchase_date,
                     request.form.get('supplier','').strip(),gross,tare,net,net,price,0,
                     request.form.get('note','').strip(),session['user_id'],purchase_label)).lastrowid
                con.execute('''INSERT INTO wheat_purchase_payments
                    (wheat_receipt_id,accountant_id,payment_method,amount,payment_date,operation_token)
                    VALUES(?,?,?,?,?,?)''',
                    (wheat_id,session['user_id'],cash_channel,total_payment,
                     date.today().isoformat(),token))
                save_remaining_operation(con, 'wheat', token, payload, wheat_id)
            flash('Bug‘doy xaridi to‘liq to‘landi va umumiy zaxiraga qo‘shildi.', 'success')
            return redirect(url_for('wheat'))
        except (ValueError, KeyError, OverflowError, InvalidOperation) as exc:
            flash(str(exc) or 'Xarid turi, vazn va narxni tekshiring.', 'error')
        except sqlite3.Error:
            flash('Bug‘doy xaridi to‘liq saqlanmadi; zaxira va kassa o‘zgarmadi.', 'error')
    with db() as con:
        rows,pagination=paginate_rows(
            con,
            'SELECT * FROM wheat_receipts ORDER BY business_date_key(date) DESC,id DESC',
            'SELECT COUNT(*) FROM wheat_receipts',
        )
        total=con.execute('SELECT COALESCE(SUM(net),0) FROM wheat_receipts').fetchone()[0]
        used=con.execute('SELECT COALESCE(SUM(COALESCE(raw_wheat_kg,wheat_kg)),0) FROM production').fetchone()[0]
        from cashflow_analytics import calculate_cashflow_analytics
        cashflow=calculate_cashflow_analytics(con)
        accountant_cash=cashflow['accounts']['buxgalter']['closing_channels']
        opening_wheat_stock=con.execute(
            'SELECT * FROM wheat_receipts WHERE purchase_method=? ORDER BY id LIMIT 1',
            (OPENING_WHEAT_METHOD,),
        ).fetchone()
    return render_template('wheat.html', rows=rows, stock=total-used,
                           accountant_cash=accountant_cash,pagination=pagination,
                           opening_wheat_stock=opening_wheat_stock)

@app.route('/bugdoy/boshlangich-zaxira', methods=['POST'])
@login_required
def add_opening_wheat_stock():
    if current_user()['role'] != 'buxgalter':
        abort(403)
    try:
        date_value = request.form.get('date', '').strip()
        if not date_value:
            raise ValueError('Boshlang‘ich zaxira sanasini kiriting.')
        stock_date = _html_date_storage(date_value)
        amount_value = Decimal(request.form.get('qty_kg', '').strip())
        if (not amount_value.is_finite() or amount_value <= 0
                or amount_value != amount_value.quantize(Decimal('0.01'))):
            raise ValueError('Bug‘doy miqdori 0.01 kg qadamida musbat bo‘lishi kerak.')
        amount = float(amount_value)
        if not math.isfinite(amount):
            raise ValueError('Bug‘doy miqdori noto‘g‘ri.')
        note = request.form.get('note', '').strip()
        if len(note) > 500:
            raise ValueError('Izoh 500 belgidan oshmasin.')

        with db() as con:
            con.execute('BEGIN IMMEDIATE')
            token, payload, previous, _ = check_remaining_operation(
                con, 'wheat_opening_stock')
            if previous:
                flash('Boshlang‘ich bug‘doy zaxirasi oldin saqlangan. Takroriy kirim yaratilmadi.', 'success')
                return redirect(url_for('wheat'))
            if con.execute(
                    'SELECT 1 FROM wheat_receipts WHERE purchase_method=?',
                    (OPENING_WHEAT_METHOD,)).fetchone():
                raise ValueError('Boshlang‘ich bug‘doy zaxirasi avval kiritilgan.')
            if (con.execute('SELECT 1 FROM wheat_receipts LIMIT 1').fetchone()
                    or con.execute('SELECT 1 FROM production LIMIT 1').fetchone()):
                raise ValueError(
                    'Boshlang‘ich bug‘doy zaxirasini faqat bug‘doy xaridi va ishlab '
                    'chiqarish boshlanishidan oldin kiriting.')
            receipt_id = con.execute('''INSERT INTO wheat_receipts
                (date,supplier,gross,tare,net,cleaned_net,price_per_kg,vat_rate,
                 note,user_id,purchase_method)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                (stock_date, '', amount, 0, amount, amount, 0, 0,
                 note, session['user_id'], OPENING_WHEAT_METHOD)).lastrowid
            save_remaining_operation(
                con, 'wheat_opening_stock', token, payload, receipt_id)
        flash('Boshlang‘ich bug‘doy zaxirasi omborga qo‘shildi; xarid yoki pul harakati yozilmadi.', 'success')
    except (ValueError, KeyError, OverflowError) as exc:
        flash(str(exc) or 'Sana va bug‘doy miqdorini tekshiring.', 'error')
    except sqlite3.Error:
        flash('Boshlang‘ich bug‘doy zaxirasi saqlanmadi; ombor va kassa o‘zgarmadi.', 'error')
    return redirect(url_for('wheat'))

@app.context_processor
def production_context():
    def production_token():
        return URLSafeSerializer(app.secret_key, salt='production-operation-v1').dumps({
            'action': 'production', 'user_id': session['user_id'],
            'nonce': secrets.token_urlsafe(32)})
    return {'production_token': production_token}


@app.route('/ishlab-chiqarish', methods=['GET','POST'])
@login_required
def production_page():
    role = current_user()['role']
    if role not in ('umid', 'rahbar'):
        abort(403)
    if role == 'rahbar' and request.method != 'GET':
        abort(403)
    if request.method == 'POST':
        try:
            unit = request.form.get('unit', 'tonna')
            if unit not in ('kg', 'tonna'): raise ValueError()
            try:
                production_date = _html_date_storage(
                    request.form.get('date') or date.today().isoformat()
                )
            except ValueError as exc:
                flash(f'Ishlab chiqarish sanasi yaroqsiz: {exc}', 'error')
                return redirect(url_for('production_page'))
            factor = 1000 if unit == 'tonna' else 1
            raw = float(request.form['raw_wheat']) * factor
            w = float(request.form['cleaned_wheat']) * factor
            f = float(request.form.get('flour_kg',0))
            b = float(request.form.get('bran_kg',0))
            m = float(request.form.get('muchka_kg',0))
            t = float(request.form.get('telashka_kg',0))
            flour_types = {p: float(request.form.get('flour_'+str(i), 0) or 0) for i,p in enumerate(FLOUR_TYPES)}
            bag_products = request.form.getlist('bag_product')
            bag_sizes = request.form.getlist('bag_kg')
            bag_counts = request.form.getlist('bag_count')
            if not (len(bag_products) == len(bag_sizes) == len(bag_counts)):
                raise ValueError('Qop ishlab chiqarish qatorlarini tekshiring.')
            bag_lines = {}
            for product, raw_size, raw_count in zip(bag_products, bag_sizes, bag_counts):
                if product not in BAGGED_FLOUR_PRODUCTS:
                    raise ValueError('Qopli mahsulot turi noto‘g‘ri.')
                try:
                    size_value = float(raw_size)
                    count_value = float(raw_count) if raw_count.strip() else 0
                except (TypeError, ValueError, OverflowError) as exc:
                    raise ValueError('Qop o‘lchami va sonini tekshiring.') from exc
                if (not math.isfinite(size_value) or size_value not in (25, 50)
                        or not math.isfinite(count_value) or count_value < 0
                        or count_value != int(count_value)):
                    raise ValueError('Qop o‘lchami 25 yoki 50 kg, qop soni esa musbat butun son bo‘lsin.')
                if count_value == 0:
                    continue
                key = (product, int(size_value))
                bag_lines[key] = bag_lines.get(key, 0) + int(count_value)
                flour_types[product] += size_value * count_value
            if not all(math.isfinite(value) for value in (raw,w,f,b,m,t,*flour_types.values())):
                raise ValueError()
            f = sum(flour_types.values())
            if not math.isfinite(f): raise ValueError()
            if min(flour_types.values()) < 0: raise ValueError()
            if raw <= 0 or w <= 0 or w > raw or min(f,b,m,t) < 0 or f+b+m+t > w or f+b+m+t <= 0:
                raise ValueError()
            saved = False
            with db() as con:
                con.execute('BEGIN IMMEDIATE')
                token = request.form.get('operation_token', '')
                try:
                    operation = URLSafeSerializer(app.secret_key, salt='production-operation-v1').loads(token)
                except BadSignature:
                    raise ValueError('Ishlab chiqarish tokeni yaroqsiz.')
                if (not isinstance(operation, dict) or operation.get('action') != 'production'
                        or operation.get('user_id') != session['user_id']):
                    raise ValueError('Token bu foydalanuvchiga tegishli emas.')
                # Bind all submitted production inputs, excluding CSRF and the operation token.
                payload = json.dumps({key: request.form.getlist(key) for key in request.form
                    if key not in ('csrf_token', 'operation_token')},
                    ensure_ascii=False, sort_keys=True, separators=(',', ':'))
                previous = con.execute('SELECT * FROM production_operations WHERE token=?', (token,)).fetchone()
                if previous:
                    if previous['user_id'] != session['user_id'] or previous['payload'] != payload:
                        raise ValueError('Bu token boshqa ishlab chiqarish mazmuni uchun ishlatilgan.')
                    loss = raw - w
                    flash(f'Ishlab chiqarish saqlandi. Tozalash kamayishi: {loss/1000:.3f} t ({loss/raw*100:.2f}%).', 'success')
                    return redirect(url_for('production_page'))
                stock=con.execute('''SELECT
                    (SELECT COALESCE(SUM(net),0) FROM wheat_receipts)
                    - (SELECT COALESCE(SUM(COALESCE(raw_wheat_kg,wheat_kg)),0) FROM production)''').fetchone()[0]
                if raw > stock + 0.000001:
                    flash('Umumiy bug‘doy zaxirasida yetarli qoldiq yo‘q.', 'error')
                else:
                    con.execute('''INSERT INTO production
                        (date,wheat_kg,raw_wheat_kg,flour_kg,bran_kg,note,user_id,muchka_kg,telashka_kg)
                        VALUES(?,?,?,?,?,?,?,?,?)''',
                        (production_date,w,raw,f,b,
                         request.form.get('note','').strip(),session['user_id'],m,t))
                    pid = con.execute('SELECT last_insert_rowid()').fetchone()[0]
                    for product, kg in flour_types.items():
                        if kg: con.execute('INSERT INTO production_flour_types(production_id,product,qty_kg) VALUES(?,?,?)', (pid,product,kg))
                    for (product, bag_kg), bag_count in bag_lines.items():
                        con.execute('''INSERT INTO bagged_production_lines
                            (production_id,product,bag_kg,bag_count) VALUES(?,?,?,?)''',
                            (pid,product,bag_kg,bag_count))
                    con.execute('INSERT INTO production_operations(token,user_id,payload,production_id) VALUES(?,?,?,?)',
                                (token,session['user_id'],payload,pid))
                    saved = True
            if saved:
                loss = raw - w
                flash(f'Ishlab chiqarish saqlandi. Tozalash kamayishi: {loss/1000:.3f} t ({loss/raw*100:.2f}%).', 'success')
                return redirect(url_for('production_page'))
        except sqlite3.Error:
            flash("Ishlab chiqarish saqlanmadi. Shu so‘rovni qayta yuboring.", 'error')
        except (ValueError, KeyError):
            flash('Vaznlarni tekshiring: tozalangan vazn olingan vazndan oshmasin, mahsulotlar jami esa tozalangan vazndan oshmasin.', 'error')
    with db() as con:
        rows,pagination=paginate_rows(
            con, 'SELECT * FROM production ORDER BY id DESC',
            'SELECT COUNT(*) FROM production',
        )
        bag_lines = {}
        production_ids=[row['id'] for row in rows]
        if production_ids:
            placeholders=','.join('?' for _ in production_ids)
            bagged_sql=f"""SELECT production_id,product,bag_kg,bag_count
                FROM bagged_production_lines
                WHERE production_id IN ({placeholders})
                ORDER BY product,bag_kg"""
            bagged_params=production_ids
        else:
            bagged_sql=None
            bagged_params=()
        for line in (con.execute(bagged_sql,bagged_params) if bagged_sql else ()):
            bag_lines.setdefault(line['production_id'],[]).append({
                'product':line['product'],'bag_kg':line['bag_kg'],
                'bag_count':line['bag_count'],
                'qty_kg':line['bag_kg']*line['bag_count'],
            })
        total=con.execute('SELECT COALESCE(SUM(net),0) FROM wheat_receipts').fetchone()[0]
        used=con.execute('SELECT COALESCE(SUM(COALESCE(raw_wheat_kg,wheat_kg)),0) FROM production').fetchone()[0]
    return render_template('production.html', rows=rows, stock=total-used,
        flour_types=FLOUR_TYPES, bagged_flour_products=BAGGED_FLOUR_PRODUCTS,
        bag_lines=bag_lines,pagination=pagination)

@app.context_processor
def accountant_sale_context():
    def accountant_sale_token():
        return URLSafeSerializer(app.secret_key, salt='accountant-sale-operation-v1').dumps({
            'action': 'accountant_sale', 'user_id': session['user_id'],
            'nonce': secrets.token_urlsafe(32)})
    return {'accountant_sale_token': accountant_sale_token}


@app.route('/sotuv', methods=['GET','POST'])
@login_required
def sales_page():
    if request.method == 'POST':
        try:
            product, mode, bag_kg, count, unit_price, qty, total, paid = sale_values(request.form)
            customer = request.form.get('customer','').strip()
            if not customer or not request.form.get('customer_phone','').strip():
                raise ValueError('Xaridor nomi va telefon raqami majburiy.')
            sale_date = _html_date_storage(request.form.get('date') or date.today().isoformat())
            due = _business_date_storage(_business_date_after(sale_date,25)) if total - paid > 0.009 else ''
            with db() as con:
                con.execute('BEGIN IMMEDIATE')
                token = request.form.get('operation_token', '')
                try:
                    operation = URLSafeSerializer(app.secret_key, salt='accountant-sale-operation-v1').loads(token)
                except BadSignature:
                    raise ValueError('Sotuv tokeni yaroqsiz. Formani qayta oching.')
                if (not isinstance(operation, dict) or operation.get('action') != 'accountant_sale'
                        or operation.get('user_id') != session['user_id']):
                    raise ValueError('Sotuv tokeni bu foydalanuvchiga tegishli emas.')
                payload = json.dumps({key: request.form.getlist(key) for key in request.form
                    if key not in ('csrf_token', 'operation_token')},
                    ensure_ascii=False, sort_keys=True, separators=(',', ':'))
                previous = con.execute('SELECT * FROM accountant_sale_operations WHERE token=?', (token,)).fetchone()
                if previous:
                    if previous['user_id'] != session['user_id'] or previous['payload'] != payload:
                        raise ValueError('Bu token boshqa sotuv mazmuni uchun ishlatilgan.')
                    return redirect(url_for('invoice_saved', invoice_id=previous['invoice_id']))
                worker_role = next(
                    (role for role, products in WORKER_PRODUCTS.items() if product in products),
                    None)
                if worker_role is None:
                    raise ValueError('Mahsulot Umid yoki Maxmud sotuviga biriktirilmagan.')
                worker_rows = con.execute(
                    'SELECT id FROM users WHERE role=? AND active=1 ORDER BY id',
                    (worker_role,)).fetchall()
                if len(worker_rows) != 1:
                    raise ValueError('Mahsulotga tegishli yagona faol xodim topilmadi; sotuv saqlanmadi.')
                seller_id = worker_rows[0]['id']
                recipient_id = None
                sale_payment_method = 'naqd'
                if paid > 0:
                    recipient_choice = request.form.get('initial_payment_recipient', '').strip()
                    if not recipient_choice:
                        raise ValueError('Boshlang‘ich to‘lov bo‘lsa, pulni qabul qilgan shaxsni tanlang.')
                    if recipient_choice == 'buxgalter':
                        recipient_id = session['user_id']
                    elif recipient_choice == 'worker':
                        recipient_id = seller_id
                    else:
                        raise ValueError('Pulni qabul qilgan shaxs noto‘g‘ri.')
                    if not request.form.get('payment_method', '').strip():
                        raise ValueError('Boshlang‘ich to‘lov bo‘lsa, to‘lov usulini tanlang.')
                    sale_payment_method = payment_method_from_form(request.form)
                validate_bagged_sale(con,product,mode,bag_kg,count)
                if qty > product_stocks(con).get(product, 0) + 0.000001:
                    flash(f'{product} omborida yetarli qoldiq yo‘q.', 'error')
                else:
                    sale_cursor = con.execute('''INSERT INTO sales(date,customer,customer_phone,due_date,product,qty_kg,
                        price_per_kg,vat_rate,paid,note,user_id,sale_mode,bag_kg,bag_count,unit_price,
                        payment_method,initial_payment_recipient_id)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                        (sale_date,customer,request.form.get('customer_phone','').strip(),due,product,qty,
                         total/qty,0,paid,request.form.get('note','').strip(),seller_id,
                         mode,bag_kg,count,unit_price,sale_payment_method,recipient_id))
                    record_bagged_sale(con,sale_cursor.lastrowid,product,mode,bag_kg,count)
                    invoice_id = ensure_invoice(con, sale_cursor.lastrowid, product, sale_date)
                    con.execute('INSERT INTO accountant_sale_operations(token,user_id,payload,sale_id,invoice_id) VALUES(?,?,?,?,?)',
                                (token,session['user_id'],payload,sale_cursor.lastrowid,invoice_id))
                    return redirect(url_for('invoice_saved', invoice_id=invoice_id))
        except (ValueError, KeyError, OverflowError, sqlite3.Error) as exc:
            flash(str(exc) if str(exc) else 'Mahsulot, qop soni, miqdor va narxni tekshiring.', 'error')
    with db() as con:
        customer_context=customer_identity_context(con)
        raw_rows,pagination=paginate_rows(
            con,
            """SELECT sales.*, invoices.id AS invoice_id,
                invoices.number AS invoice_number,
                CASE WHEN sale_cancellations.sale_id IS NULL THEN 0 ELSE 1 END AS cancelled
                FROM sales
                LEFT JOIN invoices ON invoices.sale_id=sales.id
                LEFT JOIN sale_cancellations ON sale_cancellations.sale_id=sales.id
                ORDER BY sales.id DESC""",
            'SELECT COUNT(*) FROM sales',
        )
        rows=[]
        for row in raw_rows:
            current_name,current_phone=customer_display(
                customer_context,row['customer'],row['customer_phone'])
            rows.append(dict(row,customer=current_name,customer_phone=current_phone))
        customers=con.execute('SELECT name AS customer,phone AS customer_phone FROM customers ORDER BY name,id').fetchall()
        stocks=product_stocks(con)
        bagged_stocks=bagged_product_stocks(con)
        sale_workers = {role: [] for role in WORKER_PRODUCTS}
        for worker in con.execute(
                "SELECT id,username,role FROM users WHERE active=1 AND role IN (?,?) ORDER BY id",
                tuple(WORKER_PRODUCTS)):
            sale_workers[worker['role']].append(
                {'id': worker['id'], 'username': worker['username']})
    return render_template('sales.html', rows=rows, stocks=stocks,
        bagged_stocks=bagged_stocks,
        customers=[{'name':r['customer'],'phone':r['customer_phone']} for r in customers],
        products=PRODUCTS, bag_options=BAG_OPTIONS, loose_products=LOOSE_PRODUCTS,
        worker_products=WORKER_PRODUCTS, sale_workers=sale_workers,
        pagination=pagination)

@app.route('/nakladnoy/saqlandi/<int:invoice_id>')
@login_required
def invoice_saved(invoice_id):
    with db() as con:
        inv = con.execute('SELECT * FROM invoices WHERE id=?', (invoice_id,)).fetchone()
    if not inv:
        return 'Nakladnoy topilmadi', 404
    return render_template('invoice_saved.html', invoice=inv)

@app.route('/nakladnoylar')
@login_required
def invoices_page():
    search=request.args.get('q','').strip()[:100]
    search_conditions="""(
        instr(lower(COALESCE(CAST(i.issued_date AS TEXT),'')),lower(?))>0 OR
        instr(lower(substr(business_date_key(i.issued_date),9,2)||'.'||
            substr(business_date_key(i.issued_date),6,2)||'.'||
            substr(business_date_key(i.issued_date),1,4)),lower(?))>0 OR
        instr(lower(CASE s.product
            WHEN 'Robiya' THEN 'Oliy nav un'
            WHEN 'Sara' THEN '1-nav un'
            WHEN 'Yevro' THEN '1-nav un'
            WHEN 'Zar Bug‘doy' THEN '1-nav un'
            WHEN 'Telashka atxod' THEN 'Teleshka atxod'
            ELSE s.product END),lower(?))>0 OR
        instr(lower(COALESCE(s.customer,'')),lower(?))>0 OR
        instr(lower(COALESCE(s.customer_phone,'')),lower(?))>0 OR
        instr(lower(COALESCE(s.product,'')),lower(?))>0 OR
        instr(printf('%.2f',COALESCE(s.qty_kg,0)),lower(?))>0 OR
        instr(CAST(i.number AS TEXT),?)>0
    )"""
    search_params=(search,search,search,search,search,search,search,search)
    with db() as con:
        rows,pagination=paginate_rows(
            con,
            f"""SELECT i.*, s.customer, s.customer_phone, s.product, s.qty_kg
                FROM invoices i JOIN sales s ON s.id=i.sale_id
                WHERE {search_conditions}
                ORDER BY business_date_key(i.issued_date) DESC, i.id DESC""",
            f"""SELECT COUNT(*) FROM invoices i JOIN sales s ON s.id=i.sale_id
                WHERE {search_conditions}""",
            search_params,
        )
    return render_template('invoices.html', rows=rows, groups=INVOICE_GROUPS,
        pagination=pagination,search=search)

@app.route('/nakladnoy/<int:invoice_id>')
@login_required
def invoice_print(invoice_id):
    with db() as con:
        row = con.execute("""SELECT i.*, s.customer, s.customer_phone, s.product,
                            s.sale_mode, s.bag_kg, s.bag_count, s.qty_kg
                            FROM invoices i JOIN sales s ON s.id=i.sale_id
                            WHERE i.id=?""", (invoice_id,)).fetchone()
    if not row:
        return 'Nakladnoy topilmadi', 404
    with db() as con:
        is_batch=con.execute('SELECT 1 FROM worker_sale_batch_items WHERE sale_id=?',(row['sale_id'],)).fetchone() if 'sale_id' in row.keys() else None
    if is_batch: return redirect(url_for('worker_batch_invoice',invoice_id=invoice_id))
    group_name = INVOICE_GROUPS.get(row['product'], ('boshqa','Boshqa'))[1]
    return render_template('invoice_print.html', r=row, group_name=group_name)

@app.route('/sotuv/tahrirlash/<int:sale_id>', methods=['GET','POST'])
@login_required
def edit_sale(sale_id):
    with db() as con:
        if request.method == 'POST':
            con.execute('BEGIN IMMEDIATE')
        sale=con.execute('SELECT * FROM sales WHERE id=?',(sale_id,)).fetchone()
        if con.execute('SELECT 1 FROM sale_cancellations WHERE sale_id=?',(sale_id,)).fetchone():
            flash('Bekor qilingan sotuv tahrirlanmaydi.', 'error'); return redirect(url_for('sales_page'))
        if con.execute('SELECT 1 FROM invoices WHERE sale_id=?',(sale_id,)).fetchone():
            flash('Nakladnoy raqami berilgan sotuvni tahrirlash mumkin emas. Xato bo‘lsa sotuvni bekor qilib, yangisini kiriting.', 'error'); return redirect(url_for('sales_page'))
        if sale is not None and sale['product'] == 'Un':
            flash('Eski Un sinov sotuvini yangi turga tahrirlab o‘tkazmang; sinov oxirida tozalanadi.', 'error')
            return redirect(url_for('sales_page'))
        if sale is None:
            flash('Sotuv topilmadi.', 'error'); return redirect(url_for('sales_page'))
        version_row = con.execute('SELECT version FROM sale_edit_versions WHERE sale_id=?', (sale_id,)).fetchone()
        edit_state = {'action': 'edit_sale', 'user_id': session['user_id'],
                      'version': version_row['version'] if version_row else 0, 'sale': dict(sale)}
        if request.method == 'POST':
            try:
                try:
                    submitted_state = URLSafeSerializer(app.secret_key, salt='sale-edit-state-v1').loads(request.form.get('edit_version_token', ''))
                except BadSignature:
                    raise ValueError('Tahrirlash formasi yaroqsiz. Sahifani qayta oching.')
                if submitted_state != edit_state:
                    raise ValueError('Sotuv boshqa so‘rovda o‘zgargan yoki forma sizga tegishli emas. Sahifani qayta ochib, yangi ma’lumotni tekshiring.')

                product, mode, bag_kg, count, unit_price, qty, total, paid = sale_values(request.form)
                customer=request.form.get('customer','').strip()
                if not customer or not request.form.get('customer_phone','').strip():
                    raise ValueError('Xaridor nomi va telefon raqami majburiy.')
                available=product_stocks(con).get(product,0)
                if sale['product'] == product: available += sale['qty_kg']
                validate_bagged_sale(
                    con,product,mode,bag_kg,count,exclude_sale_id=sale_id)
                if qty > available + 0.000001:
                    flash(f'{product} omborida yetarli qoldiq yo‘q.', 'error')
                else:
                    sale_date=_html_date_storage(request.form.get('date') or date.today().isoformat())
                    due=_business_date_storage(_business_date_after(sale_date,25)) if total - paid > 0.009 else ''
                    con.execute('''UPDATE sales SET date=?,customer=?,customer_phone=?,due_date=?,product=?,
                        qty_kg=?,price_per_kg=?,vat_rate=0,paid=?,note=?,sale_mode=?,bag_kg=?,bag_count=?,unit_price=?
                        WHERE id=?''', (sale_date,customer,request.form.get('customer_phone','').strip(),due,product,
                        qty,total/qty,paid,request.form.get('note','').strip(),mode,bag_kg,count,unit_price,sale_id))
                    record_bagged_sale(con,sale_id,product,mode,bag_kg,count)
                    con.execute('INSERT INTO sale_edit_versions(sale_id,version) VALUES(?,1) ON CONFLICT(sale_id) DO UPDATE SET version=version+1', (sale_id,))
                    flash('Sotuv tahrirlandi.', 'success'); return redirect(url_for('sales_page'))
            except (ValueError, KeyError, OverflowError) as exc: flash(str(exc) if str(exc) else 'Miqdor, qop va narxni tekshiring.', 'error')
    return render_template('edit_sale.html',sale=sale,products=PRODUCTS,
                           edit_version_token=URLSafeSerializer(app.secret_key, salt='sale-edit-state-v1').dumps(edit_state),
                           bag_options=BAG_OPTIONS,loose_products=LOOSE_PRODUCTS)

def customer_debt_details(con, customer):
    """Oldest-first payment allocation across sales and opening debts."""
    obligations = []
    for r in con.execute("""SELECT id, date, due_date, qty_kg*price_per_kg-paid-COALESCE((SELECT SUM(wr.amount) FROM worker_receipts wr WHERE wr.sale_id=sales.id),0) balance
                            FROM sales WHERE customer=? AND id NOT IN (SELECT sale_id FROM sale_cancellations)""", (customer,)):
        due = r['due_date'] or _business_date_after(r['date'],25)
        _business_date(due)
        if r['balance'] > 0.009:
            obligations.append({'kind':'sale', 'id':r['id'], 'date':r['date'], 'due_date':due, 'balance':float(r['balance'])})
    for r in con.execute('''SELECT d.id,d.date,d.due_date,d.amount,
             COALESCE((SELECT SUM(r.amount) FROM opening_debt_receipts r WHERE r.opening_debt_id=d.id),0) paid
             FROM opening_debts d WHERE d.customer=?''', (customer,)):
        _business_date(r['date'])
        _business_date(r['due_date'])
        remaining = float(r['amount'])-float(r['paid'])
        if remaining>0.009:
            obligations.append({'kind':'opening', 'id':r['id'], 'date':r['date'], 'due_date':r['due_date'], 'balance':remaining})
    obligations.sort(key=lambda r: (_business_date(r['date'])[0], 0 if r['kind']=='opening' else 1, r['id']))
    remaining_payments = float(con.execute('SELECT COALESCE(SUM(amount),0) FROM payments WHERE customer=?', (customer,)).fetchone()[0] or 0)
    items = []
    for obligation in obligations:
        applied = min(obligation['balance'], max(0.0, remaining_payments))
        remaining_payments -= applied
        balance = obligation['balance'] - applied
        if balance > 0.009:
            items.append({'kind': obligation['kind'], 'id':obligation['id'], 'balance':balance, 'due_date':obligation['due_date']})
    return items

def debt_customers(con, financial=None, per_obligation=False):
    # Debt pages and exports retain customer totals; control can classify each due separately.
    if financial is None:
        financial=calculate_financial_snapshot(con)
    if financial is not None:
        raw_phones = {}
        customer_context=customer_identity_context(con)
        if not per_obligation:
            for table, kind in (('sales', 'sale'), ('opening_debts', 'opening')):
                for row in con.execute('SELECT id,customer,customer_phone FROM '+table):
                    raw_phones[(kind,row['id'])] = customer_display(
                        customer_context,row['customer'],row['customer_phone'])[1]
        result = []
        for (name, phone), totals in financial['customers'].items():
            if totals['debt'] <= 0:
                continue
            unpaid = [item for item in totals['obligations'] if item['balance'] > 0]
            if per_obligation:
                for item in unpaid:
                    due = item['due_date'] or _business_date_after(item['date'], 25)
                    card = customer_context['cards'].get(totals['customer_id'])
                    result.append({'customer': name,
                        'phone': card['phone'] if card else phone,
                        'debt': item['balance'], 'due_date': due,
                        'kind': item['kind'], 'obligation_id': item['id']})
                continue
            due = min(
                (item['due_date'] or _business_date_after(item['date'],25) for item in unpaid),
                key=lambda value: _business_date(value)[0],
            )
            phones = sorted({raw_phones[(item['kind'],item['id'])] for item in totals['obligations']})
            result.append({'customer':name,'phone':phone,'phones':phones,
                'total':totals['sales_revenue']+totals['opening_debt'],
                'sales_revenue':totals['sales_revenue'],'opening_debt':totals['opening_debt'],
                'initial_paid':totals['initial_paid'],'linked_paid':totals['linked_paid'],
                'legacy_received':totals['legacy_received'],'legacy_applied':totals['legacy_applied'],
                'paid':totals['received'],'excess':totals['linked_excess']+totals['legacy_excess'],
                'debt':totals['debt'],'due_date':due})
        return sorted(result,key=lambda r:(r['customer'].casefold(),r['phone']))

@app.route('/eski-qarz', methods=['GET','POST'])
@login_required
def opening_debt_page():
    if request.method=='POST':
        if current_user()['role'] != 'buxgalter':
            abort(403)
        try:
            worker_raw = request.form.get('worker_id', '').strip()
            if not worker_raw.isdecimal():
                raise ValueError('Mas’ul xodimni tanlang.')
            worker_id = int(worker_raw)
            customer=request.form.get('customer','').strip()
            phone=request.form.get('customer_phone','').strip()
            amount=float(request.form.get('amount','0'))
            debt_date=request.form.get('date','').strip()
            due=request.form.get('due_date','').strip()
            if not customer or not phone: raise ValueError('Xaridor ismi va telefon raqami majburiy.')
            if not math.isfinite(amount) or amount<=0: raise ValueError('Qarz summasi musbat bo‘lishi kerak.')
            if not debt_date or not due: raise ValueError('Qarz sanasi va muddati majburiy.')
            debt_day = iso_to_date(debt_date)
            due_day = iso_to_date(due)
            if due_day < debt_day: raise ValueError('To‘lov muddati qarz sanasidan oldin bo‘lmasin.')
            debt_date = _html_date_storage(debt_date)
            due = _html_date_storage(due)
            with db() as con:
                con.execute('BEGIN IMMEDIATE')
                token, payload, previous, _ = check_remaining_operation(con, 'opening_debt')
                if previous:
                    flash('Eski qarz oldin saqlangan. Takror qarz yaratilmadi.', 'success')
                    return redirect(url_for('debtors_page'))
                worker = con.execute("SELECT id FROM users WHERE id=? AND role IN ('umid','maxmud') AND active=1", (worker_id,)).fetchone()
                if worker is None:
                    raise ValueError('Tanlangan mas’ul xodim mavjud yoki faol emas.')
                debt_id = con.execute('INSERT INTO opening_debts(customer,customer_phone,amount,date,due_date,note,user_id,worker_id) VALUES(?,?,?,?,?,?,?,?)',
                    (customer,phone,amount,debt_date,due,request.form.get('note','').strip(),session['user_id'],worker_id)).lastrowid
                save_remaining_operation(con, 'opening_debt', token, payload, debt_id)
            flash('Eski qarz saqlandi. Ombor va sotuv summasi o‘zgarmadi.','success')
            return redirect(url_for('debtors_page'))
        except (ValueError,OverflowError,sqlite3.Error) as exc:
            flash(str(exc) or 'Ma’lumotlarni tekshiring.','error')
    with db() as con:
        customer_context=customer_identity_context(con)
        workers = con.execute("SELECT id,username,role FROM users WHERE role IN ('umid','maxmud') AND active=1 ORDER BY id").fetchall()
        history,history_pagination=paginate_rows(con,
            '''SELECT d.*,u.username AS worker_name FROM opening_debts d LEFT JOIN users u ON u.id=d.worker_id ORDER BY d.id DESC''',
            'SELECT COUNT(*) FROM opening_debts')
        display_history=[]
        for row in history:
            name,phone=customer_display(
                customer_context,row['customer'],row['customer_phone'])
            display_history.append(dict(row,customer=name,customer_phone=phone))
    return render_template('opening_debt.html',rows=display_history,
        today=date.today().isoformat(),pagination=history_pagination,workers=workers)


@app.route('/eski-qarz/<int:debt_id>/masul', methods=['POST'])
@login_required
def assign_opening_debt_worker(debt_id):
    """Faqat ilgari biriktirilmagan qarzni buxgalter nomiga bog'lash.

    Qarz summasi, ombor, tushum yoki tarixiy to'lovlar qayta yozilmaydi.
    """
    if current_user()['role'] != 'buxgalter':
        abort(403)
    try:
        worker_raw = request.form.get('worker_id', '').strip()
        if not worker_raw.isdecimal():
            raise ValueError('Mas’ul xodimni tanlang.')
        worker_id = int(worker_raw)
        with db() as con:
            con.execute('BEGIN IMMEDIATE')
            token, payload, previous, _ = check_remaining_operation(con, 'opening_debt_assign')
            if previous:
                flash('Mas’ul xodim ilgari biriktirilgan. Takror o‘zgartirilmadi.', 'success')
                return redirect(url_for('opening_debt_page'))
            debt = con.execute('SELECT id,worker_id FROM opening_debts WHERE id=?', (debt_id,)).fetchone()
            if debt is None:
                raise ValueError('Eski qarz topilmadi.')
            if debt['worker_id'] is not None:
                raise ValueError('Bu qarzga mas’ul xodim allaqachon biriktirilgan; qayta biriktirish alohida audit talab qiladi.')
            worker = con.execute("SELECT id FROM users WHERE id=? AND role IN ('umid','maxmud') AND active=1", (worker_id,)).fetchone()
            if worker is None:
                raise ValueError('Tanlangan mas’ul xodim mavjud yoki faol emas.')
            con.execute('UPDATE opening_debts SET worker_id=? WHERE id=? AND worker_id IS NULL', (worker_id,debt_id))
            con.execute("INSERT INTO opening_debt_assignment_audit (opening_debt_id,old_worker_id,new_worker_id,actor_user_id) VALUES(?,NULL,?,?)", (debt_id,worker_id,session['user_id']))
            save_remaining_operation(con,'opening_debt_assign',token,payload,debt_id)
        flash('Eski qarz mas’ul xodimga biriktirildi; qarz summasi o‘zgarmadi.','success')
    except (ValueError,sqlite3.Error) as exc:
        flash(str(exc) or 'Biriktirishda xato.','error')
    return redirect(url_for('opening_debt_page'))


@app.route('/qarzdorlar')
@login_required
def debtors_page():
    today = date.today()
    with db() as con:
        con.execute('BEGIN')
        try:
            financial = calculate_financial_snapshot(con)
        except ValueError as exc:
            abort(400, f'Qarz sanalarini tekshiring: {exc}')
        financial_warnings = list(financial['warnings'])
        phones_by_name = {}
        for name, phone in financial['customers']:
            phones_by_name.setdefault(name, set()).add(phone)
        for name, phones in phones_by_name.items():
            if len(phones) > 1:
                financial_warnings.append({'code':'same_name_multiple_phones','customer':name})
        groups = {'ontime': [], 'soon': [], 'late': []}
        for r in debt_customers(con, financial=financial):
            due, _ = _business_date(r['due_date'])
            days_left = (due - today).days
            item = dict(r, days_left=days_left)
            if days_left < 0: groups['late'].append(item)
            elif days_left <= 5: groups['soon'].append(item)
            else: groups['ontime'].append(item)
        debt_total = sum(r['debt'] for g in groups.values() for r in g)
        group_pagination={}
        group_counts={}
        for key in groups:
            group_counts[key]=len(groups[key])
            groups[key],group_pagination[key]=paginate_items(
                groups[key],f'{key}_page',f'{key}_per_page')
        unallocated_legacy,unallocated_pagination=paginate_items(
            financial['unallocated_legacy'],'unallocated_page','unallocated_per_page')
    return render_template('debtors.html', groups=groups, debt_total=debt_total,
        financial_totals=financial['totals'],financial_warnings=financial_warnings,
        unallocated_legacy=unallocated_legacy,
        debt_difference=financial['totals']['debt']-debt_total,today=today.isoformat(),
        group_pagination=group_pagination,group_counts=group_counts,
        unallocated_pagination=unallocated_pagination)

@app.context_processor
def accountant_payment_context():
    def accountant_payment_token(worker_id, customer, phone):
        return URLSafeSerializer(app.secret_key, salt='accountant-payment-operation-v1').dumps({
            'action': 'accountant_payment', 'accountant_id': session['user_id'],
            'worker_id': worker_id, 'customer': customer, 'phone_key': phone_key(phone) or phone,
            'nonce': secrets.token_urlsafe(32)})
    return {'accountant_payment_token': accountant_payment_token}


@app.route('/tolovlar', methods=['GET', 'POST'])
@login_required
def payments_page():
    # accountant_worker_payment_page_marker
    if current_user()['role'] not in ('buxgalter','rahbar'): abort(403)
    user=current_user()
    try:
        with db() as con:
            con.execute('BEGIN IMMEDIATE' if request.method=='POST' else 'BEGIN')
            financial = calculate_financial_snapshot(con)
            customer_context = customer_identity_context(con)
            workers=con.execute("SELECT id,username FROM users WHERE role IN ('umid','maxmud') AND active=1 ORDER BY id").fetchall()
            selected_worker=request.form.get('worker_id','') if request.method=='POST' else request.args.get('worker_id','')
            if selected_worker and not selected_worker.isdecimal(): raise ValueError('Xodimni tanlang.')
            wid=int(selected_worker) if selected_worker else None
            if request.method=='POST':
                if user['role']!='buxgalter': abort(403)
                if wid is None or wid not in [w['id'] for w in workers]: raise ValueError('Umid yoki Maxmudni tanlang.')
                phone=request.form.get('customer_phone','').strip()
                customer=request.form.get('customer','').strip()
                if not customer or not phone: raise ValueError('Qarzdor xaridorni tanlang.')
                method=payment_method_from_form(request.form)
                cents=payment_amount_cents(request.form.get('amount','0'))
                amount=cents/100
                pay_date=_html_date_storage(request.form.get('date') or date.today().isoformat())
                if _business_date(pay_date)[0] > date.today(): raise ValueError('Kelajak sanasi mumkin emas.')
                key=phone_key(phone) or phone
                token=request.form.get('operation_token','')
                try:
                    operation=URLSafeSerializer(app.secret_key, salt='accountant-payment-operation-v1').loads(token)
                except BadSignature:
                    raise ValueError('To‘lov tokeni yaroqsiz. Formani qayta oching.')
                if (not isinstance(operation,dict) or operation.get('action')!='accountant_payment'
                        or operation.get('accountant_id')!=user['id'] or operation.get('worker_id')!=wid
                        or operation.get('customer')!=customer or operation.get('phone_key')!=key):
                    raise ValueError('To‘lov tokeni bu foydalanuvchi, xodim yoki xaridorga tegishli emas.')
                payload=json.dumps({'worker_id':wid,'customer':customer,'phone':phone,'amount_cents':cents,
                    'payment_method':method,'date':pay_date,'note':request.form.get('note','')},
                    ensure_ascii=False,sort_keys=True,separators=(',',':'))
                previous=con.execute('SELECT * FROM accountant_payment_operations WHERE token=?',(token,)).fetchone()
                if previous:
                    if previous['accountant_id']!=user['id'] or previous['payload']!=payload:
                        raise ValueError('Bu token boshqa to‘lov mazmuni uchun ishlatilgan.')
                    flash('To‘lov saqlandi. Qarz kamaydi, pul faqat buxgalter kassasiga yozildi.','success')
                    return redirect(url_for('payments_page',worker_id=wid))
                if snapshot_has_ambiguous_legacy(financial, customer):
                    raise ValueError('Eski to‘lov xaridorning telefoniga ishonchli bog‘lanmagan. Avval aniqlashtiring.')
                debts=[(item['kind'],item['id'],balance_cents(item['balance']))
                       for item in worker_snapshot_obligations(financial,wid,customer,phone)
                       if balance_cents(item['balance'])>0]
                if not debts: raise ValueError('Ushbu xodimda xaridor qarzi topilmadi.')
                if cents>sum(r for _,_,r in debts): raise ValueError('To‘lov qarzdan oshmasligi kerak.')
                con.execute('INSERT INTO accountant_payment_operations(token,accountant_id,payload) VALUES(?,?,?)',
                            (token,user['id'],payload))
                left=cents
                note=request.form.get('note','').strip()[:300]
                for kind,oid,rem in debts:
                    if not left: break
                    part=min(left,rem);left-=part
                    if kind == 'opening':
                        con.execute('''INSERT INTO opening_debt_receipts
                            (opening_debt_id,worker_id,amount,date,payment_method,received_by,receiver_id,operation_token,note)
                            VALUES(?,?,?,?,?,'accountant',?,?,?)''',
                            (oid,wid,part/100,pay_date,method,user['id'],token,note))
                    else:
                        receipt=con.execute('INSERT INTO worker_receipts(sale_id,worker_id,amount,date,payment_method) VALUES(?,?,?,?,?)',
                           (oid,wid,part/100,pay_date,method)).lastrowid
                        accountant_receipt=con.execute("""INSERT INTO accountant_worker_receipts
                           (receipt_id,worker_id,accountant_id,customer,customer_phone,payment_method,amount,payment_date,note)
                           VALUES(?,?,?,?,?,?,?,?,?)""",(receipt,wid,user['id'],customer,phone,method,part/100,pay_date,note)).lastrowid
                        con.execute('INSERT INTO accountant_payment_operation_items(token,receipt_id,accountant_receipt_id) VALUES(?,?,?)',
                                    (token,receipt,accountant_receipt))
                flash('To‘lov saqlandi. Qarz kamaydi, pul faqat buxgalter kassasiga yozildi.','success')
                return redirect(url_for('payments_page',worker_id=wid))
            groups={}
            if wid:
                for item in worker_snapshot_obligations(financial,wid):
                    if item['balance']<=Decimal('0.009'):continue
                    k=(item['customer'],item['phone'])
                    group=groups.setdefault(k,{'debt':Decimal('0'),'legacy_estimated':Decimal('0')})
                    group['debt']+=item['balance']
                    group['legacy_estimated']+=item['legacy_applied']
            customers=[{
                'name':k[0],'phone':k[1],'debt':round(v['debt'],2),
                'legacy_estimated_amount':round(v['legacy_estimated'],2),
                'payment_blocked':snapshot_has_ambiguous_legacy(financial,k[0]),
            } for k,v in sorted(groups.items())]
            history,history_pagination=paginate_rows(con,
                """SELECT * FROM (
                    SELECT a.payment_date,a.customer,a.customer_phone,a.payment_method,
                           a.amount,a.note,u.username worker_name,au.username accountant_name,
                           'sotuv' AS source_type,a.id AS source_id
                    FROM accountant_worker_receipts a
                    JOIN users u ON u.id=a.worker_id
                    JOIN users au ON au.id=a.accountant_id
                    UNION ALL
                    SELECT r.date,d.customer,d.customer_phone,r.payment_method,r.amount,r.note,
                           u.username,au.username,'eski_qarz',r.id
                    FROM opening_debt_receipts r JOIN opening_debts d ON d.id=r.opening_debt_id
                    JOIN users u ON u.id=r.worker_id JOIN users au ON au.id=r.receiver_id
                    WHERE r.received_by='accountant'
                ) ORDER BY business_date_key(payment_date) DESC,source_id DESC""",
                """SELECT (SELECT COUNT(*) FROM accountant_worker_receipts)
                           +(SELECT COUNT(*) FROM opening_debt_receipts WHERE received_by='accountant')""",
                page_param='history_page',per_page_param='history_per_page')
            legacy,legacy_pagination=paginate_rows(con,
                'SELECT * FROM payments ORDER BY id DESC',
                'SELECT COUNT(*) FROM payments',
                page_param='legacy_page',per_page_param='legacy_per_page')
            display_history=[]
            for row in history:
                name,phone=customer_display(
                    customer_context,row['customer'],row['customer_phone'])
                display_history.append(dict(row,customer=name,customer_phone=phone))
            history=display_history
            legacy = [
                dict(row, customer=customer_display(
                    customer_context,row['customer'])[0])
                for row in legacy
            ]
        return render_template('payments.html',workers=workers,selected_worker=wid,customers=customers,
            rows=history,legacy_rows=legacy,payment_methods=PAYMENT_METHODS,
            today=date.today().isoformat(),history_pagination=history_pagination,
            legacy_pagination=legacy_pagination)
    except (ValueError,OverflowError,sqlite3.Error) as exc:
        flash(str(exc) or 'To‘lovni tekshiring.','error')
        return redirect(url_for('payments_page'))

@app.route('/tolovlar/xodim-excel')
@login_required
def accountant_worker_payments_excel():
    if current_user()['role'] not in ('buxgalter','rahbar'): abort(403)
    from flask import Response
    from io import BytesIO
    from zipfile import ZipFile, ZIP_DEFLATED
    from xml.sax.saxutils import escape
    from openpyxl.utils.datetime import to_excel
    from excel_dates import excel_date
    with db() as con:
        customer_context=customer_identity_context(con)
        rows=con.execute("""SELECT a.payment_date,u.username worker_name,a.customer,a.customer_phone,a.payment_method,a.amount,au.username accountant_name,a.note FROM accountant_worker_receipts a JOIN users u ON u.id=a.worker_id JOIN users au ON au.id=a.accountant_id ORDER BY a.id DESC""").fetchall()
    headings=['Sana','Xodim','Xaridor','Telefon','To‘lov turi','Summa (so‘m)','Qabul qilgan','Izoh']
    values=[headings]
    for r in rows:
        customer,phone=customer_display(customer_context,r['customer'],r['customer_phone'])
        values.append([r['payment_date'],r['worker_name'],customer,phone,
            PAYMENT_METHODS.get(r['payment_method'],r['payment_method']),
            float(r['amount']),r['accountant_name'],r['note']])
    def col(n):
        ret=''
        while n:
            n,k=divmod(n-1,26);ret=chr(65+k)+ret
        return ret
    rendered=[]
    for y,line in enumerate(values,1):
        cells=[]
        for x,v in enumerate(line,1):
            addr=col(x)+str(y)
            if y > 1 and x == 1:
                try: value = excel_date(v)
                except ValueError as exc: abort(400, str(exc))
                cells.append('<c r="%s" s="1">%s</c>' % (addr, '<v>%s</v>' % to_excel(value) if value is not None else ''))
            elif isinstance(v,(float,int)):
                cells.append('<c r="%s"><v>%s</v></c>'%(addr,str(v)))
            else:
                cells.append('<c r="%s" t="inlineStr"><is><t>%s</t></is></c>'%(addr,escape(str(v or ''))))
        rendered.append('<row r="%s">%s</row>'%(y,''.join(cells)))
    NS='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    worksheet=('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="'+NS+'"><sheetViews><sheetView workbookViewId="0">'
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        '</sheetView></sheetViews><cols>'
        '<col min="1" max="1" width="16" customWidth="1"/>'
        '<col min="2" max="3" width="24" customWidth="1"/>'
        '<col min="4" max="4" width="21" customWidth="1"/>'
        '<col min="5" max="5" width="24" customWidth="1"/>'
        '<col min="6" max="6" width="20" customWidth="1"/>'
        '<col min="7" max="8" width="25" customWidth="1"/>'
        '</cols><sheetData>'+''.join(rendered)+'</sheetData>'
        '<autoFilter ref="A1:H'+str(len(values))+'"/></worksheet>')
    b=BytesIO()
    with ZipFile(b,'w',ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml','<?xml version="1.0" encoding="UTF-8"?>'
           '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
           '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
           '<Default Extension="xml" ContentType="application/xml"/>'
           '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
           '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
           '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
           '</Types>')
        z.writestr('_rels/.rels','<?xml version="1.0" encoding="UTF-8"?>'
           '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
           '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
           '</Relationships>')
        z.writestr('xl/workbook.xml','<?xml version="1.0" encoding="UTF-8"?>'
           '<workbook xmlns="'+NS+'" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
           '<sheets><sheet name="Qarz tolovlari" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels','<?xml version="1.0" encoding="UTF-8"?>'
           '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
           '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
           '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
           '</Relationships>')
        z.writestr('xl/styles.xml','<?xml version="1.0" encoding="UTF-8"?>'
           '<styleSheet xmlns="'+NS+'"><numFmts count="1"><numFmt numFmtId="164" formatCode="dd.mm.yy"/></numFmts>'
           '<fonts count="1"><font><name val="Calibri"/><family val="2"/><color theme="1"/><sz val="11"/><scheme val="minor"/></font></fonts>'
           '<fills count="2"><fill><patternFill/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
           '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
           '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
           '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
           '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/></cellXfs>'
           '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')
        z.writestr('xl/worksheets/sheet1.xml',worksheet)
    return Response(b.getvalue(),mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition':'attachment; filename="buxgalter_xodim_qarz_tolovlari.xlsx"'})

@app.context_processor
def accountant_expense_context():
    def accountant_expense_token():
        return URLSafeSerializer(app.secret_key, salt='accountant-expense-operation-v1').dumps({
            'action': 'accountant_expense', 'accountant_id': session['user_id'],
            'nonce': secrets.token_urlsafe(32)})
    return {'accountant_expense_token': accountant_expense_token}


@app.route('/xarajatlar', methods=['GET','POST'])
@login_required
def expenses_page():
    if request.method == 'POST':
        try:
            amount=float(request.form['amount'])
            category=request.form.get('category','').strip()
            note=request.form.get('note','').strip()
            payment_method=payment_method_from_form(request.form)
            raw_expense_date=request.form.get('date','').strip()
            try:
                parsed_expense_date=(
                    iso_to_date(raw_expense_date)
                    if raw_expense_date else datetime.now().date()
                )
            except ValueError as exc:
                raise ValueError(f'Xarajat sanasini tekshiring: {exc}') from exc
            if parsed_expense_date > date(2099,12,31):
                raise ValueError('Xarajat sanasi 2099-12-31 dan keyin bo‘lishi mumkin emas.')
            expense_date=_html_date_storage(parsed_expense_date.isoformat())
            if not math.isfinite(amount) or amount<=0 or not category:
                raise ValueError('Xarajat summasi va turini tekshiring.')
            with db() as con:
                con.execute('BEGIN IMMEDIATE')
                token=request.form.get('operation_token','')
                try:
                    operation=URLSafeSerializer(app.secret_key, salt='accountant-expense-operation-v1').loads(token)
                except BadSignature:
                    raise ValueError('Xarajat tokeni yaroqsiz. Formani qayta oching.')
                if (not isinstance(operation,dict) or operation.get('action')!='accountant_expense'
                        or operation.get('accountant_id')!=session['user_id']):
                    raise ValueError('Xarajat tokeni bu foydalanuvchiga tegishli emas.')
                payload=json.dumps({'amount':amount.hex(),'category':category,
                    'date':request.form.get('date') or '', 'payment_method':payment_method,
                    'note':note},
                    ensure_ascii=False,sort_keys=True,separators=(',',':'))
                previous=con.execute('SELECT * FROM accountant_expense_operations WHERE token=?',(token,)).fetchone()
                if previous:
                    if previous['accountant_id']!=session['user_id'] or previous['payload']!=payload:
                        raise ValueError('Bu token boshqa xarajat mazmuni uchun ishlatilgan.')
                    flash('Xarajat saqlandi.', 'success')
                    return redirect(url_for('expenses_page'))
                expense=con.execute('''INSERT INTO expenses
                    (date,category,amount,note,user_id,payment_method,entered_by,fund_source)
                    VALUES(?,?,?,?,?,?,?,?)''',
                    (expense_date,category,amount,note,session['user_id'],payment_method,
                     session['user_id'],'buxgalter')).lastrowid
                con.execute('INSERT INTO accountant_expense_operations(token,accountant_id,payload,expense_id,expense_date) VALUES(?,?,?,?,?)',
                            (token,session['user_id'],payload,expense,expense_date))
            flash('Xarajat saqlandi.', 'success'); return redirect(url_for('expenses_page'))
        except (ValueError,KeyError,sqlite3.Error) as exc: flash(str(exc) or 'Xarajat summasini tekshiring.', 'error')
    with db() as con:
        raw_rows,pagination=paginate_rows(con,"""SELECT e.*,
            entered.username AS entered_by_name, source.role AS source_role
            FROM expenses e
            LEFT JOIN users entered ON entered.id=COALESCE(e.entered_by,e.user_id)
            LEFT JOIN users source ON source.id=e.user_id
            ORDER BY e.id DESC""",
            'SELECT COUNT(*) FROM expenses')
        rows=[expense_display(row) for row in raw_rows]
    return render_template('expenses.html', rows=rows,pagination=pagination)

@app.route('/hisobot')
@login_required
def reports():
    with db() as con:
        sales=con.execute('SELECT product, SUM(qty_kg) qty, SUM(qty_kg*price_per_kg) amount FROM sales WHERE id NOT IN (SELECT sale_id FROM sale_cancellations) GROUP BY product').fetchall()
        expenses=con.execute('SELECT category,SUM(amount) amount FROM expenses GROUP BY category').fetchall()
        cleaning,cleaning_pagination=paginate_rows(con,
            '''SELECT date, COALESCE(raw_wheat_kg,wheat_kg) raw_kg,
                wheat_kg cleaned_kg, COALESCE(raw_wheat_kg,wheat_kg)-wheat_kg loss_kg,
                CASE WHEN COALESCE(raw_wheat_kg,wheat_kg)>0 THEN
                  (COALESCE(raw_wheat_kg,wheat_kg)-wheat_kg)*100.0/COALESCE(raw_wheat_kg,wheat_kg)
                ELSE 0 END loss_pct
                FROM production ORDER BY business_date_key(date) DESC,id DESC''',
            'SELECT COUNT(*) FROM production',
        )
        totals=con.execute('''SELECT COALESCE(SUM(COALESCE(raw_wheat_kg,wheat_kg)),0),
            COALESCE(SUM(wheat_kg),0) FROM production''').fetchone()
    raw,cleaned=totals[0],totals[1]
    return render_template('reports.html', sales=sales, expenses=expenses,
        cleaning=cleaning, raw_total=raw, cleaned_total=cleaned,
        loss_total=raw-cleaned, loss_pct=((raw-cleaned)*100/raw if raw else 0),
        cleaning_pagination=cleaning_pagination)

@app.route('/omborlar', methods=['GET','POST'])
@login_required
def warehouses():
    user = current_user()
    if request.method == 'POST':
        if user['role'] != 'buxgalter':
            abort(403)
        if request.form.get('action') != 'opening_product_stock':
            abort(400)
        try:
            stock_date_value = request.form.get('date', '').strip()
            stock_day = _business_date_key(stock_date_value)
            if stock_day > '2099-12-31':
                raise ValueError('Boshlang‘ich qoldiq sanasi 2099-12-31 dan oshmasin.')
            product = request.form.get('product', '').strip()
            if product not in PRODUCTS:
                raise ValueError('Mahsulot turi noto‘g‘ri.')
            note = request.form.get('note', '').strip()
            if len(note) > 500:
                raise ValueError('Izoh 500 belgidan oshmasin.')
            con = db()
            try:
                con.execute('BEGIN IMMEDIATE')
                if product in BAGGED_FLOUR_PRODUCTS:
                    def opening_bag_count(field):
                        try:
                            value = float(request.form.get(field, '0').strip() or '0')
                        except (TypeError, ValueError, OverflowError) as exc:
                            raise ValueError('Qop soni musbat butun son bo‘lishi kerak.') from exc
                        if (not math.isfinite(value) or value < 0
                                or value > 9_223_372_036_854_775_807):
                            raise ValueError('Qop soni manfiy bo‘lmagan butun son bo‘lishi kerak.')
                        if value != int(value):
                            raise ValueError('Qop soni manfiy bo‘lmagan butun son bo‘lishi kerak.')
                        return int(value)

                    bag_25_count = opening_bag_count('bag_25_count')
                    bag_50_count = opening_bag_count('bag_50_count')
                    amount = bag_25_count * 25 + bag_50_count * 50
                    if amount <= 0:
                        raise ValueError('Kamida bitta 25 yoki 50 kg qop kiriting.')
                    if con.execute(
                            'SELECT 1 FROM opening_bagged_product_stocks WHERE product=?',
                            (product,)).fetchone():
                        raise ValueError(
                            f'{product} uchun boshlang‘ich qop qoldig‘i avval kiritilgan.')
                    existing_kg_opening = con.execute(
                        'SELECT stock_date FROM opening_product_stocks WHERE product=?',
                        (product,)).fetchone()
                    if (existing_kg_opening and
                            stock_day < _business_date_key(existing_kg_opening['stock_date'])):
                        raise ValueError(
                            'Qop qoldig‘i sanasi avvalgi kilogramm qoldig‘i sanasidan oldin bo‘lmasin.')
                else:
                    try:
                        amount = float(request.form.get('qty_kg', ''))
                    except (TypeError, ValueError, OverflowError) as exc:
                        raise ValueError('Miqdor 0.01 kg qadamida musbat son bo‘lishi kerak.') from exc
                    if (not math.isfinite(amount) or amount < 0.01
                            or abs(amount * 100 - round(amount * 100)) > 0.001):
                        raise ValueError('Miqdor 0.01 kg qadamida musbat son bo‘lishi kerak.')
                    if con.execute(
                            'SELECT 1 FROM opening_product_stocks WHERE product=?',
                            (product,)).fetchone():
                        raise ValueError(
                            f'{product} uchun boshlang‘ich qoldiq avval kiritilgan.')
                if product_stock_activity_exists(con, product, stock_day):
                    raise ValueError(
                        f'{product} bo‘yicha tanlangan sana yoki undan keyin tarixiy ishlab '
                        'chiqarish/sotuv mavjud. Ikki marta hisoblamaslik uchun undan keyingi '
                        'sana tanlang.')
                if product in BAGGED_FLOUR_PRODUCTS:
                    con.execute("""INSERT INTO opening_bagged_product_stocks
                        (product,stock_date,bag_25_count,bag_50_count,note,entered_by)
                        VALUES(?,?,?,?,?,?)""",
                        (product, _business_date_storage(stock_date_value),
                         bag_25_count, bag_50_count, note, user['id']))
                else:
                    con.execute("""INSERT INTO opening_product_stocks
                        (product,stock_date,qty_kg,note,entered_by)
                        VALUES(?,?,?,?,?)""",
                        (product, _business_date_storage(stock_date_value),
                         amount, note, user['id']))
                con.commit()
            finally:
                con.close()
            flash(f'{product} boshlang‘ich qoldig‘i omborga qo‘shildi.', 'success')
        except sqlite3.IntegrityError:
            flash('Bu mahsulot uchun boshlang‘ich qoldiq allaqachon kiritilgan.', 'error')
        except (ValueError, KeyError, OverflowError, sqlite3.Error) as exc:
            flash(str(exc) or 'Sana, mahsulot va miqdorni tekshiring.', 'error')
        return redirect(url_for('warehouses'))
    con = db()
    try:
        stocks=product_stocks(con)
        bagged_stocks=bagged_product_stocks(con)
        purchased=con.execute('SELECT COALESCE(SUM(net),0) FROM wheat_receipts').fetchone()[0]
        consumed=con.execute('SELECT COALESCE(SUM(COALESCE(raw_wheat_kg,wheat_kg)),0) FROM production').fetchone()[0]
        kg_openings=con.execute("""SELECT s.*,u.username AS entered_by_name
            FROM opening_product_stocks s JOIN users u ON u.id=s.entered_by
            ORDER BY business_date_key(s.stock_date),s.product""").fetchall()
        bag_openings=con.execute("""SELECT s.*,u.username AS entered_by_name
            FROM opening_bagged_product_stocks s JOIN users u ON u.id=s.entered_by
            ORDER BY business_date_key(s.stock_date),s.product""").fetchall()
    finally:
        con.close()
    existing_kg_products = {row['product'] for row in kg_openings}
    existing_bag_products = {row['product'] for row in bag_openings}
    opening_products = [
        product for product in PRODUCTS
        if (product in BAGGED_FLOUR_PRODUCTS and product not in existing_bag_products)
        or (product not in BAGGED_FLOUR_PRODUCTS and product not in existing_kg_products)
    ]
    stock_openings = [
        dict(row, opening_bags=False, bag_25_count=0, bag_50_count=0)
        for row in kg_openings
    ]
    stock_openings.extend(
        dict(row, qty_kg=row['bag_25_count'] * 25 + row['bag_50_count'] * 50,
             opening_bags=True)
        for row in bag_openings
    )
    stock_openings.sort(
        key=lambda row: (_business_date_key(row['stock_date']), row['product'],
                         row['opening_bags'])
    )
    return render_template('warehouses.html', stocks=stocks,
                           bagged_stocks=bagged_stocks, wheat_stock=purchased-consumed,
                           opening_products=opening_products,
                           opening_bagged_products=BAGGED_FLOUR_PRODUCTS,
                           stock_openings=stock_openings,
                           today=date.today().isoformat())

@app.route('/hisobot/yagona-excel')
@login_required
def unified_excel_report():
    from io import BytesIO
    from datetime import datetime
    from flask import send_file
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    from date_sql import mixed_date_to_iso

    start = request.args.get('start', '').strip()
    end = request.args.get('end', '').strip()
    try:
        start = mixed_date_to_iso(start) if start else ''
        end = mixed_date_to_iso(end) if end else ''
        if start and end and start > end:
            raise ValueError()
    except ValueError:
        abort(400, 'Sana oralig‘i noto‘g‘ri; YYYY-MM-DD yoki DD-MM-YYYY formatidan foydalaning.')

    def date_key(value):
        return mixed_date_to_iso(value)

    cutoff = end or date.today().isoformat()

    def in_period(value):
        key = date_key(value)
        return (not start or key >= start) and key <= cutoff

    def date_value(value):
        return datetime.strptime(date_key(value), '%Y-%m-%d') if value else None

    def period_text():
        if not start and not end:
            return 'Barcha sanalar'
        return f'{date_value(start).strftime("%d.%m.%Y") if start else "Boshlanishi"} — {date_value(end).strftime("%d.%m.%Y") if end else "Bugungi holat"}'

    zero = Decimal('0')
    balance_epsilon = Decimal('0.000001')

    def amount(value):
        return Decimal(str(value or 0))

    def report_balance(value):
        return zero if abs(value) <= balance_epsilon else value

    def payment_method(value):
        return {'naqd': 'Naqd', 'plastik': 'Plastik', 'hisob': 'Bank'}.get(
            value, value or 'Aniqlanmagan')

    def status_for(balance, paid):
        balance = report_balance(balance)
        if balance <= zero:
            return 'To‘langan'
        return 'Qisman to‘langan' if paid > zero else 'Qarzdor'

    try:
        con = db()
        try:
            con.execute('BEGIN')
            customer_context = customer_identity_context(con)
            users = {
                row['id']: ROLES.get(row['role'], row['username'])
                for row in con.execute('SELECT id,username,role FROM users')
            }
            all_sales = [dict(row) for row in con.execute("""
                SELECT s.*, i.number AS invoice_number
                FROM sales s LEFT JOIN invoices i ON i.sale_id=s.id
                ORDER BY business_date_key(s.date),s.id
            """)]
            cancellation_rows = [dict(row) for row in con.execute(
                'SELECT sale_id,cancelled_at FROM sale_cancellations')]
            cancelled_at_cutoff = {
                row['sale_id'] for row in cancellation_rows
                if not row['cancelled_at']
                or date_key(str(row['cancelled_at'])[:10]) <= cutoff
            }
            active_period_sales = [
                row for row in all_sales
                if row['id'] not in cancelled_at_cutoff and in_period(row['date'])
            ]
            period_sales = [row for row in all_sales if in_period(row['date'])]
            receipts = [dict(row) for row in con.execute(
                'SELECT * FROM worker_receipts ORDER BY id')]
            legacy_payments = [dict(row) for row in con.execute(
                'SELECT * FROM payments ORDER BY id')]
            direct_receipts = {
                row['receipt_id']: dict(row)
                for row in con.execute('SELECT * FROM accountant_worker_receipts')
            }
            batch_by_sale = {
                row['sale_id']: row['batch_id']
                for row in con.execute('SELECT sale_id,batch_id FROM worker_sale_batch_items')
            }
            batch_channels = {}
            for row in con.execute(
                    'SELECT batch_id,payment_method,amount FROM worker_sale_batch_payments ORDER BY id'):
                batch_channels.setdefault(row['batch_id'], []).append(
                    f"{payment_method(row['payment_method'])}: {amount(row['amount']):,.0f}")

            def financial_as_of(connection, cutoff_date):
                snapshot = sqlite3.connect(':memory:')
                snapshot.row_factory = sqlite3.Row
                try:
                    connection.backup(snapshot)
                    as_of_tables = (
                        ('sales', 'date'),
                        ('opening_debts', 'date'),
                        ('opening_debt_receipts', 'date'),
                        ('worker_receipts', 'date'),
                        ('accountant_worker_receipts', 'payment_date'),
                        ('payments', 'date'),
                    )
                    for table, date_column in as_of_tables:
                        expired = [
                            row['id'] for row in snapshot.execute(
                                f'SELECT id,{date_column} FROM {table}')
                            if date_key(row[date_column]) > cutoff_date
                        ]
                        snapshot.executemany(
                            f'DELETE FROM {table} WHERE id=?',
                            ((row_id,) for row_id in expired))
                    expired_cancellations = []
                    for row in snapshot.execute(
                            'SELECT sale_id,cancelled_at FROM sale_cancellations'):
                        cancellation_date = str(row['cancelled_at'] or '')[:10]
                        if cancellation_date and date_key(cancellation_date) > cutoff_date:
                            expired_cancellations.append((row['sale_id'],))
                    snapshot.executemany(
                        'DELETE FROM sale_cancellations WHERE sale_id=?',
                        expired_cancellations)
                    return calculate_financial_snapshot(snapshot)
                finally:
                    snapshot.close()

            historical = financial_as_of(con, cutoff)
            sales_debt_balance = sum(
                (report_balance(obligation['balance'])
                 for group in historical['customers'].values()
                 for obligation in group['obligations']
                 if obligation['kind'] == 'sale'),
                zero,
            )
            opening_debt_balance = sum(
                (report_balance(obligation['balance'])
                 for group in historical['customers'].values()
                 for obligation in group['obligations']
                 if obligation['kind'] == 'opening'),
                zero,
            )
            prior_period_debt = sum(
                (report_balance(obligation['balance'])
                 for group in historical['customers'].values()
                 for obligation in group['obligations']
                 if start and date_key(obligation['date']) < start),
                zero,
            )
            allocation = {
                item['id']: item
                for group in historical['customers'].values()
                for item in group['obligations']
                if item['kind'] == 'sale'
            }
            unallocated_legacy_ids = {
                row['id'] for row in historical['unallocated_legacy']
            }
            def display_customer(name, phone):
                return customer_display(customer_context, name or '', phone or '')

            def invoice_label(row):
                sale_id = f"S-{row['id']}"
                return (
                    f"{sale_id} / № {row['invoice_number']}"
                    if row.get('invoice_number') else sale_id
                )

            # Payment rows intentionally use source events; accountant records classify
            # linked receipts and are never added as an additional receipt.
            events = []
            sales_by_id = {row['id']: row for row in all_sales}
            for sale in all_sales:
                paid = amount(sale['paid'])
                if paid <= zero or not in_period(sale['date']):
                    continue
                recipient_id = sale.get('initial_payment_recipient_id') or sale.get('user_id')
                note = sale.get('note') or ''
                batch_id = batch_by_sale.get(sale['id'])
                method = payment_method(sale.get('payment_method'))
                if batch_id is not None:
                    method = 'Aniqlanmagan'
                    batch_note = (
                        f" | Paket {batch_id}: avans sotuvlar orasida taqsimlangan; "
                        f"kanallar qayta qo‘shilmaydi ({'; '.join(batch_channels.get(batch_id, []))})."
                    )
                    note = (note + batch_note).strip()
                if sale['id'] in cancelled_at_cutoff:
                    note = (note + ' | Bekor qilingan sotuvdagi to‘lov; qaytarilgan deb hisoblanmadi.').strip()
                events.append({
                    'date': sale['date'], 'id': f"S-{sale['id']}-AVANS",
                    'customer': display_customer(sale['customer'], sale['customer_phone'])[0],
                    'sale_id': sale['id'], 'amount': paid,
                    'method': method,
                    'recipient': users.get(recipient_id, 'Aniqlanmagan'),
                    'owner': users.get(sale.get('user_id'), 'Aniqlanmagan'),
                    'note': note, 'kind': 'initial',
                })
            for row in legacy_payments:
                if not in_period(row['date']):
                    continue
                card = resolve_customer_name(customer_context, row['customer'])
                customer_name = card['name'] if card else row['customer']
                note = row.get('note') or ''
                if row['id'] in unallocated_legacy_ids:
                    note = (
                        note + ' | Eski to‘lov ishonchli majburiyat/xodimga '
                        'bog‘lanmagani uchun qarzdan chegirilmadi.'
                    ).strip()
                events.append({
                    'date': row['date'], 'id': f"P-{row['id']}",
                    'customer': customer_name, 'sale_id': '',
                    'amount': amount(row['amount']),
                    'method': 'Aniqlanmagan', 'recipient': 'Aniqlanmagan',
                    'owner': 'Aniqlanmagan', 'note': note,
                    'kind': 'legacy',
                })
            for receipt in receipts:
                if not in_period(receipt['date']):
                    continue
                sale = sales_by_id.get(receipt['sale_id'])
                direct = direct_receipts.get(receipt['id'])
                recipient_id = direct['accountant_id'] if direct else receipt['worker_id']
                note = direct.get('note', '') if direct else ''
                if not sale or receipt['sale_id'] in cancelled_at_cutoff:
                    note = (note + ' | Faol sotuvga bog‘lanmagan to‘lov.').strip()
                if direct and (
                        direct['amount'] != receipt['amount']
                        or direct['worker_id'] != receipt['worker_id']
                        or direct['payment_method'] != receipt.get('payment_method')):
                    note = (note + ' | Buxgalter belgilashi asosiy to‘lovga mos emas.').strip()
                name, phone = display_customer(
                    sale.get('customer', '') if sale else '',
                    sale.get('customer_phone', '') if sale else '')
                events.append({
                    'date': receipt['date'], 'id': f"T-{receipt['id']}",
                    'customer': name, 'sale_id': receipt['sale_id'],
                    'amount': amount(receipt['amount']),
                    'method': payment_method(receipt.get('payment_method')),
                    'recipient': users.get(recipient_id, 'Aniqlanmagan'),
                    'owner': users.get(sale.get('user_id'), 'Aniqlanmagan') if sale else 'Aniqlanmagan',
                    'note': note, 'kind': 'linked',
                })
            events.sort(key=lambda row: (date_key(row['date']), row['kind'], row['id']))
            period_paid = sum((row['amount'] for row in events), zero)
            period_sales_total = sum(
                (amount(row['qty_kg']) * amount(row['price_per_kg'])
                 for row in active_period_sales), zero)

            debt_rows = []
            debt_by_customer_id = {}
            for group in historical['customers'].values():
                if group['debt'] <= zero:
                    continue
                customer_id = group.get('customer_id')
                card = customer_context['cards'].get(customer_id)
                obligation_ids = []
                workers = set()
                for obligation in group['obligations']:
                    if obligation['balance'] <= zero:
                        continue
                    if obligation['kind'] == 'sale':
                        obligation_ids.append(f"S-{obligation['id']}")
                        sale = sales_by_id.get(obligation['id'])
                        if sale and sale.get('user_id') is not None:
                            workers.add(users.get(sale['user_id'], 'Aniqlanmagan'))
                    else:
                        obligation_ids.append(f"Q-{obligation['id']}")
                if len(workers) == 1:
                    owner = next(iter(workers))
                elif workers:
                    owner = 'Aralash: ' + ', '.join(sorted(workers))
                else:
                    owner = 'Aniqlanmagan'
                last_dates = []
                sale_obligation_ids = {
                    item['id'] for item in group['obligations']
                    if item['kind'] == 'sale'
                }
                for sale_id in sale_obligation_ids:
                    sale = sales_by_id.get(sale_id)
                    if sale and amount(sale['paid']) > zero:
                        last_dates.append(sale['date'])
                for receipt in historical['receipts']:
                    if receipt['sale_id'] in sale_obligation_ids:
                        last_dates.append(receipt['date'])
                legacy_ids = set(group.get('legacy_payment_ids', []))
                last_dates.extend(
                    row['date'] for row in legacy_payments
                    if row['id'] in legacy_ids)
                last_payment = max(last_dates, key=date_key) if last_dates else ''
                customer_name = card['name'] if card else group['customer']
                customer_phone = card['phone'] if card else group['phone']
                debt_rows.append({
                    'customer_id': customer_id,
                    'name': customer_name, 'phone': customer_phone,
                    'owner': owner, 'total': group['sales_revenue'] + group['opening_debt'],
                    'paid': group['received'], 'debt': group['debt'],
                    'prior_debt': sum(
                        (report_balance(item['balance']) for item in group['obligations']
                         if (start and date_key(item['date']) < start)
                         or (not start and item['kind'] == 'opening')),
                        zero,
                    ),
                    'last_payment': last_payment,
                    'status': status_for(group['debt'], group['received']),
                    'ids': ', '.join(obligation_ids),
                })
                if customer_id is not None:
                    prior = debt_by_customer_id.get(customer_id, zero)
                    debt_by_customer_id[customer_id] = prior + group['debt']
            debt_rows.sort(key=lambda row: (row['name'].casefold(), row['phone']))

            cards = [dict(row) for row in con.execute(
                'SELECT id,name,phone FROM customers ORDER BY name,id')]
            period_sales_by_customer = {}
            period_paid_by_customer = {}
            last_sale_by_customer = {}
            for sale in all_sales:
                if sale['id'] in cancelled_at_cutoff:
                    continue
                key = date_key(sale['date'])
                card = resolve_customer_identity(
                    customer_context, sale['customer'], sale['customer_phone'])
                if not card:
                    continue
                if key <= cutoff:
                    previous = last_sale_by_customer.get(card['id'])
                    if previous is None or key > previous[0]:
                        last_sale_by_customer[card['id']] = (key, sale['date'])
                if in_period(sale['date']):
                    period_sales_by_customer[card['id']] = (
                        period_sales_by_customer.get(card['id'], zero)
                        + amount(sale['qty_kg']) * amount(sale['price_per_kg']))
            for event in events:
                customer_id = None
                if event['kind'] == 'initial':
                    sale = sales_by_id.get(event['sale_id'])
                    if sale:
                        card = resolve_customer_identity(
                            customer_context, sale['customer'], sale['customer_phone'])
                        customer_id = card['id'] if card else None
                elif event['kind'] == 'linked':
                    sale = sales_by_id.get(event['sale_id'])
                    if sale:
                        card = resolve_customer_identity(
                            customer_context, sale['customer'], sale['customer_phone'])
                        customer_id = card['id'] if card else None
                elif event['kind'] == 'legacy':
                    card = resolve_customer_name(customer_context, event['customer'])
                    customer_id = card['id'] if card else None
                if customer_id is not None:
                    period_paid_by_customer[customer_id] = (
                        period_paid_by_customer.get(customer_id, zero) + event['amount'])

            customer_rows = []
            for card in cards:
                customer_id = card['id']
                sales_total = period_sales_by_customer.get(customer_id, zero)
                paid_total = period_paid_by_customer.get(customer_id, zero)
                debt = debt_by_customer_id.get(customer_id, zero)
                if not (sales_total or paid_total or debt):
                    continue
                historical_group = next(
                    (group for group in historical['customers'].values()
                     if group.get('customer_id') == customer_id), None)
                cumulative_paid = historical_group['received'] if historical_group else paid_total
                customer_rows.append({
                    'id': customer_id, 'name': card['name'], 'phone': card['phone'],
                    'sales': sales_total, 'paid': paid_total, 'debt': debt,
                    'last_sale': last_sale_by_customer.get(customer_id, (None, ''))[1],
                    'status': status_for(debt, cumulative_paid),
                })
            customer_rows.sort(key=lambda row: (row['name'].casefold(), row['id']))

            wb = Workbook()
            summary = wb.active
            summary.title = 'JAMLANMA'
            sales_ws = wb.create_sheet('SOTUVLAR')
            debt_ws = wb.create_sheet('QARZLAR')
            payments_ws = wb.create_sheet('TO‘LOVLAR')
            customers_ws = wb.create_sheet('MIJOZLAR')
            wb.properties.title = 'TO‘RTKO‘LDONMAHSULOTI yagona moliyaviy hisoboti'
            wb.properties.subject = f'Yagona hisobot: {period_text()}'

            dark_green = '075B3B'
            mid_green = '128451'
            pale_green = 'EAF5EE'
            white = 'FFFFFF'
            light_border = Side(style='thin', color='D8E5DC')
            money_format = '#,##0.## "so‘m"'
            number_format = '#,##0.##'
            date_format = 'dd.mm.yyyy'

            def paint_table(ws, title, headers, rows, widths, status_index=None,
                            total_values=None, header_row=4, title_row=1,
                            subtitle_row=2, period_note=None, money_columns=()):
                last_column = len(headers)
                ws.merge_cells(start_row=title_row, start_column=1,
                               end_row=title_row, end_column=last_column)
                title_cell = ws.cell(title_row, 1, title)
                title_cell.fill = PatternFill('solid', fgColor=dark_green)
                title_cell.font = Font(name='Aptos Display', size=16, bold=True, color=white)
                title_cell.alignment = Alignment(vertical='center')
                ws.row_dimensions[title_row].height = 30
                ws.merge_cells(start_row=subtitle_row, start_column=1,
                               end_row=subtitle_row, end_column=last_column)
                subtitle = period_note or (
                    f"Hisobot davri: {period_text()}. Sotuv/to‘lov aylanmasi davr bo‘yicha; "
                    "qarz qoldig‘i tanlangan davr oxirida, oldingi qarzlar bilan."
                )
                subtitle_cell = ws.cell(subtitle_row, 1, subtitle)
                subtitle_cell.font = Font(size=10, italic=True, color='45564B')
                subtitle_cell.alignment = Alignment(wrap_text=True, vertical='center')
                ws.row_dimensions[subtitle_row].height = 30
                for column, heading in enumerate(headers, 1):
                    cell = ws.cell(header_row, column, heading)
                    cell.fill = PatternFill('solid', fgColor=dark_green)
                    cell.font = Font(bold=True, color=white)
                    cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
                    cell.border = Border(bottom=Side(style='medium', color=mid_green))
                ws.row_dimensions[header_row].height = 32
                for row_index, values in enumerate(rows, header_row + 1):
                    for column, value in enumerate(values, 1):
                        cell = ws.cell(row_index, column, value)
                        cell.fill = PatternFill(
                            'solid', fgColor=white if (row_index - header_row) % 2 else pale_green)
                        cell.alignment = Alignment(vertical='top', wrap_text=True)
                        cell.border = Border(bottom=light_border)
                        if isinstance(value, str):
                            cell.data_type = 's'
                        elif isinstance(value, datetime):
                            cell.number_format = date_format
                        elif isinstance(value, Decimal):
                            cell.number_format = (
                                money_format if column in money_columns else number_format)
                            cell.alignment = Alignment(horizontal='right', vertical='top')
                        elif isinstance(value, (int, float)) and not isinstance(value, bool):
                            cell.number_format = number_format
                            cell.alignment = Alignment(horizontal='right', vertical='top')
                    if status_index is not None:
                        status_cell = ws.cell(row_index, status_index)
                        color = {
                            'To‘langan': 'D9EFD9',
                            'Qisman to‘langan': 'FFF0C2',
                            'Qarzdor': 'FCE0E0',
                            'Bekor qilingan': 'E8E8E8',
                        }.get(status_cell.value)
                        if color:
                            status_cell.fill = PatternFill('solid', fgColor=color)
                            status_cell.font = Font(bold=True)
                last_data_row = header_row + len(rows)
                if total_values is not None:
                    total_row = last_data_row + 1
                    for column, value in enumerate(total_values, 1):
                        cell = ws.cell(total_row, column, value)
                        cell.fill = PatternFill('solid', fgColor='DCEFE2')
                        cell.font = Font(bold=True, color='163D2B')
                        cell.border = Border(top=Side(style='medium', color=mid_green))
                        if isinstance(value, Decimal):
                            cell.number_format = (
                                money_format if column in money_columns else number_format)
                            cell.alignment = Alignment(horizontal='right')
                    last_data_row = total_row
                ws.auto_filter.ref = (
                    f'A{header_row}:{get_column_letter(last_column)}'
                    f'{max(header_row, header_row + len(rows))}'
                )
                ws.freeze_panes = f'A{header_row + 1}'
                ws.sheet_view.showGridLines = False
                ws.page_setup.orientation = 'landscape'
                ws.page_setup.paperSize = ws.PAPERSIZE_A4
                ws.page_setup.fitToWidth = 1
                ws.page_setup.fitToHeight = 0
                ws.sheet_properties.pageSetUpPr.fitToPage = True
                ws.print_title_rows = f'{header_row}:{header_row}'
                ws.print_area = f'A1:{get_column_letter(last_column)}{last_data_row}'
                for column, width in enumerate(widths, 1):
                    ws.column_dimensions[get_column_letter(column)].width = width
                return last_data_row

            summary_headers = [
                'Sana', 'Hujjat №', 'Xaridor', 'Telefon', 'Mahsulot',
                'Sotuv summasi', 'To‘langan summa', 'Qolgan qarz', 'Holat',
            ]
            summary_rows = []
            for sale in active_period_sales:
                item = allocation.get(sale['id'])
                if not item:
                    continue
                name, phone = display_customer(sale['customer'], sale['customer_phone'])
                paid_to_date = item['initial_paid'] + item['linked_paid'] + item['legacy_applied']
                summary_rows.append([
                    date_value(sale['date']), invoice_label(sale), name, phone,
                    sale['product'], item['amount'], paid_to_date,
                    report_balance(item['balance']),
                    status_for(item['balance'], paid_to_date),
                ])

            summary.merge_cells('C1:I1')
            summary.merge_cells('C2:I2')
            summary.merge_cells('C3:I3')
            summary['C1'] = 'TO‘RTKO‘LDONMAHSULOTI MCHJ'
            summary['C1'].font = Font(name='Aptos Display', size=20, bold=True, color=dark_green)
            summary['C2'] = 'Yagona hisobot (Sotuvlar • Qarzlar • To‘lovlar)'
            summary['C2'].font = Font(size=13, bold=True, color='233C30')
            summary['C3'] = f'Hisobot davri: {period_text()}'
            summary['C3'].font = Font(size=10, italic=True, color='526458')
            for cell in ('C1', 'C2', 'C3'):
                summary[cell].alignment = Alignment(vertical='center')
            summary.row_dimensions[1].height = 34
            summary.row_dimensions[2].height = 26
            summary.row_dimensions[3].height = 22
            logo_path = Path(BASE_DIR) / 'static' / 'company_logo.png'
            if not logo_path.is_file():
                abort(500, 'Korxona logotipi topilmadi; yagona hisobot yaratilmadi.')
            cards = [
                ('📈 JAMI SOTUV', period_sales_total, 'DDF1E3', '17643C'),
                ('💳 JAMI TO‘LOV', period_paid, 'E8F5EA', '2A7B4B'),
                ('⚠ JAMI QARZ (DAVR OXIRI)', historical['totals']['debt'], 'FCE6E6', 'B62828'),
                ('👥 QARZDORLAR SONI', Decimal(len(debt_rows)), 'E4F1FA', '17608A'),
            ]
            card_ranges = [('A4:B4', 'A5:B5'), ('C4:D4', 'C5:D5'),
                           ('E4:F4', 'E5:F5'), ('G4:I4', 'G5:I5')]
            for (label, value, fill, color), (label_range, value_range) in zip(cards, card_ranges):
                summary.merge_cells(label_range)
                summary.merge_cells(value_range)
                label_cell = summary[label_range.split(':')[0]]
                value_cell = summary[value_range.split(':')[0]]
                label_cell.value = label
                value_cell.value = value
                for ref in (label_range, value_range):
                    for row in summary[ref]:
                        for cell in row:
                            cell.fill = PatternFill('solid', fgColor=fill)
                            cell.border = Border(
                                left=light_border, right=light_border,
                                top=light_border, bottom=light_border)
                label_cell.font = Font(size=11, bold=True, color=color)
                value_cell.font = Font(size=18, bold=True, color=color)
                value_cell.number_format = '0 "ta"' if 'QARZDORLAR SONI' in label else money_format
                label_cell.alignment = Alignment(horizontal='center', vertical='center')
                value_cell.alignment = Alignment(horizontal='center', vertical='center')
            summary.row_dimensions[4].height = 30
            summary.row_dimensions[5].height = 44
            summary.merge_cells('A6:I6')
            debt_note = summary['A6']
            debt_note.value = (
                f"Qarz tarkibi — sotuvlar qoldig‘i: {sales_debt_balance:,.0f} so‘m; "
                f"boshlang‘ich qarzlar qoldig‘i: {opening_debt_balance:,.0f} so‘m; "
                f"umumiy qarz: {historical['totals']['debt']:,.0f} so‘m."
            )
            if start:
                debt_note.value += (
                    f" Davr boshlanishigacha bo‘lgan qoldiq: "
                    f"{prior_period_debt:,.0f} so‘m."
                )
            debt_note.font = Font(size=10, italic=True, color='45564B')
            debt_note.fill = PatternFill('solid', fgColor='F1F6F2')
            debt_note.alignment = Alignment(vertical='center', wrap_text=True)
            summary.row_dimensions[6].height = 30
            summary_headers_row = 7
            for column, heading in enumerate(summary_headers, 1):
                cell = summary.cell(summary_headers_row, column, heading)
                cell.fill = PatternFill('solid', fgColor=dark_green)
                cell.font = Font(bold=True, color=white)
                cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
                cell.border = Border(bottom=Side(style='medium', color=mid_green))
            summary.row_dimensions[summary_headers_row].height = 32
            for row_index, values in enumerate(summary_rows, summary_headers_row + 1):
                for column, value in enumerate(values, 1):
                    cell = summary.cell(row_index, column, value)
                    cell.fill = PatternFill(
                        'solid', fgColor=white if (row_index - summary_headers_row) % 2 else pale_green)
                    cell.alignment = Alignment(vertical='top', wrap_text=True)
                    cell.border = Border(bottom=light_border)
                    if isinstance(value, str):
                        cell.data_type = 's'
                    elif isinstance(value, datetime):
                        cell.number_format = date_format
                    elif isinstance(value, Decimal):
                        cell.number_format = (
                            money_format if column in (6, 7, 8) else number_format)
                        cell.alignment = Alignment(horizontal='right')
                status_cell = summary.cell(row_index, 9)
                status_cell.fill = PatternFill('solid', fgColor={
                    'To‘langan': 'D9EFD9', 'Qisman to‘langan': 'FFF0C2',
                    'Qarzdor': 'FCE0E0',
                }[status_cell.value])
                status_cell.font = Font(bold=True)
            summary_total_row = summary_headers_row + len(summary_rows) + 1
            summary_totals = [
                'JAMI', '', '', '', '',
                sum((row[5] for row in summary_rows), zero),
                sum((row[6] for row in summary_rows), zero),
                sum((row[7] for row in summary_rows), zero), '',
            ]
            for column, value in enumerate(summary_totals, 1):
                cell = summary.cell(summary_total_row, column, value)
                cell.fill = PatternFill('solid', fgColor='DCEFE2')
                cell.font = Font(bold=True, color='163D2B')
                cell.border = Border(top=Side(style='medium', color=mid_green))
                if isinstance(value, Decimal):
                    cell.number_format = (
                        money_format if column in (6, 7, 8) else number_format)
                    cell.alignment = Alignment(horizontal='right')
            summary.auto_filter.ref = (
                f'A7:I{max(7, 7 + len(summary_rows))}')
            summary.freeze_panes = 'A8'
            summary.sheet_view.showGridLines = False
            summary.page_setup.orientation = 'landscape'
            summary.page_setup.paperSize = summary.PAPERSIZE_A4
            summary.page_setup.fitToWidth = 1
            summary.page_setup.fitToHeight = 0
            summary.sheet_properties.pageSetUpPr.fitToPage = True
            summary.print_title_rows = '7:7'
            summary.print_area = f'A1:I{summary_total_row}'
            for column, width in enumerate((16, 23, 32, 22, 28, 21, 21, 21, 22), 1):
                summary.column_dimensions[get_column_letter(column)].width = width

            sales_headers = [
                'Sana', 'Sotuv ID / hujjat №', 'Xaridor nomi', 'Telefon',
                'Mahsulot', 'Qop og‘irligi (kg)', 'Qop soni', 'Jami kg',
                'Birlik narxi', 'Sotuv summasi', 'Sotuv xodimi',
                'Sotuv vaqtidagi to‘lov', 'Keyingi to‘lovlar (davr oxirigacha)',
                'Eski to‘lovlardan FIFO', 'Qolgan qarz', 'Holat',
            ]
            sales_data = []
            active_sales_amount = active_sales_initial = active_sales_linked = zero
            active_sales_legacy = active_sales_debt = zero
            historical_receipts = {
                row['id']: row for row in historical['receipts']
            }
            for sale in period_sales:
                item = allocation.get(sale['id'])
                initial = amount(sale['paid'])
                if sale['id'] in cancelled_at_cutoff:
                    linked = sum(
                        (row['amount'] for row in historical_receipts.values()
                         if row['sale_id'] == sale['id']), zero)
                    legacy_applied = zero
                    balance = zero
                    label = 'Bekor qilingan'
                    sale_amount = amount(sale['qty_kg']) * amount(sale['price_per_kg'])
                else:
                    if not item:
                        continue
                    linked = item['linked_paid']
                    legacy_applied = item['legacy_applied']
                    balance = report_balance(item['balance'])
                    label = status_for(balance, initial + linked + legacy_applied)
                    sale_amount = item['amount']
                    active_sales_amount += sale_amount
                    active_sales_initial += initial
                    active_sales_linked += linked
                    active_sales_legacy += legacy_applied
                    active_sales_debt += balance
                name, phone = display_customer(sale['customer'], sale['customer_phone'])
                sales_data.append([
                    date_value(sale['date']), invoice_label(sale), name, phone,
                    sale['product'], sale.get('bag_kg'), sale.get('bag_count'),
                    amount(sale['qty_kg']), amount(sale.get('unit_price') or sale['price_per_kg']),
                    sale_amount, users.get(sale.get('user_id'), 'Aniqlanmagan'),
                    initial, linked, legacy_applied, balance, label,
                ])
            sales_total = [
                'JAMI', '', '', '', '', '', '', None, None,
                active_sales_amount, '', active_sales_initial, active_sales_linked,
                active_sales_legacy, active_sales_debt, '',
            ]
            paint_table(
                sales_ws, 'SOTUVLAR — sotuvlar va ularning to‘lov holati',
                sales_headers, sales_data,
                [13, 19, 25, 18, 23, 16, 12, 14, 17, 18, 18, 19, 23, 20, 18, 20],
                status_index=16, total_values=sales_total,
                money_columns=(9, 10, 12, 13, 14, 15),
            )

            debt_headers = [
                'Xaridor nomi', 'Telefon', 'Mas’ul xodim',
                'Sotuvlar / eski qarzlar summasi', 'To‘langan jami',
                'Qolgan qarz', 'Oxirgi to‘lov sanasi', 'Holat',
                'Bog‘langan sotuv/qarz IDlari',
                ('Davrdan oldingi qarz qoldig‘i' if start
                 else 'Boshlang‘ich qarz qoldig‘i'),
            ]
            debt_data = [[
                row['name'], row['phone'], row['owner'], row['total'],
                row['paid'], row['debt'], date_value(row['last_payment']),
                row['status'], row['ids'], row['prior_debt'],
            ] for row in debt_rows]
            debt_total = [
                'JAMI', '', '', sum((row['total'] for row in debt_rows), zero),
                sum((row['paid'] for row in debt_rows), zero),
                sum((row['debt'] for row in debt_rows), zero), '', '', '',
                sum((row['prior_debt'] for row in debt_rows), zero),
            ]
            paint_table(
                debt_ws, 'QARZLAR — davr oxiridagi ochiq majburiyatlar',
                debt_headers, debt_data,
                [25, 18, 22, 25, 20, 20, 19, 20, 32, 27],
                status_index=8, total_values=debt_total,
                money_columns=(4, 5, 6, 10),
            )

            payment_headers = [
                'To‘lov sanasi', 'To‘lov ID', 'Xaridor',
                'Bog‘langan sotuv ID', 'To‘lov summasi', 'To‘lov turi',
                'Pulni qabul qilgan shaxs', 'Sotuv uchun mas’ul xodim', 'Izoh',
            ]
            payment_data = [[
                date_value(row['date']), row['id'], row['customer'],
                f"S-{row['sale_id']}" if row['sale_id'] else '',
                row['amount'], row['method'], row['recipient'], row['owner'],
                row['note'],
            ] for row in events]
            payment_total = [
                'JAMI', '', '', '', period_paid, '', '', '',
                'Kassa ichidagi o‘tkazmalar va accountant_worker_receipts dublikatlari kiritilmagan.',
            ]
            paint_table(
                payments_ws, 'TO‘LOVLAR — davrda haqiqatan qabul qilingan mablag‘lar',
                payment_headers, payment_data,
                [16, 19, 26, 20, 20, 19, 24, 24, 55],
                total_values=payment_total,
                money_columns=(5,),
            )

            customer_headers = [
                'Xaridor ID', 'Hozirgi to‘g‘ri nomi', 'Telefon',
                'Jami xarid summasi (davr)', 'Jami to‘langan summa (davr)',
                'Qolgan qarz (davr oxiri)', 'Oxirgi sotuv sanasi', 'Holat',
            ]
            customer_data = [[
                row['id'], row['name'], row['phone'], row['sales'], row['paid'],
                row['debt'], date_value(row['last_sale']), row['status'],
            ] for row in customer_rows]
            customer_total = [
                'JAMI', '', '',
                sum((row['sales'] for row in customer_rows), zero),
                sum((row['paid'] for row in customer_rows), zero),
                sum((row['debt'] for row in customer_rows), zero), '', '',
            ]
            paint_table(
                customers_ws, 'MIJOZLAR — customers.id bo‘yicha jamlangan',
                customer_headers, customer_data,
                [14, 28, 19, 25, 26, 25, 19, 20],
                status_index=8, total_values=customer_total,
                money_columns=(4, 5, 6),
            )

            for ws in wb.worksheets:
                ws.sheet_properties.pageSetUpPr.fitToPage = True
                ws.oddFooter.center.text = 'TO‘RTKO‘LDONMAHSULOTI MCHJ — maxfiy ichki hisobot'
                ws.oddFooter.right.text = 'Sahifa &[Page] / &[Pages]'
            output = BytesIO()
            wb.save(output)
            import xml.etree.ElementTree as ET
            from zipfile import ZIP_DEFLATED, ZipFile

            spreadsheet_ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
            office_rel_ns = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
            package_rel_ns = 'http://schemas.openxmlformats.org/package/2006/relationships'
            content_ns = 'http://schemas.openxmlformats.org/package/2006/content-types'
            drawing_ns = 'http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing'
            drawingml_ns = 'http://schemas.openxmlformats.org/drawingml/2006/main'

            def serialize_xml(root, default_namespace, prefixes=()):
                ET.register_namespace('', default_namespace)
                for prefix, namespace in prefixes:
                    ET.register_namespace(prefix, namespace)
                return ET.tostring(root, encoding='utf-8', xml_declaration=True)

            replacements = {}
            additions = {}
            with ZipFile(BytesIO(output.getvalue()), 'r') as source:
                content_types = ET.fromstring(source.read('[Content_Types].xml'))
                if not any(
                        node.tag == f'{{{content_ns}}}Default'
                        and node.get('Extension') == 'png'
                        for node in content_types):
                    ET.SubElement(content_types, f'{{{content_ns}}}Default', {
                        'Extension': 'png', 'ContentType': 'image/png',
                    })
                drawing_name = 'xl/drawings/drawing1.xml'
                if not any(
                        node.tag == f'{{{content_ns}}}Override'
                        and node.get('PartName') == '/xl/drawings/drawing1.xml'
                        for node in content_types):
                    ET.SubElement(content_types, f'{{{content_ns}}}Override', {
                        'PartName': '/xl/drawings/drawing1.xml',
                        'ContentType': 'application/vnd.openxmlformats-officedocument.drawing+xml',
                    })
                sheet_name = 'xl/worksheets/sheet1.xml'
                sheet_root = ET.fromstring(source.read(sheet_name))
                relation_name = 'xl/worksheets/_rels/sheet1.xml.rels'
                if relation_name in source.namelist():
                    sheet_relations = ET.fromstring(source.read(relation_name))
                else:
                    sheet_relations = ET.Element(f'{{{package_rel_ns}}}Relationships')
                existing_ids = {node.get('Id') for node in sheet_relations}
                relation_number = 1
                while f'rId{relation_number}' in existing_ids:
                    relation_number += 1
                drawing_relation_id = f'rId{relation_number}'
                ET.SubElement(sheet_relations, f'{{{package_rel_ns}}}Relationship', {
                    'Id': drawing_relation_id,
                    'Type': f'{office_rel_ns}/drawing',
                    'Target': '../drawings/drawing1.xml',
                })
                drawing_element = ET.Element(
                    f'{{{spreadsheet_ns}}}drawing',
                    {f'{{{office_rel_ns}}}id': drawing_relation_id},
                )
                insert_before = {
                    'legacyDrawing', 'legacyDrawingHF', 'picture', 'oleObjects',
                    'controls', 'webPublishItems', 'tableParts', 'extLst',
                }
                insert_at = next(
                    (index for index, child in enumerate(list(sheet_root))
                     if child.tag.rsplit('}', 1)[-1] in insert_before),
                    len(sheet_root),
                )
                sheet_root.insert(insert_at, drawing_element)
                replacements[sheet_name] = serialize_xml(
                    sheet_root, spreadsheet_ns, (('r', office_rel_ns),))
                serialized_sheet_relations = serialize_xml(
                    sheet_relations, package_rel_ns)
                if relation_name in source.namelist():
                    replacements[relation_name] = serialized_sheet_relations
                else:
                    additions[relation_name] = serialized_sheet_relations

                drawing_root = ET.Element(f'{{{drawing_ns}}}wsDr')
                anchor = ET.SubElement(drawing_root, f'{{{drawing_ns}}}oneCellAnchor')
                origin = ET.SubElement(anchor, f'{{{drawing_ns}}}from')
                for name, value in (
                        ('col', '0'), ('colOff', '0'), ('row', '0'), ('rowOff', '0')):
                    ET.SubElement(origin, f'{{{drawing_ns}}}{name}').text = value
                ET.SubElement(anchor, f'{{{drawing_ns}}}ext', {
                    'cx': '914400', 'cy': '914400',
                })
                picture = ET.SubElement(anchor, f'{{{drawing_ns}}}pic')
                non_visual = ET.SubElement(picture, f'{{{drawing_ns}}}nvPicPr')
                ET.SubElement(non_visual, f'{{{drawing_ns}}}cNvPr', {
                    'id': '1', 'name': 'TO‘RTKO‘LDONMAHSULOTI logotipi',
                })
                ET.SubElement(
                    ET.SubElement(non_visual, f'{{{drawing_ns}}}cNvPicPr'),
                    f'{{{drawingml_ns}}}picLocks', {'noChangeAspect': '1'})
                fill = ET.SubElement(picture, f'{{{drawing_ns}}}blipFill')
                ET.SubElement(fill, f'{{{drawingml_ns}}}blip', {
                    f'{{{office_rel_ns}}}embed': 'rId1',
                })
                stretch = ET.SubElement(fill, f'{{{drawingml_ns}}}stretch')
                ET.SubElement(stretch, f'{{{drawingml_ns}}}fillRect')
                shape = ET.SubElement(picture, f'{{{drawing_ns}}}spPr')
                geometry = ET.SubElement(shape, f'{{{drawingml_ns}}}prstGeom', {
                    'prst': 'rect',
                })
                ET.SubElement(geometry, f'{{{drawingml_ns}}}avLst')
                ET.SubElement(anchor, f'{{{drawing_ns}}}clientData')
                additions[drawing_name] = serialize_xml(
                    drawing_root, drawing_ns, (('a', drawingml_ns), ('r', office_rel_ns)))
                drawing_relations = ET.Element(f'{{{package_rel_ns}}}Relationships')
                ET.SubElement(drawing_relations, f'{{{package_rel_ns}}}Relationship', {
                    'Id': 'rId1', 'Type': f'{office_rel_ns}/image',
                    'Target': '../media/unified_report_logo.png',
                })
                additions['xl/drawings/_rels/drawing1.xml.rels'] = serialize_xml(
                    drawing_relations, package_rel_ns)
                additions['xl/media/unified_report_logo.png'] = logo_path.read_bytes()
                replacements['[Content_Types].xml'] = serialize_xml(
                    content_types, content_ns)
                embedded = BytesIO()
                with ZipFile(embedded, 'w', ZIP_DEFLATED) as destination:
                    for item in source.infolist():
                        destination.writestr(
                            item, replacements.get(item.filename, source.read(item.filename)))
                    for name, data in additions.items():
                        destination.writestr(name, data)
            output = BytesIO(embedded.getvalue())
            output.seek(0)
            return send_file(
                output, as_attachment=True,
                download_name='tortkuldon_yagona_hisobot.xlsx',
                mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                max_age=0,
            )
        finally:
            con.close()
    except ValueError as exc:
        abort(400, f'Yagona hisobotni yaratib bo‘lmadi: {exc}')

@app.route('/hisobot/excel')
@login_required
def excel_report():
    from excel_dates import format_date_columns, display_date
    from date_sql import (
        mixed_date_key_expression, mixed_date_to_iso,
        register_mixed_date_key_function, validate_mixed_date_column,
    )
    from io import BytesIO
    from flask import send_file, abort
    from openpyxl import Workbook
    from openpyxl import load_workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.worksheet.filters import AutoFilter
    from openpyxl.utils import get_column_letter
    from copy import copy
    kinds = {'wheat':'Bug‘doy xaridi','cleaning':'Bug‘doy tozalash',
             'production':'Ishlab chiqarish','sales':'Sotuvlar',
             'debtors':'Xaridor qarzlari','payments':'To‘lovlar',
             'expenses':'Xarajatlar','stocks':'Ombor qoldiqlari'}
    kind=request.args.get('type','')
    if kind not in kinds: abort(400, 'Hisobot turini tanlang.')
    start=request.args.get('start','').strip(); end=request.args.get('end','').strip()
    try:
        if start: start = mixed_date_to_iso(start)
        if end: end = mixed_date_to_iso(end)
        if start and end and start>end: raise ValueError()
    except ValueError:
        abort(400, 'Sana oralig‘i noto‘g‘ri; YYYY-MM-DD yoki DD-MM-YYYY formatidan foydalaning.')
    if kind == 'production':
        template_path = Path(BASE_DIR) / 'static' / 'production_report_template.xlsx'
        if not template_path.is_file():
            abort(500, 'Ishlab chiqarish Excel dizayn shabloni topilmadi.')
        wb = load_workbook(template_path)
        ws = wb['Umumiy ishlab chiqarish']
    else:
        wb = Workbook()
        ws = wb.active
        ws.title = kinds[kind][:31]
    def query(con,sql,args=(),column='date',table=None):
        try:
            validate_mixed_date_column(con, table, column)
        except ValueError as exc:
            abort(400, f'Hisobotdagi sana yaroqsiz: {exc}')
        register_mixed_date_key_function(con)
        date_key = mixed_date_key_expression(column)
        filters=[]; params=list(args)
        if start: filters.append(f'{date_key}>=?'); params.append(start)
        if end: filters.append(f'{date_key}<=?'); params.append(end)
        sql, order_separator, ordering = sql.partition(' ORDER BY ')
        if order_separator:
            ordering = re.sub(
                rf'\b{re.escape(column)}\b', date_key, ordering, count=1
            )
        else:
            order_separator = ' ORDER BY '
            ordering = f'{date_key}, id'
        if filters:
            sql += (' AND ' if ' WHERE ' in sql else ' WHERE ')+' AND '.join(filters)
        if order_separator:
            sql += order_separator + ordering
        return con.execute(sql,params).fetchall()
    def financial_snapshot(con):
        try:
            return calculate_financial_snapshot(con)
        except ValueError as exc:
            abort(400, f'Moliyaviy hisobotni yaratib bo‘lmadi: {exc}')
    with db() as con:
        if kind=='wheat':
            ws.append(['Sana','Xarid turi','Yetkazuvchi','Kg','Narx / kg','Jami so‘m','Izoh'])
            for r in query(con,'SELECT date,purchase_method,supplier,net,price_per_kg,note FROM wheat_receipts',table='wheat_receipts'):
                ws.append([r['date'],r['purchase_method'],r['supplier'],r['net'],r['price_per_kg'],r['net']*r['price_per_kg'],r['note']])
        elif kind in ('cleaning','production'):
            prods=query(con,"""SELECT id,date,COALESCE(raw_wheat_kg,wheat_kg) raw,wheat_kg clean,
                flour_kg,bran_kg,muchka_kg,telashka_kg,note FROM production""",table='production')
            if kind=='cleaning':
                ws.append(['Sana','Olingan kg','Tozalangan kg','Kamaygan kg','Kamayish %'])
                for r in prods:
                    ws.append([r['date'],r['raw'],r['clean'],r['raw']-r['clean'],round(100*(r['raw']-r['clean'])/r['raw'],2) if r['raw'] else 0])
            else:
                from datetime import datetime
                from xml.etree import ElementTree as ET
                from zipfile import ZIP_DEFLATED, ZipFile

                bagged_products = BAGGED_FLOUR_PRODUCTS
                detail_by_id = {}
                for r in prods:
                    types={x['product']:x['qty_kg'] for x in con.execute('SELECT product,qty_kg FROM production_flour_types WHERE production_id=?',(r['id'],))}
                    bags = {(p, size): count for p, size, count in con.execute(
                        'SELECT product,bag_kg,bag_count FROM bagged_production_lines '
                        'WHERE production_id=?', (r['id'],))}
                    bag_kg = {
                        product: sum(
                            size * bags.get((product, size), 0) for size in (25, 50)
                        )
                        for product in bagged_products
                    }
                    product_kg = {
                        product: float(types[product])
                        if product in types else float(bag_kg[product])
                        for product in bagged_products
                    }
                    classified_kg = sum(product_kg.values()) + float(types.get('Qora un', 0) or 0)
                    flour_kg = float(r['flour_kg'] or 0)
                    detail_by_id[r['id']] = {
                        'types': types, 'bags': bags, 'bag_kg': bag_kg,
                        'product_kg': product_kg,
                        'classification_gap': flour_kg - classified_kg,
                    }

                layout_sheet = wb['Oliy va 1-nav un']
                summary_labels = ('Jami 25 kg qop', 'Jami 50 kg qop', 'Jami qop', 'Jami un, kg')
                summary_label_styles = [
                    copy(layout_sheet.cell(15, column)._style)
                    for column in (1, 3, 5, 7)
                ]
                summary_value_styles = [
                    copy(layout_sheet.cell(16, column)._style)
                    for column in (1, 3, 5, 7)
                ]
                summary_row_heights = {
                    row: layout_sheet.row_dimensions[row].height
                    for row in (15, 16, 17, 19)
                }
                summary_note = layout_sheet['A19'].value
                summary_note_style = copy(layout_sheet['A19']._style)
                period_cells = {
                    'Umumiy ishlab chiqarish': 'I1',
                    'Oliy va 1-nav un': 'G1',
                    'Qora un': 'D1',
                    'Kepak': 'D1',
                    'Muchka': 'D1',
                    'Telashka atxod': 'D1',
                }
                for sheet in wb.worksheets:
                    for merged in list(sheet.merged_cells.ranges):
                        if merged.min_row >= 8:
                            sheet.unmerge_cells(str(merged))
                    for row in sheet.iter_rows(
                            min_row=8, max_row=sheet.max_row,
                            max_col=sheet.max_column):
                        for cell in row:
                            cell.value = None

                def report_date(value):
                    return datetime.strptime(mixed_date_to_iso(value), '%Y-%m-%d')

                def report_date_text(value):
                    return report_date(value).strftime('%d.%m.%Y')

                if start or end:
                    period_start = report_date_text(start) if start else 'Cheklanmagan'
                    period_end = report_date_text(end) if end else 'Cheklanmagan'
                    period_text = f'{period_start} — {period_end}'
                elif prods:
                    period_text = (
                        f'{report_date_text(prods[0]["date"])} — '
                        f'{report_date_text(prods[-1]["date"])}'
                    )
                else:
                    period_text = 'Ma’lumot yo‘q'
                for sheet in wb.worksheets:
                    sheet[period_cells[sheet.title]] = f'Hisobot davri:\n{period_text}'
                    sheet.freeze_panes = 'A8'

                def copy_report_row(sheet, target_row, source_row, columns):
                    if target_row != source_row:
                        source_height = sheet.row_dimensions[source_row].height
                        if source_height is not None:
                            sheet.row_dimensions[target_row].height = source_height
                    for column in range(1, columns + 1):
                        target = sheet.cell(target_row, column)
                        source = sheet.cell(source_row, column)
                        if target_row != source_row and source.has_style:
                            target._style = copy(source._style)
                        target.value = None

                def write_report_row(sheet, target_row, values, style_row,
                                     date_columns=(), text_columns=()):
                    copy_report_row(sheet, target_row, style_row, len(values))
                    for column, value in enumerate(values, 1):
                        cell = sheet.cell(target_row, column)
                        if column in date_columns and value:
                            cell.value = report_date(value)
                            cell.number_format = 'dd.mm.yyyy'
                        else:
                            cell.value = value
                        if column in text_columns and isinstance(cell.value, str):
                            cell.data_type = 's'

                def set_report_table(sheet, headers, last_data_row):
                    for column, heading in enumerate(headers, 1):
                        sheet.cell(7, column).value = heading
                        sheet.cell(7, column).data_type = 's'
                    table_ref = (
                        f'A7:{get_column_letter(len(headers))}'
                        f'{max(7, last_data_row)}'
                    )
                    for table_name in sheet.tables:
                        table = sheet.tables[table_name]
                        if len(table.tableColumns) != len(headers):
                            abort(500, f'{sheet.title} varag‘idagi jadval ustunlari mos emas.')
                        table.ref = table_ref
                        if table.autoFilter is None:
                            table.autoFilter = AutoFilter(ref=table_ref)
                        else:
                            table.autoFilter.ref = table_ref
                        for column, heading in zip(table.tableColumns, headers):
                            column.name = heading

                general = wb['Umumiy ishlab chiqarish']
                general_headers = [
                    'Sana', 'Olingan bug‘doy (kg)', 'Tozalangan bug‘doy (kg)',
                    'Kamayish (kg)', 'Kamayish (%)', 'Jami un (kg)',
                    'Kepak (kg)', 'Muchka (kg)', 'Telashka (kg)', 'Izoh',
                ]
                production_notes = {}
                for r in prods:
                    detail = detail_by_id[r['id']]
                    note_parts = [str(r['note'])] if r['note'] else []
                    gap = detail['classification_gap']
                    if abs(gap) > 0.000001:
                        if gap > 0:
                            note_parts.append(f'Tasniflanmagan un: {gap:g} kg')
                        else:
                            note_parts.append(f'Tasniflangan un farqi: {gap:g} kg')
                    for product in bagged_products:
                        bag_gap = detail['bag_kg'][product] - detail['product_kg'][product]
                        if abs(bag_gap) > 0.000001:
                            note_parts.append(f'{product} qop/kg farqi: {bag_gap:+g} kg')
                    production_notes[r['id']] = '; '.join(note_parts)
                for index, r in enumerate(prods):
                    row_number = 8 + index
                    raw = float(r['raw'] or 0)
                    cleaned = float(r['clean'] or 0)
                    loss = raw - cleaned
                    write_report_row(
                        general, row_number,
                        [r['date'], raw, cleaned, loss,
                         loss / raw if raw else 0,
                         float(r['flour_kg'] or 0), float(r['bran_kg'] or 0),
                         float(r['muchka_kg'] or 0), float(r['telashka_kg'] or 0),
                         production_notes[r['id']]],
                        8, date_columns=(1,), text_columns=(10,),
                    )
                general_total_row = 8 + len(prods)
                copy_report_row(general, general_total_row, 11, 10)
                general_raw = sum(float(r['raw'] or 0) for r in prods)
                general_cleaned = sum(float(r['clean'] or 0) for r in prods)
                general_loss = general_raw - general_cleaned
                general_totals = [
                    'JAMI', general_raw, general_cleaned, general_loss,
                    general_loss / general_raw if general_raw else 0,
                    sum(float(r['flour_kg'] or 0) for r in prods),
                    sum(float(r['bran_kg'] or 0) for r in prods),
                    sum(float(r['muchka_kg'] or 0) for r in prods),
                    sum(float(r['telashka_kg'] or 0) for r in prods), None,
                ]
                for column, value in enumerate(general_totals, 1):
                    cell = general.cell(general_total_row, column)
                    cell.value = value
                    if column == 1:
                        cell.data_type = 's'
                set_report_table(general, general_headers, 7 + len(prods))

                white = wb['Oliy va 1-nav un']
                white_headers = [
                    'Sana', 'Ishlab chiqarish ID', 'Un mahsuloti',
                    '25 kg qop soni', '50 kg qop soni', 'Jami qop soni',
                    'Jami kg', 'Izoh',
                ]
                white_rows = []
                white_bag_25 = white_bag_50 = 0
                for r in prods:
                    detail = detail_by_id[r['id']]
                    for product in bagged_products:
                        count_25 = int(detail['bags'].get((product, 25), 0))
                        count_50 = int(detail['bags'].get((product, 50), 0))
                        product_kg = detail['product_kg'][product]
                        if not product_kg and not count_25 and not count_50:
                            continue
                        note_parts = []
                        if r['note']:
                            note_parts.append(str(r['note']))
                        bag_gap = detail['bag_kg'][product] - product_kg
                        if abs(bag_gap) > 0.000001:
                            note_parts.append(f'Qop kilogrammi va mahsulot yozuvi farqi: {bag_gap:+g} kg')
                        if product not in detail['types'] and (count_25 or count_50):
                            note_parts.append('Mahsulot kg yozuvi yo‘q; kg qop hisobidan ko‘rsatildi')
                        white_rows.append([
                            r['date'], r['id'], product, count_25, count_50,
                            count_25 + count_50, product_kg, '; '.join(note_parts) or None,
                        ])
                        white_bag_25 += count_25
                        white_bag_50 += count_50
                    gap = detail['classification_gap']
                    if gap > 0.000001:
                        white_rows.append([
                            r['date'], r['id'], 'Eski tasniflanmagan un',
                            0, 0, 0, gap,
                            'Qop o‘lchami noma’lum yoki mahsulot turi tasniflanmagan.',
                        ])
                    elif gap < -0.000001:
                        white_rows.append([
                            r['date'], r['id'], 'Hisob farqi (jami un - tasnif)',
                            0, 0, 0, gap,
                            f'Tasniflangan jami un umumiy undan {abs(gap):g} kg ko‘p.',
                        ])
                for index, values in enumerate(white_rows):
                    source_row = 8 if values[2] == 'Eski tasniflanmagan un' else 9
                    write_report_row(
                        white, 8 + index, values, source_row,
                        date_columns=(1,), text_columns=(3, 8),
                    )
                white_total_row = 8 + len(white_rows)
                copy_report_row(white, white_total_row, 13, 8)
                white_kg_total = sum(float(row[6] or 0) for row in white_rows)
                white_totals = [
                    'JAMI', None, None, white_bag_25, white_bag_50,
                    white_bag_25 + white_bag_50, white_kg_total, None,
                ]
                for column, value in enumerate(white_totals, 1):
                    cell = white.cell(white_total_row, column)
                    cell.value = value
                    if column == 1:
                        cell.data_type = 's'
                set_report_table(white, white_headers, 7 + len(white_rows))

                summary_label_row = white_total_row + 2
                summary_value_row = summary_label_row + 1
                for target_row, source_row in (
                        (summary_label_row, 15), (summary_value_row, 16),
                        (summary_value_row + 1, 17), (summary_label_row + 4, 19)):
                    if summary_row_heights[source_row] is not None:
                        white.row_dimensions[target_row].height = summary_row_heights[source_row]
                summary_columns = (1, 3, 5, 7)
                summary_values = (
                    white_bag_25, white_bag_50,
                    white_bag_25 + white_bag_50, white_kg_total,
                )
                for index, column in enumerate(summary_columns):
                    white.merge_cells(
                        start_row=summary_label_row, start_column=column,
                        end_row=summary_label_row, end_column=column + 1,
                    )
                    label_cell = white.cell(summary_label_row, column)
                    label_cell._style = copy(summary_label_styles[index])
                    label_cell.value = summary_labels[index]
                    label_cell.data_type = 's'
                    white.merge_cells(
                        start_row=summary_value_row, start_column=column,
                        end_row=summary_value_row + 1, end_column=column + 1,
                    )
                    value_cell = white.cell(summary_value_row, column)
                    value_cell._style = copy(summary_value_styles[index])
                    value_cell.value = summary_values[index]
                note_row = summary_label_row + 4
                white.merge_cells(
                    start_row=note_row, start_column=1,
                    end_row=note_row, end_column=8,
                )
                note_cell = white.cell(note_row, 1)
                note_cell._style = copy(summary_note_style)
                note_cell.value = summary_note
                note_cell.data_type = 's'

                def fill_product_sheet(sheet_name, header, rows, total_label='JAMI'):
                    sheet = wb[sheet_name]
                    for index, values in enumerate(rows):
                        write_report_row(
                            sheet, 8 + index, values, 8,
                            date_columns=(1,), text_columns=(4,),
                        )
                    total_row = 8 + len(rows)
                    copy_report_row(sheet, total_row, 10, len(header))
                    total_values = [None] * len(header)
                    total_values[0] = total_label
                    total_values[2] = sum(float(row[2] or 0) for row in rows)
                    for column, value in enumerate(total_values, 1):
                        sheet.cell(total_row, column).value = value
                    sheet.merge_cells(
                        start_row=total_row, start_column=1,
                        end_row=total_row, end_column=2,
                    )
                    sheet.cell(total_row, 1).data_type = 's'
                    set_report_table(sheet, header, 7 + len(rows))

                by_product = {
                    'Qora un': [],
                    'Kepak': [],
                    'Muchka': [],
                    'Telashka atxod': [],
                }
                for r in prods:
                    detail = detail_by_id[r['id']]
                    for product in by_product:
                        if product == 'Qora un':
                            amount = float(detail['types'].get(product, 0) or 0)
                        elif product == 'Kepak':
                            amount = float(r['bran_kg'] or 0)
                        elif product == 'Muchka':
                            amount = float(r['muchka_kg'] or 0)
                        else:
                            amount = float(r['telashka_kg'] or 0)
                        if amount:
                            by_product[product].append([
                                r['date'], r['id'], amount, r['note'],
                            ])
                product_headers = ['Sana', 'Ishlab chiqarish ID', 'Ishlab chiqarilgan kg', 'Izoh']
                for product in ('Qora un', 'Kepak', 'Muchka', 'Telashka atxod'):
                    fill_product_sheet(product, product_headers, by_product[product])
        elif kind=='sales':
            con.execute('BEGIN')
            financial = financial_snapshot(con)
            customer_context=customer_identity_context(con)
            allocation = {item['id']: item for group in financial['customers'].values()
                          for item in group['obligations'] if item['kind']=='sale'}
            ws.append(['Sana','Xaridor','Telefon','Mahsulot','Sotuv turi','Qop kg','Qop soni','Jami kg','Birlik narxi','Jami so‘m','Sotuvda to‘langan','Keyingi to‘lovlar (worker_receipts)','Joriy qoldiq qarz','Qarz muddati','Eski to‘lovlardan FIFO','Jami to‘langan'])
            for r in query(con,'SELECT * FROM sales WHERE id NOT IN (SELECT sale_id FROM sale_cancellations) ORDER BY date,id',table='sales'):
                item = allocation[r['id']]
                customer,phone=customer_display(
                    customer_context,r['customer'],r['customer_phone'])
                ws.append([r['date'],customer,phone,r['product'],r['sale_mode'],r['bag_kg'],r['bag_count'],r['qty_kg'],r['unit_price'],item['amount'],item['initial_paid'],item['linked_paid'],item['balance'],r['due_date'] if item['balance']>0 else '',item['legacy_applied'],item['initial_paid']+item['linked_paid']+item['legacy_applied']])
            notes = wb.create_sheet('Izoh va ogohlantirishlar')
            notes.append(['Mavzu','Tushuntirish'])
            notes.append(['Sotuv sanasi filtri',f'{display_date(start)} — {display_date(end)}'])
            notes.append(['Qarz holati','To‘lovlar va qarz eksport paytidagi bazaning joriy holatidir. Sana filtri faqat sotuv sanasiga tegishli; davrdan tashqaridagi barcha qayd etilgan to‘lovlar ham hisoblanadi. Bu davr oxiridagi tarixiy qarz hisoboti emas.'])
            notes.append(['FIFO','Avval aniq bog‘langan to‘lovlar chegiriladi. Eski payments faqat bitta xaridor/telefon guruhiga ishonchli mos bo‘lsa, barcha sotuvlar va eski qarzlar orasida sana, eski qarz ustuvorligi va ID bo‘yicha FIFO taqsimlanadi. Bu tarixiy bog‘lanish isboti emas.'])
            notes.append(['Jami to‘langan','Boshlang‘ich + worker_receipts + ushbu sotuvga FIFO. Buxgalter qabul qilgan worker_receipts bir marta sanaladi. Ortiqcha to‘lov boshqa sotuvga ko‘chirilmaydi; qoldiq manfiy bo‘lmaydi.'])
            notes.append(['Ogohlantirishlar doirasi','Quyidagi ogohlantirishlar butun baza hisobiga tegishli, faqat tanlangan davrga emas. Eski qarzlar sotuv summasiga kiritilmagan.'])
            messages = {'legacy_identity_ambiguous':'Bir xil ism, turli telefon: eski to‘lov taqsimlanmadi',
                        'legacy_without_obligation':'Majburiyatga bog‘lanmagan eski to‘lov',
                        'legacy_fifo_not_explicit_link':'Eski to‘lov FIFO hisobida, aniq sotuv bog‘lanishi yo‘q',
                        'sale_overpaid':'Sotuvda ortiqcha to‘lov',
                        'receipt_without_active_sale':'Faol sotuvga bog‘lanmagan keyingi to‘lov',
                        'cancelled_sale_initial_payment':'Bekor qilingan sotuvda boshlang‘ich to‘lov',
                        'accountant_receipt_mismatch':'Buxgalter to‘lovi bilan manba yozuvi mos emas',
                        'accountant_receipt_without_source':'Buxgalter to‘lovining manba yozuvi yo‘q'}
            for warning in financial['warnings']:
                notes.append([messages.get(warning['code'],warning['code']),json.dumps(warning,ensure_ascii=False,default=str)])
            for row in financial['unallocated_legacy']:
                notes.append(['Taqsimlanmagan eski to‘lov',json.dumps(row,ensure_ascii=False,default=str)])
            for group in financial['customers'].values():
                if group['legacy_excess']:
                    notes.append(['Taqsimlanmagan ortiqcha eski to‘lov',json.dumps({'customer':group['customer'],'phone':group['phone'],'amount':group['legacy_excess']},ensure_ascii=False,default=str)])
            notes.freeze_panes='A2'; notes.auto_filter.ref=notes.dimensions
            notes.column_dimensions['A'].width=40; notes.column_dimensions['B'].width=100
            for row in notes:
                for cell in row:
                    cell.data_type='s'; cell.alignment=Alignment(wrap_text=True)
            for cell in notes[1]:
                cell.font=Font(bold=True,color='FFFFFF'); cell.fill=PatternFill('solid',fgColor='175B42')
        elif kind=='debtors':
            con.execute('BEGIN')
            financial = financial_snapshot(con)
            debtors = debt_customers(con, financial=financial)
            ws.append(['Xaridor','Telefon','Jami sotuv va eski qarz so‘m','To‘langan so‘m','Qolgan qarz so‘m','Eng yaqin muddat','Faol sotuvlar summasi','Eski qarz','Boshlang‘ich to‘lov','Keyingi to‘lov (worker_receipts)','Eski FIFO to‘lovi','Ortiqcha to‘lov'])
            for r in debtors:
                ws.append([r['customer'],'; '.join(phone or 'Ko‘rsatilmagan' for phone in r['phones']),r['total'],r['paid'],r['debt'],r['due_date'],r['sales_revenue'],r['opening_debt'],r['initial_paid'],r['linked_paid'],r['legacy_applied'],r['excess']])
            notes = wb.create_sheet('Izoh va ogohlantirishlar')
            debt_total = sum(r['debt'] for r in debtors)
            notes.append(['Mavzu','Qiymat yoki tushuntirish'])
            notes.append(['Eksportdagi jami qarz',debt_total])
            notes.append(['Umumiy moduldagi jami qarz',financial['totals']['debt']])
            notes.append(['Qarz farqi',financial['totals']['debt']-debt_total])
            notes.append(['Davr','Joriy qarzdorlar va barcha qayd etilgan to‘lovlar hisoblanadi. Sana filtri bu eksportga qo‘llanmaydi; bu tarixiy sana holatidagi qarz hisoboti emas.'])
            notes.append(['To‘langan so‘m','Qarzdorlar sahifasidagi kabi boshlang‘ich + worker_receipts + xaridorga ishonchli bog‘langan eski payments. Eski FIFO ustuni faqat majburiyatlarga ajratilgan qism. Ortiqcha pul ham qabul qilingan pul tarkibida, ammo boshqa majburiyat yoki xaridor qarzidan chegirilmaydi.'])
            notes.append(['Hisoblash','Eski qarz faol sotuv daromadiga kirmaydi. Buxgalter qabul qilgan worker_receipts bir marta hisoblanadi. Nol qoldiqli xaridorlar asosiy varaqda yo‘q.'])
            notes.append(['FIFO','Eski to‘lov faqat bitta ism/telefon guruhiga mos bo‘lsa, avval aniq bog‘langan to‘lovlar chegirilib, sana, eski qarz ustuvorligi va ID tartibida taqsimlanadi. FIFO tarixiy sotuv bog‘lanishining isboti emas.'])
            messages = {'legacy_identity_ambiguous':'Bir xil ism, turli telefon: eski to‘lov taqsimlanmadi',
                        'legacy_without_obligation':'Majburiyatga bog‘lanmagan eski to‘lov',
                        'legacy_fifo_not_explicit_link':'Eski to‘lov FIFO hisobida, aniq sotuv bog‘lanishi yo‘q',
                        'sale_overpaid':'Sotuvda ortiqcha to‘lov',
                        'receipt_without_active_sale':'Faol sotuvga bog‘lanmagan keyingi to‘lov',
                        'cancelled_sale_initial_payment':'Bekor qilingan sotuvda boshlang‘ich to‘lov',
                        'accountant_receipt_mismatch':'Buxgalter to‘lovi bilan manba yozuvi mos emas',
                        'accountant_receipt_without_source':'Buxgalter to‘lovining manba yozuvi yo‘q'}
            for warning in financial['warnings']:
                notes.append([messages.get(warning['code'],warning['code']),json.dumps(warning,ensure_ascii=False,default=str)])
            for row in financial['unallocated_legacy']:
                notes.append(['Taqsimlanmagan eski to‘lov',json.dumps(row,ensure_ascii=False,default=str)])
            phones_by_name = {}
            for (name, phone), group in financial['customers'].items():
                phones_by_name.setdefault(name,set()).add(phone)
                if group['legacy_excess'] or group['linked_excess']:
                    notes.append(['Ortiqcha to‘lov (nol qarzli xaridorlar ham)',json.dumps({'customer':name,'phone':phone,'legacy_excess':group['legacy_excess'],'linked_excess':group['linked_excess']},ensure_ascii=False,default=str)])
            for name, phones in phones_by_name.items():
                if len(phones)>1:
                    notes.append(['Bir xil ismli xaridorlar alohida hisoblandi',json.dumps({'customer':name,'phones':sorted(phones)},ensure_ascii=False)])
            notes.freeze_panes='A2'; notes.auto_filter.ref=notes.dimensions
            notes.column_dimensions['A'].width=40; notes.column_dimensions['B'].width=100
            for row in notes:
                for cell in row:
                    if isinstance(cell.value,str): cell.data_type='s'
                    cell.alignment=Alignment(wrap_text=True)
            for cell in notes[1]:
                cell.font=Font(bold=True,color='FFFFFF'); cell.fill=PatternFill('solid',fgColor='175B42')
        elif kind=='payments':
            from decimal import Decimal
            con.execute('BEGIN')
            financial = financial_snapshot(con)
            customer_context=customer_identity_context(con)
            users = {r['id']:r['username'] for r in con.execute('SELECT id,username FROM users')}
            sales = {r['id']:dict(r) for r in con.execute('SELECT * FROM sales')}
            cancelled = {r['sale_id'] for r in con.execute('SELECT sale_id FROM sale_cancellations')}
            direct = {r['receipt_id']:dict(r) for r in con.execute('SELECT * FROM accountant_worker_receipts')}
            batches = {r['sale_id']:r['batch_id'] for r in con.execute('SELECT * FROM worker_sale_batch_items')}
            batch_channels = {}
            for r in con.execute('SELECT * FROM worker_sale_batch_payments ORDER BY id'):
                batch_channels.setdefault(r['batch_id'],[]).append(dict(r))
            ws.append(['Sana','Xaridor','Telefon','Qabul qilingan summa','Izoh','To‘lov turi','Manba','Manba ID','Qabul qiluvchi / yozuvdagi foydalanuvchi','Tegishli xodim','Kanal (yozuv bo‘yicha)'])
            events = []
            for r in query(con,'SELECT * FROM sales WHERE paid>0 ORDER BY date,id',table='sales'):
                note = r['note'] or ''
                channel = r['payment_method']
                if r['id'] in batches:
                    batch_id = batches[r['id']]
                    channel = ''
                    note += ' | Paket '+str(batch_id)+': sales.paid — taqsimlangan boshlang‘ich summa; kanal sotuvga aniq bog‘lanmagan. Paket kanallari (qayta qo‘shilmaydi): '+json.dumps(batch_channels.get(batch_id,[]),ensure_ascii=False)
                if r['id'] in cancelled: note += ' | Bekor qilingan sotuvdagi pul yozuvi; avtomatik qaytarilgan deb hisoblanmadi.'
                if not users.get(r['user_id']): note += ' | Qabul qiluvchi aniqlanmagan.'
                customer,phone=customer_display(
                    customer_context,r['customer'],r['customer_phone'])
                events.append([r['date'],customer,phone,Decimal(str(r['paid'])),note,'Boshlang‘ich sotuv to‘lovi','sales.paid',r['id'],users.get(r['user_id'],''),users.get(r['user_id'],''),channel])
            for r in query(con,'SELECT * FROM payments ORDER BY date,id',table='payments'):
                customer,_=customer_display(customer_context,r['customer'])
                events.append([r['date'],customer,'',Decimal(str(r['amount'])),r['note'] or '','Eski to‘lov','payments',r['id'],'','',''])
            for r in query(con,'SELECT * FROM worker_receipts ORDER BY date,id',table='worker_receipts'):
                sale = sales.get(r['sale_id'],{})
                accountant = direct.get(r['id'])
                recipient = accountant['accountant_id'] if accountant else r['worker_id']
                note = accountant['note'] if accountant else ''
                if not sale or r['sale_id'] in cancelled: note += ' | Faol sotuvga bog‘lanmagan pul yozuvi.'
                if not users.get(recipient): note += ' | Qabul qiluvchi aniqlanmagan.'
                if accountant and (accountant['payment_date']!=r['date'] or accountant['amount']!=r['amount'] or accountant['payment_method']!=r['payment_method'] or accountant['worker_id']!=r['worker_id'] or accountant['customer']!=sale.get('customer') or accountant['customer_phone']!=sale.get('customer_phone')):
                    note += ' | Buxgalter belgilashi manbaga mos emas; sana, summa, kanal worker_receipts yozuvidan olindi.'
                customer,phone=customer_display(
                    customer_context,sale.get('customer',''),sale.get('customer_phone',''))
                events.append([r['date'],customer,phone,Decimal(str(r['amount'])),note,'Buxgalter olgan qarz to‘lovi' if accountant else 'Xodim olgan qarz to‘lovi','worker_receipts',r['id'],users.get(recipient,''),users.get(r['worker_id'],''),r['payment_method']])
            for r in query(con,'SELECT * FROM opening_debt_receipts ORDER BY date,id',
                           table='opening_debt_receipts'):
                debt=con.execute('SELECT customer,customer_phone FROM opening_debts WHERE id=?',
                                 (r['opening_debt_id'],)).fetchone()
                if debt is None:
                    continue
                customer,phone=customer_display(customer_context,debt['customer'],debt['customer_phone'])
                events.append([r['date'],customer,phone,Decimal(str(r['amount'])),
                    (r['note'] or '')+' | Eski qarz #'+str(r['opening_debt_id']),
                    'Eski qarz to‘lovi','opening_debt_receipts',r['id'],
                    users.get(r['receiver_id'],''),users.get(r['worker_id'],''),r['payment_method']])
            for row in sorted(events,key=lambda r:(mixed_date_to_iso(r[0]),r[6],r[7])): ws.append(row)
            notes = wb.create_sheet('Izoh va ogohlantirishlar')
            notes.append(['Mavzu','Qiymat yoki tushuntirish'])
            notes.append(['Tanlangan davrda jami qabul qilingan pul',sum((r[3] for r in events),Decimal(0))])
            notes.append(['Sana mezoni',f'{display_date(start)} — {display_date(end)}. Boshlang‘ich to‘lov: sales.date (alohida to‘lov sanasi yo‘q); eski to‘lov: payments.date; keyingi to‘lov: worker_receipts.date. Chegaralar kiritiladi.'])
            notes.append(['Pul va daromad','Jami qabul qilingan pul sotuv daromadi emas. Ichki worker_transfers, accountant_worker_receipts va paket kanal summalari yangi pul sifatida qo‘shilmaydi.'])
            notes.append(['Yozuv birligi','Manba + Manba ID noyob. Bitta haqiqiy to‘lov bir necha sotuvga bo‘lingan bo‘lsa, bazada umumiy hodisa ID yo‘qligi sabab satrlar taxmin bilan birlashtirilmaydi. Paket sales.paid summasi taqsimlangan qismdir.'])
            notes.append(['Kanal aniqligi','naqd / plastik / hisob (bank). Eski yozuvlarga migratsiya naqd qiymatini bergan bo‘lishi mumkin; kanal ustuni bazadagi belgini ko‘rsatadi, tarixiy tasdiq emas. Paketlarda sotuvga kanal taqsimlanmaydi. Bo‘sh yoki noma’lum kanal taxmin bilan to‘ldirilmaydi.'])
            notes.append(['Qabul qiluvchi','Boshlang‘ich to‘lovda sales.user_id — yozuvdagi foydalanuvchi. Keyingi to‘lovda worker_id yoki accountant_id. Eski payments uchun qabul qiluvchi tasdiqlanmagan.'])
            notes.append(['Eski payments','Telefon, qabul qiluvchi va kanal aniq bog‘lanmaganligi sabab bo‘sh qoldirildi. Boshqa xaridor yozuvidan taxminan olinmadi; asl izoh saqlandi.'])
            notes.append(['Barcha sanalar: modul tushumi',financial['totals']['received']])
            if not start and not end:
                notes.append(['Eksport va modul farqi',financial['totals']['received']-sum((r[3] for r in events),Decimal(0))])
            notes.append(['Ogohlantirishlar doirasi','Quyidagi modul ogohlantirishlari barcha sanalarga tegishli. Bekor qilingan sotuvdagi qayd etilgan pul yo‘qolmaydi; qaytarish taxmin qilinmaydi.'])
            for warning in financial['warnings']:
                notes.append(['Modul ogohlantirishi',json.dumps(warning,ensure_ascii=False,default=str)])
            for row in ws.iter_rows(min_row=2):
                for cell in row:
                    if isinstance(cell.value,str): cell.data_type='s'
            notes.freeze_panes='A2'; notes.auto_filter.ref=notes.dimensions
            notes.column_dimensions['A'].width=40; notes.column_dimensions['B'].width=100
            for row in notes:
                for cell in row:
                    if isinstance(cell.value,str): cell.data_type='s'
                    cell.alignment=Alignment(wrap_text=True)
            for cell in notes[1]:
                cell.font=Font(bold=True,color='FFFFFF'); cell.fill=PatternFill('solid',fgColor='175B42')
        elif kind=='expenses':
            ws.append(['Sana','Tizimga kiritgan shaxs','Toifa','Summa',
                       'To‘lov kanali','Pul manbai','Izoh'])
            expense_rows=query(con,'''SELECT e.*, entered.username AS entered_by_name,
                    source.role AS source_role
                    FROM expenses e
                    LEFT JOIN users entered ON entered.id=COALESCE(e.entered_by,e.user_id)
                    LEFT JOIN users source ON source.id=e.user_id
                    ORDER BY date,e.id''',table='expenses')
            for r in expense_rows:
                item=expense_display(r)
                ws.append([item['date'],item['entered_by_display'],
                           item['category'],item['amount'],
                           item['payment_method_display'],item['fund_source_display'],item['note']])
        elif kind=='stocks':
            ws.append(['Mahsulot','Joriy qoldiq kg'])
            purchased=con.execute('SELECT COALESCE(SUM(net),0) FROM wheat_receipts').fetchone()[0]
            used=con.execute('SELECT COALESCE(SUM(COALESCE(raw_wheat_kg,wheat_kg)),0) FROM production').fetchone()[0]
            ws.append(['Bug‘doy',purchased-used])
            for name,qty in product_stocks(con).items(): ws.append([name,qty])
    if kind != 'production':
        # Explicit text columns: preserve user input verbatim, never as a formula.
        text_columns = {'wheat': (1,2,3,7), 'cleaning': (1,), 'sales': (1,2,3,4,5,14),
                        'debtors': (1,2,6), 'payments': (1,2,3,5),
                        'expenses': (1,2,3,5,6,7), 'stocks': (1,)}
        for row in ws.iter_rows(min_row=2):
            for column in text_columns[kind]:
                cell = row[column-1]
                if isinstance(cell.value, str):
                    cell.data_type = 's'
        ws.freeze_panes='A2'; ws.auto_filter.ref=ws.dimensions
        for cell in ws[1]:
            cell.font=Font(bold=True,color='FFFFFF');cell.fill=PatternFill('solid',fgColor='175B42');cell.alignment=Alignment(wrap_text=True)
        for col in ws.columns:
            letter=get_column_letter(col[0].column)
            ws.column_dimensions[letter].width=min(40,max(15,max(len(str(c.value or '')) for c in list(col)[:150])+2))
        date_columns = {'wheat': (1,), 'cleaning': (1,), 'sales': (1,14),
                        'debtors': (6,), 'payments': (1,), 'expenses': (1,), 'stocks': ()}
        try: format_date_columns(ws, date_columns[kind])
        except ValueError as exc: abort(400, str(exc))
    output=BytesIO();wb.save(output);output.seek(0)
    if kind == 'production':
        logo_path = Path(BASE_DIR) / 'static' / 'production_report_logo.png'
        if not logo_path.is_file():
            abort(500, 'Ishlab chiqarish Excel logotipi topilmadi.')
        logo_bytes = logo_path.read_bytes()
        spreadsheet_ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
        office_rel_ns = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
        package_rel_ns = 'http://schemas.openxmlformats.org/package/2006/relationships'
        content_ns = 'http://schemas.openxmlformats.org/package/2006/content-types'
        drawing_ns = 'http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing'
        drawingml_ns = 'http://schemas.openxmlformats.org/drawingml/2006/main'

        def xlsx_xml(root, default_namespace, prefixes=()):
            ET.register_namespace('', default_namespace)
            for prefix, namespace in prefixes:
                ET.register_namespace(prefix, namespace)
            return ET.tostring(root, encoding='utf-8', xml_declaration=True)

        def add_logo_to_workbook(workbook_bytes, image_bytes):
            replacements = {}
            additions = {}
            with ZipFile(BytesIO(workbook_bytes), 'r') as source:
                content_types = ET.fromstring(source.read('[Content_Types].xml'))
                if not any(
                        element.tag == f'{{{content_ns}}}Default'
                        and element.get('Extension') == 'png'
                        for element in content_types):
                    ET.SubElement(content_types, f'{{{content_ns}}}Default', {
                        'Extension': 'png', 'ContentType': 'image/png',
                    })
                for sheet_number in range(1, 7):
                    drawing_name = f'xl/drawings/drawing{sheet_number}.xml'
                    ET.SubElement(content_types, f'{{{content_ns}}}Override', {
                        'PartName': f'/{drawing_name}',
                        'ContentType': 'application/vnd.openxmlformats-officedocument.drawing+xml',
                    })
                    sheet_name = f'xl/worksheets/sheet{sheet_number}.xml'
                    sheet_root = ET.fromstring(source.read(sheet_name))
                    rel_name = f'xl/worksheets/_rels/sheet{sheet_number}.xml.rels'
                    if rel_name in source.namelist():
                        sheet_rels = ET.fromstring(source.read(rel_name))
                    else:
                        sheet_rels = ET.Element(f'{{{package_rel_ns}}}Relationships')
                    used_ids = {item.get('Id') for item in sheet_rels}
                    rel_id = 1
                    while f'rId{rel_id}' in used_ids:
                        rel_id += 1
                    drawing_rel_id = f'rId{rel_id}'
                    ET.SubElement(sheet_rels, f'{{{package_rel_ns}}}Relationship', {
                        'Id': drawing_rel_id,
                        'Type': f'{office_rel_ns}/drawing',
                        'Target': f'../drawings/drawing{sheet_number}.xml',
                    })
                    drawing_element = ET.Element(
                        f'{{{spreadsheet_ns}}}drawing',
                        {f'{{{office_rel_ns}}}id': drawing_rel_id},
                    )
                    insert_before = {
                        'legacyDrawing', 'legacyDrawingHF', 'picture', 'oleObjects',
                        'controls', 'webPublishItems', 'tableParts', 'extLst',
                    }
                    insert_at = next(
                        (index for index, child in enumerate(list(sheet_root))
                         if child.tag.rsplit('}', 1)[-1] in insert_before),
                        len(sheet_root),
                    )
                    sheet_root.insert(insert_at, drawing_element)
                    replacements[sheet_name] = xlsx_xml(
                        sheet_root, spreadsheet_ns, (('r', office_rel_ns),)
                    )
                    replacements[rel_name] = xlsx_xml(sheet_rels, package_rel_ns)

                    drawing_root = ET.Element(f'{{{drawing_ns}}}wsDr')
                    anchor = ET.SubElement(drawing_root, f'{{{drawing_ns}}}oneCellAnchor')
                    origin = ET.SubElement(anchor, f'{{{drawing_ns}}}from')
                    for name, value in (('col', '0'), ('colOff', '0'), ('row', '0'), ('rowOff', '0')):
                        ET.SubElement(origin, f'{{{drawing_ns}}}{name}').text = value
                    ET.SubElement(anchor, f'{{{drawing_ns}}}ext', {
                        'cx': '1047750', 'cy': '1114425',
                    })
                    picture = ET.SubElement(anchor, f'{{{drawing_ns}}}pic')
                    non_visual = ET.SubElement(picture, f'{{{drawing_ns}}}nvPicPr')
                    ET.SubElement(non_visual, f'{{{drawing_ns}}}cNvPr', {
                        'id': '1', 'name': f'Production report logo {sheet_number}',
                    })
                    ET.SubElement(
                        ET.SubElement(non_visual, f'{{{drawing_ns}}}cNvPicPr'),
                        f'{{{drawingml_ns}}}picLocks',
                        {'noChangeAspect': '1'},
                    )
                    fill = ET.SubElement(picture, f'{{{drawing_ns}}}blipFill')
                    ET.SubElement(fill, f'{{{drawingml_ns}}}blip', {
                        f'{{{office_rel_ns}}}embed': 'rId1',
                    })
                    stretch = ET.SubElement(fill, f'{{{drawingml_ns}}}stretch')
                    ET.SubElement(stretch, f'{{{drawingml_ns}}}fillRect')
                    shape_properties = ET.SubElement(picture, f'{{{drawing_ns}}}spPr')
                    geometry = ET.SubElement(shape_properties, f'{{{drawingml_ns}}}prstGeom', {
                        'prst': 'rect',
                    })
                    ET.SubElement(geometry, f'{{{drawingml_ns}}}avLst')
                    ET.SubElement(anchor, f'{{{drawing_ns}}}clientData')
                    additions[drawing_name] = xlsx_xml(
                        drawing_root, drawing_ns,
                        (('a', drawingml_ns), ('r', office_rel_ns)),
                    )
                    drawing_rel_root = ET.Element(f'{{{package_rel_ns}}}Relationships')
                    ET.SubElement(drawing_rel_root, f'{{{package_rel_ns}}}Relationship', {
                        'Id': 'rId1',
                        'Type': f'{office_rel_ns}/image',
                        'Target': '../media/production_report_logo.png',
                    })
                    additions[
                        f'xl/drawings/_rels/drawing{sheet_number}.xml.rels'
                    ] = xlsx_xml(drawing_rel_root, package_rel_ns)
                replacements['[Content_Types].xml'] = xlsx_xml(
                    content_types, content_ns
                )
                additions['xl/media/production_report_logo.png'] = image_bytes
                result = BytesIO()
                with ZipFile(result, 'w', ZIP_DEFLATED) as destination:
                    for item in source.infolist():
                        destination.writestr(
                            item, replacements.get(item.filename, source.read(item.filename))
                        )
                    for name, data in additions.items():
                        destination.writestr(name, data)
            return result.getvalue()

        output = BytesIO(add_logo_to_workbook(output.getvalue(), logo_bytes))
        output.seek(0)
    return send_file(output,as_attachment=True,download_name=f'tortkuldon_{kind}.xlsx',
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


def sale_payment_allocations(con):
    result = {}
    names = [r[0] for r in con.execute('SELECT customer FROM sales UNION SELECT customer FROM opening_debts')]
    for customer in names:
        debts = []
        for r in con.execute('SELECT id,date,qty_kg*price_per_kg-paid AS balance FROM sales WHERE customer=? AND id NOT IN (SELECT sale_id FROM sale_cancellations)', (customer,)):
            if r['balance']>0: debts.append((r['date'],1,r['id'],r['balance']))
        for r in con.execute('SELECT id,date,amount FROM opening_debts WHERE customer=?',(customer,)):
            debts.append((r['date'],0,r['id'],r['amount']))
        left=con.execute('SELECT COALESCE(SUM(amount),0) FROM payments WHERE customer=?',(customer,)).fetchone()[0]
        for d,kind,id_,balance in sorted(debts):
            allocated=min(max(0,left),balance);left-=allocated
            if kind==1:result[id_]=allocated
    return result

def make_backup():
    folder = Path(os.environ.get('ERP_BACKUP_DIR') or os.environ.get('BACKUP_DIR') or (Path(BASE_DIR) / 'zaxira_nusxalar'))
    folder.mkdir(parents=True, exist_ok=True)
    destination=folder/('tortkuldon_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f')+'.db')
    source=db()
    try:
        target=sqlite3.connect(str(destination))
        try:source.backup(target)
        finally:target.close()
    finally:source.close()
    return destination

@app.route('/zaxira')
@login_required
def backup_page():
    from flask import send_file
    path=make_backup()
    return send_file(path,as_attachment=True,download_name=path.name)

@app.route('/sotuv/bekor/<int:sale_id>',methods=['POST'])
@login_required
def cancel_sale(sale_id):
    if session.get('role')!='buxgalter':
        flash('Faqat buxgalter sotuvni bekor qila oladi.','error');return redirect(url_for('sales_page'))
    reason=request.form.get('reason','').strip()
    if len(reason)<5:
        flash('Bekor qilish sababini kamida 5 ta belgi bilan yozing.','error');return redirect(url_for('sales_page'))
    make_backup()
    with db() as con:
        con.execute('BEGIN IMMEDIATE')
        row=con.execute('SELECT * FROM sales WHERE id=?',(sale_id,)).fetchone()
        if not row or con.execute('SELECT 1 FROM sale_cancellations WHERE sale_id=?',(sale_id,)).fetchone():
            flash('Sotuv topilmadi yoki avval bekor qilingan.','error');return redirect(url_for('sales_page'))
        if con.execute('SELECT 1 FROM worker_sale_batch_items WHERE sale_id=?',(sale_id,)).fetchone():
            flash('Bir nechta mahsulotli sotuvni alohida bekor qilib bo‘lmaydi. Buxgalter bilan tekshiring.','error')
            return redirect(url_for('sales_page'))
        if row['paid']>0.009:
            flash('Pul qabul qilingan sotuvni avtomatik bekor qilish xavfli. Avval buxgalter bilan hisobni tekshiring.','error');return redirect(url_for('sales_page'))
        if con.execute('SELECT 1 FROM payments WHERE customer=? LIMIT 1',(row['customer'],)).fetchone():
            flash('Xaridorda keyingi to‘lovlar bor. Qarzni qayta taqsimlamaslik uchun sotuv bekor qilinmadi.','error');return redirect(url_for('sales_page'))
        if con.execute('SELECT 1 FROM worker_receipts WHERE sale_id=? LIMIT 1',(sale_id,)).fetchone():
            flash('Bu sotuv bo‘yicha keyingi qarz to‘lovi mavjud. Sotuv bekor qilinmadi. Pul va mahsulot qaytarilishini buxgalter bilan alohida tekshiring.','error');return redirect(url_for('sales_page'))
        con.execute('INSERT INTO sale_cancellations(sale_id,reason,cancelled_at,user_id) VALUES(?,?,?,?)',(sale_id,reason,datetime.now().isoformat(timespec='seconds'),session['user_id']))
    flash('Sotuv bekor qilindi, mahsulot omborga qaytdi. Nakladnoy raqami arxivda saqlandi.','success')
    return redirect(url_for('sales_page'))

@app.route('/xaridor/<path:customer>')
@login_required
def customer_statement(customer):
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
    return render_template('customer_statement.html',customer=display_customer,groups=groups,
        financial_warnings=financial_warnings,exception_history=exception_history,
        unallocated_legacy=unallocated_legacy)


@app.route('/moliya-hisobotlar')
@login_required
def combined_finance_page():
    if current_user()['role'] not in ('buxgalter','rahbar'): abort(403)
    day = request.args.get('date') or date.today().isoformat()
    try: day = _business_date_key(day)
    except (ValueError, TypeError): abort(400, 'Hisobot sanasi noto‘g‘ri.')
    today = date.today()
    current_start = today.replace(day=1)
    previous_end = current_start - timedelta(days=1)
    previous_start = previous_end.replace(day=1)
    preset = request.args.get('period')
    if preset == 'current_month':
        monthly_start, monthly_end = current_start, today
    elif preset == 'previous_month':
        monthly_start, monthly_end = previous_start, previous_end
    else:
        raw_start = request.args.get('start', '').strip()
        raw_end = request.args.get('end', '').strip()
        try:
            if not raw_start and not raw_end:
                monthly_start, monthly_end = current_start, today
            else:
                parsed_start = _business_date(raw_start)[0] if raw_start else None
                parsed_end = _business_date(raw_end)[0] if raw_end else None
                monthly_start = parsed_start or parsed_end
                monthly_end = parsed_end or parsed_start
            if monthly_start > monthly_end:
                abort(400, 'Boshlanish sanasi tugash sanasidan keyin bo‘lishi mumkin emas.')
        except (ValueError, TypeError):
            abort(400, 'Hisobot davri sanalari noto‘g‘ri.')
    monthly_start_iso = monthly_start.isoformat()
    monthly_end_iso = monthly_end.isoformat()
    with db() as con:
        con.execute('BEGIN')
        try:
            cash = cash_receipt_totals(con, day, 'accountant')
            worker_cash = cash_receipt_totals(con, day, 'workers')
            general_cash = cash_receipt_totals(con, day)
        except ValueError as exc:
            abort(400, f'Kassa sanalarini yoki summalarini tekshiring: {exc}')
        production=con.execute('SELECT COALESCE(SUM(flour_kg+bran_kg+muchka_kg+telashka_kg),0) FROM production WHERE business_date_key(date)=?',(day,)).fetchone()[0]
        sold=con.execute('SELECT COALESCE(SUM(qty_kg),0) kg, COALESCE(SUM(qty_kg*price_per_kg),0) total, COALESCE(SUM(paid),0) paid FROM sales WHERE business_date_key(date)=? AND id NOT IN (SELECT sale_id FROM sale_cancellations)',(day,)).fetchone()
        legacy_pay=con.execute('SELECT COALESCE(SUM(amount),0) FROM payments WHERE business_date_key(date)=?',(day,)).fetchone()[0]
        expense=con.execute("""SELECT COALESCE(SUM(amount),0) FROM (
            SELECT amount FROM expenses WHERE business_date_key(date)=?
            UNION ALL
            SELECT amount FROM wheat_purchase_payments WHERE business_date_key(payment_date)=?
        )""",(day,day)).fetchone()[0]
        cash_additions_by_channel={method:0.0 for method in ('naqd','hisob')}
        for row in con.execute("""SELECT payment_method,SUM(amount) amount
            FROM accountant_cash_additions WHERE business_date_key(date)=?
            GROUP BY payment_method""",(day,)):
            cash_additions_by_channel[row['payment_method']]=float(row['amount'])
        wheat_payments_by_channel={method:0.0 for method in ('naqd','hisob')}
        for row in con.execute("""SELECT payment_method,SUM(amount) amount
            FROM wheat_purchase_payments WHERE business_date_key(payment_date)=?
            GROUP BY payment_method""",(day,)):
            wheat_payments_by_channel[row['payment_method']]=float(row['amount'])
        initial=cash['initial']
        direct_channels=cash['channels']
        direct_total=cash['direct']
        worker_sales=con.execute("""SELECT u.username,COUNT(DISTINCT s.id) count_sales,COALESCE(SUM(s.qty_kg),0) kg,
              COALESCE(SUM(s.qty_kg*s.price_per_kg),0) sales_amount
              FROM sales s JOIN users u ON u.id=s.user_id
              WHERE business_date_key(s.date)=? AND u.role IN ('umid','maxmud')
              AND NOT EXISTS(SELECT 1 FROM sale_cancellations c WHERE c.sale_id=s.id)
              GROUP BY u.id,u.username ORDER BY u.username""",(day,)).fetchall()
        worker_receipts=con.execute("""SELECT u.username,COALESCE(SUM(r.amount),0) amount FROM worker_receipts r
              JOIN users u ON u.id=r.worker_id
              WHERE business_date_key(r.date)=? AND NOT EXISTS(SELECT 1 FROM accountant_worker_receipts a WHERE a.receipt_id=r.id)
              GROUP BY u.id,u.username ORDER BY u.username""",(day,)).fetchall()
        approved_transfers=con.execute("""SELECT u.username,t.payment_method,SUM(t.amount) amount
              FROM worker_transfers t JOIN users u ON u.id=t.worker_id
              WHERE t.status='approved' AND substr(t.decided_at,1,10)=?
              GROUP BY u.id,t.payment_method ORDER BY u.username""",(day,)).fetchall()
        total_sales=con.execute('SELECT COALESCE(SUM(qty_kg*price_per_kg),0) FROM sales WHERE id NOT IN (SELECT sale_id FROM sale_cancellations)').fetchone()[0]
        wheat=con.execute('SELECT COALESCE(SUM(net*price_per_kg),0) FROM wheat_receipts').fetchone()[0]
        total_expense=con.execute('SELECT COALESCE(SUM(amount),0) FROM expenses').fetchone()[0]
        all_direct={m:0.0 for m in PAYMENT_METHODS}
        for r in con.execute('SELECT payment_method,SUM(amount) amount FROM accountant_worker_receipts GROUP BY payment_method'):
            all_direct[r['payment_method']]=float(r['amount'])
        snapshot_cache = {}

        def monthly_snapshot(cutoff, warnings):
            if cutoff not in snapshot_cache:
                snapshot_cache[cutoff] = _monthly_historical_snapshot(
                    con, cutoff, warnings)
            return snapshot_cache[cutoff]

        try:
            monthly_report = build_monthly_report(
                con, monthly_start, monthly_end, FLOUR_TYPES,
                ('Kepak', 'Muchka', 'Telashka atxod'),
                monthly_snapshot, _monthly_parse_date,
                lambda value: value.strftime('%d.%m.%Y'))
        except (ValueError, sqlite3.Error) as exc:
            abort(400, f'Oylik hisobotni hisoblab bo‘lmadi: {exc}')
    payments=cash['payments']
    balance=cash['balance']
    return render_template('combined_finance.html',day=day,production=production,sold=sold,
        legacy_pay=legacy_pay,expense=expense,initial=initial,
        cash_sales=cash['sales'],cash_expense=cash['expenses'],cash_transfers=cash['transfers'],
        cash_additions=cash['additions'],
        worker_balance_total=worker_cash['balance'],general_balance=general_cash['balance'],
        daily_receipts=general_cash['payments'],cash_undated=cash['undated'],cash_unassigned=cash['unassigned'],
        cash_additions_by_channel=cash_additions_by_channel,
        wheat_payments_by_channel=wheat_payments_by_channel,
        direct_channels=direct_channels,direct_total=direct_total,payments=payments,balance=balance,
        worker_sales=worker_sales,worker_receipts=worker_receipts,approved_transfers=approved_transfers,
        total_sales=total_sales,wheat=wheat,total_expense=total_expense,
        estimate=total_sales-wheat-total_expense,all_direct=all_direct,
        all_direct_total=sum(all_direct.values()),
        monthly_report=monthly_report,
        monthly_start=monthly_start_iso,monthly_end=monthly_end_iso,
        current_month_start=current_start.isoformat(),
        current_month_end=today.isoformat(),
        previous_month_start=previous_start.isoformat(),
        previous_month_end=previous_end.isoformat())


@app.route('/moliya-hisobotlar/oylik.xlsx')
@login_required
def monthly_finance_excel():
    if current_user()['role'] not in ('buxgalter', 'rahbar'):
        abort(403)
    raw_start = request.args.get('start', '').strip()
    raw_end = request.args.get('end', '').strip()
    try:
        if not raw_start or not raw_end:
            raise ValueError('Boshlanish va tugash sanalarini tanlang.')
        start = _business_date(raw_start)[0]
        end = _business_date(raw_end)[0]
        if start > end:
            raise ValueError('Boshlanish sanasi tugash sanasidan keyin bo‘lishi mumkin emas.')
    except (ValueError, TypeError) as exc:
        abort(400, f'Excel hisobot davri noto‘g‘ri: {exc}')

    with db() as con:
        con.execute('BEGIN')
        snapshot_cache = {}

        def monthly_snapshot(cutoff, warnings):
            if cutoff not in snapshot_cache:
                snapshot_cache[cutoff] = _monthly_historical_snapshot(
                    con, cutoff, warnings)
            return snapshot_cache[cutoff]

        try:
            report = build_monthly_report(
                con, start, end, FLOUR_TYPES,
                ('Kepak', 'Muchka', 'Telashka atxod'),
                monthly_snapshot, _monthly_parse_date,
                lambda value: value.strftime('%d.%m.%Y'))
        except (ValueError, sqlite3.Error) as exc:
            abort(400, f'Excel hisobotni hisoblab bo‘lmadi: {exc}')
    output = build_monthly_workbook(report)
    filename = f"oylik_hisobot_{start:%Y%m%d}_{end:%Y%m%d}.xlsx"
    return send_file(
        output, as_attachment=True, download_name=filename,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@app.route('/pul-aylanmasi-sinov')
@login_required
def cashflow_test_page():
    if current_user()['role'] not in ('buxgalter', 'rahbar'):
        abort(403)
    from cashflow_analytics import calculate_cashflow_analytics

    raw_start = request.args.get('start', '').strip()
    raw_end = request.args.get('end', '').strip() or date.today().isoformat()
    try:
        start = _business_date_key(raw_start) if raw_start else None
        end = _business_date_key(raw_end)
        if start and start > end:
            abort(400, 'Boshlanish sanasi tugash sanasidan keyin bo‘lishi mumkin emas.')
    except ValueError:
        abort(400, 'Sana YYYY-MM-DD yoki DD-MM-YYYY formatida bo‘lishi kerak.')

    uri = Path(DB_PATH).resolve().as_uri() + '?mode=ro'
    con = sqlite3.connect(uri, uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    con.create_function('business_date_key', 1, _business_date_key, deterministic=True)
    con.execute('PRAGMA query_only=ON')
    con.execute('PRAGMA busy_timeout=30000')
    try:
        con.execute('BEGIN')
        report = calculate_cashflow_analytics(con, start, end)
        report['events'],events_pagination=paginate_items(
            report['events'],'event_page','event_per_page')
    except ValueError as exc:
        abort(400, f'Pul aylanmasi hisobini yaratib bo‘lmadi: {exc}')
    finally:
        con.close()
    return render_template('cashflow_test.html', report=report,
                           events_pagination=events_pagination,
                           channel_labels={
                               'naqd': 'Naqd', 'plastik': 'Plastik',
                               'hisob': 'Bank', 'noma_lum': 'Aniqlanmagan kanal',
                           })


def cash_receipt_totals(con, day, custodian='all'):
    # payments is the legacy accountant receipt ledger; a named recipient is not inferred.
    # Cancellation is not a cash refund. Internal transfers only change custody.
    from decimal import Decimal
    if custodian not in ('all','accountant','workers'):
        raise ValueError('Kassa turi noto‘g‘ri.')
    zero = Decimal('0')
    result = {key: zero for key in ('initial','sales','legacy','linked','expenses','additions','direct','undated','unassigned','transfers')}
    result['channels'] = {method: zero for method in PAYMENT_METHODS}
    events = con.execute("""SELECT date,paid amount,'sales' kind,payment_method,0 direct,
            (SELECT role FROM users
             WHERE id=COALESCE(sales.initial_payment_recipient_id,sales.user_id)) owner
            FROM sales WHERE paid<>0
        UNION ALL SELECT date,amount,'legacy','','0','buxgalter' FROM payments
        UNION ALL SELECT r.date,r.amount,'linked',r.payment_method,
            EXISTS(SELECT 1 FROM accountant_worker_receipts a WHERE a.receipt_id=r.id),'' FROM worker_receipts r
        UNION ALL SELECT r.date,r.amount,'linked',r.payment_method,
            CASE WHEN r.received_by='accountant' THEN 1 ELSE 0 END,
            (SELECT role FROM users WHERE id=r.receiver_id) FROM opening_debt_receipts r
        UNION ALL SELECT date,amount,'expenses','',0,(SELECT role FROM users WHERE id=expenses.user_id) FROM expenses
        UNION ALL SELECT payment_date,amount,'expenses',payment_method,0,'buxgalter'
            FROM wheat_purchase_payments
        UNION ALL SELECT date,amount,'additions',payment_method,0,'buxgalter'
            FROM accountant_cash_additions
        UNION ALL SELECT decided_at,amount,'transfers',payment_method,0,'buxgalter'
            FROM worker_transfers WHERE status='approved'""")
    for row in events:
        kind = row['kind']
        unknown_owner = False
        if kind=='transfers' and custodian=='all':
            continue
        if custodian!='all':
            if kind in ('sales','expenses','additions'):
                if row['owner']=='buxgalter':
                    if custodian=='workers': continue
                elif row['owner'] in ('umid','maxmud','ishchi'):
                    if custodian=='accountant': continue
                else:
                    unknown_owner = True
            elif kind=='legacy' and custodian=='workers':
                continue
            elif kind=='linked' and bool(row['direct']) != (custodian=='accountant'):
                continue
        amount = Decimal(str(row['amount']))
        if not amount.is_finite() or amount < zero:
            raise ValueError('Pul yozuvida yaroqsiz summa mavjud.')
        event_day = _business_date_key(str(row['date'])[:10])
        if unknown_owner:
            if event_day<=day: result['unassigned'] += amount
            continue
        delta = -amount if kind=='expenses' or (kind=='transfers' and custodian=='workers') else amount
        if event_day < day:
            result['initial'] += delta
        elif event_day == day:
            result[kind] += amount
            if kind=='linked' and row['direct']:
                result['direct'] += amount
                method = row['payment_method']
                result['channels'][method] = result['channels'].get(method,zero) + amount
    result['payments'] = result['legacy'] + result['linked']
    transfer_delta = -result['transfers'] if custodian=='workers' else result['transfers']
    result['balance'] = (
        result['initial'] + result['sales'] + result['payments']
        + result['additions'] - result['expenses'] + transfer_delta)
    return result


@app.route('/kassa')
@login_required
def cash_page():
    day=request.args.get('date') or date.today().isoformat()
    try: day=_business_date_key(day)
    except ValueError: abort(400, 'Hisobot sanasi noto‘g‘ri.')
    with db() as con:
        con.execute('BEGIN')
        try: cash = cash_receipt_totals(con, day)
        except ValueError as exc: abort(400, f'Kassa sanalarini yoki summalarini tekshiring: {exc}')
    return render_template('cash.html',day=day,initial=cash['initial'],sales=cash['sales'],
        payments=cash['payments'],expenses=cash['expenses'],worker_payment=cash['direct'],
        additions=cash['additions'],channels=cash['channels'],
        balance=cash['balance'],undated=cash['undated'])

@app.route('/nazorat',methods=['GET','POST'])
@login_required
def control_page():
    with db() as con:
        if request.method=='POST':
            if session.get('role')!='buxgalter':flash('Faqat buxgalter limitni o‘zgartira oladi.','error')
            else:
                try:
                    product=request.form['product'];amount=float(request.form['min_kg'])
                    if product not in PRODUCTS or not math.isfinite(amount) or amount<0:raise ValueError()
                    con.execute('INSERT INTO stock_alerts(product,min_kg) VALUES(?,?) ON CONFLICT(product) DO UPDATE SET min_kg=excluded.min_kg',(product,amount))
                    flash('Ombor ogohlantirish chegarasi saqlandi.','success')
                except (ValueError,KeyError):flash('Mahsulot yoki kg noto‘g‘ri.','error')
        if not con.in_transaction:
            con.execute('BEGIN')
        limits={r['product']:r['min_kg'] for r in con.execute('SELECT * FROM stock_alerts')}
        stocks=product_stocks(con)
        try:
            financial = calculate_financial_snapshot(con)
            debtors=debt_customers(con, financial=financial, per_obligation=True)
        except ValueError as exc:
            abort(400, f'Qarz sanalarini tekshiring: {exc}')
        overdue=[];upcoming=[]
        today=date.today()
        try:
            for r in debtors:
                days=(today-_business_date(r['due_date'])[0]).days
                if days>0:overdue.append((r,days))
                elif days>=-3:upcoming.append((r,-days))
        except ValueError as exc:
            abort(400, f'Qarz sanalarini tekshiring: {exc}')
        overdue,overdue_pagination=paginate_items(
            overdue,'overdue_page','overdue_per_page')
        upcoming,upcoming_pagination=paginate_items(
            upcoming,'upcoming_page','upcoming_per_page')
    return render_template('control.html',overdue=overdue,upcoming=upcoming,
        overdue_pagination=overdue_pagination,
        upcoming_pagination=upcoming_pagination,
        stocks=stocks,limits=limits,products=PRODUCTS)

@app.route('/kunlik')
@login_required
def daily_page():
    day=request.args.get('date') or date.today().isoformat()
    try: day=_business_date_key(day)
    except ValueError: abort(400, 'Hisobot sanasi noto‘g‘ri.')
    with db() as con:
        con.execute('BEGIN')
        try: cash = cash_receipt_totals(con, day)
        except ValueError as exc: abort(400, f'Kassa sanalarini yoki summalarini tekshiring: {exc}')
        production=con.execute('SELECT COALESCE(SUM(flour_kg+bran_kg+muchka_kg+telashka_kg),0) FROM production WHERE business_date_key(date)=?',(day,)).fetchone()[0]
        sold=con.execute('SELECT COALESCE(SUM(qty_kg),0),COALESCE(SUM(qty_kg*price_per_kg),0),COALESCE(SUM(paid),0) FROM sales WHERE business_date_key(date)=? AND id NOT IN (SELECT sale_id FROM sale_cancellations)',(day,)).fetchone()
        received=cash['payments']
        expense=con.execute("""SELECT COALESCE(SUM(amount),0) FROM (
            SELECT amount FROM expenses WHERE business_date_key(date)=?
            UNION ALL
            SELECT amount FROM wheat_purchase_payments WHERE business_date_key(payment_date)=?
        )""",(day,day)).fetchone()[0]
    return render_template('daily.html',day=day,production=production,sold=sold,
        received=received,expense=expense,cash_additions=cash['additions'],
        undated=cash['undated'])

@app.route('/moliyaviy')
@login_required
def finance_page():
    with db() as con:
        sales=con.execute('SELECT COALESCE(SUM(qty_kg*price_per_kg),0) FROM sales WHERE id NOT IN (SELECT sale_id FROM sale_cancellations)').fetchone()[0]
        wheat=con.execute('SELECT COALESCE(SUM(net*price_per_kg),0) FROM wheat_receipts').fetchone()[0]
        expense=con.execute('SELECT COALESCE(SUM(amount),0) FROM expenses').fetchone()[0]
        accountant_cash={m:0 for m in PAYMENT_METHODS}
        for r in con.execute('SELECT payment_method,SUM(amount) amount FROM accountant_worker_receipts GROUP BY payment_method'):
            accountant_cash[r['payment_method']]=float(r['amount'])
        for r in con.execute("SELECT payment_method,SUM(amount) amount FROM opening_debt_receipts WHERE received_by='accountant' GROUP BY payment_method"):
            accountant_cash[r['payment_method']]=accountant_cash.get(r['payment_method'],0)+float(r['amount'])
    # Debt collections are cash receipts on sales recognized earlier, NOT additional revenue.
    return render_template('finance.html',sales=sales,wheat=wheat,expense=expense,
       estimate=sales-wheat-expense,accountant_cash=accountant_cash,
       accountant_cash_total=sum(accountant_cash.values()))

@app.route('/parol', methods=['GET', 'POST'])
@login_required
def change_password():
    if request.method == 'POST':
        user = current_user()
        old = request.form.get('old_password', '')
        new = request.form.get('new_password', '')
        if not check_password_hash(user['password_hash'], old):
            flash('Joriy parol noto‘g‘ri.', 'error')
        elif len(new) < 10:
            flash('Yangi parol kamida 10 belgidan iborat bo‘lsin.', 'error')
        else:
            with db() as con:
                con.execute('UPDATE users SET password_hash=? WHERE id=?', (generate_password_hash(new),user['id']))
                con.execute('''INSERT INTO user_auth_versions(user_id,version) VALUES(?,1)
                    ON CONFLICT(user_id) DO UPDATE SET version=version+1''', (user['id'],))
                con.execute('INSERT INTO user_audit(actor_user_id,target_user_id,action) VALUES(?,?,?)', (user['id'],user['id'],'change_password'))
            session.clear()
            flash('Parol yangilandi. Davom etish uchun qayta kiring.', 'success')
            return redirect(url_for('login'))
    return render_template('change_password.html')

@app.route('/foydalanuvchilar', methods=['GET', 'POST'])
@login_required
def manage_users():
    if current_user()['role'] != 'buxgalter': abort(403)
    if request.method == 'POST':
        action = request.form.get('action')
        target = request.form.get('user_id', type=int)
        if action != 'set_active' and 'operation_token' in request.form:
            abort(400, 'Holat tokeni boshqa amal uchun ishlatilmaydi.')
        if action == 'set_active':
            try:
                with db() as con:
                    con.execute('BEGIN IMMEDIATE')
                    token, payload, previous, claim = check_remaining_operation(con, 'user_status')
                    if previous:
                        flash('Bu holat o‘zgarishi oldin saqlangan. Joriy holat qayta o‘zgartirilmadi.', 'success')
                        return redirect(url_for('manage_users'))
                    row = con.execute('SELECT u.active,COALESCE(v.version,0) version FROM users u LEFT JOIN user_status_versions v ON v.user_id=u.id WHERE u.id=?', (target,)).fetchone()
                    if not row or target == session['user_id']:
                        raise ValueError('Bu foydalanuvchi holatini o‘zgartirib bo‘lmaydi.')
                    desired = request.form.get('target_active')
                    state = {'target_id': target, 'expected_active': row['active'],
                             'version': row['version'], 'desired_active': 1-row['active']}
                    if claim.get('state') != state or desired != str(state['desired_active']):
                        raise ValueError('Foydalanuvchi holati o‘zgargan yoki forma eskirgan. Sahifani qayta oching.')
                    con.execute('UPDATE users SET active=? WHERE id=?', (int(desired),target))
                    con.execute('INSERT INTO user_status_versions(user_id,version) VALUES(?,1) ON CONFLICT(user_id) DO UPDATE SET version=version+1', (target,))
                    con.execute('INSERT INTO user_audit(actor_user_id,target_user_id,action,details) VALUES(?,?,?,?)',
                                (session['user_id'],target,'set_active',json.dumps({'active':int(desired)})))
                    save_remaining_operation(con, 'user_status', token, payload, target)
                flash('Foydalanuvchining belgilangan holati saqlandi.', 'success')
            except (ValueError,sqlite3.Error) as exc:
                flash(str(exc) or 'Holat saqlanmadi.', 'error')
            return redirect(url_for('manage_users'))
        with db() as con:
            if action == 'reset':
                password = request.form.get('new_password','')
                if len(password) < 10: abort(400, 'Parol kamida 10 belgi bo‘lsin.')
                updated = con.execute('UPDATE users SET password_hash=? WHERE id=? AND id<>?',
                            (generate_password_hash(password),target,session['user_id']))
                if updated.rowcount != 1:
                    abort(404)
                con.execute('''INSERT INTO user_auth_versions(user_id,version) VALUES(?,1)
                    ON CONFLICT(user_id) DO UPDATE SET version=version+1''', (target,))
                con.execute('INSERT INTO user_audit(actor_user_id,target_user_id,action) VALUES(?,?,?)',(session['user_id'],target,'reset_password'))
            else: abort(400)
        return redirect(url_for('manage_users'))
    with db() as con:
        users=con.execute('SELECT u.id,u.username,u.role,u.phone,u.active,COALESCE(v.version,0) status_version FROM users u LEFT JOIN user_status_versions v ON v.user_id=u.id ORDER BY u.id').fetchall()
    return render_template('manage_users.html', users=users)

if __name__ == '__main__':
    if not os.path.isfile(DB_PATH):
        clean_template = os.path.join(BASE_DIR, 'data', 'tortkuldon_clean.sqlite')
        if os.path.isfile(clean_template):
            os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
            shutil.copy2(clean_template, DB_PATH)
    init_db()
    try:
        make_backup()
    except Exception as exc:
        print('Zaxira yaratishda xatolik:', exc)
    host = os.environ.get('ERP_HOST') or os.environ.get('HOST', '0.0.0.0')
    port = int(os.environ.get('ERP_PORT') or os.environ.get('PORT', '5051'))
    debug = os.environ.get('FLASK_DEBUG', 'false').lower() in ('true', '1', 'yes')
    app.run(host=host, port=port, debug=debug)
