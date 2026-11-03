"""Strict conversions for plain business dates stored as DD-MM-YYYY."""

import re
from datetime import date


_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_DMY_DATE = re.compile(r"\d{2}-\d{2}-\d{4}\Z")


def _require_date_text(value, pattern, expected_format):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"Sana {expected_format} formatida bo‘lishi kerak.")


def iso_to_date(value):
    """Parse an exact YYYY-MM-DD date, rejecting invalid calendar dates."""
    _require_date_text(value, _ISO_DATE, "YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError("Kalendar sanasi yaroqsiz.") from None


def dmy_to_date(value):
    """Parse an exact DD-MM-YYYY date, rejecting invalid calendar dates."""
    _require_date_text(value, _DMY_DATE, "DD-MM-YYYY")
    day, month, year = (int(part) for part in value.split("-"))
    try:
        return date(year, month, day)
    except ValueError:
        raise ValueError("Kalendar sanasi yaroqsiz.") from None


def iso_to_dmy(value):
    """Convert an exact YYYY-MM-DD date to DD-MM-YYYY."""
    parsed = iso_to_date(value)
    return f"{parsed.day:02d}-{parsed.month:02d}-{parsed.year:04d}"


def dmy_to_iso(value):
    """Convert DD-MM-YYYY to the chronological YYYY-MM-DD sort key."""
    return dmy_to_date(value).isoformat()


def dmy_to_display(value):
    """Convert DD-MM-YYYY to the screen format DD.MM.YYYY."""
    parsed = dmy_to_date(value)
    return f"{parsed.day:02d}.{parsed.month:02d}.{parsed.year:04d}"
