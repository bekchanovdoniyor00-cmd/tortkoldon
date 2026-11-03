import os
import tempfile
import unittest
from pathlib import Path

_previous_secret_key = os.environ.get("SECRET_KEY")
os.environ["SECRET_KEY"] = "synthetic-date-test-secret"
import app as app_module
from app import (
    _business_date_display,
    _business_date_input,
    _business_date_key,
    _html_date_storage,
    app as flask_app,
    db,
)
if _previous_secret_key is None:
    os.environ.pop("SECRET_KEY", None)
else:
    os.environ["SECRET_KEY"] = _previous_secret_key


class BusinessDateAppIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="business-date-app-")
        self.original_db_path = app_module.DB_PATH
        app_module.DB_PATH = str(Path(self.temp_dir.name) / "synthetic.sqlite")

    def tearDown(self):
        app_module.DB_PATH = self.original_db_path
        if hasattr(self, "connection"):
            self.connection.close()
        self.temp_dir.cleanup()

    def test_form_storage_and_display_conversions(self):
        self.assertEqual(_html_date_storage("2026-10-29"), "29-10-2026")
        self.assertEqual(_business_date_input("29-10-2026"), "2026-10-29")
        self.assertEqual(_business_date_display("2026-10-29"), "29.10.2026")
        self.assertEqual(_business_date_display("29-10-2026"), "29.10.2026")
        self.assertEqual(_business_date_key("29-10-2026"), "2026-10-29")
        for invalid in ("abc", "2026-02-30", "29-02-2025"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    _html_date_storage(invalid)

    def test_sqlite_date_order_and_inclusive_range_for_mixed_values(self):
        self.connection = db()
        with self.connection as connection:
            connection.execute("CREATE TABLE sample_dates (id INTEGER, date TEXT)")
            connection.executemany(
                "INSERT INTO sample_dates VALUES (?, ?)",
                [
                    (1, "31-12-2026"),
                    (2, "2027-01-01"),
                    (3, "2026-12-30"),
                    (4, "2026-12-31"),
                ],
            )
            ordered = connection.execute(
                "SELECT id FROM sample_dates ORDER BY business_date_key(date), id"
            ).fetchall()
            bounded = connection.execute(
                """SELECT id FROM sample_dates
                   WHERE business_date_key(date)>=?
                     AND business_date_key(date)<=?
                   ORDER BY business_date_key(date), id""",
                ("2026-12-31", "2027-01-01"),
            ).fetchall()
        self.assertEqual([row[0] for row in ordered], [3, 1, 4, 2])
        self.assertEqual([row[0] for row in bounded], [1, 4, 2])

    def test_template_business_date_filter_renders_display_format(self):
        self.assertEqual(
            flask_app.jinja_env.filters["business_date"]("29-10-2026"),
            "29.10.2026",
        )
        self.assertEqual(
            flask_app.jinja_env.filters["business_date_input"]("29-10-2026"),
            "2026-10-29",
        )


if __name__ == "__main__":
    unittest.main()
