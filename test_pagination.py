import os
import pathlib
import re
import tempfile
import unittest
from io import BytesIO

os.environ.setdefault("SECRET_KEY", "isolated-pagination-test-key")

import app as app_module
from openpyxl import load_workbook


PROJECT_ROOT = pathlib.Path(__file__).resolve().parent


class InvoicePaginationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="pagination-test-", dir=str(PROJECT_ROOT)
        )
        self.original_db = app_module.db
        self.original_db_path = app_module.DB_PATH
        self.connections = []

        def tracked_db():
            connection = self.original_db()
            self.connections.append(connection)
            return connection

        app_module.db = tracked_db
        app_module.DB_PATH = str(pathlib.Path(self.temp_dir.name) / "pagination.sqlite")
        app_module.init_db()
        connection = app_module.db()
        connection.execute(
            "INSERT INTO users(id,username,password_hash,role) VALUES(1,'manager','unused','rahbar')"
        )
        connection.executemany(
            "INSERT INTO users(id,username,password_hash,role) VALUES(?,?,?,?)",
            (
                (2, "worker", "unused", "umid"),
                (3, "accountant", "unused", "buxgalter"),
            ),
        )
        for index in range(1, 1324):
            customer = "Filter Target" if index <= 13 else f"Customer {index}"
            sale_id = connection.execute(
                """INSERT INTO sales
                   (date,customer,customer_phone,due_date,product,qty_kg,
                    price_per_kg,vat_rate,paid,user_id)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                ("01-11-2026", customer, "", "01-11-2026",
                 "Sara", 25, 1000, 0, 0, 1),
            ).lastrowid
            connection.execute(
                """INSERT INTO invoices
                   (sale_id,category,month_key,number,issued_date)
                   VALUES(?,?,?,?,?)""",
                (sale_id, "Sara", "2026-11", index, "01-11-2026"),
            )
        connection.commit()
        self.client = app_module.app.test_client()
        with self.client.session_transaction() as session:
            session["user_id"] = 1
            session["role"] = "rahbar"
            session["auth_version"] = 0

    def tearDown(self):
        for connection in self.connections:
            connection.close()
        app_module.db = self.original_db
        app_module.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def invoice_ids(self, response):
        return [
            int(invoice_id)
            for invoice_id in re.findall(rb'href="/nakladnoy/(\d+)"', response.data)
        ]

    def test_default_pages_cover_1323_invoices_once(self):
        ids = []
        for page in range(1, 68):
            response = self.client.get(f"/nakladnoylar?page={page}")
            self.assertEqual(response.status_code, 200)
            page_ids = self.invoice_ids(response)
            self.assertEqual(len(page_ids), 3 if page == 67 else 20)
            ids.extend(page_ids)
        self.assertEqual(len(ids), 1323)
        self.assertEqual(len(set(ids)), 1323)

    def test_page_sizes_and_filtered_count(self):
        for size, pages, final_count in ((50, 27, 23), (100, 14, 23)):
            response = self.client.get(f"/nakladnoylar?per_page={size}&page={pages}")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(self.invoice_ids(response)), final_count)
            self.assertIn(f"Jami 1323 ta".encode(), response.data)

        filtered = self.client.get("/nakladnoylar?q=Filter%20Target")
        self.assertEqual(filtered.status_code, 200)
        self.assertIn(b"Jami 13 ta", filtered.data)
        self.assertEqual(len(self.invoice_ids(filtered)), 13)

        for search in ("01.11.2026", "1-nav un", "25.00"):
            response = self.client.get(f"/nakladnoylar?q={search.replace(' ', '%20')}")
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"Jami 1323 ta", response.data)

    def test_page_links_preserve_search_and_size(self):
        response = self.client.get("/nakladnoylar?q=Filter%20Target&per_page=50")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'name="q" value="Filter Target"', response.data)
        self.assertIn(b'name="per_page"', response.data)
        self.assertIn(b"Jami 13 ta", response.data)

    def test_sales_excel_export_is_not_limited_by_page_arguments(self):
        response = self.client.get("/hisobot/excel?type=sales&page=67&per_page=20")
        self.assertEqual(response.status_code, 200)
        workbook = load_workbook(BytesIO(response.data), read_only=True)
        self.assertEqual(workbook.active.max_row, 1324)
        workbook.close()

    def test_paginated_manager_and_worker_pages_render(self):
        manager_pages = (
            "/qarzdorlar",
            "/boshliq/statistika",
            "/nazorat",
            "/sotuv",
            "/ishlab-chiqarish",
            "/hisobot",
            "/nakladnoylar",
        )
        for path in manager_pages:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            if path == "/nazorat":
                self.assertIn(b"Jami 1323 ta", response.data)

        with self.client.session_transaction() as session:
            session["user_id"] = 2
            session["role"] = "umid"
            session["auth_version"] = 0
        for path in ("/xodim/qarzlar", "/xodim/arxiv"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)

        with self.client.session_transaction() as session:
            session["user_id"] = 3
            session["role"] = "buxgalter"
            session["auth_version"] = 0
        for path in (
            "/xodimlar/nazorat",
            "/tolovlar",
            "/bugdoy",
            "/xarajatlar",
            "/pul-aylanmasi-sinov",
            "/eski-qarz",
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)


if __name__ == "__main__":
    unittest.main()
