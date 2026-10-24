"use strict";

const CACHE = "stockroom-public-v8";
const PUBLIC_ASSETS = [
  "/static/offline.html",
  "/static/style.css",
  "/static/mobile.css",
  "/static/theme.css",
  "/static/icons/app.svg",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/icons/apple-touch-icon.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(PUBLIC_ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys().then((keys) => Promise.all(
    keys.filter((key) => key.startsWith("stockroom-public-") && key !== CACHE).map((key) => caches.delete(key))
  )).then(() => self.clients.claim()));
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== self.location.origin) return;

  // Never store authenticated pages, inventory, exports, or login/session responses.
  if (request.mode === "navigate") {
    event.respondWith(fetch(request).catch(async () =>
      await caches.match("/static/offline.html") ||
      new Response("You're offline. Reconnect to open Qmarket Stockroom.", {
        status: 503, headers: { "Content-Type": "text/plain; charset=utf-8" },
      })
    ));
    return;
  }
  if (!PUBLIC_ASSETS.includes(url.pathname)) return;
  event.respondWith((async () => {
    try {
      const response = await fetch(request);
      if (response.ok) {
        const cache = await caches.open(CACHE);
        await cache.put(url.pathname, response.clone());
      }
      return response;
    } catch (error) {
      const cached = await caches.match(url.pathname);
      if (cached) return cached;
      throw error;
    }
  })());
});
