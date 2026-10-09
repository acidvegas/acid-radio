// acid-radio - Developed by acidvegas in JavaScript (https://github.com/acidvegas)
// static/radio.js

const audio      = document.getElementById('audio');
const splash     = document.getElementById('splash');
const radioEl    = document.getElementById('radio');
const artistEl   = document.getElementById('now-artist');
const trackEl    = document.getElementById('now-track');
const genreEl    = document.getElementById('now-genre');
const bgVideo    = document.getElementById('bg-video');
const bgVideo2   = document.getElementById('bg-video2');
const progressB  = document.getElementById('progress-bar');
const elapsedEl  = document.getElementById('time-elapsed');
const totalEl    = document.getElementById('time-total');
const volBtn     = document.getElementById('vol-btn');
const volDrop    = document.getElementById('vol-dropdown');
const volEl      = document.getElementById('vol');
const tuneinBtn  = document.getElementById('tunein');
const skipBtn    = document.getElementById('skip-btn');
const thpsBtn    = document.getElementById('thps-btn');
const hxcBtn     = document.getElementById('hxc-btn');
const voteUpBtn    = document.getElementById('vote-up');
const voteDownBtn  = document.getElementById('vote-down');
const countUpEl    = document.getElementById('count-up');
const countDownEl  = document.getElementById('count-down');
const voteSkipBtn  = document.getElementById('vote-skip');
const skipCountEl  = document.getElementById('skip-vote-count');
const listenerEl   = document.getElementById('listener-count');
const listenerEl2  = document.getElementById('listener-count-radio');
const reconnectEl  = document.getElementById('reconnect');
const carBtn       = document.getElementById('car-btn');
const bufferingEl  = document.getElementById('buffering');
const skippingEl   = document.getElementById('skipping');
const lyricsEl     = document.getElementById('lyrics');
const lyricsInner  = document.getElementById('lyrics-inner');

// Must match --lyric-line-h in radio.css.
const LYRIC_LINE_H = 28;
const LYRIC_VISIBLE = 5;

// The server publishes a 120s playlist window, so how much runway we have in a
// dead zone is decided entirely by how far behind the live edge we sit.
const SEGMENT_SECONDS = 4;
const HLS_PROFILES = {
	normal: { liveSyncDurationCount: 3,  liveMaxLatencyDurationCount: 8,  maxBufferLength: 30  },
	car:    { liveSyncDurationCount: 10, liveMaxLatencyDurationCount: 28, maxBufferLength: 150 },
};

let schedule        = [];
let activeTrackKey  = null;
let activeEntry     = null;
let myVote          = null;
let hasVotedSkip    = false;
let audioCtx        = null;
let gainNode        = null;
let hlsInstance     = null;
let carMode         = localStorage.getItem('acid_radio_car') === 'on';
let disconnected    = false;
let streamStalled   = false;
let hlsRetryDelay   = 1000;
let hlsRetryTimer   = null;
let scheduleTimer   = null;
let lyricLines      = [];
let lyricEls        = [];
let lyricIdx        = -1;
let lyricToken      = 0;
const sessionId     = Math.random().toString(36).slice(2);

audio.volume = 0.8;

function setBuffering(on) {
	// Driven by both the media element and hls.js: the element's waiting event
	// does not fire while hls.js is retrying a failed segment, which would
	// otherwise leave the player silent with nothing on screen.
	bufferingEl.classList.toggle('hidden', !on);
}

audio.addEventListener('waiting', () => setBuffering(true));
audio.addEventListener('stalled', () => setBuffering(true));
audio.addEventListener('playing', () => { streamStalled = false; resetHlsBackoff(); setBuffering(false); });
audio.addEventListener('canplay', () => { if (!streamStalled) setBuffering(false); });


function initAudioBoost() {
	if (audioCtx) return;
	audioCtx = new (window.AudioContext || window.webkitAudioContext)();
	const source = audioCtx.createMediaElementSource(audio);
	gainNode = audioCtx.createGain();
	source.connect(gainNode);
	gainNode.connect(audioCtx.destination);
	gainNode.gain.value = volEl.value / 100;
	audio.volume = 1.0;
}


