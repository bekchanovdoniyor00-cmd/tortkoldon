"""Verified 75% milling profile from the supplied Form 117 workbook."""
from decimal import Decimal

from bulk_flour_service import amount


OUTPUT_LABELS = {
    'flour': 'Un',
    'bran': 'Kepak',
    'feed': 'Yem-bop don mahsuloti',
    'waste': 'Yaroqsiz chiqindi va mexanik yo‘qotishlar',
    'moisture': 'Qurish / namlanish',
}
CORRECTION_LABELS = {
    'f13': 'F13 · namlik tuzatishi',
    'f14': 'F14 · donli aralashma va mayda don',
    'f15': 'F15 · un va kepak o‘rtasidagi tuzatish',
}


def signed_amount(value, label):
    value = str(value).strip()
    if value.startswith('-'):
        return -amount(value[1:], label)
    return amount(value, label)


def from_form(form, cleaned_kg):
    fields = [form.get(f'form117_{key}', '').strip()
              for key in (*CORRECTION_LABELS, *OUTPUT_LABELS, 'document')]
    if not any(fields):
        return None
    if not all(fields) or len(fields[-1]) > 250:
        raise ValueError('117-shaklning barcha qiymatlari va hujjat raqamini kiriting.')
    if not cleaned_kg:
        raise ValueError('117-shakl uchun o‘lchangan tozalangan bug‘doy kerak.')
    moisture, feed, flour_to_bran = (
        Decimal(str((signed_amount if key == 'f13' else amount)(form[f'form117_{key}'], label)))
        for key, label in CORRECTION_LABELS.items())
    actual = {
        key: Decimal(str((signed_amount if key == 'moisture' else amount)(
            form[f'form117_{key}'], label)))
        for key, label in OUTPUT_LABELS.items()
    }
    clean = Decimal(str(cleaned_kg))
    if sum(actual.values()) != clean:
        raise ValueError('117-shaklning ishorali haqiqiy chiqish jami tozalangan bug‘doyga mos kelmadi.')
    # The workbook stores percentages as numbers such as 75, not 0.75.
    base_flour, base_bran = Decimal(75), Decimal('21.5')
    split = (moisture - feed) / (base_flour + base_bran)
    def documented(cell):
        return Decimal(str(signed_amount(form.get(f'calc_{cell}') or '0', cell)))
    for row in (16, 19):
        if form.get(f'calc_H{row}') and documented(f'H{row}') != documented(f'J{row}'):
            raise ValueError('75% bir navli hisobda H va J tuzatishlari mos bo‘lishi kerak.')
    calculated = {
        'flour': base_flour + split * base_flour - flour_to_bran + documented('J16') + documented('J19'),
        'bran': base_bran + split * base_bran + flour_to_bran + documented('K16'),
        'feed': Decimal('2.8') + feed + documented('L16'),
        'waste': Decimal('0.7'),
        'moisture': -moisture,
    }
    if any(calculated[key] < 0 for key in OUTPUT_LABELS if key != 'moisture'):
        raise ValueError('117-shaklning hisobiy mahsulot chiqishi manfiy bo‘ldi.')
    if abs(sum(calculated.values()) - 100) > Decimal('0.000001'):
        raise ValueError('117-shakl hisobiy foizlari jami 100% ga mos kelmadi.')
    rows = {}
    for key, rate in calculated.items():
        planned_kg = rate * clean / 100
        actual_pct = actual[key] * 100 / clean
        rows[key] = {
            'calculated_pct': float(rate), 'calculated_kg': float(planned_kg),
            'actual_pct': float(actual_pct), 'actual_kg': float(actual[key]),
            'difference_pct': float(rate - actual_pct),
            'difference_kg': float(actual[key] - planned_kg),
        }
    return {
        'document': fields[-1], 'cleaned_kg': cleaned_kg, 'rows': rows,
        'corrections': {'f13': float(moisture), 'f14': float(feed), 'f15': float(flour_to_bran)},
    }
