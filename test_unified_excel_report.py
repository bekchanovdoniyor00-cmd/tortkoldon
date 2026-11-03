import gc
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from io import BytesIO
from pathlib import Path

sys.dont_write_bytecode = True
import app as erp
from openpyxl import load_workbook


class ClosingConnection:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection.__enter__()

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return self.connection.__exit__(exc_type, exc_value, traceback)
        finally:
            self.connection.close()

    def close(self):
        self.connection.close()

    def __getattr__(self, name):
        return getattr(self.connection, name)


class UnifiedExcelReportTests(unittest.TestCase):
    def setUp(self):
        self.workbooks = []
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = erp.DB_PATH
        erp.DB_PATH = str(Path(self.temp_dir.name) / 'temporary_test.db')
        self.original_db = erp.db
        erp.db = lambda: ClosingConnection(self.original_db())
        original_backup = erp.backup_before_internal_cash_transfer_migration
        erp.backup_before_internal_cash_transfer_migration = lambda: None
        try:
            erp.init_db()
        finally:
            erp.backup_before_internal_cash_transfer_migration = original_backup
        erp.app.config.update(TESTING=True)
        con = erp.db()
        try:
            con.executemany(
                'INSERT INTO users(id,username,password_hash,role,active) VALUES(?,?,?,?,1)',
                [
                    (1, 'Rahbar', 'test', 'rahbar'),
                    (2, 'Umid', 'test', 'umid'),
                    (3, 'Maxmud', 'test', 'maxmud'),
                    (4, 'Buxgalter', 'test', 'buxgalter'),
                ],
            )
            con.commit()
        finally:
            con.close()
        self.original_current_user = erp.current_user
        erp.current_user = lambda: {
            'id': 1,
            'role': 'rahbar',
            'active': 1,
            'auth_version': 0,
        }
        with closing(erp.db()) as con:
            con.execute(
                "INSERT INTO customers(id,name,phone,phone_key) VALUES(1,?,?,?)",
                ('Yangi nom', '+998 90 111 22 33', '998901112233'),
            )
            con.execute(
                """INSERT INTO customer_audit
                   (id,customer_id,actor_user_id,old_name,new_name,old_phone,new_phone)
                   VALUES(1,1,4,?,?,?,?)""",
                ('Eski nom', 'Yangi nom', '+998 90 111 22 33', '+998 90 111 22 33'),
            )
            con.execute(
                """INSERT INTO opening_debts
                   (id,customer,customer_phone,amount,date,due_date,user_id)
                   VALUES(1,?,?,?,?,?,4)""",
                ('Eski nom', '+998 90 111 22 33', 30000, '2024-12-20', '2025-01-15'),
            )
            con.execute(
                """INSERT INTO sales
                   (id,date,customer,customer_phone,due_date,product,qty_kg,
                    price_per_kg,unit_price,vat_rate,paid,note,user_id,
                    sale_mode,bag_kg,bag_count,payment_method,
                    initial_payment_recipient_id)
                   VALUES(1,'2025-01-10','Eski nom','+998 90 111 22 33',
                    '2025-02-04','Sara',100,1000,1000,0,20000,'Avans',2,
                    'bag',50,2,'naqd',4)"""
            )
            con.execute(
                """INSERT INTO sales
                   (id,date,customer,customer_phone,due_date,product,qty_kg,
                    price_per_kg,unit_price,vat_rate,paid,note,user_id,
                    sale_mode,payment_method)
                   VALUES(2,'2025-01-20','Eski nom','+998 90 111 22 33',
                    '2025-02-14','Kepak',50,1000,1000,0,0,'',3,'kg','plastik')"""
            )
            con.executemany(
                """INSERT INTO sales
                   (id,date,customer,customer_phone,due_date,product,qty_kg,
                    price_per_kg,unit_price,vat_rate,paid,note,user_id,
                    sale_mode,payment_method)
                   VALUES(?,?,?,?,?,?,?,1000,1000,0,?,?,?,'kg','naqd')""",
                [
                    (4, '2025-01-26', 'Eski nom', '+998 90 111 22 33',
                     '2025-02-20', 'Sara', 10, 10000, 'To‘liq to‘lov', 2),
                    (5, '2025-01-27', 'Eski nom', '+998 90 111 22 33',
                     '2025-02-21', 'Kepak', 20, 0, 'To‘lov yo‘q', 3),
                ],
            )
            con.execute(
                """INSERT INTO sales
                   (id,date,customer,customer_phone,due_date,product,qty_kg,
                    price_per_kg,unit_price,vat_rate,paid,note,user_id,
                    sale_mode,payment_method)
                   VALUES(3,'2025-02-01','Eski nom','+998 90 111 22 33',
                    '2025-02-26','Sara',10,1000,1000,0,0,'Kelajakdagi sotuv',2,
                    'kg','naqd')"""
            )
            con.execute(
                """INSERT INTO sales
                   (id,date,customer,customer_phone,due_date,product,qty_kg,
                    price_per_kg,unit_price,vat_rate,paid,note,user_id,
                    sale_mode,payment_method)
                   VALUES(6,'2025-01-28','Eski nom','+998 90 111 22 33',
                    '2025-02-22','Sara',5,1000,1000,0,2000,'Bekor qilingan',2,
                    'kg','naqd')"""
            )
            con.execute(
                """INSERT INTO sale_cancellations(sale_id,reason,cancelled_at,user_id)
                   VALUES(6,'Sinov bekor qilish','2025-01-29 12:00:00',1)"""
            )
            con.execute(
                """INSERT INTO sales
                   (id,date,customer,customer_phone,due_date,product,qty_kg,
                    price_per_kg,unit_price,vat_rate,paid,note,user_id,
                    sale_mode,payment_method)
                   VALUES(7,'2025-01-30','Eski nom','+998 90 111 22 33',
                    '2025-02-24','Sara',5,1000,1000,0,0,'Keyin bekor qilingan',2,
                    'kg','naqd')"""
            )
            con.execute(
                """INSERT INTO sale_cancellations(sale_id,reason,cancelled_at,user_id)
                   VALUES(7,'Keyin bekor qilindi','2025-02-05 12:00:00',1)"""
            )
            con.execute(
                'INSERT INTO invoices(sale_id,category,month_key,number,issued_date) VALUES(1,?,?,?,?)',
                ('un', '2025-01', 17, '2025-01-10'),
            )
            con.execute(
                """INSERT INTO worker_sale_batches
                   (id,worker_id,customer,customer_phone,sale_date,total,paid)
                   VALUES(1,2,'Eski nom','+998 90 111 22 33','2025-01-26',10000,10000)"""
            )
            con.execute(
                'INSERT INTO worker_sale_batch_items(batch_id,sale_id) VALUES(1,4)')
            con.execute(
                """INSERT INTO worker_sale_batch_payments
                   (batch_id,payment_method,amount) VALUES(1,'plastik',10000)"""
            )
            con.executemany(
                """INSERT INTO worker_receipts
                   (id,sale_id,worker_id,amount,date,payment_method)
                   VALUES(?,?,?,?,?,?)""",
                [
                    (1, 1, 2, 30000, '2025-01-15', 'plastik'),
                    (2, 2, 3, 5000, '2025-01-25', 'naqd'),
                    (3, 1, 2, 7000, '2025-02-05', 'naqd'),
                ],
            )
            con.execute(
                """INSERT INTO accountant_worker_receipts
                   (id,receipt_id,worker_id,accountant_id,customer,customer_phone,
                    payment_method,amount,payment_date,note)
                   VALUES(1,1,2,4,'Eski nom','+998 90 111 22 33',
                    'plastik',30000,'2025-01-15','Buxgalter qabul qildi')"""
            )
            con.execute(
                """INSERT INTO payments(id,date,customer,amount,note,user_id)
                   VALUES(1,'2025-01-22','Eski nom',10000,'Eski qarz to‘lovi',4)"""
            )
            con.commit()
        self.client = erp.app.test_client()
        with self.client.session_transaction() as session:
            session['user_id'] = 1
            session['role'] = 'rahbar'
            session['auth_version'] = 0

    def tearDown(self):
        for workbook in self.workbooks:
            workbook.close()
        erp.current_user = self.original_current_user
        erp.db = self.original_db
        erp.DB_PATH = self.original_db_path
        gc.collect()
        self.temp_dir.cleanup()

    def workbook(self, query='start=2025-01-01&end=2025-01-31'):
        response = self.client.get('/hisobot/yagona-excel?' + query)
        if response.status_code != 200:
            self.fail(f'HTTP {response.status_code}: {response.get_data()[:500]!r}')
        self.assertIn(
            'tortkuldon_yagona_hisobot.xlsx',
            response.headers.get('Content-Disposition', ''),
        )
        workbook = load_workbook(BytesIO(response.data))
        self.workbooks.append(workbook)
        return workbook

    def test_exact_five_sheets_design_and_date_filtered_totals(self):
        wb = self.workbook()
        self.assertEqual(
            wb.sheetnames,
            ['JAMLANMA', 'SOTUVLAR', 'QARZLAR', 'TO‘LOVLAR', 'MIJOZLAR'],
        )
        summary = wb['JAMLANMA']
        self.assertEqual(summary['A5'].value, 185000)
        self.assertEqual(summary['C5'].value, 77000)
        self.assertEqual(summary['E5'].value, 150000)
        self.assertEqual(summary['G5'].value, 1)
        self.assertEqual(len(summary._images), 1)
        self.assertGreaterEqual(summary._images[0].width, 96)
        self.assertEqual(summary.freeze_panes, 'A8')
        self.assertTrue(summary.auto_filter.ref.startswith('A7:I'))
        self.assertEqual(summary['A4'].value, '📈 JAMI SOTUV')
        self.assertEqual(summary['E4'].value, '⚠ JAMI QARZ (DAVR OXIRI)')
        self.assertIn('Davr boshlanishigacha bo‘lgan qoldiq', summary['A6'].value)
        self.assertGreaterEqual(summary.column_dimensions['I'].width, 22)
        self.assertEqual(summary['G5'].number_format, '0 "ta"')
        self.assertEqual(summary['F8'].value, 100000)
        self.assertEqual(summary['G8'].value, 50000)
        self.assertEqual(summary['H8'].value, 50000)
        self.assertEqual(summary['I8'].value, 'Qisman to‘langan')
        self.assertIn('S-1 / № 17', summary['B8'].value)
        self.assertIn('so‘m', summary['F8'].number_format)
        sales = wb['SOTUVLAR']
        self.assertEqual(sales['P7'].value, 'To‘langan')
        self.assertEqual(sales['P8'].value, 'Qarzdor')
        self.assertEqual(sales['P9'].value, 'Bekor qilingan')
        self.assertEqual(sales['P10'].value, 'Qarzdor')
        self.assertNotIn('so‘m', sales['F5'].number_format)
        self.assertIn('so‘m', sales['J5'].number_format)
        self.assertEqual(summary['A8'].number_format, 'dd.mm.yyyy')
        self.assertEqual(wb['QARZLAR']['J4'].value, 'Davrdan oldingi qarz qoldig‘i')
        for sheet_name in wb.sheetnames:
            sheet = wb[sheet_name]
            self.assertTrue(sheet.freeze_panes)
            self.assertTrue(sheet.auto_filter.ref)

    def test_old_period_debt_is_separated_from_period_sales_balance(self):
        with closing(erp.db()) as con:
            con.execute(
                "INSERT INTO customers(id,name,phone,phone_key) VALUES(2,?,?,?)",
                ('Farq sinov', '+998 90 222 33 44', '998902223344'),
            )
            con.execute(
                """INSERT INTO opening_debts
                   (id,customer,customer_phone,amount,date,due_date,user_id)
                   VALUES(2,?,?,?,?,?,4)""",
                ('Farq sinov', '+998 90 222 33 44', 14500000,
                 '2024-12-01', '2025-01-15'),
            )
            con.execute(
                """INSERT INTO sales
                   (id,date,customer,customer_phone,due_date,product,qty_kg,
                    price_per_kg,unit_price,vat_rate,paid,note,user_id,
                    sale_mode,payment_method)
                   VALUES(20,'2025-01-21','Farq sinov','+998 90 222 33 44',
                    '2025-02-20','Sara',7418.76,1000,1000,0,5525000,'',2,
                    'kg','naqd')"""
            )
            con.commit()

        wb = self.workbook()
        sale_row = next(
            row for row in wb['JAMLANMA'].iter_rows(min_row=8, values_only=True)
            if row[1] == 'S-20')
        self.assertEqual(sale_row[5:9], (7418760, 5525000, 1893760, 'Qisman to‘langan'))
        debt_row = next(
            row for row in wb['QARZLAR'].iter_rows(min_row=5, values_only=True)
            if row[0] == 'Farq sinov')
        self.assertEqual(debt_row[5], 16393760)
        self.assertEqual(debt_row[9], 14500000)
        self.assertIn('boshlang‘ich qarzlar qoldig‘i: 14,530,000 so‘m',
                      wb['JAMLANMA']['A6'].value)
        self.assertIn('sotuvlar qoldig‘i: 2,013,760 so‘m',
                      wb['JAMLANMA']['A6'].value)
        self.assertIn('Q-2', debt_row[8])

    def test_all_dates_labels_opening_debt_without_prior_period_claim(self):
        with closing(erp.db()) as con:
            con.execute(
                """INSERT INTO opening_debts
                   (id,customer,customer_phone,amount,date,due_date,user_id)
                   VALUES(2,?,?,?,?,?,4)""",
                ('Eski nom', '+998 90 111 22 33', 14500000,
                 '2024-12-01', '2025-01-15'),
            )
            con.commit()

        wb = self.workbook('')
        note = wb['JAMLANMA']['A6'].value
        self.assertIn('boshlang‘ich qarzlar qoldig‘i: 14,530,000 so‘m', note)
        self.assertNotIn('oldingi davr', note.casefold())
        self.assertEqual(wb['QARZLAR']['J4'].value, 'Boshlang‘ich qarz qoldig‘i')
        debt_row = next(
            row for row in wb['QARZLAR'].iter_rows(min_row=5, values_only=True)
            if row[0] == 'Yangi nom')
        self.assertEqual(debt_row[9], 14530000)
        self.assertIn('Q-1', debt_row[8])

    def test_fully_paid_sale_status_uses_its_own_balance(self):
        with closing(erp.db()) as con:
            con.execute(
                """INSERT INTO sales
                   (id,date,customer,customer_phone,due_date,product,qty_kg,
                    price_per_kg,unit_price,vat_rate,paid,note,user_id,
                    sale_mode,payment_method)
                   VALUES(13,'2025-01-29','Eski nom','+998 90 111 22 33',
                    '2025-02-20','Sara',85,1000,1000,0,85000,'S-13 test',2,
                    'kg','naqd')"""
            )
            con.commit()

        wb = self.workbook()
        summary_sale = next(
            row for row in wb['JAMLANMA'].iter_rows(min_row=8, values_only=True)
            if row[1] == 'S-13')
        self.assertEqual(summary_sale[5:9], (85000, 85000, 0, 'To‘langan'))
        sales_sale = next(
            row for row in wb['SOTUVLAR'].iter_rows(min_row=5, values_only=True)
            if row[1] == 'S-13')
        self.assertEqual(sales_sale[14:16], (0, 'To‘langan'))
        customer = next(
            row for row in wb['MIJOZLAR'].iter_rows(min_row=5, values_only=True)
            if row[1] == 'Yangi nom')
        self.assertNotEqual(customer[7], 'To‘langan')

    def test_s13_floating_point_remainder_is_reported_as_paid(self):
        with closing(erp.db()) as con:
            con.execute(
                """INSERT INTO sales
                   (id,date,customer,customer_phone,due_date,product,qty_kg,
                    price_per_kg,unit_price,vat_rate,paid,note,user_id,
                    sale_mode,payment_method)
                   VALUES(13,'2025-01-29','Eski nom','+998 90 111 22 33',
                    '2025-02-20','Kepak',23,3695.6521739130435,3695.65,0,85000,'S-13 actual values',2,
                    'kg','naqd')"""
            )
            con.commit()

        wb = self.workbook()
        summary_sale = next(
            row for row in wb['JAMLANMA'].iter_rows(min_row=8, values_only=True)
            if row[1] == 'S-13')
        self.assertAlmostEqual(summary_sale[5], 85000, places=2)
        self.assertEqual(summary_sale[6:9], (85000, 0, 'To‘langan'))
        sales_sale = next(
            row for row in wb['SOTUVLAR'].iter_rows(min_row=5, values_only=True)
            if row[1] == 'S-13')
        self.assertEqual(sales_sale[14:16], (0, 'To‘langan'))

    def test_payment_rows_do_not_duplicate_accountant_receipts_or_future_events(self):
        wb = self.workbook()
        payments = wb['TO‘LOVLAR']
        payment_rows = list(payments.iter_rows(min_row=5, max_row=10, values_only=True))
        self.assertEqual(len(payment_rows), 6)
        self.assertEqual(sum(row[4] for row in payment_rows), 77000)
        accountant_receipt = next(row for row in payment_rows if row[1] == 'T-1')
        self.assertEqual(accountant_receipt[5], 'Plastik')
        self.assertEqual(accountant_receipt[6], 'Buxgalter')
        self.assertEqual(accountant_receipt[7], 'Umid')
        initial_receipt = next(row for row in payment_rows if row[1] == 'S-1-AVANS')
        self.assertEqual(initial_receipt[5], 'Naqd')
        self.assertEqual(initial_receipt[6], 'Buxgalter')
        self.assertEqual(initial_receipt[7], 'Umid')
        legacy = next(row for row in payment_rows if row[1] == 'P-1')
        self.assertIn('qarzdan chegirilmadi', legacy[8])
        self.assertFalse(any(row[1] == 'T-3' for row in payment_rows))
        maxmud_receipt = next(row for row in payment_rows if row[1] == 'T-2')
        self.assertEqual(maxmud_receipt[6], 'Maxmud')
        self.assertEqual(maxmud_receipt[7], 'Maxmud')
        batch_avans = next(row for row in payment_rows if row[1] == 'S-4-AVANS')
        self.assertEqual(batch_avans[5], 'Aniqlanmagan')
        self.assertIn('kanallar qayta qo‘shilmaydi', batch_avans[8])
        cancelled_avans = next(row for row in payment_rows if row[1] == 'S-6-AVANS')
        self.assertIn('qaytarilgan deb hisoblanmadi', cancelled_avans[8])

    def test_ui_replaces_financial_export_buttons(self):
        for path in ('/hisobot', '/boshliq/statistika'):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            html = response.get_data(as_text=True)
            self.assertIn('Yagona Excel hisobot', html)
            self.assertNotIn('Qarzdorlar Excel', html)
            self.assertNotIn('Sotuvlar Excel', html)
            self.assertNotIn('To‘lovlar Excel', html)

    def test_unbounded_and_invalid_date_ranges(self):
        wb = self.workbook('')
        self.assertEqual(
            wb.sheetnames,
            ['JAMLANMA', 'SOTUVLAR', 'QARZLAR', 'TO‘LOVLAR', 'MIJOZLAR'],
        )
        self.assertEqual(wb['JAMLANMA']['A5'].value, 190000)
        self.assertEqual(wb['JAMLANMA']['C5'].value, 84000)
        self.assertEqual(wb['JAMLANMA']['E5'].value, 148000)
        response = self.client.get(
            '/hisobot/yagona-excel?start=2025-01-31&end=2025-01-01')
        self.assertEqual(response.status_code, 400)

    def test_customer_identity_and_prior_debt_survive_period_filter(self):
        wb = self.workbook('start=2025-01-20&end=2025-01-31')
        customers = wb['MIJOZLAR']
        self.assertEqual(customers['B5'].value, 'Yangi nom')
        self.assertEqual(customers['A5'].value, 1)
        self.assertEqual(customers['D5'].value, 85000)
        self.assertEqual(customers['E5'].value, 27000)
        self.assertEqual(customers['F5'].value, 150000)
        self.assertEqual(wb['JAMLANMA']['A5'].value, 85000)
        self.assertEqual(wb['JAMLANMA']['E5'].value, 150000)
        debts = wb['QARZLAR']
        self.assertEqual(debts['A5'].value, 'Yangi nom')
        self.assertEqual(debts['F5'].value, 150000)
        self.assertIn('Q-1', debts['I5'].value)
        con = sqlite3.connect(erp.DB_PATH)
        try:
            self.assertEqual(
                con.execute('SELECT COUNT(*) FROM sales').fetchone()[0],
                7,
            )
        finally:
            con.close()


if __name__ == '__main__':
    unittest.main()
