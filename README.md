# 🎬 Media Tools Bot

A production-oriented Telegram media processing bot built on **Telegram MTProto (Telethon)**, **FFmpeg/FFprobe**, and **GoFile**.

The bot is designed for VPS/Docker deployment and treats Telegram media and HTTP/HTTPS URLs as first-class inputs. Processing, upload, cancellation, cleanup, archive extraction, stream manipulation, and progress reporting share the same stateful workflow.

> **Transport note:** this project uses Telegram's MTProto API through Telethon. `BOT_TOKEN` is used only to authorize the bot account over MTProto; the HTTP Bot API file-transfer endpoint is not used.

---

## ✨ Feature overview

### 📥 Input

- Telegram documents, videos, audio and other media
- HTTP/HTTPS direct file URLs
- URL input uses the same processing workflow as Telegram input
- URL/file uploader can upload the current file to Telegram or GoFile
- Download progress with speed and ETA

### 🎥 Video

- Media Information → Telegraph
- Stream Remover
- Stream Extractor
- Individual stream selection
- All audio / all subtitle selection
- Custom stream selection
- Video trimmer
- Remove audio
- Lossless optimize/remux
- Video splitter
- Screenshots: 1–20
- Manual screenshots: 1–20 timestamps
- Low-overhead sample generation using stream copy
- Video → Audio
- Video → MP4
- Video → MKV
- Thumbnail extraction
- Merge Tracks

### 🎵 Audio

- Audio converter
- 8D
- Equalizer
- Bass boost
- Treble boost
- Audio trimmer
- Auto trim
- Speed change
- Volume change
- Compression

### 🔀 Merge Tracks

Merge means **container/stream muxing**, not timeline concatenation.

Examples:

```text
Video + Audio
Video + Video
Video + Audio + Audio
Video + Subtitle
Video + Video + Audio + Subtitle
```

The current media is automatically the first merge input. Users send additional Telegram files or HTTP/HTTPS URLs and the live queue count increases.

The merge UI contains only:

```text
Files queued: N

1. file-a.mkv — 2.35 GiB
2. audio.ac3 — 166.93 MiB

✅ Finish Merge
❌ Cancel Merge
⬅️ Back
```

Inputs are deduplicated before FFmpeg starts, preventing accidental double-muxing.

### 📦 Archives

Supported by the installed extractor stack:

- ZIP
- TAR / TAR.GZ / TGZ / TAR.BZ2 / TAR.XZ
- 7z
- RAR where the installed 7-Zip/unar build supports it
- Multi-part archive sets such as `.part1.rar`, `.part01.rar`, `.7z.001`, `.zip.001`, and common numbered parts
- Password-protected archives
- Safe path traversal checks for ZIP/TAR

Workflow:

```text
Download archive
      ↓
📦 Extract Archive
      ↓
📦 Normal Extract   OR   🧩 Multi-Part Extract
      ↓
Password prompt when required
      ↓
Choose Telegram / GoFile
```

For multi-part extraction, send the remaining parts as Telegram files or URLs. The bot validates that they belong to the same archive series.

For GoFile uploads, extracted directory structure is recreated using GoFile folders.

### ☁️ GoFile

- API-token uploads
- Guest uploads when no token is configured
- Per-user reusable GoFile destination folder
- Multiple files share one destination folder
- Extracted archive trees preserve directory structure
- Final result contains only useful file/folder links
- Upload progress with speed and ETA

GoFile API documentation: <https://gofile.io/api>

### 📤 Telegram uploads

Telegram uploads use Telethon/MTProto.

Two modes are available in Settings:

- **Document** — default
- **Media** — Telegram chooses the appropriate media presentation

Files larger than **1.95 GiB** are split into numbered chunks before upload. Each chunk is uploaded as a document so arbitrary large files can be transferred without relying on the HTTP Bot API file limit.

### 🖼️ Custom thumbnail

Users can permanently store one custom thumbnail.

```text
Settings
  ↓
🖼️ Set Custom Thumbnail
  ↓
Send image
  ↓
Stored in user_data/<telegram-id>/
```

The thumbnail is normalized to JPEG and stored separately from temporary job files, so normal cleanup does not remove it.

