"""Immutable-name catalogue and product-to-worker groupings for DonControl ERP."""

BAGGED_FLOUR_PRODUCTS = ('Sara', 'Yevro', 'Zar Bug‘doy', 'Robiya', '1-nav un')
FLOUR_TYPES = BAGGED_FLOUR_PRODUCTS + ('Qora un',)
PRODUCTS = FLOUR_TYPES + ('Kepak', 'Muchka', 'Telashka atxod')
BAG_OPTIONS = {'Sara': (25, 50), 'Yevro': (25, 50), 'Zar Bug‘doy': (25, 50),
               'Robiya': (25, 50), '1-nav un': (25, 50), 'Qora un': (30,), 'Kepak': (23,),
               'Muchka': (50,), 'Telashka atxod': ()}
LOOSE_PRODUCTS = ('Kepak', 'Muchka', 'Telashka atxod')

INVOICE_GROUPS = {
    'Robiya': ('oliy', 'Oliy nav un'),
    'Sara': ('birinchi', '1-nav un'),
    'Yevro': ('birinchi', '1-nav un'),
    'Zar Bug‘doy': ('birinchi', '1-nav un'),
    '1-nav un': ('birinchi', '1-nav un'),
    'Qora un': ('qora', 'Qora un'),
    'Kepak': ('kepak', 'Kepak'),
    'Muchka': ('muchka', 'Muchka'),
    'Telashka atxod': ('telashka', 'Teleshka atxod'),
}

WORKER_PRODUCTS = {
    'umid': BAGGED_FLOUR_PRODUCTS,
    'maxmud': ('Kepak', 'Qora un', 'Muchka', 'Telashka atxod'),
}

