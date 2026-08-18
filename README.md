# Media Tools Bot — Telegram MTProto edition

This build intentionally uses **Telegram MTProto**, not the HTTP Bot API and not the Local Bot API Server.

Telegram officially supports bot authorization over MTProto with `API_ID`, `API_HASH` and `BOT_TOKEN`. Telethon 1.44.0 is used for that transport.

## Features

### Latest fixes

- `/start` now shows bot status/information instead of opening the media-function menu.
- Added **System Stats** for host OS, kernel, CPU/load, RAM, disk and uptime.
- Generate Sample now uses low-overhead FFmpeg stream-copy into MKV, avoiding the RAM-heavy H.264 re-encode that can cause SIGKILL/OOM on constrained hosts.
- Merge Tracks now creates a durable merge session seeded from the current media. The original first file is never lost when the current output changes. Each added Telegram/URL track is validated and counted before Finish Merge.
- Media Information now calculates exact packet payload bytes per stream on demand and reports detailed codec, profile, language, resolution, pixel format, bit depth, FPS, color, channels, sample rate, bitrate, flags and per-stream size.
- Cancellation clears pending merge sessions and removes the active process keyboard.
- Merge validation no longer duplicates queued inputs; a 2.35 GiB video + 166.93 MiB audio is muxed once rather than being accidentally duplicated.
- Functionality navigation edits the existing menu message and provides Back buttons.
- Sudo-only Ongoing Processes view shows all active user sessions/jobs.
- New-user notifications send Telegram ID and username to every configured sudo user on first contact.
- Per-user concurrency is fixed at one; global concurrency defaults to ten.
- Six-hour inactivity timeout removes the user's server-side files and unfinished workflow, with a timeout message explaining how to redo the task.
- Successful Telegram/GoFile uploads clean up local files; valid direct-link files are retained until their link expires.
- GoFile batch completion is written into the same progress message with individual filename, size and link.


- Screenshot-matched inline menu hierarchy
- Telegram MTProto media download with live progress bar
- Telegram MTProto upload with live progress bar
- GoFile upload with live progress bar
- GoFile authenticated or guest upload
- `/upload telegram` and `/upload gofile`
- `/setgofile TOKEN`, `/cleargofile`
- Dynamic FFprobe stream listing
- Stream removal / custom stream removal
- Stream extraction
- Media information
- Video trimming, optimization, splitting, screenshots, samples
- Video to audio / MP4 / MKV
- Audio conversion, 8D, equalizer, bass, treble, trim, auto trim, speed, volume, compression
- Thumbnail extraction
- ZIP/TAR extraction plus 7z/RAR support through installed system tools
- URL downloading with progress
- Link shortening/unshortening
- Cancellation checks during network transfers
- SQLite per-user settings
- Docker deployment

## Environment

Copy `.env.example` to `.env` and set:

```env
API_ID=123456
API_HASH=...
BOT_TOKEN=...
```

`API_ID` and `API_HASH` come from https://my.telegram.org. `BOT_TOKEN` comes from @BotFather.

Optional:

```env
GOFILE_API_TOKEN=
DOWNLOAD_DIR=/data/downloads
WORK_DIR=/data/work
DB_PATH=/data/bot.sqlite3
MAX_CONCURRENT_JOBS=10
PROGRESS_INTERVAL=3
SUDO_USERS=123456789
SESSION_TIMEOUT=21600
```

## Docker

```bash
docker compose up -d --build
```

The bot container contains FFmpeg/FFprobe and archive tools. There is **no `telegram-bot-api` service**.

## VPS without Docker

Install:

```bash
sudo apt update
sudo apt install -y ffmpeg p7zip-full unar python3 python3-venv
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env
python -m app
```

## Large files

MTProto removes the normal HTTP Bot API's 20 MB download / 50 MB upload limitations. Telegram still has its own MTProto-side file and account limits, so the bot does not claim that literally unlimited files can be uploaded to Telegram. GoFile is available as the alternate destination.

## Progress

Telegram download/upload callbacks are supplied directly to Telethon. The callback updates a Telegram status message every `PROGRESS_INTERVAL` seconds and checks the user's cancellation event. GoFile uses a streaming multipart payload that reports bytes written without loading the entire file into RAM.

## Tests

Install test dependencies with `pip install -r requirements-dev.txt`, then run `pytest -q`.

The repository includes offline tests for:

