import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from daily_sqlite_backup import load_config, main
from sqlite_backup import BackupError, create_backup, restore_backup, verify_backup
import sqlite_backup


def connect(path):
    connection = sqlite3.connect(path)
    connection.execute('PRAGMA foreign_keys=ON')
    return connection


def seed_database(path):
    with closing(connect(path)) as connection, connection:
        connection.executescript(
            '''
            CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT, role TEXT);
            CREATE TABLE customers(id INTEGER PRIMARY KEY, name TEXT, phone TEXT);
            CREATE TABLE sales(
                id INTEGER PRIMARY KEY, customer_id INTEGER, qty_kg REAL,
                price_per_kg REAL, paid REAL,
                FOREIGN KEY(customer_id) REFERENCES customers(id));
            CREATE TABLE payments(
                id INTEGER PRIMARY KEY, customer_id INTEGER, amount REAL,
                FOREIGN KEY(customer_id) REFERENCES customers(id));
            CREATE TABLE opening_debts(
                id INTEGER PRIMARY KEY, customer_id INTEGER, amount REAL,
                FOREIGN KEY(customer_id) REFERENCES customers(id));
            CREATE TABLE worker_receipts(
                id INTEGER PRIMARY KEY, sale_id INTEGER, amount REAL,
                FOREIGN KEY(sale_id) REFERENCES sales(id));
            CREATE TABLE accountant_worker_receipts(
                id INTEGER PRIMARY KEY, sale_id INTEGER, amount REAL,
                FOREIGN KEY(sale_id) REFERENCES sales(id));
            CREATE TABLE account_cash_transfers(
                id INTEGER PRIMARY KEY, amount REAL, status TEXT);
            CREATE TABLE wheat_receipts(id INTEGER PRIMARY KEY, date TEXT NOT NULL);
            CREATE TABLE wheat_purchase_payments(
                id INTEGER PRIMARY KEY, wheat_receipt_id INTEGER NOT NULL UNIQUE,
                accountant_id INTEGER NOT NULL,
                payment_method TEXT NOT NULL CHECK(payment_method IN ('naqd','hisob')),
                amount REAL NOT NULL CHECK(amount>0), payment_date TEXT NOT NULL,
                operation_token TEXT NOT NULL UNIQUE,
                FOREIGN KEY(wheat_receipt_id) REFERENCES wheat_receipts(id),
                FOREIGN KEY(accountant_id) REFERENCES users(id));
            CREATE TABLE accountant_cash_additions(
                id INTEGER PRIMARY KEY, date TEXT NOT NULL, amount REAL NOT NULL,
                payment_method TEXT NOT NULL, source TEXT NOT NULL,
                note TEXT NOT NULL, entered_by INTEGER NOT NULL,
                created_at TEXT NOT NULL, operation_token TEXT NOT NULL UNIQUE,
                payload TEXT NOT NULL,
                FOREIGN KEY(entered_by) REFERENCES users(id));
            CREATE TABLE production(
                id INTEGER PRIMARY KEY, wheat_kg REAL, flour_kg REAL, bran_kg REAL);
            CREATE TABLE production_flour_types(
                production_id INTEGER, product TEXT, qty_kg REAL,
                FOREIGN KEY(production_id) REFERENCES production(id));
            CREATE TABLE bagged_production_lines(
                production_id INTEGER, product TEXT, bag_kg INTEGER, bag_count INTEGER,
                FOREIGN KEY(production_id) REFERENCES production(id));
            CREATE TABLE opening_product_stocks(product TEXT PRIMARY KEY, qty_kg REAL);
            CREATE TABLE opening_bagged_product_stocks(
                product TEXT PRIMARY KEY, stock_date TEXT,
                bag_25_count INTEGER, bag_50_count INTEGER);
            INSERT INTO users VALUES
                (1, 'accountant_test', 'buxgalter'),
                (2, 'boss_test', 'rahbar'),
                (3, 'umid_test', 'umid'),
                (4, 'maxmud_test', 'maxmud');
            INSERT INTO accountant_cash_additions VALUES
                (1, '02-11-2026', 10000000, 'naqd', 'Owner contribution',
                 'backup test', 1, '2026-11-02 10:00:00',
                 'cash-add-backup-test', '{}');
            INSERT INTO customers VALUES (1, 'Asl xaridor', '998901112233');
            INSERT INTO sales VALUES
                (1, 1, 25, 10000, 100000),
                (2, 1, 50, 12000, 200000);
            INSERT INTO payments VALUES (1, 1, 50000);
            INSERT INTO opening_debts VALUES (1, 1, 70000);
            INSERT INTO worker_receipts VALUES (1, 1, 20000);
            INSERT INTO accountant_worker_receipts VALUES (1, 2, 30000);
            INSERT INTO account_cash_transfers VALUES (1, 50000, 'approved');
            INSERT INTO wheat_receipts VALUES (1, '02-11-2026');
            INSERT INTO wheat_purchase_payments VALUES
                (1, 1, 1, 'hisob', 12500, '2026-11-02', 'backup-test-token');
            INSERT INTO production VALUES (1, 1000, 700, 200);
            INSERT INTO production_flour_types VALUES (1, 'Sara', 400);
            INSERT INTO bagged_production_lines VALUES (1, 'Sara', 50, 8);
            INSERT INTO opening_product_stocks VALUES ('Kepak', 125);
            INSERT INTO opening_bagged_product_stocks
                VALUES ('Sara', '02-11-2026', 100, 50);
            '''
        )


