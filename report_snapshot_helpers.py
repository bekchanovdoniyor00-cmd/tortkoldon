"""Read-only historical snapshots for reporting and stop-date reconciliation."""
import sqlite3
from decimal import Decimal
from functools import lru_cache
from business_dates import _business_date_key, _monthly_parse_date
from inventory_sales_helpers import product_stocks, bagged_product_stocks
from finance_calculations import calculate_financial_snapshot
from rop_service import DATED_INVENTORY_TABLES, wheat_warehouse_used


def _monthly_historical_snapshot(con, cutoff, warnings, *, inventory_only=False):
    snapshot = sqlite3.connect(':memory:')
    snapshot.row_factory = sqlite3.Row
    try:
        con.backup(snapshot)
        snapshot.create_function(
            'business_date_key', 1, _business_date_key, deterministic=True)

        @lru_cache(maxsize=4096)
        def snapshot_day(value):
            try:
                return _monthly_parse_date(value)[0].isoformat()
            except (TypeError, ValueError):
                return None

        snapshot.create_function('snapshot_day', 1, snapshot_day, deterministic=True)
        cutoff_text = cutoff.isoformat()
        dated_tables = {
            'sales': 'date',
            'opening_debts': 'date',
            'opening_debt_receipts': 'date',
            'worker_receipts': 'date',
            'accountant_worker_receipts': 'payment_date',
            'payments': 'date',
            'sale_cancellations': 'cancelled_at',
            'production': 'date',
            'flour_packaging': 'date',
            'rop_receipts': 'day',
            'wheat_receipts': 'date',
            'opening_product_stocks': 'stock_date',
            'opening_bagged_product_stocks': 'stock_date',
            'expenses': 'date',
            'accountant_cash_additions': 'date',
            'wheat_purchase_payments': 'payment_date',
            'account_cash_transfers': 'date',
            'worker_sale_batch_payments': 'created_at',
            'worker_sale_batches': 'created_at',
        }
        for table, date_column in dated_tables.items():
            if inventory_only and table not in DATED_INVENTORY_TABLES:
                continue
            exists = snapshot.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,)).fetchone()
            if not exists:
                continue
            columns = {
                row['name'] for row in
                snapshot.execute(f'PRAGMA table_info("{table}")')
            }
            if date_column not in columns:
                continue
            day = f'snapshot_day("{date_column}")'
            for row in snapshot.execute(
                    f'SELECT rowid AS _rowid, "{date_column}" AS _date '
                    f'FROM "{table}" WHERE {day} IS NULL ORDER BY rowid'):
                warnings.append({
                    'key': 'historical_snapshot_undated',
                    'message': 'Sana aniqlanmagan tarixiy yozuv cutoff hisobiga kiritilmadi.',
                    'table': table,
                    'record_id': row['_rowid'],
                    'date': row['_date'],
                })
            snapshot.execute(f'DELETE FROM "{table}" WHERE {day} IS NULL OR {day}>?',
                             (cutoff_text,))
        for table, parent_table, parent_id, child_id in (
                ('production_flour_types', 'production', 'id', 'production_id'),
                ('bagged_production_lines', 'production', 'id', 'production_id'),
                ('bulk_flour_batches', 'production', 'id', 'production_id'),
                ('flour_packaging_lines', 'flour_packaging', 'id', 'packaging_id'),
                ('bagged_sale_deductions', 'sales', 'id', 'sale_id'),
                ('worker_sale_batch_items', 'sales', 'id', 'sale_id')):
            child_exists = snapshot.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,)).fetchone()
            parent_exists = snapshot.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (parent_table,)).fetchone()
            if child_exists and parent_exists:
                snapshot.execute(
                    f'DELETE FROM "{table}" WHERE "{child_id}" NOT IN '
                    f'(SELECT "{parent_id}" FROM "{parent_table}")')
        if not inventory_only and snapshot.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='worker_transfers'").fetchone():
            for row in snapshot.execute(
                        "SELECT rowid AS _rowid,id,decided_at FROM worker_transfers "
                    "WHERE status='approved'").fetchall():
                try:
                    transfer_day = _monthly_parse_date(row['decided_at'])[0]
                except (TypeError, ValueError):
                    snapshot.execute(
                        'DELETE FROM worker_transfers WHERE rowid=?',
                        (row['_rowid'],))
                    warnings.append({
                        'key': 'historical_snapshot_undated_transfer',
                        'message': 'Tasdiqlangan transfer sanasi noaniq; '
                                   'tarixiy kassa snapshotiga kiritilmadi.',
                        'record_id': row['id'],
                    })
                    continue
                if transfer_day > cutoff:
                    snapshot.execute(
                        'DELETE FROM worker_transfers WHERE rowid=?',
                        (row['_rowid'],))

        stocks = product_stocks(snapshot)
        bags = bagged_product_stocks(snapshot)
        if inventory_only:
            return {'stocks': stocks, 'bags': bags}
        wheat_in = snapshot.execute(
            'SELECT COALESCE(SUM(net),0) FROM wheat_receipts').fetchone()[0]
        wheat_used = wheat_warehouse_used(snapshot)
        try:
            financial = calculate_financial_snapshot(snapshot)
        except ValueError as exc:
            financial = None
            warnings.append({
                'key': 'historical_debt_snapshot_error',
                'message': 'Tarixiy qarz snapshotini hisoblab bo‘lmadi: '
                           f'{exc}',
                'cutoff': cutoff_text,
            })
        return {
            'stocks': stocks,
            'bags': bags,
            'wheat_stock': Decimal(str(wheat_in or 0)) - Decimal(str(wheat_used or 0)),
            'financial': financial,
        }
    finally:
        snapshot.close()