// All audio routes through the gain node, so a suspended context means silence
// even though the element reports playing. iOS suspends it after a phone call
// or backgrounding and never resumes on its own.
function resumeAudioCtx() {
	if (audioCtx && audioCtx.state === 'suspended') audioCtx.resume().catch(() => {});
}

document.addEventListener('visibilitychange', () => { if (!document.hidden) resumeAudioCtx(); });
audio.addEventListener('play', resumeAudioCtx);
document.addEventListener('click', resumeAudioCtx);


function getClientId() {
	let id = localStorage.getItem('acid_radio_id');
	if (!id) {
		try { id = crypto.randomUUID(); } catch (e) {
			id = Array.from(crypto.getRandomValues(new Uint8Array(16)),
				b => b.toString(16).padStart(2, '0')).join('');
		}
		localStorage.setItem('acid_radio_id', id);
	}
	return id;
}
const clientId = getClientId();


function fmt(s) {
	s = Math.max(0, Math.floor(s));
	return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
}


function getPlayingTimeSec() {
	if (hlsInstance && hlsInstance.playingDate) {
		return hlsInstance.playingDate.getTime() / 1000;
	}
	if (typeof audio.getStartDate === 'function') {
		const sd = audio.getStartDate();
		if (sd && !isNaN(sd.getTime())) {
			return sd.getTime() / 1000 + audio.currentTime;
		}
	}
	return null;
}


async function fetchSchedule() {
	try {
		const r = await fetch('/api/radio/now?sid=' + sessionId);
		const data = await r.json();
		if (data && data.schedule) {
			schedule = data.schedule;
			if (disconnected) {
				disconnected = false;
				reconnectEl.classList.add('hidden');
			}
		}
	} catch (e) {
		if (!disconnected) {
			disconnected = true;
			reconnectEl.classList.remove('hidden');
		}
	}
}


async function fetchListeners() {
	try {
		const r = await fetch('/api/radio/listeners');
		const data = await r.json();
		const txt = data.count + ' listening';
		listenerEl.textContent = txt;
		listenerEl2.textContent = txt;
	} catch (e) {}
}


function notify(artist, track) {
	try {
		if (Notification.permission !== 'granted') return;
		new Notification('ACID RADIO', {
			body: artist + ' — ' + track,
			silent: true,
		});
	} catch (e) {}
}


function updateMediaSession(artist, track, genre) {
	if (!('mediaSession' in navigator)) return;
	navigator.mediaSession.metadata = new MediaMetadata({
		title: track,
		artist: artist,
		album: genre || 'ACID RADIO',
	});
}


async function fetchVotes() {
	if (!activeEntry) return;
	const key = activeEntry.folder + '/' + activeEntry.file;
	try {
		const r = await fetch('/api/radio/votes?song=' + encodeURIComponent(key) + '&client=' + encodeURIComponent(clientId));
		const data = await r.json();
		countUpEl.textContent = data.up;
		countDownEl.textContent = data.down;
		myVote = data.my_vote;
		voteUpBtn.classList.toggle('voted', myVote === 'up');
		voteDownBtn.classList.toggle('voted', myVote === 'down');
	} catch (e) {}
}


async function castVote(direction) {
	if (!activeEntry) return;
	const key = activeEntry.folder + '/' + activeEntry.file;
	const newVote = (myVote === direction) ? null : direction;
	try {
		const r = await fetch('/api/radio/vote', {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ song: key, client: clientId, vote: newVote }),
		});
		const data = await r.json();
		countUpEl.textContent = data.up;
		countDownEl.textContent = data.down;
		myVote = newVote;
		voteUpBtn.classList.toggle('voted', myVote === 'up');
		voteDownBtn.classList.toggle('voted', myVote === 'down');
	} catch (e) {}
}


async function fetchSkipInfo() {
	if (!activeEntry) return;
	try {
		const r = await fetch('/api/radio/skip-info?ts=' + activeEntry.pdt + '&client=' + encodeURIComponent(clientId));
		const data = await r.json();
		skipCountEl.textContent = data.votes;
		hasVotedSkip = data.voted;
		voteSkipBtn.classList.toggle('voted', data.voted);
		voteSkipBtn.classList.toggle('hidden', data.remaining <= 0);
	} catch (e) {}
}


