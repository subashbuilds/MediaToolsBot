# Production Verification Report

## Build artifact

This directory is the exact source tree packaged into the release ZIP.

## Automated verification

```text
pytest -q
154 passed

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
- **Telegram-faithful UI semantics** (`tests/test_ui_semantics.py`): a fake client that raises
  `MessageNotModifiedError` on an unchanged edit and returns `None` for a deleted message, covering
  re-render behaviour, card recovery after deletion, mid-download action handling, menu placement,
  user-keyed status state, stale-button feedback, and a full scripted user journey

### Why the first pass missed the interface bugs

The original fake Telegram client never raised `MessageNotModifiedError` and never returned `None`
for a deleted message, so it could not reproduce the two failures users actually hit. Every
"screen re-render" test passed against a fake that always reported success. `tests/test_ui_semantics.py`
models both behaviours explicitly. Any future UI change should use that client rather than the
optimistic one.

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

## Bugs fixed after the first deployment feedback

These were reported by users against the deployed bot. Every one is now covered by a test that
fails without the fix.

| Area | Problem | Effect |
|---|---|---|
| `safe_edit` | `MessageNotModifiedError` was swallowed and returned as `None`, which callers read as "edit failed" | Every re-render of an identical screen **sent a brand-new message** — the chat filled with duplicate menus |
| `answer()` | `event.respond(text, alert=...)` — Telethon's `respond` has no `alert` parameter | `TypeError` was swallowed, so **every toast in the bot silently did nothing** |
| Card placement | `render_card` fell back to the previous card's ids after a new input | The new menu rendered **above** the file instead of below it |
| `await_input` | Actions checked `st.path` before the background download finished | "Send a media file first" **even though the bot was already downloading the file** |
| `video_media` | `ffprobe` ran synchronously on the event loop and any error collapsed to `None` | A slow probe froze the whole bot, and any hiccup produced "Send a video first" |
| `new_status_message` | Keyed its state by `chat_id` while `clear_status_message`/`progress_message` keyed by `uid` | In groups, status messages were never deleted and progress edits went to the wrong user |
| `run_trim` | Validated `st.path` before calling `await_input` | Rejected a valid trim request while the file was still downloading |
| Audio filters | `run_audio_convert`/`run_audio_filter` never waited and replied with `send_message` | Claimed no file existed, and added an extra message per attempt |
| Reply routing | Many validation replies used `client.send_message` instead of editing the card | Extra messages accumulated on every error |
| Telethon API surface | Calls were not checked against the real library signatures | Verified with an AST audit against Telethon 1.44.0: no remaining invalid keyword arguments |

## Bugs fixed in the download / merge / metadata pass

| Area | Problem | Effect |
|---|---|---|
| **Merge** | `merge_status_text()` was called from five places but never defined | `AttributeError` on every merge action — **the entire Merge feature was dead** |
| Merge order | Tracks were joined in download-completion order | Files could be merged in the wrong order with no way to correct it |
| Download limit | `MAX_DOWNLOAD_MB` defaulted to 2048 | Any file over 2 GiB was refused. That limit is Telegram's **upload** limit and never applied to downloads |
| Download guard | No protection once the artificial cap was removed | Added a real free-space check that fails fast with a readable message, instead of filling the disk mid-write |
| Progress throttle | `_should_emit` short-circuited to "always emit" when `total` was unknown | A server without `Content-Length` edited the message on **every chunk** and hit flood-wait. It only looked throttled because the speed text changed each time and defeated the duplicate-text suppression |
| Stream metadata | Track list was only known after the full download | Added a background `ffprobe` on the link so audio/video tracks are available while the file is still transferring, plus `Remove all audio` / `Keep default audio` actions |
| Merge order UI | No keyboard for reordering | Added per-track up/down, move-to-top/bottom and remove, with a numbered order screen |
| `self.*` audit | `pyflakes` cannot see attribute access | Added an AST check: 0 of 113 called methods are undefined |

### Verified against real tools, not mocks

- Remote metadata is read from a real `ffprobe` over HTTP against a live
  `http.server` serving a genuine 2-audio-track MKV.
- The Telegram split threshold is asserted against the documented 2 GiB bot limit.
- The GoFile path is asserted **not** to chunk.
- The free-space guard is exercised with a real `shutil.disk_usage` reading.

## Bugs found from the production logs and screenshot

The deployment log and the attached screenshot exposed three separate defects.

| Area | Problem | Effect |
|---|---|---|
| Start-up sweep | `await asyncio.to_thread(self._sweep_orphans)` passed an `async def` to a thread helper | Telethon-free but fatal: `RuntimeWarning: coroutine 'MediaToolsBot._sweep_orphans' was never awaited`. The sweep **never ran**, so every restart left the previous run's downloads on disk until the volume filled |
| Download panel | The non-bulk panel printed only the file name | A healthy transfer looked completely frozen — no bar, no byte counts, no ETA |
| Panel vs navigation | The background panel repainted whenever `st.view == "queue"`, but nothing ever changed `view` on a button press | Tapping any button opened the submenu and it **immediately snapped back to the main menu** |
| Merge sizing | No size or duration feedback | Added a running total, estimated output (muxing re-muxes, so ≈ inputs +3%) and longest-track duration after every added file, plus a pre-merge disk-space check |

`tests/test_reported_symptoms.py` reproduces each of these. The submenu regression
was confirmed to fail against the pre-fix code and pass after it.

## External integration boundary

The isolated build environment does not contain a Docker daemon and does not have live Telegram/GoFile credentials. Therefore the following cannot honestly be claimed as live-network-tested here:

- Real Telegram authentication with the supplied production bot account
- Real multi-gigabyte Telegram transfer
- Real GoFile upload against a live account
- Live Railway/Render ingress
- Live Telegraph account creation

The release therefore distinguishes **automated local verification** from **live deployment verification** instead of claiming that external services were tested with credentials that were not available to the build environment.

The SSRF guard is covered by tests that assert the block; the two local-HTTP download tests opt in explicitly with `allow_private=True`, which is the same escape hatch the `ALLOW_PRIVATE_DOWNLOADS` environment variable provides in production.
