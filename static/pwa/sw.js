// 选股台 App（PWA）：本站的页面和图标先从网上取最新的（数据每小时更新），取不到时用上次存下的，断网也能看最近一次的数据。
// 行情、快讯等第三方接口不经过这里，照常实时请求。
const CACHE = "stock-desk-v1";
const SHELL = ["./", "./manifest.webmanifest", "./icon-192.png", "./icon-512.png"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).catch(() => {}));
  self.skipWaiting();
});

self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});

self.addEventListener("fetch", e => {
  const req = e.request, url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== self.location.origin) return;
  const page = req.mode === "navigate";
  e.respondWith(
    fetch(req).then(res => {
      if (res.ok) { const copy = res.clone(); caches.open(CACHE).then(c => c.put(page ? "./" : req, copy)); }
      return res;
    }).catch(() => caches.match(page ? "./" : req, {ignoreSearch: true}).then(r => r || caches.match("./")))
  );
});
