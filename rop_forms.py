"""Period documents for the supplied Forms 34 and 117."""
from datetime import date
from decimal import Decimal
import json

from bulk_flour_service import amount
import form117


GRAIN = (
    ('kg', 'Og‘irlik, kg', 'kg'), ('nature', 'Natura, g/l', 'kg'),
    ('moisture', 'Namlik, %', 'percent'), ('foreign', 'Iflos aralashma, %', 'percent'),
    ('harmful', 'Shundan zararli, %', 'percent'), ('grain', 'Donli aralashma, %', 'percent'),
    ('infestation', 'Zararlanganlik nomi', 'text'), ('infestation_count', 'Zararlanganlik soni', 'kg'),
    ('analysis', 'Laboratoriya tahlil raqami', 'text'),
)
WASTE = (
    ('seed', 'Urug‘lik xomashyosi donli aralashmasi'),
    ('mix50', 'Oraliq mahsulot · 50% dan yuqori, 70% gacha'),
    ('mix70', 'Oraliq mahsulot · 70% dan yuqori, 85% gacha'),
    ('cat1_high', 'I toifa · 30% dan yuqori, 50% gacha'),
    ('cat1_low', 'I toifa · 10% dan yuqori, 30% gacha'),
    ('cat2', 'II toifa · 2% dan yuqori, 10% gacha'),
    ('cat3', 'III toifa · 2% gacha'),
)
WASTE_COLUMNS = (
    ('kg', 'Og‘irlik, kg', 'kg'), ('moisture', 'Namlik, %', 'percent'),
    ('main', 'Asosiy don, %', 'percent'), ('grain', 'Donli aralashma, %', 'percent'),
    ('foreign', 'Iflos aralashma, %', 'percent'), ('analysis', 'Tahlil raqami', 'text'),
)
GRAIN_QUALITY = (
    (37, 'Namlik'), (38, 'Kullilik'), (39, 'Natura'), (40, 'Iflos aralashma, jami'),
    (41, 'Organik aralashma'), (42, 'Begona aralashma'), (43, 'Mineral aralashma'),
    (44, 'Donli aralashma, jami'), (45, 'Singan don'), (46, 'Puch don'), (47, 'Arpa'),
    (48, 'Mayda don'), (49, 'Shishasimonlik'), (50, 'Tip tarkibi'),
    (52, 'Qattiq bug‘doy aralashmasi'), (54, 'Kleykovina'),
)
PRODUCT_QUALITY = (
    (37, 'Namlik'), (38, 'Kullilik'), (39, 'Elakda qolgan yiriklik'),
    (40, 'Elakdan o‘tish bir xilligi'), (41, 'Metall aralashma'),
    (42, 'Xomashyo kleykovinasi'), (43, 'Non chiqish hajmi'),
    (44, 'Balandlik va diametr nisbati'), (45, 'Sifatli yadro va singan don'),
    (46, 'Iflos va mineral aralashma'), (50, 'Rang'), (51, 'Ta’m'),
    (52, 'Hid'), (53, 'Qirsillash'), (54, 'Zararlanganlik'),
)
PRODUCT_QUALITY_COLS = (('G', 'Yorma'), ('I', '1-nav un'), ('L', 'Kepak'), ('M', 'O‘rtacha tortilgan'))
SIGNS = {
    34: (('director', 'Tashkilot rahbari'), ('lab', 'Laborant'), ('lab_head', 'ICHTL boshlig‘i'),
         ('warehouse', 'Don ombori mudiri'), ('accountant', 'Bosh hisobchi')),
    117: (('director', 'Rahbar'), ('engineer', 'Bosh muhandis'), ('quality', 'OTXK boshlig‘i'),
          ('shop', 'Ishlab chiqarish sexi boshlig‘i'), ('accountant', 'Bosh hisobchi'),
          ('elevator', 'Elevator ombori mudiri'), ('warehouse', 'Mahsulot ombori mudiri')),
}
OVERRIDES = (
    ('actual_pct', 'Haqiqiy %'), ('difference_pct', 'Og‘ish, foiz bandi'),
    ('difference_kg', 'Og‘ish, kg'),
)


def prefixed(prefix, fields):
    return tuple((f'{prefix}_{key}', label, kind) for key, label, kind in fields)


