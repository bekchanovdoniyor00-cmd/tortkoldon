"""Small customer-facing helpers extracted without changing business logic."""

import re
from finance_calculations import resolve_customer_identity, resolve_customer_name


def phone_key(raw):
    digits=re.sub(r'\D','',raw or '')
    if len(digits)==9: digits='998'+digits
    return digits if len(digits)==12 and digits.startswith('998') else ''


def customer_display(context, name, phone=None):
    card = (resolve_customer_identity(context, name, phone)
            if phone is not None else resolve_customer_name(context, name))
    if card is None:
        return name, phone
    return card['name'], card['phone'] if phone is not None else None
