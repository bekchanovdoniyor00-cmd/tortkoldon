"""Static asset handlers. Kept independent from app.py to avoid import cycles."""

from flask import current_app, send_from_directory


def serve_service_worker():
    response = send_from_directory(
        current_app.static_folder, 'service-worker.js', mimetype='application/javascript'
    )
    response.headers['Service-Worker-Allowed'] = '/'
    response.headers['Cache-Control'] = 'no-cache'
    return response


def serve_robots_txt():
    return send_from_directory(
        current_app.static_folder, 'robots.txt', mimetype='text/plain'
    )


def serve_sitemap_xml():
    return send_from_directory(
        current_app.static_folder, 'sitemap.xml', mimetype='application/xml'
    )
