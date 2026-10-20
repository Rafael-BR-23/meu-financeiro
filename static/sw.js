// Service Worker para Meu Financeiro PWA
const CACHE_NAME = 'meu-financeiro-v5';
const STATIC_ASSETS = [
  '/static/manifest.json',
  '/static/icon-192.png',
  '/static/icon-512.png',
  '/static/icon.svg'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      return cache.addAll(STATIC_ASSETS).catch((err) => {
        console.log('[SW] Cache parcial inicial:', err);
      });
    })
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => {
      return Promise.all(
        keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))
      );
    })
  );
  self.clients.claim();
});

// Network-first com fallback para cache
self.addEventListener('fetch', (event) => {
  // O navegador envia o multipart original e segue o 303 uma única vez.
  // Seguir o redirect aqui e redirecionar novamente consome os avisos da sessão.
  if (event.request.method !== 'GET') {
    return;
  }
  // Apenas recursos estáticos: nunca recuperar finanças ou login do cache.
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin || !url.pathname.startsWith('/static/')) return;

  event.respondWith(
    fetch(event.request)
      .then((response) => {
        return response;
      })
      .catch(() => {
        return caches.match(event.request);
      })
  );
});
