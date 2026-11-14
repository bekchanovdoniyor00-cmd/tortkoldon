"""Pagination utilities extracted without changing behavior from app.py."""

from flask import request

PAGINATION_SIZES = (20, 50, 100)


def _pagination_state(total, page_param='page', per_page_param='per_page'):
    try:
        per_page = int(request.args.get(per_page_param, '20'))
    except (TypeError, ValueError):
        per_page = 20
    if per_page not in PAGINATION_SIZES:
        per_page = 20
    try:
        requested_page = int(request.args.get(page_param, '1'))
    except (TypeError, ValueError):
        requested_page = 1
    total = max(0, int(total))
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(max(1, requested_page), pages)
    if pages <= 7:
        page_window = list(range(1, pages + 1))
    else:
        page_window = [1]
        if page > 4:
            page_window.append(None)
        page_window.extend(range(max(2, page - 1), min(pages, page + 1) + 1))
        if page < pages - 3:
            page_window.append(None)
        page_window.append(pages)
    return {
        'total': total,
        'per_page': per_page,
        'page': page,
        'pages': pages,
        'offset': (page - 1) * per_page,
        'start': (page - 1) * per_page + 1 if total else 0,
        'end': min(page * per_page, total),
        'window': page_window,
        'page_param': page_param,
        'per_page_param': per_page_param,
    }


def paginate_rows(con, select_sql, count_sql, params=(),
                  page_param='page', per_page_param='per_page'):
    total = con.execute(count_sql, params).fetchone()[0]
    pagination = _pagination_state(total, page_param, per_page_param)
    rows = con.execute(
        f'{select_sql} LIMIT ? OFFSET ?',
        (*params, pagination['per_page'], pagination['offset']),
    ).fetchall()
    return rows, pagination


def paginate_items(items, page_param='page', per_page_param='per_page'):
    pagination = _pagination_state(len(items), page_param, per_page_param)
    start = pagination['offset']
    return items[start:start + pagination['per_page']], pagination
