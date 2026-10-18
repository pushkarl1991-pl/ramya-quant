// RAMYA QUANT GLOBAL - Progressive Web App Service Worker
const CACHE_NAME = 'rqg-cache-v1';

self.addEventListener('install', (e) => {
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil(clients.claim());
});

self.addEventListener('fetch', (e) => {
  // Pass-through to network for real-time live data
  e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
});
