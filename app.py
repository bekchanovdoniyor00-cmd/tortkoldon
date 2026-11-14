from flask import Flask, render_template, request, redirect, url_for, session, flash, abort, send_file, send_from_directory
import sqlite3, os, shutil, json, secrets, hmac, re
from pathlib import Path
from functools import wraps
import math
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP
from datetime import datetime, date, timedelta
from contextlib import closing
from werkzeug.security import generate_password_hash, check_password_hash
from itsdangerous import URLSafeSerializer, BadSignature
from finance_calculations import (
    calculate_financial_snapshot, customer_identity_context,
    resolve_customer_identity, resolve_customer_name,
)
from date_conversions import dmy_to_date, iso_to_date
from business_dates import (
    _business_date,
    _business_date_after,
    _business_date_key,
    _business_date_storage,
    _html_date_storage,
    _business_date_input,
    _business_date_display,
    _monthly_parse_date,
)
from ui_translations import DEFAULT_LANGUAGE, LANGUAGES, get_language, translate
from monthly_reporting import build_monthly_report, build_monthly_workbook
from unified_excel_export import export_unified_excel_report
from legacy_excel_export import export_legacy_excel_report
from accountant_worker_excel_export import export_accountant_worker_payments_excel
from report_snapshot_helpers import _monthly_historical_snapshot
from debt_report_helpers import customer_debt_details, debt_customers
from cash_report_helpers import sale_payment_allocations, cash_receipt_totals
from worker_finance_helpers import (
    worker_channel_balances, worker_balance, worker_snapshot_obligations,
    snapshot_has_ambiguous_legacy, balance_cents, payment_amount_cents,
)
from inventory_sales_helpers import (
    product_stocks, bagged_product_stocks, validate_bagged_sale,
    record_bagged_sale, product_stock_activity_exists, sale_values,
)
from worker_read_services import (
    build_worker_context, load_worker_stock_status, search_worker_customers,
)
from static_routes import (
    serve_service_worker, serve_robots_txt, serve_sitemap_xml,
)
from bulk_flour_service import (amount, ensure_schema as ensure_bulk_schema, bunker_balance, save_opening, save_production, save_packaging)
import rop_service as rop
import production_edit
from zachiska_print import build_zachiska_draft
import rop_forms
import rop_form_exports
from product_catalog import (
    FLOUR_TYPES, BAGGED_FLOUR_PRODUCTS, PRODUCTS, BAG_OPTIONS,
    LOOSE_PRODUCTS, INVOICE_GROUPS, WORKER_PRODUCTS,
)
from customer_helpers import phone_key, customer_display
from invoices_page_service import render_invoices_page
from customer_statement_service import build_customer_statement
from daily_finished_stock import record_daily_receipt
from warehouse_stock_corrections import (
    record_adjustment, edit_daily_receipt, receipt_fingerprint,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = (
    os.environ.get('ERP_DATABASE_PATH')
    or os.environ.get('DATABASE_PATH')
    or os.environ.get('DB_PATH')
    or os.path.join(BASE_DIR, 'tortkuldon.db')
)
OPENING_WHEAT_METHOD = 'Boshlang‘ich zaxira'


app = Flask(__name__)

from pagination_utils import PAGINATION_SIZES, _pagination_state, paginate_rows, paginate_items
from pagination_context_helpers import build_pagination_context


@app.context_processor
def pagination_context():
    return build_pagination_context(request, url_for)


@app.route('/service-worker.js')
def service_worker():
    return serve_service_worker()


@app.route('/robots.txt')
def robots_txt():
    return serve_robots_txt()


@app.route('/sitemap.xml')
def sitemap_xml():
    return serve_sitemap_xml()


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
    if os.path.isfile(DB_PATH):
        with closing(db()) as con:
            tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not set(rop.ROP_TABLES) <= tables:
            backup_path = make_backup()
            if not backup_path.is_file():
                raise RuntimeError('ROP migratsiyasi oldidan zaxira yaratilmadi.')
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
        ensure_bulk_schema(con)
        if not con.in_transaction:
            con.execute('BEGIN IMMEDIATE')
        rop.ensure_schema(con)
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
    'dashboard', 'boss_statistics', 'wheat', 'production_page', 'zachiska_print', 'edit_rop_production', 'packaging_page', 'sales_page', 'worker_customer_search', 'invoices_page',
    'invoice_saved', 'invoice_print', 'debtors_page', 'payments_page', 'unified_excel_report', 'rop_form_document',
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
            if endpoint not in ('dashboard', 'change_password', 'worker_sales', 'worker_new_sale', 'worker_receipt', 'worker_expense', 'worker_transfer', 'worker_customer_search', 'worker_customer_edit', 'worker_sale_form', 'worker_debts', 'worker_archive', 'worker_customers_page', 'worker_cash_page', 'worker_group_receipt', 'worker_stock_status', 'worker_multi_sale', 'worker_batch_invoice', 'worker_sale_result') :
                abort(403, description='Xodimning shaxsiy hisobi keyingi bosqichda ishga tushadi.')
        elif role == 'rahbar':
            if endpoint in ('change_password', 'debtor_legacy_edit', 'debtor_card_edit'):
                pass
            elif request.method != 'GET' or endpoint not in BOSS_READ_ENDPOINTS:
                abort(403, description='Boshliq faqat ma’lumotlarni ko‘ra oladi.')
        elif role != 'buxgalter':
            abort(403)
        if endpoint in ('production_page', 'edit_rop_production', 'packaging_page'):
            if role not in ('buxgalter', 'rahbar') or (role == 'rahbar' and request.method != 'GET'):
                abort(403, description='Ishlab chiqarish va qoplash faqat buxgalterga biriktirilgan.')
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
            and request.endpoint not in ('set_language', 'logout', 'debtor_legacy_edit', 'debtor_card_edit')):
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
        wheat_used = rop.wheat_warehouse_used(con)
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
            ('un', 'Un', (*BAGGED_FLOUR_PRODUCTS, 'Un')),
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


