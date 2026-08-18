# Media Tools Bot — Verification Report

Date: 2026-08-18

## Changes verified in this build

- Plain HTTP/HTTPS URLs are accepted directly in the chat, including Telegram messages that have a WebPage preview attached.
- URL inputs use the same media-processing menu as Telegram files.
- `/urlupload` and the URL Uploader menu remain available.
- Merge collection accepts both Telegram media and HTTP/HTTPS URLs.
- Direct/Stream Link uses the bot's own HTTP server with signed expiring URLs and HTTP Range support.
- `PUBLIC_BASE_URL` is optional on supported managed hosts; Railway, Render, `APP_URL`, and Heroku-style domains are auto-detected. A VPS/custom reverse proxy still requires an explicit public URL because a public hostname cannot be invented safely.
- Stream extraction preserves the original media source so later stream-removal/video operations do not accidentally operate on an extracted SRT/audio file.
- Stream listing is generated from the preserved source and includes video, audio, subtitle and other streams.
- Cancellation clears pending workflows and removes the inline keyboard from the cancelled message.
- Successful transfer messages no longer retain a progress bar or Cancel button.
- Pending workflows take precedence over the generic URL handler, so Link Short/Unshort and other text-entry modes still receive their intended input.
- `MESSAGE_NOT_MODIFIED` is treated as a harmless no-op.

## Transport verification

- Telegram transport: MTProto via Telethon 1.44.0
- Bot authorization: `API_ID` + `API_HASH` + `BOT_TOKEN`
- HTTP Bot API endpoint: not used
- Local Bot API Server: not used
- `progress_callback` wired to both Telegram `download_media()` and `send_file()`
- Cancellation wired to Telegram transfer callbacks

## Local test results

`pytest -q`:

**26 passed**

The suite covers:

- FFprobe stream detection
- lossless remux / stream removal
- multiple audio + subtitle stream detection
- multi-input track merge
- audio conversion and filters
- MP4/MKV conversion
- text-subtitle conversion to MP4
- video trimming
- screenshots
- sample generation
- video splitting
- optimization
- ZIP extraction
- archive path-traversal rejection
- streaming multipart payloads
- GoFile payload progress
- GoFile payload cancellation
- URL download cancellation and partial-file cleanup
- real local HTTP URL download with Content-Disposition filename handling
- direct HTTP Range / `206 Partial Content` serving behavior
- progress-bar rendering, speed and ETA
- URL-input routing order
- direct-link public-base auto detection
- cancellation keyboard cleanup contract
- preserved-source fallback when the current file is an extracted subtitle
- UI layout and callback size
- MTProto transport contract (source-level)
- Python compilation / AST parsing
- absence of Bot API / Local Bot API configuration

## Additional checks

- `python -m compileall -q app tests`: **PASS**
- Docker Compose YAML parse: **PASS**
- Final ZIP extraction: **PASS**
- ZIP integrity: **PASS**

Docker itself is not installed in this isolated build environment, so a real `docker build` could not be executed here.

## Live integration limitation

This isolated build environment has no outbound package/network access and does not contain the user's Telegram credentials. Therefore a real Telegram MTProto login, real Telegram multi-gigabyte download/upload, live GoFile upload, and live Telegraph page creation cannot honestly be marked as executed here.

Telethon 1.44.0 documentation was checked before implementation. Its documented `download_media()` and `send_file()` APIs accept `(current, total)` progress callbacks. Telegram's MTProto bot documentation confirms bot authorization with API ID, API hash and bot token.