def fields(number):
    common = (
        ('organization', 'Tashkilot', 'text'), ('document', 'Dalolatnoma raqami', 'text'),
        ('document_date', 'Dalolatnoma sanasi', 'date'), ('approval_date', 'Tasdiqlash sanasi', 'date'),
    )
    signature_fields = tuple(item for role, label in SIGNS[number] for item in (
        (f'{role}_name', label, 'text'), (f'{role}_signature', f'{label} · imzo hujjati', 'text')))
    if number == 34:
        return (
            ('Topshiriq va dalolatnoma', common + (
                ('warehouse_no', 'Ombor / elevator raqami', 'text'),
                ('destination', 'Tozalangan don joylashtiriladigan ombor / silos', 'text'),
                ('deadline', 'Topshiriq muddati', 'date'), ('department', 'Tozalash bo‘limi', 'text'),
                ('start_day', 'Tozalash boshlangan sana', 'date'), ('end_day', 'Tozalash tugagan sana', 'date'),
                ('scale', 'Tarozi va tortim ilovasi', 'text'), ('lab_report', 'Laboratoriya varaqasi', 'text'),
                ('ledger_confirmed', 'Nakladnoy miqdori jadvalga kiritilganini tasdiqlang', 'check'),
                ('cleaning_balance_adjustment', 'Hujjat bilan asoslangan og‘irlik farqi, kg', 'signed'),
                ('cleaning_balance_basis', 'Og‘irlik farqi asosi va hujjati', 'text'),
            )),
            ('Tozalashdan oldin', prefixed('raw', GRAIN)),
            ('Tozalashdan keyin', prefixed('clean', GRAIN)),
            *((label, prefixed(key, WASTE_COLUMNS)) for key, label in WASTE),
            ('Quritish · qo‘llansa', (
                ('dry_start', 'Boshlangan sana', 'date'), ('dry_end', 'Tugagan sana', 'date'),
                ('dry_method', 'Usul va uskuna', 'text'), ('dry_kg', 'Quritilgan miqdor, kg', 'kg'),
                ('dry_before', 'Oldingi namlik, %', 'percent'), ('dry_before_ref', 'Oldingi tahlil raqami', 'text'),
                ('dry_after', 'Keyingi namlik, %', 'percent'), ('dry_after_ref', 'Keyingi tahlil raqami', 'text'),
                ('dry_difference', 'Hujjatdagi namlik kamayishi, %', 'percent'),
                ('warehouse_before', 'Saqlash joyi tozalanishidan oldingi hisob, kg', 'kg'),
                ('planned_tons', 'Rejalashtirilgan ish hajmi, t', 'kg'),
            )),
            ('Mas’ul shaxslar va tasdiq', signature_fields),
        )
    return (
        ('Dalolatnoma', common + (
            ('organization_code', 'KTUT kodi', 'text'), ('report_month', 'Hisobot oyi', 'month'),
            ('scale', 'Tarozi tizimi, sig‘imi va holati', 'text'),
            ('waste_note', 'Donli chiqindi topshirish izohi', 'text'),
            ('remarks', 'Zachiska va omborga topshirish qaydi', 'text'),
        )),
        ('75% un tortish · hujjatli tuzatishlar', tuple(
            (f'form117_{key}', label, 'signed' if key == 'f13' else 'percent')
            for key, label in form117.CORRECTION_LABELS.items()
        ) + tuple((f'calc_{row}', f'{row}-qator · laboratoriya hisobi matni', 'text') for row in (13,14,15,16,17,18,19))
        + tuple((f'calc_{cell}', f'{cell} · hujjatli qo‘shimcha tuzatish, foiz bandi', 'signed')
                for cell in ('H16','J16','K16','L16','H19','J19'))),
        ('Haqiqiy chiqish', tuple(
            (f'form117_{key}', f'{label}, kg', 'signed' if key == 'moisture' else 'kg')
            for key, label in form117.OUTPUT_LABELS.items()) + (
                ('form117_document', 'Chiqish hisobi va laboratoriya hujjati', 'text'),
        ) + tuple((f'actual_basis_{key}', f'{label} · manbadagi yozuv yoki hisob izohi', 'text')
                  for key, label in form117.OUTPUT_LABELS.items())),
        ('Qo‘lda yozilgan istisnolar · hujjat bo‘lsa', tuple(
            (f'override_{key}_{metric}', f'{label} · {title}', 'signed')
            for key, label in form117.OUTPUT_LABELS.items() for metric, title in OVERRIDES) + (
                ('override_total_difference_pct', 'O25 · jami og‘ish, foiz bandi', 'signed'),
                ('override_total_difference_kg', 'O26 · jami og‘ish, kg', 'signed'),
                ('override_total_actual_pct', 'O23 · jami haqiqiy %', 'signed'),
                ('override_basis', 'Istisno sababi, varaq/katak va hujjat raqami', 'text'),
        )),
        ('Don sifati · laboratoriya', tuple(
            (f'cell_{column}{row}', f'{label} · {side}', 'text')
            for row, label in GRAIN_QUALITY for column, side in (('C','oldin'),('D','keyin'))) + (
                ('cell_B13', 'B13 · chiqish hisobidagi namlik, %', 'percent'),
                ('cell_B14', 'B14 · chiqish hisobidagi donli aralashma va mayda don, %', 'percent'),
                ('cell_B16', 'B16 · chiqish hisobidagi iflos aralashma, %', 'percent'),
                ('cell_B17', 'B17 · chiqish hisobidagi natura, g/l', 'kg'),
                ('cell_B18', 'B18 · chiqish hisobidagi shaffoflik, %', 'percent'),
                ('cell_B19', 'B19 · chiqish hisobidagi kullilik, %', 'percent'),
        )),
        ('Mahsulot sifati · laboratoriya', tuple(
            (f'cell_{column}{row}', f'{label} · {product}', 'text')
            for row, label in PRODUCT_QUALITY for column, product in PRODUCT_QUALITY_COLS) + (
                ('cell_N37', 'Nostandart mahsulot, kg', 'kg'),
                ('cell_O37', 'Nostandart mahsulot, %', 'percent'),
                ('cell_P37', 'Nostandart mahsulot qo‘shimcha miqdori, kg', 'kg'),
        )),
        ('Namlik bo‘yicha Excel yordamchi kataklari · manba bo‘lsa', (
            ('cell_R22', 'R22 · manbadagi namlik, %', 'percent'),
            ('cell_S22', 'S22 · taqqoslash namligi, %', 'percent'),
            ('cell_R23', 'R23 · manbadagi baza, %', 'percent'),
            ('cell_S23', 'S23 · manbadagi tuzatish, %', 'percent'),
            ('cell_T29', 'T29 · un namligi, %', 'percent'),
            ('cell_T30', 'T30 · kepak namligi, %', 'percent'),
            ('cell_T37', 'T37 · taqqoslash un namligi, %', 'percent'),
            ('cell_T38', 'T38 · taqqoslash kepak namligi, %', 'percent'),
        )),
        ('Mas’ul shaxslar va tasdiq', signature_fields),
    )


