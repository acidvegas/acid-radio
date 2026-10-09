#!/usr/bin/env python3
# ACID RADIO - Developed by acidvegas in Python (https://git.supernets.org/acidvegas/acid-radio)
# acid-radio/server.py

import argparse
import gzip
import http.server
import ipaddress
import json
import os
import random
import re
import signal
import sqlite3
import subprocess
import threading
import time
import urllib.parse

try:
    import mutagen
except ImportError:
    raise ImportError('missing mutagen library (pip install mutagen)')

ROOT_DIR   = os.path.dirname(os.path.abspath(__file__))
MUSIC_DIR  = os.path.join(ROOT_DIR, 'music')
STATIC_DIR = os.path.join(ROOT_DIR, 'static')
DATA_DIR   = os.path.join(ROOT_DIR, 'data')
DB_PATH    = os.path.join(DATA_DIR, 'votes.db')
STREAM_DIR = os.path.join(DATA_DIR, 'stream')

SAMPLE_RATE = 44100
CHANNELS = 2
PCM_BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * 2  # s16le

SEGMENT_SECONDS = 4
HLS_LIST_SIZE = 30  # 30 * 4s = 120s window, so deep-buffering clients have segments to fetch

HOST = '0.0.0.0'
PORT = 7000

# Tailscale's CGNAT range. ipaddress.is_private excludes 100.64.0.0/10, so the
# tailnet has to be allowed explicitly for the admin endpoints.
TAILSCALE_NET = ipaddress.ip_network('100.64.0.0/10')
DEBUG = False
MAX_SKIPS_PER_HOUR = 3

skip_votes_lock = threading.Lock()
skip_votes = {}
skip_history = []

listeners_lock = threading.Lock()
listeners = {}
LISTENER_TIMEOUT = 15


def get_listener_count():
    now = time.time()
    with listeners_lock:
        stale = [k for k, t in listeners.items() if now - t > LISTENER_TIMEOUT]
        for k in stale:
            del listeners[k]
        return len(listeners)


def touch_listener(addr):
    with listeners_lock:
        # Session ids are client-generated, so a flood of unique ones would grow
        # this unbounded between the 15s prunes. Sweep early if it gets silly.
        if len(listeners) > 10000:
            now = time.time()
            for k in [k for k, t in listeners.items() if now - t > LISTENER_TIMEOUT]:
                del listeners[k]
        listeners[addr] = time.time()


def get_skips_remaining():
    now = time.time()
    skip_history[:] = [t for t in skip_history if t > now - 3600]
    return max(0, MAX_SKIPS_PER_HOUR - len(skip_history))


def prune_skip_votes():
    '''Drop skip-vote sets for tracks that have long since played out.'''
    cutoff = time.time() - 3600
    for ts in [ts for ts in skip_votes if ts < cutoff]:
        del skip_votes[ts]

AUDIO_EXTS = {'.mp3', '.flac', '.ogg', '.wav', '.m4a', '.opus', '.aac'}

# [mm:ss.xx] or [mm:ss:xx] or [mm:ss], one or more per line
LRC_TIME_RE = re.compile(r'\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]')

lyrics_lock = threading.Lock()
lyrics_cache = {}


def parse_lrc(text):
    '''Turn LRC text into [{"t": seconds, "text": ...}] sorted by time.

    A single line may carry several timestamps (repeated choruses), and metadata
    tags like [ar:] have no timestamp, so they fall out naturally.
    '''
    lines = []
    for raw in text.splitlines():
        stamps = list(LRC_TIME_RE.finditer(raw))
        if not stamps:
            continue
        words = raw[stamps[-1].end():].strip()
        for m in stamps:
            mins, secs, frac = m.group(1), m.group(2), m.group(3) or '0'
            t = int(mins) * 60 + int(secs) + int(frac) / (10 ** len(frac))
            lines.append({'t': round(t, 2), 'text': words})
    lines.sort(key=lambda l: l['t'])
    return lines


def load_lyrics(audio_path):
    '''Parsed lyrics for a track, or [] when there is no .lrc beside it.'''
    lrc_path = os.path.splitext(audio_path)[0] + '.lrc'
    try:
        mtime = os.path.getmtime(lrc_path)
    except OSError:
        return []
    with lyrics_lock:
        hit = lyrics_cache.get(lrc_path)
        if hit and hit[0] == mtime:
            return hit[1]
    try:
        with open(lrc_path, 'rb') as f:
            text = f.read().decode('utf-8', errors='replace')
    except OSError:
        return []
    lines = parse_lrc(text)
    with lyrics_lock:
        if len(lyrics_cache) > 500:
            lyrics_cache.clear()
        lyrics_cache[lrc_path] = (mtime, lines)
    return lines
