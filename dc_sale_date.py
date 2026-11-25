"""DonControl ERP: explicit, Uzbekistan-local sale date selection.

Keep sale business date separate from server execution time; existing created_at
fields continue to record when the operation really happened.
"""
from datetime import date, datetime, timedelta, timezone
import re

UZBEKISTAN_TZ = timezone(timedelta(hours=5), name='Asia/Tashkent')


def business_today():
    return datetime.now(UZBEKISTAN_TZ).date()


def business_today_iso():
    return business_today().isoformat()


def selected_sale_date(form, *, current_day=None):
    """Return YYYY-MM-DD; reject future days and invalid dates.

    Missing field remains backward compatible with older clients/tests and
    uses the Uzbekistan business day, not potentially UTC host-local date.
    """
    upper = current_day or business_today()
    raw = str(form.get('date') or '').strip()
    if not raw:
        return upper.isoformat()
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', raw):
        pattern = '%Y-%m-%d'
    elif re.fullmatch(r'\d{2}[.]\d{2}[.]\d{4}', raw):
        pattern = '%d.%m.%Y'
    elif re.fullmatch(r'\d{2}-\d{2}-\d{4}', raw):
        pattern = '%d-%m-%Y'
    else:
        raise ValueError('Sotuv sanasi noto‘g‘ri. DD.MM.YYYY ko‘rinishida kiriting.')
    try:
        d = datetime.strptime(raw, pattern).date()
    except ValueError as exc:
        raise ValueError('Sotuv sanasi mavjud emas. Kiritilgan sanani tekshiring.') from exc
    if d > upper:
        raise ValueError('Kelajakdagi sana uchun sotuv kiritib bo‘lmaydi.')
    return d.isoformat()