async function castSkipVote() {
	if (!activeEntry || hasVotedSkip) return;
	try {
		const r = await fetch('/api/radio/vote-skip', {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ ts: activeEntry.pdt, client: clientId }),
		});
		const data = await r.json();
		skipCountEl.textContent = data.votes;
		hasVotedSkip = true;
		voteSkipBtn.classList.add('voted');
		voteSkipBtn.classList.toggle('hidden', data.remaining <= 0);
		// The cut happens at the encoder, which is well ahead of what we are
		// hearing, so without this the skip looks like it did nothing.
		if (data.skipped) skippingEl.classList.remove('hidden');
	} catch (e) {}
}


function clearLyrics() {
	lyricLines = [];
	lyricEls = [];
	lyricIdx = -1;
	lyricsInner.textContent = '';
	lyricsInner.style.transform = '';
	lyricsEl.classList.add('hidden');
}


async function loadLyrics(entry) {
	// Bumped on every track change so a slow response for the previous track
	// cannot land after the next one has already started.
	const token = ++lyricToken;
	clearLyrics();
	if (carMode) return;
	const key = entry.folder + '/' + entry.file;
	let data;
	try {
		const r = await fetch('/api/radio/lyrics?song=' + encodeURIComponent(key));
		data = await r.json();
	} catch (e) { return; }
	if (token !== lyricToken || !data.lines || !data.lines.length) return;

	lyricLines = data.lines;
	const frag = document.createDocumentFragment();
	lyricEls = lyricLines.map(l => {
		const d = document.createElement('div');
		d.className = 'lyric-line';
		d.textContent = l.text;
		frag.appendChild(d);
		return d;
	});
	lyricsInner.appendChild(frag);
	lyricsEl.classList.remove('hidden');
	// -2 rather than -1: before the first line lands the computed index is -1,
	// which would match and skip setting the initial scroll offset.
	lyricIdx = -2;
	syncLyrics(currentElapsed());
}


// Lyrics can load mid-song (tuning in late, or leaving car mode), so start from
// the real position rather than the top of the track.
function currentElapsed() {
	const cur = currentEntry();
	return cur ? cur.t - cur.entry.pdt : 0;
}


function syncLyrics(elapsed) {
	if (!lyricLines.length) return;
	let idx = -1;
	for (let i = 0; i < lyricLines.length; i++) {
		if (lyricLines[i].t <= elapsed) idx = i;
		else break;
	}
	if (idx === lyricIdx) return;
	if (lyricEls[lyricIdx]) lyricEls[lyricIdx].classList.remove('active');
	if (lyricEls[idx]) lyricEls[idx].classList.add('active');
	lyricIdx = idx;
	// Park the active line in the middle of the window. Before the first line
	// hits, idx is -1 and we sit one slot above it.
	const centre = Math.floor(LYRIC_VISIBLE / 2);
	lyricsInner.style.transform = 'translateY(' + ((centre - idx) * LYRIC_LINE_H) + 'px)';
}


function clearBackground() {
	if (window._bgInterval) {
		clearInterval(window._bgInterval);
		window._bgInterval = null;
	}
	bgVideo.onended = null;
	bgVideo2.onended = null;
	bgVideo.loop = false;
	bgVideo2.loop = false;
	bgVideo.classList.remove('active');
	bgVideo2.classList.remove('active');
	bgVideo.pause();
	bgVideo2.pause();
	bgVideo.removeAttribute('src');
	bgVideo2.removeAttribute('src');
	document.body.style.backgroundImage = '';
	document.body.classList.remove('artist-bg');
}


function loopClip(src) {
	if (bgVideo.getAttribute('src') !== src) bgVideo.src = src;
	bgVideo.loop = true;
	bgVideo.classList.add('active');
	bgVideo.play().catch(() => {});
}


