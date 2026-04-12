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

let currentStartedAt = null;
let syncElapsed      = 0;
let syncLocalTime    = 0;
let songDuration     = 0;
let pollTimer        = null;
let firstSong        = true;
let currentSongKey   = null;
let myVote           = null;
let audioCtx         = null;
let gainNode         = null;
let hasVotedSkip     = false;
const sessionId      = Math.random().toString(36).slice(2);
const reconnectEl  = document.getElementById('reconnect');
const carBtn       = document.getElementById('car-btn');
const bufferingEl  = document.getElementById('buffering');
let carMode          = localStorage.getItem('acid_radio_car') === 'on';
let disconnected     = false;
let preloadedUrl     = null;
let preloadedKey     = null;
let activeBlobUrl    = null;

audio.volume = 0.8;

audio.addEventListener('waiting', () => bufferingEl.classList.remove('hidden'));
audio.addEventListener('playing', () => bufferingEl.classList.add('hidden'));
audio.addEventListener('canplay', () => bufferingEl.classList.add('hidden'));

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


async function fetchNow() {
	const r = await fetch('/api/radio/now?sid=' + sessionId);
	return await r.json();
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
			body: artist + ' \u2014 ' + track,
			silent: true,
		});
	} catch (e) {}
}


function preloadNext(folder, file) {
	const key = folder + '/' + file;
	if (key === preloadedKey) return;
	if (preloadedUrl) URL.revokeObjectURL(preloadedUrl);
	preloadedUrl = null;
	preloadedKey = key;
	fetch('/music/' + encodeURIComponent(folder) + '/' + encodeURIComponent(file))
		.then(r => r.blob())
		.then(blob => {
			if (preloadedKey !== key) return;
			preloadedUrl = URL.createObjectURL(blob);
		})
		.catch(() => { if (preloadedKey === key) preloadedKey = null; });
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
	if (!currentSongKey) return;
	try {
		const r = await fetch('/api/radio/votes?song=' + encodeURIComponent(currentSongKey) + '&client=' + encodeURIComponent(clientId));
		const data = await r.json();
		countUpEl.textContent = data.up;
		countDownEl.textContent = data.down;
		myVote = data.my_vote;
		voteUpBtn.classList.toggle('voted', myVote === 'up');
		voteDownBtn.classList.toggle('voted', myVote === 'down');
	} catch (e) {}
}


