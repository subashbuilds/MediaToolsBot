"""Bugs found in the full-system audit, each reproduced before it was fixed.

1. After Cancel, every later action was dead. ``cancel_event`` was set and
   never cleared, so ``await_input`` returned "cancelled" immediately and each
   action answered "I have no file or link to work on" for a file the user had
   just sent - for the rest of the session (6 hours by default).
2. A cancelled ranged Telegram download left its pre-allocated partial file on
   disk. Nothing else removed it, so repeated cancels ate the volume.
3. Stream Extractor -> Custom Streams ran a *remux*. The screen says "tap every
   track you want to extract", and Apply deleted exactly those tracks and
   returned the file holding the ones the user had not tapped.
4. GoFile answered HTTP 429 (rate limit) and the retry loop treated it as a
   permanent 4xx, so a rate-limited upload failed on the first response.
5. The start-up orphan sweep ran twice: once in a thread and once inline on the
   event loop, blocking every user's messages while it deleted directories.
"""
import asyncio
import sys
import types
from pathlib import Path

import pytest

from tests.test_download_pipeline import make_bot
from tests.test_ui_semantics import FaithfulClient
from tests.test_user_reported_bugs import Event, telegram_message

main_mod = __import__("app.main", fromlist=["main"])

STREAMS = [
    {"index": 0, "codec_type": "video", "codec_name": "hevc", "disposition": {}},
    {"index": 1, "codec_type": "audio", "codec_name": "eac3", "disposition": {}},
    {"index": 2, "codec_type": "subtitle", "codec_name": "subrip", "disposition": {}},
]


# ----------------------------------------------------------------------
# 1. Cancel wedged the whole session
# ----------------------------------------------------------------------

def test_an_action_still_works_after_the_user_pressed_cancel(tmp_path):
    """The reported dead end: cancel once and the bot never works again."""
    async def run():
        bot = make_bot(tmp_path, FaithfulClient())
        bot.cfg.reactions_enabled = False
        bot.cfg.public_base_url = "https://files.example.com"
        uid = 1
        bot.db.register_user(uid, "u", "U", None)

        await bot.on_new_message(Event(bot, uid, message=telegram_message(mid=1)))
        await bot.callback_router(Event(bot, uid, "cancel"), uid, "cancel")
        assert bot.state(uid).cancel_event.is_set(), "cancel must actually cancel"

        # The user sends a new file and picks an action, as anyone would.
        await bot.on_new_message(Event(bot, uid, message=telegram_message(mid=2)))
        target = tmp_path / "dl" / "movie.mkv"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * 64)
        started = []

        async def fake_download(u, item):
            started.append(item.display_name)
            item.advance(64, 64)
            return target

        bot._download_telegram_item = fake_download
        bot.client.rendered.clear()
        await bot.callback_router(Event(bot, uid, "menu:direct"), uid, "menu:direct")
        return started, list(bot.client.rendered)

    started, rendered = asyncio.run(run())
    assert started, "the queued input was never transferred"
    assert not any("no file or link to work on" in t for t in rendered), rendered
    assert any("Direct/Stream Link" in t for t in rendered), rendered


def test_sending_a_new_input_clears_a_stale_cancel_flag(tmp_path):
    async def run():
        bot = make_bot(tmp_path, FaithfulClient())
        bot.cfg.reactions_enabled = False
        uid = 1
        bot.db.register_user(uid, "u", "U", None)
        bot.state(uid).cancel_event.set()
        await bot.on_new_message(Event(bot, uid, message=telegram_message()))
        return bot.state(uid)

    st = asyncio.run(run())
    assert not st.cancel_event.is_set(), "a stale cancel flag carried into the new workflow"


# ----------------------------------------------------------------------
# 2. Cancelled ranged download left a partial file
# ----------------------------------------------------------------------

