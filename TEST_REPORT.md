# Production Verification Report

Date: 2026-08-18

## Automated result

`47 passed`

The final ZIP was tested from the extracted project directory after all source changes.

## Key regression tests

- Merge queue validation uses a snapshot and never appends to the list being iterated.
- Video + audio merge produces one copy of each queued input; no accidental duplicate-input mux.
- Captioned Telegram audio/document messages enter the merge collector.
- Merge starts with the current media already counted as file 1.
- Merge UI exposes Finish, Cancel and Back; no Add More Files button.
- Per-user concurrency is one; global semaphore defaults to 10 and also covers heavy transfers.
- Sudo-only ongoing-process controls are present.
- First-contact user registration stores Telegram ID/username and supports sudo notification.
- Six-hour inactivity timeout removes user files/workflow and sends a redo message.
- Successful GoFile/Telegram uploads clean local files while active direct-link files are protected until expiry.
- GoFile batch uploads reuse one folder and completion links are rendered into the existing progress message.
- Start/status/system/help/settings navigation edits the existing UI message where possible.
- Video/audio functionality menus include Back buttons.
- Direct/stream links retain HTTP Range support and use the configured/auto-detected public origin.
- MTProto transport uses Telethon with API ID, API hash and bot token authorization; no HTTP Bot API endpoint or Local Bot API server is configured.
- FFmpeg/FFprobe and archive functionality compile and the offline media integration tests pass.

## Live-network limitation

A live Telegram MTProto transfer and live GoFile account upload cannot be executed in this isolated build environment because external network access and deployer credentials are unavailable. Those are not falsely marked as live-tested.
