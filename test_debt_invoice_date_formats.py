import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

os.environ["SECRET_KEY"] = "isolated-debt-date-test-key"

ALLOWED_DATABASE_ROOTS = set()


def restrict_sqlite_to_temporary_databases(event, args):
    if event == "sqlite3.connect":
        database = args[0]
        if isinstance(database, str) and database.startswith("file:"):
            uri = urlsplit(database)
            if uri.netloc not in ("", "localhost"):
                raise RuntimeError("SQLite database URI must refer to a local test file.")
            database = url2pathname(unquote(uri.path))
            if (
                os.name == "nt"
                and len(database) >= 3
                and database[0] in ("\\", "/")
                and database[1].isalpha()
                and database[2] == ":"
            ):
                database = database[1:]
        path = Path(database).resolve()
        if not any(path.is_relative_to(root) for root in ALLOWED_DATABASE_ROOTS):
            raise RuntimeError(f"SQLite access outside temporary test databases blocked: {path}")


sys.addaudithook(restrict_sqlite_to_temporary_databases)

import app as app_module
from date_conversions import dmy_to_date, iso_to_date


class DebtAndInvoiceDateTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="debt-invoice-date-", dir=str(Path(__file__).resolve().parent)
        )
        self.root = Path(self.temp_dir.name).resolve()
        ALLOWED_DATABASE_ROOTS.add(self.root)
        self.connections = []
        self.original_db = app_module.db
        self.original_db_path = app_module.DB_PATH

        def tracked_db():
            connection = self.original_db()
            self.connections.append(connection)
            return connection

        app_module.db = tracked_db

    def tearDown(self):
        for connection in self.connections:
            connection.close()
        app_module.db = self.original_db
        app_module.DB_PATH = self.original_db_path
        ALLOWED_DATABASE_ROOTS.discard(self.root)
        self.temp_dir.cleanup()

    def new_database(self, date_format):
        app_module.DB_PATH = str(self.root / f"{date_format}.sqlite")
        app_module.init_db()
        with app_module.db() as connection:
            connection.execute(
                "INSERT INTO users(id,username,password_hash,role) "
                "VALUES(1,'accountant','unused','buxgalter')"
            )
        client = app_module.app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = 1
            session["role"] = "buxgalter"
            session["auth_version"] = 0
        return client

    @staticmethod
    def fmt(day, date_format):
        if date_format == "iso":
            return day.isoformat()
        return day.strftime("%d-%m-%Y")

    def add_sale(self, connection, customer, sale_day, due_day, date_format):
        return connection.execute(
            """INSERT INTO sales
               (date,customer,customer_phone,due_date,product,qty_kg,price_per_kg,
                vat_rate,paid,user_id)
               VALUES(?,?,?,?,?,10,10,0,0,1)""",
            (
                self.fmt(sale_day, date_format),
                customer,
                "998901234567",
                self.fmt(due_day, date_format),
                "Sara",
            ),
        ).lastrowid

    def test_due_date_addition_handles_rollover_and_leap_years_in_both_formats(self):
        cases = (
            (date(2026, 12, 20), date(2027, 1, 14)),
            (date(2024, 2, 10), date(2024, 3, 6)),
            (date(2024, 2, 29), date(2024, 3, 25)),
        )
        for date_format in ("iso", "dmy"):
            for start, expected in cases:
                with self.subTest(format=date_format, start=start):
                    actual = app_module._business_date_after(
                        self.fmt(start, date_format), 25
                    )
                    expected_text = self.fmt(expected, date_format)
                    self.assertEqual(actual, expected_text)
                    parser = iso_to_date if date_format == "iso" else dmy_to_date
                    self.assertEqual(parser(actual), expected)

    def test_invoice_month_key_and_numbering_match_for_iso_and_dmy(self):
        for date_format in ("iso", "dmy"):
            with self.subTest(format=date_format):
                self.new_database(date_format)
                with app_module.db() as connection:
                    results = []
                    for index, day in enumerate((
                        date(2026, 12, 31),
                        date(2027, 1, 1),
                        date(2026, 12, 30),
                    )):
                        sale_date = self.fmt(day, date_format)
                        sale_id = self.add_sale(
                            connection, f"Customer {index}", day, day, date_format
                        )
                        invoice_id = app_module.ensure_invoice(
                            connection, sale_id, "Sara", sale_date
                        )
                        row = connection.execute(
                            "SELECT month_key,number,issued_date FROM invoices WHERE id=?",
                            (invoice_id,),
                        ).fetchone()
                        results.append((row["month_key"], row["number"], row["issued_date"]))
                    self.assertEqual(
                        [(item[0], item[1]) for item in results],
                        [("2026-12", 1), ("2027-01", 1), ("2026-12", 2)],
                    )

    def test_blank_due_date_backfill_preserves_iso_and_dmy_format(self):
        for date_format in ("iso", "dmy"):
            with self.subTest(format=date_format):
                self.new_database(date_format)
                sale_day = date(2026, 12, 20)
                expected_due = date(2027, 1, 14)
                with app_module.db() as connection:
                    sale_id = self.add_sale(
                        connection, "Customer", sale_day, sale_day, date_format
                    )
                    connection.execute(
                        "UPDATE sales SET due_date='' WHERE id=?", (sale_id,)
                    )
                app_module.init_db()
                with app_module.db() as connection:
                    due = connection.execute(
                        "SELECT due_date FROM sales WHERE id=?", (sale_id,)
                    ).fetchone()["due_date"]
                parser = iso_to_date if date_format == "iso" else dmy_to_date
                self.assertEqual(parser(due), expected_due)

    def test_oldest_first_debt_details_use_calendar_order(self):
        for date_format in ("iso", "dmy"):
            with self.subTest(format=date_format):
                self.new_database(date_format)
                with app_module.db() as connection:
                    january_id = self.add_sale(
                        connection, "Customer", date(2027, 1, 1),
                        date(2027, 1, 26), date_format,
                    )
                    december_id = self.add_sale(
                        connection, "Customer", date(2026, 12, 31),
                        date(2027, 1, 25), date_format,
                    )
                    connection.execute(
                        "INSERT INTO payments(date,customer,amount) VALUES(?,?,?)",
                        (self.fmt(date(2027, 1, 2), date_format), "Customer", 100),
                    )
                    remaining = app_module.customer_debt_details(connection, "Customer")
                self.assertEqual(len(remaining), 1)
                self.assertEqual(remaining[0]["id"], january_id)
                self.assertNotEqual(remaining[0]["id"], december_id)

    def test_debtor_and_control_pages_classify_expired_today_and_future_dates(self):
        today = date.today()
        for date_format in ("iso", "dmy"):
            with self.subTest(format=date_format):
                client = self.new_database(date_format)
                with app_module.db() as connection:
                    for name, offset in (
                        ("Expired", -1),
                        ("DueToday", 0),
                        ("Upcoming", 1),
                    ):
                        due = today + timedelta(days=offset)
                        sale = due - timedelta(days=25)
                        self.add_sale(connection, name, sale, due, date_format)

                debtors = client.get("/qarzdorlar")
                self.assertEqual(debtors.status_code, 200)
                self.assertIn(b"1 kun kechikkan", debtors.data)
                self.assertIn(b"0 kun", debtors.data)
                self.assertIn(b"1 kun", debtors.data)

                control = client.get("/nazorat")
                self.assertEqual(control.status_code, 200)
                self.assertIn(b"Expired", control.data)
                self.assertIn(b"DueToday", control.data)
                self.assertIn(b"Upcoming", control.data)

    def test_invalid_calendar_dates_are_rejected_by_debt_and_invoice_paths(self):
        self.new_database("dmy")
        with app_module.db() as connection:
            sale_id = self.add_sale(
                connection, "Existing", date(2026, 12, 31), date(2027, 1, 25), "dmy"
            )
            app_module.ensure_invoice(connection, sale_id, "Sara", "31-12-2026")
            for invalid in ("2026-02-30", "30-02-2026", "2026/12/31", ""):
                with self.subTest(value=invalid):
                    with self.assertRaises(ValueError):
                        app_module.ensure_invoice(connection, sale_id, "Sara", invalid)
        with app_module.db() as connection:
            connection.execute(
                """INSERT INTO sales
                   (date,customer,customer_phone,due_date,product,qty_kg,price_per_kg,
                    vat_rate,paid,user_id)
                   VALUES('31-02-2026','Bad','998901234567','01-03-2026','Sara',10,10,0,0,1)"""
            )
        client = app_module.app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = 1
            session["role"] = "buxgalter"
            session["auth_version"] = 0
        for path in ("/qarzdorlar", "/nazorat"):
            with self.subTest(path=path):
                response = client.get(path)
                self.assertEqual(response.status_code, 400)
                self.assertIn(b"Qarz sanalarini tekshiring", response.data)

    def test_debtors_snapshot_accepts_mixed_stored_formats(self):
        client = self.new_database("dmy")
        with app_module.db() as connection:
            self.add_sale(
                connection, "DMY", date(2026, 12, 31), date(2027, 1, 25), "dmy"
            )
            self.add_sale(
                connection, "ISO", date(2026, 12, 30), date(2027, 1, 24), "iso"
            )
        response = client.get("/qarzdorlar")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Xaridor qarzlari".encode(), response.data)
        self.assertIn("200.00".encode(), response.data)


if __name__ == "__main__":
    unittest.main()
