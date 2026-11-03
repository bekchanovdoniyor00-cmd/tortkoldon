import sqlite3
import tempfile
import unittest
from pathlib import Path

from date_sql import (
    date_equality_filter,
    date_filter,
    date_key_expression,
    date_order_by,
    mixed_date_key_expression,
    mixed_date_to_iso,
    query_date_rows,
    register_mixed_date_key_function,
    register_date_key_function,
    validate_mixed_date_column,
    validate_date_column,
)


class DateSqlTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="date-sql-test-")
        database_path = Path(self.temp_dir.name) / "temporary_dates.sqlite"
        self.connection = sqlite3.connect(database_path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            "CREATE TABLE date_samples (id INTEGER PRIMARY KEY, business_date TEXT NOT NULL)"
        )
        self.connection.executemany(
            "INSERT INTO date_samples(business_date) VALUES(?)",
            [
                ("01-01-2027",),
                ("31-12-2026",),
                ("15-12-2026",),
                ("30-12-2026",),
            ],
        )

    def tearDown(self):
        self.connection.close()
        self.temp_dir.cleanup()

    def test_order_by_is_chronological_across_year_boundary(self):
        rows = query_date_rows(
            self.connection, "date_samples", "business_date"
        )
        self.assertEqual(
            [row["business_date"] for row in rows],
            ["15-12-2026", "30-12-2026", "31-12-2026", "01-01-2027"],
        )

    def test_greater_than_or_equal_filter(self):
        where_sql, parameters = date_filter(
            "business_date", start_iso="2026-12-31"
        )
        register_date_key_function(self.connection)
        rows = self.connection.execute(
            f"SELECT business_date FROM date_samples WHERE {where_sql} "
            f"ORDER BY {date_order_by('business_date')}",
            parameters,
        ).fetchall()
        self.assertEqual(
            [row["business_date"] for row in rows],
            ["31-12-2026", "01-01-2027"],
        )

    def test_less_than_or_equal_filter(self):
        where_sql, parameters = date_filter(
            "business_date", end_iso="2026-12-30"
        )
        register_date_key_function(self.connection)
        rows = self.connection.execute(
            f"SELECT business_date FROM date_samples WHERE {where_sql} "
            f"ORDER BY {date_order_by('business_date')}",
            parameters,
        ).fetchall()
        self.assertEqual(
            [row["business_date"] for row in rows],
            ["15-12-2026", "30-12-2026"],
        )

    def test_range_includes_both_boundary_days(self):
        rows = query_date_rows(
            self.connection,
            "date_samples",
            "business_date",
            start_iso="2026-12-30",
            end_iso="2027-01-01",
        )
        self.assertEqual(
            [row["business_date"] for row in rows],
            ["30-12-2026", "31-12-2026", "01-01-2027"],
        )

    def test_equality_filter(self):
        where_sql, parameters = date_equality_filter(
            "business_date", "2026-12-31"
        )
        register_date_key_function(self.connection)
        rows = self.connection.execute(
            f"SELECT business_date FROM date_samples WHERE {where_sql}",
            parameters,
        ).fetchall()
        self.assertEqual([row["business_date"] for row in rows], ["31-12-2026"])

    def test_descending_order(self):
        rows = query_date_rows(
            self.connection, "date_samples", "business_date", order="DESC"
        )
        self.assertEqual(
            [row["business_date"] for row in rows],
            ["01-01-2027", "31-12-2026", "30-12-2026", "15-12-2026"],
        )

    def test_invalid_stored_iso_and_calendar_dates_are_rejected(self):
        for invalid_value in ("2026-12-31", "32-12-2026", ""):
            with self.subTest(value=invalid_value):
                self.connection.execute("DELETE FROM date_samples")
                self.connection.execute(
                    "INSERT INTO date_samples(business_date) VALUES(?)",
                    (invalid_value,),
                )
                with self.assertRaisesRegex(ValueError, "DD-MM-YYYY"):
                    query_date_rows(
                        self.connection, "date_samples", "business_date"
                    )

    def test_invalid_iso_filter_boundary_is_rejected(self):
        with self.assertRaises(ValueError):
            date_filter("business_date", start_iso="2026-02-30")

    def test_unsafe_sql_identifiers_are_rejected(self):
        with self.assertRaises(ValueError):
            date_key_expression('business_date); DROP TABLE date_samples; --')
        with self.assertRaises(ValueError):
            mixed_date_key_expression('business_date); DROP TABLE date_samples; --')
        with self.assertRaises(ValueError):
            validate_date_column(self.connection, "date_samples; DROP TABLE x", "business_date")
        with self.assertRaises(ValueError):
            date_order_by("business_date", "ASC; DROP TABLE x")

    def test_mixed_iso_and_dmy_values_sort_and_filter_chronologically(self):
        self.connection.execute("DELETE FROM date_samples")
        self.connection.executemany(
            "INSERT INTO date_samples(business_date) VALUES(?)",
            [
                ("01-01-2027",),
                ("2026-12-31",),
                ("31-12-2026",),
                ("2027-01-01",),
                ("2026-12-30",),
            ],
        )
        register_mixed_date_key_function(self.connection)
        validate_mixed_date_column(self.connection, "date_samples", "business_date")
        rows = self.connection.execute(
            f"SELECT business_date FROM date_samples "
            f"ORDER BY {mixed_date_key_expression('business_date')}, id"
        ).fetchall()
        self.assertEqual(
            [row["business_date"] for row in rows],
            ["2026-12-30", "2026-12-31", "31-12-2026", "01-01-2027", "2027-01-01"],
        )
        rows = self.connection.execute(
            f"SELECT business_date FROM date_samples "
            f"WHERE {mixed_date_key_expression('business_date')}>=? "
            f"AND {mixed_date_key_expression('business_date')}<=? "
            f"ORDER BY {mixed_date_key_expression('business_date')}, id",
            ("2026-12-31", "2027-01-01"),
        ).fetchall()
        self.assertEqual(len(rows), 4)
        self.assertEqual(mixed_date_to_iso("31-12-2026"), "2026-12-31")
        self.assertEqual(mixed_date_to_iso("2027-01-01"), "2027-01-01")

    def test_mixed_date_parser_and_validation_reject_invalid_values(self):
        for value in ("2026-02-30", "32-12-2026", "2026-12-31 00:00:00", ""):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    mixed_date_to_iso(value)
                self.connection.execute("DELETE FROM date_samples")
                self.connection.execute(
                    "INSERT INTO date_samples(business_date) VALUES(?)", (value,)
                )
                with self.assertRaisesRegex(ValueError, "sana kutiladi"):
                    validate_mixed_date_column(
                        self.connection, "date_samples", "business_date"
                    )


if __name__ == "__main__":
    unittest.main()
