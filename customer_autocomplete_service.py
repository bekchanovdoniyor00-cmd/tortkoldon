"""Read-only customer autocomplete for DonControl ERP.

The caller is responsible for authentication and endpoint authorization.  No
UPDATE/INSERT/DELETE statements are performed by this module.
"""

import re
import unicodedata

# Name search only: NEVER transliterate the original stored/displayed name.
_CYRILLIC_TO_LATIN = {
    'а':'a','б':'b','в':'v','г':'g','д':'d','е':'e','ё':'yo','ж':'j',
    'з':'z','и':'i','й':'y','к':'k','л':'l','м':'m','н':'n','о':'o',
    'п':'p','р':'r','с':'s','т':'t','у':'u','ф':'f','х':'x','ц':'ts',
    'ч':'ch','ш':'sh','щ':'sh','ъ':'','ы':'i','ь':'','э':'e','ю':'yu',
    'я':'ya','ў':'o','қ':'q','ғ':'g','ҳ':'h','і':'i',
}


def _name_key(value):
    value = unicodedata.normalize('NFKC', str(value or '')).casefold()
    value = ''.join(_CYRILLIC_TO_LATIN.get(ch, ch) for ch in value)
    # Fold all apostrophe variants, spacing and common separators for matching.
    return ''.join(ch for ch in value if ch.isalnum())


def _phone_digits(value):
    return re.sub(r'\D', '', str(value or ''))


def _tables(con):
    return {row[0] for row in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
        "('customers','sales','opening_debts')")}


def _has_column(con, table, column):
    return any(row[1] == column for row in con.execute('PRAGMA table_info('+table+')'))


def find_customers(con, query, role, user_id, limit=12):
    """Return card and permitted historical matches, max *limit* rows.

    id is None for historical records that do not yet have a customer card.
    Workers may see existing shared cards (as before) but their historical
    sales/opening-debts expansion is limited to their own records.
    """
    if role not in ('buxgalter', 'rahbar', 'umid', 'maxmud'):
        return []
    query = str(query or '').strip()[:80]
    name_query = _name_key(query)
    phone_query = _phone_digits(query)
    is_phone_query = len(phone_query) >= 3 and not any(ch.isalpha() for ch in query)
    if (not is_phone_query and len(name_query) < 2):
        return []
    limit = max(1, min(12, int(limit)))
    tables = _tables(con)
    # Each record keeps a set of historical aliases, so searching an older
    # spelling still returns the current card without renaming it.
    records = []
    phone_index = {}

    def add(name, phone, card_id=None):
        name = str(name or '').strip()
        phone = str(phone or '').strip()
        if not name:
            return
        digits = _phone_digits(phone)
        pos = phone_index.get(digits) if digits else None
        if pos is not None:
            existing = records[pos]
            existing['_aliases'].add(_name_key(name))
            # A registered card always wins over a historical spelling.
            if card_id is not None and existing['id'] is None:
                existing.update(id=card_id, name=name, phone=phone)
            return
        # Keep separate contacts sharing a name but using different numbers.
        record = {'id':card_id, 'name':name, 'phone':phone,
                  '_aliases':{_name_key(name)}, '_digits':digits}
        records.append(record)
        if digits:
            phone_index[digits] = len(records) - 1

    if 'customers' in tables:
        for row in con.execute('SELECT id,name,phone FROM customers ORDER BY id'):
            add(row['name'], row['phone'], row['id'])

    worker = role in ('umid', 'maxmud')
    if 'sales' in tables:
        where = ' WHERE user_id=?' if worker else ''
        params = (user_id,) if worker else ()
        for row in con.execute(
                'SELECT DISTINCT customer,customer_phone FROM sales'+where,params):
            add(row['customer'], row['customer_phone'])

    if 'opening_debts' in tables:
        if not worker or _has_column(con, 'opening_debts', 'worker_id'):
            where = ' WHERE worker_id=?' if worker else ''
            params = (user_id,) if worker else ()
            for row in con.execute(
                    'SELECT DISTINCT customer,customer_phone FROM opening_debts'+where, params):
                add(row['customer'], row['customer_phone'])

    candidates = []
    for row in records:
        aliases = row['_aliases']
        digits = row['_digits']
        if is_phone_query:
            if phone_query not in digits:
                continue
            score = (0 if digits == phone_query else 1 if digits.startswith(phone_query) else 2)
        else:
            matching = [name for name in aliases if name_query in name]
            if not matching:
                continue
            score = min(0 if name == name_query else 1 if name.startswith(name_query) else 2
                        for name in matching)
        candidates.append((score, row['id'] is None, row['name'].casefold(), row))
    candidates.sort(key=lambda item: item[:3])
    return [{'id':row['id'], 'name':row['name'], 'phone':row['phone']}
            for _, _, _, row in candidates[:limit]]