from payment_helpers import (
    PAYMENT_METHODS, EXPENSE_PAYMENT_METHODS, EXPENSE_FUND_SOURCES,
    expense_display, payment_method_from_form,
)














def worker_context():
    user = current_user()
    if user['role'] not in WORKER_PRODUCTS: abort(403)
    return build_worker_context(
        user=user, endpoint=request.endpoint, db_factory=db,
        paginate_rows=paginate_rows, paginate_items=paginate_items,
        calculate_financial_snapshot=calculate_financial_snapshot,
        customer_identity_context=customer_identity_context,
        worker_snapshot_obligations=worker_snapshot_obligations,
        customer_display=customer_display,
        worker_channel_balances=worker_channel_balances,
        expense_display=expense_display,
        snapshot_has_ambiguous_legacy=snapshot_has_ambiguous_legacy,
        _business_date=_business_date,
        WORKER_PRODUCTS=WORKER_PRODUCTS, ROLES=ROLES,
        PAYMENT_METHODS=PAYMENT_METHODS, BAG_OPTIONS=BAG_OPTIONS,
        LOOSE_PRODUCTS=LOOSE_PRODUCTS,
        debt_query=request.args.get('q', '') if request.endpoint == 'worker_debts' else '',
    )

@app.route('/xodim/sotuvlar')
@login_required
def worker_sales():
    return render_template('worker_home.html', **worker_context())

@app.route('/xodim/ombor-qoldiq')
@login_required
def worker_stock_status():
    user=current_user()
    if user['role'] not in WORKER_PRODUCTS: abort(403)
    return load_worker_stock_status(
        user=user, db_factory=db, product_stocks=product_stocks,
        bagged_product_stocks=bagged_product_stocks,
        worker_products=WORKER_PRODUCTS,
    )


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
    # DC_CUSTOMER_AUTOCOMPLETE_20261110: read-only shared lookup
    from customer_autocomplete_service import find_customers
    user = current_user()
    if user['role'] not in ('buxgalter', 'rahbar', 'umid', 'maxmud'):
        abort(403)
    with closing(db()) as con:
        matches = find_customers(con, request.args.get('q', ''),
                                 user['role'], user['id'])
    return {'customers': matches}, 200, {'Cache-Control': 'no-store'}

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
        used = rop.wheat_warehouse_used(con)
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
        used=rop.wheat_warehouse_used(con)
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

@app.route('/bugdoy/xarid/<int:receipt_id>/tahrir', methods=['GET', 'POST'])
@login_required
def edit_wheat_purchase_metadata(receipt_id):
    # DC-001A: metadata-only edit; money/stock/date edits require separate safeguards.
    if current_user()['role'] != 'buxgalter':
        abort(403)
    from wheat_purchase_edit_service import snapshot_hash, update_purchase_metadata
    from itsdangerous import URLSafeSerializer, BadSignature
    signer = URLSafeSerializer(app.secret_key, salt='wheat-purchase-metadata-v1')
    if request.method == 'POST':
        try:
            token = signer.loads(request.form.get('edit_token', ''))
            if (not isinstance(token, dict) or token.get('action') != 'wheat_metadata'
                    or token.get('receipt_id') != receipt_id
                    or token.get('actor_id') != session['user_id']):
                raise ValueError('Tahrirlash formasi yaroqsiz. Qayta oching.')
            with db() as con:
                con.execute('BEGIN IMMEDIATE')
                outcome = update_purchase_metadata(
                    con, receipt_id, session['user_id'], token.get('snapshot'),
                    request.form.get('supplier', ''), request.form.get('note', ''))
            if outcome == 'updated':
                flash('Xaridning yetkazuvchisi va izohi yangilandi; pul va ombor hisobi o‘zgarmadi.', 'success')
            else:
                flash('O‘zgartirish mavjud emas.', 'success')
            return redirect(url_for('wheat'))
        except (ValueError, BadSignature, sqlite3.Error) as exc:
            flash(str(exc) or 'Tahrirlash saqlanmadi.', 'error')
            return redirect(url_for('edit_wheat_purchase_metadata', receipt_id=receipt_id))
    with db() as con:
        record = con.execute('SELECT * FROM wheat_receipts WHERE id=?',
                             (receipt_id,)).fetchone()
    if record is None:
        abort(404)
    if record['purchase_method'] == OPENING_WHEAT_METHOD:
        abort(403)
    edit_token = signer.dumps({'action': 'wheat_metadata', 'receipt_id': receipt_id,
                               'actor_id': session['user_id'],
                               'snapshot': snapshot_hash(record)})
    return render_template('wheat_purchase_edit.html', receipt=record,
                           edit_token=edit_token)

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

def zachiska_inventory(con, day):
    warnings = []
    snapshot = _monthly_historical_snapshot(
        con, date.fromisoformat(day), warnings, inventory_only=True)
    if warnings:
        raise ValueError('Omborda sanasi noaniq yozuvlar bor. Zachiskadan oldin tekshiring.')
    return rop.finished_counts(snapshot['stocks'], snapshot['bags'])


def production_form_values(form, *, editing=False):
    unit = 'kg' if editing else form.get('unit', 'tonna')
    if unit not in ('kg', 'tonna'):
        raise ValueError('O‘lchov birligi noto‘g‘ri.')
    note = form.get('note', '').strip()
    if len(note) > 500:
        raise ValueError('Izoh 500 belgidan oshmasin.')
    def quantity(field, label, multiplier=1):
        value = form.get(field)
        return amount(value if editing else value or 0, label, multiplier=multiplier)

    return {
        'wheat_kg': amount(form.get('cleaned_wheat'), 'Tozalangan bug‘doy', positive=True,
                           multiplier=1000 if unit == 'tonna' else 1),
        'bulk_kg': quantity('bulk_kg', 'Bunker uni') if editing else
                   quantity('bulk_flour_tons', 'Bunker uni', 1000),
        'black_kg': quantity('black_kg', 'Qora un'),
        'bran_kg': quantity('bran_kg', 'Kepak'),
        'muchka_kg': quantity('muchka_kg', 'Muchka'),
        'telashka_kg': quantity('telashka_kg', 'Telashka atxod'),
        'note': note,
    }


