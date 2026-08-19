# 🎬 Media Tools Bot

> A production-oriented Telegram media toolkit built around **Telegram MTProto + FFmpeg/FFprobe**, with large-file transfers, stream manipulation, screenshots, merging, GoFile uploads, direct browser links, per-user cleanup, and sudo administration.

[![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Telethon](https://img.shields.io/badge/Telegram-MTProto-26A5E4?logo=telegram&logoColor=white)](https://docs.telethon.dev/)
[![FFmpeg](https://img.shields.io/badge/FFmpeg-8.x%2B-007808?logo=ffmpeg&logoColor=white)](https://ffmpeg.org/)
[![Docker](https://img.shields.io/badge/Docker-ready-2496ED?logo=docker&logoColor=white)](https://www.docker.com/)

## ✨ What it does

Media Tools Bot accepts either a **Telegram media file** or an **HTTP/HTTPS URL**. Once media is available, the bot exposes the same processing workflow for both input types.

### 🎞️ Video

- Detailed Media Information → Telegraph page
- Stream Remover
- Stream Extractor
- Custom stream selection
- Video trimming
- Audio removal
- Lossless video optimization/remuxing
- Video splitting
- Screenshots: **1–20** images
- Manual screenshots: **1–20 timestamps** in one request
- Low-overhead sample generation
- Video → Audio
- Video → MP4
- Video → MKV
- Thumbnail extraction
- Merge Tracks

### 🎵 Audio

- Audio conversion
- 8D
- Equalizer
- Bass / treble boost
- Audio trimming
- Automatic trimming
- Speed change
- Volume change
- Compression

### 🔀 Merge Tracks

Merge is **stream/container muxing**, not timeline concatenation.

Examples:

```text
Video + Audio
Video + Video
Video + Audio + Audio
Video + Subtitle
Video + Video + Audio + Subtitle
```

The first media already in the user's session becomes **file 1 automatically**. Users simply send more media files or URLs. The UI shows the live queue count and only exposes **Finish Merge**, **Cancel Merge**, and **Back**.

### ☁️ Uploads

- Telegram upload through **Telethon MTProto**
- GoFile upload
- GoFile guest uploads when no API token is configured
- GoFile API-token uploads
- Multiple output files share one GoFile destination folder
- Per-file filename, human-readable size and GoFile link in the final result
- Upload progress with speed and ETA

### 🔗 Direct / Stream Links

The bot can serve a local media file through its own HTTP server.

- Browser playback
- Download
- HTTP Range requests / seeking
- Signed random token
- Configurable expiry
- Railway/Render public URL auto-detection

## 🧭 User workflow

### 1. `/start`

`/start` is an information dashboard, not the media-function menu.

It shows:

- Bot status
- MTProto transport
- FFmpeg / FFprobe status
- Current user's job state
- Global concurrency limit
- Settings
- Help
- System statistics
- Sudo-only admin controls

### 2. Send media

Send:

- Telegram document/video/audio, or
- HTTP/HTTPS URL

URL input is detected **before Telegram WebPage preview handling**, so a URL is downloaded as a URL instead of being passed to Telethon's Telegram-media downloader.

### 3. Process

The media-function menu is edited in place. Navigation uses one stable UI message wherever Telegram allows it.

### 4. Upload

If Rename File is enabled, the bot asks:

```text
Rename
Skip
```

`Skip` goes directly to the configured upload destination or destination selector.

## ⚙️ Settings

The settings screen displays the currently selected values:

- Rename File: Yes / No
- Upload Destination: Telegram / GoFile / Choose before upload

Commands:

```text
/rename on
/rename off
/uploadmode telegram
/uploadmode gofile
/uploadmode choose
```

## 📤 Upload commands

```text
/upload
/upload telegram
/upload gofile
```

GoFile token:

```text
/setgofile YOUR_API_TOKEN
/cleargofile
```

If no GoFile token exists, the uploader starts a guest upload. The returned guest token and destination folder are retained for subsequent files in the same user session/account model.

GoFile's current API documents `folderId` reuse for putting subsequent uploads into the same folder.

## 🛡️ Sudo administration

Set:

```env
SUDO_USERS=123456789,987654321
```

Only those Telegram IDs receive the admin controls.

### Ongoing Processes

The admin view intentionally stays simple:

```text
username — number of processes
```

Normal users never see this screen.

### Cancel User Job

Sudo users can cancel a user's active workflow by:

```text
/canceluser 123456789
/canceluser @username
```

The admin UI provides the same action.

### Broadcast

```text
/broadcast Your message here
```

or use the sudo-only Broadcast button and enter the message interactively.

New users are reported to sudo users with:

- Display name
- Username
- Telegram ID

## ⏱️ Concurrency and cleanup

Defaults:

```env
MAX_CONCURRENT_JOBS=10
```

- Maximum **1 active process per normal user**
- Maximum **10 active heavy jobs globally** by default
- Six-hour inactivity timeout
- Cancelled workflows do **not** delete the user's files immediately
- Completed Telegram/GoFile uploads clean local files
- Expired direct-link files are cleaned when their links expire
- Timed-out sessions remove server-side working files and reset the workflow

## 🧹 Cancellation behavior

Cancellation is intentionally stateful:

1. Stop the user's pending workflow.
2. Signal/cancel the active task.
3. Remove the active progress message.
4. Clear operation menus.
5. Return the existing UI to the Start dashboard.
6. Keep the user's files intact unless the session later expires or an upload has already completed.

This prevents stale `Cancel Process` keyboards from accumulating.

## 📋 Media Information

The Telegram response is intentionally compact:

```text
📋 filename.mkv

🔗 Open detailed Media Information
```

The linked Telegraph page contains the detailed report, including:

- Human-readable file size
- Human-readable duration
- Container / format
- Overall bitrate
- Stream count
- Individual stream sizes
- Codec and full codec name
- Codec profile
- Language
- Title
- Resolution
- Pixel format
- Bit depth
- FPS
- Color information
- Channels / channel layout
- Sample rate
- Stream bitrate
- Default / forced / original flags

The Telegraph API provides `createPage` for creating the detailed page.

## 🖼️ Screenshots

### Automatic Screenshots

Choose **Screenshots**, then enter a count from **1 to 20**.

The bot distributes the screenshots across the video's duration.

### Manual Shots

Choose **Manual Shots**, then send timestamps separated by commas:

```text
00:05:30, 00:12:10, 00:25:00
```

Maximum: **20 screenshots**.

## 🧰 Installation

### Prerequisites

- Linux VPS/server
- Python 3.12+
- FFmpeg + FFprobe
- Telegram API ID/hash
- Telegram bot token
- Optional GoFile API token

### Get Telegram credentials

1. Create an application at [my.telegram.org](https://my.telegram.org).
2. Obtain `API_ID` and `API_HASH`.
3. Create the bot with [@BotFather](https://t.me/BotFather).
4. Obtain `BOT_TOKEN`.

The application uses Telethon's MTProto client and starts the bot account with its bot token. It does **not** use `api.telegram.org/bot...` or the Local Bot API Server for file transfers.

Telethon documents `download_media()` and `send_file()` with asynchronous progress callbacks.Telethon documentation: https://docs.telethon.dev/en/stable/quick-references/client-reference.html

### Docker — recommended

```bash
git clone <your-repository>
cd media-tools-bot
cp .env.example .env
nano .env
docker compose up -d --build
```

Check logs:

```bash
docker compose logs -f
```

### VPS without Docker

Ubuntu/Debian example:

```bash
sudo apt update
sudo apt install -y ffmpeg python3 python3-venv p7zip-full unrar

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
nano .env

python -m app
```

## 🔐 Environment variables

```env
API_ID=123456
API_HASH=your_api_hash
BOT_TOKEN=123456:your_bot_token

# Leave empty for GoFile guest uploads.
GOFILE_API_TOKEN=

SUDO_USERS=123456789

DOWNLOAD_DIR=/data/downloads
WORK_DIR=/data/work
DB_PATH=/data/bot.sqlite3

MAX_CONCURRENT_JOBS=10
PROGRESS_INTERVAL=3
SESSION_TIMEOUT=21600

# Direct / Stream Link server
PUBLIC_BASE_URL=
WEB_HOST=0.0.0.0
WEB_PORT=8080
DIRECT_LINK_TTL=86400

# Optional Telegraph access token
TELEGRAPH_ACCESS_TOKEN=
```

### Direct-link public URL

For Railway/Render, the application can use the platform's standard public URL environment variables automatically.

For a VPS, configure:

```env
PUBLIC_BASE_URL=https://media.example.com
WEB_PORT=8080
```

The domain must route to the bot's HTTP service. A reverse proxy such as Caddy or Nginx can terminate HTTPS.

## 📦 Project structure

```text
app/
├── main.py                 # Telegram event routing + workflows
├── config.py               # Environment configuration
├── storage/
│   └── db.py               # SQLite state/settings
├── services/
│   ├── ffmpeg.py           # Media processing
│   ├── ffprobe.py          # Stream inspection
│   ├── merge.py            # Track muxing
│   ├── gofile.py            # GoFile upload
│   ├── telegraph.py         # Detailed media reports
│   ├── downloader.py       # HTTP/HTTPS downloads
│   ├── direct.py            # Direct-link helpers
│   ├── archive.py           # Archive extraction
│   └── process_control.py   # Cancellable FFmpeg/merge process registry
├── ui/
│   ├── keyboards.py        # Inline keyboards
│   └── text.py             # UI text
└── utils/
    ├── files.py
    └── progress.py
```

## 🧪 Testing

Install development dependencies:

```bash
pip install -r requirements-dev.txt
```

Run:

```bash
pytest -q
```

The test suite covers:

- Python compilation/import contracts
- FFmpeg/FFprobe media operations
- Stream-copy sample generation
- Merge regression cases
- URL routing
- Telegram MTProto transfer contracts
- GoFile multipart payloads and folder reuse
- Progress throttling
- Cancellation state
- UI callback routing
- Settings persistence
- Sudo controls
- Screenshot limits
- Rename/upload workflow
- Session timeout behavior
- Direct-link Range serving
- Archive safety

A real Telegram or GoFile production transfer requires the deployment's own credentials and external network access. Those credentials are never included in the repository.

## 🧠 Design principles

### No unnecessary re-encoding

Operations such as stream removal, optimization and merging use FFmpeg stream copy whenever possible.

Sample generation also uses stream copy into MKV instead of re-encoding a large HEVC source, reducing CPU/RAM pressure on VPS/Railway deployments.

### Large-file friendly

Transfers are streamed through Telethon/HTTP rather than loading entire files into Python memory.

### State isolation

Each Telegram user has an independent workflow state. Per-user concurrency is one, while a global semaphore protects the host from excessive simultaneous processing.

### Cleanup by lifecycle

Files remain available while the user is actively working. Completed uploads are cleaned immediately; abandoned sessions expire after six hours.

## ⚠️ Important limits

"Supports large files" does not mean an infinite Telegram limit. Telegram's current MTProto-side limits and the limits of the selected Telegram account still apply.

GoFile guest/standard storage is temporary; consult the current GoFile service documentation for retention and traffic limits.

## 📚 Current API references

- [Telegram Bot API](https://core.telegram.org/bots/api) — callback/message editing and deletion semantics.
- [aiogram documentation](https://docs.aiogram.dev/) — relevant Telegram Bot API method behavior.
- [Telethon documentation](https://docs.telethon.dev/) — MTProto client and transfer APIs.
- [GoFile API](https://gofile.io/api) — upload and `folderId` reuse.
- [Telegraph API](https://telegra.ph/api) — detailed report pages.
- [FFmpeg documentation](https://ffmpeg.org/documentation.html)

## 📄 License

Add the license appropriate for your distribution before publishing this repository publicly.
