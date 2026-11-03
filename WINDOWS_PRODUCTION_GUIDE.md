# Windows server and service operations

This guide covers the 4-C test-copy preparation only. No Windows service,
scheduled task, public listener, DNS record, HTTPS proxy, Cloudflare Tunnel, or
production database was configured by this work.

## What is prepared

- `windows_service_host.py` validates the production configuration, secret,
  database/schema, and log destination before binding Waitress. Normal service
  startup never calls `init_db()` or runs a migration.
- The Windows service launch path (`windows_service_host.py` through
  `prepare_application()`) routes application backup calls to the configured
  `backup_directory` and SQLite's online backup API. Each requested snapshot is
  integrity-checked; backup errors propagate instead of being reported as
  success. These operation-triggered backups do not delete older backups and
  do not replace a separately scheduled daily backup policy.
- Production Waitress binds only to `127.0.0.1`. The dedicated lifecycle test
  uses `127.0.0.1:5053`; production example remains on `127.0.0.1:5052`.
- The host exposes no web-based stop control. WinSW's stop command signals a
  Windows named event. The listener closes, new queued WSGI work gets an
  explicit 503, and already-running WSGI responses are drained before workers
  exit. Waitress keep-alive channels expire after 10 seconds.
- Startup, stop, and failure events go to a rotating application log and WinSW
  stdout/stderr logs. Waitress access logging is not enabled. The application
  does not log form bodies; the configured session secret is redacted from
  messages. Never add passwords, session cookies, CSRF values, customer data,
  or request bodies to logs.
- WinSW XML and guarded install/remove/control scripts are templates only.
  WinSW itself is not included or installed.

The normal app log is configured by `service_logging` in the external runtime
JSON. Its directory must already exist, be outside the app folder, and be
writable by the service account. Log rotation is size-bounded by `max_bytes`
and `backup_count`. WinSW wrapper logs use the separate `logpath` in
`ToRtkoERP.xml.example`.

## Test-only local service

The automated integration suite creates its own SQLite database and log file
under the OS temporary directory, starts a test-only host on port 5053, stops it
through the named event, restarts it, and compares the DB hash. The test-only
mode refuses a non-test config or any bind other than `127.0.0.1:5053`. It does
not use the operational `tortkuldon.db`.

```powershell
.\.venv\Scripts\python.exe -m unittest -v test_windows_service
```

Port 5051 and 5052 are separate from that suite. Do not stop or reuse either
port for service testing.

## Prepare a later Windows service installation

Do not run these steps until a separate production-install approval is given.

1. Make a versioned application release directory and keep the previous release
   intact. Obtain a supported WinSW v3 release only from its official project
   release page; verify its publisher/hash by the organization's normal process.
   Do not copy an unverified service wrapper into production.
2. Create and configure `C:\ERP\config`, `C:\ERP\secrets`, `C:\ERPData`,
   `D:\ERPBackups`, `C:\ERPLogs\app`, and `C:\ERPLogs\winsw` (or approved
   alternatives). Keep database, secrets, logs, and backups outside the code
   tree. Update all paths in an external copy of
   `production_config.example.json`.
3. Configure the external production runtime JSON with `mode=production`,
   `host=127.0.0.1`, the approved database and backup paths, exact trusted
   proxy IP, trusted hostnames, log path, and the intended public port. The
   production JSON itself must not contain a secret.
4. Create a dedicated, non-administrator local or domain service account (for
   example `svc_ToRtkoERP`). Store a unique strong password in the approved
   password manager and apply the organization's rotation policy. During
   installation, the WinSW `<prompt>console</prompt>` asks for account
   credentials; do not put the password in XML, a batch file, command-line
   arguments, or source control. Ensure the account has “Log on as a service”.
5. Grant only the rights needed: read/execute on code and configuration, read
   on the secret file, Modify on the SQLite database directory (SQLite needs
   to create journal/WAL files), Modify on the backup directory and log
   directories, and no administrator membership. Use NTFS ACLs and verify them
   as the service account. Protect the secret and database volumes with the
   approved disk-encryption policy.
6. Create a random secret of at least 32 characters in the approved secret
   manager, then write it to the external file named by `ERP_SECRET_FILE`.
   Restrict that file to the service account, SYSTEM, and administrators; the
   service account needs read-only access. The file is plaintext protected by
   its NTFS ACL, so do not place it on a broadly readable share. The sample XML
   contains only the secret-file path, never the secret value.
