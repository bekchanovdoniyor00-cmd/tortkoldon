# Linux VPS deployment preparation

This guide prepares the 4-C test copy for a later Ubuntu VPS deployment. It does
not deploy the application, alter the Windows service files, register a domain,
or configure DNS/TLS. Never point these examples at the real `tortkuldon.db`
without a separately approved data migration and verified recovery plan.

## Architecture

Internet -> Nginx (HTTPS) -> Gunicorn on `127.0.0.1:8000` -> Flask -> SQLite.

Gunicorn is configured to load only `linux_wsgi:create_application()`. That
entrypoint requires explicit production mode, an external runtime config, and an
external secret file; it validates the database and schema before importing
the ERP application. It installs the existing production security settings and
only trusts forwarded headers when the WSGI peer address is in the exact proxy
allow-list. Do not run `gunicorn app:app`: that bypasses these checks and uses
the app's test-copy defaults.

The existing production runtime loader still requires the `waitress` settings
object in its JSON schema. The Linux sample keeps that object as the validated
loopback bind contract (port 8000); Gunicorn's own configuration uses the same
address. The `threads` value is not a Waitress server on Linux.

## Suggested VPS

- Ubuntu Server 26.04 LTS x86-64, or 24.04 LTS if required by the provider.
- Initial size: 2 vCPU, 4 GB RAM, 80 GB SSD/NVMe. Use 4 vCPU and 8 GB RAM if
  concurrent users or Excel reporting load requires it, based on a load test.
- Keep the SQLite database on persistent local block storage, not a network
  filesystem. Keep encrypted off-host backups in a different failure domain.
- Gunicorn starts with one `gthread` worker and eight threads. SQLite serializes
  writers; test actual traffic before increasing worker/process count.

## Deployment preparation

1. Provision the VPS with an unprivileged `tortkuldon` system account with no
   interactive shell, no sudo rights, and an `/opt/tortkuldon` release layout.
   Keep a versioned release and rollback copy.
2. Create `/opt/tortkuldon/venv` and install the Linux dependencies with
   `pip install -r requirements-linux.txt`. Do not copy the Windows `.venv`.
3. Create `/etc/tortkuldon`, `/var/lib/tortkuldon`, `/var/backups/tortkuldon`,
   and `/var/log/tortkuldon` before starting the service. Put application code
   read-only to the service account; grant write access only to the database,
   backup, and log locations.
4. Copy `deploy/linux/production_config.example.json` to
   `/etc/tortkuldon/production.json`. Keep it outside the release. Replace paths
   as approved; `database_path` must name an already-existing migrated database.
   The production startup validator refuses a missing DB/schema and does not
   initialize or migrate it.
5. Generate a unique session secret of at least 32 characters and save it in
   `/etc/tortkuldon/session_secret`, readable only by root and the service
   account. Do not put the value in JSON, the unit, command line, or source.
6. Copy `deploy/linux/backup_config.example.json` to
   `/etc/tortkuldon/backup.json`. Set `source_database` explicitly to the same
   approved database path. Keep the example's empty source path unchanged in
   the repository; the backup runner refuses to guess a database.
7. Install the systemd unit examples as
   `/etc/systemd/system/tortkuldon.service` and
   `/etc/systemd/system/tortkuldon-backup.service`, and install the timer as
   `/etc/systemd/system/tortkuldon-backup.timer`. Review all paths and
   permissions before enabling either unit. The app service binds only to
   loopback and runs as the non-root account.
8. Configure Nginx from `deploy/linux/nginx-tortkoldon.conf.example`. The TLS
   certificate paths are placeholders until the domain is registered, DNS is
   pointed to the VPS, and a certificate has been issued. Bootstrap HTTP
   certificate validation separately; then enable the HTTPS server and
   redirect.
9. Keep the host firewall limited to SSH (source-restricted), TCP 80, and TCP
   443. Do not expose port 8000. Validate Nginx configuration and service
   health in staging before public traffic.

## SQLite data and app-generated backups

SQLite must be writable by the service account because it needs to create
journal/WAL files. The DB directory and file belong to the service account and
must be outside the release tree. Startup validates an existing database; it
does not create, truncate, clean, replace, or migrate the production DB.

The existing ERP's `make_backup()` writes to `BASE_DIR/zaxira_nusxalar`. To
preserve the existing pre-cancellation backup behavior without making release
code writable, the Linux WSGI adapter replaces that helper at runtime with the
existing verified SQLite online-backup implementation and sends its output to
the configured external `backup_directory`. The application source file is
unchanged. The `/zaxira` download route is blocked in the Nginx example because
it returns a full database backup to any authenticated account; provide a
separately approved, role-restricted backup workflow if users need downloads.

