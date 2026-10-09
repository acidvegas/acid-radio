// acid-radio - Developed by acidvegas in JavaScript (https://github.com/acidvegas)
// static/sw.js

const CACHE = 'acid-radio-v3';
const ASSETS = [
	'/',
	'/radio.css',
	'/radio.js',
	'/hls.min.js',
	'/manifest.json',
	'/icon-192.png',
	'/icon-512.png',
	'/fonts/rubikglitch-latin.woff2',
	'/fonts/rubikglitch-latin-ext.woff2',
	'/fonts/bebasneue-latin.woff2',
	'/fonts/bebasneue-latin-ext.woff2',
];

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
		url.pathname.startsWith('/video/') || url.pathname.startsWith('/images/') ||
		url.pathname.startsWith('/stream/')) {
		return;
	}
	e.respondWith(
		fetch(e.request).then(r => {
			// Only cache real successes. Caching a 502 thrown by a restarting
			// server would poison the offline fallback until the next deploy.
			if (r.ok && r.type === 'basic') {
				const clone = r.clone();
				caches.open(CACHE).then(c => c.put(e.request, clone));
			}
			return r;
		}).catch(() => caches.match(e.request))
	);
});
