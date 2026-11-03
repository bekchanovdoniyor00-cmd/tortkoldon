import gc
import os
import re
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

os.environ['SECRET_KEY'] = 'isolated-waitress-tests-only'

import app as erp


class WaitressIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix='erp_waitress_test_')
        self.database_path = Path(self.temp_dir.name) / 'test.sqlite'
        erp.DB_PATH = str(self.database_path)
        original_db = erp.db

        @contextmanager
        def closing_db():
            connection = original_db()
            try:
                with connection:
                    yield connection
            finally:
                connection.close()

        self.original_db = original_db
        erp.db = closing_db
        erp.init_db()
        self.users = {
            'buxgalter': ('test_buxgalter', 'Buxgalter-Only-Test-2026!'),
            'rahbar': ('test_rahbar', 'Rahbar-Only-Test-2026!'),
            'umid': ('test_umid', 'Umid-Only-Test-2026!'),
            'maxmud': ('test_maxmud', 'Maxmud-Only-Test-2026!'),
        }
        with erp.db() as connection:
            for role, (username, password) in self.users.items():
                connection.execute(
                    'INSERT INTO users(username,password_hash,role,phone) VALUES(?,?,?,?)',
                    (
                        username,
                        erp.generate_password_hash(password),
                        role,
                        '',
                    ),
                )

    def tearDown(self):
        erp.db = self.original_db
        gc.collect()
        self.temp_dir.cleanup()

    @staticmethod
    def csrf_token(client_or_response):
        if hasattr(client_or_response, 'get_data'):
            response = client_or_response
        else:
            response = client_or_response.get('/login')
        match = re.search(
            r'name="csrf_token" value="([^"]+)"',
            response.get_data(as_text=True),
        )
        if not match:
            raise AssertionError('Login sahifasidan CSRF token topilmadi.')
        return match.group(1)

    def login(self, client, role):
        username, password = self.users[role]
        response = client.post(
            '/login',
            data={
                'username': username,
                'password': password,
                'csrf_token': self.csrf_token(client),
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        return response

    def test_all_four_profiles_login_open_and_logout(self):
        expected_destinations = {
            'buxgalter': '/',
            'rahbar': '/boshliq/statistika',
            'umid': '/xodim/sotuvlar',
            'maxmud': '/xodim/sotuvlar',
        }
        for role, expected_path in expected_destinations.items():
            with self.subTest(role=role):
                client = erp.app.test_client()
                response = self.login(client, role)
                self.assertEqual(response.request.path, expected_path)
                self.assertIn(b'<html', response.data.lower())

                logout = client.post(
                    '/logout',
                    data={'csrf_token': self.csrf_token(client.get(expected_path))},
                    follow_redirects=True,
                )
                self.assertEqual(logout.status_code, 200)
                unauthenticated = client.get('/')
                self.assertEqual(unauthenticated.status_code, 302)
                self.assertTrue(unauthenticated.headers['Location'].endswith('/login'))

    def test_language_switch_persists_and_can_switch_back(self):
        client = erp.app.test_client()
        self.login(client, 'buxgalter')
        token = self.csrf_token(client)

        response = client.post(
            '/language',
            data={'language': 'uz-Cyrl', 'next': '/', 'csrf_token': token},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        with client.session_transaction() as session:
            self.assertEqual(session['ui_language'], 'uz-Cyrl')
        self.assertEqual(client.get('/').status_code, 200)

        token = self.csrf_token(client)
        response = client.post(
            '/language',
            data={'language': 'uz-Latn', 'next': '/', 'csrf_token': token},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        with client.session_transaction() as session:
            self.assertEqual(session['ui_language'], 'uz-Latn')

    def test_pwa_manifest_service_worker_and_icon(self):
        client = erp.app.test_client()
        manifest = client.get('/static/manifest.webmanifest')
        worker = client.get('/service-worker.js')
        icon = client.get('/static/pwa-icon-192.png')
        try:
            self.assertEqual(manifest.status_code, 200)
            self.assertEqual(worker.status_code, 200)
            self.assertEqual(icon.status_code, 200)
            self.assertIn(b'"display": "standalone"', manifest.data)
            self.assertIn(b'fetch(request).catch(', worker.data)
            self.assertIn(b'Ma\xe2\x80\x99lumotlar saqlanmadi.', worker.data)
        finally:
            manifest.close()
            worker.close()
            icon.close()

    def test_concurrent_temporary_database_writes(self):
        with erp.db() as connection:
            connection.execute(
                'CREATE TABLE waitress_concurrency_probe (value INTEGER NOT NULL)'
            )

        def write_batch(batch):
            with erp.db() as connection:
                connection.execute('BEGIN IMMEDIATE')
                connection.executemany(
                    'INSERT INTO waitress_concurrency_probe(value) VALUES(?)',
                    [(batch * 20 + item,) for item in range(20)],
                )

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(write_batch, range(8)))

        with erp.db() as connection:
            count = connection.execute(
                'SELECT COUNT(*) FROM waitress_concurrency_probe'
            ).fetchone()[0]
        self.assertEqual(count, 160)


if __name__ == '__main__':
    unittest.main()