@app.route('/ishlab-chiqarish', methods=['GET', 'POST'])
@login_required
def production_page():
    user = current_user()
    if request.method == 'POST':
        if user['role'] != 'buxgalter':
            abort(403)
        try:
            action = request.form.get('action', 'rop_production')
            with closing(db()) as con, con:
                con.execute('BEGIN IMMEDIATE')
                token, payload, previous, claim = check_remaining_operation(con, action)
                if previous:
                    flash('Bu operatsiya avval saqlangan.', 'success')
                    return redirect(url_for('production_page'))
                period = rop.latest_period(con)
                state = claim['state']
                if state.get('period_id') != (period['id'] if period else None):
                    raise ValueError('ROP davri o‘zgargan. Formani qayta oching.')
                date_value = _html_date_storage(request.form.get('date', ''))
                day = _business_date_key(date_value)
                note = request.form.get('note', '').strip()
                if len(note) > 500:
                    raise ValueError('Izoh 500 belgidan oshmasin.')
                if action == 'rop_receive':
                    result_id = rop.receive(con, day, request.form.get('qty_kg'),
                                            request.form.get('source', 'direct'), note, user['id'],
                                            transfer_document=request.form.get('transfer_document'))
                    message = 'ROP kirimi saqlandi.'
                elif action == 'rop_production':
                    quantities = production_form_values(request.form)
                    period_id = rop.production_period(con, day)
                    result_id = save_production(con, date_value, quantities['wheat_kg'],
                                                quantities['bulk_kg'], quantities['black_kg'],
                                                quantities['bran_kg'], quantities['muchka_kg'],
                                                quantities['telashka_kg'], note, user['id'], period_id)
                    message = 'Kunlik tozalangan bug‘doy va ishlab chiqarish saqlandi.'
                elif action == 'rop_measure':
                    result_id = rop.measure(con, day, request.form.get('qty_kg'),
                                            request.form.get('document_ref'), user['id'])
                    message = 'Hujjatli fizik ROP sanoqi saqlandi.'
                elif action in ('rop_grain_waste', 'rop_grain_waste_edit'):
                    result_id = rop.save_grain_waste(
                        con, period, day, request.form, user['id'],
                        receipt_id=state['receipt_id'] if action.endswith('_edit') else None,
                        revision=state['revision'] if action.endswith('_edit') else None)
                    message = 'Зерно отход nakladnoyi saqlandi.'
                elif action == 'rop_approve':
                    if state.get('stop_day') != day:
                        raise ValueError('To‘xtash sanasini qayta tekshiring.')
                    revisions = [rop_forms.get(con, period['id'], number)[0] for number in (34,117)] if period else [0,0]
                    if state.get('form_revisions', [0,0]) != revisions:
                        raise ValueError('34 yoki 117-shakl o‘zgargan. Balansni qayta tekshiring.')
                    report_month = rop.report_month_start(
                        request.form.get('report_month'), day).isoformat()[:7]
                    if state.get('report_month', day[:7]) != report_month:
                        raise ValueError('Hisobot oyi o‘zgargan. Balansni qayta tekshiring.')
                    rop.validate_stop(con, period, day)
                    # BEGIN IMMEDIATE excludes writers while a separate reader copies
                    # the committed inventory as of the actual stop day.
                    with closing(db()) as reader:
                        expected = zachiska_inventory(reader, day)
                    result_id = rop.approve(con, period, day, request.form, expected, user['id'],
                                            report_month=report_month)
                    message = 'Yagona zachiska tasdiqlandi.'
                else:
                    raise ValueError('Amal noto‘g‘ri.')
                save_remaining_operation(con, action, token, payload, result_id)
            flash(message, 'success')
        except (ValueError, KeyError, OverflowError) as exc:
            flash(str(exc) or 'Ishlab chiqarish ma’lumotlarini tekshiring.', 'error')
        except sqlite3.Error:
            flash('Operatsiya saqlanmadi. Sana va ma’lumotlarni tekshiring.', 'error')
        stop_day = request.form.get('date') if request.form.get('action') == 'rop_approve' else None
        return redirect(url_for('production_page', stop_day=stop_day,
                                report_month=request.form.get('report_month') if stop_day else None))

    con=db()
    try:
        rows,pagination=paginate_rows(con,'''SELECT p.*, r.period_id AS rop_period_id
            FROM production p LEFT JOIN rop_production r ON r.production_id=p.id ORDER BY p.id DESC''',
                                      'SELECT COUNT(*) FROM production')
        total=con.execute('SELECT COALESCE(SUM(net),0) FROM wheat_receipts').fetchone()[0]
        used=rop.wheat_warehouse_used(con)
        bulk_available=bunker_balance(con)
        period = rop.latest_period(con)
        measurement = rop.latest_measurement(con, period)
        active = period is not None and period['stop_day'] is None
        totals = rop.period_totals(con, period) if active else None
        revision34, form34 = rop_forms.get(con, period['id'], 34) if active else (0,{})
        revision117, form117_doc = rop_forms.get(con, period['id'], 117) if active else (0,{})
        waste_receipts = con.execute('''SELECT * FROM rop_grain_waste WHERE period_id=?
            ORDER BY day DESC,id DESC''', (period['id'],)).fetchall() if active else []
        waste_kg = rop.grain_waste_summary(con, period['id'], date.today().isoformat())['kg'] if active else None
        receipts = con.execute('''SELECT r.*, u.username, d.document_ref FROM rop_receipts r
            JOIN users u ON u.id=r.user_id
            LEFT JOIN rop_transfer_documents d ON d.receipt_id=r.id
            ORDER BY r.id DESC LIMIT 30''').fetchall()
        approvals = con.execute('''SELECT p.*, u.username FROM rop_periods p
            JOIN users u ON u.id=p.approved_by ORDER BY p.id DESC''').fetchall()
        approved = [(row, json.loads(row['reconciliation'])) for row in approvals]
        stop_day = request.args.get('stop_day')
        report_month = request.args.get('report_month')
        finished = None
        if stop_day and active:
            try:
                stop_day = _business_date_key(_html_date_storage(stop_day))
                report_month = rop.report_month_start(report_month or None, stop_day).isoformat()[:7]
                rop.validate_stop(con, period, stop_day)
                finished = zachiska_inventory(con, stop_day)
            except (ValueError, sqlite3.Error) as exc:
                flash(str(exc), 'error')
        # Preserve historic bagging history from the old single-step process.
        ids=[r['id'] for r in rows]
        bag_lines={}
        if ids:
            placeholders=','.join('?' for _ in ids)
            for line in con.execute(
                'SELECT production_id,product,bag_kg,bag_count FROM bagged_production_lines '
                f'WHERE production_id IN ({placeholders}) ORDER BY product,bag_kg',ids):
                bag_lines.setdefault(line['production_id'],[]).append(line)
    finally:
        con.close()
    return render_template('production.html',rows=rows,pagination=pagination,
                           stock=total-used,bunker_kg=bulk_available,bag_lines=bag_lines,
                           period=period, active=active, measurement=measurement, totals=totals,
                           receipts=receipts, approved=approved, stop_day=stop_day, report_month=report_month,
                           finished=finished, loss_fields=rop.LOSS_FIELDS,
                           rop_adjustments=rop.ROP_ADJUSTMENTS,
                           waste_receipts=waste_receipts, waste_kg=waste_kg,
                           form34_saved=bool(form34), form117_saved=bool(form117_doc),
                           form_revisions=[revision34,revision117],
                           form117_outputs=rop.form117.OUTPUT_LABELS,
                           form117_corrections=rop.form117.CORRECTION_LABELS,
                           today=date.today().isoformat())