function setBackgroundForEntry(entry) {
	// In car mode the videos are display:none, so loading them would burn
	// cellular data and battery decoding frames nobody can see.
	if (carMode) {
		clearBackground();
		return;
	}

	if (window._bgInterval) {
		clearInterval(window._bgInterval);
		window._bgInterval = null;
	}
	bgVideo.onended = null;
	bgVideo2.onended = null;
	// Must clear loop too: a looping video never fires 'ended', so leaving it
	// set from a previous track would stop the punk clips from alternating.
	bgVideo.loop = false;
	bgVideo2.loop = false;
	bgVideo2.classList.remove('active');
	bgVideo2.pause();
	bgVideo2.removeAttribute('src');

	const artistBgs = {
		'Tony Hawks': [
			'/images/tonyhawks/background.gif',
			'/images/tonyhawks/2.gif',
			'/images/tonyhawks/3.gif',
			'/images/tonyhawks/4.gif',
			'/images/tonyhawks/5.gif',
		],
	};
	const genreBgs = {
		'indie': [
			'/images/indie/arnold.gif',
			'/images/indie/bart.gif',
			'/images/indie/drum.gif',
		],
	};
	const genre = (entry.genre || '').toLowerCase();
	const isHardcore = genre === 'hardcore';
	const isPostHardcore = genre === 'post hardcore' || genre === 'post-hardcore';
	const isFolkPunk = genre === 'folk punk' || genre === 'folk-punk';
	const isPunk = genre === 'punk';
	const bgs = artistBgs[entry.folder];

	if (isHardcore) {
		document.body.style.backgroundImage = '';
		document.body.classList.remove('artist-bg');
		loopClip('/video/crowdkill.mp4');
	} else if (isPostHardcore) {
		document.body.style.backgroundImage = '';
		document.body.classList.remove('artist-bg');
		loopClip('/video/posthardcore.mp4');
	} else if (isFolkPunk) {
		document.body.style.backgroundImage = '';
		document.body.classList.remove('artist-bg');
		loopClip('/video/folkpunk.mp4');
	} else if (isPunk) {
		document.body.style.backgroundImage = '';
		document.body.classList.remove('artist-bg');
		const jayClips = ['/video/jay.mp4', '/video/jay2.mp4'];
		const vids = [bgVideo, bgVideo2];
		let cur = 0;
		// Both clips stay loaded for the life of the track. Re-assigning src on
		// every swap re-downloaded them each cycle.
		if (vids[0].getAttribute('src') !== jayClips[0]) vids[0].src = jayClips[0];
		if (vids[1].getAttribute('src') !== jayClips[1]) vids[1].src = jayClips[1];
		vids[1].load();
		function jaySwap() {
			const next = 1 - cur;
			vids[next].currentTime = 0;
			vids[next].classList.add('active');
			vids[next].play().catch(() => {});
			vids[cur].classList.remove('active');
			cur = next;
		}
		vids[0].onended = jaySwap;
		vids[1].onended = jaySwap;
		vids[0].classList.add('active');
		vids[0].play().catch(() => {});
	} else if (bgs) {
		bgVideo.classList.remove('active');
		bgVideo.pause();
		let idx = 0;
		document.body.style.backgroundImage = 'url(' + bgs[idx] + ')';
		document.body.classList.add('artist-bg');
		window._bgInterval = setInterval(() => {
			idx = (idx + 1) % bgs.length;
			document.body.style.backgroundImage = 'url(' + bgs[idx] + ')';
		}, 5000);
	} else if (genreBgs[genre]) {
		bgVideo.classList.remove('active');
		bgVideo.pause();
		let idx = 0;
		const gBgs = genreBgs[genre];
		document.body.style.backgroundImage = 'url(' + gBgs[idx] + ')';
		document.body.classList.add('artist-bg');
		window._bgInterval = setInterval(() => {
			idx = (idx + 1) % gBgs.length;
			document.body.style.backgroundImage = 'url(' + gBgs[idx] + ')';
		}, 5000);
	} else {
		bgVideo.classList.remove('active');
		bgVideo.pause();
		document.body.style.backgroundImage = '';
		document.body.classList.remove('artist-bg');
	}
}


function onTrackChange(entry) {
	activeEntry = entry;
	artistEl.textContent = entry.artist;
	artistEl.setAttribute('data-text', entry.artist);
	trackEl.textContent = entry.track;
	genreEl.textContent = entry.genre || '';
	updateMediaSession(entry.artist, entry.track, entry.genre);
	setBackgroundForEntry(entry);
	loadLyrics(entry);

	myVote = null;
	hasVotedSkip = false;
	voteUpBtn.classList.remove('voted');
	voteDownBtn.classList.remove('voted');
	voteSkipBtn.classList.remove('voted');
	skippingEl.classList.add('hidden');
	skipCountEl.textContent = '0';
	fetchVotes();
	fetchSkipInfo();

	notify(entry.artist, entry.track);
}