- FFprobe
- FFmpeg remuxing/conversion/trim/sample/screenshots/splitting/audio filters
- ZIP extraction and archive traversal protection
- streaming multipart upload payload and progress callback
- filename/progress helpers
- source compilation/import checks

A live Telegram test requires the deployer's own API ID, API hash and bot token, so those credentials are never included in the repository.

## Verification notes

This package is intentionally MTProto-only. The bot uses `TelegramClient(..., API_ID, API_HASH)` and `start(bot_token=BOT_TOKEN)`; it does not configure `api.telegram.org/bot...` or a Local Bot API server.

Telethon 1.44.0 documents asynchronous `progress_callback(current, total)` support for both `download_media()` and `send_file()`. `cryptg` is optional and is installed opportunistically in Docker for faster MTProto encryption/decryption; the bot remains installable without it.

The offline test suite validates media operations, progress rendering, streaming GoFile multipart upload, cancellation cleanup, archive safety, UI callback sizes, and Python compilation. A real Telegram MTProto transfer cannot be performed in an isolated build environment without the deployer's credentials and Telegram network access, so the package does not claim a live-transfer test that was not actually performed.


## Direct / Stream Links

The **Make Direct/Stream Link** feature is now served by the bot itself instead of relying on a GoFile direct-link field that may not be available for guest accounts. The HTTP endpoint uses `aiohttp.web.FileResponse`, so browsers can stream media and use HTTP Range requests.

Set: 

```env
PUBLIC_BASE_URL=https://your-public-domain.example
WEB_PORT=8080
DIRECT_LINK_TTL=86400
```

`PUBLIC_BASE_URL` must point to this bot's HTTP service. On a VPS, put Nginx/Caddy/Cloudflare in front of port 8080 if desired. On Railway, use the service's public domain and let `WEB_PORT` fall back to Railway's `PORT` when `WEB_PORT` is not set.

## Media Information

The exact per-stream payload calculation intentionally runs only when Media Information is requested because FFprobe must inspect packets; this can take longer on multi-gigabyte files but avoids slowing every normal media operation.


Media Information creates a formatted Telegra.ph page with file size, format, duration, bitrate and per-stream codec/language/title/resolution/FPS/channels/sample-rate/bitrate/default/forced information, then sends the page link in Telegram. The Telegraph API documents `createAccount` and `createPage` for this workflow.

## Stream source preservation

Extracting a subtitle/audio/video stream no longer replaces the source used by the Stream Remover/Extractor menus. This prevents a common failure where extracting an SRT caused later video operations to inspect only the SRT file. Video-only operations automatically fall back to the preserved source video.

## Merge Tracks

`🔀 Merge Tracks` accepts multiple Telegram media files and muxes all their streams into one MKV container without re-encoding. This supports combinations such as:

- video + video
- video + multiple audio tracks
- video + subtitles
- video + audio + subtitles

This is **track/container merging**, not timeline concatenation.

## Progress completion

Transfer progress messages are finalized into a plain completion message and the Cancel button is removed after a successful transfer. This prevents the old completed progress message from remaining actionable.

## Help

`/help` now provides a dedicated feature/command guide instead of opening the main function menu.

## Merge Tracks workflow

Merge is now a simple queue: after choosing **Merge Tracks**, the current media is immediately counted as file 1. Send additional Telegram media or HTTP(S) URLs directly; captioned Telegram audio/document messages are routed into the merge collector instead of being mistaken for text. The UI shows the live **Files queued** count and **Finish Merge**, **Cancel Merge**, and **Back** buttons. The queue is validated from a snapshot, so inputs are never duplicated during validation.

## GoFile folder reuse

GoFile uploads reuse one destination folder per user. The first upload without a configured token creates the guest account/folder and persists the returned guest token and parent folder. Subsequent files are uploaded with the same `folderId`, so split/archive batches are kept together instead of creating a new folder for every file. `/cleargofile` clears the saved token and folder and returns to guest mode on the next upload. This follows GoFile's current API documentation, which explicitly supports reusing `folderId` for subsequent uploads.

## Human-readable media information

Media Information displays duration as `HH:MM:SS`/`MM:SS`, file and per-stream payload sizes using B/KiB/MiB/GiB, and bitrates using bps/kb/s/Mb/s/Gb/s. The Telegraph detail page uses the same readable units while retaining detailed codec/stream metadata.
