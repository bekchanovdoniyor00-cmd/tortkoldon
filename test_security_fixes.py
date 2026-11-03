import os
import re
import tempfile
import unittest
import gc
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault(
    'SECRET_KEY', 'temporary-security-fix-tests-only-secret-key'
)

import app as erp
from production_server import prepare_application
from sqlite_backup import BackupError, verify_backup


class SecurityFixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory(
            prefix='erp_security_fix_test_'
        )
        cls.root = Path(cls.temp_dir.name)
        cls.database = cls.root / 'test.sqlite'
        cls.backup_directory = cls.root / 'protected_backups'
        cls.backup_directory.mkdir()
        cls.config = SimpleNamespace(
            secret_key='temporary-security-fix-config-secret',
            database_path=cls.database,
            backup_directory=cls.backup_directory,
            mode='test',
            session_lifetime_seconds=3600,
            trusted_hosts=('127.0.0.1', 'localhost'),
            login_attempts=5,
            login_window_seconds=900,
            ip_attempts=30,
            ip_window_seconds=900,
        )
        erp.DB_PATH = str(cls.database)
        erp.app.secret_key = cls.config.secret_key
        erp.init_db()
        cls.user_ids = {}
        connection = erp.db()
        try:
            for username, role in (
                ('test_accountant', 'buxgalter'),
                ('test_manager', 'rahbar'),
                ('test_umid', 'umid'),
                ('test_maxmud', 'maxmud'),
            ):
                cls.user_ids[role] = connection.execute(
                    '''INSERT INTO users(username,password_hash,role,phone)
                       VALUES(?,?,?,'')''',
                    (
                        username,
                        erp.generate_password_hash('Old-Testing-Password-2026!'),
                        role,
                    ),
                ).lastrowid
            connection.commit()
        finally:
            connection.close()
        prepare_application(cls.config)

    @classmethod
    def tearDownClass(cls):
        gc.collect()
        last_error = None
        for _ in range(20):
            try:
                cls.temp_dir.cleanup()
                last_error = None
                break
            except PermissionError as exc:
                last_error = exc
                gc.collect()
                time.sleep(0.1)
        if last_error is not None:
            raise last_error

    def setUp(self):
        connection = erp.db()
        try:
            for role, username in (
                ('buxgalter', 'test_accountant'),
                ('rahbar', 'test_manager'),
                ('umid', 'test_umid'),
                ('maxmud', 'test_maxmud'),
            ):
                connection.execute(
                    'UPDATE users SET password_hash=?,active=1 WHERE username=?',
                    (
                        erp.generate_password_hash('Old-Testing-Password-2026!'),
                        username,
                    ),
                )
            connection.execute('DELETE FROM user_auth_versions')
            connection.execute('DELETE FROM user_audit')
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _csrf(client, path):
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

    def _login(self, client, role, password='Old-Testing-Password-2026!'):
        csrf = self._csrf(client, '/login')
        response = client.post('/login', data={
            'csrf_token': csrf,
            'username': {
                'buxgalter': 'test_accountant',
                'rahbar': 'test_manager',
                'umid': 'test_umid',
                'maxmud': 'test_maxmud',
            }[role],
            'password': password,
        })
        self.assertEqual(response.status_code, 302)
        return response

    def test_self_service_password_change_revokes_all_old_sessions(self):
        first_session = erp.app.test_client()
        second_session = erp.app.test_client()
        self._login(first_session, 'umid')
        self._login(second_session, 'umid')

        csrf = self._csrf(first_session, '/parol')
        changed = first_session.post('/parol', data={
            'csrf_token': csrf,
            'old_password': 'Old-Testing-Password-2026!',
            'new_password': 'New-Testing-Password-2026!',
        })
        self.assertEqual(changed.status_code, 302)
        self.assertTrue(changed.headers['Location'].endswith('/login'))
        self.assertTrue(first_session.get('/parol').headers['Location'].endswith('/login'))
        self.assertTrue(second_session.get('/parol').headers['Location'].endswith('/login'))

        new_session = erp.app.test_client()
        self._login(
            new_session, 'umid', password='New-Testing-Password-2026!'
        )
        self.assertEqual(new_session.get('/parol').status_code, 200)

        invalid_session = erp.app.test_client()
        csrf = self._csrf(invalid_session, '/login')
        rejected = invalid_session.post('/login', data={
            'csrf_token': csrf,
            'username': 'test_umid',
            'password': 'Old-Testing-Password-2026!',
        })
        self.assertEqual(rejected.status_code, 200)
        with invalid_session.session_transaction() as session:
            self.assertNotIn('user_id', session)

    def test_accountant_reset_revokes_target_sessions_but_not_own(self):
        accountant = erp.app.test_client()
        target_first = erp.app.test_client()
        target_second = erp.app.test_client()
        self._login(accountant, 'buxgalter')
        self._login(target_first, 'maxmud')
        self._login(target_second, 'maxmud')

        csrf = self._csrf(accountant, '/foydalanuvchilar')
        response = accountant.post('/foydalanuvchilar', data={
            'csrf_token': csrf,
            'action': 'reset',
            'user_id': str(self.user_ids['maxmud']),
            'new_password': 'Reset-Testing-Password-2026!',
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(accountant.get('/foydalanuvchilar').status_code, 200)
        self.assertTrue(target_first.get('/parol').headers['Location'].endswith('/login'))
        self.assertTrue(target_second.get('/parol').headers['Location'].endswith('/login'))

        reset_session = erp.app.test_client()
        self._login(
            reset_session, 'maxmud', password='Reset-Testing-Password-2026!'
        )
        self.assertEqual(reset_session.get('/parol').status_code, 200)

    def test_all_roles_can_login_and_logout_normally(self):
        for role in ('buxgalter', 'rahbar', 'umid', 'maxmud'):
            with self.subTest(role=role):
                client = erp.app.test_client()
                self._login(client, role)
                self.assertEqual(client.get('/parol').status_code, 200)
                csrf = self._csrf(client, '/parol')
                logout = client.post('/logout', data={'csrf_token': csrf})
                self.assertEqual(logout.status_code, 302)
                self.assertTrue(client.get('/parol').headers['Location'].endswith('/login'))

    def test_production_backup_uses_configured_directory_and_surfaces_errors(self):
        existing_backup = self.backup_directory / (
            'tortkuldon_20000101_000000_000000_UTC.sqlite'
        )
        existing_backup.write_bytes(b'preserve existing backup')

        backup_path = erp.make_backup()
        self.assertEqual(backup_path.parent, self.backup_directory.resolve())
        self.assertTrue(backup_path.is_file())
        self.assertEqual(verify_backup(backup_path)['integrity_check'], 'ok')
        self.assertEqual(existing_backup.read_bytes(), b'preserve existing backup')

        with mock.patch(
            'sqlite_backup.create_backup',
            side_effect=BackupError('simulated backup failure'),
        ):
            with self.assertRaisesRegex(BackupError, 'simulated backup failure'):
                erp.make_backup()

    def test_auth_version_schema_migration_backs_up_and_preserves_users(self):
        connection = erp.db()
        try:
            before = connection.execute(
                'SELECT id,username,password_hash,role FROM users ORDER BY id'
            ).fetchall()
            connection.execute('DROP TABLE user_auth_versions')
            connection.commit()
        finally:
            connection.close()

        erp.init_db()

        connection = erp.db()
        try:
            after = connection.execute(
                'SELECT id,username,password_hash,role FROM users ORDER BY id'
            ).fetchall()
            self.assertEqual([tuple(row) for row in after], [tuple(row) for row in before])
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM sqlite_master "
                    "WHERE type='table' AND name='user_auth_versions'"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute('PRAGMA integrity_check').fetchone()[0],
                'ok',
            )
        finally:
            connection.close()
        backups = list(self.backup_directory.glob('tortkuldon_*.sqlite'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(verify_backup(backups[0])['integrity_check'], 'ok')


if __name__ == '__main__':
    unittest.main()
