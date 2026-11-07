{% load static %}
// Service worker for Claude Manager.
// Bump CACHE (e.g. "claude-manager-v4") whenever the precache list or the logic
// below changes, so clients pick up the new version instead of serving stale
// cached assets.
const CACHE = "claude-manager-v6";

const PRECACHE = [
  "{% url 'core:offline' %}",
{% if not debug %}
  "{% static 'css/style.css' %}",
  "{% static 'vendor/htmx.min.js' %}",
  "{% static 'icons/icon-192.png' %}",
  "{% static 'icons/icon-512.png' %}",
  "{% static 'icons/icon-512-maskable.png' %}",
{% endif %}
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(PRECACHE))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;

  // Only handle same-origin GET requests; let everything else pass through.
  if (request.method !== "GET" || new URL(request.url).origin !== self.location.origin) {
    return;
  }

  if (request.mode === "navigate") {
    // Network-first for pages, falling back to cache, then the offline page.
    event.respondWith(
      fetch(request)
        .then((response) => {
          if (response.ok) {
            const copy = response.clone();
            caches.open(CACHE).then((cache) => cache.put(request, copy));
          }
          return response;
        })
        .catch(() =>
          caches.match(request).then((cached) => cached || caches.match("{% url 'core:offline' %}"))
        )
    );
    return;
  }

{% if debug %}
  // DEBUG: static files are served under their plain, unhashed names, so a
  // cache-first strategy would pin the first version of style.css or htmx.js a
  // browser ever saw and serve it after every edit. Leave /static/ to the
  // network (Django answers conditional requests, so this stays cheap).
{% else %}
  if (new URL(request.url).pathname.startsWith("/static/")) {
    // Cache-first for static assets, populating the cache on a network hit.
    // Safe in production: CompressedManifestStaticFilesStorage hashes the
    // filenames, so changed content always arrives under a new URL.
    event.respondWith(
      caches.match(request).then((cached) => {
        if (cached) {
          return cached;
        }
        return fetch(request).then((response) => {
          if (response.ok) {
            const copy = response.clone();
            caches.open(CACHE).then((cache) => cache.put(request, copy));
          }
          return response;
        });
      })
    );
  }
{% endif %}
});
