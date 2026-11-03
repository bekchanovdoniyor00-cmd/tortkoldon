import argparse
import json
import sys
from pathlib import Path

from sqlite_backup import BackupError, create_backup


def load_config(path):
    config_path = Path(path).expanduser().resolve(strict=True)
    try:
        with config_path.open(encoding='utf-8') as config_file:
            config = json.load(config_file)
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupError(f'Could not read backup configuration {config_path}: {exc}') from exc
    if not isinstance(config, dict):
        raise BackupError('Backup configuration must be a JSON object.')

    source_value = config.get('source_database')
    if not isinstance(source_value, str) or not source_value.strip():
        raise BackupError(
            'source_database is empty. Set the intended database path in the config; '
            'the backup runner will not guess or default to tortkuldon.db.'
        )

    source = Path(source_value).expanduser()
    if not source.is_absolute():
        source = config_path.parent / source
    backup_value = config.get('backup_directory')
    if not isinstance(backup_value, str) or not backup_value.strip():
        raise BackupError('backup_directory must be a non-empty path.')
    backup_dir = Path(backup_value).expanduser()
    if not backup_dir.is_absolute():
        backup_dir = config_path.parent / backup_dir

    retention = config.get('retention_days', 30)
    if isinstance(retention, bool) or not isinstance(retention, int) or retention < 1:
        raise BackupError('retention_days must be a positive integer.')
    return source, backup_dir, retention


def main(argv=None):
    parser = argparse.ArgumentParser(description='Create and verify a SQLite online backup.')
    parser.add_argument(
        '--config',
        default=str(Path(__file__).with_name('backup_config.json')),
        help='JSON configuration file (default: backup_config.json)',
    )
    args = parser.parse_args(argv)
    try:
        source, backup_dir, retention = load_config(args.config)
        result = create_backup(source, backup_dir, retention_days=retention)
    except BackupError as exc:
        print(f'BACKUP_FAILED: {exc}', file=sys.stderr)
        return 1

    print(f'BACKUP_OK: {result["path"]}')
    print(f'INTEGRITY_CHECK: {result["integrity_check"]}')
    print(f'SIZE_BYTES: {result["size_bytes"]}')
    print(f'CREATED_AT_UTC: {result["created_at_utc"]}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
