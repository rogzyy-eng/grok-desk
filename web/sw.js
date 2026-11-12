// sw.js — offline shell + Web Push. Never caches /api responses.
const SHELL = "desk-shell-v1";
const FILES = ["/", "/index.html", "/app.css", "/app.js", "/manifest.webmanifest", "/icon-192.png"];
self.addEventListener("install", e => { e.waitUntil(caches.open(SHELL).then(c => c.addAll(FILES))); self.skipWaiting(); });
self.addEventListener("activate", e => { e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== SHELL).map(k => caches.delete(k))))); self.clients.claim(); });
self.addEventListener("fetch", e => {
  const u = new URL(e.request.url);
  if (u.pathname.startsWith("/api/") || e.request.method !== "GET") return;
  e.respondWith(fetch(e.request).then(r => { const c = r.clone(); caches.open(SHELL).then(s => s.put(e.request, c)); return r; })
    .catch(() => caches.match(e.request).then(r => r || caches.match("/index.html"))));
});
self.addEventListener("push", e => {
  let d = {}; try { d = e.data.json(); } catch (_) { d = { title: "Grok Desk", body: e.data ? e.data.text() : "" }; }
  const title = (d.mode === "PAPER" && !String(d.title || "").startsWith("PAPER") ? "PAPER · " : "") + (d.title || "Grok Desk");
  e.waitUntil(self.registration.showNotification(title, { body: d.body || "", icon: "/icon-192.png", badge: "/icon-192.png",
    data: { url: d.url || "/", mint: d.mint || null }, tag: d.mint ? "coin-" + d.mint : undefined }));
});
self.addEventListener("notificationclick", e => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || "/";
  e.waitUntil(clients.openWindow(url));
});
