#!/usr/bin/env python3
# ACID RADIO - Developed by acidvegas in Python (https://git.supernets.org/acidvegas/acid-radio)
# acid-radio/bot/radiobot.py

import asyncio
import fnmatch
import json
import os
import re
import time
import urllib.parse
import urllib.request

try:
	from dotenv import load_dotenv
except ImportError:
	raise ImportError('missing dotenv library (pip install python-dotenv)')

try:
	from y2mp3 import YouTubeMP3
except ImportError:
	raise ImportError('missing y2mp3 locally')


load_dotenv()


BOT_DIR      = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BOT_DIR)

IRC_SERVER        = 'irc.supernets.org'
IRC_PORT          = 6697
IRC_SSL           = True
NICK              = 'ACID_RADIO'
NICKSERV_PASSWORD = os.getenv('NICKSERV_PASSWORD')
USER              = 'radio'
REALNAME          = 'https://radio.acid.vegas'
CHANNEL           = '#superbowl'
RADIO_URL         = 'https://radio.acid.vegas'
USER_AGENT        = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
ANNOUNCE_INTERVAL = 14_400 # 4 hours
COOLDOWN          = 3
BOT_CLIENT_ID     = 'irc-bot'
ADMIN_MASK        = 'acidvegas!~stillfree@most.dangerous.motherfuck'
MUSIC_DIR         = os.path.join(PROJECT_ROOT, 'music')
DOWNLOAD_DIR      = os.path.join(MUSIC_DIR, 'Downloads')
IGNORE_FILE       = os.path.join(BOT_DIR, 'ignores.json')


def load_ignores():
	try:
		with open(IGNORE_FILE) as f:
			return json.load(f)
	except (FileNotFoundError, json.JSONDecodeError):
		return []


def save_ignores(ignores):
	with open(IGNORE_FILE, 'w') as f:
		json.dump(ignores, f, indent=2)


def is_ignored(source, ignores):
	for pattern in ignores:
		if fnmatch.fnmatch(source.lower(), pattern.lower()):
			return True
	return False


def api_get(path):
	req = urllib.request.Request(f'{RADIO_URL}{path}', headers={'User-Agent': USER_AGENT})
	with urllib.request.urlopen(req, timeout=5) as r:
		return json.loads(r.read())


def api_post(path, body):
	data = json.dumps(body).encode()
	req = urllib.request.Request(
		f'{RADIO_URL}{path}',
		data=data,
		headers={'Content-Type': 'application/json', 'User-Agent': USER_AGENT},
	)
	with urllib.request.urlopen(req, timeout=5) as r:
		return json.loads(r.read())


def get_now_playing():
	try:
		data = api_get('/api/radio/now')
		if not data:
			return None
		return {
			'artist': data['artist'],
			'track': data['track'],
			'genre': data.get('genre') or 'unknown',
			'song_key': data['folder'] + '/' + data['file'],
		}
	except Exception as e:
		print(f'[bot] get_now_playing failed: {e}', flush=True)
		return None


def get_votes(song_key):
	try:
		data = api_get(f'/api/radio/votes?song={urllib.parse.quote(song_key)}&client={BOT_CLIENT_ID}')
		return data
	except Exception as e:
		print(f'[bot] get_votes failed: {e}', flush=True)
		return {'up': 0, 'down': 0, 'my_vote': None}


def get_listener_count():
	try:
		return api_get('/api/radio/listeners').get('count', 0)
	except Exception:
		return 0


def cast_vote(song_key, vote):
	try:
		return api_post('/api/radio/vote', {
			'song': song_key,
			'client': BOT_CLIENT_ID,
			'vote': vote,
		})
	except Exception as e:
		print(f'[bot] cast_vote failed: {e}', flush=True)
		return None


def irc_color(text, fg):
	return f'\x03{fg:02d}{text}\x03'


