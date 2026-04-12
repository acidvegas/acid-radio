const audio   = document.getElementById('audio');
const listEl  = document.getElementById('list');
const nowEl   = document.getElementById('now');
const timeEl  = document.getElementById('time');
const seekEl  = document.getElementById('seek');
const volEl   = document.getElementById('vol');
const playBtn = document.getElementById('play');

let currentArtist = null;
let trackList     = [];
let trackIdx      = -1;
let seeking       = false;

audio.volume = 0.8;


function fmt(s) {
	s = Math.floor(s);
	return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
}


function toggleArtist(header, tracksDiv, name) {
	const wasOpen = header.classList.contains('open');

	if (wasOpen) {
		header.classList.remove('open');
		tracksDiv.classList.remove('open');
		return;
	}

	header.classList.add('open');

	if (tracksDiv.children.length === 0) {
		fetch('/api/tracks?artist=' + encodeURIComponent(name))
			.then(r => r.json())
			.then(list => {
				list.forEach((t, i) => {
					const d = document.createElement('div');
					d.className = 'track';
					d.textContent = t.replace(/\.[^.]+$/, '');
					d.onclick = e => {
						e.stopPropagation();
						selectArtistTracks(name, list);
						playTrack(i);
					};
					tracksDiv.appendChild(d);
				});
				tracksDiv.classList.add('open');
			});
	} else {
		tracksDiv.classList.add('open');
	}
}


function selectArtistTracks(name, list) {
	currentArtist = name;
	trackList = list;
}


function playTrack(i) {
	trackIdx = i;
	const track = trackList[i];
	audio.src = '/music/' + encodeURIComponent(currentArtist) + '/' + encodeURIComponent(track);
	audio.play();
	nowEl.innerHTML = '<span>' + currentArtist + '</span> &mdash; ' + track.replace(/\.[^.]+$/, '');
	playBtn.innerHTML = '&#9646;&#9646;';

	listEl.querySelectorAll('.track').forEach(d => d.classList.remove('active'));
	const artistSections = listEl.querySelectorAll('.artist-section');
	artistSections.forEach(section => {
		const header = section.querySelector('.artist-header');
		if (header.dataset.name === currentArtist) {
			const tracks = section.querySelectorAll('.track');
			tracks.forEach((d, j) => d.classList.toggle('active', j === i));
		}
	});
}


fetch('/api/artists')
	.then(r => r.json())
	.then(artists => {
		artists.forEach(a => {
			const section = document.createElement('div');
			section.className = 'artist-section';

			const header = document.createElement('div');
			header.className = 'artist-header';
			header.dataset.name = a.name;

			const nameSpan = document.createElement('span');
			nameSpan.className = 'artist-name';
			nameSpan.textContent = a.name;

			const countSpan = document.createElement('span');
			countSpan.className = 'artist-count';
			countSpan.textContent = a.count;

			header.appendChild(nameSpan);
			header.appendChild(countSpan);

			const tracksDiv = document.createElement('div');
			tracksDiv.className = 'track-list';

			header.onclick = () => toggleArtist(header, tracksDiv, a.name);

			section.appendChild(header);
			section.appendChild(tracksDiv);
			listEl.appendChild(section);
		});
	});


playBtn.onclick = () => {
	if (audio.paused) {
		audio.play();
		playBtn.innerHTML = '&#9646;&#9646;';
	} else {
		audio.pause();
		playBtn.innerHTML = '&#9654;';
	}
};

document.getElementById('prev').onclick = () => {
	if (trackIdx > 0) playTrack(trackIdx - 1);
};

document.getElementById('next').onclick = () => {
	if (trackIdx < trackList.length - 1) playTrack(trackIdx + 1);
};

audio.onended = () => {
	if (trackIdx < trackList.length - 1) playTrack(trackIdx + 1);
};

audio.ontimeupdate = () => {
	if (!audio.duration) return;
	timeEl.textContent = fmt(audio.currentTime) + ' / ' + fmt(audio.duration);
	if (!seeking) seekEl.value = (audio.currentTime / audio.duration) * 100;
};

seekEl.oninput  = () => { seeking = true; };
seekEl.onchange = () => { audio.currentTime = (seekEl.value / 100) * audio.duration; seeking = false; };
volEl.oninput   = () => { audio.volume = volEl.value / 100; };

document.onkeydown = e => {
	if (e.code === 'Space')    { e.preventDefault(); playBtn.click(); }
	if (e.code === 'ArrowRight' && e.shiftKey) document.getElementById('next').click();
	if (e.code === 'ArrowLeft'  && e.shiftKey) document.getElementById('prev').click();
};
