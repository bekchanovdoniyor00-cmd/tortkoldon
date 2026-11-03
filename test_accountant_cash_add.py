import os
import pathlib
import re
import sqlite3
import sys
import tempfile
import unittest

os.environ["SECRET_KEY"] = "isolated-accountant-cash-add-test-key"

import app as app_module
from cashflow_analytics import calculate_cashflow_analytics


PROJECT_ROOT = pathlib.Path(__file__).resolve().parent
ALLOWED_DATABASE_ROOTS = set()


def restrict_sqlite_to_temporary_databases(event, args):
    if event == "sqlite3.connect":
        path = pathlib.Path(args[0]).resolve()
        if not any(path.is_relative_to(root) for root in ALLOWED_DATABASE_ROOTS):
            raise RuntimeError(f"SQLite access outside temporary test databases blocked: {path}")


sys.addaudithook(restrict_sqlite_to_temporary_databases)


class AccountantCashAdditionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="accountant-cash-add-test-", dir=str(PROJECT_ROOT)
        )
        self.database_root = pathlib.Path(self.temp_dir.name).resolve()
        ALLOWED_DATABASE_ROOTS.add(self.database_root)
        self.original_db = app_module.db
        self.original_db_path = app_module.DB_PATH
        self.connections = []

        def tracked_db():
            connection = self.original_db()
            self.connections.append(connection)
            return connection

        app_module.db = tracked_db
        app_module.DB_PATH = str(self.database_root / "accountant-cash-test.sqlite")
        app_module.init_db()
        self.connection = app_module.db()
        self.connection.executemany(
            "INSERT INTO users(id,username,password_hash,role) VALUES(?,?,?,?)",
            (
                (1, "accountant", "unused", "buxgalter"),
                (2, "manager", "unused", "rahbar"),
                (3, "worker", "unused", "umid"),
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
        ALLOWED_DATABASE_ROOTS.discard(self.database_root)
        self.temp_dir.cleanup()

    def form_data(self, channel, amount, source="Owner contribution", note="cash test"):
        response = self.client.get("/xodimlar/nazorat")
        self.assertEqual(response.status_code, 200)
        token = re.search(
            rb'name="operation_token" value="([^"]+)"[^>]*>\s*'
            rb'<input type="hidden" name="action" value="add_accountant_cash"',
            response.data,
        )
        self.assertIsNotNone(token)
        with self.client.session_transaction() as session:
            csrf_token = session["csrf_token"]
        return {
            "action": "add_accountant_cash",
            "operation_token": token.group(1).decode(),
            "csrf_token": csrf_token,
            "date": app_module.date.today().isoformat(),
            "amount": str(amount),
            "payment_method": channel,
            "source": source,
            "note": note,
        }

    def purchase_form(self, price):
        response = self.client.get("/bugdoy")
        self.assertEqual(response.status_code, 200)
        purchase_form = re.search(
            rb'<form\b(?:(?!</form>).)*?name="purchase_method"(?:(?!</form>).)*</form>',
            response.data,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(purchase_form)
        token = re.search(
            rb'name="operation_token" value="([^"]+)"', purchase_form.group(0)
        )
        self.assertIsNotNone(token)
        with self.client.session_transaction() as session:
            csrf_token = session["csrf_token"]
        return {
            "operation_token": token.group(1).decode(),
            "csrf_token": csrf_token,
            "date": app_module.date.today().isoformat(),
            "purchase_method": "naqd" if price == 3000 else "bank",
            "supplier": "Test supplier",
            "unit": "kg",
            "gross": "1000",
            "tare": "0",
            "price_unit": "kg",
            "price": str(price),
            "note": "cash integration test",
        }

    def add_cash(self, channel, amount):
        return self.client.post(
            "/xodimlar/nazorat",
            data=self.form_data(channel, amount),
            follow_redirects=True,
        )

    def buy_wheat(self, price):
        return self.client.post(
            "/bugdoy",
            data=self.purchase_form(price),
            follow_redirects=True,
        )

    def balances(self):
        return calculate_cashflow_analytics(self.connection)["accounts"]["buxgalter"][
            "closing_channels"
        ]

    def test_cash_add_then_wheat_purchase_debits_same_cash_ledgers_once(self):
        sales_before = calculate_cashflow_analytics(self.connection)["sales"]["amount"]

        cash_add = self.add_cash("naqd", 10_000_000)
        self.assertEqual(cash_add.status_code, 200)
        self.assertEqual(self.balances()["naqd"], 10_000_000)
        cash_purchase = self.buy_wheat(3000)
        self.assertEqual(cash_purchase.status_code, 200)
        self.assertEqual(self.balances()["naqd"], 7_000_000)

        bank_add = self.add_cash("hisob", 20_000_000)
        self.assertEqual(bank_add.status_code, 200)
        self.assertEqual(self.balances()["hisob"], 20_000_000)
        bank_purchase = self.buy_wheat(5000)
        self.assertEqual(bank_purchase.status_code, 200)
        self.assertEqual(self.balances()["hisob"], 15_000_000)

        report = calculate_cashflow_analytics(self.connection)
        self.assertEqual(report["sales"]["amount"], sales_before)
        self.assertEqual(report["source_counts"]["cash_additions"], 2)
        self.assertEqual(report["source_counts"]["wheat_purchase_payments"], 2)
        self.assertEqual(report["cash"]["period_cash_additions"], 30_000_000)
        self.assertEqual(
            tuple(self.connection.execute(
                "SELECT COUNT(*),SUM(amount) FROM accountant_cash_additions"
            ).fetchone()),
            (2, 30_000_000),
        )
        self.assertEqual(
            tuple(self.connection.execute(
                "SELECT COUNT(*),SUM(amount) FROM wheat_purchase_payments"
            ).fetchone()),
            (2, 8_000_000),
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM expenses").fetchone()[0],
            0,
        )

        with self.client.session_transaction() as session:
            session["user_id"] = 2
            session["role"] = "rahbar"
            session["auth_version"] = 0
        manager_page = self.client.get("/xodimlar/nazorat")
        self.assertEqual(manager_page.status_code, 200)
        self.assertIn("Owner contribution".encode(), manager_page.data)
        self.assertIn("7,000,000.00".encode(), manager_page.data)
        self.assertIn("15,000,000.00".encode(), manager_page.data)
        self.assertEqual(
            self.client.post(
                "/xodimlar/nazorat",
                data={"action": "add_accountant_cash"},
            ).status_code,
            403,
        )

    def test_repeated_cash_addition_token_is_idempotent(self):
        data = self.form_data("naqd", 10_000_000)
        first = self.client.post("/xodimlar/nazorat", data=data)
        second = self.client.post("/xodimlar/nazorat", data=data, follow_redirects=True)
        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(
            tuple(self.connection.execute(
                "SELECT COUNT(*),SUM(amount) FROM accountant_cash_additions"
            ).fetchone()),
            (1, 10_000_000),
        )
        self.assertEqual(self.balances()["naqd"], 10_000_000)

    def test_invalid_additions_do_not_change_cash(self):
        for values in (
            {"amount": "0"},
            {"amount": "-100"},
            {"amount": "10.001"},
            {"payment_method": "plastik"},
            {"source": ""},
        ):
            with self.subTest(values=values):
                data = self.form_data("naqd", 10_000_000)
                data.update(values)
                response = self.client.post(
                    "/xodimlar/nazorat", data=data, follow_redirects=True
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    self.connection.execute(
                        "SELECT COUNT(*) FROM accountant_cash_additions"
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(self.balances()["naqd"], 0)

    def test_accountant_cash_add_form_and_history_are_manager_readable(self):
        page = self.client.get("/xodimlar/nazorat")
        self.assertIn(b"Kassaga pul qo", page.data)
        self.add_cash("naqd", 10_000_000)
        with self.client.session_transaction() as session:
            session["user_id"] = 2
            session["role"] = "rahbar"
            session["auth_version"] = 0
        page = self.client.get("/xodimlar/nazorat")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn(b'name="action" value="add_accountant_cash"', page.data)
        self.assertIn(b"Owner contribution", page.data)


if __name__ == "__main__":
    unittest.main()
