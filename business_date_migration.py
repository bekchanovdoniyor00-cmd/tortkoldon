"""Explicit, transactional ISO-to-DMY migration for historical databases.

Never run this against the live database without a separate verified backup,
a maintenance window that stops all writers, and the user's explicit approval.
"""

import re

from date_conversions import dmy_to_date, iso_to_dmy, iso_to_date


BUSINESS_DATE_COLUMNS = {
    "wheat_receipts": ("date",),
    "production": ("date",),
    "sales": ("date", "due_date"),
    "expenses": ("date",),
    "payments": ("date",),
    "opening_debts": ("date", "due_date"),
    "invoices": ("issued_date",),
    "worker_receipts": ("date",),
    "accountant_worker_receipts": ("payment_date",),
    "worker_sale_batches": ("sale_date",),
    "worker_expense_operations": ("expense_date",),
    "accountant_expense_operations": ("expense_date",),
}
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_DMY_DATE = re.compile(r"\d{2}-\d{2}-\d{4}\Z")


def _convert(value, *, allow_empty=False):
    if allow_empty and value == "":
        return value, False
    if isinstance(value, str) and _ISO_DATE.fullmatch(value):
        iso_to_date(value)
        return iso_to_dmy(value), True
    if isinstance(value, str) and _DMY_DATE.fullmatch(value):
        dmy_to_date(value)
        return value, False
    raise ValueError(f"Yaroqsiz biznes sana qiymati: {value!r}.")


def migrate_business_dates(connection):
    """Convert allowlisted ISO date columns to DMY, atomically and idempotently."""
    connection.execute("SAVEPOINT business_date_migration")
    converted = 0
    already_dmy = 0
    try:
        for table, columns in BUSINESS_DATE_COLUMNS.items():
            available_tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if table not in available_tables:
                continue
            available_columns = {
                row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')
            }
            for column in columns:
                if column not in available_columns:
                    continue
                allow_empty = table == "sales" and column == "due_date"
                rows = connection.execute(
                    f'SELECT rowid, "{column}" FROM "{table}"'
                ).fetchall()
                for rowid, value in rows:
                    replacement, should_update = _convert(
                        value, allow_empty=allow_empty
                    )
                    if should_update:
                        connection.execute(
                            f'UPDATE "{table}" SET "{column}"=? WHERE rowid=?',
                            (replacement, rowid),
                        )
                        converted += 1
                    elif value != "":
                        already_dmy += 1
        connection.execute("RELEASE SAVEPOINT business_date_migration")
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT business_date_migration")
        connection.execute("RELEASE SAVEPOINT business_date_migration")
        raise
    return {"converted": converted, "already_dmy": already_dmy}