MIME_TYPES = {
    '.html':  'text/html',
    '.css':   'text/css',
    '.js':    'application/javascript',
    '.mp3':   'audio/mpeg',
    '.flac':  'audio/flac',
    '.ogg':   'audio/ogg',
    '.wav':   'audio/wav',
    '.m4a':   'audio/mp4',
    '.opus':  'audio/opus',
    '.aac':   'audio/aac',
}

STATIC_ROUTES = {
    '/':                                        'index.html',
    '/radio':                                   'index.html',
    '/radio.css':                               'radio.css',
    '/radio.js':                                'radio.js',
    '/sw.js':                                   'sw.js',
    '/manifest.json':                           'manifest.json',
    '/icon-192.png':                            'icon-192.png',
    '/icon-512.png':                            'icon-512.png',
    '/hls.min.js':                              'hls.min.js',
}

MIME_TYPES['.gif']  = 'image/gif'
MIME_TYPES['.png']  = 'image/png'
MIME_TYPES['.jpg']  = 'image/jpeg'
MIME_TYPES['.mp4']  = 'video/mp4'
MIME_TYPES['.json']  = 'application/json'
MIME_TYPES['.m3u8']  = 'application/vnd.apple.mpegurl'
MIME_TYPES['.ts']    = 'video/MP2T'
MIME_TYPES['.woff2'] = 'font/woff2'


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''CREATE TABLE IF NOT EXISTS votes (
        song TEXT NOT NULL,
        client TEXT NOT NULL,
        vote TEXT NOT NULL,
        ts REAL NOT NULL,
        PRIMARY KEY (song, client)
    )''')
    conn.commit()
    conn.close()


class VoteStore:
    def __init__(self, db_path):
        self.db_path = db_path
        self.lock = threading.Lock()

    def _conn(self):
        return sqlite3.connect(self.db_path)

    def cast(self, song, client, vote):
        with self.lock:
            conn = self._conn()
            if vote is None:
                conn.execute('DELETE FROM votes WHERE song=? AND client=?', (song, client))
            else:
                conn.execute(
                    'INSERT INTO votes (song, client, vote, ts) VALUES (?, ?, ?, ?) '
                    'ON CONFLICT(song, client) DO UPDATE SET vote=?, ts=?',
                    (song, client, vote, time.time(), vote, time.time())
                )
            conn.commit()
            counts = self._counts(conn, song)
            conn.close()
            return counts

    def get(self, song, client):
        with self.lock:
            conn = self._conn()
            counts = self._counts(conn, song)
            row = conn.execute(
                'SELECT vote FROM votes WHERE song=? AND client=?', (song, client)
            ).fetchone()
            conn.close()
            counts['my_vote'] = row[0] if row else None
            return counts

    def _counts(self, conn, song):
        up = conn.execute(
            "SELECT COUNT(*) FROM votes WHERE song=? AND vote='up'", (song,)
        ).fetchone()[0]
        down = conn.execute(
            "SELECT COUNT(*) FROM votes WHERE song=? AND vote='down'", (song,)
        ).fetchone()[0]
        return {'up': up, 'down': down}


class Radio:
    def __init__(self, music_dir, audio_exts, stream_dir):
        self.music_dir = music_dir
        self.audio_exts = audio_exts
        self.stream_dir = stream_dir
        self.lock = threading.Lock()
        self.songs = []
        self.meta_cache = {}
        self.recent_artists = []
        self.schedule = []
        self.encoder = None
        self.encoder_epoch = 0
        self.encoder_start_pdt = None
        self.cumulative_pcm_bytes = 0
        self.current_decoder = None
        self.skip_requested = False
        self.forced_next = None
        self.next_seg_floor = 0

        self._clear_stream_dir()
        self._scan()

        threading.Thread(target=self._run_encoder, daemon=True).start()
        threading.Thread(target=self._feed_loop, daemon=True).start()
        threading.Thread(target=self._rescan_loop, daemon=True).start()

    def _clear_stream_dir(self):
        os.makedirs(self.stream_dir, exist_ok=True)
        for f in os.listdir(self.stream_dir):
            if f.endswith('.ts') or f.endswith('.m3u8'):
                try:
                    os.remove(os.path.join(self.stream_dir, f))
                except OSError:
                    pass

    def _scan(self):
        try:
            folders = os.listdir(self.music_dir)
        except OSError as e:
            print(f'[radio] cannot read music directory {self.music_dir}: {e}')
            return

        songs = []
        cache = {}
        reread = 0
        for folder in folders:
            ap = os.path.join(self.music_dir, folder)
            if not os.path.isdir(ap):
                continue
            for f in os.listdir(ap):
                if os.path.splitext(f)[1].lower() not in self.audio_exts:
                    continue
                path = os.path.join(ap, f)
                try:
                    mtime = os.path.getmtime(path)
                except OSError:
                    continue
                meta = self.meta_cache.get(path)
                if not meta or meta['mtime'] != mtime:
                    tags = self._read_tags(path)
                    meta = {
                        'mtime': mtime,
                        'artist': tags['artist'] or folder,
                        'title': tags['title'] or f.rsplit('.', 1)[0],
                        'genre': tags['genre'],
                        'duration': None,
                    }
                    reread += 1
                cache[path] = meta
                songs.append((folder, f, meta['artist'], meta['genre']))

        with self.lock:
            self.songs = songs
            self.meta_cache = cache
        if songs:
            print(f'[radio] scanned {len(songs)} tracks ({reread} read from disk)')

    def _rescan_loop(self):
        while True:
            time.sleep(300)
            self._scan()

    def _read_tags(self, path):
        result = {'artist': None, 'title': None, 'genre': None}
        try:
            tags = mutagen.File(path, easy=True)
            if tags:
                if 'artist' in tags:
                    result['artist'] = tags['artist'][0]
                if 'title' in tags:
                    result['title'] = tags['title'][0]
                if 'genre' in tags:
                    result['genre'] = tags['genre'][0]
        except Exception:
            pass
        return result

    def _probe_duration(self, path):
        try:
            r = subprocess.run(
                ['ffprobe', '-v', 'quiet', '-show_entries',
                 'format=duration', '-of', 'csv=p=0', path],
                capture_output=True, text=True, timeout=10
            )
            return float(r.stdout.strip())
        except Exception:
            return 240.0

    def _prune_orphan_segments(self):
        '''Delete segments left behind by a previous encoder run.

        ffmpeg's delete_segments only manages segments from its own run, so each
        restart strands up to a full window on disk. The age cutoff is well past
        the playlist window, so clients still draining the old playlist are safe.
        '''
        cutoff = time.time() - 300
        try:
            names = os.listdir(self.stream_dir)
        except OSError:
            return
        for f in names:
            if not re.fullmatch(r'seg(\d+)\.ts', f):
                continue
            path = os.path.join(self.stream_dir, f)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                pass

    def _next_segment_number(self):
        '''Highest segment index written so far, plus a gap.

        Restarting ffmpeg at 0 would rewind EXT-X-MEDIA-SEQUENCE and make every
        connected player think the live stream jumped backwards. Continuing the
        numbering keeps the sequence monotonic so clients just carry on. The
        in-memory floor covers the case where pruning removed every file.
        '''
        highest = -1
        try:
            for f in os.listdir(self.stream_dir):
                m = re.fullmatch(r'seg(\d+)\.ts', f)
                if m:
                    highest = max(highest, int(m.group(1)))
        except OSError:
            pass
        nxt = max(highest + 10 if highest >= 0 else 0, self.next_seg_floor)
        self.next_seg_floor = nxt
        return nxt

    def _run_encoder(self):
        playlist = os.path.join(self.stream_dir, 'playlist.m3u8')
        seg_pattern = os.path.join(self.stream_dir, 'seg%d.ts')
        while True:
            start_number = self._next_segment_number()
            self.encoder = subprocess.Popen([
                'ffmpeg', '-loglevel', 'error', '-re',
                '-f', 's16le', '-ar', str(SAMPLE_RATE), '-ac', str(CHANNELS),
                '-i', 'pipe:0',
                '-c:a', 'aac', '-b:a', '128k',
                '-f', 'hls',
                '-hls_time', str(SEGMENT_SECONDS),
                '-hls_list_size', str(HLS_LIST_SIZE),
                '-start_number', str(start_number),
                '-hls_flags', 'delete_segments+program_date_time+independent_segments',
                '-hls_segment_filename', seg_pattern,
                playlist,
            ], stdin=subprocess.PIPE)
            self.encoder.wait()
            print('[radio] encoder exited, restarting in 2s')
            with self.lock:
                # Bump the epoch so the in-flight _pipe_track cannot write its
                # byte count into the new stream's freshly-zeroed counter.
                self.encoder_epoch += 1
                self.encoder_start_pdt = None
                self.cumulative_pcm_bytes = 0
                self.schedule = []
            time.sleep(2)
            self._prune_orphan_segments()

    def _select_next(self):
        with self.lock:
            forced = self.forced_next
            self.forced_next = None
            candidates = list(self.songs)
            recent = list(self.recent_artists)
        if forced:
            return forced
        fresh = [s for s in candidates if s[2] not in recent]
        pool = fresh if fresh else candidates
        random.shuffle(pool)
        for folder, filename, _artist, _genre in pool:
            path = os.path.join(self.music_dir, folder, filename)
            if os.path.isfile(path):
                return (folder, filename)
        return None

    def _feed_loop(self):
        time.sleep(0.5)
        while True:
            # Wait for an encoder that is actually alive. Feeding a dead one
            # burns through the library at zero bytes per track and floods the
            # schedule with bogus entries that all share a timestamp.
            encoder = self.encoder
            if encoder is None or encoder.stdin is None or encoder.poll() is not None:
                time.sleep(0.5)
                continue
            track = self._select_next()
            if not track:
                time.sleep(1)
                continue
            self._pipe_track(track)

    def _track_meta(self, path, folder, filename):
        '''Metadata for a track, reading tags/duration only when not already cached.'''
        with self.lock:
            meta = self.meta_cache.get(path)
        if meta is None:
            tags = self._read_tags(path)
            meta = {
                'mtime': None,
                'artist': tags['artist'] or folder,
                'title': tags['title'] or filename.rsplit('.', 1)[0],
                'genre': tags['genre'],
                'duration': None,
            }
            with self.lock:
                self.meta_cache[path] = meta
        if meta['duration'] is None:
            meta['duration'] = self._probe_duration(path)
        return meta

    def _pipe_track(self, track):
        folder, filename = track
        path = os.path.join(self.music_dir, folder, filename)
        if not os.path.isfile(path):
            return
        meta = self._track_meta(path, folder, filename)
        artist = meta['artist']
        title = meta['title']
        genre = meta['genre']
        duration = meta['duration']

        with self.lock:
            epoch = self.encoder_epoch
            encoder = self.encoder
            if self.encoder_start_pdt is None:
                self.encoder_start_pdt = time.time()
            offset = self.cumulative_pcm_bytes / PCM_BYTES_PER_SECOND
            entry = {
                'pdt': self.encoder_start_pdt + offset,
                'artist': artist,
                'track': title,
                'genre': genre,
                'folder': folder,
                'file': filename,
                'duration': duration,
            }
            self.schedule.append(entry)
            if len(self.schedule) > 20:
                self.schedule.pop(0)
            self.recent_artists.append(artist)
            if len(self.recent_artists) > 20:
                self.recent_artists.pop(0)

        print(f'[radio] queued: {artist} - {title} ({duration:.0f}s) [{genre or "unknown"}]')

        decoder = subprocess.Popen([
            'ffmpeg', '-loglevel', 'error',
            '-i', path,
            '-f', 's16le', '-ar', str(SAMPLE_RATE), '-ac', str(CHANNELS),
            'pipe:1',
        ], stdout=subprocess.PIPE)

        with self.lock:
            self.current_decoder = decoder

        bytes_piped = 0
        try:
            while True:
                chunk = decoder.stdout.read(65536)
                if not chunk:
                    break
                with self.lock:
                    if self.skip_requested:
                        break
                if encoder is None or encoder.poll() is not None:
                    break
                try:
                    encoder.stdin.write(chunk)
                except (BrokenPipeError, OSError):
                    break
                bytes_piped += len(chunk)
        finally:
            if decoder.poll() is None:
                decoder.terminate()
                try:
                    decoder.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    decoder.kill()
            with self.lock:
                self.current_decoder = None
                self.skip_requested = False
                # Only credit the byte count if the encoder we fed is still the
                # live one; otherwise this would offset the new stream's clock.
                if self.encoder_epoch == epoch:
                    self.cumulative_pcm_bytes += bytes_piped

    def skip(self):
        # Only arm the flag while a track is actually being piped. Setting it in
        # the gap between tracks would leave it pending for the next one.
        with self.lock:
            dec = self.current_decoder
            if dec is None:
                return False
            self.skip_requested = True
        if dec.poll() is None:
            try:
                dec.terminate()
            except OSError:
                pass
        return True

    def skip_to_artist(self, artist_name):
        matches = [(f, t) for f, t, _a, _g in self.songs if f == artist_name]
        if not matches:
            return
        folder, filename = random.choice(matches)
        with self.lock:
            self.forced_next = (folder, filename)
        self.skip()

    def skip_to_genre(self, genre_query):
        q = genre_query.lower()
        matches = [(f, t) for f, t, _a, g in self.songs if g and q in g.lower()]
        if not matches:
            return
        folder, filename = random.choice(matches)
        with self.lock:
            self.forced_next = (folder, filename)
        self.skip()

    def now(self):
        with self.lock:
            sched = [dict(e) for e in self.schedule]
        return {
            'schedule': sched,
            'server_time': time.time(),
        }


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def resolve_under(self, base, rel):
        '''Resolve rel under base, or None if it escapes.

        A plain startswith() check would also accept a sibling directory whose
        name merely begins with base (music -> musicXXX), so compare paths.
        '''
        base = os.path.realpath(base)
        target = os.path.realpath(os.path.join(base, rel.lstrip('/')))
        if target != base and not target.startswith(base + os.sep):
            return None
        return target

    def is_local_client(self):
        '''True when the request came from the host or a private network.

        nginx overwrites X-Real-IP with the real peer address, so a client
        cannot forge it. No header at all means the request bypassed nginx,
        which only a local process can do (the port is bound to 127.0.0.1).
        '''
        raw = self.headers.get('X-Real-IP')
        if raw is None:
            return True
        try:
            ip = ipaddress.ip_address(raw.strip())
        except ValueError:
            return False
        if ip in TAILSCALE_NET:
            return True
        return ip.is_loopback or ip.is_private or ip.is_link_local

    def serve_json(self, data):
        self.respond(200, 'application/json', json.dumps(data).encode())

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path in STATIC_ROUTES:
            self.serve_file(os.path.join(STATIC_DIR, STATIC_ROUTES[path]))

        elif path.startswith('/stream/'):
            file_path = self.resolve_under(STREAM_DIR, path[len('/stream/'):])
            if file_path is None:
                self.respond(403, 'text/plain', b'forbidden')
                return
            if not os.path.isfile(file_path):
                self.respond(404, 'text/plain', b'not found')
                return
            self.serve_stream_file(file_path)

        elif path.startswith('/images/') or path.startswith('/video/') or path.startswith('/fonts/'):
            file_path = self.resolve_under(STATIC_DIR, urllib.parse.unquote(path))
            if file_path is None:
                self.respond(403, 'text/plain', b'forbidden')
                return
            if not os.path.isfile(file_path):
                self.respond(404, 'text/plain', b'not found')
                return
            if file_path.endswith('.mp4'):
                self.stream_file(file_path)
            else:
                self.serve_file(file_path)

        elif path == '/api/debug':
            self.serve_json({'debug': DEBUG, 'admin': self.is_local_client()})

        elif path == '/api/radio/now':
            params = urllib.parse.parse_qs(parsed.query)
            sid = params.get('sid', [''])[0]
            if sid:
                touch_listener(sid)
            self.serve_json(radio.now())

        elif path == '/api/radio/listeners':
            self.serve_json({'count': get_listener_count()})

        elif path in ('/api/radio/skip', '/api/radio/skip-to', '/api/radio/skip-to-genre'):
            if not self.is_local_client():
                self.respond(403, 'text/plain', b'forbidden')
                return
            params = urllib.parse.parse_qs(parsed.query)
            if path == '/api/radio/skip':
                radio.skip()
            elif path == '/api/radio/skip-to':
                radio.skip_to_artist(params.get('artist', [''])[0])
            else:
                radio.skip_to_genre(params.get('genre', [''])[0])
            self.serve_json(radio.now())

        elif path == '/api/radio/lyrics':
            params = urllib.parse.parse_qs(parsed.query)
            song = params.get('song', [''])[0]
            # song is "folder/file.mp3" straight from the client, so it gets the
            # same containment treatment as any other path we take from a user.
            audio_path = self.resolve_under(MUSIC_DIR, song) if song else None
            if audio_path is None:
                self.respond(403, 'text/plain', b'forbidden')
                return
            self.serve_json({'lines': load_lyrics(audio_path)})

        elif path == '/api/radio/votes':
            params = urllib.parse.parse_qs(parsed.query)
            song = params.get('song', [''])[0]
            client = params.get('client', [''])[0]
            self.serve_json(votes.get(song, client))

        elif path == '/api/radio/skip-info':
            params = urllib.parse.parse_qs(parsed.query)
            try:
                ts = float(params.get('ts', ['0'])[0])
            except ValueError:
                self.respond(400, 'text/plain', b'bad ts')
                return
            client = params.get('client', [''])[0]
            with skip_votes_lock:
                voters = skip_votes.get(ts, set())
                data = {
                    'votes': len(voters),
                    'voted': client in voters,
                    'remaining': get_skips_remaining(),
                }
            self.serve_json(data)

        else:
            self.respond(404, 'text/plain', b'not found')

    def read_json_body(self):
        '''Parsed JSON object from the request body, or None (a 400 is sent).'''
        try:
            length = int(self.headers.get('Content-Length', 0))
        except ValueError:
            length = -1
        if length < 0 or length > 65536:
            self.respond(400, 'text/plain', b'bad content-length')
            return None
        try:
            body = json.loads(self.rfile.read(length) or b'{}')
        except (ValueError, UnicodeDecodeError):
            self.respond(400, 'text/plain', b'bad json')
            return None
        if not isinstance(body, dict):
            self.respond(400, 'text/plain', b'bad json')
            return None
        return body

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == '/api/radio/vote':
            body = self.read_json_body()
            if body is None:
                return
            song = body.get('song', '')
            client = body.get('client', '')
            vote = body.get('vote')
            if vote not in ('up', 'down', None):
                self.respond(400, 'text/plain', b'bad vote')
                return
            # Both land in the votes table as a primary key, so cap them rather
            # than letting anyone grow the database with junk rows.
            if not isinstance(song, str) or not isinstance(client, str) \
                    or not song or len(song) > 512 or len(client) > 128:
                self.respond(400, 'text/plain', b'bad key')
                return
            self.serve_json(votes.cast(song, client, vote))

        elif path == '/api/radio/vote-skip':
            body = self.read_json_body()
            if body is None:
                return
            ts = body.get('ts', 0)
            client = body.get('client', '')
            if not isinstance(ts, (int, float)):
                self.respond(400, 'text/plain', b'bad ts')
                return
            skipped = False

            with skip_votes_lock:
                prune_skip_votes()
                remaining = get_skips_remaining()
                if ts not in skip_votes:
                    skip_votes[ts] = set()
                already_voted = client in skip_votes[ts]

                if already_voted or remaining <= 0:
                    self.serve_json({
                        'votes': len(skip_votes[ts]),
                        'voted': True,
                        'remaining': remaining,
                        'skipped': False,
                    })
                    return

                count_before = len(skip_votes[ts])
                skip_votes[ts].add(client)
                chance = 1.0 / max(1, 10 - count_before)
                skipped = random.random() < chance
                stamp = time.time()
                if skipped:
                    skip_history.append(stamp)
                remaining = get_skips_remaining()
                vote_count = len(skip_votes[ts])

            if skipped and not radio.skip():
                # Landed in the gap between tracks, so nothing was cut. Hand the
                # credit back rather than burning one of the three hourly skips.
                with skip_votes_lock:
                    try:
                        skip_history.remove(stamp)
                    except ValueError:
                        pass
                    remaining = get_skips_remaining()
                skipped = False

            self.serve_json({
                'votes': vote_count,
                'voted': True,
                'remaining': remaining,
                'skipped': skipped,
            })

        else:
            self.respond(404, 'text/plain', b'not found')

    def respond(self, code, content_type, body):
        self.send_response(code)
        accept_enc = self.headers.get('Accept-Encoding', '')
        if 'gzip' in accept_enc and content_type in (
            'text/html', 'text/css', 'text/plain',
            'application/javascript', 'application/json',
        ):
            body = gzip.compress(body)
            self.send_header('Content-Encoding', 'gzip')
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', len(body))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def serve_file(self, filepath):
        if not os.path.isfile(filepath):
            self.respond(404, 'text/plain', b'not found')
            return
        stat = os.stat(filepath)
        etag = f'"{stat.st_mtime_ns:x}-{stat.st_size:x}"'
        if self.headers.get('If-None-Match') == etag:
            self.send_response(304)
            self.send_header('ETag', etag)
            self.end_headers()
            return
        ext = os.path.splitext(filepath)[1].lower()
        mime = MIME_TYPES.get(ext, 'application/octet-stream')
        with open(filepath, 'rb') as f:
            body = f.read()
        self.send_response(200)
        accept_enc = self.headers.get('Accept-Encoding', '')
        if 'gzip' in accept_enc and mime in (
            'text/html', 'text/css', 'text/plain',
            'application/javascript', 'application/json',
        ):
            body = gzip.compress(body)
            self.send_header('Content-Encoding', 'gzip')
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', len(body))
        cache = 'no-cache' if filepath.endswith('sw.js') else 'public, max-age=3600'
        self.send_header('Cache-Control', cache)
        self.send_header('ETag', etag)
        # These responses are cacheable and conditionally gzipped, so the cache
        # key has to include the encoding or a gzipped body can be replayed to a
        # client that never asked for one.
        self.send_header('Vary', 'Accept-Encoding')
        self.end_headers()
        self.wfile.write(body)

    def serve_stream_file(self, filepath):
        ext = os.path.splitext(filepath)[1].lower()
        mime = MIME_TYPES.get(ext, 'application/octet-stream')
        try:
            with open(filepath, 'rb') as f:
                body = f.read()
        except OSError:
            self.respond(404, 'text/plain', b'not found')
            return
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', len(body))
        self.send_header('Cache-Control', 'no-cache' if ext == '.m3u8' else 'public, max-age=60')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def parse_range(self, header, size):
        '''Parse a single byte range. Returns (start, end), None, or 'invalid'.

        Handles the suffix form (bytes=-500) and clamps the end to the file, both
        of which iOS Safari exercises when fetching video. Multi-range requests
        are answered with the whole file, which is a legal response.
        '''
        if not header or not header.startswith('bytes='):
            return None
        spec = header[len('bytes='):].strip()
        if ',' in spec:
            return None
        if '-' not in spec:
            return 'invalid'
        first, _, last = spec.partition('-')
        first, last = first.strip(), last.strip()
        try:
            if not first:
                if not last:
                    return 'invalid'
                length = int(last)
                if length <= 0:
                    return 'invalid'
                start = max(0, size - length)
                end = size - 1
            else:
                start = int(first)
                end = int(last) if last else size - 1
        except ValueError:
            return 'invalid'
        if start >= size or start < 0 or end < start:
            return 'invalid'
        return start, min(end, size - 1)

    def stream_file(self, filepath):
        ext = os.path.splitext(filepath)[1].lower()
        mime = MIME_TYPES.get(ext, 'application/octet-stream')
        size = os.path.getsize(filepath)
        rng = self.parse_range(self.headers.get('Range'), size)

        if rng == 'invalid':
            self.send_response(416)
            self.send_header('Content-Range', f'bytes */{size}')
            self.send_header('Content-Length', 0)
            self.end_headers()
            return

        if rng:
            start, end = rng
            length = end - start + 1
            self.send_response(206)
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
            self.send_header('Content-Length', length)
        else:
            start = 0
            length = size
            self.send_response(200)
            self.send_header('Content-Length', size)

        self.send_header('Content-Type', mime)
        self.send_header('Accept-Ranges', 'bytes')
        self.end_headers()

        try:
            with open(filepath, 'rb') as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (ConnectionResetError, BrokenPipeError):
            pass


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('-d', '--debug', action='store_true', help='Enable debug controls (skip, genre jump)')
    args = parser.parse_args()
    DEBUG = args.debug
    init_db()
    votes = VoteStore(DB_PATH)
    radio = Radio(MUSIC_DIR, AUDIO_EXTS, STREAM_DIR)
    server = http.server.ThreadingHTTPServer((HOST, PORT), Handler)
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    print(f'listening on http://{HOST}:{PORT}' + (' [debug]' if DEBUG else ''))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print('[server] stopped')
