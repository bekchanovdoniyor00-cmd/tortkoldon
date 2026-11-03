import os
import pathlib
import re
import tempfile
import unittest

os.environ["SECRET_KEY"] = "isolated-finance-report-test-key"

import app as app_module


class CombinedFinanceReportTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="finance-report-test-")
        self.original_db = app_module.db
        self.original_db_path = app_module.DB_PATH
        self.connections = []

        def tracked_db():
            connection = self.original_db()
            self.connections.append(connection)
            return connection

        app_module.db = tracked_db
        app_module.DB_PATH = str(pathlib.Path(self.temp_dir.name) / "finance-report.sqlite")
        app_module.init_db()
        self.connection = app_module.db()
        self.connection.execute(
            "INSERT INTO users(id,username,password_hash,role) VALUES(1,?,?,?)",
            ("accountant", "unused", "buxgalter"),
        )
        self.connection.execute(
            """INSERT INTO accountant_cash_additions
               (date,amount,payment_method,source,note,entered_by,operation_token,payload)
               VALUES(?,?,?,?,?,?,?,?)""",
            ("2026-11-02", 100_000, "naqd", "Test source", "", 1, "cash-add-test", "{}"),
        )
        receipts = (
            (1, "2026-11-02", 300, 0, 300, 100),
            (2, "2026-11-02", 300, 0, 300, 100),
        )
        for receipt_id, receipt_date, gross, tare, net, price in receipts:
            self.connection.execute(
                """INSERT INTO wheat_receipts
                   (id,date,supplier,gross,tare,net,price_per_kg,vat_rate,note,user_id)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (receipt_id, receipt_date, "Test supplier", gross, tare, net, price, 0, "", 1),
            )
        self.connection.executemany(
            """INSERT INTO wheat_purchase_payments
               (wheat_receipt_id,accountant_id,payment_method,amount,payment_date,operation_token)
               VALUES(?,?,?,?,?,?)""",
            (
                (1, 1, "naqd", 30_000, "2026-11-02", "wheat-cash-test"),
                (2, 1, "hisob", 30_000, "2026-11-02", "wheat-bank-test"),
            ),
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
        self.temp_dir.cleanup()

    def test_selected_day_details_do_not_duplicate_balance_or_profit(self):
        response = self.client.get("/moliya-hisobotlar?date=2026-11-02")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Kassaga qo‘shimcha mablag‘ kirimi", html)
        self.assertIn("Bug‘doy xaridi uchun to‘lovlar", html)
        self.assertIn("100,000 so‘m", html)
        self.assertIn("30,000 so‘m", html)
        self.assertIn("60,000 so‘m", html)
        self.assertIn("40,000 so‘m", html)
        self.assertRegex(
            html,
            r"<td>2026-11-02</td><td>100,000[^<]*</td><td>0[^<]*</td><td>100,000",
        )
        self.assertRegex(
            html,
            r"<td>2026-11-02</td><td>30,000[^<]*</td><td>30,000[^<]*</td><td>60,000",
        )
        self.assertIn("Tashqi mablag‘ kirimi sotuv tushumi yoki foyda emas", html)
        self.assertIn("alohida qo‘shimcha chiqim emas", html)

        totals = app_module.cash_receipt_totals(self.connection, "2026-11-02")
        self.assertEqual(totals["additions"], 100_000)
        self.assertEqual(totals["expenses"], 60_000)
        self.assertEqual(totals["balance"], 40_000)

        daily = self.client.get("/kunlik?date=2026-11-02")
        self.assertEqual(daily.status_code, 200)
        daily_html = daily.get_data(as_text=True)
        self.assertIn("100000 so", daily_html)
        self.assertIn("60000 so", daily_html)

        financial = self.client.get("/moliyaviy")
        self.assertEqual(financial.status_code, 200)
        financial_html = financial.get_data(as_text=True)
        self.assertIn("Jami sotuv summasi: 0 so", financial_html)
        self.assertIn("Jami bug", financial_html)
        self.assertIn("60000 so", financial_html)
        self.assertIn("Oddiy farq: -60000 so", financial_html)


if __name__ == "__main__":
    unittest.main()
