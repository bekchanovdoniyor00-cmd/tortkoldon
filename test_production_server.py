import hashlib
import gc
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPCookieProcessor, build_opener

from production_server import ProductionConfigError, load_runtime_config


APP_ROOT = Path(__file__).resolve().parent
PYTHON = Path(sys.executable)
BASE_URL = 'http://127.0.0.1:5053'


def write_config(path, database_path, backup_directory, mode='test', **overrides):
    config = {
        'mode': mode,
        'database_path': str(database_path),
        'backup_directory': str(backup_directory),
        'trusted_hosts': ['127.0.0.1', 'localhost'],
        'https_termination': mode == 'production',
        'proxy': {
            'enabled': mode == 'production',
            'trusted_hops': 1 if mode == 'production' else 0,
            'remote_addresses': ['127.0.0.1'] if mode == 'production' else [],
        },
        'session_lifetime_seconds': 3600,
        'drain_timeout_seconds': 10,
        'login_rate_limit': {
            'attempts': 2,
            'window_seconds': 900,
            'ip_attempts': 30,
            'ip_window_seconds': 900,
        },
        'waitress': {'host': '127.0.0.1', 'port': 5053, 'threads': 8},
        'service_logging': {
            'path': str(path.parent / 'service.log'),
            'max_bytes': 1048576,
            'backup_count': 2,
        },
    }
    config.update(overrides)
    path.write_text(json.dumps(config), encoding='utf-8')
    return config


