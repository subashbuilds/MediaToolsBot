# Video Converter Bot

A VPS/Docker-friendly Telegram media toolbox inspired by the supplied UI screenshots.

## Stack

- Python 3.13
- aiogram 3.30.0 / Telegram Bot API 10.2
- FFmpeg + FFprobe
- GoFile API
- yt-dlp for media metadata/thumbnail extraction
- SQLite for per-user settings

## Important Telegram size limitation

The normal Telegram Bot API currently limits bot uploads to 50 MB and downloads through the normal API are limited. The official Local Bot API Server removes the download size limit and allows uploads up to 2000 MB. This project supports the Local Bot API Server through `TELEGRAM_API_BASE`.

For files larger than Telegram's 2 GB bot upload limit, use GoFile (guest or authenticated).

## Setup

1. Copy `.env.example` to `.env` and set `BOT_TOKEN`.
2. For large Telegram files, obtain Telegram API ID/hash from my.telegram.org and set `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` in `.env`.
3. Run:

```bash
docker compose up -d --build
```

4. If using the bundled Local Bot API server, set:

```env
USE_LOCAL_BOT_API=true
TELEGRAM_API_BASE=http://telegram-bot-api:8081
```

then restart the bot.

## Commands

- `/start` - main menu
- `/help` - help
- `/settings` - settings
- `/upload` - choose upload destination
- `/upload telegram` - set Telegram as upload destination
- `/upload gofile` - set GoFile as upload destination
- `/setgofile TOKEN` - save a personal GoFile API token
- `/cleargofile` - remove personal GoFile token; uploads become guest uploads
- `/cancel` - cancel current job

A plain HTTP/HTTPS URL is downloaded and then shown in the action menu.
A Telegram document/video/audio/photo can also be processed.

## Implemented operations

### Main
- Thumbnail Downloader
- Direct/Stream Link (GoFile upload + returned URL)
- Extract Archive (zip/rar/7z/tar variants)
- URL Uploader
- Link Short & Unshort
- Audio menu
- Video menu

### Video
- Media Information
- Stream Remover
- Stream Extractor
- Video Trimmer
- Remove Audio
- Video Optimize
- Video Splitter
- Screenshots
- Manual Shots
- Generate Sample
- Video to Audio
- Video to MP4
- Video to MKV

### Audio
- Audio Converter
- 8D Converter
- Music Equalizer
- Bass Booster
- Treble Booster
- Audio Trimmer
- Auto Trimmer
- Speed Change
- Volume Change
- Media Information
- Compress Audio

## Testing

The repository includes unit tests for keyboard generation, stream mapping, command parsing, safe filenames and progress formatting. Run:

```bash
pytest -q
```

The startup check requires FFmpeg/FFprobe only. ZIP/TAR extraction uses Python stdlib; RAR/7Z extraction uses `unar`/`7z` when available and reports a feature-specific error if an archive tool is missing. The Docker image installs `p7zip-full` and `unar`.

## Documentation verification used for this build

The implementation was checked against the current documentation available on 2026-08-18:

- aiogram 3.30.0 documentation / Bot API 10.2 support
- Telegram Bot API 10.2 file limits and Local Bot API Server behavior
- FFmpeg/FFprobe current documentation
- GoFile API documentation, including guest uploads and regional upload endpoints

The bot deliberately does not promise unlimited Telegram uploads: Telegram's Local Bot API Server currently supports bot uploads up to 2000 MB. GoFile is the destination to use for files beyond Telegram's bot limit.
