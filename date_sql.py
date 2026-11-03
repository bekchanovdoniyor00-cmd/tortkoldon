"""Chronological SQLite helpers for DD-MM-YYYY business-date text."""

import re
import sqlite3

from date_conversions import dmy_to_iso, iso_to_date


_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
SQL_DATE_KEY_FUNCTION = "dmy_iso_key"
MIXED_SQL_DATE_KEY_FUNCTION = "iso_dmy_date_key"
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_DMY_DATE = re.compile(r"\d{2}-\d{2}-\d{4}\Z")


def _quote_identifier(identifier):
    if not isinstance(identifier, str) or not _IDENTIFIER.fullmatch(identifier):
        raise ValueError("SQL jadval yoki ustun nomi yaroqsiz.")
    return f'"{identifier}"'


def date_key_expression(column):
    """Return a SQL expression whose result sorts DD-MM-YYYY chronologically."""
    return f"{SQL_DATE_KEY_FUNCTION}({_quote_identifier(column)})"


def register_date_key_function(connection):
    """Register the strict DMY-to-ISO SQLite scalar function on a connection."""
    connection.create_function(
        SQL_DATE_KEY_FUNCTION, 1, dmy_to_iso, deterministic=True
    )


def validate_date_column(connection, table, column):
    """Reject empty, malformed, impossible, or mixed-format stored dates."""
    quoted_table = _quote_identifier(table)
    quoted_column = _quote_identifier(column)
    rows = connection.execute(
        f"SELECT {quoted_column} FROM {quoted_table}"
    )
    for row_number, row in enumerate(rows, start=1):
        value = row[0]
        try:
            dmy_to_iso(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"{table}.{column} dagi {row_number}-qiymat yaroqsiz "
                f"({value!r}); DD-MM-YYYY sana kutiladi."
            ) from exc


def date_filter(column, start_iso=None, end_iso=None):
    """Build inclusive ISO-bound filters over a validated DMY date column."""
    expression = date_key_expression(column)
    clauses = []
    parameters = []

    if start_iso is not None:
        iso_to_date(start_iso)
        clauses.append(f"{expression} >= ?")
        parameters.append(start_iso)
    if end_iso is not None:
        iso_to_date(end_iso)
        clauses.append(f"{expression} <= ?")
        parameters.append(end_iso)

    return " AND ".join(clauses), tuple(parameters)


def date_equality_filter(column, iso_day):
    """Build an equality filter using an exact ISO date parameter."""
    iso_to_date(iso_day)
    return f"{date_key_expression(column)} = ?", (iso_day,)


def date_order_by(column, direction="ASC"):
    """Build a chronological ORDER BY expression with a fixed direction."""
    normalized_direction = direction.upper() if isinstance(direction, str) else ""
    if normalized_direction not in ("ASC", "DESC"):
        raise ValueError("Saralash yo‘nalishi ASC yoki DESC bo‘lishi kerak.")
    return f"{date_key_expression(column)} {normalized_direction}"


def mixed_date_to_iso(value):
    """Convert one exact ISO or DMY plain date to its chronological key."""
    if isinstance(value, str) and _ISO_DATE.fullmatch(value):
        return iso_to_date(value).isoformat()
    if isinstance(value, str) and _DMY_DATE.fullmatch(value):
        return dmy_to_iso(value)
    raise ValueError("Sana YYYY-MM-DD yoki DD-MM-YYYY formatida bo‘lishi kerak.")


def mixed_date_key_expression(column):
    """Return a SQL expression for strict ISO/DMY date ordering."""
    return f"{MIXED_SQL_DATE_KEY_FUNCTION}({_quote_identifier(column)})"


def register_mixed_date_key_function(connection):
    """Register an ISO/DMY date key function on a SQLite connection."""
    connection.create_function(
        MIXED_SQL_DATE_KEY_FUNCTION, 1, mixed_date_to_iso, deterministic=True
    )


def validate_mixed_date_column(connection, table, column):
    """Reject malformed dates before an ISO/DMY report query can filter them out."""
    quoted_table = _quote_identifier(table)
    quoted_column = _quote_identifier(column)
    rows = connection.execute(
        f"SELECT {quoted_column} FROM {quoted_table}"
    )
    for row_number, row in enumerate(rows, start=1):
        value = row[0]
        try:
            mixed_date_to_iso(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"{table}.{column} dagi {row_number}-qiymat yaroqsiz "
                f"({value!r}); YYYY-MM-DD yoki DD-MM-YYYY sana kutiladi."
            ) from exc


def query_date_rows(
    connection, table, column, start_iso=None, end_iso=None, order="ASC"
):
    """Validate stored dates, then return rows with optional inclusive filters."""
    quoted_table = _quote_identifier(table)
    quoted_column = _quote_identifier(column)
    validate_date_column(connection, table, column)
    register_date_key_function(connection)
    where_sql, parameters = date_filter(column, start_iso, end_iso)
    sql = f"SELECT * FROM {quoted_table}"
    if where_sql:
        sql += f" WHERE {where_sql}"
    sql += f" ORDER BY {date_order_by(column, order)}"
    return connection.execute(sql, parameters).fetchall()
