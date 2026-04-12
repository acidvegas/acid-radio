# ACID RADIO

A self-hosted internet radio station with a web UI, vote system, and IRC bot. Built with pure Python and vanilla JavaScript — no frameworks, no build steps, no npm. Drop your music in a folder and go.

## Setup

The server requires Python 3, [mutagen](https://pypi.org/project/mutagen/) for reading genre tags, and `ffprobe` *(part of ffmpeg)* for determining track duration. Install the dependency and run:

```
pip install mutagen
python3 server.py
```

The server listens on port 7000 by default. Place your music in a `music/` directory next to `server.py`, organized as `music/Artist Name/track.mp3`. Supported formats are mp3, flac, ogg, wav, m4a, opus, and aac.

Running with `-d` or `--debug` enables admin controls in the web UI *(skip, genre jump, artist jump)* that are otherwise hidden from listeners.

```
python3 server.py --debug
```

## How It Works

The server picks a random song, streams it to all connected listeners simultaneously, and advances to the next track when the current one ends. Everyone hears the same song at the same position — there are no individual streams or playlists. The web client syncs its playback position against the server clock on each poll so late joiners pick up mid-song.

### Smart Shuffle

The shuffle system maintains a rolling history of the last 20 artists played. When selecting the next song, it filters out any artist that appears in this history, ensuring broad coverage across your library before repeating an artist. If you have fewer than 20 artists *(or all artists are in the recent history)*, it falls back to the full pool so playback never stalls.

The music library is rescanned every 5 minutes, so you can add or remove files on the fly without restarting the server. If a selected track no longer exists on disk, the server silently skips it and picks another.

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

### Listener Count

The server tracks active listeners by session ID. Each browser tab generates a unique session on load, and the server prunes any session that hasn't polled in 15 seconds. The count is displayed on both the splash page and the player, updating every 10 seconds. No IP addresses or identifying information are ever exposed to clients — the API only returns a number.

### Volume Boost

The volume slider goes up to 200%. Values above 100% use a Web Audio API GainNode to amplify beyond the browser's native limit. This is initialized on the first "Tune In" click to satisfy browser autoplay policies.

## API Endpoints

All endpoints return JSON unless otherwise noted.

### GET

| Endpoint | Description |
|:---------|:------------|
| `/api/radio/now?sid=` | Current song state *(artist, track, genre, duration, timestamps)*. The `sid` parameter registers the session as an active listener. |
| `/api/radio/listeners` | Active listener count. Returns `{"count": N}`. |
| `/api/radio/votes?song=&client=` | Vote counts and the client's current vote for a song. |
| `/api/radio/skip-info?ts=&client=` | Skip vote count, whether the client has voted, and remaining hourly skips. |
| `/api/radio/skip` | Force skip to next song. *Debug mode only.* |
| `/api/radio/skip-to?artist=` | Skip to a random song by the given artist. *Debug mode only.* |
| `/api/radio/skip-to-genre?genre=` | Skip to a random song matching the genre. *Debug mode only.* |
| `/api/artists` | List of all artists with track counts. |
| `/api/tracks?artist=` | List of tracks for a given artist. |
| `/api/debug` | Returns `{"debug": true/false}` indicating if debug mode is active. |
| `/music/Artist/track.mp3` | Streams an audio file with range request support. |

### POST

| Endpoint | Body | Description |
|:---------|:-----|:------------|
| `/api/radio/vote` | `{"song", "client", "vote"}` | Cast a vote. `vote` is `"up"`, `"down"`, or `null` *(to remove)*. |
| `/api/radio/vote-skip` | `{"ts", "client"}` | Vote to skip the current song. `ts` is the song's `started_at` timestamp. |

## IRC Bot

The bot *(radiobot.py)* connects to IRC over SSL and announces what's playing. It uses pure asyncio with no external dependencies.

```
python3 radiobot.py
```

It connects to `irc.supernets.org` on port 6697 *(SSL)*, joins `#superbowl` 6 seconds after registration, and sits quietly until someone uses a command or the announcement timer fires.

| Command | Description |
|:--------|:------------|
| `!np` | Shows the current artist, track, genre, and listener count. |
| `@radio` | Links to the radio URL. |

Every 4 hours, the bot automatically announces the currently playing song with the listener count and a tune-in link.

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
