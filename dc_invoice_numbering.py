"""Read-only stable display serials for employee invoices.

The legacy invoices.number remains unchanged. Each employee gets an
independent, month-scoped 1-based serial ordered by immutable invoice ID.
Batch invoices count once because they have one invoices row.
"""


def employee_invoice_label(con, invoice_id):
    row = con.execute("""
        SELECT i.id, i.number, i.month_key, s.user_id, COALESCE(u.role, '') AS role
          FROM invoices i
          JOIN sales s ON s.id = i.sale_id
          LEFT JOIN users u ON u.id = s.user_id
         WHERE i.id = ?
    """, (int(invoice_id),)).fetchone()
    if row is None:
        return ''
    user_id = row['user_id']
    if user_id == 3:
        prefix = 'U'
    elif user_id == 4:
        prefix = 'M'
    else:
        return str(row['number'])
    serial = con.execute("""
        SELECT COUNT(*)
          FROM invoices i
          JOIN sales s ON s.id = i.sale_id
         WHERE i.month_key = ? AND s.user_id = ? AND i.id <= ?
    """, (row['month_key'], user_id, row['id'])).fetchone()[0]
    if serial < 1:
        raise ValueError('Invoice numbering inconsistency')
    return '%s-%03d' % (prefix, serial)