@app.get('/ishlab-chiqarish/zachiska/<int:period_id>/117')
@login_required
def zachiska_print(period_id):
    with closing(db()) as con:
        con.execute('PRAGMA query_only=ON')
        con.execute('BEGIN')
        period = con.execute('SELECT * FROM rop_periods WHERE id=?', (period_id,)).fetchone()
        if period is None:
            abort(404)
        try:
            draft = build_zachiska_draft(
                con, period, request.args.get('stop_day'), request.args.get('report_month'))
        except ValueError as exc:
            abort(400, description=str(exc))
    return render_template('zachiska_print.html', **draft), {'Cache-Control': 'private, no-store'}


@app.route('/ishlab-chiqarish/zachiska/<int:period_id>/shakl/<int:number>', methods=['GET','POST'])
@login_required
def rop_form_document(period_id, number):
    if number not in (34, 117):
        abort(404)
    user = current_user()
    if request.method == 'POST' and user['role'] != 'buxgalter':
        abort(403)
    output = request.args.get('output')
    with closing(db()) as con:
        if request.method == 'POST':
            con.execute('BEGIN IMMEDIATE')
        else:
            con.execute('PRAGMA query_only=ON')
            con.execute('BEGIN')
        period = con.execute('SELECT * FROM rop_periods WHERE id=?', (period_id,)).fetchone()
        if period is None:
            abort(404)
        if request.method == 'POST':
            try:
                rop_forms.save(con, period, number, int(request.form.get('revision','-1')), request.form, user['id'])
                con.commit()
            except ValueError as exc:
                con.rollback()
                flash(str(exc) or 'Hujjat qiymatlarini tekshiring.', 'error')
            else:
                flash(f'{number}-shakl saqlandi.', 'success')
            return redirect(url_for('rop_form_document', period_id=period_id, number=number))
        revision, values = rop_forms.get(con, period_id, number)
        closed = period['stop_day'] is not None
        saved = json.loads(period['reconciliation'] or '{}') if closed else {}
        _, quality34 = rop_forms.get(con, period_id, 34)
        cutoff = period['stop_day'] or date.today().isoformat()
        ledger_end = quality34.get('end_day', cutoff)
        ledger_start = quality34.get('start_day', period['start_day'])
        ledger = con.execute('''SELECT ROUND(SUM(qty_kg),3) FROM rop_grain_waste
            WHERE period_id=? AND day BETWEEN ? AND ?''',
            (period_id, ledger_start, ledger_end)).fetchone()[0]
        ledger_rows = con.execute('''SELECT day, document_ref, qty_kg FROM rop_grain_waste
            WHERE period_id=? AND day BETWEEN ? AND ? ORDER BY day,id''',
            (period_id, ledger_start, ledger_end)).fetchall()
        totals = saved if closed else rop.period_totals(con, period)
        measured = totals.get('cleaned') or None
    report = saved.get('form117_yield') if closed else None
    basis = quality34.get('clean_kg') if quality34 else measured
    issue = None
    if number == 117 and values and not closed:
        try:
            report = rop_forms.yield_report(values, basis)
        except ValueError as exc:
            issue = str(exc)
    if number == 117 and closed:
        values = {'report_month': saved.get('report_month') or '', **values}
    linked34 = rop_forms.document_status(34, quality34, ledger_kg=ledger) == 'TASDIQLANGAN'
    status = rop_forms.document_status(number, values, ledger_kg=ledger, report=report,
                                       linked34=linked34, closed=closed)
    headers = {'Cache-Control': 'private, no-store'}
    if output in ('print','download'):
        if number == 34:
            data = rop_form_exports.form34_context(values, period_id=period_id, ledger_kg=ledger, status=status)
            if output == 'print':
                return render_template('rop_form34_print.html', data=data, period=period,
                                       grain=rop_forms.GRAIN,waste=rop_forms.WASTE,
                                       waste_columns=rop_forms.WASTE_COLUMNS, signs=rop_forms.SIGNS[34]), headers
            stream = rop_form_exports.form34_download(data)
            extension = 'docx'
        else:
            book, display = rop_form_exports.form117_workbook(values,quality34,report,status,basis)
            if output == 'print':
                grid,widths = rop_form_exports.print_grid(book.active,display)
                return render_template('rop_form117_print.html', grid=grid,widths=widths,
                                       period=period,status=status,issue=issue), headers
            stream = rop_form_exports.workbook_download(book,display)
            extension = 'xlsx'
        response = send_file(stream, as_attachment=True, download_name=f'{number}-shakl-ROP-{period_id}.{extension}')
        response.headers.update(headers)
        return response
    return render_template('rop_form_edit.html', number=number, period=period, groups=rop_forms.fields(number),
                           values=values, revision=revision, status=status, issue=issue, measured=measured,
                           basis=basis, linked34=bool(quality34), ledger=ledger, ledger_rows=ledger_rows,
                           editable=not closed and user['role']=='buxgalter'), headers