def parse(number, form, period):
    values = {}
    for _, group in fields(number):
        for key, label, kind in group:
            raw = form.get(key, '').strip()
            if not raw:
                continue
            if kind in ('kg', 'percent', 'signed'):
                value = (form117.signed_amount if kind == 'signed' else amount)(raw, label)
                if kind == 'percent' and value > 100:
                    raise ValueError(f'{label} 100% dan oshmasin.')
            elif kind in ('date', 'month'):
                parsed = date.fromisoformat(raw if kind == 'date' else raw+'-01').isoformat()
                value = parsed if kind == 'date' else parsed[:7]
                if kind == 'date' and key != 'deadline' and value > date.today().isoformat():
                    raise ValueError(f'{label} kelajakda bo‘lmasin.')
            elif len(raw) <= 250:
                value = raw
            else:
                raise ValueError(f'{label} 250 belgidan oshmasin.')
            values[key] = value
    for begin, end in (('start_day','end_day'),('dry_start','dry_end')):
        if values.get(begin) and values.get(end) and values[begin] > values[end]:
            raise ValueError('Boshlanish sanasi tugash sanasidan keyin bo‘lmasin.')
        if any(values[k] < period['start_day'] for k in (begin,end) if k in values):
            raise ValueError('Hujjat sanalari ROP davridan oldin bo‘lmasin.')
    if number == 34:
        if values.get('cleaning_balance_adjustment') and not values.get('cleaning_balance_basis'):
            raise ValueError('Og‘irlik farqi uchun asos va hujjatni kiriting.')
        for key, label in WASTE:
            parts = [values.get(f'{key}_{name}') for name in ('main','grain','foreign')]
            if sum(part or 0 for part in parts) > 100.001:
                raise ValueError(f'{label}: sifat qismlari jami 100% dan oshdi.')
    elif any(key.startswith('override_') and key!='override_basis' for key in values) and 'override_basis' not in values:
        raise ValueError('Qo‘lda yozilgan istisno uchun manba va sabab kerak.')
    return values


def get(con, period_id, number):
    row = con.execute('''SELECT revision, data FROM rop_form_documents
        WHERE period_id=? AND number=?''', (period_id, number)).fetchone()
    return (row['revision'], json.loads(row['data'])) if row else (0, {})


def save(con, period, number, revision, form, user_id):
    if period['stop_day'] is not None:
        raise ValueError('Yopilgan ROP davri hujjati o‘zgartirilmaydi.')
    if get(con, period['id'], number)[0] != revision:
        raise ValueError('Hujjat boshqa so‘rovda o‘zgargan. Formani qayta oching.')
    values = parse(number, form, period)
    con.execute('''INSERT INTO rop_form_documents(period_id,number,revision,data,updated_by)
        VALUES(?,?,?,?,?) ON CONFLICT(period_id,number) DO UPDATE SET
        revision=excluded.revision,data=excluded.data,updated_by=excluded.updated_by,
        updated_at=CURRENT_TIMESTAMP''',
        (period['id'], number, revision+1, json.dumps(values, ensure_ascii=False), user_id))