function currentEntry() {
	const t = getPlayingTimeSec();
	if (t === null || !schedule.length) return null;
	let active = null;
	for (const e of schedule) {
		if (e.pdt <= t) active = e;
		else break;
	}
	return active ? { entry: active, t: t } : null;
}


// Track changes run on a timer, not requestAnimationFrame. rAF is paused when
// the screen is off or the tab is backgrounded, which left the lock screen and
// car head unit showing whichever track was playing when the screen went dark.
function syncTrack() {
	const cur = currentEntry();
	if (!cur) return;
	const key = cur.entry.folder + '/' + cur.entry.file;
	if (key !== activeTrackKey) {
		activeTrackKey = key;
		onTrackChange(cur.entry);
	}
}


function tick() {
	const cur = currentEntry();
	if (cur) {
		const elapsed = cur.t - cur.entry.pdt;
		const dur = cur.entry.duration || 1;
		const pct = Math.min(Math.max(elapsed / dur, 0), 1) * 100;
		progressB.style.width = pct + '%';
		elapsedEl.textContent = fmt(elapsed);
		totalEl.textContent = fmt(dur);
		syncLyrics(elapsed);
	}
	requestAnimationFrame(tick);
}


// Retries back off and stop entirely while the device reports no connection,
// so a long dead zone does not turn into a tight loop of failing requests.
function scheduleHlsRetry(fn) {
	if (hlsRetryTimer !== null) return;
	if (navigator.onLine === false) return;
	hlsRetryTimer = setTimeout(() => {
		hlsRetryTimer = null;
		fn();
	}, hlsRetryDelay);
	hlsRetryDelay = Math.min(hlsRetryDelay * 2, 15000);
}


// Only resets the backoff. A pending retry is deliberately left to fire: audio
// draining from the buffer can emit 'playing' mid-backoff, and cancelling the
// retry there would leave a stopped loader with nothing to restart it.
function resetHlsBackoff() {
	hlsRetryDelay = 1000;
}


function applyHlsProfile() {
	const p = carMode ? HLS_PROFILES.car : HLS_PROFILES.normal;
	// hls.js config only exists on the MSE path. iOS Safari has no MSE and plays
	// the playlist natively, so the repositioning below is the only lever there
	// — which is exactly where car mode matters most.
	if (hlsInstance) Object.assign(hlsInstance.config, p);

	// Buffer depth is exactly how far back from the live edge we sit, since
	// segments ahead of the edge do not exist yet. Getting a deeper buffer
	// therefore means repositioning, not just raising maxBufferLength.
	if (!audio.seekable.length) return;
	const end = audio.seekable.end(audio.seekable.length - 1);
	const start = audio.seekable.start(0);
	const want = Math.max(start, end - p.liveSyncDurationCount * SEGMENT_SECONDS);
	if (Math.abs(audio.currentTime - want) > SEGMENT_SECONDS) {
		try { audio.currentTime = want; } catch (e) {}
	}
}


function setupHls() {
	const src = '/stream/playlist.m3u8';
	if (window.Hls && Hls.isSupported()) {
		hlsInstance = new Hls(carMode ? HLS_PROFILES.car : HLS_PROFILES.normal);
		hlsInstance.loadSource(src);
		hlsInstance.attachMedia(audio);
		hlsInstance.on(Hls.Events.FRAG_BUFFERED, () => {
			resetHlsBackoff();
			streamStalled = false;
			setBuffering(false);
		});
		hlsInstance.on(Hls.Events.ERROR, (event, data) => {
			if (!data.fatal) return;
			streamStalled = true;
			setBuffering(true);
			switch (data.type) {
				case Hls.ErrorTypes.NETWORK_ERROR:
					scheduleHlsRetry(() => { if (hlsInstance) hlsInstance.startLoad(); });
					break;
				case Hls.ErrorTypes.MEDIA_ERROR:
					scheduleHlsRetry(() => { if (hlsInstance) hlsInstance.recoverMediaError(); });
					break;
				default:
					scheduleHlsRetry(() => {
						if (hlsInstance) hlsInstance.destroy();
						hlsInstance = null;
						setupHls();
					});
			}
		});
	} else if (audio.canPlayType('application/vnd.apple.mpegurl')) {
		// Native HLS (iOS Safari). Reposition once the seekable window is known,
		// otherwise the browser parks us at the live edge with no runway.
		audio.src = src;
		audio.addEventListener('playing', applyHlsProfile, { once: true });
		audio.addEventListener('error', () => {
			streamStalled = true;
			setBuffering(true);
			scheduleHlsRetry(() => {
				audio.src = src;
				audio.load();
				audio.play().catch(() => {});
			});
		});
	}
}


