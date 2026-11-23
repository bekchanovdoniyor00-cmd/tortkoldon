"""Read-only invoice archive page; original SQL, ordering and template preserved."""


def render_invoices_page(*, INVOICE_GROUPS, db, paginate_rows, render_template, request):
    search=request.args.get('q','').strip()[:100]
    search_conditions="""(
        instr(lower(COALESCE(CAST(i.issued_date AS TEXT),'')),lower(?))>0 OR
        instr(lower(substr(business_date_key(i.issued_date),9,2)||'.'||
            substr(business_date_key(i.issued_date),6,2)||'.'||
            substr(business_date_key(i.issued_date),1,4)),lower(?))>0 OR
        instr(lower(CASE s.product
            WHEN 'Robiya' THEN 'Oliy nav un'
            WHEN 'Sara' THEN '1-nav un'
            WHEN 'Yevro' THEN '1-nav un'
            WHEN 'Zar Bug‘doy' THEN '1-nav un'
            WHEN 'Telashka atxod' THEN 'Teleshka atxod'
            ELSE s.product END),lower(?))>0 OR
        instr(lower(COALESCE(s.customer,'')),lower(?))>0 OR
        instr(lower(COALESCE(s.customer_phone,'')),lower(?))>0 OR
        instr(lower(COALESCE(s.product,'')),lower(?))>0 OR
        instr(printf('%.2f',COALESCE(s.qty_kg,0)),lower(?))>0 OR
        instr(CAST(i.number AS TEXT),?)>0 OR
        (s.user_id IN (3,4) AND instr(lower(
           (CASE s.user_id WHEN 3 THEN 'U-' WHEN 4 THEN 'M-' END) ||
             printf('%03d', (SELECT COUNT(*) FROM invoices j
                    JOIN sales sj ON sj.id=j.sale_id
                    WHERE sj.user_id=s.user_id AND j.month_key=i.month_key AND j.id<=i.id))
           ), lower(?))>0)
    )"""
    search_params=(search,search,search,search,search,search,search,search,search)
    with db() as con:
        rows,pagination=paginate_rows(
            con,
            f"""SELECT i.*, s.customer, s.customer_phone, s.product, s.qty_kg
                FROM invoices i JOIN sales s ON s.id=i.sale_id
                WHERE {search_conditions}
                ORDER BY business_date_key(i.issued_date) DESC, i.id DESC""",
            f"""SELECT COUNT(*) FROM invoices i JOIN sales s ON s.id=i.sale_id
                WHERE {search_conditions}""",
            search_params,
        )
    return render_template('invoices.html', rows=rows, groups=INVOICE_GROUPS,
        pagination=pagination,search=search)
