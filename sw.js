// sw.js - Service Worker
self.addEventListener('install', (e) => {
  console.log('[Service Worker] Instalado');
  self.skipWaiting();
});

self.addEventListener('fetch', (e) => {
  // Ignora chamadas da API do Google Apps Script e métodos não-GET
  if (e.request.url.includes('script.google.com') || e.request.method !== 'GET') {
    return;
  }

  e.respondWith(fetch(e.request));
});
