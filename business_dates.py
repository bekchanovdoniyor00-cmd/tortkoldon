import re
from datetime import timedelta
from functools import lru_cache

from date_conversions import dmy_to_date, iso_to_date


def _business_date(value):
    if not isinstance(value, str):
        raise ValueError('Sana YYYY-MM-DD yoki DD-MM-YYYY formatida bo‘lishi kerak.')
    return _parse_business_date_text(value)


@lru_cache(maxsize=4096)
def _parse_business_date_text(value):
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        return iso_to_date(value), 'iso'
    if re.fullmatch(r'\d{2}-\d{2}-\d{4}', value):
        return dmy_to_date(value), 'dmy'
    raise ValueError('Sana YYYY-MM-DD yoki DD-MM-YYYY formatida bo‘lishi kerak.')

def _business_date_after(value, days):
    parsed, date_format = _business_date(value)
    result = parsed + timedelta(days=days)
    if date_format == 'iso':
        return result.isoformat()
    return f'{result.day:02d}-{result.month:02d}-{result.year:04d}'

def _business_date_key(value):
    return _business_date(value)[0].isoformat()

def _business_date_storage(value):
    parsed, _ = _business_date(value)
    return f'{parsed.day:02d}-{parsed.month:02d}-{parsed.year:04d}'

def _html_date_storage(value):
    parsed = iso_to_date(value)
    return f'{parsed.day:02d}-{parsed.month:02d}-{parsed.year:04d}'

def _business_date_input(value):
    return _business_date(value)[0].isoformat()

def _business_date_display(value):
    parsed, _ = _business_date(value)
    return parsed.strftime('%d.%m.%Y')

def _monthly_parse_date(value):
    if isinstance(value, str) and len(value) > 10:
        value = value[:10]
    return _business_date(value)
