import os
import pathlib
import re
import sqlite3
import sys
import tempfile
import unittest

os.environ["SECRET_KEY"] = "isolated-wheat-date-test-key"

import app as app_module


PROJECT_ROOT = pathlib.Path(__file__).resolve().parent
ALLOWED_DATABASE_ROOTS = set()


def restrict_sqlite_to_temporary_databases(event, args):
    if event == "sqlite3.connect":
        path = pathlib.Path(args[0]).resolve()
        if not any(path.is_relative_to(root) for root in ALLOWED_DATABASE_ROOTS):
            raise RuntimeError(f"SQLite access outside temporary test databases blocked: {path}")


sys.addaudithook(restrict_sqlite_to_temporary_databases)


class WheatDateValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="wheat-date-test-", dir=str(PROJECT_ROOT)
        )
        self.database_root = pathlib.Path(self.temp_dir.name).resolve()
        ALLOWED_DATABASE_ROOTS.add(self.database_root)
        self.connections = []
        self.original_db = app_module.db
        self.original_db_path = app_module.DB_PATH

        def tracked_db():
            connection = self.original_db()
            self.connections.append(connection)
            return connection

        app_module.db = tracked_db
        app_module.DB_PATH = str(self.database_root / "wheat-test.sqlite")
        app_module.init_db()
        self.connection = app_module.db()
        self.connection.execute(
            "INSERT INTO users(id,username,password_hash,role) VALUES(1,'accountant','unused','buxgalter')"
        )
        self.connection.execute(
            "INSERT INTO users(id,username,password_hash,role) VALUES(2,'worker','unused','umid')"
        )
        self.connection.commit()
        self.client = app_module.app.test_client()
        with self.client.session_transaction() as session:
            session["user_id"] = 1
            session["role"] = "buxgalter"
            session["auth_version"] = 0

    def tearDown(self):
        for connection in self.connections:
            connection.close()
        app_module.db = self.original_db
        app_module.DB_PATH = self.original_db_path
        ALLOWED_DATABASE_ROOTS.discard(self.database_root)
        self.temp_dir.cleanup()

    def fund_accountant(self, channel, amount):
        today = app_module._business_date_storage(app_module.date.today().isoformat())
        due_date = app_module._business_date_after(today, 25)
        self.connection.execute(
            """INSERT INTO sales
               (date,customer,product,qty_kg,price_per_kg,vat_rate,paid,note,
                user_id,customer_phone,due_date,payment_method,initial_payment_recipient_id)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (today, "Sinov xaridor", "Kepak", 1, amount, 0, amount,
             "Sinov uchun kassa mablag‘i", 2, "", due_date, channel, 1),
        )
        self.connection.commit()

    def form_data(self, date_value):
        response = self.client.get("/bugdoy")
        self.assertEqual(response.status_code, 200)
        purchase_form = re.search(
            rb'<form\b(?:(?!</form>).)*?name="purchase_method"(?:(?!</form>).)*</form>',
            response.data,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(purchase_form)
        match = re.search(
            rb'name="operation_token" value="([^"]+)"', purchase_form.group(0)
        )
        self.assertIsNotNone(match)
        with self.client.session_transaction() as session:
            csrf_token = session["csrf_token"]
        return {
            "date": date_value,
            "purchase_method": "bank",
            "supplier": "Sinov yetkazuvchi",
            "unit": "kg",
            "gross": "1000",
            "tare": "50",
            "price_unit": "kg",
            "price": "2.5",
            "note": "Sana tekshiruvi",
            "operation_token": match.group(1).decode(),
            "csrf_token": csrf_token,
        }

    def post_purchase(self, data, **kwargs):
        return self.client.post("/bugdoy", data=data, **kwargs)

    def test_purchase_form_has_only_naqd_and_bank_through_birja(self):
        response = self.client.get("/bugdoy")
        self.assertEqual(response.status_code, 200)
        select = re.search(
            rb'<select name="purchase_method".*?</select>',
            response.data,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(select)
        methods = re.findall(rb'<option value="([^"]+)"', select.group(0))
        self.assertEqual(methods, [b"naqd", b"bank"])

    def assert_no_purchase_or_financial_changes(self):
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM wheat_receipts").fetchone()[0],
            0,
        )
        self.assertEqual(
            self.connection.execute("SELECT COALESCE(SUM(net),0) FROM wheat_receipts").fetchone()[0],
            0,
        )
        for table in (
            "remaining_write_operations",
            "wheat_purchase_payments",
            "expenses",
            "payments",
            "worker_receipts",
            "accountant_worker_receipts",
        ):
            self.assertEqual(
                self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                0,
                table,
            )

    def test_valid_date_saves_purchase_and_existing_units(self):
        self.fund_accountant("hisob", 5000)
        response = self.post_purchase(
            self.form_data("2026-10-29"), follow_redirects=True
        )
        self.assertEqual(response.status_code, 200)
        row = self.connection.execute(
            "SELECT date,gross,tare,net,cleaned_net,price_per_kg,purchase_method "
            "FROM wheat_receipts"
        ).fetchone()
        self.assertEqual(
            tuple(row),
            ("29-10-2026", 1000, 50, 950, 950, 2.5, "Bank orqali (birja)"),
        )
        self.assertIn("Bug‘doy xaridi to‘liq to‘landi".encode(), response.data)
        payment = self.connection.execute(
            "SELECT payment_method,amount FROM wheat_purchase_payments"
        ).fetchone()
        self.assertEqual(tuple(payment), ("hisob", 2375))
        from cashflow_analytics import calculate_cashflow_analytics
        cashflow = calculate_cashflow_analytics(self.connection)
        self.assertEqual(
            cashflow["accounts"]["buxgalter"]["closing_channels"]["hisob"],
            2625,
        )
        self.assertEqual(
            cashflow["accounts"]["buxgalter"]["closing_channels"]["naqd"],
            0,
        )
        self.assertEqual(cashflow["source_counts"]["wheat_purchase_payments"], 1)
        daily_cash = app_module.cash_receipt_totals(
            self.connection, app_module.date.today().isoformat(), "accountant")
        self.assertEqual(daily_cash["expenses"], 2375)
        self.assertEqual(daily_cash["balance"], 2625)
        daily_response = self.client.get(
            "/kunlik?date=" + app_module.date.today().isoformat())
        self.assertEqual(daily_response.status_code, 200)
        self.assertIn(b"Xarajatlar: 2375 so", daily_response.data)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM expenses").fetchone()[0],
            0,
        )

    def test_empty_date_defaults_to_today(self):
        self.fund_accountant("hisob", 5000)
        before = app_module.date.today().strftime("%d-%m-%Y")
        response = self.post_purchase(self.form_data(""), follow_redirects=True)
        after = app_module.date.today().strftime("%d-%m-%Y")
        self.assertEqual(response.status_code, 200)
        saved_date = self.connection.execute(
            "SELECT date FROM wheat_receipts"
        ).fetchone()[0]
        self.assertIn(saved_date, (before, after))

    def test_invalid_dates_are_rejected_without_writes(self):
        for invalid_date in (
            "abc",
            "2026-02-30",
            "2026-13-01",
            "32.13.2026",
            " 2026-10-29 ",
        ):
            with self.subTest(date=invalid_date):
                response = self.post_purchase(
                    self.form_data(invalid_date), follow_redirects=True
                )
                self.assertEqual(response.status_code, 200)
                self.assertIn(
                    "Xarid sanasi YYYY-MM-DD formatida haqiqiy sana bo‘lishi kerak.".encode(),
                    response.data,
                )
                self.assert_no_purchase_or_financial_changes()

    def test_repeated_token_does_not_duplicate_purchase(self):
        self.fund_accountant("hisob", 5000)
        data = self.form_data("2026-10-29")
        first = self.post_purchase(data)
        second = self.post_purchase(data, follow_redirects=True)
        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM wheat_receipts").fetchone()[0],
            1,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM remaining_write_operations WHERE action='wheat'"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM wheat_purchase_payments").fetchone()[0],
            1,
        )
        from cashflow_analytics import calculate_cashflow_analytics
        self.assertEqual(
            calculate_cashflow_analytics(self.connection)["accounts"]["buxgalter"]
            ["closing_channels"]["hisob"],
            2625,
        )

    def test_naqd_payment_debits_only_accountant_cash(self):
        self.fund_accountant("naqd", 5000)
        self.fund_accountant("hisob", 10000)
        data = self.form_data("2026-10-29")
        data["purchase_method"] = "naqd"
        response = self.post_purchase(data, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        from cashflow_analytics import calculate_cashflow_analytics
        cash = calculate_cashflow_analytics(self.connection)["accounts"]["buxgalter"]["closing_channels"]
        self.assertEqual(cash["naqd"], 2625)
        self.assertEqual(cash["hisob"], 10000)
        self.assertEqual(
            tuple(self.connection.execute(
                "SELECT payment_method,amount FROM wheat_purchase_payments"
            ).fetchone()),
            ("naqd", 2375),
        )

    def test_insufficient_selected_channel_rejects_entire_purchase(self):
        self.fund_accountant("naqd", 10000)
        data = self.form_data("2026-10-29")
        response = self.post_purchase(data, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Bank hisobi".encode(), response.data)
        self.assert_no_purchase_or_financial_changes()

    def test_legacy_exchange_method_is_rejected(self):
        self.fund_accountant("hisob", 5000)
        data = self.form_data("2026-10-29")
        data["purchase_method"] = "Birja"
        response = self.post_purchase(data, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Xarid turi, vazn va narxni tekshiring.".encode(), response.data)
        self.assert_no_purchase_or_financial_changes()

    def test_new_schema_preserves_old_wheat_purchase_rows(self):
        self.connection.execute(
            """INSERT INTO wheat_receipts
               (date,supplier,gross,tare,net,cleaned_net,price_per_kg,vat_rate,
                note,user_id,purchase_method)
               VALUES('29-10-2026','Old supplier',1000,0,1000,1000,1,0,'legacy',1,'Birja')"""
        )
        self.connection.execute("DROP TABLE wheat_purchase_payments")
        self.connection.commit()
        original_backup_guard = app_module.backup_before_internal_cash_transfer_migration
        app_module.backup_before_internal_cash_transfer_migration = lambda: None
        try:
            app_module.init_db()
        finally:
            app_module.backup_before_internal_cash_transfer_migration = original_backup_guard
        row = self.connection.execute(
            "SELECT purchase_method,net FROM wheat_receipts"
        ).fetchone()
        self.assertEqual(tuple(row), ("Birja", 1000))
        self.assertIsNotNone(
            self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='wheat_purchase_payments'"
            ).fetchone()
        )

    def test_csrf_and_role_protections_remain_active(self):
        data = self.form_data("2026-10-29")
        data.pop("csrf_token")
        self.assertEqual(self.post_purchase(data).status_code, 400)
        self.assert_no_purchase_or_financial_changes()
        with self.client.session_transaction() as session:
            session["user_id"] = 2
            session["role"] = "umid"
            session["auth_version"] = 0
        self.assertEqual(self.client.get("/bugdoy").status_code, 403)
        self.assert_no_purchase_or_financial_changes()

    def test_invalid_weight_or_price_still_rejects_purchase(self):
        for field, value in (("gross", "50"), ("price", "-1"), ("price", "0")):
            with self.subTest(field=field):
                data = self.form_data("2026-10-29")
                data[field] = value
                response = self.post_purchase(data, follow_redirects=True)
                self.assertEqual(response.status_code, 200)
                self.assertIn("Xarid turi, vazn va narxni tekshiring.".encode(), response.data)
                self.assert_no_purchase_or_financial_changes()


if __name__ == "__main__":
    unittest.main()