### ✏️ Rename before upload

When enabled, the destination is selected **first** and the rename prompt appears exactly once:

```text
📤 Upload destination
        ↓
✏️ Rename before upload?
   ┌──────────────┐
   │ Rename │ Skip │
   └──────────────┘
        ↓
Upload
```

This avoids the previous double `Rename / Skip` prompt.

### 🔗 Direct / Stream Link

The bot includes a small HTTP server for locally stored files.

- Browser playback
- Download
- HTTP Range requests / seeking
- Random signed tokens
- Default lifetime: 24 hours
- Public URL can be supplied explicitly or detected from common Railway/Render environment variables

Example:

```text
https://your-domain.example/f/<token>/<filename>
```

Set `PUBLIC_BASE_URL` when the platform does not expose a detectable public domain.

### 📋 Media Information

The Telegram response is intentionally compact:

```text
📋 Movie.mkv

🔗 Open detailed Media Information
```

The Telegraph page is structured by container and individual streams and includes:

- File size
- Container/format
- Duration
- Overall bitrate
- Stream count
- Codec
- Codec name
- Profile
- Language
- Title
- Resolution
- Aspect ratio
- Pixel format
- Bit depth
- Frame rate
- Color information
- Channels
- Channel layout
- Sample rate
- Individual stream bitrate
- Stream duration
- Packet payload size
- Default/forced/original flags

Telegraph API documentation: <https://telegra.ph/api>

### ❤️ Message reactions

Every incoming private user message is optionally reacted to using Telegram's real MTProto `messages.sendReaction` method.

The implementation uses normal emoji reactions such as:

```text
👍  🔥  🎉  ❤️
```

The supplied `message_effect_id` examples are not used for this feature because outgoing message effects and reactions on an existing message are different Telegram mechanisms.

Telegram reactions documentation: <https://core.telegram.org/api/reactions>

---

## 🧭 UI behavior

### `/start`

`/start` is a dashboard, not the media-processing menu.

It shows:

- Bot status
- MTProto transport status
- FFmpeg status
- FFprobe status
- Current user's process state
- Global concurrency limit
- Help
- Settings
- System statistics
- Sudo-only administration

When a button is pressed, the same dashboard message is edited rather than sending a new message.

### Media menu

When new media is downloaded, the old processing menu is removed and a **new menu is created as the latest message**. The dashboard remains separate.

Inside a function menu, navigation edits the existing function message.

### Cancellation

A user's Cancel action:

1. Sets the cancellation event.
2. Interrupts registered FFmpeg/7-Zip subprocesses.
3. Cancels network waits where possible.
4. Removes the progress message.
5. Deletes temporary server files immediately.
6. Preserves files that are protected by an active direct link.
7. Clears the pending workflow.
8. Removes operation menus.
9. Returns the user to the dashboard.

A cancelled job does not leave a stale `Cancel Process` message behind.

---

## 👥 Concurrency and cleanup

Default limits:

```text
Normal user: 1 active process
Global:      10 active processes
Idle session: 6 hours
Direct link: 24 hours
```

Temporary files are removed after successful completion, cancellation, or failed processing. A file referenced by a valid direct link is retained until its link expires.

A background direct-link cleanup loop also runs so expired links are cleaned after a bot restart.

---

## 🛡️ Sudo administration

Set:

```env
SUDO_USERS=123456789,987654321
```

Only sudo users see **Admin Controls**.

Available controls:

- Total registered users
- Ongoing processes
- Cancel another user's job
- Broadcast a message

Commands:

```text
/canceluser 123456789
/canceluser @username
/broadcast Hello everyone
```

New users are reported to every sudo account with their Telegram ID and username.

---

## ⚙️ Commands

```text
/start
/help
/settings
/menu
/status
/cancel
/upload
/upload telegram
/upload gofile
/urlupload
/direct
/merge
/rename on
/rename off
/uploadmode telegram
/uploadmode gofile
/uploadmode choose
/setgofile YOUR_TOKEN
/cleargofile
```

---

## 🔐 Environment variables

Copy `.env.example` to `.env`.

