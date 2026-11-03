"""Date presentation only; never infer dates from customer/free-text fields."""
from datetime import date, datetime, time
import re

from date_conversions import dmy_to_date, iso_to_date

EXCEL_DATE_FORMAT = 'dd.mm.yy'
_ISO_DATE = re.compile(r'\d{4}-\d{2}-\d{2}\Z')
_DMY_DATE = re.compile(r'\d{2}-\d{2}-\d{4}\Z')


def excel_date(value):
    if value is None or value == '':
        return None
    try:
        if isinstance(value, (date, datetime)):
            result = value
        elif isinstance(value, str) and _ISO_DATE.fullmatch(value):
            result = datetime.combine(iso_to_date(value), time.min)
        elif isinstance(value, str) and _DMY_DATE.fullmatch(value):
            result = datetime.combine(dmy_to_date(value), time.min)
        elif isinstance(value, str) and ('T' in value or ' ' in value):
            result = datetime.fromisoformat(value)
        else:
            raise ValueError()
        if isinstance(result, datetime) and result.tzinfo is not None:
            raise ValueError()
        return result
    except (ValueError, TypeError):
        raise ValueError('Excel eksportida sana yaroqsiz; sana yozuvini tekshiring.') from None


def display_date(value):
    return excel_date(value).strftime('%d.%m.%y') if value else 'Cheklanmagan'


def format_date_columns(ws, columns):
    for row in ws.iter_rows(min_row=2):
        for column in columns:
            cell = row[column - 1]
            cell.value = excel_date(cell.value)
            cell.number_format = EXCEL_DATE_FORMAT
