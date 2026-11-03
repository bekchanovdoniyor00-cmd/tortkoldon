import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from finance_calculations import calculate_financial_snapshot


SCHEMA = """
CREATE TABLE sales (
    id INTEGER PRIMARY KEY, date TEXT NOT NULL, customer TEXT NOT NULL,
    customer_phone TEXT NOT NULL, due_date TEXT NOT NULL,
    qty_kg REAL NOT NULL, price_per_kg REAL NOT NULL, paid REAL NOT NULL,
    user_id INTEGER
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
    id INTEGER PRIMARY KEY, date TEXT NOT NULL, customer TEXT NOT NULL,
    amount REAL NOT NULL
);
CREATE TABLE sale_cancellations (sale_id INTEGER PRIMARY KEY);
"""


def date_text(iso, format_name):
    parsed = date.fromisoformat(iso)
    if format_name == "iso":
        return parsed.isoformat()
    return parsed.strftime("%d-%m-%Y")


class FinanceDmyTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="finance-dmy-")
        self.connections = []

    def tearDown(self):
        for connection in self.connections:
            connection.close()
        self.temp_dir.cleanup()

    def new_connection(self, filename):
        path = Path(self.temp_dir.name) / filename
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        connection.executescript(SCHEMA)
        self.connections.append(connection)
        return connection

    def populate(self, format_name, filename):
        connection = self.new_connection(filename)
        sales = [
            (1, "2026-12-15", "2027-01-09", 20),
            (2, "2026-12-31", "2027-01-25", 0),
            (3, "2026-12-31", "2027-01-25", 0),
            (4, "2027-01-02", "2027-01-27", 0),
        ]
        connection.executemany(
            """INSERT INTO sales
               (id,date,customer,customer_phone,due_date,qty_kg,price_per_kg,paid,user_id)
               VALUES(?,?, 'A','998901234567',?,?,?,?,?)""",
            [
                (sid, date_text(sale_date, format_name),
                 date_text(due_date, format_name), 1, 100, paid, 7)
                for sid, sale_date, due_date, paid in sales
            ],
        )
        connection.executemany(
            """INSERT INTO opening_debts
               (id,customer,customer_phone,amount,date,due_date)
               VALUES(?, 'A','998901234567',?,?,?)""",
            [
                (1, 100, date_text("2026-12-31", format_name),
                 date_text("2027-01-30", format_name)),
                (2, 80, date_text("2026-12-31", format_name),
                 date_text("2027-01-30", format_name)),
            ],
        )
        connection.execute(
            """INSERT INTO worker_receipts
               (id,sale_id,worker_id,amount,date,payment_method)
               VALUES(1,1,7,30,?,'naqd')""",
            (date_text("2026-12-20", format_name),),
        )
        connection.execute(
            "INSERT INTO payments(id,date,customer,amount) VALUES(1,?,'A',400)",
            (date_text("2027-01-03", format_name),),
        )
        connection.commit()
        return connection

    @staticmethod
    def customer_result(connection):
        result = calculate_financial_snapshot(connection)
        customer = result["customers"][("A", "998901234567")]
        return result, customer

    def test_iso_and_dmy_financial_results_match_and_fifo_is_chronological(self):
        iso, iso_customer = self.customer_result(
            self.populate("iso", "iso.sqlite")
        )
        dmy, dmy_customer = self.customer_result(
            self.populate("dmy", "dmy.sqlite")
        )
        self.assertEqual(dmy["totals"], iso["totals"])
        self.assertEqual(dmy_customer["obligations"], iso_customer["obligations"])
        allocations = {
            (item["kind"], item["id"]):
                (item["legacy_applied"], item["balance"])
            for item in dmy_customer["obligations"]
        }
        self.assertEqual(
            allocations,
            {
                ("sale", 1): (50, 0),
                ("opening", 1): (100, 0),
                ("opening", 2): (80, 0),
                ("sale", 2): (100, 0),
                ("sale", 3): (70, 30),
                ("sale", 4): (0, 100),
            },
        )
        self.assertEqual(dmy["totals"]["debt"], 130)
        self.assertEqual(dmy["totals"]["legacy_received"], 400)

    def test_overpayment_rule_is_unchanged_for_iso_and_dmy(self):
        results = []
        for format_name in ("iso", "dmy"):
            connection = self.populate(format_name, f"overpay-{format_name}.sqlite")
            connection.execute("UPDATE payments SET amount=1000")
            connection.commit()
            results.append(calculate_financial_snapshot(connection))
        self.assertEqual(results[0]["totals"], results[1]["totals"])
        self.assertEqual(results[0]["totals"]["debt"], 0)
        self.assertEqual(results[0]["totals"]["excess"], 470)

    def test_leap_day_dates_parse_and_match_across_formats(self):
        results = []
        for format_name in ("iso", "dmy"):
            connection = self.new_connection(f"leap-{format_name}.sqlite")
            connection.execute(
                """INSERT INTO sales
                   (id,date,customer,customer_phone,due_date,qty_kg,price_per_kg,paid,user_id)
                   VALUES(1,?,'Leap','998901234567',?,1,100,0,7)""",
                (date_text("2024-02-29", format_name),
                 date_text("2024-03-25", format_name)),
            )
            connection.commit()
            results.append(calculate_financial_snapshot(connection))
        self.assertEqual(results[0]["totals"], results[1]["totals"])
        item = results[1]["customers"][("Leap", "998901234567")]["obligations"][0]
        self.assertEqual(item["date"], "2024-02-29")
        self.assertEqual(item["due_date"], "2024-03-25")

    def test_mixed_iso_and_dmy_dates_normalize_to_identical_financial_results(self):
        baseline, _ = self.customer_result(self.populate("dmy", "baseline.sqlite"))
        connection = self.populate("dmy", "mixed.sqlite")
        connection.execute(
            "UPDATE payments SET date='2027-01-03' WHERE id=1"
        )
        connection.execute(
            "UPDATE sales SET date='2026-12-31' WHERE id=2"
        )
        connection.commit()
        mixed = calculate_financial_snapshot(connection)
        self.assertEqual(mixed["totals"], baseline["totals"])
        self.assertEqual(
            mixed["customers"][("A", "998901234567")]["obligations"],
            baseline["customers"][("A", "998901234567")]["obligations"],
        )

    def test_invalid_calendar_date_is_rejected(self):
        connection = self.populate("dmy", "invalid.sqlite")
        connection.execute(
            "UPDATE sales SET date='31-02-2026' WHERE id=1"
        )
        connection.commit()
        with self.assertRaisesRegex(ValueError, "sales\\[1\\].date"):
            calculate_financial_snapshot(connection)

    def test_invalid_or_empty_due_date_is_not_guessed(self):
        for index, value in enumerate(("", "2027-02-30")):
            with self.subTest(value=value):
                connection = self.populate("dmy", f"bad-due-{index}.sqlite")
                connection.execute(
                    "UPDATE opening_debts SET due_date=? WHERE id=1", (value,)
                )
                connection.commit()
                with self.assertRaises(ValueError):
                    calculate_financial_snapshot(connection)


if __name__ == "__main__":
    unittest.main()