@app.route('/ishlab-chiqarish/<int:production_id>/tahrir', methods=['GET', 'POST'])
@login_required
def edit_rop_production(production_id):
    user = current_user()
    if request.method == 'POST':
        try:
            with closing(db()) as con, con:
                con.execute('BEGIN IMMEDIATE')
                action = 'rop_production_edit'
                token, payload, previous, claim = check_remaining_operation(con, action)
                state = claim['state']
                if state.get('production_id') != production_id:
                    raise ValueError('Token boshqa ishlab chiqarish yozuviga tegishli.')
                if previous:
                    flash('Tahrir avval saqlangan.', 'success')
                else:
                    row = production_edit.record(con, production_id)
                    if row is None:
                        abort(404)
                    if state.get('revision') != row['revision']:
                        raise ValueError('Yozuv o‘zgargan. Joriy qiymatlarni qayta tekshiring.')
                    audit_id = production_edit.apply(
                        con, row, production_form_values(request.form, editing=True),
                        request.form.get('reason', '').strip(), user['id'])
                    save_remaining_operation(con, action, token, payload, audit_id)
                    flash('Ishlab chiqarish yozuvi tahrirlandi.', 'success')
        except ValueError as exc:
            flash(str(exc), 'error')
        except sqlite3.Error:
            flash('Tahrir saqlanmadi. Yozuvlar va sanalarni tekshiring.', 'error')
        return redirect(url_for('edit_rop_production', production_id=production_id))
    with closing(db()) as con:
        con.execute('BEGIN')
        row = production_edit.record(con, production_id)
        if row is None:
            abort(404)
        audit = con.execute('''SELECT e.*, u.username FROM rop_production_edits e
            JOIN users u ON u.id=e.user_id WHERE e.production_id=? ORDER BY e.id DESC''',
                            (production_id,)).fetchall()
    return render_template('production_edit.html', row=row,
                           can_edit=user['role'] == 'buxgalter' and row['stop_day'] is None,
                           changes=[(e, json.loads(e['previous_values']), json.loads(e['new_values']))
                                    for e in audit])


@app.context_processor
def packaging_context():
    def packaging_token():
        return URLSafeSerializer(app.secret_key,salt='flour-packaging-v1').dumps({
            'action':'packaging','user_id':session['user_id'], 'nonce':secrets.token_urlsafe(32)})
    return {'packaging_token':packaging_token}

@app.route('/un-qoplash', methods=['GET','POST'])
@login_required
def packaging_page():
    user=current_user()
    if user['role'] not in ('buxgalter','rahbar'): abort(403)
    if request.method=='POST':
        if user['role']!='buxgalter': abort(403)
        con=db()
        try:
            action=request.form.get('action','package')
            packaging_date=_html_date_storage(request.form.get('date') or date.today().isoformat())
            con.execute('BEGIN IMMEDIATE')
            if action=='opening':
                rop.check_packaging_day(con, _business_date_key(packaging_date), action)
                save_opening(con,amount(request.form.get('opening_tons'), 'Bunker qoldig‘i', multiplier=1000),
                             packaging_date,user['id'],request.form.get('note','').strip())
                message='Bunker boshlang‘ich qoldig‘i kiritildi.'
            elif action=='package':
                token=request.form.get('operation_token','')
                try:
                    token_data=URLSafeSerializer(app.secret_key,salt='flour-packaging-v1').loads(token)
                except BadSignature as exc:
                    raise ValueError('Qoplash tokeni yaroqsiz.') from exc
                if (not isinstance(token_data,dict) or token_data.get('action')!='packaging'
                        or token_data.get('user_id')!=user['id']):
                    raise ValueError('Qoplash tokeni boshqa foydalanuvchiga tegishli.')
                payload=json.dumps({k:request.form.getlist(k) for k in request.form
                    if k not in ('csrf_token','operation_token')},ensure_ascii=False,sort_keys=True,separators=(',',':'))
                previous = con.execute('SELECT 1 FROM flour_packaging WHERE token=?', (token,)).fetchone()
                if not previous:
                    rop.check_packaging_day(con, _business_date_key(packaging_date), action)
                _,created=save_packaging(con,packaging_date,
                    amount(request.form.get('withdrawn_tons'), 'Bunkerdan olingan un', multiplier=1000),
                    request.form.getlist('bag_product'),request.form.getlist('bag_kg'),
                    request.form.getlist('bag_count'),token,payload,user['id'],
                    request.form.get('note','').strip(), pending_reconciliation=True)
                message='Un qoplandi va tayyor omborga kirim qilindi.' if created else 'Qoplash avval saqlangan.'
            else:
                raise ValueError('Amal noto‘g‘ri.')
            con.commit()
            flash(message,'success')
            return redirect(url_for('packaging_page'))
        except (ValueError,KeyError,OverflowError,sqlite3.Error) as exc:
            con.rollback()
            flash(str(exc) or 'Qoplashni tekshiring.','error')
            return redirect(url_for('packaging_page'))
        finally:
            con.close()
    con=db()
    try:
        available=bunker_balance(con)
        opening=con.execute('SELECT * FROM bunker_openings LIMIT 1').fetchone()
        packaging,pagination=paginate_rows(con,'SELECT * FROM flour_packaging ORDER BY id DESC',
                                            'SELECT COUNT(*) FROM flour_packaging')
        period = rop.latest_period(con)
    finally:
        con.close()
    return render_template('packaging.html',bunker_kg=available,opening=opening,
                           packaging=packaging,bagged_products=BAGGED_FLOUR_PRODUCTS,
                           period=period, active=period is not None and period['stop_day'] is None,
                           pagination=pagination)

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
        customers=()  # DC_CUSTOMER_AUTOCOMPLETE_20261110: fetched on demand
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
    return render_invoices_page(
        INVOICE_GROUPS=INVOICE_GROUPS, db=db, paginate_rows=paginate_rows,
        render_template=render_template, request=request)

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
            # Source IDs come from the same calculated financial snapshot, not
            # from a similar-looking name or a guessed customer card.
            group = financial['customers'][(r['customer'], r['phone'])]
            legacy_ids = sorted({item['id'] for item in group['obligations']
                                 if item['kind'] == 'opening' and item['balance'] > 0})
            card_id = group['customer_id']
            item = dict(r, days_left=days_left,
                        edit_card_id=card_id,
                        legacy_opening_ids=legacy_ids)
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

