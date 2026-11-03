# SQLite backup and recovery procedure

This 4-C backup runner is deliberately not configured to use the real ERP database:
`backup_config.json` has an empty `source_database`, and the runner refuses to guess
or default to `tortkuldon.db`. Do not point it at the production database until the
deployment and recovery process has been separately approved and tested.

## What the backup runner does

- Opens the configured SQLite source in read-only mode and uses SQLite's online
  `Connection.backup()` API. The ERP may continue to use the database while the
  snapshot is made.
- Writes to a unique partial file, closes it, checks `integrity_check` and
  `foreign_key_check`, and only then publishes a timestamped
  `tortkuldon_YYYYMMDD_HHMMSS_microseconds_UTC.sqlite` backup.
- Removes only expired files matching its own backup filename pattern. The
  `retention_days` setting defaults to 30. It does not remove arbitrary files.
- Windows-service application-triggered backups disable retention cleanup:
  they use the configured runtime `backup_directory` and never remove existing
  snapshots. They are not a substitute for a scheduled daily backup.
- Refuses to restore over an existing destination. Restore writes to a partial
  file, verifies it, and publishes it only after successful validation.
- Returns a non-zero exit code and `BACKUP_FAILED` message if backup or validation
  fails. An invalid or partial backup is never reported as successful.

## Local test

The test suite creates isolated SQLite databases in the operating system's
temporary directory. It does not read or write `tortkuldon.db`.

```powershell
.\.venv\Scripts\python.exe -m unittest -v test_sqlite_backup
```

## Prepare a later daily scheduled run

1. Make a separate approved deployment copy of the app, database, and backup
   configuration. Keep backups off the application folder and, where possible,
   on a separate protected drive or encrypted off-site destination.
2. In that deployment copy only, set `source_database` in
   `backup_config.json` to the explicit absolute path of the intended database.
   Set `backup_directory` to the approved protected backup location and choose
   `retention_days`. The `daily_backup_time` field documents the intended
   schedule; the backup command itself runs once when invoked.
3. Run `run_daily_sqlite_backup.bat` manually and confirm it prints `BACKUP_OK`,
   `INTEGRITY_CHECK: ok`, and the expected backup path.
4. Configure Windows Task Scheduler only after the manual run and a restore drill
   pass. Follow `WINDOWS_PRODUCTION_GUIDE.md` for a daily trigger, least-privilege
   account, retries, and failure monitoring.
   No scheduled task is created by this 4-C change.
5. Periodically restore a chosen backup to a new, separate test path. Check
   `integrity_check`, foreign keys, users/roles, financial totals, receipts,
   cash transfers, production and stock records before considering it recoverable.

## Safe restore drill

1. Select and verify a timestamped backup. Record its path and size.
2. Stop only the intended test application instance before any operational
   recovery. Never overwrite the source database during a drill.
3. Restore to a new empty test path using `restore_backup(backup_path,
   destination_path)`. The destination must not already exist.
4. Run `verify_backup()` and compare the required record counts and financial
   aggregates with the values recorded for the backup snapshot.
5. Start the test application against the restored copy only and perform
   read-only smoke checks. Keep the original and backup unchanged.
6. A production recovery requires explicit authorization, a fresh pre-recovery
   backup, an outage window, and a documented rollback decision. This tool never
   automatically restores to the configured source database.

## Application update and rollback

1. Record the deployed application version and current database path.
2. Take and verify a database backup using the approved production configuration.
3. Copy the application code/configuration to a new versioned release directory;
   do not edit the live release in place. Keep the previous code directory intact.
4. Test the new code against a disposable copy of the database first.
5. For a code-only rollback, stop the application and point the launcher back to
   the previous code release. Do not restore or replace the database for a
   code-only rollback.
6. Restore a database only if the release introduced an approved schema/data
   change that cannot safely be reversed. Require a separate approval and verify
   the selected recovery point before restoring.

## Controlled schema migrations

Schema migrations must not run implicitly when the web server starts. Before a
future production deployment:

1. Identify and review each migration and its forward/rollback limitations.
   The password-session revocation change adds the `user_auth_versions` table;
   production startup requires this schema, so deploy it through this controlled
   migration before switching the server to the updated release. Existing
   sessions without a version are rejected and users must log in again.
2. Take a verified online backup and retain it outside the app directory.
3. Restore that backup to an isolated staging database and apply the migration
   there; test integrity, foreign keys, counts, business calculations, and app
   workflows.
4. Approve a maintenance window, stop all application writers, make another
   pre-migration backup, set both `ERP_ALLOW_MIGRATIONS` and
   `ERP_MAINTENANCE_CONFIRMED` to the exact confirmation strings documented in
   `WINDOWS_PRODUCTION_GUIDE.md`, and run the reviewed migration once as a
   separate, logged operation. These environment values are operator
   acknowledgements, not an automatic check that writers are stopped.
5. Verify the migrated database and application before reopening access.
6. If checks fail, keep the app stopped and follow the separately approved
   recovery procedure. Never silently replace the production database.
