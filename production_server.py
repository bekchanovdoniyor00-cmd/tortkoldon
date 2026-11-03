import argparse
import hashlib
import ipaddress
import json
import logging
import logging.handlers
import os
import sys
import sqlite3
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent
REQUIRED_TABLES = {
    'users',
    'sales',
    'payments',
    'customers',
    'production',
    'production_flour_types',
    'bagged_production_lines',
    'opening_debts',
    'worker_receipts',
    'accountant_worker_receipts',
    'account_cash_transfers',
    'opening_product_stocks',
    'opening_bagged_product_stocks',
    'invoices',
    'expenses',
    'wheat_purchase_payments',
    'user_auth_versions',
}


class ProductionConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeConfig:
    mode: str
    config_path: Path
    database_path: Path
    backup_directory: Path
    trusted_hosts: tuple[str, ...]
    proxy_enabled: bool
    proxy_hops: int
    proxy_networks: tuple
    session_lifetime_seconds: int
    login_attempts: int
    login_window_seconds: int
    ip_attempts: int
    ip_window_seconds: int
    host: str
    port: int
    threads: int
    log_path: Path
    log_max_bytes: int
    log_backup_count: int
    drain_timeout_seconds: int
    secret_key: str = field(repr=False)


def _is_within(path, directory):
    try:
        path.relative_to(directory)
        return True
    except ValueError:
        return False


def _absolute_path(value, field_name, config_path):
    if not isinstance(value, str) or not value.strip():
        raise ProductionConfigError(f'{field_name} must be a non-empty absolute path.')
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ProductionConfigError(f'{field_name} must be absolute: {value!r}')
    return path.resolve(strict=False)


def _positive_integer(value, field_name, minimum=1, maximum=86400):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ProductionConfigError(
            f'{field_name} must be an integer from {minimum} to {maximum}.'
        )
    return value