async function castVote(direction) {
	if (!currentSongKey) return;
	const newVote = (myVote === direction) ? null : direction;
	try {
		const r = await fetch('/api/radio/vote', {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({
				song: currentSongKey,
				client: clientId,
				vote: newVote,
			}),
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
	if (!currentStartedAt) return;
	try {
		const r = await fetch('/api/radio/skip-info?ts=' + currentStartedAt + '&client=' + encodeURIComponent(clientId));
		const data = await r.json();
		skipCountEl.textContent = data.votes;
		hasVotedSkip = data.voted;
		voteSkipBtn.classList.toggle('voted', data.voted);
		voteSkipBtn.classList.toggle('hidden', data.remaining <= 0);
	} catch (e) {}
}


async function castSkipVote() {
	if (!currentStartedAt || hasVotedSkip) return;
	try {
		const r = await fetch('/api/radio/vote-skip', {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ ts: currentStartedAt, client: clientId }),
		});
		const data = await r.json();
		skipCountEl.textContent = data.votes;
		hasVotedSkip = true;
		voteSkipBtn.classList.add('voted');
		voteSkipBtn.classList.toggle('hidden', data.remaining <= 0);
		if (data.skipped) {
			await syncSong();
		}
	} catch (e) {}
}


async function syncSong() {
	let state;
	try {
		state = await fetchNow();
	} catch (e) {
		if (!disconnected) {
			disconnected = true;
			reconnectEl.classList.remove('hidden');
		}
		return;
	}
	if (!state) return;
	if (disconnected) {
		disconnected = false;
		reconnectEl.classList.add('hidden');
	}

	const songChanged = currentStartedAt !== null && state.started_at !== currentStartedAt;

	currentStartedAt = state.started_at;
	syncElapsed = state.server_time - state.started_at;
	syncLocalTime = Date.now() / 1000;
	songDuration = state.duration;

	artistEl.textContent = state.artist;
	artistEl.setAttribute('data-text', state.artist);
	trackEl.textContent = state.track;
	genreEl.textContent = state.genre || '';
	updateMediaSession(state.artist, state.track, state.genre);

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
	if (window._bgInterval) {
		clearInterval(window._bgInterval);
		window._bgInterval = null;
	}
	bgVideo.ontimeupdate = null;
	bgVideo.onloadedmetadata = null;
	bgVideo.onended = null;
	bgVideo2.onended = null;
	bgVideo2.classList.remove('active');
	bgVideo2.pause();
	bgVideo2.removeAttribute('src');
	const genre = (state.genre || '').toLowerCase();
	const isHardcore = genre === 'hardcore';
	const isPostHardcore = genre === 'post hardcore' || genre === 'post-hardcore';
	const isFolkPunk = genre === 'folk punk' || genre === 'folk-punk';
	const isPunk = genre === 'punk';
	const bgs = artistBgs[state.folder];
	if (isHardcore) {
		document.body.style.backgroundImage = '';
		document.body.classList.remove('artist-bg');
		if (bgVideo.getAttribute('src') !== '/video/crowdkill.mp4') {
			bgVideo.src = '/video/crowdkill.mp4';
		}
		bgVideo.onended = () => { bgVideo.currentTime = 0; bgVideo.play(); };
		bgVideo.classList.add('active');
		bgVideo.play();
	} else if (isPostHardcore) {
		document.body.style.backgroundImage = '';
		document.body.classList.remove('artist-bg');
		if (bgVideo.getAttribute('src') !== '/video/posthardcore.mp4') {
			bgVideo.src = '/video/posthardcore.mp4';
		}
		bgVideo.onended = () => { bgVideo.currentTime = 0; bgVideo.play(); };
		bgVideo.classList.add('active');
		bgVideo.play();
	} else if (isFolkPunk) {
		document.body.style.backgroundImage = '';
		document.body.classList.remove('artist-bg');
		if (bgVideo.getAttribute('src') !== '/video/folkpunk.mp4') {
			bgVideo.src = '/video/folkpunk.mp4';
		}
		bgVideo.onended = () => { bgVideo.currentTime = 0; bgVideo.play(); };
		bgVideo.classList.add('active');
		bgVideo.play();
	} else if (isPunk) {
		document.body.style.backgroundImage = '';
		document.body.classList.remove('artist-bg');
		const jayClips = ['/video/jay.mp4', '/video/jay2.mp4'];
		const vids = [bgVideo, bgVideo2];
		let cur = 0;
		vids[0].src = jayClips[0];
		vids[1].src = jayClips[1];
		vids[1].load();
		function jaySwap() {
			const next = 1 - cur;
			vids[next].classList.add('active');
			vids[next].play();
			vids[cur].classList.remove('active');
			cur = next;
			const preloadIdx = 1 - cur;
			vids[preloadIdx].src = jayClips[preloadIdx];
			vids[preloadIdx].load();
		}
		vids[0].onended = jaySwap;
		vids[1].onended = jaySwap;
		vids[0].classList.add('active');
		vids[0].play();
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

	currentSongKey = state.folder + '/' + state.file;
	myVote = null;
	hasVotedSkip = false;
	voteUpBtn.classList.remove('voted');
	voteDownBtn.classList.remove('voted');
	voteSkipBtn.classList.remove('voted');
	skipCountEl.textContent = '0';
	fetchVotes();
	fetchSkipInfo();

	if (activeBlobUrl) {
		URL.revokeObjectURL(activeBlobUrl);
		activeBlobUrl = null;
	}
	const songPath = state.folder + '/' + state.file;
	if (preloadedKey === songPath && preloadedUrl) {
		audio.src = preloadedUrl;
		activeBlobUrl = preloadedUrl;
		preloadedUrl = null;
		preloadedKey = null;
	} else {
		audio.src = '/music/' + encodeURIComponent(state.folder) + '/' + encodeURIComponent(state.file);
	}
	if (state.next) preloadNext(state.next.folder, state.next.file);

	audio.onloadedmetadata = () => {
		const nowLocal = Date.now() / 1000;
		const elapsed = syncElapsed + (nowLocal - syncLocalTime);
		audio.currentTime = Math.min(elapsed, audio.duration - 0.5);
		audio.play().catch(() => {});
	};
	audio.onerror = () => {};

	if (songChanged || firstSong) {
		firstSong = false;
		notify(state.artist, state.track);
	}
}


async function checkForChange() {
	try {
		const state = await fetchNow();
		if (!state) return;
		if (disconnected) {
			disconnected = false;
			reconnectEl.classList.add('hidden');
		}
		if (state.started_at !== currentStartedAt) {
			await syncSong();
		}
	} catch (e) {
		if (!disconnected) {
			disconnected = true;
			reconnectEl.classList.remove('hidden');
		}
	}
}


function updateProgress() {
	if (!songDuration) {
		requestAnimationFrame(updateProgress);
		return;
	}
	const elapsed = syncElapsed + (Date.now() / 1000 - syncLocalTime);
	const pct = Math.min((elapsed / songDuration) * 100, 100);
	progressB.style.width = pct + '%';
	elapsedEl.textContent = fmt(elapsed);
	totalEl.textContent = fmt(songDuration);
	requestAnimationFrame(updateProgress);
}


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
	await syncSong();
	pollTimer = setInterval(checkForChange, 3000);
	setInterval(() => { fetchVotes(); fetchSkipInfo(); }, 10000);
	requestAnimationFrame(updateProgress);
};


skipBtn.onclick = async () => {
	try { await fetch('/api/radio/skip'); } catch (e) {}
	try { await syncSong(); } catch (e) {}
};
thpsBtn.onclick = async () => {
	try { await fetch('/api/radio/skip-to?artist=' + encodeURIComponent('Tony Hawks')); } catch (e) {}
	try { await syncSong(); } catch (e) {}
};
hxcBtn.onclick = async () => {
	try { await fetch('/api/radio/skip-to-genre?genre=hardcore'); } catch (e) {}
	try { await syncSong(); } catch (e) {}
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
};
if (carMode) {
	document.body.classList.add('car-mode');
	carBtn.classList.add('active');
}


function scheduleShake() {
	const delay = 1500 + Math.random() * 4000;
	setTimeout(() => {
		document.body.classList.add('shake');
		setTimeout(() => document.body.classList.remove('shake'), 150);
		scheduleShake();
	}, delay);
}
scheduleShake();

fetchListeners();
setInterval(fetchListeners, 10000);


fetch('/api/debug').then(r => r.json()).then(data => {
	if (data.debug) {
		document.querySelectorAll('.debug-btn').forEach(el => el.classList.remove('hidden'));
	}
}).catch(() => {});

if ('serviceWorker' in navigator) {
	navigator.serviceWorker.register('/sw.js').catch(() => {});
}
