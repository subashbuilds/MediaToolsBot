# Production Verification Report

## Build artifact

This directory is the exact source tree packaged into the release ZIP.

## Automated verification

```text
pytest -q
122 passed

python -m compileall -q app tests
PASS

python -m pyflakes app
PASS (only the intentional `import cryptg` availability probe)
```

### Covered areas

- Telegram MTProto transport contract
- Telegram media download contract
- Telegram upload contract
- >1.95 GiB chunking logic
- Upload progress callbacks
- GoFile upload payloads, retry and folder contracts
- GoFile reusable folder behavior
- Archive detection, extraction, traversal and decompression-bomb protection
- Multi-part archive naming
- Password argument flow and password detection precedence
- FFmpeg media processing
- FFprobe probing, caching and bounded subprocess timeouts
- Lossless stream muxing
- Merge queue deduplication
- Sample generation
- Screenshots and manual screenshots
- Settings persistence (rename, upload mode, Telegram mode, thumbnail, bulk mode, housekeeping flags)
- Rename workflow
- Custom thumbnail persistence
- Direct-link retention
- Cancellation and process-control interruption
- Idle-timeout state cleanup
- Admin controls and broadcast
- User registration
- Same-message UI navigation and stale-button fallback
- URL input workflow, including links embedded in longer text
- URL/file upload workflow
- Removal of link shortener/unshortener
- Structured Telegraph information generation
- MTProto message reactions
- **Background download queue**: parallelism limits, pausing during jobs, cancellation, duplicate/missing-file handling, and the rule that a running job keeps its input file
- **Bulk mode**: queue model, rendering, and a scripted end-to-end session (enable bulk → send three files → Done Adding → Upload All) driven against a fake Telegram client
- **Real HTTP download through the background worker** against a local aiohttp server

## Bugs fixed in this pass

Each of these had a reproduction or a code-level proof:

| Area | Problem | Effect |
|---|---|---|
| Archive menu | A second, stale `run_archive` definition shadowed the real one | `Extract Archive` from the menu raised `TypeError` — the feature was unreachable |
| Help screen | A second `send_help` definition shadowed the new one | The updated help text never displayed |
| `/broadcast` | `st` was never bound in that branch | `NameError` — the admin saw nothing at all |
| Trim | `-ss` (input) mixed with `-to` (output) | A `00:10`–`00:20` request returned a 30 s clip |
| MP4 conversion | `-c:a copy` for any audio codec | FLAC/Opus input failed with "Could not write header" |
| MP4 conversion | Every video stream was mapped | Multi-angle input aborted the whole conversion |
| Progress | Throttling was skipped when the total size was unknown | Servers without `Content-Length` triggered a flood-wait |
| Event loop | `ffprobe` ran synchronously on the loop | Probing a large file froze the whole bot |
| Message safety | Error text embedded in HTML without escaping | A `<` in a filename made Telegram reject the whole message |
| Idle timeout | Long jobs never refreshed activity | A running transcode was killed after the idle window |
| Download | No size ceiling | A single link could fill the disk |
| Download | No private-address check | The bot could be used to fetch internal services (SSRF) |
| Download | No retry | One dropped connection aborted a multi-gigabyte batch |
| Multi-part archives | The first part was copied, not moved | Disk usage doubled for large archives |
| `Manual Shots` | Out-of-range timestamps | 0-byte JPEGs that failed to upload |
| Screenshots | No busy/cancel guard | Unlimited concurrent FFmpeg jobs from one user |
| Photos | Saved as `.bin` | Photos arrived without a usable extension |
| `auto` audio trim | Unvalidated, unbounded filter input | Nonsense input reached the filter graph |
| Direct links | One sleeping task per link | Thousands of idle tasks on an active deployment |
| `Content-Disposition` | Raw UTF-8 in `filename` | Non-conformant header for non-ASCII names |
| Broadcast | Unthrottled, ran inline | Flood-wait risk and a frozen admin screen |
| Cancel | Left background downloads running | Cancelled sessions kept pulling data |
| Start-up | No orphan cleanup | Crashes left whole directories on disk forever |
| `ffmpeg.py` | `cancel_process` was broken | `AttributeError` on a bad `file.name` |
| `ffprobe` | No timeout, no cache | A corrupt container could hang the process |
| Database | No lock, write per message | SQLite contention under load |
| Config | Any malformed env var aborted boot | A typo took the whole bot down |

## External integration boundary

The isolated build environment does not contain a Docker daemon and does not have live Telegram/GoFile credentials. Therefore the following cannot honestly be claimed as live-network-tested here:

- Real Telegram authentication with the supplied production bot account
- Real multi-gigabyte Telegram transfer
- Real GoFile upload against a live account
- Live Railway/Render ingress
- Live Telegraph account creation

The release therefore distinguishes **automated local verification** from **live deployment verification** instead of claiming that external services were tested with credentials that were not available to the build environment.

The SSRF guard is covered by tests that assert the block; the two local-HTTP download tests opt in explicitly with `allow_private=True`, which is the same escape hatch the `ALLOW_PRIVATE_DOWNLOADS` environment variable provides in production.
