import os
import re
import sqlite3
import tempfile
import unittest
from datetime import date
from io import BytesIO
from pathlib import Path

from openpyxl import load_workbook


os.environ.setdefault('SECRET_KEY', 'wheat-opening-tests-only-secret')
import app as erp
from monthly_reporting import build_monthly_report, build_monthly_workbook


class WheatOpeningStockTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix='wheat_opening_stock_test_')
        self.database = Path(self.temp_dir.name) / 'test.sqlite'
        self.original_db = erp.db
        self.original_db_path = erp.DB_PATH
        self.original_secret = erp.app.secret_key
        self.connections = []

        def tracked_db():
            connection = self.original_db()
            self.connections.append(connection)
            return connection

        erp.db = tracked_db
        erp.DB_PATH = str(self.database)
        erp.app.secret_key = 'wheat-opening-tests-only-secret'
        erp.init_db()
        with erp.db() as connection:
            self.user_ids = {}
            for username, role in (
                    ('test_accountant', 'buxgalter'),
                    ('test_manager', 'rahbar'),
                    ('test_umid', 'umid'),
                    ('test_maxmud', 'maxmud')):
                self.user_ids[role] = connection.execute(
                    '''INSERT INTO users(username,password_hash,role,phone)
                       VALUES(?,?,?,'')''',
                    (username, erp.generate_password_hash(
                        'Wheat-Opening-Test-Password-2026!'), role),
                ).lastrowid

    def tearDown(self):
        erp.db = self.original_db
        erp.DB_PATH = self.original_db_path
        erp.app.secret_key = self.original_secret
        for connection in self.connections:
            connection.close()
        self.temp_dir.cleanup()

    @staticmethod
    def csrf(client, path):
        response = client.get(path)
        if response.status_code != 200:
            raise AssertionError(f'GET {path} returned {response.status_code}')
        match = re.search(
            r'name="csrf_token" value="([^"]+)"',
            response.get_data(as_text=True),
        )
        if match is None:
            raise AssertionError(f'CSRF token not found on {path}')
        return match.group(1)

    def login(self, role):
        client = erp.app.test_client()
        usernames = {
            'buxgalter': 'test_accountant',
            'rahbar': 'test_manager',
            'umid': 'test_umid',
            'maxmud': 'test_maxmud',
        }
        response = client.post('/login', data={
            'csrf_token': self.csrf(client, '/login'),
            'username': usernames[role],
            'password': 'Wheat-Opening-Test-Password-2026!',
        })
        self.assertEqual(response.status_code, 302)
        return client

    @staticmethod
    def opening_form(client):
        response = client.get('/bugdoy')
        if response.status_code != 200:
            raise AssertionError(f'GET /bugdoy returned {response.status_code}')
        html = response.get_data(as_text=True)
        token = re.search(
            r'<form[^>]+action="/bugdoy/boshlangich-zaxira".*?'
            r'name="operation_token" value="([^"]+)"',
            html,
            re.DOTALL,
        )
        csrf = re.search(
            r'<form[^>]+action="/bugdoy/boshlangich-zaxira".*?'
            r'name="csrf_token" value="([^"]+)"',
            html,
            re.DOTALL,
        )
        if token is None or csrf is None:
            raise AssertionError('Opening wheat stock form/token is missing.')
        return token.group(1), csrf.group(1)

    def post_opening_stock(self, client, token, csrf, amount='10000'):
        return client.post('/bugdoy/boshlangich-zaxira', data={
            'operation_token': token,
            'csrf_token': csrf,
            'date': '2026-10-01',
            'qty_kg': amount,
            'note': 'Tasdiqlangan boshlang‘ich bug‘doy',
        })

    def monthly_report(self, start, end):
        with erp.db() as connection:
            snapshots = {}

            def historical_snapshot(cutoff, warnings):
                if cutoff not in snapshots:
                    snapshots[cutoff] = erp._monthly_historical_snapshot(
                        connection, cutoff, warnings)
                return snapshots[cutoff]

            return build_monthly_report(
                connection, start, end, erp.FLOUR_TYPES,
                ('Kepak', 'Muchka', 'Telashka atxod'),
                historical_snapshot, erp._monthly_parse_date,
                lambda value: value.strftime('%d.%m.%Y'))

    def test_opening_stock_is_single_idempotent_non_cash_receipt(self):
        accountant = self.login('buxgalter')
        first_token, first_csrf = self.opening_form(accountant)

        concurrent_form_client = self.login('buxgalter')
        second_token, second_csrf = self.opening_form(concurrent_form_client)

        response = self.post_opening_stock(
            accountant, first_token, first_csrf)
        self.assertEqual(response.status_code, 302)
        repeated = self.post_opening_stock(
            accountant, first_token, first_csrf)
        self.assertEqual(repeated.status_code, 302)
        second_submission = self.post_opening_stock(
            concurrent_form_client, second_token, second_csrf)
        self.assertEqual(second_submission.status_code, 302)

        with erp.db() as connection:
            opening_rows = connection.execute(
                'SELECT * FROM wheat_receipts WHERE purchase_method=?',
                (erp.OPENING_WHEAT_METHOD,),
            ).fetchall()
            self.assertEqual(len(opening_rows), 1)
            self.assertEqual(opening_rows[0]['net'], 10000)
            self.assertEqual(opening_rows[0]['price_per_kg'], 0)
            self.assertEqual(connection.execute(
                'SELECT COUNT(*) FROM wheat_purchase_payments').fetchone()[0], 0)
            self.assertEqual(connection.execute(
                'SELECT COUNT(*) FROM expenses').fetchone()[0], 0)
            self.assertEqual(connection.execute(
                'SELECT COUNT(*) FROM opening_debts').fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM remaining_write_operations "
                "WHERE action='wheat_opening_stock'").fetchone()[0], 1)
            self.assertEqual(connection.execute(
                'SELECT COALESCE(SUM(net),0) FROM wheat_receipts').fetchone()[0],
                10000)

        self.assertIn('10000.00', accountant.get('/bugdoy').get_data(as_text=True))
        self.assertEqual(
            accountant.get('/omborlar').status_code, 200)
        self.assertIn(
            '10.00',
            accountant.get('/omborlar').get_data(as_text=True))
        self.assertNotIn(
            'toggle-wheat-opening',
            accountant.get('/bugdoy').get_data(as_text=True))

    def test_only_accountant_can_add_opening_wheat_stock(self):
        worker = self.login('umid')
        response = worker.post('/bugdoy/boshlangich-zaxira', data={
            'csrf_token': self.csrf(worker, '/login'),
            'date': '2026-10-01',
            'qty_kg': '10000',
        })
        self.assertEqual(response.status_code, 403)
        with erp.db() as connection:
            self.assertEqual(connection.execute(
                'SELECT COUNT(*) FROM wheat_receipts').fetchone()[0], 0)

    def test_rejects_duplicate_stock_and_existing_wheat_activity(self):
        accountant = self.login('buxgalter')
        token, csrf = self.opening_form(accountant)
        duplicate_client = self.login('buxgalter')
        second_token, second_csrf = self.opening_form(duplicate_client)
        self.post_opening_stock(accountant, token, csrf)

        # A fresh operation token cannot add a second opening balance.
        self.post_opening_stock(
            duplicate_client, second_token, second_csrf, '500')
        with erp.db() as connection:
            self.assertEqual(connection.execute(
                'SELECT COUNT(*) FROM wheat_receipts').fetchone()[0], 1)

        # A clean one-time opening entry is not allowed after a purchase/production.
        other = Path(self.temp_dir.name) / 'activity.sqlite'
        original_path = erp.DB_PATH
        erp.DB_PATH = str(other)
        erp.init_db()
        with erp.db() as connection:
            connection.execute(
                '''INSERT INTO users(username,password_hash,role,phone)
                   VALUES('test_accountant',?,'buxgalter','')''',
                (erp.generate_password_hash(
                    'Wheat-Opening-Test-Password-2026!'),))
            connection.execute(
                '''INSERT INTO wheat_receipts
                   (date,supplier,gross,tare,net,cleaned_net,price_per_kg,
                    vat_rate,note,user_id,purchase_method)
                   VALUES('01-10-2026','',100,0,100,100,1,0,'',1,'Naqd')''')
        activity_client = self.login('buxgalter')
        form_token, form_csrf = self.opening_form(activity_client)
        self.post_opening_stock(
            activity_client, form_token, form_csrf, '500')
        with erp.db() as connection:
            self.assertEqual(connection.execute(
                'SELECT COUNT(*) FROM wheat_receipts').fetchone()[0], 1)
        erp.DB_PATH = original_path

    def test_wheat_stock_is_consumable_and_monthly_report_separates_opening(self):
        accountant = self.login('buxgalter')
        token, csrf = self.opening_form(accountant)
        self.post_opening_stock(accountant, token, csrf, '10000')

        umid = self.login('umid')
        production_page = umid.get('/ishlab-chiqarish')
        self.assertEqual(production_page.status_code, 200)
        self.assertIn('10.000', production_page.get_data(as_text=True))
        html = production_page.get_data(as_text=True)
        operation_token = re.search(
            r'name="operation_token" value="([^"]+)"', html)
        csrf_token = re.search(
            r'name="csrf_token" value="([^"]+)"', html)
        self.assertIsNotNone(operation_token)
        self.assertIsNotNone(csrf_token)
        form = {
            'operation_token': operation_token.group(1),
            'csrf_token': csrf_token.group(1),
            'date': '2026-10-02',
            'unit': 'kg',
            'raw_wheat': '250',
            'cleaned_wheat': '200',
            'flour_0': '0',
            'flour_1': '0',
            'flour_2': '0',
            'flour_3': '0',
            'flour_4': '100',
            'bran_kg': '0',
            'muchka_kg': '0',
            'telashka_kg': '0',
            'note': 'Opening wheat stock integration test',
        }
        for product in erp.BAGGED_FLOUR_PRODUCTS:
            for size in (25, 50):
                form.setdefault('bag_product', [])
                form.setdefault('bag_kg', [])
                form.setdefault('bag_count', [])
                form['bag_product'].append(product)
                form['bag_kg'].append(str(size))
                form['bag_count'].append('0')
        result = umid.post('/ishlab-chiqarish', data=form)
        self.assertEqual(result.status_code, 302)

        with erp.db() as connection:
            stock = connection.execute('''SELECT
                (SELECT COALESCE(SUM(net),0) FROM wheat_receipts) -
                (SELECT COALESCE(SUM(COALESCE(raw_wheat_kg,wheat_kg)),0)
                 FROM production)''').fetchone()[0]
            self.assertEqual(stock, 9750)
            self.assertEqual(connection.execute(
                'SELECT COUNT(*) FROM wheat_purchase_payments').fetchone()[0], 0)
            self.assertEqual(connection.execute(
                'SELECT COUNT(*) FROM expenses').fetchone()[0], 0)

        report = self.monthly_report(date(2026, 10, 1), date(2026, 10, 31))
        self.assertEqual(report['wheat']['opening_kg'], 0)
        self.assertEqual(report['wheat']['opening_stock_added_kg'], 10000)
        self.assertEqual(report['wheat']['purchased_kg'], 0)
        self.assertEqual(report['wheat']['used_kg'], 250)
        self.assertEqual(report['wheat']['closing_kg'], 9750)
        self.assertEqual(len(report['wheat']['receipts']), 0)
        self.assertEqual(len(report['wheat']['initial_stock_entries']), 1)
        self.assertNotIn(
            'wheat_payment_method_unavailable',
            {warning['key'] for warning in report['warnings']})
        next_month = self.monthly_report(
            date(2026, 11, 1), date(2026, 11, 30))
        self.assertEqual(next_month['wheat']['opening_kg'], 9750)
        self.assertEqual(next_month['wheat']['purchased_kg'], 0)
        self.assertEqual(next_month['wheat']['closing_kg'], 9750)
        self.assertNotIn(
            'wheat_opening_balance_unconfirmed',
            {warning['key'] for warning in next_month['warnings']})

        workbook = load_workbook(
            BytesIO(build_monthly_workbook(report).getvalue()),
            read_only=True, data_only=True)
        self.assertEqual(len(workbook.sheetnames), 7)
        wheat_sheet = workbook['Bug‘doy xaridi']
        rows = list(wheat_sheet.values)
        self.assertTrue(any(
            row[8] == erp.OPENING_WHEAT_METHOD and row[5] == 10000
            and row[7] == 0
            for row in rows if len(row) >= 9))
        summary = list(workbook['Umumiy natijalar'].values)
        self.assertTrue(any(
            row[0] == 'Bug‘doy'
            and row[1] == 'Boshlang‘ich zaxira qo‘shildi'
            and row[2] == 10000
            for row in summary if len(row) >= 3))
        workbook.close()

        manager = self.login('rahbar')
        report_url = (
            '/moliya-hisobotlar?date=2026-10-31&start=2026-10-01'
            '&end=2026-10-31')
        self.assertEqual(manager.get(report_url).status_code, 200)
        excel_response = manager.get(
            '/moliya-hisobotlar/oylik.xlsx?start=2026-10-01&end=2026-10-31')
        self.assertEqual(excel_response.status_code, 200)
        http_workbook = load_workbook(
            BytesIO(excel_response.data), read_only=True, data_only=True)
        self.assertEqual(len(http_workbook.sheetnames), 7)
        http_workbook.close()


if __name__ == '__main__':
    unittest.main()