def _validate_database(path, verify_schema=True):
    if not path.is_file():
        raise ProductionConfigError(f'Configured database file does not exist: {path}')
    try:
        connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=10)
        try:
            connection.execute('PRAGMA query_only=ON')
            connection.execute('PRAGMA busy_timeout=10000')
            integrity = connection.execute('PRAGMA integrity_check').fetchone()
            if integrity is None or integrity[0] != 'ok':
                result = integrity[0] if integrity else 'no result'
                raise ProductionConfigError(
                    f'Database integrity check failed for {path}: {result}'
                )
            foreign_keys = connection.execute('PRAGMA foreign_key_check').fetchmany(10)
            if foreign_keys:
                raise ProductionConfigError(
                    f'Database has foreign-key violations: {foreign_keys!r}'
                )
            if verify_schema:
                tables = {
                    row[0] for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                missing = sorted(REQUIRED_TABLES - tables)
                if missing:
                    raise ProductionConfigError(
                        'Database schema is incomplete; server startup never runs '
                        f'migrations. Missing tables: {", ".join(missing)}. '
                        'Run a separately approved migration.'
                    )
                if connection.execute('SELECT COUNT(*) FROM users').fetchone()[0] < 1:
                    raise ProductionConfigError(
                        'Database has no users; refusing to start the production server.'
                    )
        finally:
            connection.close()
    except ProductionConfigError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise ProductionConfigError(f'Could not validate database {path}: {exc}') from exc


def load_runtime_config(path, environ=None, require_secret=True, verify_schema=True):
    environ = os.environ if environ is None else environ
    if not path:
        raise ProductionConfigError(
            'Set ERP_CONFIG_PATH or pass --config to an explicit runtime config file.'
        )
    config_path = Path(path).expanduser()
    if not config_path.is_absolute():
        raise ProductionConfigError('Runtime config path must be absolute.')
    try:
        config_path = config_path.resolve(strict=True)
        with config_path.open(encoding='utf-8') as config_file:
            raw = json.load(config_file)
    except (OSError, json.JSONDecodeError) as exc:
        raise ProductionConfigError(f'Could not read runtime config: {exc}') from exc
    if not isinstance(raw, dict):
        raise ProductionConfigError('Runtime config must be a JSON object.')
    if 'secret_key' in raw or 'SECRET_KEY' in raw:
        raise ProductionConfigError(
            'Secret keys must not be stored in JSON config; use ERP_SECRET_KEY.'
        )

    mode = raw.get('mode')
    if mode not in ('production', 'test'):
        raise ProductionConfigError('mode must be exactly "production" or "test".')
    environment_mode = environ.get('ERP_MODE')
    if environment_mode and environment_mode != mode:
        raise ProductionConfigError(
            'ERP_MODE does not match the explicit runtime configuration mode.'
        )
    secret = environ.get('ERP_SECRET_KEY', '')
    secret_file = environ.get('ERP_SECRET_FILE', '')
    if not secret and secret_file:
        raw_secret_path = Path(secret_file).expanduser()
        if not raw_secret_path.is_absolute():
            raise ProductionConfigError('ERP_SECRET_FILE must be an absolute path.')
        try:
            secret_path = raw_secret_path.resolve(strict=True)
            if not secret_path.is_file():
                raise ProductionConfigError('ERP_SECRET_FILE must point to a regular file.')
            if _is_within(secret_path, APP_ROOT):
                raise ProductionConfigError(
                    'ERP_SECRET_FILE must be outside the application code tree.'
                )
            secret = secret_path.read_text(encoding='utf-8').rstrip('\r\n')
        except (OSError, UnicodeError) as exc:
            raise ProductionConfigError(
                f'Could not read the protected secret file: {type(exc).__name__}.'
            ) from exc
    if require_secret and (not isinstance(secret, str) or len(secret) < 32):
        raise ProductionConfigError(
            'Supply ERP_SECRET_KEY through the protected process environment or '
            'ERP_SECRET_FILE through a protected absolute file path; the key must '
            'contain at least 32 characters.'
        )
    if not secret:
        secret = 'migration-process-only-no-web-session'

    if mode == 'production' and _is_within(config_path, APP_ROOT):
        raise ProductionConfigError(
            'Production runtime config must be stored outside the application code tree.'
        )

    database_path = _absolute_path(raw.get('database_path'), 'database_path', config_path)
    backup_directory = _absolute_path(
        raw.get('backup_directory'), 'backup_directory', config_path
    )
    if _is_within(database_path, APP_ROOT) or _is_within(database_path.parent, APP_ROOT):
        raise ProductionConfigError('Database path must be outside the application code tree.')
    if _is_within(backup_directory, APP_ROOT):
        raise ProductionConfigError('Backup directory must be outside the application code tree.')
    if backup_directory == database_path.parent:
        raise ProductionConfigError(
            'Backup directory and database directory must be separate.'
        )
    if not backup_directory.is_dir():
        raise ProductionConfigError(
            f'Backup directory must already exist and be accessible: {backup_directory}'
        )

    trusted_hosts = raw.get('trusted_hosts')
    if (
        not isinstance(trusted_hosts, list)
        or not trusted_hosts
        or any(not isinstance(host, str) or not host.strip() or '*' in host for host in trusted_hosts)
    ):
        raise ProductionConfigError(
            'trusted_hosts must be a non-empty list of exact hostnames without wildcards.'
        )
    trusted_hosts = tuple(host.strip().lower() for host in trusted_hosts)

    proxy = raw.get('proxy')
    if not isinstance(proxy, dict):
        raise ProductionConfigError('proxy must be an object.')
    proxy_enabled = proxy.get('enabled')
    if not isinstance(proxy_enabled, bool):
        raise ProductionConfigError('proxy.enabled must be true or false.')
    proxy_hops = proxy.get('trusted_hops', 0)
    addresses = proxy.get('remote_addresses', [])
    if not isinstance(addresses, list) or any(not isinstance(item, str) for item in addresses):
        raise ProductionConfigError('proxy.remote_addresses must be a list of IPs or CIDRs.')
    try:
        proxy_networks = tuple(ipaddress.ip_network(item, strict=False) for item in addresses)
    except ValueError as exc:
        raise ProductionConfigError(f'Invalid trusted proxy address: {exc}') from exc

    https_termination = raw.get('https_termination')
    if (
        isinstance(proxy_hops, bool)
        or not isinstance(proxy_hops, int)
        or proxy_hops < 0
    ):
        raise ProductionConfigError('proxy.trusted_hops must be a non-negative integer.')
    if mode == 'production':
        if https_termination is not True:
            raise ProductionConfigError(
                'Production mode requires https_termination=true before the server can start.'
            )
        if not proxy_enabled or not 1 <= proxy_hops <= 2 or not proxy_networks:
            raise ProductionConfigError(
                'Production mode requires an enabled trusted proxy, 1-2 trusted hops, '
                'and explicit proxy remote addresses.'
            )
        if (
            len(proxy_networks) != 1
            or proxy_networks[0].prefixlen != proxy_networks[0].max_prefixlen
        ):
            raise ProductionConfigError(
                'Waitress requires one exact trusted proxy IP; broad proxy networks '
                'are not accepted.'
            )
    elif proxy_enabled or proxy_hops != 0 or proxy_networks:
        raise ProductionConfigError(
            'Local test mode must not enable forwarded-header proxy trust.'
        )
    login = raw.get('login_rate_limit')
    if not isinstance(login, dict):
        raise ProductionConfigError('login_rate_limit must be an object.')
    login_attempts = _positive_integer(login.get('attempts'), 'login_rate_limit.attempts', 1, 100)
    login_window = _positive_integer(
        login.get('window_seconds'), 'login_rate_limit.window_seconds', 60, 86400
    )
    ip_attempts = _positive_integer(
        login.get('ip_attempts'), 'login_rate_limit.ip_attempts', 1, 1000
    )
    ip_window = _positive_integer(
        login.get('ip_window_seconds'), 'login_rate_limit.ip_window_seconds', 60, 86400
    )
    session_seconds = _positive_integer(
        raw.get('session_lifetime_seconds'), 'session_lifetime_seconds', 300, 86400
    )

    waitress = raw.get('waitress')
    if not isinstance(waitress, dict):
        raise ProductionConfigError('waitress must be an object.')
    host = waitress.get('host')
    if host != '127.0.0.1':
        raise ProductionConfigError('Waitress must bind only to 127.0.0.1.')
    port = _positive_integer(waitress.get('port'), 'waitress.port', 1024, 65535)
    threads = _positive_integer(waitress.get('threads'), 'waitress.threads', 2, 64)

    service_logging = raw.get('service_logging')
    if not isinstance(service_logging, dict):
        raise ProductionConfigError('service_logging must be an object.')
    log_path = _absolute_path(
        service_logging.get('path'), 'service_logging.path', config_path
    )
    if _is_within(log_path, APP_ROOT) or _is_within(log_path.parent, APP_ROOT):
        raise ProductionConfigError('Service logs must be stored outside the application code tree.')
    if not log_path.parent.is_dir():
        raise ProductionConfigError(
            f'Service log directory must already exist and be accessible: {log_path.parent}'
        )
    log_max_bytes = _positive_integer(
        service_logging.get('max_bytes'), 'service_logging.max_bytes', 1024, 104857600
    )
    log_backup_count = _positive_integer(
        service_logging.get('backup_count'), 'service_logging.backup_count', 1, 20
    )
    drain_timeout = _positive_integer(
        raw.get('drain_timeout_seconds'), 'drain_timeout_seconds', 5, 600
    )

    _validate_database(database_path, verify_schema=verify_schema)
    return RuntimeConfig(
        mode=mode,
        config_path=config_path,
        database_path=database_path,
        backup_directory=backup_directory,
        trusted_hosts=trusted_hosts,
        proxy_enabled=proxy_enabled,
        proxy_hops=proxy_hops,
        proxy_networks=proxy_networks,
        session_lifetime_seconds=session_seconds,
        login_attempts=login_attempts,
        login_window_seconds=login_window,
        ip_attempts=ip_attempts,
        ip_window_seconds=ip_window,
        host=host,
        port=port,
        threads=threads,
        log_path=log_path,
        log_max_bytes=log_max_bytes,
        log_backup_count=log_backup_count,
        drain_timeout_seconds=drain_timeout,
        secret_key=secret,
    )


class _SensitiveLogFilter(logging.Filter):
    def __init__(self, secret):
        super().__init__()
        self.secret = secret

    def filter(self, record):
        message = record.getMessage()
        if self.secret:
            message = message.replace(self.secret, '[REDACTED]')
        record.msg = message
        record.args = ()
        return True


def configure_service_logging(config):
    formatter = logging.Formatter(
        '%(asctime)s %(levelname)s %(name)s %(message)s'
    )
    sensitive_filter = _SensitiveLogFilter(config.secret_key)
    handlers = [
        logging.handlers.RotatingFileHandler(
            config.log_path,
            maxBytes=config.log_max_bytes,
            backupCount=config.log_backup_count,
            encoding='utf-8',
        ),
        logging.StreamHandler(sys.stdout),
    ]
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(sensitive_filter)
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(logging.INFO)
    for handler in handlers:
        root_logger.addHandler(handler)
    logging.getLogger('werkzeug').setLevel(logging.WARNING)

    app_logger = logging.getLogger('app')
    app_logger.handlers.clear()
    app_logger.propagate = True


class LoginAttemptLimiter:
    def __init__(
        self,
        attempts,
        window_seconds,
        ip_attempts,
        ip_window_seconds,
        clock=time.monotonic,
    ):
        self.attempts = attempts
        self.window_seconds = window_seconds
        self.ip_attempts = ip_attempts
        self.ip_window_seconds = ip_window_seconds
        self.clock = clock
        self._failures = defaultdict(deque)
        self._lock = threading.Lock()

    @staticmethod
    def _keys(remote_addr, username):
        remote = remote_addr or 'unknown'
        identity = hashlib.sha256(
            (username or '').strip().casefold().encode('utf-8', errors='replace')
        ).hexdigest()
        return (f'ip:{remote}', f'account:{remote}:{identity}')

    def blocked_for(self, remote_addr, username):
        now = self.clock()
        ip_key, account_key = self._keys(remote_addr, username)
        with self._lock:
            ip_failures = self._failures[ip_key]
            account_failures = self._failures[account_key]
            while ip_failures and ip_failures[0] <= now - self.ip_window_seconds:
                ip_failures.popleft()
            while account_failures and account_failures[0] <= now - self.window_seconds:
                account_failures.popleft()
            if not ip_failures:
                self._failures.pop(ip_key, None)
            if not account_failures:
                self._failures.pop(account_key, None)
            if len(ip_failures) >= self.ip_attempts:
                return max(1, int(self.ip_window_seconds - (now - ip_failures[0])))
            if len(account_failures) >= self.attempts:
                return max(1, int(self.window_seconds - (now - account_failures[0])))
            self._trim(now)
            return 0

    def record_failure(self, remote_addr, username):
        now = self.clock()
        ip_key, account_key = self._keys(remote_addr, username)
        with self._lock:
            self._failures[ip_key].append(now)
            self._failures[account_key].append(now)
            self._trim(now)

    def record_success(self, remote_addr, username):
        _, account_key = self._keys(remote_addr, username)
        with self._lock:
            self._failures.pop(account_key, None)

    def _trim(self, now):
        if len(self._failures) <= 10000:
            return
        cutoff = now - max(self.window_seconds, self.ip_window_seconds)
        expired = [
            key for key, failures in self._failures.items()
            if not failures or failures[-1] <= cutoff
        ]
        for key in expired:
            self._failures.pop(key, None)


def prepare_application(config):
    os.environ['SECRET_KEY'] = config.secret_key
    import app as erp

    erp.DB_PATH = str(config.database_path)
    erp.app.secret_key = config.secret_key
    def configured_backup():
        from sqlite_backup import create_backup

        return create_backup(
            config.database_path,
            config.backup_directory,
            retention_days=None,
        )['path']

    erp.make_backup = configured_backup
    erp.app.config.update(
        SESSION_COOKIE_SECURE=config.mode == 'production',
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        PERMANENT_SESSION_LIFETIME=timedelta(
            seconds=config.session_lifetime_seconds
        ),
        SESSION_REFRESH_EACH_REQUEST=True,
        PREFERRED_URL_SCHEME='https' if config.mode == 'production' else 'http',
        TRUSTED_HOSTS=list(config.trusted_hosts),
    )
    limiter = LoginAttemptLimiter(
        config.login_attempts,
        config.login_window_seconds,
        config.ip_attempts,
        config.ip_window_seconds,
    )

    @erp.app.before_request
    def enforce_production_security():
        from flask import abort, request
        from flask import session as flask_session

        if config.mode == 'production' and not request.is_secure:
            abort(400, description='HTTPS through the configured trusted proxy is required.')
        if request.endpoint == 'login' and request.method == 'POST':
            retry_after = limiter.blocked_for(
                request.remote_addr, request.form.get('username', '')
            )
            if retry_after:
                request.environ['erp_login_limited'] = True
                request.environ['erp_retry_after'] = str(retry_after)
                abort(429, description='Login attempts are temporarily limited.')
        if flask_session.get('user_id'):
            flask_session.permanent = True

    @erp.app.after_request
    def record_login_attempt_and_security_headers(response):
        from flask import request
        from flask import session as flask_session

        if request.environ.get('erp_login_limited'):
            response.headers['Retry-After'] = request.environ['erp_retry_after']
        elif request.endpoint == 'login' and request.method == 'POST':
            username = request.form.get('username', '')
            if flask_session.get('user_id') and response.status_code < 400:
                limiter.record_success(request.remote_addr, username)
            elif response.status_code < 500:
                limiter.record_failure(request.remote_addr, username)

        response.headers.setdefault('X-Content-Type-Options', 'nosniff')
        response.headers.setdefault('X-Frame-Options', 'DENY')
        response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
        if config.mode == 'production' and request.is_secure:
            response.headers.setdefault('Strict-Transport-Security', 'max-age=15552000')
        return response

    erp.app.extensions['erp_login_limiter'] = limiter
    return erp.app


def run_server(config):
    configure_service_logging(config)
    logging.getLogger(__name__).info(
        'Starting Waitress on 127.0.0.1:%s after configuration validation.',
        config.port,
    )
    application = prepare_application(config)
    from waitress import serve

    options = {
        'host': config.host,
        'port': config.port,
        'threads': config.threads,
        'channel_timeout': 10,
        'clear_untrusted_proxy_headers': True,
    }
    if config.proxy_enabled:
        options.update(
            trusted_proxy=str(config.proxy_networks[0].network_address),
            trusted_proxy_count=config.proxy_hops,
            trusted_proxy_headers=(
                'x-forwarded-for',
                'x-forwarded-proto',
                'x-forwarded-host',
                'x-forwarded-port',
            ),
        )
    serve(application, **options)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Run the ERP with Waitress.')
    parser.add_argument('--config', default=os.environ.get('ERP_CONFIG_PATH'))
    args = parser.parse_args(argv)
    try:
        config = load_runtime_config(args.config)
        run_server(config)
    except ProductionConfigError as exc:
        print(f'SERVER_STARTUP_BLOCKED: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