@app.route('/qarzdorlar/eski-qarz/<int:debt_id>/tahrirlash', methods=['GET','POST'])
@login_required
def debtor_legacy_edit(debt_id):
    """Only immutable opening debt ID is edited; never amount or receipt rows."""
    from legacy_identity_service import legacy_fingerprint, legacy_identity_edit, Conflict
    user = current_user()
    if user['role'] not in ('buxgalter', 'rahbar'):
        abort(403)
    with closing(db()) as con:
        audit = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='opening_debt_identity_audit'").fetchone()
        if not audit:
            abort(503, description='Eski qarz tahriri audit jadvali hali tasdiqlanmagan.')
        debt = con.execute('SELECT * FROM opening_debts WHERE id=?', (debt_id,)).fetchone()
        if debt is None:
            abort(404)
        if resolve_customer_identity(customer_identity_context(con), debt['customer'], debt['customer_phone']) is not None:
            abort(409, description='Bu yozuv kartaga bog‘langan. Xaridor kartasini tahrirlang.')
        if request.method == 'GET':
            return render_template('debtor_identity_edit.html', edit_type='legacy', target=debt,
                                   digest=legacy_fingerprint(con, debt_id))
        if request.form.get('target_id') != str(debt_id):
            abort(400)
        try:
            con.execute('BEGIN IMMEDIATE')
            token, payload, previous, claim = check_remaining_operation(con, 'legacy_identity_edit')
            state = claim.get('state', {})
            if not isinstance(state, dict) or state.get('target_id') != debt_id:
                raise ValueError('Tahrir tokeni boshqa yozuvga tegishli.')
            if previous:
                con.rollback()
                return redirect(url_for('debtors_page'))
            audit_id = legacy_identity_edit(con, debt_id, user['id'], state.get('digest'),
                                            request.form.get('customer',''),
                                            request.form.get('customer_phone',''),
                                            confirm_existing_card=(request.form.get('confirm_existing_card') == 'yes'))
            save_remaining_operation(con, 'legacy_identity_edit', token, payload, audit_id)
            con.commit()
        except (Conflict, ValueError, sqlite3.Error) as exc:
            con.rollback()
            abort(409, description=str(exc))
    flash('Eski qarz ismi/telefoni tuzatildi; qarz va to‘lovlar o‘zgarmadi.', 'success')
    return redirect(url_for('debtors_page'))


@app.route('/qarzdorlar/xaridor/<int:customer_id>/tahrirlash', methods=['GET','POST'])
@login_required
def debtor_card_edit(customer_id):
    """Edit verified customer card without rewriting legacy sales or receipt rows."""
    from legacy_identity_service import card_fingerprint, card_identity_edit, Conflict
    user = current_user()
    if user['role'] not in ('buxgalter', 'rahbar'):
        abort(403)
    with closing(db()) as con:
        card = con.execute('SELECT * FROM customers WHERE id=?', (customer_id,)).fetchone()
        if card is None:
            abort(404)
        financial = calculate_financial_snapshot(con)
        if not any(g['customer_id'] == customer_id and g['debt'] > 0
                   for g in financial['customers'].values()):
            abort(409, description='Faqat faol qarzdor kartasi tahrirlanadi.')
        if request.method == 'GET':
            return render_template('debtor_identity_edit.html', edit_type='card', target=card,
                                   digest=card_fingerprint(con, customer_id))
        if request.form.get('target_id') != str(customer_id):
            abort(400)
        try:
            con.execute('BEGIN IMMEDIATE')
            token, payload, previous, claim = check_remaining_operation(con, 'card_identity_edit')
            state = claim.get('state', {})
            if not isinstance(state, dict) or state.get('target_id') != customer_id:
                raise ValueError('Tahrir tokeni boshqa kartaga tegishli.')
            if previous:
                con.rollback()
                return redirect(url_for('debtors_page'))
            audit_id = card_identity_edit(con, customer_id, user['id'], state.get('digest'),
                                         request.form.get('customer',''),
                                         request.form.get('customer_phone',''))
            save_remaining_operation(con, 'card_identity_edit', token, payload, audit_id)
            con.commit()
        except (Conflict, ValueError, sqlite3.Error) as exc:
            con.rollback()
            abort(409, description=str(exc))
    flash('Xaridor kartasi yangilandi; tarixiy to‘lovlar o‘zgarmadi.', 'success')
    return redirect(url_for('debtors_page'))


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
    return export_accountant_worker_payments_excel({
        'PAYMENT_METHODS': PAYMENT_METHODS,
        'abort': abort,
        'current_user': current_user,
        'customer_display': customer_display,
        'customer_identity_context': customer_identity_context,
        'db': db,
    })

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
        cleaning_input = '''WITH cleaning_input AS (
            SELECT p.id, p.date, p.wheat_kg AS cleaned_kg,
                   CASE WHEN r.production_id IS NULL THEN COALESCE(p.raw_wheat_kg,p.wheat_kg) END AS raw_kg
            FROM production p LEFT JOIN rop_production r ON r.production_id=p.id
        )'''
        cleaning,cleaning_pagination=paginate_rows(con,
            cleaning_input + ''' SELECT date, raw_kg, cleaned_kg, raw_kg-cleaned_kg AS loss_kg,
                (raw_kg-cleaned_kg)*100.0/NULLIF(raw_kg,0) AS loss_pct
                FROM cleaning_input ORDER BY business_date_key(date) DESC,id DESC''',
            'SELECT COUNT(*) FROM production',
        )
        raw, cleaned, loss = con.execute(cleaning_input + '''
            SELECT COALESCE(SUM(raw_kg),0), COALESCE(SUM(cleaned_kg),0),
                   COALESCE(SUM(raw_kg-cleaned_kg),0) FROM cleaning_input''').fetchone()
    return render_template('reports.html', sales=sales, expenses=expenses,
        cleaning=cleaning, raw_total=raw, cleaned_total=cleaned,
        loss_total=loss, loss_pct=(loss*100/raw if raw else 0),
        cleaning_pagination=cleaning_pagination)

