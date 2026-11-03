import ipaddress
import json
import os
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from production_server import ProductionConfigError, REQUIRED_TABLES
from linux_wsgi import (
    TrustedProxyMiddleware,
    _install_external_backup,
    create_application,
)


class LinuxWsgiTests(unittest.TestCase):
    def test_application_startup_requires_explicit_production_environment(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ProductionConfigError, 'ERP_MODE'):
                create_application()
        with patch.dict(os.environ, {'ERP_MODE': 'production'}, clear=True):
            with self.assertRaisesRegex(ProductionConfigError, 'ERP_SECRET_FILE'):
                create_application()

    def test_proxy_headers_are_applied_only_for_trusted_proxy_address(self):
        captured = []

        def application(environ, start_response):
            captured.append(
                (
                    environ.get('REMOTE_ADDR'),
                    environ.get('wsgi.url_scheme'),
                    environ.get('HTTP_HOST'),
                )
            )
            start_response('200 OK', [('Content-Length', '0')])
            return [b'']

        middleware = TrustedProxyMiddleware(
            application,
            (ipaddress.ip_network('127.0.0.1/32'),),
            1,
        )
        response_statuses = []

        def start_response(status, headers):
            response_statuses.append((status, headers))

        trusted = {
            'REMOTE_ADDR': '127.0.0.1',
            'HTTP_X_FORWARDED_FOR': '203.0.113.8',
            'HTTP_X_FORWARDED_PROTO': 'https',
            'HTTP_X_FORWARDED_HOST': 'tortkoldon.uz',
        }
        middleware(trusted, start_response)
        self.assertEqual(captured[-1], ('203.0.113.8', 'https', 'tortkoldon.uz'))

        untrusted = {
            'REMOTE_ADDR': '203.0.113.9',
            'HTTP_X_FORWARDED_FOR': '203.0.113.8',
            'HTTP_X_FORWARDED_PROTO': 'https',
            'HTTP_X_FORWARDED_HOST': 'tortkoldon.uz',
        }
        middleware(untrusted, start_response)
        self.assertEqual(captured[-1], ('203.0.113.9', None, None))
        self.assertEqual(
            response_statuses,
            [
                ('200 OK', [('Content-Length', '0')]),
                ('200 OK', [('Content-Length', '0')]),
            ],
        )

    def test_application_backups_use_external_configured_directory(self):
        with TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / 'erp.sqlite'
            backups = Path(temp_dir) / 'backups'
            application = SimpleNamespace()
            config = SimpleNamespace(
                database_path=database,
                backup_directory=backups,
            )
            result_path = backups / 'tortkuldon_snapshot_UTC.sqlite'
            with patch(
                'linux_wsgi.create_backup',
                return_value={'path': result_path},
            ) as create_backup:
                _install_external_backup(application, config)
                self.assertEqual(application.make_backup(), result_path)

            create_backup.assert_called_once_with(
                database,
                backups,
                retention_days=30,
            )

    def test_valid_production_config_bootstraps_against_disposable_database(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            database = root / 'disposable.sqlite'
            backup_dir = root / 'backups'
            backup_dir.mkdir()
            log_dir = root / 'logs'
            log_dir.mkdir()
            secret_file = root / 'session_secret'
            secret_file.write_text('temporary-linux-bootstrap-secret-123456', encoding='utf-8')
            config_file = root / 'production.json'

            with closing(sqlite3.connect(database)) as connection, connection:
                for table in sorted(REQUIRED_TABLES):
                    if table == 'users':
                        connection.execute(
                            'CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, role TEXT)'
                        )
                        connection.execute(
                            "INSERT INTO users (username, role) VALUES ('temporary', 'rahbar')"
                        )
                    else:
                        connection.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY)')

            config = {
                'mode': 'production',
                'database_path': str(database),
                'backup_directory': str(backup_dir),
                'trusted_hosts': ['tortkoldon.uz', '127.0.0.1'],
                'https_termination': True,
                'proxy': {
                    'enabled': True,
                    'trusted_hops': 1,
                    'remote_addresses': ['127.0.0.1'],
                },
                'session_lifetime_seconds': 3600,
                'drain_timeout_seconds': 45,
                'login_rate_limit': {
                    'attempts': 5,
                    'window_seconds': 900,
                    'ip_attempts': 30,
                    'ip_window_seconds': 900,
                },
                'waitress': {
                    'host': '127.0.0.1',
                    'port': 8000,
                    'threads': 8,
                },
                'service_logging': {
                    'path': str(log_dir / 'app.log'),
                    'max_bytes': 1048576,
                    'backup_count': 2,
                },
            }
            config_file.write_text(json.dumps(config), encoding='utf-8')
            environment = os.environ.copy()
            environment.update(
                {
                    'ERP_MODE': 'production',
                    'ERP_CONFIG_PATH': str(config_file),
                    'ERP_SECRET_FILE': str(secret_file),
                    'PYTHONDONTWRITEBYTECODE': '1',
                }
            )
            environment.pop('ERP_SECRET_KEY', None)
            invalid_config = dict(config)
            invalid_config['database_path'] = str(root / 'missing.sqlite')
            config_file.write_text(json.dumps(invalid_config), encoding='utf-8')
            fail_closed_script = (
                'import sys\n'
                'from linux_wsgi import create_application\n'
                'from production_server import ProductionConfigError\n'
                'try:\n'
                ' create_application()\n'
                'except ProductionConfigError as error:\n'
                ' assert "Configured database file does not exist" in str(error)\n'
                ' assert "app" not in sys.modules\n'
                ' print("INVALID_DATABASE_BLOCKED_BEFORE_APP_IMPORT")\n'
                'else: raise AssertionError("Invalid database was accepted")'
            )
            invalid_result = subprocess.run(
                [sys.executable, '-c', fail_closed_script],
                cwd=Path(__file__).resolve().parent,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(invalid_result.returncode, 0, invalid_result.stderr)
            self.assertIn(
                'INVALID_DATABASE_BLOCKED_BEFORE_APP_IMPORT',
                invalid_result.stdout,
            )

            config_file.write_text(json.dumps(config), encoding='utf-8')
            database_before = database.read_bytes()
            script = (
                'from pathlib import Path; '
                'from linux_wsgi import TrustedProxyMiddleware, create_application; '
                'application = create_application(); '
                'import app; '
                'assert isinstance(application, TrustedProxyMiddleware); '
                f'assert Path(app.DB_PATH).resolve() == Path({str(database)!r}).resolve(); '
                'assert app.app.config["SESSION_COOKIE_SECURE"] is True; '
                'assert app.app.config["SESSION_COOKIE_HTTPONLY"] is True; '
                'assert app.app.config["SESSION_COOKIE_SAMESITE"] == "Lax"; '
                'print("LINUX_WSGI_BOOTSTRAP_OK")'
            )
            result = subprocess.run(
                [sys.executable, '-c', script],
                cwd=Path(__file__).resolve().parent,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('LINUX_WSGI_BOOTSTRAP_OK', result.stdout)
            self.assertTrue(database.is_file())
            self.assertEqual(database.read_bytes(), database_before)


if __name__ == '__main__':
    unittest.main()
