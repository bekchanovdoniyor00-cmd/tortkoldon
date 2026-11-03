import json
import struct
import unittest

import app as erp


class PwaTests(unittest.TestCase):
    def setUp(self):
        self.original_testing = erp.app.config['TESTING']
        erp.app.config['TESTING'] = True
        self.client = erp.app.test_client()

    def tearDown(self):
        erp.app.config['TESTING'] = self.original_testing

    def test_manifest_declares_installable_company_app(self):
        with self.client.get('/static/manifest.webmanifest') as response:
            self.assertEqual(response.status_code, 200)
            manifest = json.loads(response.get_data(as_text=True))
        self.assertEqual(manifest['name'], 'TO‘RTKO‘LDONMAXSULOTI')
        self.assertEqual(manifest['short_name'], 'TO‘RTKO‘L DON')
        self.assertEqual(manifest['display'], 'standalone')
        self.assertEqual(manifest['scope'], '/')
        self.assertEqual(manifest['theme_color'], '#073d2e')
        self.assertEqual(
            {icon['sizes'] for icon in manifest['icons']},
            {'192x192', '512x512'},
        )

    def test_pwa_icons_are_valid_pngs_with_declared_dimensions(self):
        for size in (192, 512):
            with self.subTest(size=size):
                with self.client.get(f'/static/pwa-icon-{size}.png') as icon:
                    self.assertEqual(icon.status_code, 200)
                    self.assertEqual(icon.data[:8], b'\x89PNG\r\n\x1a\n')
                    self.assertEqual(
                        struct.unpack('>II', icon.data[16:24]), (size, size)
                    )

    def test_service_worker_is_root_scoped_and_network_only(self):
        with self.client.get('/service-worker.js') as response:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers['Service-Worker-Allowed'], '/')
            self.assertEqual(response.headers['Cache-Control'], 'no-cache')
            source = response.get_data(as_text=True)
        self.assertIn("event.respondWith(", source)
        self.assertIn("fetch(request).catch(", source)
        self.assertIn('Ma’lumotlar saqlanmadi.', source)
        self.assertNotIn('caches.open(', source)

    def test_login_page_has_install_metadata_and_registers_worker(self):
        with self.client.get('/login') as response:
            self.assertEqual(response.status_code, 200)
            page = response.get_data(as_text=True)

        self.assertIn('rel="manifest"', page)
        self.assertIn('name="theme-color" content="#073d2e"', page)
        self.assertIn('apple-mobile-web-app-capable', page)
        self.assertIn('apple-mobile-web-app-title', page)
        self.assertIn('pwa-icon-180.png', page)
        self.assertIn('navigator.serviceWorker.register(', page)
        self.assertIn('window.isSecureContext', page)


if __name__ == '__main__':
    unittest.main()
