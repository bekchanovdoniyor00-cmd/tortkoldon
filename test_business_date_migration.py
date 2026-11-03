import sqlite3
import unittest

from business_date_migration import migrate_business_dates
from finance_calculations import calculate_financial_snapshot


class BusinessDateMigrationTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE wheat_receipts (
                id INTEGER PRIMARY KEY, date TEXT NOT NULL, net REAL NOT NULL
            );
            CREATE TABLE production (
                id INTEGER PRIMARY KEY, date TEXT NOT NULL
            );
            CREATE TABLE sales (
                id INTEGER PRIMARY KEY, date TEXT NOT NULL, due_date TEXT NOT NULL,
                amount REAL NOT NULL, created_at TEXT NOT NULL, month_key TEXT NOT NULL,
                customer TEXT NOT NULL, customer_phone TEXT NOT NULL,
                qty_kg REAL NOT NULL, price_per_kg REAL NOT NULL, paid REAL NOT NULL,
                user_id INTEGER
            );
            CREATE TABLE invoices (
                id INTEGER PRIMARY KEY, issued_date TEXT NOT NULL,
                month_key TEXT NOT NULL, number INTEGER NOT NULL
            );
            CREATE TABLE worker_expense_operations (
                token TEXT PRIMARY KEY, expense_date TEXT NOT NULL, payload TEXT NOT NULL
            );
            CREATE TABLE opening_debts (
                id INTEGER PRIMARY KEY, customer TEXT NOT NULL, customer_phone TEXT NOT NULL,
                amount REAL NOT NULL, date TEXT NOT NULL, due_date TEXT NOT NULL
            );
            CREATE TABLE worker_receipts (
                id INTEGER PRIMARY KEY, sale_id INTEGER NOT NULL, worker_id INTEGER NOT NULL,
                amount REAL NOT NULL, date TEXT NOT NULL, payment_method TEXT NOT NULL
            );
            CREATE TABLE accountant_worker_receipts (
                id INTEGER PRIMARY KEY, receipt_id INTEGER NOT NULL, worker_id INTEGER NOT NULL,
                payment_method TEXT NOT NULL, amount REAL NOT NULL, payment_date TEXT NOT NULL
            );
            CREATE TABLE payments (
                id INTEGER PRIMARY KEY, date TEXT NOT NULL, customer TEXT NOT NULL, amount REAL NOT NULL
            );
            CREATE TABLE expenses (
                id INTEGER PRIMARY KEY, date TEXT NOT NULL, amount REAL NOT NULL
            );
            CREATE TABLE worker_sale_batches (
                id INTEGER PRIMARY KEY, sale_date TEXT NOT NULL
            );
            CREATE TABLE accountant_expense_operations (
                token TEXT PRIMARY KEY, expense_date TEXT NOT NULL
            );
            CREATE TABLE sale_cancellations (sale_id INTEGER PRIMARY KEY);
            INSERT INTO wheat_receipts VALUES (1, '2026-12-29', 1000);
            INSERT INTO production VALUES (1, '2026-12-28');
            INSERT INTO sales
                (id, date, due_date, amount, created_at, month_key, customer,
                 customer_phone, qty_kg, price_per_kg, paid, user_id)
            VALUES
                (1, '2026-12-31', '2027-01-25', 125.50, '2026-12-31T12:30:45', '2026-12',
                 'A', '998901234567', 1, 100, 20, 7),
                (2, '01-01-2027', '', 20.00, '2027-01-01T08:00:00', '2027-01',
                 'A', '998901234567', 1, 20, 0, 7);
            INSERT INTO invoices VALUES (1, '2026-12-31', '2026-12', 7);
            INSERT INTO opening_debts VALUES
                (1, 'A', '998901234567', 50, '2026-12-30', '2027-01-24');
            INSERT INTO worker_receipts VALUES (1, 1, 7, 10, '2026-12-20', 'naqd');
            INSERT INTO accountant_worker_receipts VALUES
                (1, 1, 7, 'naqd', 10, '2026-12-20');
            INSERT INTO payments VALUES (1, '2026-12-21', 'A', 5);
            INSERT INTO expenses VALUES (1, '2026-12-22', 4);
            INSERT INTO worker_sale_batches VALUES (1, '2026-12-23');
            INSERT INTO accountant_expense_operations VALUES
                ('expense-token', '2026-12-24');
            INSERT INTO worker_expense_operations VALUES
                ('opaque-token', '2026-12-31', '{"date":"2026-12-31"}');
            """
        )

    def tearDown(self):
        self.connection.close()

    def test_converts_idempotently_without_changing_other_data(self):
        before_financials = calculate_financial_snapshot(self.connection)["totals"]
        before_total = self.connection.execute(
            "SELECT SUM(amount) FROM sales"
        ).fetchone()[0]
        before_ids = {
            table: tuple(row[0] for row in self.connection.execute(f"SELECT id FROM {table} ORDER BY id"))
            for table in ("wheat_receipts", "production", "sales", "invoices",
                          "opening_debts", "worker_receipts",
                          "accountant_worker_receipts", "payments", "expenses",
                          "worker_sale_batches")
        }
        first = migrate_business_dates(self.connection)
        second = migrate_business_dates(self.connection)
        after_financials = calculate_financial_snapshot(self.connection)["totals"]
        after_ids = {
            table: tuple(row[0] for row in self.connection.execute(f"SELECT id FROM {table} ORDER BY id"))
            for table in before_ids
        }

        self.assertEqual(first["converted"], 14)
        self.assertEqual(second["converted"], 0)
        self.assertEqual(after_financials, before_financials)
        self.assertEqual(after_ids, before_ids)
        self.assertEqual(
            self.connection.execute("SELECT date FROM wheat_receipts").fetchone()[0],
            "29-12-2026",
        )
        self.assertEqual(
            self.connection.execute("SELECT date FROM production").fetchone()[0],
            "28-12-2026",
        )
        self.assertEqual(
            [tuple(row) for row in self.connection.execute(
                "SELECT date, due_date, amount, created_at, month_key FROM sales ORDER BY id"
            ).fetchall()],
            [
                ("31-12-2026", "25-01-2027", 125.5, "2026-12-31T12:30:45", "2026-12"),
                ("01-01-2027", "", 20.0, "2027-01-01T08:00:00", "2027-01"),
            ],
        )
        self.assertEqual(
            tuple(self.connection.execute(
                "SELECT issued_date, month_key, number FROM invoices"
            ).fetchone()),
            ("31-12-2026", "2026-12", 7),
        )
        self.assertEqual(
            tuple(self.connection.execute(
                "SELECT token, expense_date, payload FROM worker_expense_operations"
            ).fetchone()),
            ("opaque-token", "31-12-2026", '{"date":"2026-12-31"}'),
        )
        self.assertEqual(
            tuple(self.connection.execute(
                "SELECT date, due_date FROM opening_debts"
            ).fetchone()),
            ("30-12-2026", "24-01-2027"),
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT date FROM worker_receipts"
            ).fetchone()[0],
            "20-12-2026",
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT payment_date FROM accountant_worker_receipts"
            ).fetchone()[0],
            "20-12-2026",
        )
        self.assertEqual(
            self.connection.execute("SELECT date FROM payments").fetchone()[0],
            "21-12-2026",
        )
        self.assertEqual(
            self.connection.execute("SELECT date FROM expenses").fetchone()[0],
            "22-12-2026",
        )
        self.assertEqual(
            self.connection.execute("SELECT sale_date FROM worker_sale_batches").fetchone()[0],
            "23-12-2026",
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT expense_date FROM accountant_expense_operations"
            ).fetchone()[0],
            "24-12-2026",
        )
        self.assertEqual(
            self.connection.execute("SELECT SUM(amount) FROM sales").fetchone()[0],
            before_total,
        )

    def test_invalid_date_rolls_back_every_prior_conversion(self):
        self.connection.execute(
            """INSERT INTO sales
               (id,date,due_date,amount,created_at,month_key,customer,customer_phone,
                qty_kg,price_per_kg,paid)
               VALUES (3,'2026-02-30','',5,'unchanged','2026-02','A','998901234567',1,5,0)"""
        )
        with self.assertRaises(ValueError):
            migrate_business_dates(self.connection)
        self.assertEqual(
            self.connection.execute("SELECT date FROM wheat_receipts").fetchone()[0],
            "2026-12-29",
        )
        self.assertEqual(
            self.connection.execute("SELECT date FROM production").fetchone()[0],
            "2026-12-28",
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT date FROM sales WHERE id=1"
            ).fetchone()[0],
            "2026-12-31",
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT issued_date FROM invoices WHERE id=1"
            ).fetchone()[0],
            "2026-12-31",
        )


if __name__ == "__main__":
    unittest.main()
