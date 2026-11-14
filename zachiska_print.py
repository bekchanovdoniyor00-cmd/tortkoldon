"""Read-only presentation data for the 117-form draft."""
import json
from datetime import date

from bulk_reporting import build_bulk_report
from business_dates import _monthly_parse_date
import rop_service as rop


def build_zachiska_draft(con, period, selected_stop, selected_month):
    closed = period['stop_day'] is not None
    saved = json.loads(period['reconciliation'] or '{}') if closed else {}
    stop_day = period['stop_day']
    if not closed and selected_stop:
        stop_day = date.fromisoformat(selected_stop).isoformat()
        rop.validate_stop(con, period, stop_day)
    cutoff = stop_day or date.today().isoformat()
    month = saved.get('report_month') if closed else selected_month
    if not closed and (month or stop_day):
        month = rop.report_month_start(month or None, cutoff).isoformat()[:7]

    period_id = period['id']
    receipts = con.execute('''SELECT r.day, r.qty_kg, r.from_warehouse, d.document_ref
        FROM rop_receipts r LEFT JOIN rop_transfer_documents d ON d.receipt_id=r.id
        WHERE r.period_id=? AND r.day<=? ORDER BY r.day,r.id''', (period_id, cutoff)).fetchall()
    production = con.execute('''SELECT COUNT(*) AS entries, SUM(b.qty_kg) AS bunker,
        SUM(p.bran_kg) AS bran, SUM(p.muchka_kg) AS muchka, SUM(p.telashka_kg) AS telashka
        FROM rop_production r JOIN production p ON p.id=r.production_id
        LEFT JOIN bulk_flour_batches b ON b.production_id=p.id
        WHERE r.period_id=? AND business_date_key(p.date)<=?''', (period_id, cutoff)).fetchone()
    products = [(r['product'], r['kg']) for r in con.execute('''SELECT f.product, SUM(f.qty_kg) AS kg
        FROM production_flour_types f JOIN rop_production r ON r.production_id=f.production_id
        JOIN production p ON p.id=r.production_id
        WHERE r.period_id=? AND business_date_key(p.date)<=? GROUP BY f.product ORDER BY f.product''',
        (period_id, cutoff))]
    products.extend((label, production[key]) for label, key in (
        ('Bunkerga tushgan un', 'bunker'), ('Kepak', 'bran'), ('Muchka', 'muchka'),
        ('Telashka atxod', 'telashka')))
    packaging = con.execute('''SELECT SUM(withdrawn_kg) AS withdrawn, SUM(packaged_kg) AS packaged
        FROM flour_packaging WHERE id>? AND business_date_key(date)<=?''',
        (period['packaging_floor'], cutoff)).fetchone()
    bags = con.execute('''SELECT l.product, l.bag_kg, SUM(l.bag_count) AS bags
        FROM flour_packaging_lines l JOIN flour_packaging p ON p.id=l.packaging_id
        WHERE p.id>? AND business_date_key(p.date)<=?
        GROUP BY l.product,l.bag_kg ORDER BY l.product,l.bag_kg''',
        (period['packaging_floor'], cutoff)).fetchall()
    documents = saved.get('documents') or {}
    adjustments = saved.get('rop_adjustments') or {}
    if closed:
        cleaning_document = documents.get('cleaning_waste')
        cleaning_waste = adjustments.get('cleaning_waste') if cleaning_document else None
        grain_waste = saved.get('form34_grain_waste_kg')
    else:
        waste = rop.grain_waste_summary(con, period_id, cutoff)
        cleaning_waste = grain_waste = waste['kg']
        cleaning_document = 'ROP nakladnoylari' if waste['entries'] else None
    measurement = con.execute('''SELECT day, qty_kg, document_ref FROM rop_measurements
        WHERE period_id=? AND day<=? ORDER BY day DESC,id DESC LIMIT 1''',
        (period_id, cutoff)).fetchone() if not closed else None
    totals = saved if closed else rop.period_totals(con, period)
    # The existing totals use zero for empty ledgers. The document distinguishes
    # missing measurements from recorded zeros and keeps approved totals frozen.
    received = totals.get('received') if receipts else None
    cleaned = totals.get('cleaned') if production['entries'] else None
    bunker = saved.get('bunker_kg') if documents.get('balance') else None
    book_bunker = None
    if not closed:
        warnings = []
        try:
            bulk = build_bulk_report(con, date.fromisoformat(period['start_day']),
                                     date.fromisoformat(cutoff), _monthly_parse_date,
                                     date.isoformat, warnings)
        except ValueError:
            pass
        else:
            has_bunker_records = con.execute('''SELECT EXISTS (
                SELECT 1 FROM bunker_openings WHERE business_date_key(stock_date)<=?
                UNION ALL
                SELECT 1 FROM bulk_flour_batches b JOIN production p ON p.id=b.production_id
                WHERE business_date_key(p.date)<=?
                UNION ALL
                SELECT 1 FROM flour_packaging WHERE business_date_key(date)<=?)''',
                (cutoff, cutoff, cutoff)).fetchone()[0] if not warnings else False
            if has_bunker_records:
                book_bunker = bulk['summary']['closing_kg']
    return dict(
        period=period, month=month, stop_day=stop_day, cutoff=cutoff, closed=closed,
        opening=totals.get('opening'), received=received, cleaned=cleaned,
        difference=totals.get('calculated_difference') if received is not None and cleaned is not None else None,
        receipts=receipts, products=products, packaging=packaging, bags=bags,
        production_output=totals.get('output') if production['entries'] else None,
        physical_rop=saved.get('physical_rop') if documents.get('empty') else (
            measurement['qty_kg'] if measurement and measurement['document_ref'] else None),
        measurement_day=stop_day if documents.get('empty') else (measurement['day'] if measurement else None),
        measurement_doc=documents.get('empty') or (measurement['document_ref'] if measurement else None),
        book_bunker=book_bunker, bunker=bunker,
        cleaning_waste=cleaning_waste, cleaning_document=cleaning_document,
        grain_waste=grain_waste,
        other_cleaning_waste=(round(cleaning_waste - grain_waste, 3)
                              if closed and grain_waste is not None and cleaning_waste is not None else None),
        yield_report=saved.get('form117_yield'), yield_labels=rop.form117.OUTPUT_LABELS,
        moisture_change=adjustments.get('moisture_loss') if documents.get('moisture') else None,
        documents=documents, losses=saved.get('losses', {}) if documents.get('balance') else {},
        loss_fields=rop.LOSS_FIELDS,
    )