def database_digest(path):
    digest = hashlib.sha256()
    with path.open('rb') as database_file:
        for chunk in iter(lambda: database_file.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


class ProductionServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory(prefix='erp_production_test_')
        cls.root = Path(cls.temp_dir.name)
        cls.database_path = cls.root / 'erp_test.sqlite'
        cls.backup_directory = cls.root / 'backups'
        cls.backup_directory.mkdir()
        cls.config_path = cls.root / 'test_config.json'
        write_config(
            cls.config_path,
            cls.database_path,
            cls.backup_directory,
        )
        cls.production_config_path = cls.root / 'production_config.json'
        write_config(
            cls.production_config_path,
            cls.database_path,
            cls.backup_directory,
            mode='production',
        )
        cls.credentials = {
            'test_buxgalter': {'role': 'buxgalter', 'password': 'Buxgalter-Testing-2026!'},
            'test_rahbar': {'role': 'rahbar', 'password': 'Rahbar-Testing-2026!'},
            'test_umid': {'role': 'umid', 'password': 'Umid-Testing-2026!'},
            'test_maxmud': {'role': 'maxmud', 'password': 'Maxmud-Testing-2026!'},
        }
        cls.credentials_path = cls.root / 'credentials.json'
        cls.credentials_path.write_text(json.dumps(cls.credentials), encoding='utf-8')
        setup_script = (
            'import os,sys; '
            f'sys.path.insert(0, {str(APP_ROOT)!r}); '
            "os.environ['SECRET_KEY']='temporary-fixture-session-secret-only'; "
            'import app; '
            f'app.DB_PATH={str(cls.database_path)!r}; '
            'app.init_db(); '
            'connection=app.db(); '
            'connection.executemany('
            "'INSERT INTO users(username,password_hash,role,phone) VALUES(?,?,?,?)', "
            '['
            "('test_buxgalter',app.generate_password_hash('Buxgalter-Testing-2026!'),'buxgalter',''),"
            "('test_rahbar',app.generate_password_hash('Rahbar-Testing-2026!'),'rahbar',''),"
            "('test_umid',app.generate_password_hash('Umid-Testing-2026!'),'umid',''),"
            "('test_maxmud',app.generate_password_hash('Maxmud-Testing-2026!'),'maxmud','')"
            ']); connection.commit(); connection.close()'
        )
        subprocess.run(
            [str(PYTHON), '-c', setup_script],
            cwd=APP_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        cls.database_before = database_digest(cls.database_path)
        cls.env = os.environ.copy()
        cls.env['ERP_SECRET_KEY'] = 'temporary-test-key-that-is-at-least-32-chars'
        cls.env['ERP_MODE'] = 'test'
        cls.env.pop('SECRET_KEY', None)
        cls.env['PYTHONDONTWRITEBYTECODE'] = '1'
        cls.log_path = cls.root / 'server.log'
        cls.log_file = cls.log_path.open('w+b')
        cls.process = subprocess.Popen(
            [
                str(PYTHON),
                '-u',
                str(APP_ROOT / 'production_server.py'),
                '--config',
                str(cls.config_path),
            ],
            cwd=APP_ROOT,
            env=cls.env,
            stdout=cls.log_file,
            stderr=subprocess.STDOUT,
        )
        cls._wait_until_ready()

    @classmethod
    def _wait_until_ready(cls):
        deadline = time.monotonic() + 20
        last_error = None
        while time.monotonic() < deadline:
            if cls.process.poll() is not None:
                cls.log_file.flush()
                cls.log_file.seek(0)
                raise AssertionError(
                    f'Test Waitress process exited: {cls.log_file.read().decode(errors="replace")}'
                )
            try:
                response = build_opener().open(urljoin(BASE_URL, '/login'), timeout=1)
                if response.status == 200:
                    response.close()
                    return
                response.close()
            except (OSError, URLError) as exc:
                last_error = exc
            time.sleep(0.2)
        raise AssertionError(f'Waitress test server did not start: {last_error}')

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, 'process', None) is not None and cls.process.poll() is None:
            cls.process.terminate()
            try:
                cls.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                cls.process.kill()
                cls.process.wait(timeout=10)
        if getattr(cls, 'log_file', None) is not None:
            cls.log_file.close()
        if getattr(cls, 'database_path', None) and cls.database_path.exists():
            if database_digest(cls.database_path) != cls.database_before:
                raise AssertionError('The normal production server startup changed its DB.')
        last_cleanup_error = None
        for _ in range(20):
            try:
                cls.temp_dir.cleanup()
                last_cleanup_error = None
                break
            except PermissionError as exc:
                last_cleanup_error = exc
                gc.collect()
                time.sleep(0.1)
        if last_cleanup_error is not None:
            raise last_cleanup_error

    @staticmethod
    def opener():
        return build_opener(HTTPCookieProcessor(CookieJar()))

    @staticmethod
    def read_response(response):
        try:
            return response.status, response.read(), response.geturl(), response.headers
        finally:
            response.close()

    def get_csrf(self, client, path='/login'):
        status, body, _, _ = self.read_response(
            client.open(urljoin(BASE_URL, path), timeout=5)
        )
        self.assertEqual(status, 200)
        import re

        match = re.search(rb'name="csrf_token" value="([^"]+)"', body)
        self.assertIsNotNone(match)
        return match.group(1).decode('ascii')

    def post(self, client, path, values):
        return client.open(
            urljoin(BASE_URL, path),
            data=urlencode(values).encode('utf-8'),
            timeout=5,
        )

    def login(self, client, username, password):
        return self.post(
            client,
            '/login',
            {
                'username': username,
                'password': password,
                'csrf_token': self.get_csrf(client),
            },
        )

    def test_all_roles_login_logout_and_csrf(self):
        destinations = {
            'buxgalter': '/',
            'rahbar': '/boshliq/statistika',
            'umid': '/xodim/sotuvlar',
            'maxmud': '/xodim/sotuvlar',
        }
        for username, credentials in self.credentials.items():
            with self.subTest(role=credentials['role']):
                client = self.opener()
                response = self.login(client, username, credentials['password'])
                status, body, final_url, _ = self.read_response(response)
                self.assertEqual(status, 200)
                self.assertEqual(urlsplit(final_url).path, destinations[credentials['role']])
                self.assertIn(b'<html', body.lower())

                with self.assertRaises(HTTPError) as missing_csrf:
                    self.post(client, '/logout', {}).read()
                try:
                    self.assertEqual(missing_csrf.exception.code, 400)
                finally:
                    missing_csrf.exception.close()

                token = self.get_csrf(client, destinations[credentials['role']])
                status, _, final_url, _ = self.read_response(
                    self.post(client, '/logout', {'csrf_token': token})
                )
                self.assertEqual(status, 200)
                self.assertTrue(urlsplit(final_url).path.endswith('/login'))

    def test_language_pwa_and_authenticated_parallel_requests(self):
        client = self.opener()
        response = self.login(
            client,
            'test_buxgalter',
            self.credentials['test_buxgalter']['password'],
        )
        self.assertEqual(self.read_response(response)[0], 200)

        token = self.get_csrf(client, '/')
        status, body, _, _ = self.read_response(
            self.post(
                client,
                '/language',
                {'language': 'uz-Cyrl', 'next': '/', 'csrf_token': token},
            )
        )
        self.assertEqual(status, 200)
        self.assertIn('uz-Cyrl'.encode(), body)

        for path in (
            '/static/manifest.webmanifest',
            '/service-worker.js',
            '/static/pwa-icon-192.png',
            '/static/pwa-icon-512.png',
        ):
            status, _, _, _ = self.read_response(
                build_opener().open(urljoin(BASE_URL, path), timeout=5)
            )
            self.assertEqual(status, 200)

        with socket.create_connection(('127.0.0.1', 5053), timeout=2):
            pass

        def fetch_dashboard(_):
            return self.read_response(client.open(urljoin(BASE_URL, '/'), timeout=5))[0]

        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(fetch_dashboard, range(16)))
        self.assertEqual(results, [200] * 16)

    def test_login_rate_limit_and_retry_after(self):
        client = self.opener()
        for _ in range(2):
            response = self.login(client, 'unknown_test_account', 'wrong-password')
            self.assertEqual(response.status, 200)
            response.close()

        with self.assertRaises(HTTPError) as limited:
            self.login(client, 'unknown_test_account', 'wrong-password').read()
        self.assertEqual(limited.exception.code, 429)
        self.assertTrue(limited.exception.headers.get('Retry-After'))
        limited.exception.close()

    def test_production_cookie_flags_https_and_permanent_session(self):
        probe = f'''
import re
import sys

sys.path.insert(0, {str(APP_ROOT)!r})
from production_server import load_runtime_config, prepare_application

config = load_runtime_config({str(self.production_config_path)!r})
application = prepare_application(config)
client = application.test_client()
response = client.get('/login', base_url='https://127.0.0.1')
assert response.status_code == 200
cookie = response.headers.get('Set-Cookie', '')
assert 'Secure' in cookie and 'HttpOnly' in cookie and 'SameSite=Lax' in cookie, cookie
token = re.search(rb'name="csrf_token" value="([^"]+)"', response.data).group(1).decode()
response = client.post(
    '/login',
    base_url='https://127.0.0.1',
    data={{
        'username': 'test_buxgalter',
        'password': 'Buxgalter-Testing-2026!',
        'csrf_token': token,
    }},
)
assert response.status_code == 302, response.status_code
assert 'Secure' in response.headers.get('Set-Cookie', '')
with client.session_transaction(base_url='https://127.0.0.1') as user_session:
    assert user_session.permanent
assert application.permanent_session_lifetime.total_seconds() == 3600
assert client.get('/login', base_url='http://127.0.0.1').status_code == 400
'''
        result = subprocess.run(
            [str(PYTHON), '-c', probe],
            cwd=APP_ROOT,
            env={**self.env, 'ERP_MODE': 'production'},
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_forwarded_headers_are_trusted_only_from_configured_proxy(self):
        from waitress.proxy_headers import proxy_headers_middleware

        observed = {}

        def capture(environ, start_response):
            observed.update(
                remote=environ['REMOTE_ADDR'],
                scheme=environ['wsgi.url_scheme'],
                host=environ.get('HTTP_HOST'),
                forwarded_for=environ.get('HTTP_X_FORWARDED_FOR'),
            )
            start_response('200 OK', [('Content-Length', '0')])
            return [b'']

        middleware = proxy_headers_middleware(
            capture,
            trusted_proxy='127.0.0.1',
            trusted_proxy_count=1,
            trusted_proxy_headers={
                'x-forwarded-for', 'x-forwarded-proto', 'x-forwarded-host'
            },
            clear_untrusted=True,
        )
        environ = {
            'REMOTE_ADDR': '127.0.0.1',
            'wsgi.url_scheme': 'http',
            'HTTP_HOST': '127.0.0.1:5053',
            'HTTP_X_FORWARDED_FOR': '203.0.113.8',
            'HTTP_X_FORWARDED_PROTO': 'https',
            'HTTP_X_FORWARDED_HOST': 'erp.example.test',
        }
        middleware(environ, lambda status, headers: None)
        self.assertEqual(observed['remote'], '203.0.113.8')
        self.assertEqual(observed['scheme'], 'https')
        self.assertEqual(observed['host'], 'erp.example.test')

        observed.clear()
        environ['REMOTE_ADDR'] = '192.0.2.9'
        environ['wsgi.url_scheme'] = 'http'
        environ['HTTP_HOST'] = '127.0.0.1:5053'
        environ['HTTP_X_FORWARDED_PROTO'] = 'https'
        middleware(environ, lambda status, headers: None)
        self.assertEqual(observed['remote'], '192.0.2.9')
        self.assertEqual(observed['scheme'], 'http')
        self.assertEqual(observed['host'], '127.0.0.1:5053')
        self.assertIsNone(observed['forwarded_for'])

    def test_controlled_migration_requires_explicit_gate_and_creates_backup(self):
        config_path = self.root / 'migration_config.json'
        migration_db = self.root / 'migration_test.sqlite'
        setup_script = (
            'import os,sys; '
            f'sys.path.insert(0, {str(APP_ROOT)!r}); '
            "os.environ['SECRET_KEY']='temporary-migration-fixture-secret'; "
            'import app; '
            f'app.DB_PATH={str(migration_db)!r}; '
            'app.init_db(); '
            'connection=app.db(); '
            'connection.execute('
            "'INSERT INTO users(username,password_hash,role,phone) VALUES(?,?,?,?)', "
            "('migration_test',app.generate_password_hash('Migration-Testing-2026!'),'buxgalter','')); "
            "connection.execute('CREATE TABLE migration_marker(value TEXT)'); "
            "connection.execute(\"INSERT INTO migration_marker VALUES('preserve')\"); "
            'connection.commit(); connection.close()'
        )
        subprocess.run(
            [str(PYTHON), '-c', setup_script],
            cwd=APP_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        write_config(config_path, migration_db, self.backup_directory)
        before = database_digest(migration_db)

        env = self.env.copy()
        env.pop('ERP_ALLOW_MIGRATIONS', None)
        env.pop('ERP_MAINTENANCE_CONFIRMED', None)
        blocked = subprocess.run(
            [str(PYTHON), str(APP_ROOT / 'migrate_database.py'), '--config', str(config_path)],
            cwd=APP_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(blocked.returncode, 2)
        self.assertEqual(database_digest(migration_db), before)
        self.assertEqual(list(self.backup_directory.iterdir()), [])

        env['ERP_ALLOW_MIGRATIONS'] = 'I_UNDERSTAND_BACKUP_AND_MIGRATE'
        maintenance_blocked = subprocess.run(
            [str(PYTHON), str(APP_ROOT / 'migrate_database.py'), '--config', str(config_path)],
            cwd=APP_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(maintenance_blocked.returncode, 2)
        self.assertEqual(database_digest(migration_db), before)
        self.assertEqual(list(self.backup_directory.iterdir()), [])

        env['ERP_MAINTENANCE_CONFIRMED'] = 'I_CONFIRMED_ALL_ERP_WRITERS_ARE_STOPPED'
        migrated = subprocess.run(
            [str(PYTHON), str(APP_ROOT / 'migrate_database.py'), '--config', str(config_path)],
            cwd=APP_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        self.assertIn('MIGRATION_OK', migrated.stdout)
        backups = list(self.backup_directory.glob('tortkuldon_*.sqlite'))
        self.assertEqual(len(backups), 1)


class ProductionConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix='erp_config_test_')
        self.root = Path(self.temp_dir.name)
        self.db = self.root / 'config_test.sqlite'
        connection = __import__('sqlite3').connect(self.db)
        connection.execute('CREATE TABLE marker(value INTEGER)')
        connection.commit()
        connection.close()
        self.backups = self.root / 'backups'
        self.backups.mkdir()
        self.config_path = self.root / 'config.json'

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_missing_secret_fails_closed(self):
        write_config(self.config_path, self.db, self.backups)
        with self.assertRaisesRegex(ProductionConfigError, 'ERP_SECRET_KEY'):
            load_runtime_config(
                str(self.config_path),
                environ={},
                verify_schema=False,
            )

    def test_protected_secret_file_is_supported_without_logging_its_contents(self):
        secret = 'fixture-secret-file-value-that-is-long-enough'
        secret_file = self.root / 'session-secret.txt'
        secret_file.write_text(secret, encoding='utf-8')
        write_config(self.config_path, self.db, self.backups)
        config = load_runtime_config(
            str(self.config_path),
            environ={'ERP_SECRET_FILE': str(secret_file)},
            verify_schema=False,
        )
        self.assertEqual(config.secret_key, secret)
        self.assertNotIn(secret, repr(config))

    def test_secret_file_must_be_external_and_readable(self):
        write_config(self.config_path, self.db, self.backups)
        with self.assertRaisesRegex(ProductionConfigError, 'Could not read the protected secret file'):
            load_runtime_config(
                str(self.config_path),
                environ={'ERP_SECRET_FILE': str(self.root / 'missing-secret.txt')},
                verify_schema=False,
            )

    def test_secret_inside_json_is_rejected(self):
        config = write_config(self.config_path, self.db, self.backups)
        config['secret_key'] = 'never-allow-this'
        self.config_path.write_text(json.dumps(config), encoding='utf-8')
        with self.assertRaisesRegex(ProductionConfigError, 'must not be stored'):
            load_runtime_config(
                str(self.config_path),
                environ={'ERP_SECRET_KEY': 'a' * 40},
                verify_schema=False,
            )

    def test_production_requires_https_and_exact_trusted_proxy(self):
        write_config(self.config_path, self.db, self.backups, mode='production')
        with self.assertRaisesRegex(ProductionConfigError, 'schema is incomplete'):
            load_runtime_config(
                str(self.config_path),
                environ={'ERP_SECRET_KEY': 'a' * 40},
                verify_schema=True,
            )

        connection = __import__('sqlite3').connect(self.db)
        for table in (
            'users', 'sales', 'payments', 'customers', 'production',
            'production_flour_types', 'bagged_production_lines', 'opening_debts',
            'worker_receipts', 'accountant_worker_receipts', 'account_cash_transfers',
            'opening_product_stocks', 'opening_bagged_product_stocks',
            'invoices', 'expenses', 'wheat_purchase_payments',
            'accountant_cash_additions', 'user_auth_versions',
        ):
            connection.execute(f'CREATE TABLE {table}(id INTEGER PRIMARY KEY)')
        connection.execute("INSERT INTO users DEFAULT VALUES")
        connection.commit()
        connection.close()

        config = json.loads(self.config_path.read_text(encoding='utf-8'))
        config['proxy']['remote_addresses'] = ['127.0.0.0/8']
        self.config_path.write_text(json.dumps(config), encoding='utf-8')
        with self.assertRaisesRegex(ProductionConfigError, 'exact trusted proxy IP'):
            load_runtime_config(
                str(self.config_path),
                environ={'ERP_SECRET_KEY': 'a' * 40},
            )

    def test_environment_mode_must_match_runtime_config(self):
        write_config(self.config_path, self.db, self.backups, mode='test')
        with self.assertRaisesRegex(ProductionConfigError, 'does not match'):
            load_runtime_config(
                str(self.config_path),
                environ={
                    'ERP_SECRET_KEY': 'a' * 40,
                    'ERP_MODE': 'production',
                },
                verify_schema=False,
            )

    def test_startup_script_never_defaults_secret_or_database(self):
        result = subprocess.run(
            [str(PYTHON), str(APP_ROOT / 'production_server.py')],
            cwd=APP_ROOT,
            env={**os.environ, 'ERP_CONFIG_PATH': '', 'ERP_SECRET_KEY': ''},
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn('SERVER_STARTUP_BLOCKED', result.stderr)
        self.assertNotIn('tortkuldon.db', result.stderr)


if __name__ == '__main__':
    unittest.main()
