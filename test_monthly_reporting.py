import os
import re
import tempfile
import unittest
from datetime import date
from io import BytesIO

from openpyxl import load_workbook

import app as erp
from monthly_reporting import build_monthly_report, build_monthly_workbook


class MonthlyReportingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db = erp.db
        self.original_db_path = erp.DB_PATH
        self.connections = []

        def tracked_db():
            connection = self.original_db()
            self.connections.append(connection)
            return connection

        erp.db = tracked_db
        erp.DB_PATH = os.path.join(self.temp_dir.name, 'monthly-test.sqlite')
        erp.init_db()
        self.test_password = 'Monthly-Reporting-Test-Password-2026!'
        password_hash = erp.generate_password_hash(self.test_password)
        con = erp.db()
        try:
            con.executemany(
                'INSERT INTO users(id,username,password_hash,role) VALUES(?,?,?,?)',
                (
                    (1, 'Umid test', password_hash, 'umid'),
                    (2, 'Maxmud test', password_hash, 'maxmud'),
                    (3, 'Buxgalter test', password_hash, 'buxgalter'),
                    (4, 'Rahbar test', password_hash, 'rahbar'),
                ),
            )
            con.execute(
                """INSERT INTO wheat_receipts
                   (id,date,supplier,gross,tare,net,price_per_kg,vat_rate,
                    purchase_method,user_id)
                   VALUES(1,'2026-10-02','Yetkazuvchi',1010,10,1000,1000,0,
                          'Bank orqali (birja)',3)"""
            )
            con.execute(
                """INSERT INTO wheat_purchase_payments
                   (id,wheat_receipt_id,accountant_id,payment_method,amount,
                    payment_date,operation_token)
                   VALUES(1,1,3,'hisob',1000000,'2026-10-02','wheat-test')"""
            )
            con.execute(
                """INSERT INTO opening_product_stocks
                   (product,stock_date,qty_kg,note,entered_by)
                   VALUES('Sara','2026-10-01',100,'test',3)"""
            )
            con.execute(
                """INSERT INTO opening_bagged_product_stocks
                   (product,stock_date,bag_25_count,bag_50_count,note,entered_by)
                   VALUES('Sara','2026-10-01',1,1,'test',3)"""
            )
            con.execute(
                """INSERT INTO production
                   (id,date,wheat_kg,raw_wheat_kg,flour_kg,bran_kg,note,user_id,
                    muchka_kg,telashka_kg)
                   VALUES(1,'2026-10-05',500,600,300,80,'test',1,10,5)"""
            )
            con.executemany(
                'INSERT INTO production_flour_types(production_id,product,qty_kg) VALUES(1,?,?)',
                (('Sara', 200), ('Yevro', 100)),
            )
            con.executemany(
                """INSERT INTO bagged_production_lines
                   (production_id,product,bag_kg,bag_count) VALUES(1,'Sara',?,?)""",
                ((25, 4), (50, 2)),
            )
            con.execute(
                """INSERT INTO sales
                   (id,date,customer,customer_phone,product,qty_kg,price_per_kg,
                    vat_rate,paid,note,user_id,payment_method,sale_mode,bag_kg,
                    bag_count,unit_price,due_date)
                   VALUES(1,'2026-10-10','Sinov xaridor','998901234567','Sara',
                          25,100,0,500,'test',1,'naqd','qop',25,1,2500,'')"""
            )
            con.execute(
                """INSERT INTO bagged_sale_deductions
                   (sale_id,product,bag_kg,bag_count) VALUES(1,'Sara',25,1)"""
            )
            con.execute(
                """INSERT INTO accountant_cash_additions
                   (id,date,amount,payment_method,source,note,entered_by,
                    operation_token,payload)
                   VALUES(1,'2026-10-05',100000,'naqd','Tashqi mablag‘',
                          'test',3,'addition-test','{}')"""
            )
            con.execute(
                """INSERT INTO expenses
                   (id,date,category,amount,note,user_id,payment_method)
                   VALUES(1,'2026-10-06','Boshqa',5000,'test',3,'naqd')"""
            )
            con.execute(
                """INSERT INTO account_cash_transfers
                   (id,date,from_channel,to_channel,amount,note,entered_by,
                    operation_token,payload)
                   VALUES(1,'2026-10-06','naqd','hisob',20000,'test',3,
                          'transfer-test','{}')"""
            )
            con.execute(
                """INSERT INTO worker_transfers
                   (id,worker_id,amount,status,created_at,decided_at,decided_by,
                    payment_method)
                   VALUES(1,1,3000,'approved','2026-10-06 09:00:00',
                          '2026-10-06 09:00:00',3,'naqd')"""
            )
            con.execute(
                """INSERT INTO worker_receipts
                   (id,sale_id,worker_id,amount,date)
                   VALUES(1,1,1,1000,'2026-11-05')"""
            )
            con.commit()
        finally:
            con.close()

    def tearDown(self):
        for connection in self.connections:
            connection.close()
        erp.db = self.original_db
        erp.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def login_as_manager(self):
        client = erp.app.test_client()
        login_page = client.get('/login')
        self.assertEqual(login_page.status_code, 200)
        csrf = re.search(
            rb'name="csrf_token" value="([^"]+)"', login_page.data)
        self.assertIsNotNone(csrf)
        response = client.post('/login', data={
            'csrf_token': csrf.group(1).decode(),
            'username': 'Rahbar test',
            'password': self.test_password,
        })
        self.assertEqual(response.status_code, 302)
        with client.session_transaction() as session:
            self.assertEqual(session.get('user_id'), 4)
            self.assertEqual(session.get('role'), 'rahbar')
            self.assertEqual(session.get('auth_version'), 0)
        return client

    def report(self, start, end):
        con = erp.db()
        try:
            con.execute('BEGIN')
            cache = {}

            def historical_snapshot(cutoff, warnings):
                if cutoff not in cache:
                    cache[cutoff] = erp._monthly_historical_snapshot(
                        con, cutoff, warnings)
                return cache[cutoff]

            return build_monthly_report(
                con, start, end, erp.FLOUR_TYPES,
                ('Kepak', 'Muchka', 'Telashka atxod'),
                historical_snapshot, erp._monthly_parse_date,
                lambda value: value.strftime('%d.%m.%Y'))
        finally:
            con.close()

    def test_october_and_november_snapshots_keep_dates_and_bag_ledgers(self):
        october = self.report(date(2026, 10, 1), date(2026, 10, 31))
        november = self.report(date(2026, 11, 1), date(2026, 11, 30))

        sara_october = next(
            row for row in october['inventory']['products']
            if row['product'] == 'Sara')
        self.assertEqual(sara_october['opening_kg'], 175)
        self.assertEqual(sara_october['produced_kg'], 200)
        self.assertEqual(sara_october['sold_kg'], 25)
        self.assertEqual(sara_october['closing_kg'], 350)
        self.assertEqual(
            (sara_october['closing_25'], sara_october['closing_50']), (4, 3))
        self.assertEqual(october['wheat']['purchased_kg'], 1000)
        self.assertEqual(october['wheat']['used_kg'], 600)
        self.assertEqual(october['wheat']['closing_kg'], 400)
        self.assertEqual(october['debt']['payments'], 0)
        self.assertEqual(october['debt']['closing'], 2000)

        sara_november = next(
            row for row in november['inventory']['products']
            if row['product'] == 'Sara')
        self.assertEqual(sara_november['opening_kg'], 350)
        self.assertEqual(sara_november['closing_kg'], 350)
        self.assertEqual(november['wheat']['opening_kg'], 400)
        self.assertEqual(november['wheat']['closing_kg'], 400)
        self.assertEqual(november['debt']['opening'], 2000)
        self.assertEqual(november['debt']['payments'], 1000)
        self.assertEqual(november['debt']['closing'], 1000)

    def test_cash_addition_is_not_sales_and_transfer_is_neutral(self):
        report = self.report(date(2026, 10, 1), date(2026, 10, 31))
        self.assertEqual(report['cash']['flows']['external_additions'], 100000)
        self.assertEqual(report['cash']['flows']['sales_and_debt_receipts'], 500)
        self.assertEqual(report['sales']['amount'], 2500)
        self.assertEqual(report['cash']['flows']['wheat_payments'], 1000000)
        self.assertEqual(report['cash']['flows']['other_expenses'], 5000)
        self.assertEqual(report['cash']['flows']['transfers_in'], 23000)
        self.assertEqual(report['cash']['flows']['transfers_out'], 23000)
        self.assertEqual(
            report['cash']['flow_channels']['external_additions']['naqd'], 100000)
        self.assertEqual(
            report['cash']['flow_channels']['wheat_payments']['hisob'], 1000000)
        self.assertEqual(
            report['cash']['closing_channels']['naqd']
            + report['cash']['closing_channels']['hisob'],
            -904500,
        )

    def test_opening_debt_entries_are_separate_from_new_sales_debt(self):
        con = erp.db()
        try:
            con.execute(
                """INSERT INTO opening_debts
                   (customer,customer_phone,amount,date,due_date,note,user_id)
                   VALUES('Sinov xaridor','+998901234567',1450000,
                          '2026-10-01','2026-11-01','test',3)"""
            )
            con.commit()
        finally:
            con.close()

        report = self.report(date(2026, 10, 1), date(2026, 10, 31))
        self.assertEqual(report['debt']['new_debt'], 2000)
        self.assertEqual(report['debt']['opening_debt_added'], 1450000)
        self.assertEqual(report['debt']['closing'], 1452000)
        customer = next(
            row for row in report['debt']['customers']
            if row['customer'] == 'Sinov xaridor')
        self.assertEqual(customer['new_debt'], 2000)
        self.assertEqual(customer['opening_debt_added'], 1450000)

        workbook = load_workbook(
            build_monthly_workbook(report), read_only=True)
        summary = workbook['Umumiy natijalar']
        debt_rows = {
            summary.cell(row, 2).value: summary.cell(row, 3).value
            for row in range(5, summary.max_row + 1)
            if summary.cell(row, 1).value == 'Qarz'
        }
        self.assertEqual(debt_rows['Davrda yuzaga kelgan yangi sotuv qarzi'], 2000)
        self.assertEqual(debt_rows['Davrda qayd etilgan dastlabki qarzlar'], 1450000)
        debt_sheet = workbook['Xaridor qarzlari']
        self.assertEqual(
            [debt_sheet.cell(4, column).value for column in range(1, 8)],
            ['Xaridor', 'Telefon', 'Davr boshidagi qarz, so‘m',
             'Yangi sotuv qarzi, so‘m',
             'Davrda qayd etilgan dastlabki qarz, so‘m',
             'Qarz to‘lovlari, so‘m', 'Davr oxiridagi qarz, so‘m'],
        )
        total_row = next(
            row for row in range(5, debt_sheet.max_row + 1)
            if debt_sheet.cell(row, 1).value == 'JAMI')
        self.assertEqual(debt_sheet.cell(total_row, 4).value, 2000)
        self.assertEqual(debt_sheet.cell(total_row, 5).value, 1450000)
        self.assertEqual(debt_sheet.cell(total_row, 7).value, 1452000)
        workbook.close()

        client = self.login_as_manager()
        page = client.get(
            '/moliya-hisobotlar?date=2026-10-31&start=2026-10-01&end=2026-10-31')
        self.assertEqual(page.status_code, 200)
        self.assertIn('Davrda qayd etilgan dastlabki qarzlar'.encode(), page.data)
        self.assertIn('1,450,000'.encode(), page.data)
        self.assertIn('2,000'.encode(), page.data)

    def test_explicit_qop_sales_fill_missing_bag_outflow_without_converting_kg_sales(self):
        con = erp.db()
        try:
            con.executemany(
                """INSERT INTO sales
                   (id,date,customer,customer_phone,product,qty_kg,price_per_kg,
                    vat_rate,paid,note,user_id,payment_method,sale_mode,bag_kg,
                    bag_count,unit_price,due_date)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    (2, '2026-10-11', 'Qop test', '998901234568', 'Sara',
                     50, 8000, 0, 400000, 'explicit qop', 1, 'naqd', 'qop',
                     25, 2, 200000, ''),
                    (3, '2026-10-12', 'Kg test', '998901234569', 'Sara',
                     50, 8000, 0, 400000, 'kg only', 1, 'naqd', 'kg',
                     None, None, 8000, ''),
                ),
            )
            con.commit()
        finally:
            con.close()

        report = self.report(date(2026, 10, 1), date(2026, 10, 31))
        sara = next(
            row for row in report['inventory']['products']
            if row['product'] == 'Sara')
        self.assertEqual(sara['sold_kg'], 125)
        self.assertEqual(sara['sold_25'], 3)
        self.assertEqual(sara['sold_50'], 0)
        self.assertEqual((sara['closing_25'], sara['closing_50']), (2, 3))
        missing_ledger_warnings = [
            warning for warning in report['warnings']
            if warning.get('key') == 'bagged_sale_deduction_missing'
        ]
        self.assertEqual(
            [(warning['sale_id'], warning['bag_kg'], warning['bag_count'])
             for warning in missing_ledger_warnings],
            [(2, 25, 2)],
        )

        workbook = load_workbook(
            build_monthly_workbook(report), read_only=True)
        stock_sheet = workbook['Ombor']
        sara_row = next(
            row for row in stock_sheet.iter_rows(min_row=5, values_only=True)
            if row[0] == 'Sara')
        self.assertEqual((sara_row[9], sara_row[10]), (3, 0))
        workbook.close()

    def test_bag_stock_counts_legacy_explicit_sales_once_and_restores_cancelled_sale(self):
        con = erp.db()
        try:
            baseline = erp.bagged_product_stocks(con)['Sara'].copy()
            con.executemany(
                """INSERT INTO sales
                   (id,date,customer,product,qty_kg,price_per_kg,vat_rate,paid,
                    user_id,sale_mode,bag_kg,bag_count,unit_price)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    (2, '2026-10-11', 'Qop 25', 'Sara', 50, 100, 0, 0, 1,
                     'qop', 25, 2, 2500),
                    (3, '2026-10-11', 'Qop 50', 'Sara', 150, 100, 0, 0, 1,
                     'qop', 50, 3, 5000),
                    (4, '2026-10-12', 'Cancelled qop', 'Sara', 50, 100, 0, 0, 1,
                     'qop', 25, 2, 2500),
                    (5, '2026-10-12', 'Kg only', 'Sara', 50, 100, 0, 0, 1,
                     'kg', None, None, 100),
                ),
            )
            con.execute(
                """INSERT INTO sale_cancellations(sale_id,reason,cancelled_at,user_id)
                   VALUES(4,'test cancellation','2026-10-13T10:00:00',3)"""
            )
            con.commit()

            after_sales = erp.bagged_product_stocks(con)['Sara']
            self.assertEqual(
                after_sales,
                {25: baseline[25] - 2, 50: baseline[50] - 3},
            )
            self.assertEqual(after_sales[25], 2)
            self.assertEqual(after_sales[50], 0)

            erp.record_bagged_sale(con, 2, 'Sara', 'qop', 25, 2)
            con.commit()
            after_recording = erp.bagged_product_stocks(con)['Sara']
            self.assertEqual(after_recording, after_sales)
            self.assertEqual(
                con.execute(
                    'SELECT COUNT(*) FROM bagged_sale_deductions WHERE sale_id=2'
                ).fetchone()[0],
                1,
            )

            con.execute('DELETE FROM sale_cancellations WHERE sale_id=4')
            con.commit()
            after_uncancelling = erp.bagged_product_stocks(con)['Sara']
            self.assertEqual(after_uncancelling[25], after_sales[25] - 2)
            self.assertEqual(after_uncancelling[50], after_sales[50])
        finally:
            con.close()

    def test_finance_page_and_seven_sheet_excel_route(self):
        client = self.login_as_manager()
        page = client.get(
            '/moliya-hisobotlar?date=2026-10-31&start=2026-10-01&end=2026-10-31')
        self.assertEqual(page.status_code, 200)
        self.assertIn('Oylik hisobot'.encode(), page.data)
        self.assertIn('100,000'.encode(), page.data)

        response = client.get(
            '/moliya-hisobotlar/oylik.xlsx?start=2026-10-01&end=2026-10-31')
        self.assertEqual(response.status_code, 200)
        workbook = load_workbook(BytesIO(response.data), read_only=True)
        self.assertEqual(
            workbook.sheetnames,
            ['Umumiy natijalar', 'Ishlab chiqarish', 'Ombor', 'Sotuvlar',
             'Xaridor qarzlari', 'Kassa va pul aylanishi', 'Bug‘doy xaridi'],
        )
        self.assertEqual(workbook['Sotuvlar'].max_row, 5)
        self.assertEqual(workbook['Ishlab chiqarish'].max_row, 5)
        self.assertEqual(workbook['Ishlab chiqarish'].cell(5, 15).value, 4)
        workbook.close()

    def test_workbook_preserves_legacy_cashflow_warning_shape(self):
        report = self.report(date(2026, 10, 1), date(2026, 10, 31))
        report['warnings'].append({
            'code': 'legacy_cashflow_warning',
            'message': 'Tarixiy pul yozuvi aniqlanmagan.',
        })
        workbook = load_workbook(
            build_monthly_workbook(report), read_only=True)
        summary = workbook['Umumiy natijalar']
        warning_rows = [
            (summary.cell(row, 2).value, summary.cell(row, 5).value)
            for row in range(5, summary.max_row + 1)
            if summary.cell(row, 1).value == 'Ogohlantirish'
        ]
        self.assertIn(
            ('legacy_cashflow_warning',
             'Tarixiy pul yozuvi aniqlanmagan.'),
            warning_rows,
        )
        workbook.close()


if __name__ == '__main__':
    unittest.main()