| Variable | Required | Default | Description |
|---|---:|---|---|
| `API_ID` | Yes | — | Telegram API ID |
| `API_HASH` | Yes | — | Telegram API hash |
| `BOT_TOKEN` | Yes | — | Bot-account authorization token used by Telethon over MTProto |
| `SUDO_USERS` | No | — | Comma-separated Telegram IDs |
| `GOFILE_API_TOKEN` | No | — | Global GoFile token; blank = guest |
| `DOWNLOAD_DIR` | No | `/data/downloads` | Temporary downloads |
| `WORK_DIR` | No | `/data/work` | FFmpeg/work files |
| `DB_PATH` | No | `/data/bot.sqlite3` | SQLite database |
| `MAX_CONCURRENT_JOBS` | No | `10` | Global process limit |
| `PROGRESS_INTERVAL` | No | `3` | Progress update interval in seconds |
| `SESSION_TIMEOUT` | No | `21600` | Idle session timeout |
| `PUBLIC_BASE_URL` | No | auto | Public direct-link origin |
| `WEB_HOST` | No | `0.0.0.0` | Direct-link server bind address |
| `WEB_PORT` | No | `8080` | Direct-link server port |
| `DIRECT_LINK_TTL` | No | `86400` | Direct-link lifetime |
| `TELEGRAPH_ACCESS_TOKEN` | No | auto account | Optional reusable Telegraph token |

---

## 🐳 Docker deployment

### Docker Compose

```bash
git clone <your-repository>
cd media-tools-bot
cp .env.example .env
nano .env
docker compose up -d --build
```

Logs:

```bash
docker compose logs -f media-tools-bot
```

Stop:

```bash
docker compose down
```

The project installs:

- FFmpeg
- FFprobe
- p7zip
- unar
- Python dependencies

Persistent data is stored under:

```text
./data/
```

---

## 🖥️ VPS installation without Docker

Ubuntu/Debian example:

```bash
sudo apt update
sudo apt install -y ffmpeg p7zip-full unar python3 python3-venv

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
nano .env

python -m app
```

For a production VPS, run it under `systemd`, `supervisord`, or another process supervisor.

---

## 🌐 Railway / Render direct links

Expose the configured HTTP port and set:

```env
PUBLIC_BASE_URL=https://your-public-domain.example
WEB_PORT=8080
```

Railway and Render public domains are auto-detected when their standard environment variables are available, but an explicit `PUBLIC_BASE_URL` is the safest configuration.

---

## 🧪 Testing

Install development requirements:

```bash
pip install -r requirements-dev.txt
```

Run all tests:

```bash
pytest -q
```

The test suite covers:

- FFmpeg/FFprobe media processing
- Stream muxing
- Sample generation
- Archive extraction and traversal protection
- Multi-part archive naming
- Telegram chunk sizing
- UI callback contracts
- Settings persistence
- GoFile payload/folder contracts
- Cancellation/process control
- Direct-link behavior
- MTProto transport contract
- URL workflows
- Timeout cleanup
- Sudo administration

---

## 🏗️ Project structure

```text
.
├── app/
│   ├── main.py
│   ├── config.py
│   ├── services/
│   │   ├── archive.py
│   │   ├── downloader.py
│   │   ├── ffmpeg.py
│   │   ├── ffprobe.py
│   │   ├── gofile.py
│   │   ├── merge.py
│   │   ├── process_control.py
│   │   ├── system.py
│   │   ├── telegram_upload.py
│   │   └── telegraph.py
│   ├── storage/
│   │   └── db.py
│   ├── ui/
│   │   └── keyboards.py
│   └── utils/
├── tests/
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── requirements-dev.txt
└── .env.example
```

---

## 📚 Upstream documentation used for implementation

- Telethon documentation: <https://docs.telethon.dev/en/stable/>
- Telegram `messages.sendReaction`: <https://core.telegram.org/method/messages.sendReaction>
- Telegram reactions API: <https://core.telegram.org/api/reactions>
- GoFile API: <https://gofile.io/api>
- Telegraph API: <https://telegra.ph/api>

The implementation intentionally follows MTProto for Telegram media transfer rather than the HTTP Bot API file-download/upload path.

---

## 📄 License

Add the license appropriate for your deployment/repository before publishing the project publicly.
