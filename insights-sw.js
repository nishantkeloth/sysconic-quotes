// Minimal service worker for the Insights PWA (insights-app.html).
// Deliberately does NOT cache anything -- this dashboard shows live sales/
// financial numbers, so serving a stale cached response would be actively
// misleading. Its only job is to satisfy the browser's installability
// criteria (Add to Home Screen) by existing and passing requests through.
self.addEventListener('install', (event) => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  event.respondWith(fetch(event.request));
});