def summary(path):
    connection = sqlite3.connect(path)
    try:
        return {
            'sales_count': connection.execute('SELECT COUNT(*) FROM sales').fetchone()[0],
            'sales_amount': connection.execute(
                'SELECT SUM(qty_kg * price_per_kg) FROM sales'
            ).fetchone()[0],
            'initial_paid': connection.execute('SELECT SUM(paid) FROM sales').fetchone()[0],
            'payments': connection.execute('SELECT SUM(amount) FROM payments').fetchone()[0],
            'opening_debt': connection.execute(
                'SELECT SUM(amount) FROM opening_debts'
            ).fetchone()[0],
            'worker_receipts': connection.execute(
                'SELECT SUM(amount) FROM worker_receipts'
            ).fetchone()[0],
            'accountant_receipts': connection.execute(
                'SELECT SUM(amount) FROM accountant_worker_receipts'
            ).fetchone()[0],
            'cash_transfers': connection.execute(
                'SELECT SUM(amount) FROM account_cash_transfers'
            ).fetchone()[0],
            'wheat_purchase_payments': connection.execute(
                'SELECT COUNT(*), SUM(amount), SUM(payment_method=\'hisob\') '
                'FROM wheat_purchase_payments'
            ).fetchone(),
            'cash_additions': connection.execute(
                'SELECT COUNT(*), SUM(amount), SUM(payment_method=\'naqd\') '
                'FROM accountant_cash_additions'
            ).fetchone(),
            'customer_debt': connection.execute(
                '''
                SELECT COALESCE(SUM(sales.total - sales.initial_paid
                    - sales.worker_paid - sales.accountant_paid
                    - COALESCE(payments.amount, 0)
                    + COALESCE(opening.amount, 0)), 0)
                FROM (
                    SELECT c.id,
                        COALESCE(SUM(s.qty_kg * s.price_per_kg), 0) AS total,
                        COALESCE(SUM(s.paid), 0) AS initial_paid,
                        COALESCE((SELECT SUM(w.amount) FROM worker_receipts w
                            JOIN sales sw ON sw.id=w.sale_id
                            WHERE sw.customer_id=c.id), 0) AS worker_paid,
                        COALESCE((SELECT SUM(a.amount) FROM accountant_worker_receipts a
                            JOIN sales sa ON sa.id=a.sale_id
                            WHERE sa.customer_id=c.id), 0) AS accountant_paid
                    FROM customers c LEFT JOIN sales s ON s.customer_id=c.id
                    GROUP BY c.id
                ) AS sales
                LEFT JOIN (
                    SELECT customer_id, SUM(amount) AS amount FROM payments GROUP BY customer_id
                ) AS payments ON payments.customer_id=sales.id
                LEFT JOIN (
                    SELECT customer_id, SUM(amount) AS amount
                    FROM opening_debts GROUP BY customer_id
                ) AS opening ON opening.customer_id=sales.id
                '''
            ).fetchone()[0],
            'production': connection.execute(
                'SELECT COUNT(*), SUM(wheat_kg), SUM(flour_kg), SUM(bran_kg) FROM production'
            ).fetchone(),
            'produced_flour': connection.execute(
                'SELECT SUM(qty_kg) FROM production_flour_types'
            ).fetchone()[0],
            'bagged_stock': connection.execute(
                'SELECT SUM(bag_kg * bag_count) FROM bagged_production_lines'
            ).fetchone()[0],
            'opening_stock': connection.execute(
                'SELECT SUM(qty_kg) FROM opening_product_stocks'
            ).fetchone()[0],
            'opening_bagged_stock': connection.execute(
                'SELECT SUM(bag_25_count), SUM(bag_50_count), '
                'SUM(25 * bag_25_count + 50 * bag_50_count) '
                'FROM opening_bagged_product_stocks'
            ).fetchone(),
            'users_and_roles': connection.execute(
                'SELECT username, role FROM users ORDER BY id'
            ).fetchall(),
            'customers': connection.execute(
                'SELECT id, name, phone FROM customers ORDER BY id'
            ).fetchall(),
        }
    finally:
        connection.close()


class SqliteBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix='sqlite_backup_test_')
        self.root = Path(self.temp_dir.name)
        self.source = self.root / 'live_test.sqlite'
        self.backup_dir = self.root / 'backups'
        seed_database(self.source)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_online_backup_verify_and_restore_preserve_erp_totals(self):
        before = summary(self.source)
        self.assertEqual(before['sales_count'], 2)
        self.assertEqual(before['sales_amount'], 850000)
        self.assertEqual(before['initial_paid'], 300000)
        self.assertEqual(before['payments'], 50000)
        self.assertEqual(before['opening_debt'], 70000)
        self.assertEqual(before['worker_receipts'], 20000)
        self.assertEqual(before['accountant_receipts'], 30000)
        self.assertEqual(before['cash_transfers'], 50000)
        self.assertEqual(before['wheat_purchase_payments'], (1, 12500, 1))
        self.assertEqual(before['cash_additions'], (1, 10000000, 1))
        self.assertEqual(before['customer_debt'], 520000)
        self.assertEqual(before['production'], (1, 1000, 700, 200))
        self.assertEqual(before['produced_flour'], 400)
        self.assertEqual(before['bagged_stock'], 400)
        self.assertEqual(before['opening_stock'], 125)
        self.assertEqual(before['opening_bagged_stock'], (100, 50, 5000))
        self.assertEqual(len(before['users_and_roles']), 4)
        backup = create_backup(self.source, self.backup_dir, retention_days=30)
        self.assertTrue(backup['path'].is_file())
        self.assertEqual(backup['integrity_check'], 'ok')
        self.assertEqual(verify_backup(backup['path'])['integrity_check'], 'ok')

        restored_path = self.root / 'restored' / 'restored_test.sqlite'
        restored = restore_backup(backup['path'], restored_path)
        self.assertEqual(restored['integrity_check'], 'ok')
        self.assertEqual(summary(backup['path']), before)
        self.assertEqual(summary(restored_path), before)

    def test_backup_is_a_consistent_snapshot_during_concurrent_write(self):
        with closing(connect(self.source)) as connection, connection:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('CREATE TABLE snapshot_probe(value INTEGER)')
            connection.execute('INSERT INTO snapshot_probe VALUES(1)')

        write_open = threading.Event()
        allow_commit = threading.Event()

        def uncommitted_writer():
            connection = connect(self.source)
            try:
                connection.execute('BEGIN IMMEDIATE')
                connection.execute('INSERT INTO snapshot_probe VALUES(2)')
                write_open.set()
                if not allow_commit.wait(timeout=10):
                    raise TimeoutError('Test writer was not released.')
                connection.commit()
            finally:
                connection.close()

        writer = threading.Thread(target=uncommitted_writer)
        writer.start()
        try:
            self.assertTrue(write_open.wait(timeout=10))
            backup = create_backup(self.source, self.backup_dir)
            connection = sqlite3.connect(backup['path'])
            try:
                values = connection.execute(
                    'SELECT value FROM snapshot_probe ORDER BY value'
                ).fetchall()
            finally:
                connection.close()
            self.assertEqual(values, [(1,)])
        finally:
            allow_commit.set()
            writer.join(timeout=10)
        self.assertFalse(writer.is_alive())

    def test_wal_backup_restore_preserves_committed_data_and_existing_backups(self):
        with closing(connect(self.source)) as connection, connection:
            self.assertEqual(
                connection.execute('PRAGMA journal_mode=WAL').fetchone()[0].lower(),
                'wal',
            )
            connection.execute('CREATE TABLE wal_probe(value TEXT NOT NULL)')
            connection.executemany(
                'INSERT INTO wal_probe(value) VALUES(?)',
                [('committed-one',), ('committed-two',)],
            )

        old_time = datetime.now(timezone.utc) - timedelta(days=40)
        old_backup = create_backup(
            self.source, self.backup_dir, retention_days=None, now=old_time
        )['path']
        backup = create_backup(
            self.source, self.backup_dir, retention_days=None
        )
        self.assertTrue(old_backup.is_file())
        self.assertEqual(verify_backup(backup['path'])['integrity_check'], 'ok')

        restored_path = self.root / 'wal_restore' / 'restored.sqlite'
        restored = restore_backup(backup['path'], restored_path)
        self.assertEqual(restored['integrity_check'], 'ok')
        for database_path in (backup['path'], restored_path):
            with closing(connect(database_path)) as connection:
                self.assertEqual(
                    connection.execute(
                        'SELECT value FROM wal_probe ORDER BY value'
                    ).fetchall(),
                    [('committed-one',), ('committed-two',)],
                )
                self.assertEqual(
                    connection.execute('PRAGMA integrity_check').fetchone()[0],
                    'ok',
                )

    def test_failed_or_corrupt_backup_is_never_reported_as_valid(self):
        with self.assertRaises(BackupError):
            create_backup(self.root / 'missing.sqlite', self.backup_dir)
        self.assertFalse(self.backup_dir.exists())

        corrupt = self.backup_dir / 'corrupt.sqlite'
        self.backup_dir.mkdir()
        corrupt.write_bytes(b'not a sqlite database')
        with self.assertRaises(BackupError):
            verify_backup(corrupt)

        invalid_output = self.root / 'failed_backups'

        def write_corrupt_partial(source, target):
            Path(target).write_bytes(b'incomplete sqlite backup')

        with mock.patch.object(sqlite_backup, '_copy_database', write_corrupt_partial):
            with self.assertRaises(BackupError):
                create_backup(self.source, invalid_output)
        self.assertEqual(list(invalid_output.iterdir()), [])

    def test_restore_refuses_to_overwrite_an_existing_destination(self):
        backup = create_backup(self.source, self.backup_dir)
        existing = self.root / 'keep.sqlite'
        existing.write_bytes(b'keep this file')
        with self.assertRaises(BackupError):
            restore_backup(backup['path'], existing)
        self.assertEqual(existing.read_bytes(), b'keep this file')

    def test_retention_removes_only_expired_managed_backup_files(self):
        old_stamp = (datetime.now(timezone.utc) - timedelta(days=40)).strftime(
            '%Y%m%d_%H%M%S_%f'
        )
        old_backup = self.backup_dir / f'tortkuldon_{old_stamp}_UTC.sqlite'
        unrelated = self.backup_dir / 'keep-me.sqlite'
        self.backup_dir.mkdir()
        old_backup.write_bytes(b'expired placeholder')
        unrelated.write_bytes(b'unmanaged placeholder')

        result = create_backup(self.source, self.backup_dir, retention_days=30)
        self.assertTrue(result['path'].is_file())
        self.assertFalse(old_backup.exists())
        self.assertTrue(unrelated.exists())

    def test_config_refuses_to_guess_a_database_and_cli_fails_clearly(self):
        config_path = self.root / 'backup_config.json'
        config_path.write_text(
            json.dumps({
                'source_database': '',
                'backup_directory': 'backups',
                'retention_days': 30,
                'daily_backup_time': '02:00',
            }),
            encoding='utf-8',
        )
        with self.assertRaises(BackupError):
            load_config(config_path)
        with mock.patch('sys.stderr'):
            self.assertEqual(main(['--config', str(config_path)]), 1)

    def test_configured_daily_runner_creates_a_verified_backup(self):
        config_path = self.root / 'backup_config.json'
        config_path.write_text(
            json.dumps({
                'source_database': str(self.source),
                'backup_directory': str(self.backup_dir),
                'retention_days': 30,
                'daily_backup_time': '02:00',
            }),
            encoding='utf-8',
        )
        with mock.patch('sys.stdout'):
            self.assertEqual(main(['--config', str(config_path)]), 0)
        backups = list(self.backup_dir.glob('tortkuldon_*.sqlite'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(verify_backup(backups[0])['integrity_check'], 'ok')


if __name__ == '__main__':
    unittest.main()