def cleaning(values):
    amounts = [values.get(f'{key}_kg') for key, _ in WASTE]
    total = None if all(n is None for n in amounts) else sum(Decimal(str(n or 0)) for n in amounts)
    complete = all(n is not None for n in amounts)
    balance = None
    if complete and 'raw_kg' in values and 'clean_kg' in values:
        balance = Decimal(str(values['raw_kg'])) - Decimal(str(values['clean_kg'])) - total
    return (float(total) if total is not None else None, complete, float(balance) if balance is not None else None)


def yield_report(values, cleaned_kg):
    inputs = {k: str(v) for k, v in values.items()}
    report = form117.from_form(inputs, cleaned_kg)
    if report is None:
        return None
    for key, row in report['rows'].items():
        actual_override = f'override_{key}_actual_pct'
        if actual_override in values:
            row['difference_pct'] = row['calculated_pct'] - values[actual_override]
        for metric, _ in OVERRIDES:
            name = f'override_{key}_{metric}'
            if name in values:
                row[metric] = values[name]
                row.setdefault('manual', []).append(metric)
    report['override_basis'] = values.get('override_basis')
    report['total_overrides'] = {metric: values[f'override_total_{metric}']
                                 for metric in ('actual_pct','difference_pct','difference_kg')
                                 if f'override_total_{metric}' in values}
    return report


def closing_values(con, period, day, measured_clean, ledger_kg, report_month):
    _, a = get(con, period['id'], 34)
    _, b = get(con, period['id'], 117)
    if a:
        total, complete, _ = cleaning(a)
        if not complete or 'clean_kg' not in a or not a.get('start_day') or not a.get('end_day') or not a.get('document'):
            raise ValueError('34-shaklda davr sanalari, hujjat, tozalangan don va har bir chiqindi qatori kerak.')
        if a['end_day'] > day:
            raise ValueError('34-shakl tozalash davri Zachiska sanasidan keyin tugaydi.')
        if a['clean_kg'] != measured_clean:
            raise ValueError('34-shakldagi tozalangan don ERP davr tortimiga mos kelmadi.')
        if ledger_kg is not None and (not a.get('ledger_confirmed') or total < ledger_kg):
            raise ValueError('34-shakl jami ichida mavjud nakladnoy chiqindisini tasdiqlang.')
        if con.execute('''SELECT 1 FROM rop_grain_waste
            WHERE period_id=? AND (day<? OR day>?) LIMIT 1''',
            (period['id'],a['start_day'],a['end_day'])).fetchone():
            raise ValueError('34-shakl sanalari davrdagi barcha chiqindi nakladnoylarini qamrab olishi kerak.')
    if b and b.get('report_month') != report_month:
        raise ValueError('117-shakl hisobot oyi Zachiska hisobot oyiga mos kelmadi.')
    report = yield_report(b, a.get('clean_kg') if a else measured_clean) if b else None
    if b and report is None:
        raise ValueError('Saqlangan 117-shaklning chiqish hisobini to‘ldiring.')
    return a, b, report


def document_status(number, values, *, ledger_kg=None, report=None, linked34=False, closed=False):
    required = {'organization','document','document_date','approval_date'}
    required.update(f'{role}_{part}' for role, _ in SIGNS[number] for part in ('name','signature'))
    if number == 34:
        required.update(('warehouse_no','destination','deadline','department','start_day','end_day','scale','lab_report'))
        required.update(f'{prefix}_{key}' for prefix in ('raw','clean') for key, _, _ in GRAIN)
        required.update(f'{key}_kg' for key, _ in WASTE)
        required.update(f'{key}_{field}' for key, _ in WASTE if values.get(f'{key}_kg')
                        for field, _, _ in WASTE_COLUMNS)
        if any(key.startswith('dry_') for key in values):
            required.update(key for key, _, _ in dict(fields(34))['Quritish · qo‘llansa'] if key.startswith('dry_'))
        total, _, balance = cleaning(values)
        if ledger_kg is not None:
            required.add('ledger_confirmed')
        complete = (balance == values.get('cleaning_balance_adjustment',0)
                    and (ledger_kg is None or (total is not None and total >= ledger_kg)))
    else:
        required.update(('organization_code','report_month','scale','remarks'))
        # The supplied 75% profile reports the first flour grade and bran.
        required.update(f'cell_{column}{row}' for row, _ in PRODUCT_QUALITY for column in ('I','L'))
        complete = report is not None and linked34 and closed
    return 'TASDIQLANGAN' if complete and required <= values.keys() else 'LOYIHA — TASDIQLANMAGAN'
