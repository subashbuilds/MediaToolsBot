# Verification Report — Media Tools Bot

## Test environment

- Python: 3.13 runtime
- FFmpeg: 7.1.5
- FFprobe: 7.1.5
- Telethon: 1.44.0
- Platform: Linux x86_64

## Automated result

```text
61 passed
```

The final package was compiled and tested, then the **exact ZIP artifact was extracted into a clean directory and tested again**.

## Covered areas

- Telegram MTProto transport contract
- Telegram media URL-vs-media routing
- Large-file download/upload callback contracts
- Progress throttling and completion state
- Single active process per user
- Global concurrency configuration
- Cancellation and stale progress cleanup
- FFmpeg process registration and cancellation
- HEVC/sample generation without unnecessary re-encoding
- FFprobe stream detection
- Stream removal and extraction
- Merge queue persistence
- Video + audio/video merge combinations
- Merge duplicate-input regression
- Screenshot count limit 1–20
- Multiple manual screenshot timestamps
- Human-readable media helpers
- Telegraph report generation contract
- Rename-before-upload workflow
- Upload destination preference
- GoFile guest/authenticated payloads
- GoFile folder reuse
- GoFile completion output without folder-only response
- User registration/new-user notification contract
- Sudo ongoing-process view
- Sudo user-job cancellation
- Sudo broadcast
- Six-hour timeout cleanup
- Direct-link HTTP service and Range behavior
- Archive handling
- UI callback/back navigation
- Settings persistence
- Clean ZIP extraction
- Python compilation/import checks

## Live-test limitation

A real Telegram MTProto transfer and a real GoFile upload require deployment credentials and outbound network access. Those credentials are intentionally not included in the project. The automated suite therefore verifies the real local processing paths and network payload contracts without pretending that a live external transfer occurred.

Docker is included and the Dockerfile performs Python compilation during image build. The current verification environment does not have the Docker CLI installed, so an actual `docker build` was not claimed as executed.
