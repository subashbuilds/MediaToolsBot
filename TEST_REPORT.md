# Production Verification Report

## Build artifact

This directory is the exact source tree packaged into the release ZIP.

## Automated verification

```text
pytest -q
210 passed, 3 skipped

MEDIABOT_LIVE=1 pytest -q tests/test_live_integrations.py
3 passed

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
- **The four bugs reported from the live bot** (`tests/test_user_reported_bugs.py`): the GoFile
  upload crash, URL/link-post routing, the action menu during a transfer, and Direct/Stream Link —
  each driven through the real router, renderer and callback path

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

## Bugs fixed in the on-demand download pass

The bot used to start downloading the moment a file or link arrived, and Media
Information could not answer without the whole file.

| Area | Problem | Effect |
|---|---|---|
| Download on arrival | `enqueue_input()` started the worker immediately | Every link and upload was transferred whether or not the user wanted it, and a failed/pointless download still cost bandwidth and disk |
| `execute()` arity | The job callable was always called as `func(reporter)` | Every zero-argument job — `remux`, `extract_stream`, `merge_tracks`, the custom-stream removal — died with `TypeError: ...<lambda>() takes 0 positional arguments but 1 was given`, which is the "🧹 Removing stream failed" report |
| Media Information | Always probed the downloaded file | A large file had to arrive in full before the user could see what it was |
| Track lists | Only known after the download | Stream Remover/Extractor could not be opened before the transfer completed |
| Merge / bulk | Merge inputs and the bulk queue relied on the eager download | With the download removed, `Done Adding` and `Upload All` had to start the transfer themselves, with progress |

The fix:

- `_invoke_job()` inspects the job signature and passes the progress reporter
  only to callables that accept one, so both arities work at every call site.
- `enqueue_input()` queues the input and shows the menu; `_start_worker()` and
  `await_input()` start the transfer when an action asks for it, rendering the
  real progress bar, and report a failed transfer instead of claiming no file
  was sent.
- Metadata is read without the media: FFprobe reads a link's container header
  over HTTP, and a Telegram document is probed from a short prefix
  (`PROBE_HEAD_BYTES`, 8 MiB by default) that is deleted immediately. Media
  Information and the track menus use that data; only operations that really
  rewrite or send the file download it, and only then fall back to a full
  download if no header could be read.
- Bulk mode transfers on `Done Adding` / `Upload All`, with progress.

`tests/test_on_demand_download.py` pins all of it, including a real FFmpeg
stream removal — the exact call that raised the reported `TypeError`.

The ranged Telegram downloader is verified against a double that serves real
byte ranges (byte-for-byte reassembly, parallelism, cancellation and the
fallback path) and against the **installed Telethon signature**, so a library
change fails a test instead of production. A live multi-gigabyte Telegram
transfer still cannot be exercised from here — that needs a real user account
sending a document to the bot, not a bot token.

## Bugs fixed in the transfer-speed and stream-selection pass

Reported from a live session: a 2.95 GiB Telegram file crawled with
`ETA 2461s` and no speed, and tapping **Custom Streams** in the audio/stream
remover jumped back to the main menu and started a download.

| Area | Problem | Effect |
|---|---|---|
| Telegram transfer | `download_media` keeps one `upload.getFile` request in flight | Throughput capped by a single round trip; a large file looked stalled |
| Progress text | No speed, and the ETA printed raw seconds | `ETA 2461s` told the user nothing useful |
| Progress card | The hint was added by both `render_queue` and the panel renderer | The same sentence appeared twice in one message |
| Progress card | The waiting loop and the bulk ticker each repainted on their own schedule | Up to twice the configured rate of edits to one message — the flood-wait class of bug seen before |
| Custom Streams | The handler downloaded the file before deciding what the tap needed | Choosing tracks started a transfer, and the progress panel replaced the submenu, so it looked like a jump to the main menu |
| Custom Streams | The selection was re-rendered as a bare one-line sentence | The chosen tracks were invisible, and the screen lost its context |
| Apply | `_promote_item` cleared `st.streams` when the transfer finished | The selection was resolved *after* the download, so "Apply" could answer "you cannot remove every stream" and never run |
| Apply | The panel repainted the main menu over an open submenu | The user lost their place mid-operation |

The fix:

- `app/services/telegram_download.py` splits a document into request-aligned
  ranges and reads them concurrently, pre-allocating the file and writing each
  chunk at its offset. Any failure — including a short or wrong-sized result —
  falls back to the ordinary single-stream `download_media`, so the fast path
  can only ever help. `TELEGRAM_DOWNLOAD_PARTS` (default 4) and
  `TELEGRAM_PARTS_MIN_MB` (default 24) control it.
- `QueueItem.advance()` measures the speed over the last interval and smooths
  it; the progress line shows `bar · percent · current / total` and
  `speed · ETA`, with the ETA in minutes (`41m`, `1h 05m`).
- One shared timestamp throttles the progress repaint to `PROGRESS_INTERVAL`, and
  an edit is skipped when the rendered text has not changed.
- The stream screens resolve the track list from the header first; only the
  operations that rewrite the file download it. While a transfer runs with a
  submenu open, the card offers Cancel instead of the main menu.
- The custom-selection screen is a real screen: every track is listed, taps are
  marked, the count is shown, and the selection is captured before the transfer
  starts.

`tests/test_telegram_speed.py` and `tests/test_stream_selection_ux.py` pin all
of it, including a byte-for-byte reassembly check of the ranged downloader and a
real FFmpeg run of the custom removal.

## Bugs fixed in the reported-symptoms pass

Four defects were reported from the live bot with screenshots. Each one is
reproduced by a test that fails against the pre-fix code.

| Area | Problem | Effect |
|---|---|---|
| GoFile upload | `run_gofile` called `new_status_message(chat_id, uid, text, buttons=...)`, so `uid` landed in `text` and the body landed in `buttons` | `TypeError: MediaToolsBot.new_status_message() got multiple values for argument 'buttons'` — **every GoFile upload died the moment the destination was chosen** |
| Message routing | `event.message.media` was the "is this media?" test | Telegram sets it for the invisible **WebPage preview** on a text message containing a link, so a link was queued as a phantom `telegram_<id>.bin`. Every action then failed on a message that had no file, which is why **no URL functionality worked at all** |
| Link detection | Only `raw_text` was searched for a URL | Posts that hide the link behind anchor text ("Click Here") were invisible to the bot, so a message carrying two working links was treated as empty text |
| Direct/Stream Link | `run_direct` awaited the download and then answered "Send or download a media file first" whenever `st.path` was not ready | Tapping **Make Direct/Stream Link** on a file the bot had already queued answered "send a file again" |
| Real errors | The same blanket message was rendered *after* `await_input` had already reported the failure | A download that failed with a real reason was overwritten with "you never sent anything" |
| Progress card | The action menu stayed on screen underneath the progress bar | The screenshots showed the full menu under the bar; it read as though the tap had been ignored, and none of those actions work until the transfer finishes anyway |
| Upload | `choose_upload`/`run_gofile`/`upload_telegram` looked only at already-downloaded files | Uploading a freshly sent link answered "No current file to upload" — the same on-demand-download gap the transfer refactor introduced |
| URL Uploader | Asked for another link even when one was already queued | Another dead end for the reported "URL does nothing" symptom |
| Direct/Stream Link | `PUBLIC_BASE_URL` was checked *after* fetching the whole file | A multi-gigabyte transfer ran before the bot could admit it cannot build a link at all |

The fix:

- `new_status_message` is called with `uid=` as a keyword everywhere; the
  positional call site is gone.
- `is_media_message()` decides media by looking for an actual downloadable
  part, and `message_urls()` reads links from the text *and* from hidden
  `text_url` entities. `receive_media()` refuses a link-only message as a
  second line of defence.
- `_require_input()` is the single way an action obtains its media: it starts
  the queued transfer, and on failure it lets the real error stand instead of
  claiming no file was ever sent.
- The progress card shows only Cancel while bytes are moving; the action menu
  returns once the file is on disk.
- Upload and URL-Uploader paths fetch a queued-but-untransferred input rather
  than reporting it missing.
- `run_direct` checks `PUBLIC_BASE_URL` first, so no transfer is spent on a
  link the server cannot serve.
- The URL downloader sends a browser-shaped `User-Agent` (the old
  bot-identifying one was rejected by several hosts and by Cloudflare in front
  of them) and rejects a `text/html` response as "that link is a web page, not
  a media file" instead of downloading a page as if it were media.

`tests/test_user_reported_bugs.py` pins all of it. Each fix was verified by
reverting it and confirming the matching test fails with the production
symptom — the GoFile revert raises the exact reported `TypeError`, and the
routing revert produces the `telegram_2.bin` phantom file and the
"This message no longer contains a file" failure seen in the logs.

## Live verification with the supplied environment

Run with the values from `.env` (names only in this report; no secret is
printed):

```text
python -m app
INFO media-tools: Bot started via Telegram MTProto: @MediaTooolsBot id=8992989518
INFO media-tools: Transport: MTProto only; HTTP Bot API/Local Bot API is NOT used
INFO media-tools: FFmpeg=/usr/bin/ffmpeg FFprobe=/usr/bin/ffprobe cryptg=optional-not-installed
INFO media-tools: Limits: jobs=10 ffmpeg=1 parallel_downloads=3 max_download=unlimited
```

The bot authenticated over MTProto with the configured `API_ID`/`API_HASH`/
`BOT_TOKEN`, started the direct-link server, swept the download directory and
shut down cleanly on `SIGINT`.

`tests/test_live_integrations.py` (opt-in, `MEDIABOT_LIVE=1`) then runs a real
user journey against a local HTTP origin with real FFmpeg/FFprobe and the real
services:

1. a link is sent — **nothing is downloaded**;
2. Media Information is requested — a **real Telegraph page** is created from
   the container header, still without a download;
3. Stream Remover is opened — the track list comes from the header;
4. a track is removed — this is where the transfer starts, progress is shown,
   real FFmpeg muxes the result, and the file is then uploaded with the
   configured GoFile token.

Still not verifiable from here: a multi-gigabyte Telegram transfer (needs a real
user account, not a bot token) and live Railway/Render ingress. The SSRF guard
is covered by tests that assert the block; the local-HTTP tests opt in
explicitly with `allow_private=True`, the same escape hatch
`ALLOW_PRIVATE_DOWNLOADS` provides in production.

## Frozen download progress (reported symptom)

> "After clicking the audio track I want to remove it just stuck there not
> showing anything but after some hours I pressed it shows where do you want to
> upload — like the progress is not showing."

**Reproduced.** A 2.53 GB Telegram document, opened through *Custom Streams*,
one track marked, **Apply** pressed. The download screen was painted exactly
**once** and then nothing was sent to Telegram for the entire wait:

```text
📥 Downloads
Queued: 1   Ready: 0   Downloading: 1
⬇️ movie.mkv
    ░░░░░░░░░░░░░░   0.0% · 0 B downloaded
