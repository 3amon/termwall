/* termwall service worker: app-shell cache + push notifications. */
const CACHE = "termwall-v6";
const SHELL = ["./", "app.js", "app.css", "xterm.js", "xterm.css", "addon-fit.js", "manifest.webmanifest",
               "icon-192.png", "icon-512.png", "apple-touch-icon.png"];

self.addEventListener("install", (e) => {
  // cache what we can; one missing file must not keep the worker (and push) from installing
  e.waitUntil(caches.open(CACHE).then((c) => Promise.all(SHELL.map((u) => c.add(u).catch(() => null))))
    .then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});
// network first (fresh code when reachable), cache as the offline fallback; never cache the API
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.pathname.startsWith("/api/") || url.pathname.startsWith("/ws/")) return;
  e.respondWith(fetch(e.request).then((r) => {
    const copy = r.clone();
    caches.open(CACHE).then((c) => c.put(e.request, copy));
    return r;
  }).catch(() => caches.match(e.request)));
});

self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data.json(); } catch (_) { d = { title: "termwall", body: e.data && e.data.text() }; }
  e.waitUntil(self.registration.showNotification(d.title || "termwall", {
    body: d.body || "", tag: d.key || "termwall", data: { key: d.key }, icon: "icon-192.png", badge: "icon-192.png",
  }));
});
self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const key = e.notification.data && e.notification.data.key;
  e.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((cs) => {
    for (const c of cs) {
      if (key) c.postMessage({ open: key });
      return c.focus();
    }
    return self.clients.openWindow(key ? `./?open=${encodeURIComponent(key)}` : "./");
  }));
});
