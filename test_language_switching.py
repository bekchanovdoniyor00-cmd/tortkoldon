import html
import re
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import date
from io import BytesIO
from pathlib import Path

from flask import render_template, session as flask_session

sys.dont_write_bytecode = True
import app as erp
from openpyxl import load_workbook
from werkzeug.security import generate_password_hash
from ui_translations import translation_keys


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


class LanguageSwitchingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = erp.DB_PATH
        self.original_db = erp.db
        self.original_testing = erp.app.config['TESTING']
        self.original_backup = erp.backup_before_internal_cash_transfer_migration
        erp.DB_PATH = str(Path(self.temp_dir.name) / 'language_test.db')
        erp.db = lambda: ClosingConnection(self.original_db())
        erp.backup_before_internal_cash_transfer_migration = lambda: None
        erp.app.config['TESTING'] = True
        try:
            erp.init_db()
        finally:
            erp.backup_before_internal_cash_transfer_migration = self.original_backup
        with closing(erp.db()) as con:
            password = generate_password_hash('test-password')
            con.executemany(
                'INSERT INTO users(id,username,password_hash,role,active) VALUES(?,?,?,?,1)',
                [
                    (1, 'Accountant', password, 'buxgalter'),
                    (2, 'Manager', password, 'rahbar'),
                ],
            )
            con.commit()
        self.client = erp.app.test_client()

    def tearDown(self):
        erp.DB_PATH = self.original_db_path
        erp.db = self.original_db
        erp.backup_before_internal_cash_transfer_migration = self.original_backup
        erp.app.config['TESTING'] = self.original_testing
        self.temp_dir.cleanup()

    @staticmethod
    def csrf_token(response):
        match = re.search(rb'name="csrf_token" value="([^"]+)"', response.data)
        if not match:
            raise AssertionError('The rendered page does not contain a CSRF token.')
        return match.group(1).decode()

    def login(self, client, username):
        page = client.get('/login')
        response = client.post(
            '/login',
            data={
                'csrf_token': self.csrf_token(page),
                'username': username,
                'password': 'test-password',
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        return response

    def switch_language(self, client, language, next_path='/'):
        page = client.get(next_path)
        response = client.post(
            '/language',
            data={
                'csrf_token': self.csrf_token(page),
                'language': language,
                'next': next_path,
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        return response

    def database_snapshot(self):
        with closing(erp.db()) as con:
            schema = tuple(
                tuple(row)
                for row in con.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name"
                )
            )
            tables = [row['name'] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )]
            records = {
                table: tuple(
                    tuple(row) for row in con.execute(f'SELECT * FROM "{table}"')
                )
                for table in tables
            }
        return schema, records

    def test_default_latin_and_language_persists_across_refresh_and_login(self):
        login_page = self.client.get('/login')
        self.assertEqual(login_page.status_code, 200)
        self.assertIn('Tizimga kirish'.encode(), login_page.data)
        self.assertIn(b'<html lang="uz-Latn">', login_page.data)

        cyrillic = self.switch_language(self.client, 'uz-Cyrl', '/login')
        self.assertIn('Тизимга кириш'.encode(), cyrillic.data)
        self.assertIn('Синов нусхасида'.encode(), cyrillic.data)
        refreshed = self.client.get('/login')
        self.assertIn(b'<html lang="uz-Cyrl">', refreshed.data)

        dashboard = self.login(self.client, 'Accountant')
        self.assertIn('Тўловлар'.encode(), dashboard.data)
        self.assertIn('Созламалар'.encode(), dashboard.data)
        latin = self.switch_language(self.client, 'uz-Latn')
        self.assertIn(b'<html lang="uz-Latn">', latin.data)
        self.assertIn('Bosh sahifa'.encode(), latin.data)
        self.assertIn('Xaridor qarzlari'.encode(), latin.data)
        self.assertNotIn('Ягона Excel ҳисоботи'.encode(), latin.data)

    def test_login_errors_are_translated_and_language_survives_logout(self):
        cyrillic = self.switch_language(self.client, 'uz-Cyrl', '/login')
        response = self.client.post(
            '/login',
            data={
                'csrf_token': self.csrf_token(cyrillic),
                'username': 'Accountant',
                'password': 'incorrect',
            },
        )
        self.assertIn('Логин ёки парол нотўғри.'.encode(), response.data)

        self.login(self.client, 'Accountant')
        login_page = self.client.get('/')
        logged_out = self.client.post(
            '/logout',
            data={'csrf_token': self.csrf_token(login_page)},
            follow_redirects=True,
        )
        self.assertIn(b'<html lang="uz-Cyrl">', logged_out.data)

    def test_manager_and_accountant_menus_render_in_cyrillic(self):
        manager = erp.app.test_client()
        with manager.session_transaction() as user_session:
            user_session.update(
                user_id=2,
                username='Manager',
                role='rahbar',
                auth_version=0,
                ui_language='uz-Latn',
                csrf_token='manager-csrf-token',
            )
        manager_page = self.switch_language(manager, 'uz-Cyrl', '/boshliq/statistika')
        self.assertIn('Статистика'.encode(), manager_page.data)
        self.assertIn('Ягона Excel ҳисоботи'.encode(), manager_page.data)
        self.assertNotIn('Созламалар'.encode(), manager_page.data)

        accountant = erp.app.test_client()
        with accountant.session_transaction() as user_session:
            user_session.update(
                user_id=1,
                username='Accountant',
                role='buxgalter',
                auth_version=0,
                ui_language='uz-Latn',
                csrf_token='accountant-csrf-token',
            )
        accountant_page = self.switch_language(accountant, 'uz-Cyrl')
        self.assertIn('Созламалар'.encode(), accountant_page.data)
        self.assertIn('Захира нусхасини юклаш'.encode(), accountant_page.data)

    def test_language_selection_does_not_change_database_schema_or_records(self):
        before = self.database_snapshot()

        page = self.client.get('/login')
        switched = self.client.post(
            '/language',
            data={
                'csrf_token': self.csrf_token(page),
                'language': 'uz-Cyrl',
                'next': 'https://example.invalid/',
            },
        )
        self.assertEqual(switched.status_code, 302)
        self.assertEqual(switched.headers['Location'], '/')

        self.assertEqual(before, self.database_snapshot())

    def test_repeated_language_switches_preserve_product_customer_sales_and_debts(self):
        products = (
            'Sara', 'Yevro', 'Zar Bug‘doy', 'Robiya',
            'Kepak', 'Qora un', 'Muchka', 'Telashka atxod',
        )
        customer = "Mijoz O'zgarmas / Мижоз Ўзгармас"
        phone = '+998 90 123 45 67'
        today = date.today().isoformat()

        with closing(erp.db()) as con:
            con.execute(
                'INSERT INTO customers(id,name,phone,phone_key) VALUES(1,?,?,?)',
                (customer, phone, '998901234567'),
            )
            category_numbers = {}
            for sale_id, product in enumerate(products, start=1):
                con.execute(
                    """INSERT INTO sales
                       (id,date,customer,customer_phone,due_date,product,qty_kg,
                        price_per_kg,unit_price,vat_rate,paid,note,user_id,
                        sale_mode,bag_kg,bag_count,payment_method)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'kg',NULL,NULL,'naqd')""",
                    (
                        sale_id, today, customer, phone, '2099-12-31',
                        product, 10, 1000, 1000, 0, 0,
                        'Language preservation fixture', 1,
                    ),
                )
                category = erp.INVOICE_GROUPS[product][0]
                category_numbers[category] = category_numbers.get(category, 0) + 1
                con.execute(
                    """INSERT INTO invoices
                       (id,sale_id,category,month_key,number,issued_date)
                       VALUES(?,?,?,?,?,?)""",
                    (
                        sale_id, sale_id, category, today[:7],
                        category_numbers[category], today,
                    ),
                )
            con.execute(
                """INSERT INTO opening_debts
                   (id,customer,customer_phone,amount,date,due_date,user_id)
                   VALUES(1,?,?,25000,?,?,1)""",
                (customer, phone, today, '2099-12-31'),
            )
            con.commit()

        client = erp.app.test_client()
        self.login(client, 'Accountant')
        expected_db = self.database_snapshot()

        for language in ('uz-Cyrl', 'uz-Latn', 'uz-Cyrl', 'uz-Latn'):
            with self.subTest(language=language):
                switched = self.switch_language(client, language, '/sotuv')
                self.assertIn(f'<html lang="{language}">'.encode(), switched.data)

                page_expectations = {
                    '/': (True, products[-5:]),
                    '/sotuv': (True, products),
                    '/qarzdorlar': (True, ()),
                    '/omborlar': (False, products),
                    '/nakladnoylar': (True, products),
                }
                for path, (shows_customer, visible_products) in page_expectations.items():
                    page = client.get(path)
                    self.assertEqual(page.status_code, 200, path)
                    page_text = html.unescape(page.get_data(as_text=True))
                    if shows_customer:
                        self.assertTrue(
                            customer in page_text,
                            f'{path} did not preserve the customer display name',
                        )
                    for product in visible_products:
                        self.assertTrue(
                            product in page_text,
                            f'{path} did not preserve product name {product!r}',
                        )

                for sale_id, product in enumerate(products, start=1):
                    invoice = client.get(f'/nakladnoy/{sale_id}')
                    self.assertEqual(invoice.status_code, 200)
                    invoice_text = html.unescape(invoice.get_data(as_text=True))
                    self.assertTrue(customer in invoice_text)
                    self.assertTrue(product in invoice_text)

                report = client.get('/hisobot/yagona-excel')
                self.assertEqual(report.status_code, 200)
                workbook = load_workbook(BytesIO(report.data), read_only=True)
                self.assertEqual(
                    workbook.sheetnames,
                    ['JAMLANMA', 'SOTUVLAR', 'QARZLAR', 'TO‘LOVLAR', 'MIJOZLAR'],
                )
                sales_rows = list(
                    workbook['SOTUVLAR'].iter_rows(min_row=5, values_only=True)
                )
                customer_rows = list(
                    workbook['MIJOZLAR'].iter_rows(min_row=5, values_only=True)
                )
                debt_rows = list(
                    workbook['QARZLAR'].iter_rows(min_row=5, values_only=True)
                )
                self.assertEqual(
                    {row[4] for row in sales_rows if row[4] is not None},
                    set(products),
                )
                self.assertIn(customer, {row[2] for row in sales_rows})
                self.assertIn(customer, {row[1] for row in customer_rows})
                self.assertIn(customer, {row[0] for row in debt_rows})
                workbook.close()
                self.assertEqual(expected_db, self.database_snapshot())

    def test_stage_two_pages_translate_static_ui_and_preserve_dynamic_values(self):
        customer = "Stage2 Customer / Мижоз"
        phone = '+998 90 555 12 34'
        today = date.today().isoformat()
        with closing(erp.db()) as con:
            con.execute(
                'INSERT INTO users(id,username,password_hash,role,active) VALUES(3,?,?,?,1)',
                ('Umid', generate_password_hash('test-password'), 'umid'),
            )
            con.execute(
                'INSERT INTO customers(id,name,phone,phone_key) VALUES(1,?,?,?)',
                (customer, phone, '998905551234'),
            )
            con.execute(
                """INSERT INTO sales
                   (id,date,customer,customer_phone,due_date,product,qty_kg,
                    price_per_kg,unit_price,vat_rate,paid,note,user_id,
                    sale_mode,bag_kg,bag_count,payment_method)
                   VALUES(1,?,?,?,?,?,25,1000,1000,0,0,'stage two fixture',3,
                          'kg',NULL,NULL,'naqd')""",
                (today, customer, phone, '2099-12-31', 'Sara'),
            )
            con.commit()

        accountant = self.client
        self.login(accountant, 'Accountant')
        umid = erp.app.test_client()
        with umid.session_transaction() as user_session:
            user_session.update(
                user_id=3,
                username='Umid',
                role='umid',
                auth_version=0,
                ui_language='uz-Latn',
                csrf_token='umid-csrf-token',
            )

        before = self.database_snapshot()
        accountant_pages = {
            '/sotuv': ('Sotuvlar tarixi', 'Sotuvni saqlash'),
            '/qarzdorlar': ('Xaridor qarzlari', 'Jami qolgan qarz:'),
            '/tolovlar?worker_id=3': ('Qarz to‘lovlari — buxgalter', 'To‘lov turi'),
            '/omborlar': ('Omborlar', 'Mahsulotlar qoldig‘i'),
            '/kassa': ('kunlik pul qoldig‘i', 'Hisoblangan qoldiq'),
            '/xodimlar/nazorat': ('Xodimlar kassasi', 'Pul topshirish so‘rovlari'),
        }
        for path, latin_text in accountant_pages.items():
            response = accountant.get(path)
            self.assertEqual(response.status_code, 200, path)
            page = html.unescape(response.get_data(as_text=True))
            for expected in latin_text:
                self.assertIn(expected, page, path)

        latin = self.switch_language(accountant, 'uz-Cyrl', '/sotuv')
        self.assertIn('Сотувлар тарихи'.encode(), latin.data)
        cyrillic_pages = {
            '/sotuv': ('Сотувлар тарихи', 'Сотувни сақлаш'),
            '/qarzdorlar': ('Харидор қарзлари', 'Жами қолган қарз:'),
            '/tolovlar?worker_id=3': ('Қарз тўловлари — бухгалтер', 'Тўлов тури'),
            '/omborlar': ('Омборлар', 'Маҳсулотлар қолдиғи'),
            '/kassa': ('Корхона бўйича кунлик пул қолдиғи', 'Ҳисобланган қолдиқ'),
            '/xodimlar/nazorat': ('Ходимлар кассаси', 'Пул топшириш сўровлари'),
        }
        for path, expected_text in cyrillic_pages.items():
            response = accountant.get(path)
            self.assertEqual(response.status_code, 200, path)
            page = html.unescape(response.get_data(as_text=True))
            for expected in expected_text:
                self.assertIn(expected, page, path)
            if path in ('/sotuv', '/qarzdorlar', '/tolovlar?worker_id=3'):
                self.assertIn(customer, page, path)
            if path in ('/sotuv', '/omborlar'):
                self.assertIn('Sara', page, path)
            if path == '/sotuv':
                self.assertIn('value="Sara">Sara', page)
                self.assertIn('value="naqd">Нақд', page)
            if path == '/tolovlar?worker_id=3':
                self.assertIn(f'value="{customer}"', page)
                self.assertIn('Umid', page)

        production_latin = umid.get('/ishlab-chiqarish')
        self.assertEqual(production_latin.status_code, 200)
        self.assertIn('Ishlab chiqarish va tozalash'.encode(), production_latin.data)
        production = self.switch_language(umid, 'uz-Cyrl', '/ishlab-chiqarish')
        self.assertEqual(production.status_code, 200)
        production_text = html.unescape(production.get_data(as_text=True))
        self.assertIn('Ишлаб чиқариш ва тозалаш', production_text)
        self.assertIn('Sara', production_text)
        self.assertIn('Yevro', production_text)
        self.assertIn('Zar Bug‘doy', production_text)
        self.assertIn('Robiya', production_text)
        self.assertIn('Kepak', production_text)
        self.assertIn('Muchka', production_text)
        self.assertIn('Telashka atxod', production_text)
        self.assertIn('value="Sara"', production_text)

        cash = umid.get('/xodim/kassa')
        self.assertEqual(cash.status_code, 200)
        cash_text = html.unescape(cash.get_data(as_text=True))
        self.assertIn('Алоҳида ҳисоблар', cash_text)
        self.assertIn('Йўл ҳақи', cash_text)
        self.assertIn('value="Yo‘l haqi"', cash_text)
        self.assertIn('name="payment_method"', cash_text)
        self.assertIn('value="naqd">💵 Нақд', cash_text)
        latin_cash = self.switch_language(umid, 'uz-Latn', '/xodim/kassa')
        self.assertIn('Alohida hisoblar'.encode(), latin_cash.data)
        self.assertIn('value="Yo‘l haqi">Yo‘l haqi'.encode(), latin_cash.data)
        latin_production = umid.get('/ishlab-chiqarish')
        self.assertIn('Ishlab chiqarish va tozalash'.encode(), latin_production.data)
        self.assertIn('Sara', html.unescape(latin_production.get_data(as_text=True)))

        self.assertEqual(before, self.database_snapshot())

    def test_employee_pages_translate_for_umid_and_maxmud_without_changing_records(self):
        today = date.today().isoformat()
        employees = (
            (3, 'Umid', 'umid', 'Umid mijoz', '998901112233', 'Sara'),
            (4, 'Maxmud', 'maxmud', 'Maxmud mijoz', '998904445566', 'Kepak'),
        )
        with closing(erp.db()) as con:
            password = generate_password_hash('test-password')
            con.executemany(
                'INSERT INTO users(id,username,password_hash,role,active) VALUES(?,?,?,?,1)',
                [(user_id, username, password, role)
                 for user_id, username, role, *_ in employees],
            )
            category_numbers = {}
            for sale_id, (user_id, username, role, customer, phone, product) in enumerate(
                employees, start=1
            ):
                customer_id = sale_id
                con.execute(
                    'INSERT INTO customers(id,name,phone,phone_key) VALUES(?,?,?,?)',
                    (customer_id, customer, phone, phone.replace('+', '').replace(' ', '')),
                )
                con.execute(
                    """INSERT INTO sales
                       (id,date,customer,customer_phone,due_date,product,qty_kg,
                        price_per_kg,unit_price,vat_rate,paid,note,user_id,
                        sale_mode,bag_kg,bag_count,payment_method)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'kg',NULL,NULL,'naqd')""",
                    (
                        sale_id, today, customer, phone, '2099-12-31',
                        product, 10, 1000, 1000, 0, 0,
                        'Employee localization fixture', user_id,
                    ),
                )
                batch_id = sale_id
                con.execute(
                    """INSERT INTO worker_sale_batches
                       (id,worker_id,customer,customer_phone,sale_date,total,paid)
                       VALUES(?,?,?,?,?,?,?)""",
                    (batch_id, user_id, customer, phone, today, 10000, 0),
                )
                con.execute(
                    'INSERT INTO worker_sale_batch_items(batch_id,sale_id) VALUES(?,?)',
                    (batch_id, sale_id),
                )
                category = erp.INVOICE_GROUPS[product][0]
                category_numbers[category] = category_numbers.get(category, 0) + 1
                con.execute(
                    """INSERT INTO invoices
                       (id,sale_id,category,month_key,number,issued_date)
                       VALUES(?,?,?,?,?,?)""",
                    (sale_id, sale_id, category, today[:7], category_numbers[category], today),
                )
            con.commit()

        before = self.database_snapshot()
        page_expectations = {
            '/xodim/sotuvlar': 'employee.dashboard_intro',
            '/xodim/yangi-sotuv-sahifa': 'employee.items_section',
            '/xodim/qarzlar': 'employee.payment_intro',
            '/xodim/arxiv': 'employee.archive_title',
            '/xodim/xaridorlar': 'cash.worker.nav.customers',
            '/xodim/kassa': 'cash.worker.expense',
            '/xodim/sotuv-natijasi/{invoice_id}': 'employee.sale_saved',
            '/xodim/yangi-nakladnoy/{invoice_id}': 'employee.invoice_title',
        }
        dynamic_customer_paths = {
            '/xodim/qarzlar', '/xodim/arxiv',
            '/xodim/yangi-nakladnoy/{invoice_id}',
        }
        dynamic_phone_paths = {
            '/xodim/qarzlar',
            '/xodim/yangi-nakladnoy/{invoice_id}',
        }
        dynamic_product_paths = {
            '/xodim/sotuvlar', '/xodim/yangi-sotuv-sahifa', '/xodim/arxiv',
            '/xodim/yangi-nakladnoy/{invoice_id}',
        }

        for user_id, username, role, customer, phone, product in employees:
            invoice_id = user_id - 2
            client = erp.app.test_client()
            with client.session_transaction() as user_session:
                user_session.update(
                    user_id=user_id,
                    username=username,
                    role=role,
                    auth_version=0,
                    ui_language='uz-Latn',
                    csrf_token=f'{username.lower()}-csrf-token',
                )
            for language in ('uz-Cyrl', 'uz-Latn', 'uz-Cyrl'):
                self.switch_language(client, language, '/xodim/sotuvlar')
                for path_template, translation_key in page_expectations.items():
                    path = path_template.format(invoice_id=invoice_id)
                    response = client.get(path)
                    self.assertEqual(response.status_code, 200, (username, language, path))
                    page = html.unescape(response.get_data(as_text=True))
                    self.assertIn(
                        erp.translate(translation_key, language),
                        page,
                        (username, language, path),
                    )
                    if path_template not in {
                        '/xodim/sotuv-natijasi/{invoice_id}',
                        '/xodim/yangi-nakladnoy/{invoice_id}',
                    }:
                        self.assertIn(username, page, (username, language, path))
                    if path_template in dynamic_customer_paths:
                        self.assertIn(customer, page, (username, language, path))
                    if path_template in dynamic_phone_paths:
                        self.assertIn(phone, page, (username, language, path))
                    if path_template in dynamic_product_paths:
                        self.assertIn(product, page, (username, language, path))
                    opposite_script_name = 'Мижоз' if language == 'uz-Cyrl' else 'мижоз'
                    self.assertNotIn(opposite_script_name, page, (username, language, path))
                customer_search = client.get(f'/xodim/xaridor-qidirish?q={phone}')
                self.assertEqual(customer_search.status_code, 200)
                self.assertEqual(
                    customer_search.json['customers'][0]['name'],
                    customer,
                )
                self.assertEqual(
                    customer_search.json['customers'][0]['phone'],
                    phone,
                )
                with erp.app.test_request_context('/xodim/sotuvlar'):
                    flask_session.update(
                        user_id=user_id,
                        username=username,
                        role=role,
                        ui_language=language,
                        csrf_token=f'{username.lower()}-csrf-token',
                    )
                    legacy_page = html.unescape(
                        render_template('worker_sales.html', **erp.worker_context())
                    )
                self.assertIn(
                    erp.translate('employee.sales_and_receipts', language),
                    legacy_page,
                )
                self.assertIn(customer, legacy_page)
                self.assertIn(product, legacy_page)
                self.assertIn(username, legacy_page)
                self.assertIn(
                    f'value="Yo‘l haqi">{erp.translate("employee.expense_category.travel", language)}',
                    legacy_page,
                )

        self.assertEqual(before, self.database_snapshot())

    def test_catalog_has_both_translations_for_every_key(self):
        for key in translation_keys():
            with self.subTest(key=key):
                self.assertTrue(erp.translate(key, 'uz-Latn'))
                self.assertTrue(erp.translate(key, 'uz-Cyrl'))


if __name__ == '__main__':
    unittest.main()