def test_cancelling_a_ranged_download_removes_the_partial_file(tmp_path):
    """The ranged downloader pre-allocates the whole file up front."""
    from app.services.telegram_download import download_media_multipart

    size = 4 * 1024 * 1024

    class Client:
        def __init__(self, cancel):
            self.cancel = cancel
            self.fell_back = False

        async def iter_download(self, media, **kwargs):
            # Keep streaming like a slow multi-gigabyte transfer. The worker
            # checks the cancel flag between chunks and aborts.
            for _ in range(200):
                await asyncio.sleep(0.005)
                yield b"x" * 65536

        async def download_media(self, *args, **kwargs):
            self.fell_back = True
            raise AssertionError("a cancelled transfer must not fall back to a single stream")

    async def run():
        dest = tmp_path / "big.bin"
        cancel = asyncio.Event()
        client = Client(cancel)

        async def cancel_soon():
            await asyncio.sleep(0.05)
            cancel.set()

        asyncio.create_task(cancel_soon())
        with pytest.raises(asyncio.CancelledError):
            await download_media_multipart(
                client, types.SimpleNamespace(media=object()), dest,
                file_size=size, parts=4, cancel_event=cancel, min_bytes=0,
            )
        return dest, client.fell_back

    dest, fell_back = asyncio.run(run())
    assert not fell_back
    assert not dest.exists(), f"cancelled download left {dest.stat().st_size if dest.exists() else 0} bytes behind"


# ----------------------------------------------------------------------
# 3. Stream Extractor deleted the tracks it was asked to extract
# ----------------------------------------------------------------------

def _at_custom_streams(tmp_path, mode):
    async def run():
        bot = make_bot(tmp_path, FaithfulClient())
        bot.cfg.reactions_enabled = False
        uid = 1
        bot.db.register_user(uid, "u", "U", None)
        await bot.on_new_message(Event(bot, uid, message=telegram_message()))
        st = bot.state(uid)
        st.streams = [dict(s) for s in STREAMS]
        st.probe_data = {"streams": st.streams}
        source = tmp_path / "dl" / "movie.mkv"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"x" * 16)
        st.path = st.source_path = st.root_path = source
        await bot.callback_router(Event(bot, uid, f"stream:{mode}:custom"), uid, f"stream:{mode}:custom")
        return bot, uid, st, source

    return asyncio.run(run())


def test_extract_mode_extracts_the_marked_tracks(tmp_path, monkeypatch):
    """Apply in extract mode must pull the selection out, not remux it away."""
    bot, uid, st, source = _at_custom_streams(tmp_path, "extract")
    calls = []

    def fake_remux(path, out, keep, **kwargs):
        calls.append(("remux", Path(out).name, list(keep)))
        return out

    def fake_extract(path, out, index, **kwargs):
        calls.append(("extract_stream", Path(out).name, index))
        Path(out).write_bytes(b"x" * 8)
        return out

    monkeypatch.setattr(main_mod.ffmpeg, "remux", fake_remux)
    monkeypatch.setattr(main_mod.ffmpeg, "extract_stream", fake_extract)

    async def run():
        # Mark the audio track (1) and the subtitle (2), then Apply.
        await bot.callback_router(Event(bot, uid, "streamtoggle:1"), uid, "streamtoggle:1")
        await bot.callback_router(Event(bot, uid, "streamtoggle:2"), uid, "streamtoggle:2")
        await bot.callback_router(Event(bot, uid, "streamapply"), uid, "streamapply")
        return bot.state(uid), list(bot.client.rendered)

    final_state, rendered = asyncio.run(run())
    assert calls, "Apply did nothing"
    assert not any(c[0] == "remux" for c in calls), f"extract mode ran a remux: {calls}"
    assert sorted(c[2] for c in calls if c[0] == "extract_stream") == [1, 2], calls
    assert not any("failed" in t.lower() for t in rendered), rendered
    assert len(final_state.outputs) == 2, final_state.outputs