```

and `await_input` is prepared to wait up to `DOWNLOAD_WAIT` (4 hours) before it
says anything. That is exactly "stuck there not showing anything".

**Cause.** `_render_panel` skipped any repaint whose text was identical to the
last one, which is a sensible way to avoid pointless edits — but a transfer
that is not producing bytes produces identical text, so the first frame was
also the last. On top of that `claim()` never copied the known
`expected_size` onto the item, so the bar had no total to print until the first
chunk arrived, and the shared repaint throttle could swallow the very first
download frame when something else had repainted moments earlier — leaving the
submenu the user tapped still on screen with no sign anything had started.

**Fixed.**

| Change | File |
|---|---|
| The running item now always prints `⏱ Ns elapsed`, so the panel text changes on every repaint and the card keeps moving while no bytes arrive | `app/services/bulk.py` |
| `⚠️ no data for …` after 45 s without a byte, so a stall is stated instead of inferred from a frozen bar | `app/services/bulk.py` |
| `claim()` seeds `total` from `expected_size`: the first frame reads `0 B / 2.53 GiB` | `app/services/bulk.py` |
| `_render_panel` no longer suppresses an identical repaint while a transfer is running; the repaint *rate* is still bounded by `_panel_due` | `app/main.py` |
| `await_input` clears the repaint throttle before its first pass, so the action the user just tapped is always on screen at once | `app/main.py` |

**Regression tests** — `tests/test_progress_visibility.py` (5 tests):

- `test_pressing_apply_repaints_the_screen_the_user_is_on` — Apply paints the
  download screen onto the card the user was looking at, not a new message.
- `test_a_stalled_transfer_keeps_repainting` — a transfer with zero incoming
  bytes keeps producing frames whose elapsed counter advances. Removing the
  elapsed line makes this fail.
- `test_a_stalled_transfer_is_called_out` — ten minutes without a byte renders
  `⚠️ no data for 10m`.
- `test_claiming_a_transfer_knows_its_size_before_the_first_byte` — the total
  is known the moment the item is claimed.
- `test_apply_never_leaves_the_submenu_on_screen` — the reported symptom stated
  as an assertion: the Custom Streams screen is gone from the visible card.

## Full-system audit

Every module was read end to end (`app/main.py`, all of `app/services/`,
`app/storage/`, `app/ui/`, `app/utils/`, `app/config.py`) and the behaviour was
then exercised rather than assumed. Beyond reading, three sweeps were run:

- **every button the bot can emit** (113 callback payloads from
  `app/ui/keyboards.py` plus the ones built inline) was driven through
  `callback_router` to prove none of them is a dead button;
- the **direct-link server** was started and hit over real HTTP: full body,
  `Range: bytes=0-9` → `206`, unknown token → `404`, a file outside the served
  roots → `404`, an expired link → `410`, `/health` → JSON;
- **every FFmpeg entry point** (remux, remove-audio, trim with and without an
  end, optimize, mp4, mkv, video→audio, sample, split, screenshots, manual
  shot, stream extraction, merge, audio convert, audio filter) was run against
  a real generated MKV with the real binary.

### Bugs found and fixed

| # | Bug | Impact | Fix |
|---|---|---|---|
| 1 | `cancel_event` was set by Cancel and never cleared again | **Severe.** Every action afterwards short-circuited in `await_input` and answered "I have no file or link to work on" for a file the user had just sent — the bot was dead for the rest of the 6-hour session | `app/main.py` — `enqueue_input` installs a fresh `asyncio.Event`, so a new input is a new workflow |
| 2 | A cancelled ranged Telegram download left its pre-allocated partial file on disk | The file is allocated at full size up front, so repeated cancels silently ate the volume | `app/services/telegram_download.py` — the `CancelledError` branch now unlinks `dest` before re-raising |
| 3 | **Stream Extractor → Custom Streams ran a remux.** The screen says "tap every track you want to **extract**"; Apply deleted exactly those tracks and returned the file holding the ones the user had *not* tapped | The whole Custom Streams half of Stream Extractor did the opposite of what it said | `app/main.py` — new `_extract_custom_streams`; `streamapply` now branches on `st.view_mode` |
| 4 | GoFile answered HTTP 429 (rate limit) and the retry loop treated every `HTTP 4…` as permanent | A single rate-limited response failed the whole multi-gigabyte upload with no retry | `app/services/gofile.py` — 408 and 429 are retried; other 4xx still fail immediately |
| 5 | `_sweep_orphans` called the threaded helper and then repeated the entire sweep inline | Directories were walked twice, and the second pass ran `shutil.rmtree` on the event loop, blocking every user's messages at start-up | `app/main.py` — the duplicated body is gone |

Bug 1 and bug 3 were both confirmed by execution before the fix and are covered
by tests that fail when the fix is reverted.

### Regression tests — `tests/test_audit_fixes.py` (10 tests)

| Test | Bug |
|---|---|
| `test_an_action_still_works_after_the_user_pressed_cancel` | 1 |
| `test_sending_a_new_input_clears_a_stale_cancel_flag` | 1 |
| `test_cancelling_a_ranged_download_removes_the_partial_file` | 2 |
| `test_extract_mode_extracts_the_marked_tracks` | 3 |
| `test_remove_mode_still_remuxes` | 3 (guards the fix against over-reach) |
| `test_extract_mode_says_extract_on_the_screen` | 3 |
| `test_gofile_rate_limit_is_retried` | 4 |
| `test_gofile_bad_token_is_not_retried` | 4 (guards the fix against over-reach) |
| `test_orphan_sweep_runs_once` | 5 |
| `test_orphan_sweep_keeps_a_user_with_live_downloads` | 5 (guards the fix against over-reach) |

Reverting all five fixes makes exactly the six matching tests fail, so none of
them passes vacuously.

### Still not verifiable from here

A multi-gigabyte Telegram transfer against a real datacenter (needs a real user
account, not the bot token) and live Railway/Render ingress.