window.addEventListener('online', () => {
	resetHlsBackoff();
	if (hlsInstance) hlsInstance.startLoad();
	audio.play().catch(() => {});
	fetchSchedule();
});

window.addEventListener('offline', () => {
	streamStalled = true;
	setBuffering(true);
});


tuneinBtn.onclick = async () => {
	try {
		if (Notification.permission === 'default')
			await Notification.requestPermission();
	} catch (e) {}
	initAudioBoost();
	if ('mediaSession' in navigator) {
		navigator.mediaSession.setActionHandler('play', () => audio.play());
		navigator.mediaSession.setActionHandler('pause', () => audio.pause());
	}
	splash.classList.add('hidden');
	radioEl.classList.remove('hidden');
	setupHls();
	audio.play().catch(() => {});
	await fetchSchedule();
	if (scheduleTimer === null) {
		scheduleTimer = setInterval(fetchSchedule, 15000);
		setInterval(syncTrack, 1000);
		requestAnimationFrame(tick);
	}
};


skipBtn.onclick = async () => {
	try { await fetch('/api/radio/skip'); } catch (e) {}
	setTimeout(fetchSchedule, 200);
};
thpsBtn.onclick = async () => {
	try { await fetch('/api/radio/skip-to?artist=' + encodeURIComponent('Tony Hawks')); } catch (e) {}
	setTimeout(fetchSchedule, 200);
};
hxcBtn.onclick = async () => {
	try { await fetch('/api/radio/skip-to-genre?genre=hardcore'); } catch (e) {}
	setTimeout(fetchSchedule, 200);
};


volBtn.onclick = e => {
	e.stopPropagation();
	volDrop.classList.toggle('hidden');
};

document.addEventListener('click', e => {
	if (!volDrop.contains(e.target) && e.target !== volBtn && !volBtn.contains(e.target)) {
		volDrop.classList.add('hidden');
	}
});

volEl.oninput = () => {
	if (gainNode) {
		gainNode.gain.value = volEl.value / 100;
	} else {
		audio.volume = Math.min(volEl.value / 100, 1.0);
	}
};


voteUpBtn.onclick = () => castVote('up');
voteDownBtn.onclick = () => castVote('down');
voteSkipBtn.onclick = () => castSkipVote();


carBtn.onclick = () => {
	carMode = !carMode;
	localStorage.setItem('acid_radio_car', carMode ? 'on' : 'off');
	document.body.classList.toggle('car-mode', carMode);
	carBtn.classList.toggle('active', carMode);
	applyHlsProfile();
	if (carMode) {
		clearBackground();
		clearLyrics();
	} else if (activeEntry) {
		setBackgroundForEntry(activeEntry);
		loadLyrics(activeEntry);
	}
};
if (carMode) {
	document.body.classList.add('car-mode');
	carBtn.classList.add('active');
}


function scheduleShake() {
	const delay = 1500 + Math.random() * 4000;
	setTimeout(() => {
		// Car mode disables the animation in CSS, so toggling the class there is
		// pure layout thrash every few seconds for no visible effect.
		if (!carMode) {
			document.body.classList.add('shake');
			setTimeout(() => document.body.classList.remove('shake'), 150);
		}
		scheduleShake();
	}, delay);
}
scheduleShake();

fetchListeners();
setInterval(fetchListeners, 10000);
setInterval(() => { fetchVotes(); fetchSkipInfo(); }, 10000);


// Show the admin buttons only when they would actually work. The endpoints are
// restricted by network location, so a remote browser with debug on would
// otherwise get buttons that silently 403.
fetch('/api/debug').then(r => r.json()).then(data => {
	if (data.debug && data.admin) {
		document.querySelectorAll('.debug-btn').forEach(el => el.classList.remove('hidden'));
	}
}).catch(() => {});

if ('serviceWorker' in navigator) {
	navigator.serviceWorker.register('/sw.js').catch(() => {});
}
