# 🎬 Media Tools Bot

A production-oriented Telegram media processing bot built on **Telegram MTProto (Telethon)**, **FFmpeg/FFprobe**, and **GoFile**.

The bot is designed for VPS/Docker deployment and treats Telegram media and HTTP/HTTPS URLs as first-class inputs. Processing, upload, cancellation, cleanup, archive extraction, stream manipulation, and progress reporting share the same stateful workflow.

> **Transport note:** this project uses Telegram's MTProto API through Telethon. `BOT_TOKEN` is used only to authorize the bot account over MTProto; the HTTP Bot API file-transfer endpoint is not used.

---

## ✨ Feature overview

### 📥 Input

- Telegram documents, videos, audio, photos and other media
- HTTP/HTTPS direct file URLs
- A link pasted inside a longer message is detected too
- URL input uses the same processing workflow as Telegram input
- URL/file uploader can upload the current file to Telegram or GoFile
- The action menu appears immediately; downloads continue in the background
- Several files can be queued and downloaded in parallel (see Bulk mode)
- Download progress with speed and ETA
- Private/loopback addresses are refused so the bot cannot be used to reach internal services

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

Tracks appear as a numbered list showing the exact order they will be joined in.
Because files finish downloading out of order, you can reorder them before
merging: move a track up/down, to the top or bottom, or remove it.

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

The action menu appears **as soon as** a file or link arrives — the transfer itself runs in the background, so a slow download never blocks the user from choosing what to do next. Progress is rendered into that same menu, which means the chat does not fill up with one progress message per action.

Navigation always edits the message that is already on screen instead of sending a new one. When the tracked message can no longer be edited (deleted, or from before a restart) a fresh one is sent automatically.

### 📥 Bulk mode

Turn on **Settings → Bulk Mode** and the bot keeps asking for more input:

```text
📥 Bulk Mode

Queued: 3   Ready: 1   Downloading: 2

✅ Holiday.mp4 — 812.44 MiB
⬇️ Episode.mkv
    ██████████░░░░░  61.2%  512.00 MiB / 836.10 MiB  • ETA 12s
⏳ trailer.mp4 — queued

✅ Done Adding (1)
📤 Upload All
🗑️ Clear Queue
```

- Several files transfer **in parallel** while the user picks an action.
- **Done Adding** hands the collected files to the normal menu; `Upload` then sends all of them.
- **Upload All** picks a destination for the whole set in one step.
- The bulk prompt stays up until the user explicitly finishes, so a file is never silently replaced by the next one.

With bulk mode off the regular action menu is used, with a note about how many files are still downloading.

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
Global:      10 active operations
FFmpeg:      2 concurrent jobs (auto-clamped to container RAM)
Downloads:   3 files in parallel per user
Single file: 2048 MiB
Idle session: 6 hours
Direct link: 24 hours
```

FFmpeg and 7-Zip run under their own, smaller budget. A burst of users starting 1080p transcodes is the usual reason a small container gets OOM-killed, and a killed container takes every in-flight job with it.

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
/bulk on
/bulk off
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
| `SUDO_USERS` | No | — | Comma-separated Telegram IDs with elevated controls |
| `ALLOWED_USERS` | No | — | Optional whitelist. Empty = everyone allowed; when set, only these IDs plus `SUDO_USERS` may use the bot |
| `GOFILE_API_TOKEN` | No | — | Global GoFile token; blank = guest |
| `DOWNLOAD_DIR` | No | `/data/downloads` | Temporary downloads |
| `WORK_DIR` | No | `/data/work` | FFmpeg/work files |
| `DB_PATH` | No | `/data/bot.sqlite3` | SQLite database |
| `MAX_CONCURRENT_JOBS` | No | `10` | Global in-flight operation limit |
| `MAX_CONCURRENT_FFMPEG_JOBS` | No | `2` | Concurrent FFmpeg/7-Zip jobs. Auto-clamped to container RAM, because parallel transcodes are the usual cause of OOM kills |
| `MAX_PARALLEL_DOWNLOADS` | No | `3` | Files downloaded at the same time per user (max 8) |
| `MAX_DOWNLOAD_MB` | No | `0` (unlimited) | Largest single download accepted, in MiB. `0` means no cap. Telegram's 2 GiB ceiling is an **upload** limit and is applied only when sending to Telegram; downloads are unrestricted unless you set this. Disk safety comes from `MIN_FREE_BYTES` instead |
| `MAX_EXTRACT_BYTES` | No | `8589934592` | Largest expanded size accepted when extracting an archive (decompression-bomb guard) |
| `FFMPEG_TIMEOUT` | No | `14400` | Hard wall-clock limit for one FFmpeg job, in seconds |
| `MIN_FREE_BYTES` | No | `536870912` | Free disk space required before a download starts. Guards against filling the disk once downloads are uncapped |
| `FFPROBE_REMOTE_TIMEOUT` | No | `45` | Timeout for reading container metadata straight from a link, in seconds |
| `PROGRESS_INTERVAL` | No | `3` | Progress update interval in seconds |
| `SESSION_TIMEOUT` | No | `21600` | Idle session timeout |
| `BOT_REACTIONS` | No | `on` | Set to `off` to disable the automatic message reaction |
| `LOG_LEVEL` | No | `INFO` | Logging level |
| `ALLOW_PRIVATE_DOWNLOADS` | No | off | Set to `1` to allow downloads from private/loopback addresses. Leave unset in production: the bot refuses internal targets to prevent SSRF |
| `PUBLIC_BASE_URL` | No | auto | Public direct-link origin |
| `WEB_HOST` | No | `0.0.0.0` | Direct-link server bind address |
| `WEB_PORT` | No | `8080` | Direct-link server port |
| `DIRECT_LINK_TTL` | No | `86400` | Direct-link lifetime |
| `TELEGRAPH_ACCESS_TOKEN` | No | auto account | Optional reusable Telegraph token |

Malformed values in any of these fall back to the documented default instead of preventing the bot from starting.

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
- Settings persistence, including bulk mode
- GoFile payload/folder contracts
- Cancellation/process control
- Direct-link behavior
- MTProto transport contract
- URL workflows
- Timeout cleanup
- Sudo administration
- The background download queue (parallelism, pausing, cancellation, promotion)
- A scripted end-to-end bulk session against a fake Telegram client
- Regression tests for every bug fixed in the latest pass (trim bounds, MP4 audio, progress throttling, split naming, timecode validation, config tolerance, SSRF guard)

---

## 🏗️ Project structure

```text
.
├── app/
│   ├── main.py
│   ├── config.py
│   ├── services/
│   │   ├── archive.py
│   │   ├── bulk.py
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
