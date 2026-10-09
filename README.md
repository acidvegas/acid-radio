# ACID RADIO

A self-hosted internet radio station with a web UI, vote system, and IRC bot. Built with pure Python and vanilla JavaScript — no frameworks, no build steps, no npm. Drop your music in a folder and go.

## Setup

The server requires Python 3, [mutagen](https://pypi.org/project/mutagen/) for reading tags, and **ffmpeg** *(both `ffmpeg` and `ffprobe`)* — ffmpeg encodes the live stream, so it is a hard requirement, not just a helper for reading durations. Install the dependency and run:

```
pip install mutagen
python3 server.py
```

The server listens on port 7000 by default. Place your music in a `music/` directory next to `server.py`, organized as `music/Artist Name/track.mp3`. Supported formats are mp3, flac, ogg, wav, m4a, opus, and aac.

Running with `-d` or `--debug` reveals the admin controls in the web UI *(skip, genre jump, artist jump)*. Note that the buttons are only the UI half — the endpoints behind them are restricted by network location regardless of this flag, see [Admin Controls](#admin-controls).

```
python3 server.py --debug
```

## How It Works

The server decodes one track at a time to raw PCM and pipes it into a long-running ffmpeg process that encodes a single continuous **HLS live stream** *(AAC, 128kbps, 4-second segments)*. Every listener pulls the same segments, so everyone hears the same song at the same position — there are no individual streams or playlists.

Track boundaries are published separately from the audio. The server keeps a schedule of upcoming tracks, each stamped with the wall-clock time at which it enters the stream, derived from the exact count of PCM bytes fed to the encoder. The client reads the `EXT-X-PROGRAM-DATE-TIME` of whatever segment it is currently playing and matches it against that schedule. This means the displayed artist and track stay correct no matter how far behind the live edge a listener is buffered.

The playlist advertises a 120-second window *(30 segments)*, which is what makes deep client-side buffering possible.

### Smart Shuffle

The shuffle system maintains a rolling history of the last 20 artists played. When selecting the next song, it filters out any artist that appears in this history, ensuring broad coverage across your library before repeating an artist. If you have fewer than 20 artists *(or all artists are in the recent history)*, it falls back to the full pool so playback never stalls.

The music library is rescanned every 5 minutes, so you can add or remove files on the fly without restarting the server. Tags and durations are cached against each file's modification time, so a rescan only touches files that actually changed. If a selected track no longer exists on disk, the server silently skips it and picks another.

### Voting

Listeners can thumbs-up or thumbs-down the current track. Votes are persisted to a SQLite database so they survive restarts. Each listener gets a unique client ID stored in their browser, and can only cast one vote per song *(toggling it off if they click again)*.

### Vote to Skip

The skip button lets listeners collectively vote to skip a song. The system uses probability scaling — the more people who vote, the higher the chance it actually skips:

| Skip Votes | Chance |
|:----------:|:------:|
| 0 | 1 in 10 |
| 1 | 1 in 9 |
| 2 | 1 in 8 |
| 3 | 1 in 7 |
| 4 | 1 in 6 |
| 5 | 1 in 5 |
| 6 | 1 in 4 |
| 7 | 1 in 3 |
| 8 | 1 in 2 |
| 9+ | guaranteed |

A global limit of 3 successful skips per hour prevents abuse. When the limit is reached, the skip button hides entirely until the cooldown passes. Each listener can only vote to skip once per song.

Because the cut happens at the encoder — which is well ahead of what anyone is actually hearing — a successful skip takes effect roughly 20 to 60 seconds later depending on how deeply buffered you are. The UI shows a `SKIPPING...` indicator in the meantime so the delay doesn't read as a failure.

### Synced Lyrics

Drop an `.lrc` file next to a track — same name, `.lrc` extension — and the player shows karaoke-style scrolling lyrics with the current line highlighted. Tracks without one simply don't show the panel.

Standard timestamped LRC is expected:

```
[ar:Coalesce]
[ti:The Plot Against My Love]
[00:02.70] I had to cut them off.
[00:06.95] They had me bought and sold.
```

The server parses the file into `{"t": seconds, "text": ...}` and caches it against the file's mtime. Lines carrying several timestamps *(repeated choruses)* expand into one entry per timestamp, and untimestamped metadata tags are ignored.

Sync is driven by the same clock as the progress bar — the position derived from the stream's `EXT-X-PROGRAM-DATE-TIME` — so lyrics stay aligned no matter how far behind the live edge a listener is buffered. Lyrics are hidden in car mode.

### Car Mode

The car button trades latency for resilience, for listening somewhere with patchy signal. It hides the visual chrome, stops the background videos from being downloaded and decoded at all, and reconfigures the player to sit about 40 seconds behind the live edge instead of 12.

That distance *is* the buffer: segments ahead of the live edge do not exist yet, so the only way to hold more audio in reserve is to play further back. Car mode therefore rides out roughly 40 seconds of dead zone where the default sits at about 12. The setting persists in `localStorage`.

The player also backs off exponentially on network errors and stops retrying entirely while the device reports itself offline, rather than hammering a dead connection.

### Listener Count

The server tracks active listeners by session ID. Each browser tab generates a unique session on load, and the server prunes any session that hasn't polled in 15 seconds. The count is displayed on both the splash page and the player, updating every 10 seconds. No IP addresses or identifying information are ever exposed to clients — the API only returns a number.

### Volume Boost

The volume slider goes up to 200%. Values above 100% use a Web Audio API GainNode to amplify beyond the browser's native limit. This is initialized on the first "Tune In" click to satisfy browser autoplay policies.

## API Endpoints

All endpoints return JSON unless otherwise noted.

### GET

| Endpoint | Description |
|:---------|:------------|
| `/api/radio/now?sid=` | The track schedule — a list of entries, each with `artist`, `track`, `genre`, `folder`, `file`, `duration` and `pdt` *(the wall-clock time that track enters the stream)*, plus `server_time`. The `sid` parameter registers the session as an active listener. |
| `/api/radio/listeners` | Active listener count. Returns `{"count": N}`. |
| `/api/radio/votes?song=&client=` | Vote counts and the client's current vote for a song. |
| `/api/radio/lyrics?song=` | Timestamped lyrics for a track as `{"lines": [{"t", "text"}]}`, or an empty list when it has no `.lrc`. `song` is `folder/file.mp3`. |
| `/api/radio/skip-info?ts=&client=` | Skip vote count, whether the client has voted, and remaining hourly skips. `ts` is the track's `pdt`. |
| `/api/debug` | Returns `{"debug": bool, "admin": bool}` — whether debug mode is on, and whether *this* caller is allowed to use the admin endpoints. The UI shows the admin buttons only when both are true. |
| `/stream/playlist.m3u8` | The HLS media playlist. `/stream/segN.ts` serves the segments. |

### Admin Controls

These three are restricted to the loopback address, private networks *(10/8, 172.16/12, 192.168/16)* and the Tailscale CGNAT range *(100.64/10)*. Requests arriving from a public address get a 403 regardless of whether `--debug` is set — the flag only controls whether the buttons are visible.

The check reads `X-Real-IP`, which nginx overwrites with the true peer address, so it cannot be forged by a client. A request with no such header is treated as local, which is only reachable by a process on the host itself since the port binds to `127.0.0.1`.

| Endpoint | Description |
|:---------|:------------|
| `/api/radio/skip` | Force skip to next song. |
| `/api/radio/skip-to?artist=` | Skip to a random song by the given artist *(matched on folder name)*. |
| `/api/radio/skip-to-genre?genre=` | Skip to a random song whose genre tag contains the query. |

### POST

| Endpoint | Body | Description |
|:---------|:-----|:------------|
| `/api/radio/vote` | `{"song", "client", "vote"}` | Cast a vote. `vote` is `"up"`, `"down"`, or `null` *(to remove)*. |
| `/api/radio/vote-skip` | `{"ts", "client"}` | Vote to skip the current song. `ts` is the track's `pdt` from the schedule. |

## IRC Bot

The bot *(radiobot.py)* connects to IRC over SSL and announces what's playing. It uses pure asyncio, plus [python-dotenv](https://pypi.org/project/python-dotenv/) for the NickServ password and `y2mp3.py` for downloads.

```
python3 radiobot.py
```

It connects to `irc.supernets.org` on port 6697 *(SSL)*, joins `#superbowl` 6 seconds after registration, and sits quietly until someone uses a command or the announcement timer fires. If the connection drops or goes silent for 5 minutes, it reconnects with exponential backoff *(5s up to 5min)* rather than exiting.

| Command | Description |
|:--------|:------------|
| `!np` | Now playing — artist, track, genre, vote counts, and listener count. |
| `!like` | Vote up the current song. |
| `!dislike` | Vote down the current song. |
| `@radio` | Show the help banner. |

Admin-only *(matched on a hardcoded nick!user@host mask)*:

| Command | Description |
|:--------|:------------|
| `@radio download <url> "<band>" "<song>" "<genre>"` | Download a YouTube URL as a tagged mp3 into `music/Downloads`. |
| `@radio ignore` | List ignored masks. |
| `@radio ignore [+/-]<mask>` | Add or remove an ignore *(fnmatch wildcards)*. |
| `@radio togglevotes` | Enable or disable `!like` / `!dislike`. |

Every 4 hours, the bot announces the currently playing song with the listener count and a tune-in link. The timer runs independently of channel traffic.

## Reverse Proxy

If you're running behind nginx with SSL *(recommended)*, point your domain at the server:

```nginx
server {
    listen 443 ssl;
    server_name radio.acid.vegas;

    ssl_certificate /etc/letsencrypt/live/radio.acid.vegas/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/radio.acid.vegas/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:7000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
        proxy_request_buffering off;
    }
}
```

Generate a cert with `sudo certbot certonly --standalone -d radio.acid.vegas`.

---

###### Mirrors: [SuperNETs](https://git.supernets.org/acidvegas/) • [GitHub](https://github.com/acidvegas/) • [GitLab](https://gitlab.com/acidvegas/) • [Codeberg](https://codeberg.org/acidvegas/)
