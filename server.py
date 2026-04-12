#!/usr/bin/env python3
# ACID RADIO - Developed by acidvegas in Python (https://git.supernets.org/acidvegas/acid-radio)
# acid-radio/server.py

import argparse
import gzip
import http.server
import json
import os
import random
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

HOST = '0.0.0.0'
PORT = 7000
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
        listeners[addr] = time.time()


def get_skips_remaining():
    now = time.time()
    skip_history[:] = [t for t in skip_history if t > now - 3600]
    return max(0, MAX_SKIPS_PER_HOUR - len(skip_history))

AUDIO_EXTS = {'.mp3', '.flac', '.ogg', '.wav', '.m4a', '.opus', '.aac'}
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
    '/style.css':                               'style.css',
    '/app.js':                                  'app.js',
    '/radio':                                   'index.html',
    '/radio.css':                               'radio.css',
    '/radio.js':                                'radio.js',
    '/sw.js':                                   'sw.js',
    '/manifest.json':                           'manifest.json',
    '/icon-192.png':                            'icon-192.png',
    '/icon-512.png':                            'icon-512.png',
}

MIME_TYPES['.gif'] = 'image/gif'
MIME_TYPES['.png'] = 'image/png'
MIME_TYPES['.jpg'] = 'image/jpeg'
MIME_TYPES['.mp4'] = 'video/mp4'
MIME_TYPES['.json'] = 'application/json'


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
    def __init__(self, music_dir, audio_exts):
        self.music_dir = music_dir
        self.audio_exts = audio_exts
        self.lock = threading.Lock()
        self.current = None
        self.next_track = None
        self.songs = []
        self.recent_artists = []
        self._scan()
        if self.songs:
            self._advance()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _scan(self):
        songs = []
        for folder in os.listdir(self.music_dir):
            ap = os.path.join(self.music_dir, folder)
            if not os.path.isdir(ap):
                continue
            for f in os.listdir(ap):
                if os.path.splitext(f)[1].lower() in self.audio_exts:
                    path = os.path.join(ap, f)
                    tags = self._read_tags(path)
                    id3_artist = tags['artist'] or folder
                    songs.append((folder, f, id3_artist))
        with self.lock:
            self.songs = songs
        if songs:
            print(f'[radio] scanned {len(songs)} tracks')

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

    def _select_next(self):
        with self.lock:
            candidates = list(self.songs)
            recent = list(self.recent_artists)
            cur = (self.current['folder'], self.current['file']) if self.current else None
        fresh = [(f, t, a) for f, t, a in candidates if a not in recent]
        pool = fresh if fresh else candidates
        random.shuffle(pool)
        for folder, filename, _artist in pool:
            if cur and (folder, filename) == cur:
                continue
            path = os.path.join(self.music_dir, folder, filename)
            if os.path.isfile(path):
                tags = self._read_tags(path)
                with self.lock:
                    self.next_track = {
                        'folder': folder,
                        'file': filename,
                        'artist': tags['artist'] or folder,
                        'track': tags['title'] or filename.rsplit('.', 1)[0],
                    }
                return
        with self.lock:
            self.next_track = None

    def _advance(self):
        with self.lock:
            nxt = self.next_track
            self.next_track = None
        if nxt:
            path = os.path.join(self.music_dir, nxt['folder'], nxt['file'])
            if os.path.isfile(path):
                self._play(nxt['folder'], nxt['file'])
                self._select_next()
                return
        with self.lock:
            candidates = list(self.songs)
            recent = list(self.recent_artists)
        fresh = [(f, t, a) for f, t, a in candidates if a not in recent]
        pool = fresh if fresh else candidates
        random.shuffle(pool)
        for folder, filename, _artist in pool:
            path = os.path.join(self.music_dir, folder, filename)
            if os.path.isfile(path):
                self._play(folder, filename)
                self._select_next()
                return

    def _loop(self):
        last_scan = time.time()
        while True:
            time.sleep(0.5)
            if time.time() - last_scan >= 300:
                self._scan()
                last_scan = time.time()
            with self.lock:
                if not self.current:
                    continue
                elapsed = time.time() - self.current['started_at']
                if elapsed < self.current['duration']:
                    continue
            self._advance()

    def skip(self):
        self._advance()

    def skip_to_artist(self, artist_name):
        matches = [(f, t) for f, t, _a in self.songs if f == artist_name]
        if not matches:
            return
        folder, filename = random.choice(matches)
        self._play(folder, filename)
        self._select_next()

    def skip_to_genre(self, genre_query):
        matches = []
        for folder, filename, _artist in self.songs:
            path = os.path.join(self.music_dir, folder, filename)
            tags = self._read_tags(path)
            if tags['genre'] and genre_query.lower() in tags['genre'].lower():
                matches.append((folder, filename))
        if not matches:
            return
        folder, filename = random.choice(matches)
        self._play(folder, filename)
        self._select_next()

    def _play(self, folder, filename):
        path = os.path.join(self.music_dir, folder, filename)
        dur = self._probe_duration(path)
        tags = self._read_tags(path)
        artist = tags['artist'] or folder
        title = tags['title'] or filename.rsplit('.', 1)[0]
        genre = tags['genre']
        with self.lock:
            self.current = {
                'artist': artist,
                'track': title,
                'folder': folder,
                'file': filename,
                'started_at': time.time(),
                'duration': dur,
                'genre': genre,
            }
            self.recent_artists.append(artist)
            if len(self.recent_artists) > 20:
                self.recent_artists.pop(0)
        print(f'[radio] now playing: {artist} - {title} ({dur:.0f}s) [{genre or "unknown"}]')

    def now(self):
        with self.lock:
            if not self.current:
                return None
            data = {**self.current, 'server_time': time.time()}
            if self.next_track:
                data['next'] = {
                    'folder': self.next_track['folder'],
                    'file': self.next_track['file'],
                }
            return data


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path in STATIC_ROUTES:
            self.serve_file(os.path.join(STATIC_DIR, STATIC_ROUTES[path]))

        elif path.startswith('/images/') or path.startswith('/video/'):
            file_path = os.path.realpath(os.path.join(STATIC_DIR, path.lstrip('/')))
            if not file_path.startswith(os.path.realpath(STATIC_DIR)):
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
            self.respond(200, 'application/json', json.dumps({'debug': DEBUG}).encode())

        elif path == '/api/artists':
            artists = []
            for d in os.listdir(MUSIC_DIR):
                dp = os.path.join(MUSIC_DIR, d)
                if not os.path.isdir(dp):
                    continue
                count = sum(
                    1 for f in os.listdir(dp)
                    if os.path.isfile(os.path.join(dp, f))
                    and os.path.splitext(f)[1].lower() in AUDIO_EXTS
                )
                artists.append({'name': d, 'count': count})
            artists.sort(key=lambda a: a['name'].lower())
            self.respond(200, 'application/json', json.dumps(artists).encode())

        elif path == '/api/tracks':
            params = urllib.parse.parse_qs(parsed.query)
            artist = params.get('artist', [''])[0]
            artist_path = os.path.join(MUSIC_DIR, artist)
            if not os.path.isdir(artist_path):
                self.respond(404, 'text/plain', b'not found')
                return
            tracks = sorted([
                f for f in os.listdir(artist_path)
                if os.path.isfile(os.path.join(artist_path, f))
                and os.path.splitext(f)[1].lower() in AUDIO_EXTS
            ], key=str.lower)
            self.respond(200, 'application/json', json.dumps(tracks).encode())

        elif path == '/api/radio/now':
            params = urllib.parse.parse_qs(parsed.query)
            sid = params.get('sid', [''])[0]
            if sid:
                touch_listener(sid)
            state = radio.now()
            self.respond(200, 'application/json', json.dumps(state).encode())

        elif path == '/api/radio/listeners':
            self.respond(200, 'application/json', json.dumps({'count': get_listener_count()}).encode())

        elif path == '/api/radio/skip':
            radio.skip()
            state = radio.now()
            self.respond(200, 'application/json', json.dumps(state).encode())

        elif path == '/api/radio/skip-to':
            params = urllib.parse.parse_qs(parsed.query)
            artist = params.get('artist', [''])[0]
            radio.skip_to_artist(artist)
            state = radio.now()
            self.respond(200, 'application/json', json.dumps(state).encode())

        elif path == '/api/radio/skip-to-genre':
            params = urllib.parse.parse_qs(parsed.query)
            genre = params.get('genre', [''])[0]
            radio.skip_to_genre(genre)
            state = radio.now()
            self.respond(200, 'application/json', json.dumps(state).encode())

        elif path == '/api/radio/votes':
            params = urllib.parse.parse_qs(parsed.query)
            song = params.get('song', [''])[0]
            client = params.get('client', [''])[0]
            data = votes.get(song, client)
            self.respond(200, 'application/json', json.dumps(data).encode())

        elif path == '/api/radio/skip-info':
            params = urllib.parse.parse_qs(parsed.query)
            ts = float(params.get('ts', ['0'])[0])
            client = params.get('client', [''])[0]
            with skip_votes_lock:
                voters = skip_votes.get(ts, set())
                data = {
                    'votes': len(voters),
                    'voted': client in voters,
                    'remaining': get_skips_remaining(),
                }
            self.respond(200, 'application/json', json.dumps(data).encode())

        elif path.startswith('/music/'):
            file_path = urllib.parse.unquote(path[7:])
            full_path = os.path.realpath(os.path.join(MUSIC_DIR, file_path))
            if not full_path.startswith(os.path.realpath(MUSIC_DIR)):
                self.respond(403, 'text/plain', b'forbidden')
                return
            if not os.path.isfile(full_path):
                self.respond(404, 'text/plain', b'not found')
                return
            self.stream_file(full_path)

        else:
            self.respond(404, 'text/plain', b'not found')

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == '/api/radio/vote':
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length))
            song = body.get('song', '')
            client = body.get('client', '')
            vote = body.get('vote')
            if vote not in ('up', 'down', None):
                self.respond(400, 'text/plain', b'bad vote')
                return
            data = votes.cast(song, client, vote)
            self.respond(200, 'application/json', json.dumps(data).encode())

        elif path == '/api/radio/vote-skip':
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length))
            ts = body.get('ts', 0)
            client = body.get('client', '')
            skipped = False

            with skip_votes_lock:
                remaining = get_skips_remaining()
                if ts not in skip_votes:
                    skip_votes[ts] = set()
                already_voted = client in skip_votes[ts]

                if already_voted or remaining <= 0:
                    data = {
                        'votes': len(skip_votes[ts]),
                        'voted': True,
                        'remaining': remaining,
                        'skipped': False,
                    }
                    self.respond(200, 'application/json', json.dumps(data).encode())
                    return

                count_before = len(skip_votes[ts])
                skip_votes[ts].add(client)
                chance = 1.0 / max(1, 10 - count_before)
                skipped = random.random() < chance
                if skipped:
                    skip_history.append(time.time())
                remaining = get_skips_remaining()
                vote_count = len(skip_votes[ts])

            if skipped:
                radio.skip()

            data = {
                'votes': vote_count,
                'voted': True,
                'remaining': remaining,
                'skipped': skipped,
            }
            self.respond(200, 'application/json', json.dumps(data).encode())

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
        self.end_headers()
        self.wfile.write(body)

    def stream_file(self, filepath):
        ext = os.path.splitext(filepath)[1].lower()
        mime = MIME_TYPES.get(ext, 'application/octet-stream')
        size = os.path.getsize(filepath)
        range_header = self.headers.get('Range')

        if range_header:
            ranges = range_header.replace('bytes=', '').split('-')
            start = int(ranges[0])
            end = int(ranges[1]) if ranges[1] else size - 1
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
    radio = Radio(MUSIC_DIR, AUDIO_EXTS)
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
