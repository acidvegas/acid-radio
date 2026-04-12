#!/usr/bin/env python3
# ACID RADIO - Developed by acidvegas in Python (https://git.supernets.org/acidvegas/acid-radio)
# acid-radio/bot/y2mp3.py

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import time
import urllib.request

try:
    from mutagen.id3 import ID3, ID3NoHeaderError, TPE1, TIT2, TCON
except ImportError:
    raise ImportError('missing mutagen library (pip install mutagen)')


API_BASE        = 'https://embed.dlsrv.online'
UA              = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
QUALITY_CHOICES = ['320', '256', '128', '96', '64']


class YouTubeMP3:
    def __init__(self, output_dir='music/Downloads', quality='320', verbose=False):
        self.output_dir = output_dir
        self.quality = quality
        self.verbose = verbose
        self._sign_key = None

    def _log(self, msg, debug=False):
        if debug and not self.verbose:
            return
        print(msg, flush=True)

    @staticmethod
    def extract_video_id(url):
        for pat in [
            r'(?:v=|/v/|youtu\.be/)([a-zA-Z0-9_-]{11})',
            r'^([a-zA-Z0-9_-]{11})$',
        ]:
            m = re.search(pat, url)
            if m:
                return m.group(1)
        return None

    def _fetch_sign_key(self):
        if self._sign_key:
            return self._sign_key

        self._log('[*] Extracting signing key...', debug=True)
        headers = {'User-Agent': UA, 'Accept': '*/*'}
        req = urllib.request.Request(API_BASE + '/v1/full?videoId=dQw4w9WgXcQ', headers=headers)
        html = urllib.request.urlopen(req, timeout=15).read().decode('utf-8', errors='replace')

        for src in re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', html):
            if src.startswith('/cdn-cgi/') or 'turbopack' in src:
                continue
            if src.startswith('/'):
                src = API_BASE + src
            self._log(f'  checking {src}', debug=True)
            try:
                req = urllib.request.Request(src, headers=headers)
                js = urllib.request.urlopen(req, timeout=15).read().decode('utf-8', errors='replace')
            except Exception:
                continue
            m = re.search(r'TextEncoder\(\)\.encode\(\w+\s*\+\s*["\']([^"\']{20,}?)["\']\)', js)
            if m:
                self._sign_key = m.group(1)
                self._log(f'[*] Signing key: {self._sign_key[:12]}...', debug=True)
                return self._sign_key

        raise RuntimeError('could not extract signing key')

    def _sign(self):
        key = self._fetch_sign_key()
        ts = str(int(time.time() * 1000))
        sig = hashlib.sha256((ts + key).encode()).hexdigest()
        return ts, sig

    def _api_post(self, endpoint, payload):
        url = API_BASE + endpoint
        ts, sig = self._sign()
        headers = {
            'User-Agent': UA,
            'Content-Type': 'application/json',
            'Accept': '*/*',
            'Origin': API_BASE,
            'Referer': API_BASE + '/',
            'x-app-timestamp': ts,
            'x-app-signature': sig,
        }
        body = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=body, headers=headers)
        self._log(f'  [POST] {url}', debug=True)
        resp = urllib.request.urlopen(req, timeout=30)
        raw = resp.read().decode('utf-8', errors='replace')
        self._log(f'  [{resp.status}] {raw[:300]}', debug=True)
        return json.loads(raw)

    def _download_file(self, url, output):
        headers = {'User-Agent': UA, 'Accept': '*/*', 'Referer': API_BASE + '/'}
        req = urllib.request.Request(url, headers=headers)
        self._log(f'  [GET] {url[:120]}', debug=True)
        with urllib.request.urlopen(req, timeout=120) as resp:
            with open(output, 'wb') as f:
                total = 0
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    total += len(chunk)
        return os.path.getsize(output)

    @staticmethod
    def _set_id3(filepath, artist, title, genre):
        try:
            tags = ID3(filepath)
        except ID3NoHeaderError:
            tags = ID3()
        tags.add(TPE1(encoding=3, text=[artist]))
        tags.add(TIT2(encoding=3, text=[title]))
        if genre:
            tags.add(TCON(encoding=3, text=[genre]))
        tags.save(filepath)

    @staticmethod
    def _human_size(nbytes):
        for unit in ('B', 'KB', 'MB', 'GB'):
            if nbytes < 1024:
                return f'{nbytes:.1f} {unit}' if unit != 'B' else f'{nbytes} B'
            nbytes /= 1024
        return f'{nbytes:.1f} TB'

    @staticmethod
    def _human_duration(seconds):
        try:
            s = int(seconds)
        except (TypeError, ValueError):
            return '?:??'
        m, s = divmod(s, 60)
        return f'{m}:{s:02d}'

    def _download_sync(self, url, artist, title, genre=None):
        '''Synchronous download — returns dict with keys: ok, msg, path, size, duration.'''
        fail = lambda m: {'ok': False, 'msg': m, 'path': None, 'size': 0, 'duration': 0}

        video_id = self.extract_video_id(url)
        if not video_id:
            return fail(f'cannot parse video ID from: {url}')

        self._log(f'[*] Video ID: {video_id}')

        try:
            info_resp = self._api_post('/api/info', {'videoId': video_id})
        except Exception as e:
            return fail(f'/api/info failed: {e}')

        if info_resp.get('error'):
            return fail(f'API error: {info_resp}')

        info = info_resp.get('info', info_resp)
        yt_title = info.get('title', video_id)
        duration = info.get('duration', 0)
        self._log(f'[*] YouTube title: {yt_title}')
        self._log(f'[*] Duration: {duration}s')

        try:
            dl_resp = self._api_post('/api/download/mp3', {
                'videoId': video_id,
                'format': 'mp3',
                'quality': self.quality,
            })
        except Exception as e:
            return fail(f'/api/download/mp3 failed: {e}')

        if dl_resp.get('error'):
            return fail(f'download API error: {dl_resp}')

        dl_url = dl_resp.get('url')
        if not dl_url:
            return fail('no download URL in response')

        safe_artist = re.sub(r'[^\w\s\-\(\)\[\]&]', '', artist).strip()
        safe_title = re.sub(r'[^\w\s\-\(\)\[\]&]', '', title).strip()
        filename = f'{safe_artist} - {safe_title}.mp3'
        os.makedirs(self.output_dir, exist_ok=True)
        filepath = os.path.join(self.output_dir, filename)

        self._log(f'[*] Downloading to: {filepath}')
        try:
            size = self._download_file(dl_url, filepath)
        except Exception as e:
            return fail(f'download failed: {e}')

        if size < 10000:
            with open(filepath, 'rb') as f:
                head = f.read(500)
            if b'<html' in head.lower() or b'<!doctype' in head.lower():
                os.remove(filepath)
                return fail('got HTML instead of audio (URL expired?)')

        self._log(f'[*] Setting ID3 tags: artist={artist}, title={title}, genre={genre}')
        try:
            self._set_id3(filepath, artist, title, genre)
        except Exception as e:
            self._log(f'[!] ID3 tagging failed (file kept): {e}')

        self._log(f'[+] Done! {filename} ({self._human_size(size)}, {self._human_duration(duration)})')
        return {
            'ok'       : True,
            'msg'      : filename,
            'path'     : filepath,
            'size'     : size,
            'duration' : duration,
        }

    async def download(self, url, artist, title, genre=None):
        '''Async wrapper — runs the blocking download in an executor.'''
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._download_sync, url, artist, title, genre)


def main():
    parser = argparse.ArgumentParser(description='Download YouTube audio as MP3 via dlsrv')
    parser.add_argument('url', help='YouTube video URL or video ID')
    parser.add_argument('-a', '--artist', required=True, help='Artist / band name')
    parser.add_argument('-t', '--title', required=True, help='Song title')
    parser.add_argument('-g', '--genre', default=None, help='Genre tag')
    parser.add_argument('-o', '--output-dir', default='music/Downloads', help='Output directory')
    parser.add_argument('-q', '--quality', default='320', choices=QUALITY_CHOICES)
    parser.add_argument('-v', '--verbose', action='store_true')
    args = parser.parse_args()

    dl = YouTubeMP3(output_dir=args.output_dir, quality=args.quality, verbose=args.verbose)
    result = dl._download_sync(args.url, args.artist, args.title, args.genre)
    if not result['ok']:
        print(f'[!] {result["msg"]}')
        sys.exit(1)
    print(f'[+] {result["msg"]} ({dl._human_size(result["size"])}, {dl._human_duration(result["duration"])})')



if __name__ == '__main__':
    main()