7. Copy `ToRtkoERP.xml.example` beside the verified WinSW executable, rename the
   pair to `ToRtkoERP.xml` and `ToRtkoERP.exe`, then replace all sample paths.
   Create the WinSW log directory before installation. The XML requests
   automatic delayed start, prompts for a dedicated service identity, and
   restarts a failed process with bounded retries.
8. In an elevated console, set only the non-secret `ERP_MODE=production` and
   `ERP_SECRET_FILE` path, and set `ERP_SERVICE_APPROVED=INSTALL-APPROVED`.
   Review the exact XML and configuration paths, then run
   `install_windows_service.bat`. The script only invokes WinSW's install
   command and does not elevate itself. Do not start the service until its
   account, ACLs, secret path, config, proxy boundary, and backups are checked.
9. Start and inspect it with
   `manage_windows_service.bat start` and `manage_windows_service.bat status`.
   Verify logs, the local listener, login, required roles, and the reverse
   proxy before allowing users to connect.

WinSW's documented XML and commands are at
[WinSW XML configuration](https://github.com/winsw/winsw/blob/v3/docs/xml-config-file.md)
and [WinSW CLI commands](https://github.com/winsw/winsw/blob/v3/docs/cli-commands.md).
The sample uses WinSW's console credential prompt to avoid storing account
passwords in XML.

## Start, stop, restart, and removal

After an approved installation, use an elevated console:

```bat
manage_windows_service.bat status
manage_windows_service.bat start
manage_windows_service.bat stop
manage_windows_service.bat restart
```

Stop requests first stop accepting work; already-running requests are allowed
up to `drain_timeout_seconds` to finish. WinSW waits up to 90 seconds before its
hard stop. A hung operation can exceed the drain window; in that case the
service logs a failure and an eventual forced termination could interrupt that
operation. Investigate logs and database integrity before restarting after
such a failure. Never assume a timed-out stop completed cleanly.

For intentional removal, set `ERP_SERVICE_APPROVED=REMOVE-APPROVED` and run
`remove_windows_service.bat` from an elevated console. It refuses to uninstall
if the stop command fails. Uninstalling the wrapper does not delete the app,
database, configuration, logs, secrets, or backups. Remove those separately
only under an approved retention and decommissioning plan.

## Daily SQLite backups through Task Scheduler

The backup job is separate from the service lifecycle and can use SQLite's
online backup API while the ERP is running. It must use an external copy of
`backup_config.json` with an explicit database path, protected backup
directory, and approved retention period. The 4-C template intentionally has
an empty source path and must not guess the real database.

After a manual run and isolated restore drill pass:

1. Open Task Scheduler and create a daily task under the dedicated backup
   account (or a specifically approved service identity).
2. Set the action to the deployed Python executable, arguments
   `C:\ERP\app\daily_sqlite_backup.py --config C:\ERP\config\backup.json`,
   and Start in `C:\ERP\app`.
3. Run with least privilege; do not select “Run with highest privileges”
   unless separately justified. Grant read access to the database and Modify
   only to the backup directory. Configure retry-on-failure and alerting.
4. Confirm a run reports `BACKUP_OK` and `INTEGRITY_CHECK: ok`, record the
   resulting path, and regularly restore into a new disposable database and
   compare business totals and record counts.

No Task Scheduler task is created here. See
`SQLITE_BACKUP_GUIDE.md` for restore and migration procedures.

## Go-live gates and known limitations

- No Windows service, scheduled task, 5053 production listener, DNS, HTTPS,
  firewall, router, or Cloudflare setting was installed or changed.
- The service account, secret-file ACL, external paths, WinSW binary, and
  production proxy must be reviewed and manually configured.
- The login rate limiter is process-local, resets on restart, and is not shared
  between server instances.
- Migration confirmation does not detect other writers; operators must stop
  and verify every writer before a separately approved migration.
- Normal stop/drain was tested with temporary data and a WSGI request fixture;
  power loss, forced process termination, a stuck database operation, and
  real production traffic were not tested.
- Do not expose Waitress directly. Remote use still requires a separately
  approved HTTPS reverse proxy and network/DNS deployment.
