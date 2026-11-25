"""Pagination template context; unchanged request/URL behavior, no database access."""


def build_pagination_context(request, url_for):
    def pagination_url(page, pagination):
        values = request.args.to_dict(flat=False)
        values[pagination['page_param']] = str(page)
        values[pagination['per_page_param']] = str(pagination['per_page'])
        route_values = dict(request.view_args or {})
        route_values.update(values)
        return url_for(request.endpoint, **route_values)
    return {'pagination_url': pagination_url}
