/**
 * Ouroboros PWA service worker.
 * Caches the UI shell; API and WebSocket always use the network.
 */

const CACHE_SHELL = 'ouroboros-shell-v3';

const PRECACHE_URLS = [
    '/',
    '/static/style.css',
    '/static/settings.css',
    '/static/onboarding.css',
    '/static/app.js',
    '/static/chart.umd.min.js',
    '/static/marked.min.js',
    '/static/purify.min.js',
    '/static/icons/favicon.ico',
    '/static/icons/favicon-16.png',
    '/static/icons/favicon-32.png',
    '/static/icons/favicon-48.png',
    '/static/icons/favicon-76.png',
    '/static/icons/apple-touch-icon.png',
    '/static/icons/icon-192x192.png',
    '/static/icons/icon-512x512.png',
];

function isApiOrLiveRequest(url) {
    return url.pathname.startsWith('/api/')
        || url.pathname.startsWith('/auth/')
        || url.pathname === '/ws';
}

function isStaticAsset(url) {
    return url.pathname.startsWith('/static/');
}

self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open(CACHE_SHELL)
            .then(async (cache) => {
                await Promise.allSettled(
                    PRECACHE_URLS.map((url) => cache.add(url)),
                );
            })
            .then(() => self.skipWaiting()),
    );
});

self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys()
            .then((keys) => Promise.all(
                keys
                    .filter((key) => key.startsWith('ouroboros-shell-') && key !== CACHE_SHELL)
                    .map((key) => caches.delete(key)),
            ))
            .then(() => self.clients.claim()),
    );
});

self.addEventListener('fetch', (event) => {
    const { request } = event;
    if (request.method !== 'GET') return;

    const url = new URL(request.url);
    if (url.origin !== self.location.origin) return;
    if (isApiOrLiveRequest(url)) return;

    if (request.mode === 'navigate') {
        event.respondWith(
            fetch(request)
                .then((response) => {
                    const copy = response.clone();
                    caches.open(CACHE_SHELL).then((cache) => cache.put('/', copy));
                    return response;
                })
                .catch(() => caches.match('/') || caches.match('/index.html')),
        );
        return;
    }

    if (isStaticAsset(url)) {
        event.respondWith(
            caches.open(CACHE_SHELL).then(async (cache) => {
                const cached = await cache.match(request);
                const networkPromise = fetch(request).then((response) => {
                    if (response.ok) cache.put(request, response.clone());
                    return response;
                });
                return cached || networkPromise;
            }),
        );
    }
});

self.addEventListener('message', (event) => {
    if (event.data && event.data.type === 'SKIP_WAITING') {
        self.skipWaiting();
    }
});
