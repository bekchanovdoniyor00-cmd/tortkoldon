"""Existing ERP payment labels and simple input/display helpers.

Extracted without behavior changes from app.py at commit 77e4ed1.
"""

PAYMENT_METHODS = {'naqd': 'Naqd', 'plastik': 'Plastik', 'hisob': 'Hisobdan o‘tkazish'}
EXPENSE_PAYMENT_METHODS = {'naqd': 'Naqd', 'plastik': 'Plastik', 'hisob': 'Bank'}
EXPENSE_FUND_SOURCES = {
    'umid': 'Umid kassasi',
    'maxmud': 'Maxmud kassasi',
    'buxgalter': 'Buxgalter kassasi',
}


def expense_display(row):
    result = dict(row)
    result['actual_person_display'] = (result.get('actual_person') or '').strip() or 'Aniqlanmagan'
    result['entered_by_display'] = result.get('entered_by_name') or 'Aniqlanmagan'
    result['purpose_display'] = (result.get('purpose') or '').strip() or 'Aniqlanmagan'
    result['payment_method_display'] = EXPENSE_PAYMENT_METHODS.get(
        result.get('payment_method'), 'Aniqlanmagan')
    source = result.get('fund_source')
    if source not in EXPENSE_FUND_SOURCES:
        source = result.get('source_role')
    result['fund_source_display'] = EXPENSE_FUND_SOURCES.get(source, 'Aniqlanmagan')
    return result


def payment_method_from_form(form):
    method = form.get('payment_method', 'naqd')
    if method not in PAYMENT_METHODS:
        raise ValueError('To‘lov turi noto‘g‘ri.')
    return method