The systemd oneshot service runs `daily_sqlite_backup.py`, which uses SQLite's
online backup API and checks `integrity_check` and `foreign_key_check`. It
retains 30 days by default and removes only its own timestamped SQLite backup
files; it never deletes or replaces the source DB. The timer is daily at 02:00
host-local time with persistent catch-up. Set the VPS timezone to
`Asia/Tashkent` if 02:00 is intended to mean Uzbekistan local time.

Local VPS backups alone are not disaster recovery. Before go-live, configure a
separate encrypted off-host copy (for example, an object-storage backup tool
with credentials outside the code and least-privilege write access), monitor
upload failures, and confirm the remote copy is readable. Do not count provider
VM snapshots on the same account/region as the only backup.

### Restore drill

1. Choose a timestamped verified backup and record its size and checksum.
2. Restore with `restore_backup(backup_path, new_empty_test_path)` to a new,
   separate test location. The restore helper refuses to overwrite a path.
3. Check database integrity, foreign keys, user/role access, financial
   aggregates, customer debts, receipts, cash transfers, stock, and production.
4. Run the application only against the restored disposable copy and perform
   read-only smoke tests.
5. Production recovery requires approval, a maintenance window, a fresh
   pre-recovery backup, and a documented rollback decision. Stop writers and
   manually switch to a verified restored copy; never automate replacement.

## Nginx, HTTPS, and login boundaries

Nginx terminates HTTPS and forwards a sanitized single proxy hop from its local
connection to Gunicorn. The WSGI adapter only honors forwarded headers from
`127.0.0.1`; do not bind Gunicorn to a public interface or broaden that
allow-list. Nginx must overwrite (not append untrusted client values to) the
`X-Forwarded-*` headers as shown.

The Flask production setup retains secure, HTTP-only, SameSite=Lax session
cookies, CSRF checks, host validation, and login rate limits. The Nginx example
adds a login POST rate limit. Review its rate and the app's per-process limiter
against real user behavior; the app limiter resets on restart. With one
Gunicorn worker it is shared by the app process, but it is not persistent.

The PWA manifest and service worker remain served by Flask. The service worker
is network-only and explicitly reports when the server is unreachable; it does
not save ERP transactions offline. Verify HTTPS, installability, icons, and
service-worker scope on the final domain.

## Domain/DNS after registration

The required hostname is `tortkoldon.uz` (with the spelling exactly as
registered). After registration and VPS IP assignment:

1. Create an `A` record for `tortkoldon.uz` to the VPS IPv4 address. Add a
   `www` CNAME to the root name if `www` will be supported. Add `AAAA` only if
   IPv6 is configured and reachable.
2. Ensure Nginx `server_name` and `trusted_hosts` match those exact hostnames.
3. Issue/renew HTTPS certificates and test HTTP-to-HTTPS redirects, certificate
   chain, login, proxy-derived client address, rate limits, and PWA resources.
4. Confirm port 8000 is not reachable externally and only Nginx receives
   application traffic.

No registration, DNS, VPS, TLS, or external network operation has been
performed as part of this preparation.

## Regression and Linux-specific validation

The prior root-level inventory contains 98 test methods. Four of the five
methods in `test_windows_service.py` exercise Windows service behavior; the
remaining drain-middleware unit test is platform-neutral. The 94 existing
non-Windows integration/regression methods are candidates, not a Linux PASS.
Some existing server tests hard-code `.venv/Scripts/python.exe` and test ports;
`test_waitress_http.py` also expects an independently running server. Adapt or
select those launchers for Linux staging and verify the test ports are free
before running. The Windows service integration tests must not be reported as
Linux passes.

The locally validated, isolated subset can be run with
`python -m unittest -v test_linux_wsgi test_sqlite_backup`. This does not start
Gunicorn, Nginx, systemd, or an HTTP server.

Before staging tests, create a disposable test DB and set all test environment
variables explicitly. Run tests that may bind local HTTP ports only after
checking those ports are free; do not reuse an occupied user/test server port.
For final Linux validation, use an isolated Ubuntu VM/container with the same
Python version and filesystem permissions as production:

- Run the portable regression suite and the new `test_linux_wsgi.py`.
- Confirm bad/missing config, secret, DB, schema, and log paths fail before
  Gunicorn accepts traffic.
- Confirm Gunicorn binds only to `127.0.0.1:8000`, the systemd unit starts and
  restarts cleanly, and the process runs as `tortkuldon`, not root.
- Test forwarded HTTPS/client-IP headers from Nginx and confirm untrusted
  forwarded headers are ignored.
- Test backup timer, external copy, restore drill, roles, login/CSRF, sales,
  debts, receipts, cash, production, 25/50 kg bags, customer lookup, Latin and
  Cyrillic, Excel exports, and PWA.

Windows cannot run Gunicorn natively. Local tests on Windows can validate
platform-neutral logic and SQLite behavior, but they do not prove Linux,
Gunicorn, systemd, Nginx, or filesystem permission behavior. Those checks must
run in Linux staging before deployment.