@app.route('/omborlar/kirim/<int:receipt_id>/tahrirlash', methods=['GET','POST'])
@login_required
def warehouse_daily_receipt_edit(receipt_id):
    user=current_user()
    if user['role']!='buxgalter':
        abort(403)
    con=db()
    try:
        row=con.execute('SELECT * FROM daily_finished_stock_receipts WHERE id=?',
                        (receipt_id,)).fetchone()
        if row is None:
            abort(404)
        if request.method=='POST':
            try:
                token=request.form.get('operation_token','')
                try:
                    payload=URLSafeSerializer(app.secret_key,salt='dc65-receipt-edit-v1').loads(token)
                except BadSignature as exc:
                    raise ValueError('Tahrirlash formasi eskirgan. Qayta oching.') from exc
                if (not isinstance(payload,dict) or payload.get('action')!='edit_daily_receipt'
                        or payload.get('user_id')!=user['id'] or payload.get('receipt_id')!=receipt_id):
                    raise ValueError('Tahrirlash tokeni bu hisobga tegishli emas.')
                edit_daily_receipt(con,receipt_id,request.form,user['id'],token,
                                   payload.get('fingerprint',''))
                flash('Kunlik kirim audit bilan yangilandi. Ombor qoldig‘i qayta hisoblandi.', 'success')
                return redirect(url_for('warehouses'))
            except (ValueError,KeyError,OverflowError,sqlite3.Error) as exc:
                flash(str(exc) or 'Kirim tahririni tekshiring.', 'error')
                return redirect(url_for('warehouse_daily_receipt_edit',receipt_id=receipt_id))
        form_token=URLSafeSerializer(app.secret_key,salt='dc65-receipt-edit-v1').dumps({
            'action':'edit_daily_receipt','receipt_id':receipt_id,
            'user_id':user['id'],'fingerprint':receipt_fingerprint(row),
            'nonce':secrets.token_urlsafe(24)})
        return render_template('warehouse_receipt_edit.html',receipt=row,
                               form_token=form_token,
                               bagged=row['product'] in BAGGED_FLOUR_PRODUCTS)
    finally:
        con.close()