def format_np(np, votes, listeners):
	return (
		f'🎵 '
		f'\x02{irc_color(np["artist"], 3)}\x02 - '
		f'{irc_color(np["track"], 7)} '
		f'[{irc_color(np["genre"], 6)}] '
		f'👍 {irc_color(str(votes["up"]), 3)} '
		f'👎 {irc_color(str(votes["down"]), 4)} '
		f'({irc_color(str(listeners) + " listening", 14)}) '
		f'🎵 '
		f'\x1f{RADIO_URL}\x1f'
	)


def format_announce(np, votes, listeners):
	return (
		f'🎵 '
		f'\x02{irc_color(np["artist"], 3)}\x02 - '
		f'{irc_color(np["track"], 7)} '
		f'[{irc_color(np["genre"], 6)}] '
		f'👍 {irc_color(str(votes["up"]), 3)} '
		f'👎 {irc_color(str(votes["down"]), 4)} '
		f'({irc_color(str(listeners) + " listening", 14)}) '
		f'🎵 '
		f'\x1f{RADIO_URL}\x1f'
	)


def parse_source(raw):
	'''Parse :nick!user@host into the full mask string.'''
	if raw.startswith(':'):
		raw = raw[1:]
	return raw.split(' ', 1)[0]


def parse_quoted_args(text):
	'''Parse space-separated args where quoted strings are kept together.'''
	return re.findall(r'"([^"]+)"|(\S+)', text)


