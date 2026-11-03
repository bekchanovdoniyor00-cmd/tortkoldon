self.addEventListener('install', event => {
    event.waitUntil(self.skipWaiting());
});

self.addEventListener('activate', event => {
    event.waitUntil(self.clients.claim());
});

self.addEventListener('fetch', event => {
    const request = event.request;
    if (new URL(request.url).origin !== self.location.origin) return;
    if (request.method !== 'GET') return;

    event.respondWith(
        fetch(request).catch(() => {
            if (request.mode !== 'navigate') return Response.error();

            return new Response(
                '<!doctype html><html lang="uz"><meta charset="utf-8">' +
                '<meta name="viewport" content="width=device-width,initial-scale=1">' +
                '<title>Server bilan aloqa yo‘q</title>' +
                '<body><main><h1>Server bilan aloqa yo‘q</h1>' +
                '<p>ERP bilan ishlash uchun internet va server aloqasi kerak. ' +
                'Ma’lumotlar saqlanmadi. Aloqa tiklangach sahifani qayta yuklang.</p>' +
                '</main></body></html>',
                {
                    status: 503,
                    headers: {
                        'Content-Type': 'text/html; charset=utf-8',
                        'Cache-Control': 'no-store'
                    }
                }
            );
        })
    );
});
