import os
import json
import re
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.cookiejar import CookieJar
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPCookieProcessor, build_opener


BASE_URL = 'http://127.0.0.1:5052'


class WaitressHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database_path = os.environ.get('WAITRESS_TEST_DATABASE')
        if not database_path:
            raise RuntimeError('WAITRESS_TEST_DATABASE vaqtinchalik yo‘li berilishi kerak.')
        with open(database_path.replace('waitress_test.sqlite', 'test_credentials.json'),
                  encoding='utf-8') as credentials_file:
            cls.credentials = json.load(credentials_file)
        cls.clients = {}
        cls.paths = {
            'buxgalter': '/',
            'rahbar': '/boshliq/statistika',
            'umid': '/xodim/sotuvlar',
            'maxmud': '/xodim/sotuvlar',
        }

    def open(self, client, path):
        return client.open(urljoin(BASE_URL, path))

    def token_for(self, client, path):
        response = self.open(client, path)
        body = response.read().decode('utf-8')
        response.close()
        match = re.search(r'name="csrf_token" value="([^"]+)"', body)
        self.assertIsNotNone(match, f'CSRF token not found at {path}')
        return match.group(1)

    def post_form(self, client, path, values):
        return client.open(
            urljoin(BASE_URL, path),
            data=urlencode(values).encode('utf-8'),
        )

    def test_server_is_loopback_and_serves_pages(self):
        import socket

        connections = socket.getaddrinfo('127.0.0.1', 5052, type=socket.SOCK_STREAM)
        self.assertTrue(connections)
        response = self.open(build_opener(), '/login')
        self.assertEqual(response.status, 200)
        response.close()

        response = self.open(build_opener(), '/')
        self.assertTrue(urlsplit(response.geturl()).path.endswith('/login'))
        response.close()

    def test_all_roles_login_open_dashboard_and_logout(self):
        for username, details in self.credentials.items():
            role = details['role']
            with self.subTest(role=role):
                client = build_opener(HTTPCookieProcessor(CookieJar()))
                token = self.token_for(client, '/login')
                response = self.post_form(
                    client,
                    '/login',
                    {
                        'username': username,
                        'password': details['password'],
                        'csrf_token': token,
                    },
                )
                self.assertEqual(response.status, 200)
                self.assertEqual(urlsplit(response.geturl()).path, self.paths[role])
                self.assertIn(b'<html', response.read().lower())
                response.close()

                token = self.token_for(client, self.paths[role])
                response = self.post_form(
                    client,
                    '/logout',
                    {'csrf_token': token},
                )
                self.assertEqual(response.status, 200)
                self.assertTrue(urlsplit(response.geturl()).path.endswith('/login'))
                response.close()

                client = build_opener(HTTPCookieProcessor(CookieJar()))
                token = self.token_for(client, '/login')
                response = self.post_form(
                    client,
                    '/login',
                    {
                        'username': username,
                        'password': details['password'],
                        'csrf_token': token,
                    },
                )
                self.assertEqual(urlsplit(response.geturl()).path, self.paths[role])
                response.close()
                self.clients[role] = client

    def test_language_switch_and_pwa_resources_over_http(self):
        client = self.clients['buxgalter']
        token = self.token_for(client, '/')
        response = self.post_form(
            client,
            '/language',
            {'language': 'uz-Cyrl', 'next': '/', 'csrf_token': token},
        )
        self.assertEqual(response.status, 200)
        body = response.read().decode('utf-8')
        response.close()
        self.assertIn('uz-Cyrl', body)
        self.assertEqual(urlsplit(self.open(client, '/').geturl()).path, '/')

        for path in (
            '/static/manifest.webmanifest',
            '/service-worker.js',
            '/static/pwa-icon-192.png',
            '/static/pwa-icon-512.png',
        ):
            with self.subTest(path=path):
                response = self.open(build_opener(), path)
                self.assertEqual(response.status, 200)
                response.close()

    def test_concurrent_authenticated_requests(self):
        def fetch_dashboard(role):
            response = self.open(self.clients[role], self.paths[role])
            status = response.status
            response.close()
            return status

        roles = ['buxgalter', 'rahbar', 'umid', 'maxmud'] * 3
        with ThreadPoolExecutor(max_workers=8) as executor:
            statuses = list(executor.map(fetch_dashboard, roles))
        self.assertEqual(statuses, [200] * len(roles))


if __name__ == '__main__':
    unittest.main()
