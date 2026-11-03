import ipaddress
import os
from pathlib import Path

from werkzeug.middleware.proxy_fix import ProxyFix

from production_server import (
    ProductionConfigError,
    configure_service_logging,
    load_runtime_config,
    prepare_application,
)
from sqlite_backup import create_backup


BACKUP_RETENTION_DAYS = 30


class TrustedProxyMiddleware:
    def __init__(self, application, proxy_networks, trusted_hops):
        self.application = application
        self.proxy_fix = ProxyFix(
            application,
            x_for=trusted_hops,
            x_proto=trusted_hops,
            x_host=trusted_hops,
            x_port=trusted_hops,
        )
        self.proxy_networks = proxy_networks

    def __call__(self, environ, start_response):
        remote_address = environ.get('REMOTE_ADDR', '')
        try:
            remote_ip = ipaddress.ip_address(remote_address)
        except ValueError:
            return self.application(environ, start_response)
        if any(remote_ip in network for network in self.proxy_networks):
            return self.proxy_fix(environ, start_response)
        return self.application(environ, start_response)


def _install_external_backup(application, config):
    def make_backup():
        result = create_backup(
            config.database_path,
            config.backup_directory,
            retention_days=BACKUP_RETENTION_DAYS,
        )
        return Path(result['path'])

    application.make_backup = make_backup


def create_application():
    if os.environ.get('ERP_MODE') != 'production':
        raise ProductionConfigError('ERP_MODE must be explicitly set to production.')
    if os.environ.get('ERP_SECRET_KEY'):
        raise ProductionConfigError(
            'Linux production requires ERP_SECRET_FILE; do not pass the secret in the environment.'
        )
    if not os.environ.get('ERP_SECRET_FILE'):
        raise ProductionConfigError(
            'Linux production requires ERP_SECRET_FILE to point to the protected secret file.'
        )

    config = load_runtime_config(os.environ.get('ERP_CONFIG_PATH'))
    if config.mode != 'production':
        raise ProductionConfigError('The Linux WSGI entrypoint requires production mode.')

    configure_service_logging(config)
    application = prepare_application(config)
    _install_external_backup(application, config)
    return TrustedProxyMiddleware(
        application,
        config.proxy_networks,
        config.proxy_hops,
    )