async def main():
	if IRC_SSL:
		import ssl
		ctx = ssl.create_default_context()
		reader, writer = await asyncio.open_connection(IRC_SERVER, IRC_PORT, ssl=ctx)
	else:
		reader, writer = await asyncio.open_connection(IRC_SERVER, IRC_PORT)

	downloader = YouTubeMP3(output_dir=DOWNLOAD_DIR, quality='320', verbose=True)
	votes_enabled = True
	ignores = load_ignores()

	def send(line):
		writer.write((line + '\r\n').encode())

	def privmsg(text):
		send(f'PRIVMSG {CHANNEL} :{text}')

	send(f'NICK {NICK}')
	send(f'USER {USER} 0 * :{REALNAME}')
	await writer.drain()

	joined = False
	last_announce = 0
	last_cmd = 0

	while True:
		line = await reader.readline()
		if not line:
			break
		line = line.decode('utf-8', errors='replace').strip()

		if line.startswith('PING'):
			send('PONG' + line[4:])
			await writer.drain()
			continue

		parts = line.split()

		if not joined and len(parts) > 1 and parts[1] == '001':
			if NICKSERV_PASSWORD:
				send(f'PRIVMSG NickServ :IDENTIFY {NICKSERV_PASSWORD}')
				await writer.drain()
			await asyncio.sleep(6)
			send(f'JOIN {CHANNEL}')
			await writer.drain()
			joined = True
			last_announce = time.time()
			continue

		if joined and len(parts) > 3 and parts[1] == 'PRIVMSG' and parts[2] == CHANNEL:
			source = parse_source(parts[0])
			msg = line.split(' :', 1)[-1] if ' :' in line else ''
			cmd = msg.strip()
			now = time.time()
			is_admin = source == ADMIN_MASK

			if not is_admin and is_ignored(source, ignores):
				continue

			if cmd.startswith('@radio ') and is_admin:
				subcmd = cmd[7:].strip()

				if subcmd == 'togglevotes':
					votes_enabled = not votes_enabled
					state = irc_color('ENABLED', 3) if votes_enabled else irc_color('DISABLED', 4)
					privmsg(f'🎵 Voting is now {state}')
					await writer.drain()
					continue

				if subcmd == 'ignore':
					if not ignores:
						privmsg('ignore list is empty')
					else:
						privmsg('ignores: ' + ', '.join(ignores))
					await writer.drain()
					continue

				if subcmd.startswith('ignore '):
					mask = subcmd[7:].strip()
					if mask.startswith('+'):
						mask = mask[1:].strip()
						if mask and mask not in ignores:
							ignores.append(mask)
							save_ignores(ignores)
							privmsg(f'+ {mask}')
						elif mask in ignores:
							privmsg(f'{mask} already ignored')
					elif mask.startswith('-'):
						mask = mask[1:].strip()
						if mask in ignores:
							ignores.remove(mask)
							save_ignores(ignores)
							privmsg(f'- {mask}')
						else:
							privmsg(f'{mask} not in ignore list')
					else:
						privmsg('usage: @radio ignore [+/-]nick!user@host')
					await writer.drain()
					continue

				if subcmd.startswith('download '):
					args_str = subcmd[9:].strip()
					tokens = parse_quoted_args(args_str)
					flat = [quoted or unquoted for quoted, unquoted in tokens]

					if len(flat) < 4:
						privmsg('usage: @radio download <url> "<band>" "<song>" "<genre>"')
						await writer.drain()
						continue

					yt_url, band, song, genre = flat[0], flat[1], flat[2], flat[3]

					vid = YouTubeMP3.extract_video_id(yt_url)
					if not vid:
						privmsg(f'❌ cannot parse video ID from: {yt_url}')
						await writer.drain()
						continue

					privmsg(f'⏳ downloading \x02{band}\x02 - {song} [{genre}]...')
					await writer.drain()

					try:
						result = await downloader.download(yt_url, band, song, genre)
					except Exception as e:
						result = {'ok': False, 'msg': str(e)}

					if result['ok']:
						size_str = YouTubeMP3._human_size(result['size'])
						dur_str = YouTubeMP3._human_duration(result['duration'])
						privmsg(f'✅ \x02{band}\x02 - {song} [{genre}] ({size_str}, {dur_str})')
					else:
						privmsg(f'❌ {result["msg"]}')
					await writer.drain()
					continue

			if cmd == '@radio':
				if now - last_cmd < COOLDOWN:
					continue
				last_cmd = now
				privmsg(f'🎵 {RADIO_URL}')
				await writer.drain()
				continue

			if cmd not in ('!np', '!like', '!dislike'):
				continue

			if now - last_cmd < COOLDOWN:
				continue
			last_cmd = now

			if cmd == '!np':
				np = get_now_playing()
				if np:
					votes = get_votes(np['song_key'])
					listeners = get_listener_count()
					privmsg(format_np(np, votes, listeners))
				else:
					privmsg('nothing playing right now')
				await writer.drain()

			elif cmd == '!like':
				if not votes_enabled:
					privmsg('voting is currently disabled')
					await writer.drain()
					continue
				np = get_now_playing()
				if not np:
					privmsg('nothing playing right now')
				else:
					result = cast_vote(np['song_key'], 'up')
					if result:
						privmsg(f'👍 {irc_color(str(result["up"]), 3)} 👎 {irc_color(str(result["down"]), 4)}')
					else:
						privmsg('❌ failed to cast vote')
				await writer.drain()

			elif cmd == '!dislike':
				if not votes_enabled:
					privmsg('voting is currently disabled')
					await writer.drain()
					continue
				np = get_now_playing()
				if not np:
					privmsg('nothing playing right now')
				else:
					result = cast_vote(np['song_key'], 'down')
					if result:
						privmsg(f'👍 {irc_color(str(result["up"]), 3)} 👎 {irc_color(str(result["down"]), 4)}')
					else:
						privmsg('❌ failed to cast vote')
				await writer.drain()

		if joined and time.time() - last_announce >= ANNOUNCE_INTERVAL:
			np = get_now_playing()
			if np:
				v = get_votes(np['song_key'])
				listeners = get_listener_count()
				privmsg(format_announce(np, v, listeners))
				await writer.drain()
			last_announce = time.time()



if __name__ == '__main__':
	asyncio.run(main())
