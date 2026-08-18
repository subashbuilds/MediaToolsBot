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
