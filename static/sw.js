const CACHE = 'acid-radio-v1';
const ASSETS = ['/', '/radio.css', '/radio.js'];

self.addEventListener('install', e => {
	e.waitUntil(caches.open(CACHE).then(c => c.addAll(ASSETS)));
	self.skipWaiting();
});

self.addEventListener('activate', e => {
	e.waitUntil(
		caches.keys().then(keys =>
			Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
		)
	);
	self.clients.claim();
});

self.addEventListener('fetch', e => {
	const url = new URL(e.request.url);
	if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/music/') ||
		url.pathname.startsWith('/video/') || url.pathname.startsWith('/images/')) {
		return;
	}
	e.respondWith(
		fetch(e.request).then(r => {
			const clone = r.clone();
			caches.open(CACHE).then(c => c.put(e.request, clone));
			return r;
		}).catch(() => caches.match(e.request))
	);
});
