// sw.js - Service Worker Mínimo para PWA
self.addEventListener('install', (e) => {
  console.log('[Service Worker] Instalado');
  self.skipWaiting();
});

self.addEventListener('fetch', (e) => {
  // Ignora completamente as requisições para os domínios do Google Apps Script
  if (e.request.url.includes('script.google.com') || e.request.url.includes('script.googleusercontent.com')) {
    return; // O navegador assume o controle nativamente
  }

  // Repassa as demais requisições (necessário para o funcionamento básico do PWA)
  e.respondWith(fetch(e.request));
});
