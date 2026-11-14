"""Accountant worker XLSX export; original Stage14 body preserved."""


def export_accountant_worker_payments_excel(_app_dependencies):
    PAYMENT_METHODS = _app_dependencies['PAYMENT_METHODS']
    abort = _app_dependencies['abort']
    current_user = _app_dependencies['current_user']
    customer_display = _app_dependencies['customer_display']
    customer_identity_context = _app_dependencies['customer_identity_context']
    db = _app_dependencies['db']
    if current_user()['role'] not in ('buxgalter','rahbar'): abort(403)
    from flask import Response
    from io import BytesIO
    from zipfile import ZipFile, ZIP_DEFLATED
    from xml.sax.saxutils import escape
    from openpyxl.utils.datetime import to_excel
    from excel_dates import excel_date
    with db() as con:
        customer_context=customer_identity_context(con)
        rows=con.execute("""SELECT a.payment_date,u.username worker_name,a.customer,a.customer_phone,a.payment_method,a.amount,au.username accountant_name,a.note FROM accountant_worker_receipts a JOIN users u ON u.id=a.worker_id JOIN users au ON au.id=a.accountant_id ORDER BY a.id DESC""").fetchall()
    headings=['Sana','Xodim','Xaridor','Telefon','To‘lov turi','Summa (so‘m)','Qabul qilgan','Izoh']
    values=[headings]
    for r in rows:
        customer,phone=customer_display(customer_context,r['customer'],r['customer_phone'])
        values.append([r['payment_date'],r['worker_name'],customer,phone,
            PAYMENT_METHODS.get(r['payment_method'],r['payment_method']),
            float(r['amount']),r['accountant_name'],r['note']])
    def col(n):
        ret=''
        while n:
            n,k=divmod(n-1,26);ret=chr(65+k)+ret
        return ret
    rendered=[]
    for y,line in enumerate(values,1):
        cells=[]
        for x,v in enumerate(line,1):
            addr=col(x)+str(y)
            if y > 1 and x == 1:
                try: value = excel_date(v)
                except ValueError as exc: abort(400, str(exc))
                cells.append('<c r="%s" s="1">%s</c>' % (addr, '<v>%s</v>' % to_excel(value) if value is not None else ''))
            elif isinstance(v,(float,int)):
                cells.append('<c r="%s"><v>%s</v></c>'%(addr,str(v)))
            else:
                cells.append('<c r="%s" t="inlineStr"><is><t>%s</t></is></c>'%(addr,escape(str(v or ''))))
        rendered.append('<row r="%s">%s</row>'%(y,''.join(cells)))
    NS='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    worksheet=('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="'+NS+'"><sheetViews><sheetView workbookViewId="0">'
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        '</sheetView></sheetViews><cols>'
        '<col min="1" max="1" width="16" customWidth="1"/>'
        '<col min="2" max="3" width="24" customWidth="1"/>'
        '<col min="4" max="4" width="21" customWidth="1"/>'
        '<col min="5" max="5" width="24" customWidth="1"/>'
        '<col min="6" max="6" width="20" customWidth="1"/>'
        '<col min="7" max="8" width="25" customWidth="1"/>'
        '</cols><sheetData>'+''.join(rendered)+'</sheetData>'
        '<autoFilter ref="A1:H'+str(len(values))+'"/></worksheet>')
    b=BytesIO()
    with ZipFile(b,'w',ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml','<?xml version="1.0" encoding="UTF-8"?>'
           '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
           '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
           '<Default Extension="xml" ContentType="application/xml"/>'
           '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
           '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
           '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
           '</Types>')
        z.writestr('_rels/.rels','<?xml version="1.0" encoding="UTF-8"?>'
           '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
           '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
           '</Relationships>')
        z.writestr('xl/workbook.xml','<?xml version="1.0" encoding="UTF-8"?>'
           '<workbook xmlns="'+NS+'" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
           '<sheets><sheet name="Qarz tolovlari" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels','<?xml version="1.0" encoding="UTF-8"?>'
           '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
           '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
           '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
           '</Relationships>')
        z.writestr('xl/styles.xml','<?xml version="1.0" encoding="UTF-8"?>'
           '<styleSheet xmlns="'+NS+'"><numFmts count="1"><numFmt numFmtId="164" formatCode="dd.mm.yy"/></numFmts>'
           '<fonts count="1"><font><name val="Calibri"/><family val="2"/><color theme="1"/><sz val="11"/><scheme val="minor"/></font></fonts>'
           '<fills count="2"><fill><patternFill/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
           '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
           '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
           '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
           '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/></cellXfs>'
           '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')
        z.writestr('xl/worksheets/sheet1.xml',worksheet)
    return Response(b.getvalue(),mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition':'attachment; filename="buxgalter_xodim_qarz_tolovlari.xlsx"'})