def test_remove_mode_still_remuxes(tmp_path, monkeypatch):
    """The fix must not change what Stream Remover does."""
    bot, uid, st, source = _at_custom_streams(tmp_path, "remove")
    calls = []

    def fake_remux(path, out, keep, **kwargs):
        calls.append(("remux", list(keep)))
        Path(out).write_bytes(b"x" * 8)
        return out

    monkeypatch.setattr(main_mod.ffmpeg, "remux", fake_remux)

    async def run():
        await bot.callback_router(Event(bot, uid, "streamtoggle:1"), uid, "streamtoggle:1")
        await bot.callback_router(Event(bot, uid, "streamapply"), uid, "streamapply")

    asyncio.run(run())
    assert calls == [("remux", [0, 2])], calls


def test_extract_mode_says_extract_on_the_screen(tmp_path):
    """The screen and the action must agree."""
    _, _, st, _ = _at_custom_streams(tmp_path, "extract")
    assert st.view_mode == "extract"


# ----------------------------------------------------------------------
# 4. GoFile rate limiting
# ----------------------------------------------------------------------

def test_gofile_rate_limit_is_retried(tmp_path, monkeypatch):
    """HTTP 429 must be retried; a genuine 4xx must not be."""
    from app.services import gofile

    attempts = []

    async def fake_once(path, token, folder_id, progress, cancel_event):
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("GoFile HTTP 429: Too Many Requests")
        return {"downloadPage": "https://gofile.io/d/abc"}

    monkeypatch.setattr(gofile, "_upload_once", fake_once)
    payload = tmp_path / "movie.mkv"
    payload.write_bytes(b"x" * 32)

    async def run():
        return await gofile.upload_gofile(payload, token=None)

    assert asyncio.run(run())["downloadPage"].endswith("abc")
    assert len(attempts) == 3, f"the 429 was not retried ({len(attempts)} attempt(s))"


def test_gofile_bad_token_is_not_retried(tmp_path, monkeypatch):
    from app.services import gofile

    attempts = []

    async def fake_once(path, token, folder_id, progress, cancel_event):
        attempts.append(1)
        raise RuntimeError("GoFile HTTP 401: Unauthorized")

    monkeypatch.setattr(gofile, "_upload_once", fake_once)
    payload = tmp_path / "movie.mkv"
    payload.write_bytes(b"x" * 32)

    async def run():
        with pytest.raises(RuntimeError):
            await gofile.upload_gofile(payload, token=None, attempts=3)

    asyncio.run(run())
    assert len(attempts) == 1, "a permanent 4xx must fail immediately"


# ----------------------------------------------------------------------
# 5. The orphan sweep ran twice, once blocking the event loop
# ----------------------------------------------------------------------

def test_orphan_sweep_runs_once(tmp_path):
    async def run():
        bot = make_bot(tmp_path, FaithfulClient())
        bot.db.register_user(7, "u", "U", None)
        orphan = tmp_path / "dl" / "999999"
        orphan.mkdir(parents=True, exist_ok=True)
        (orphan / "leftover.bin").write_bytes(b"x" * 1024)
        calls = []
        real = bot._sweep_orphans_sync

        def counted():
            calls.append(1)
            real()

        bot._sweep_orphans_sync = counted
        await bot._sweep_orphans()
        return calls, orphan.exists()

    calls, still_there = asyncio.run(run())
    assert len(calls) == 1, f"the sweep ran {len(calls)} times"
    assert not still_there, "the orphan directory was not removed"


def test_orphan_sweep_keeps_a_user_with_live_downloads(tmp_path):
    async def run():
        bot = make_bot(tmp_path, FaithfulClient())
        uid = 11
        bot.db.register_user(uid, "u", "U", None)
        bot.state(uid).queue.add(main_mod.QueueItem(kind="url", label="a.mkv", chat_id=uid, url="https://x/1"))
        live = tmp_path / "dl" / str(uid)
        live.mkdir(parents=True, exist_ok=True)
        (live / "partial.bin").write_bytes(b"x" * 16)
        await bot._sweep_orphans()
        return live.exists()

    assert asyncio.run(run()), "the sweep deleted a directory that still has work in it"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))