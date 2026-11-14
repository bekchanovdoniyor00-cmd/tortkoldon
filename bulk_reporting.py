"""Read-only reporting for the separate bunker and packaging ledgers."""
from datetime import date, timedelta
from decimal import Decimal

ZERO = Decimal('0')


def _exists(con, name):
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def bulk_by_production(con):
    if not _exists(con, 'bulk_flour_batches'):
        return {}
    return {row['production_id']: Decimal(str(row['qty_kg']))
            for row in con.execute('SELECT production_id,qty_kg FROM bulk_flour_batches')}


def build_bulk_report(con, start, end, parse_date, format_date, warnings=None):
    """One mass balance plus dated packaging lines; no allocated/fictitious bags."""
    def rows(table):
        return [dict(r) for r in con.execute(f'SELECT * FROM "{table}"')] if _exists(con, table) else []

    openings = rows('bunker_openings')
    packaging = rows('flour_packaging')
    lines = rows('flour_packaging_lines')
    production = {r['id']: r for r in con.execute('SELECT id,date FROM production')}
    batches = rows('bulk_flour_batches')

    def value(row, key):
        return Decimal(str(row.get(key) or 0))

    undated = {(item.get('table'), item.get('record_id')) for item in (warnings or ())
               if item.get('key') == 'undated_period_record'}

    def valid(rows, key, table):
        result = []
        for row in rows:
            try:
                row['_bulk_day'] = parse_date(row[key])[0]
            except (TypeError, ValueError, IndexError):
                message = 'Sana noaniq yozuv tanlangan davrga taxminan biriktirilmadi.'
                if warnings is None:
                    raise ValueError(message) from None
                if (table, row['id']) not in undated:
                    warnings.append(dict(key='undated_period_record', message=message,
                                         table=table, record_id=row['id'], date=row[key]))
                continue
            result.append(row)
        return result

    openings = valid(openings, 'stock_date', 'bunker_openings')
    packaging = valid(packaging, 'date', 'flour_packaging')
    production = {r['id']: r for r in valid(
        (dict(r) for r in production.values()), 'date', 'production')}

    def day(row):
        return row['_bulk_day']

    def at(cutoff):
        opened = sum((value(r, 'qty_kg') for r in openings
                      if day(r) <= cutoff), ZERO)
        made = sum((value(r, 'qty_kg') for r in batches
                    if r['production_id'] in production
                    and day(production[r['production_id']]) <= cutoff), ZERO)
        used = sum((value(r, 'withdrawn_kg') for r in packaging
                    if day(r) <= cutoff), ZERO)
        return opened + made - used

    selected = {r['id']: r for r in packaging if start <= day(r) <= end}
    summary = {
        'opening_kg': at(start - timedelta(days=1)) if start > date.min else ZERO,
        'opening_added_kg': sum((value(r, 'qty_kg') for r in openings
                                 if start <= day(r) <= end), ZERO),
        'produced_kg': sum((value(r, 'qty_kg') for r in batches
                            if r['production_id'] in production
                            and start <= day(production[r['production_id']]) <= end), ZERO),
        'withdrawn_kg': sum((value(r, 'withdrawn_kg') for r in selected.values()), ZERO),
        'packaged_kg': sum((value(r, 'packaged_kg') for r in selected.values()), ZERO),
        'loss_kg': sum((value(r, 'loss_kg') for r in selected.values()), ZERO),
        'closing_kg': at(end),
    }
    # A mass difference is a measurement, while the saved loss is a separate field.
    summary['difference_kg'] = summary['withdrawn_kg'] - summary['packaged_kg']
    if abs(summary['opening_kg'] + summary['opening_added_kg']
           + summary['produced_kg'] - summary['withdrawn_kg']
           - summary['closing_kg']) > Decimal('0.001'):
        raise ValueError('Bunker balansi hisobotda tenglashmadi.')
    detail = []
    for r in sorted(lines, key=lambda r: (r['packaging_id'], r['product'], r['bag_kg'])):
        parent = selected.get(r['packaging_id'])
        if parent is None:
            continue
        detail.append({
            'date': format_date(day(parent)), 'id': parent['id'],
            'product': r['product'], 'bag_kg': r['bag_kg'],
            'bag_count': r['bag_count'],
            'qty_kg': value(r, 'bag_kg') * value(r, 'bag_count'),
        })
    line_total = sum((r['qty_kg'] for r in detail), ZERO)
    if abs(line_total - summary['packaged_kg']) > Decimal('0.001'):
        raise ValueError('Qoplash satrlari jami tayyor un bilan tenglashmadi.')
    return {'summary': summary, 'rows': detail,
            'operations': [{'date': format_date(day(r)), 'id': r['id'],
                            'withdrawn_kg': value(r, 'withdrawn_kg'),
                            'packaged_kg': value(r, 'packaged_kg'),
                            'loss_kg': value(r, 'loss_kg'),
                            'difference_kg': value(r, 'withdrawn_kg') - value(r, 'packaged_kg')}
                           for r in sorted(selected.values(), key=lambda r: (day(r), r['id']))]}


def append_bulk_sheets(workbook, report):
    """Use the same balance model in monthly and legacy production exports."""
    from openpyxl.styles import Font
    ws = workbook.create_sheet('Bunker harakati')
    ws.append(['Ko‘rsatkich', 'Miqdor, kg'])
    labels = (
        ('opening_kg', 'Davr boshidagi bunker'),
        ('opening_added_kg', 'Davrda kiritilgan boshlang‘ich qoldiq'),
        ('produced_kg', 'Yangi ishlab chiqarilgan bunker uni'),
        ('withdrawn_kg', 'Qoplashga sarflangan'),
        ('packaged_kg', 'Qoplangan tayyor un'),
        ('difference_kg', 'Qoplashdagi hisobiy vazn farqi'),
        ('loss_kg', 'Qoplash yozuvida saqlangan kamayish'),
        ('closing_kg', 'Davr oxiridagi bunker'),
    )
    for key, label in labels:
        ws.append([label, report['summary'][key]])
    ws.column_dimensions['A'].width = 48
    ws.column_dimensions['B'].width = 22
    ws = workbook.create_sheet('Qoplash operatsiyalari')
    ws.append(['Sana', 'Qoplash ID', 'Sarflangan, kg', 'Qoplangan, kg',
               'Hisobiy farq, kg', 'Saqlangan kamayish, kg'])
    for r in report['operations']:
        ws.append([r['date'], r['id'], r['withdrawn_kg'], r['packaged_kg'],
                   r['difference_kg'], r['loss_kg']])
    ws = workbook.create_sheet('Qoplangan mahsulotlar')
    ws.append(['Sana', 'Qoplash ID', 'Mahsulot', 'Qop, kg', 'Qop soni', 'Tayyor un, kg'])
    for r in report['rows']:
        ws.append([r['date'], r['id'], r['product'], r['bag_kg'], r['bag_count'], r['qty_kg']])
    for name in ('Bunker harakati', 'Qoplash operatsiyalari', 'Qoplangan mahsulotlar'):
        sheet = workbook[name]
        sheet.freeze_panes = 'A2'
        sheet.auto_filter.ref = sheet.dimensions
        for cell in sheet[1]:
            cell.font = Font(bold=True)
        for col in sheet.columns:
            if name != 'Bunker harakati':
                sheet.column_dimensions[col[0].column_letter].width = 24
        for row in sheet.iter_rows(min_row=2):
            for cell in row:
                if isinstance(cell.value, str):
                    cell.data_type = 's'
