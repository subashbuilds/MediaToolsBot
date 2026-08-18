# Media Tools Bot — Telegram MTProto edition

This build intentionally uses **Telegram MTProto**, not the HTTP Bot API and not the Local Bot API Server.

Telegram officially supports bot authorization over MTProto with `API_ID`, `API_HASH` and `BOT_TOKEN`. Telethon 1.44.0 is used for that transport.

## Features

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
MAX_CONCURRENT_JOBS=2
PROGRESS_INTERVAL=3
SUDO_USERS=
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

Configuration:

```env
PUBLIC_BASE_URL=
WEB_PORT=8080
DIRECT_LINK_TTL=86400
```

The bot automatically detects `RENDER_EXTERNAL_URL`, `RAILWAY_PUBLIC_DOMAIN`, `APP_URL`, or a Heroku app name when those hosting variables are present. For a VPS/custom reverse proxy, set `PUBLIC_BASE_URL` to the public HTTPS origin that reaches this service. There is no safe way to invent a public URL when the host does not provide one.

After pressing **Make Direct/Stream Link**, the bot creates a signed URL such as `https://your-domain/f/<token>/<filename>`. The token expires after `DIRECT_LINK_TTL` seconds. The HTTP endpoint supports browser playback/download and Range requests.

## Media Information

Media Information creates a formatted Telegra.ph page with file size, format, duration, bitrate and per-stream codec/language/title/resolution/FPS/channels/sample-rate/bitrate/default/forced information, then sends the page link in Telegram. The Telegraph API documents `createAccount` and `createPage` for this workflow.

## URL input and stream source preservation

Any plain `http://` or `https://` URL sent to the bot is now treated as a media URL automatically, even when Telegram attaches a WebPage preview. It is downloaded with progress and then opens the same function menu as a Telegram file. The URL uploader menu and `/urlupload` command remain available as explicit alternatives.

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
