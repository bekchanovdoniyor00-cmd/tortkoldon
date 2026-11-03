import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from windows_service_host import DrainMiddleware


APP_ROOT = Path(__file__).resolve().parent
PYTHON = Path(sys.executable)
BASE_URL = 'http://127.0.0.1:5053'
TEST_SECRET = 'isolated-service-test-secret-0123456789'
TEST_PASSWORD = 'Temporary-Service-Password-2026!'


def file_digest(path):
    digest = hashlib.sha256()
    with path.open('rb') as database_file:
        for chunk in iter(lambda: database_file.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


class DrainMiddlewareTests(unittest.TestCase):
    def test_existing_request_drains_and_new_request_gets_503(self):
        release_response = threading.Event()
        completed = []

        def application(environ, start_response):
            start_response('200 OK', [('Content-Length', '2')])

            def response_body():
                release_response.wait(timeout=5)
                yield b'ok'
                completed.append(True)

            return response_body()

        middleware = DrainMiddleware(application)
        first_status = []
        first_response = middleware({}, lambda status, headers, exc_info=None: first_status.append(status))
        consumed = []
        consumer = threading.Thread(target=lambda: consumed.extend(first_response))
        consumer.start()
        time.sleep(0.05)
        middleware.stop_accepting()

        stopped_status = []
        stopped_response = middleware(
            {},
            lambda status, headers, exc_info=None: stopped_status.append((status, headers)),
        )
        self.assertEqual(stopped_status[0][0], '503 Service Unavailable')
        self.assertEqual(b''.join(stopped_response), b'Service shutting down.')
        self.assertFalse(middleware.wait_for_drain(0.05, lambda: 0))

        release_response.set()
        consumer.join(timeout=3)
        self.assertFalse(consumer.is_alive())
        self.assertEqual(consumed, [b'ok'])
        self.assertEqual(completed, [True])
        self.assertTrue(middleware.wait_for_drain(1, lambda: 0))


class WindowsServiceIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory(prefix='erp_service_5053_')
        cls.root = Path(cls.temp_dir.name)
        cls.database_path = cls.root / 'service_test.sqlite'
        cls.backups = cls.root / 'backups'
        cls.backups.mkdir()
        cls.log_path = cls.root / 'service.log'
        cls.config_path = cls.root / 'service_config.json'
        cls._create_database()
        cls.config = {
            'mode': 'test',
            'database_path': str(cls.database_path),
            'backup_directory': str(cls.backups),
            'trusted_hosts': ['127.0.0.1', 'localhost'],
            'https_termination': False,
            'proxy': {
                'enabled': False,
                'trusted_hops': 0,
                'remote_addresses': [],
            },
            'session_lifetime_seconds': 3600,
            'drain_timeout_seconds': 10,
            'login_rate_limit': {
                'attempts': 5,
                'window_seconds': 900,
                'ip_attempts': 30,
                'ip_window_seconds': 900,
            },
            'waitress': {
                'host': '127.0.0.1',
                'port': 5053,
                'threads': 4,
            },
            'service_logging': {
                'path': str(cls.log_path),
                'max_bytes': 1048576,
                'backup_count': 2,
            },
        }
        cls._write_config()
        cls.env = os.environ.copy()
        cls.env['ERP_MODE'] = 'test'
        cls.env['ERP_SECRET_KEY'] = TEST_SECRET
        cls.env['PYTHONDONTWRITEBYTECODE'] = '1'
        cls.database_before = file_digest(cls.database_path)
        cls.process = None

    @classmethod
    def _create_database(cls):
        setup_script = (
            'import os,sys; '
            f'sys.path.insert(0, {str(APP_ROOT)!r}); '
            "os.environ['SECRET_KEY']='fixture-only-session-key'; "
            'import app; '
            f'app.DB_PATH={str(cls.database_path)!r}; '
            'app.init_db(); '
            'connection=app.db(); '
            'connection.execute('
            "'INSERT INTO users(username,password_hash,role,phone) VALUES(?,?,?,?)', "
            f"('service_test',app.generate_password_hash({TEST_PASSWORD!r}),'buxgalter','')); "
            'connection.commit(); connection.close()'
        )
        subprocess.run(
            [str(PYTHON), '-c', setup_script],
            cwd=APP_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )

    @classmethod
    def _write_config(cls):
        cls.config_path.write_text(
            json.dumps(cls.config),
            encoding='utf-8',
        )

    @classmethod
    def _start(cls):
        cls.process = subprocess.Popen(
            [
                str(PYTHON),
                '-u',
                str(APP_ROOT / 'windows_service_host.py'),
                '--config',
                str(cls.config_path),
                '--test-mode',
            ],
            cwd=APP_ROOT,
            env=cls.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if cls.process.poll() is not None:
                raise AssertionError(
                    f'Service test process exited with {cls.process.returncode}. '
                    f'Log: {cls.log_path.read_text(encoding="utf-8", errors="replace")}'
                )
            try:
                with urlopen(f'{BASE_URL}/login', timeout=1) as response:
                    if response.status == 200:
                        return
            except (OSError, URLError):
                time.sleep(0.1)
        raise AssertionError('Temporary service did not become ready on 127.0.0.1:5053.')

    @classmethod
    def _stop(cls):
        subprocess.run(
            [
                str(PYTHON),
                str(APP_ROOT / 'windows_service_host.py'),
                '--stop',
                '--config',
                str(cls.config_path),
            ],
            cwd=APP_ROOT,
            env=cls.env,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        cls.process.wait(timeout=25)
        cls.assert_process_stopped()
        cls.process = None

    @classmethod
    def assert_process_stopped(cls):
        if cls.process.returncode != 0:
            raise AssertionError(
                f'Service host exited with {cls.process.returncode}. '
                f'Log: {cls.log_path.read_text(encoding="utf-8", errors="replace")}'
            )

    @classmethod
    def tearDownClass(cls):
        if cls.process is not None and cls.process.poll() is None:
            try:
                cls._stop()
            except (OSError, subprocess.SubprocessError):
                cls.process.kill()
                cls.process.wait(timeout=10)
        if cls.database_path.exists() and file_digest(cls.database_path) != cls.database_before:
            raise AssertionError('Service startup or restart modified the isolated DB.')
        cls.temp_dir.cleanup()

    def test_bad_database_fails_closed_before_binding(self):
        original_database = self.config['database_path']
        self.config['database_path'] = str(self.root / 'missing.sqlite')
        self._write_config()
        result = subprocess.run(
            [
                str(PYTHON),
                str(APP_ROOT / 'windows_service_host.py'),
                '--config',
                str(self.config_path),
                '--test-mode',
            ],
            cwd=APP_ROOT,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.config['database_path'] = original_database
        self._write_config()
        self.assertEqual(result.returncode, 2)
        self.assertIn('Configured database file does not exist', result.stderr)
        with socket.socket() as probe:
            self.assertNotEqual(probe.connect_ex(('127.0.0.1', 5053)), 0)

    def test_service_template_uses_graceful_stop_and_manual_install_gates(self):
        xml_path = APP_ROOT / 'ToRtkoERP.xml.example'
        service = ET.parse(xml_path).getroot()
        self.assertEqual(service.findtext('startmode'), 'Automatic')
        self.assertEqual(service.findtext('stoptimeout'), '90sec')
        self.assertIn('--stop --config', service.findtext('stoparguments'))
        self.assertEqual(service.findtext('serviceaccount/prompt'), 'console')
        env_values = {
            entry.get('name'): entry.get('value')
            for entry in service.findall('env')
        }
        self.assertEqual(env_values['ERP_MODE'], 'production')
        self.assertEqual(env_values['ERP_SECRET_FILE'], r'C:\ERP\secrets\erp_session_secret.txt')

        install_script = (APP_ROOT / 'install_windows_service.bat').read_text(encoding='utf-8')
        remove_script = (APP_ROOT / 'remove_windows_service.bat').read_text(encoding='utf-8')
        self.assertIn('INSTALL-APPROVED', install_script)
        self.assertIn('REMOVE-APPROVED', remove_script)

    def test_missing_secret_fails_closed_before_binding(self):
        env = self.env.copy()
        env.pop('ERP_SECRET_KEY', None)
        env.pop('ERP_SECRET_FILE', None)
        result = subprocess.run(
            [
                str(PYTHON),
                str(APP_ROOT / 'windows_service_host.py'),
                '--config',
                str(self.config_path),
                '--test-mode',
            ],
            cwd=APP_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn('ERP_SECRET_KEY', result.stderr)
        with socket.socket() as probe:
            self.assertNotEqual(probe.connect_ex(('127.0.0.1', 5053)), 0)

    def test_service_stops_and_restarts_without_db_writes_or_secret_logs(self):
        for _ in range(2):
            self._start()
            with urlopen(f'{BASE_URL}/login', timeout=5) as response:
                self.assertEqual(response.status, 200)
            self._stop()
            with socket.socket() as probe:
                self.assertNotEqual(probe.connect_ex(('127.0.0.1', 5053)), 0)

        logs = self.log_path.read_text(encoding='utf-8', errors='replace')
        self.assertIn('Graceful shutdown completed.', logs)
        self.assertNotIn(TEST_SECRET, logs)
        self.assertNotIn(TEST_PASSWORD, logs)
        self.assertIn('127.0.0.1:5053', logs)
        self.assertEqual(file_digest(self.database_path), self.database_before)


if __name__ == '__main__':
    unittest.main()
