import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote, urlencode, urlsplit
from urllib.request import url2pathname

from openpyxl import Workbook, load_workbook

os.environ["SECRET_KEY"] = "isolated-excel-date-test-key"

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
from excel_dates import (
    EXCEL_DATE_FORMAT,
    display_date,
    excel_date,
    format_date_columns,
)
from worker_cash_export import build_worker_cash_workbook


class ExcelDateFormatTests(unittest.TestCase):
    def test_iso_and_dmy_export_as_real_excel_dates(self):
        with tempfile.TemporaryDirectory(prefix="excel-date-formats-") as temp_dir:
            path = Path(temp_dir) / "date-formats.xlsx"
            workbook = Workbook()
            sheet = workbook.active
            sheet.append(["Input", "Date"])
            sheet.append(["ISO", "2026-10-29"])
            sheet.append(["DMY", "29-10-2026"])

            format_date_columns(sheet, (2,))
            workbook.save(path)

            exported = load_workbook(path, data_only=True).active
            for row in (2, 3):
                with self.subTest(row=row):
                    self.assertIsInstance(exported.cell(row, 2).value, datetime)
                    self.assertEqual(exported.cell(row, 2).value.date(), date(2026, 10, 29))
                    self.assertEqual(exported.cell(row, 2).number_format, EXCEL_DATE_FORMAT)
                    self.assertEqual(
                        exported.cell(row, 2).value.strftime("%d.%m.%y"),
                        "29.10.26",
                    )

    def test_december_january_sorting_and_inclusive_range_edges(self):
        values = [
            excel_date("2027-01-01"),
            excel_date("31-12-2026"),
            excel_date("2027-01-02"),
        ]
        start = excel_date("31-12-2026")
        end = excel_date("2027-01-01")

        self.assertEqual(sorted(values), [
            datetime(2026, 12, 31),
            datetime(2027, 1, 1),
            datetime(2027, 1, 2),
        ])
        self.assertEqual([value for value in values if start <= value <= end], [
            datetime(2027, 1, 1),
            datetime(2026, 12, 31),
        ])

    def test_leap_day_is_a_real_excel_date(self):
        result = excel_date("29-02-2024")
        self.assertEqual(result, datetime(2024, 2, 29))
        self.assertEqual(display_date("2024-02-29"), "29.02.24")

    def test_invalid_or_mixed_plain_date_formats_are_rejected(self):
        for value in (
            "abc",
            "2026-02-30",
            "2026-13-01",
            "32.13.2026",
            " 2026-10-29 ",
            "29-10-2026 12:30:00",
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "Excel eksportida sana yaroqsiz"):
                    excel_date(value)

    def test_iso_timestamp_keeps_its_time_component(self):
        value = "2026-12-31 23:59:58"
        result = excel_date(value)
        self.assertEqual(result, datetime(2026, 12, 31, 23, 59, 58))

    def test_missing_and_timezone_aware_values_keep_existing_behavior(self):
        self.assertIsNone(excel_date(None))
        self.assertIsNone(excel_date(""))
        with self.assertRaisesRegex(ValueError, "Excel eksportida sana yaroqsiz"):
            excel_date("2026-10-29T12:30:00+05:00")

    def test_worker_cash_v3_keeps_layout_and_accepts_dmy_dates(self):
        with tempfile.TemporaryDirectory(prefix="worker-cash-date-format-") as temp_dir:
            template_path = Path(temp_dir) / "worker-cash-template.xlsx"
            template = Workbook()
            summary = template.active
            summary.title = "Umumiy hisobot"
            history = template.create_sheet("Tarix")
            for sheet in template.worksheets:
                for row in range(1, 17):
                    for column in range(1, 13):
                        sheet.cell(row, column, f"template-{row}-{column}")
            template.save(template_path)

            content = build_worker_cash_workbook(
                template_path,
                workers=[{"id": 1, "username": "Umid"}],
                transfers=[{
                    "id": 1,
                    "worker_id": 1,
                    "payment_method": "naqd",
                    "amount": 20,
                    "status": "approved",
                    "created_at": "2026-12-31 23:59:58",
                    "decided_at": None,
                    "accountant": "Buxgalter",
                }],
                directs=[{
                    "id": 2,
                    "worker_id": 1,
                    "payment_method": "plastik",
                    "amount": 15,
                    "payment_date": "01-01-2027",
                    "accountant": "Buxgalter",
                    "customer": "Xaridor",
                    "customer_phone": "998901234567",
                    "note": "",
                }],
                report_date="29-10-2026",
            )
            exported = load_workbook(BytesIO(content), data_only=True)
            self.assertEqual(exported.sheetnames, ["Umumiy hisobot", "Tarix"])
            self.assertEqual(exported["Tarix"].freeze_panes, "D7")
            self.assertEqual(exported["Tarix"].auto_filter.ref, "A6:J8")
            self.assertEqual(exported["Tarix"]["B7"].value.date(), date(2026, 12, 31))
            self.assertEqual(exported["Tarix"]["B8"].value.date(), date(2027, 1, 1))
            self.assertEqual(exported["Tarix"]["B7"].number_format, EXCEL_DATE_FORMAT)
            self.assertEqual(exported["Tarix"]["B8"].number_format, EXCEL_DATE_FORMAT)
            self.assertEqual(exported["Umumiy hisobot"]["C3"].value.date(), date(2026, 10, 29))
            self.assertEqual(exported["Umumiy hisobot"]["C3"].number_format, EXCEL_DATE_FORMAT)

    def test_wheat_excel_export_filters_and_sorts_mixed_iso_dmy_dates(self):
        with tempfile.TemporaryDirectory(
            prefix="excel-date-export-", dir=str(Path(__file__).resolve().parent)
        ) as temp_dir:
            database_root = Path(temp_dir).resolve()
            ALLOWED_DATABASE_ROOTS.add(database_root)
            connections = []
            original_db = app_module.db
            original_db_path = app_module.DB_PATH

            def tracked_db():
                connection = original_db()
                connections.append(connection)
                return connection

            app_module.db = tracked_db
            app_module.DB_PATH = str(database_root / "excel-date-export.sqlite")
            try:
                app_module.init_db()
                with app_module.db() as connection:
                    connection.execute(
                        "INSERT INTO users(id,username,password_hash,role) "
                        "VALUES(1,'accountant','unused','buxgalter')"
                    )
                    for index, day in enumerate((
                        "2026-12-30",
                        "2026-12-31",
                        "2027-01-01",
                        "2027-01-02",
                        "31-12-2026",
                        "01-01-2027",
                    )):
                        connection.execute(
                            """INSERT INTO wheat_receipts
                               (date,supplier,gross,tare,net,cleaned_net,price_per_kg,
                                vat_rate,note,user_id,purchase_method)
                               VALUES(?,?,100,0,100,100,2.5,0,'test',1,'Birja')""",
                            (day, f"Supplier {index}"),
                        )
                client = app_module.app.test_client()
                with client.session_transaction() as session:
                    session["user_id"] = 1
                    session["role"] = "buxgalter"
                    session["auth_version"] = 0

                all_response = client.get("/hisobot/excel?type=wheat")
                self.assertEqual(all_response.status_code, 200)
                all_sheet = load_workbook(BytesIO(all_response.data), data_only=True).active
                exported_dates = [all_sheet.cell(row, 1).value for row in range(2, 8)]
                self.assertEqual(
                    [value.date() for value in exported_dates],
                    [
                        date(2026, 12, 30),
                        date(2026, 12, 31),
                        date(2026, 12, 31),
                        date(2027, 1, 1),
                        date(2027, 1, 1),
                        date(2027, 1, 2),
                    ],
                )
                self.assertEqual(
                    [all_sheet.cell(row, 3).value for row in range(2, 8)],
                    ["Supplier 0", "Supplier 1", "Supplier 4", "Supplier 2", "Supplier 5", "Supplier 3"],
                )
                self.assertTrue(all(value.number_format == EXCEL_DATE_FORMAT for value in
                                    [all_sheet.cell(row, 1) for row in range(2, 8)]))

                for start, end in (
                    ("2026-12-31", "2027-01-01"),
                    ("31-12-2026", "01-01-2027"),
                ):
                    with self.subTest(start=start, end=end):
                        filtered_response = client.get(
                            "/hisobot/excel?" + urlencode({
                                "type": "wheat", "start": start, "end": end,
                            })
                        )
                        self.assertEqual(filtered_response.status_code, 200)
                        filtered_sheet = load_workbook(
                            BytesIO(filtered_response.data), data_only=True
                        ).active
                        self.assertEqual(
                            [filtered_sheet.cell(row, 1).value.date()
                             for row in range(2, 6)],
                            [
                                date(2026, 12, 31), date(2026, 12, 31),
                                date(2027, 1, 1), date(2027, 1, 1),
                            ],
                        )
                        self.assertEqual(filtered_sheet.max_row, 5)
                        self.assertEqual(
                            sum(filtered_sheet.cell(row, 6).value for row in range(2, 6)),
                            1000,
                        )

                invalid_boundary = client.get(
                    "/hisobot/excel?" + urlencode({
                        "type": "wheat", "start": "2026-02-30",
                    })
                )
                self.assertEqual(invalid_boundary.status_code, 400)
                with app_module.db() as connection:
                    connection.execute(
                        """INSERT INTO wheat_receipts
                           (date,supplier,gross,tare,net,cleaned_net,price_per_kg,
                            vat_rate,note,user_id,purchase_method)
                           VALUES('bad-date','Bad',100,0,100,100,2.5,0,'test',1,'Birja')"""
                    )
                invalid_stored_date = client.get(
                    "/hisobot/excel?" + urlencode({
                        "type": "wheat",
                        "start": "2026-12-31",
                        "end": "2027-01-01",
                    })
                )
                self.assertEqual(invalid_stored_date.status_code, 400)
                self.assertIn(b"Hisobotdagi sana yaroqsiz", invalid_stored_date.data)
            finally:
                for connection in connections:
                    connection.close()
                app_module.db = original_db
                app_module.DB_PATH = original_db_path
                ALLOWED_DATABASE_ROOTS.discard(database_root)

    def test_filtered_report_routes_accept_dmy_rows_without_changing_totals(self):
        with tempfile.TemporaryDirectory(
            prefix="excel-report-routes-", dir=str(Path(__file__).resolve().parent)
        ) as temp_dir:
            database_root = Path(temp_dir).resolve()
            ALLOWED_DATABASE_ROOTS.add(database_root)
            connections = []
            original_db = app_module.db
            original_db_path = app_module.DB_PATH

            def tracked_db():
                connection = original_db()
                connections.append(connection)
                return connection

            app_module.db = tracked_db
            app_module.DB_PATH = str(database_root / "excel-report-routes.sqlite")
            try:
                app_module.init_db()
                with app_module.db() as connection:
                    connection.execute(
                        "INSERT INTO users(id,username,password_hash,role) "
                        "VALUES(1,'accountant','unused','buxgalter')"
                    )
                    sale_ids = []
                    for index, day in enumerate(("31-12-2026", "01-01-2027")):
                        sale_ids.append(connection.execute(
                            """INSERT INTO sales
                               (date,customer,product,qty_kg,price_per_kg,vat_rate,
                                paid,user_id,customer_phone,due_date)
                               VALUES(?,?,?,10,2,0,?,?,?,?)""",
                            (day, "Customer", "Flour", 10 if index == 0 else 0,
                             1, "998901234567", day),
                        ).lastrowid)
                        connection.execute(
                            """INSERT INTO production(date,wheat_kg,flour_kg,bran_kg)
                               VALUES(?,100,90,10)""",
                            (day,),
                        )
                        connection.execute(
                            """INSERT INTO expenses(date,category,amount,note)
                               VALUES(?,'Fuel',10,'test')""",
                            (day,),
                        )
                    connection.execute(
                        "INSERT INTO payments(date,customer,amount,note) "
                        "VALUES('31-12-2026','Customer',7,'test')"
                    )
                    connection.execute(
                        """INSERT INTO worker_receipts
                           (sale_id,worker_id,amount,date) VALUES(?,?,5,'01-01-2027')""",
                        (sale_ids[0], 1),
                    )

                client = app_module.app.test_client()
                with client.session_transaction() as session:
                    session["user_id"] = 1
                    session["role"] = "buxgalter"
                    session["auth_version"] = 0

                for kind in ("cleaning", "production", "sales", "payments", "expenses"):
                    with self.subTest(kind=kind):
                        response = client.get(
                            "/hisobot/excel?" + urlencode({
                                "type": kind,
                                "start": "2026-12-31",
                                "end": "2027-01-01",
                            })
                        )
                        self.assertEqual(response.status_code, 200)
                        workbook = load_workbook(BytesIO(response.data), data_only=True)
                        sheet = workbook.active
                        expected_events = 3 if kind == "payments" else 2
                        expected_dates = (
                            [date(2026, 12, 31), date(2026, 12, 31),
                             date(2027, 1, 1)]
                            if kind == "payments"
                            else [date(2026, 12, 31), date(2027, 1, 1)]
                        )
                        if kind == "production":
                            data_rows = [
                                row for row in range(8, sheet.max_row + 1)
                                if isinstance(sheet.cell(row, 1).value, (date, datetime))
                            ]
                        else:
                            self.assertEqual(sheet.max_row, expected_events + 1)
                            data_rows = list(range(2, expected_events + 2))
                        self.assertEqual(len(data_rows), expected_events)
                        self.assertEqual(
                            [sheet.cell(row, 1).value.date() for row in data_rows],
                            expected_dates,
                        )
                        self.assertTrue(all(
                            sheet.cell(row, 1).number_format == (
                                "dd.mm.yyyy" if kind == "production"
                                else EXCEL_DATE_FORMAT
                            )
                            for row in data_rows
                        ))
                        if kind == "expenses":
                            self.assertEqual(
                                sum(sheet.cell(row, 4).value for row in (2, 3)), 20
                            )
                payments = load_workbook(
                    BytesIO(client.get(
                        "/hisobot/excel?" + urlencode({
                            "type": "payments",
                            "start": "2026-12-31",
                            "end": "2027-01-01",
                        })
                    ).data),
                    data_only=True,
                )
                self.assertEqual(payments.active.max_row, 4)
                self.assertEqual(
                    payments["Izoh va ogohlantirishlar"]["B2"].value, 22
                )
                with app_module.db() as connection:
                    connection.execute(
                        "UPDATE sales SET date='2026-12-31' WHERE id=?",
                        (sale_ids[0],),
                    )
                for kind, expected_dates in (
                    ("sales", [date(2026, 12, 31), date(2027, 1, 1)]),
                    ("payments", [
                        date(2026, 12, 31), date(2026, 12, 31), date(2027, 1, 1),
                    ]),
                ):
                    response = client.get(
                        "/hisobot/excel?" + urlencode({
                            "type": kind,
                            "start": "2026-12-31",
                            "end": "2027-01-01",
                        })
                    )
                    self.assertEqual(response.status_code, 200)
                    self.assertTrue(response.content_type.startswith(
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    ))
                    sheet = load_workbook(
                        BytesIO(response.data), data_only=True
                    ).active
                    self.assertEqual(sheet.max_row, len(expected_dates) + 1)
                    self.assertEqual(
                        [sheet.cell(row, 1).value.date()
                         for row in range(2, sheet.max_row + 1)],
                        expected_dates,
                    )
            finally:
                for connection in connections:
                    connection.close()
                app_module.db = original_db
                app_module.DB_PATH = original_db_path
                ALLOWED_DATABASE_ROOTS.discard(database_root)


if __name__ == "__main__":
    unittest.main()
