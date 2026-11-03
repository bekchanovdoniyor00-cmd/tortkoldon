import os
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path


BACKUP_NAME = re.compile(r'^tortkuldon_\d{8}_\d{6}_\d+_UTC\.sqlite$')


class BackupError(RuntimeError):
    pass


def _read_only_connection(path):
    resolved = Path(path).expanduser().resolve(strict=True)
    if not resolved.is_file():
        raise BackupError(f'SQLite database is not a file: {resolved}')
    connection = sqlite3.connect(
        resolved.as_uri() + '?mode=ro',
        uri=True,
        timeout=30,
    )
    connection.execute('PRAGMA query_only=ON')
    connection.execute('PRAGMA busy_timeout=30000')
    return resolved, connection


def _validate_database(path):
    try:
        resolved, connection = _read_only_connection(path)
        try:
            integrity = connection.execute('PRAGMA integrity_check').fetchone()
            if integrity is None or integrity[0] != 'ok':
                detail = integrity[0] if integrity else 'no integrity result'
                raise BackupError(f'SQLite integrity_check failed for {resolved}: {detail}')
            violations = connection.execute('PRAGMA foreign_key_check').fetchmany(10)
            if violations:
                raise BackupError(
                    f'SQLite foreign_key_check found violations in {resolved}: '
                    f'{violations!r}'
                )
            size = resolved.stat().st_size
            if size <= 0:
                raise BackupError(f'SQLite database is empty: {resolved}')
            return {'path': resolved, 'size_bytes': size, 'integrity_check': 'ok'}
        finally:
            connection.close()
    except BackupError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise BackupError(f'Could not validate SQLite database {path}: {exc}') from exc


def verify_backup(path):
    return _validate_database(path)


def _copy_database(source_path, target_path):
    source, source_connection = _read_only_connection(source_path)
    target_connection = None
    try:
        target_connection = sqlite3.connect(str(target_path), timeout=30)
        source_connection.backup(target_connection, pages=256, sleep=0.05)
        target_connection.commit()
    finally:
        if target_connection is not None:
            target_connection.close()
        source_connection.close()
    return source


def _remove_expired_backups(backup_dir, retention_days, now):
    cutoff = now - timedelta(days=retention_days)
    for candidate in backup_dir.iterdir():
        if not candidate.is_file() or not BACKUP_NAME.fullmatch(candidate.name):
            continue
        timestamp = datetime.strptime(
            candidate.name.removeprefix('tortkuldon_').removesuffix('_UTC.sqlite'),
            '%Y%m%d_%H%M%S_%f',
        ).replace(tzinfo=timezone.utc)
        if timestamp < cutoff:
            candidate.unlink()


def create_backup(source_path, backup_dir, retention_days=30, now=None):
    if retention_days is not None:
        if isinstance(retention_days, bool) or not isinstance(retention_days, int):
            raise BackupError('retention_days must be a positive integer or None.')
        if retention_days < 1:
            raise BackupError('retention_days must be at least 1.')

    try:
        source = Path(source_path).expanduser().resolve(strict=True)
        if not source.is_file():
            raise BackupError(f'SQLite source is not a file: {source}')
        target_dir = Path(backup_dir).expanduser().resolve()
        if target_dir == source.parent:
            raise BackupError('Backup directory must be separate from the source database directory.')
        target_dir.mkdir(parents=True, exist_ok=True)

        created_at = now or datetime.now(timezone.utc)
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        created_at = created_at.astimezone(timezone.utc)
        stamp = created_at.strftime('%Y%m%d_%H%M%S_%f')
        destination = target_dir / f'tortkuldon_{stamp}_UTC.sqlite'
        if destination.exists():
            raise BackupError(f'Backup destination already exists: {destination}')
        partial = target_dir / f'.{destination.name}.{uuid.uuid4().hex}.partial'

        try:
            _copy_database(source, partial)
            validation = _validate_database(partial)
            if destination.exists():
                raise BackupError(f'Backup destination already exists: {destination}')
            os.rename(partial, destination)
            try:
                with destination.open('r+b') as backup_file:
                    os.fsync(backup_file.fileno())
            except OSError as exc:
                destination.unlink(missing_ok=True)
                raise BackupError(f'Could not flush completed backup {destination}: {exc}') from exc
        except Exception:
            partial.unlink(missing_ok=True)
            raise

        if retention_days is not None:
            try:
                _remove_expired_backups(target_dir, retention_days, created_at)
            except OSError as exc:
                raise BackupError(
                    f'Backup was created and verified at {destination}, but retention cleanup '
                    f'failed: {exc}'
                ) from exc

        return {
            'path': destination,
            'size_bytes': validation['size_bytes'],
            'integrity_check': validation['integrity_check'],
            'created_at_utc': created_at.isoformat(),
        }
    except BackupError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise BackupError(f'Could not create SQLite backup: {exc}') from exc


def restore_backup(backup_path, destination_path):
    try:
        backup = Path(backup_path).expanduser().resolve(strict=True)
        _validate_database(backup)
        destination = Path(destination_path).expanduser().resolve()
        if backup == destination:
            raise BackupError('Restore destination must not be the backup file.')
        if destination.exists():
            raise BackupError(
                f'Restore destination already exists; refusing to overwrite: {destination}'
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(
            f'.{destination.name}.{uuid.uuid4().hex}.restore.partial'
        )
        try:
            _copy_database(backup, partial)
            validation = _validate_database(partial)
            if destination.exists():
                raise BackupError(
                    f'Restore destination appeared during restore; refusing to overwrite: '
                    f'{destination}'
                )
            os.rename(partial, destination)
        except Exception:
            partial.unlink(missing_ok=True)
            raise
        return {
            'path': destination,
            'size_bytes': validation['size_bytes'],
            'integrity_check': validation['integrity_check'],
        }
    except BackupError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise BackupError(f'Could not restore SQLite backup: {exc}') from exc
