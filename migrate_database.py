import argparse
import os
import secrets
import sys

from production_server import ProductionConfigError, load_runtime_config


CONFIRMATION = 'I_UNDERSTAND_BACKUP_AND_MIGRATE'
MAINTENANCE_CONFIRMATION = 'I_CONFIRMED_ALL_ERP_WRITERS_ARE_STOPPED'


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Run an explicitly approved ERP database migration.'
    )
    parser.add_argument('--config', default=os.environ.get('ERP_CONFIG_PATH'))
    args = parser.parse_args(argv)
    if os.environ.get('ERP_ALLOW_MIGRATIONS') != CONFIRMATION:
        print(
            'MIGRATION_BLOCKED: Set ERP_ALLOW_MIGRATIONS to the explicit '
            'confirmation string after approving a maintenance window.',
            file=sys.stderr,
        )
        return 2
    if os.environ.get('ERP_MAINTENANCE_CONFIRMED') != MAINTENANCE_CONFIRMATION:
        print(
            'MIGRATION_BLOCKED: Stop all ERP writers and set '
            'ERP_MAINTENANCE_CONFIRMED to the explicit maintenance confirmation.',
            file=sys.stderr,
        )
        return 2
    try:
        config = load_runtime_config(
            args.config,
            require_secret=False,
            verify_schema=False,
        )
    except ProductionConfigError as exc:
        print(f'MIGRATION_BLOCKED: {exc}', file=sys.stderr)
        return 2

    os.environ['SECRET_KEY'] = secrets.token_hex(32)
    import app as erp
    from sqlite_backup import create_backup, verify_backup

    erp.DB_PATH = str(config.database_path)
    try:
        backup = create_backup(
            config.database_path,
            config.backup_directory,
            retention_days=30,
        )
        erp.make_backup = lambda: backup['path']
        erp.init_db()
        from production_server import _validate_database
        _validate_database(config.database_path, verify_schema=True)
        verify_backup(backup['path'])
    except Exception as exc:
        print(
            f'MIGRATION_FAILED: {exc}. Pre-migration backup: {locals().get("backup", {}).get("path", "not created")}',
            file=sys.stderr,
        )
        return 1

    print(f'MIGRATION_OK: database validated at {config.database_path}')
    print(f'PRE_MIGRATION_BACKUP: {backup["path"]}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