@app.route('/omborlar', methods=['GET','POST'])
@login_required
def warehouses():
    user = current_user()
    if request.method == 'POST':
        if user['role'] != 'buxgalter':
            abort(403)
        action = request.form.get('action')
        if action == 'finished_stock_adjustment':
            try:
                token=request.form.get('operation_token','')
                try:
                    verified=URLSafeSerializer(app.secret_key, salt='dc65-stock-adjustment-v1').loads(token)
                except BadSignature as exc:
                    raise ValueError('Tuzatish formasi eskirgan. Qayta oching.') from exc
                if (not isinstance(verified,dict) or verified.get('user_id')!=user['id']
                        or verified.get('action')!='finished_stock_adjustment'):
                    raise ValueError('Tuzatish tokeni ushbu buxgalterga tegishli emas.')
                con=db()
                try:
                    result=record_adjustment(con,request.form,user['id'],token)
                finally:
                    con.close()
                flash(f"{result['product']}: {result['qty_delta_kg']:+.2f} kg ombor tuzatishi saqlandi.", 'success')
            except (ValueError,KeyError,OverflowError,sqlite3.Error) as exc:
                flash(str(exc) or 'Tuzatish ma’lumotlarini tekshiring.', 'error')
            return redirect(url_for('warehouses'))
        if action == 'daily_finished_stock':
            try:
                token = request.form.get('operation_token','')
                try:
                    verified = URLSafeSerializer(app.secret_key, salt='daily-finished-stock-v1').loads(token)
                except BadSignature as exc:
                    raise ValueError('Kirim sahifasi eskirgan. Yangidan oching.') from exc
                if (not isinstance(verified,dict) or verified.get('user_id')!=user['id']
                        or verified.get('action')!='daily_finished_stock'):
                    raise ValueError('Kirim tokeni buxgalterga tegishli emas.')
                con = db()
                try:
                    recorded = record_daily_receipt(con, request.form, user['id'], token)
                finally:
                    con.close()
                flash(f"{recorded['product']} — {recorded['qty_kg']:.2f} kg kunlik kirim saqlandi.", 'success')
            except (ValueError, KeyError, OverflowError, sqlite3.Error) as exc:
                flash(str(exc) or 'Kunlik kirimni tekshiring.', 'error')
            return redirect(url_for('warehouses'))
        if action != 'opening_product_stock':
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
                # Prevent opening snapshot from swallowing or duplicating newer daily postings.
                if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='daily_finished_stock_receipts'").fetchone():
                    if con.execute('SELECT 1 FROM daily_finished_stock_receipts WHERE product=? LIMIT 1',
                        (product,)).fetchone():
                        raise ValueError('Bu mahsulot uchun kunlik kirim mavjud. Endi boshlang‘ich qoldiq kiritib bo‘lmaydi.')
                if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='finished_stock_adjustments'").fetchone():
                    if con.execute('SELECT 1 FROM finished_stock_adjustments WHERE product=? LIMIT 1',(product,)).fetchone():
                        raise ValueError('Bu mahsulot uchun ombor korrektirovkasi mavjud. Boshlang‘ich qoldiq endi kiritilmaydi.')
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
        consumed=rop.wheat_warehouse_used(con)
        period = rop.latest_period(con)
        rop_measurement = rop.latest_measurement(con, period)
        rop_totals = rop.period_totals(con, period) if period and not period['stop_day'] else None
        kg_openings=con.execute("""SELECT s.*,u.username AS entered_by_name
            FROM opening_product_stocks s JOIN users u ON u.id=s.entered_by
            ORDER BY business_date_key(s.stock_date),s.product""").fetchall()
        bag_openings=con.execute("""SELECT s.*,u.username AS entered_by_name
            FROM opening_bagged_product_stocks s JOIN users u ON u.id=s.entered_by
            ORDER BY business_date_key(s.stock_date),s.product""").fetchall()
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='daily_finished_stock_receipts'").fetchone():
            daily_entries, daily_pagination = paginate_rows(
                con,
                """SELECT r.id,r.receipt_date,r.product,r.qty_kg,r.bag_25_count,r.bag_50_count,
                    r.note,r.created_at,u.username AS entered_by_name
                    FROM daily_finished_stock_receipts r JOIN users u ON u.id=r.entered_by
                    ORDER BY business_date_key(r.receipt_date) DESC,r.id DESC""",
                'SELECT COUNT(*) FROM daily_finished_stock_receipts',
                page_param='daily_page',per_page_param='daily_per_page')
        else:
            daily_entries, daily_pagination = [], _pagination_state(0,'daily_page','daily_per_page')
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='finished_stock_adjustments'").fetchone():
            adjustments, adjustments_pagination = paginate_rows(
                con,
                """SELECT a.id,a.adjustment_date,a.product,a.qty_delta_kg,
                    a.bag_25_delta,a.bag_50_delta,a.reason,a.created_at,
                    u.username AS entered_by_name
                    FROM finished_stock_adjustments a JOIN users u ON u.id=a.entered_by
                    ORDER BY business_date_key(a.adjustment_date) DESC,a.id DESC""",
                'SELECT COUNT(*) FROM finished_stock_adjustments',
                page_param='adj_page',per_page_param='adj_per_page')
        else:
            adjustments, adjustments_pagination = [], _pagination_state(0,'adj_page','adj_per_page')
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
                           rop_measurement=rop_measurement, rop_totals=rop_totals,
                           opening_products=opening_products,
                           opening_bagged_products=BAGGED_FLOUR_PRODUCTS,
                           stock_openings=stock_openings,
                           today=date.today().isoformat(),
                           products=PRODUCTS, daily_entries=daily_entries,
                           daily_pagination=daily_pagination,
                           stock_adjustments=adjustments,
                           adjustments_pagination=adjustments_pagination,
                           adjustment_token=URLSafeSerializer(app.secret_key, salt='dc65-stock-adjustment-v1').dumps({
                               'action':'finished_stock_adjustment','user_id':user['id'],
                               'nonce':secrets.token_urlsafe(24)}) if user['role']=='buxgalter' else '',
                           daily_token=URLSafeSerializer(app.secret_key, salt='daily-finished-stock-v1').dumps({
                               'action':'daily_finished_stock','user_id':user['id'],
                               'nonce':secrets.token_urlsafe(24)}) if user['role']=='buxgalter' else '')

@app.route('/hisobot/yagona-excel')
@login_required
def unified_excel_report():
    return export_unified_excel_report({
        'BASE_DIR': BASE_DIR,
        'Decimal': Decimal,
        'Path': Path,
        'ROLES': ROLES,
        'abort': abort,
        'calculate_financial_snapshot': calculate_financial_snapshot,
        'customer_display': customer_display,
        'customer_identity_context': customer_identity_context,
        'date': date,
        'db': db,
        'request': request,
        'resolve_customer_identity': resolve_customer_identity,
        'resolve_customer_name': resolve_customer_name,
        'sqlite3': sqlite3,
    })

@app.route('/hisobot/excel')
@login_required
def excel_report():
    return export_legacy_excel_report({
        'BAGGED_FLOUR_PRODUCTS': BAGGED_FLOUR_PRODUCTS,
        'BASE_DIR': BASE_DIR,
        'Path': Path,
        'calculate_financial_snapshot': calculate_financial_snapshot,
        'customer_display': customer_display,
        'customer_identity_context': customer_identity_context,
        'db': db,
        'debt_customers': debt_customers,
        'expense_display': expense_display,
        'json': json,
        'product_stocks': product_stocks,
        're': re,
        'request': request,
    })



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
    flash('Sotuv bekor qilindi. Ombor hisobida sotuv chiqimi tiklandi. Nakladnoy raqami arxivda saqlandi.','success')
    return redirect(url_for('sales_page'))

@app.route('/xaridor/<path:customer>')
@login_required
def customer_statement(customer):
    report = build_customer_statement(
        customer, db=db, calculate_financial_snapshot=calculate_financial_snapshot,
        customer_identity_context=customer_identity_context,
        resolve_customer_name=resolve_customer_name,
        resolve_customer_identity=resolve_customer_identity,
        customer_display=customer_display, _business_date=_business_date)
    return render_template('customer_statement.html', **report)


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
