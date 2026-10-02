"""Reported bugs around a finished delivery and around the track menus.

1. After a successful rename + GoFile upload the card still showed
   "✏️ Rename file / Send the new filename…" with a Cancel button, because the
   result is posted as a *new* message and the prompt card is never touched.
2. Pressing that leftover Cancel deleted the delivered link message and the
   output files: ``cancel`` deleted whatever ``status_message_id`` pointed at
   (the finished GoFile result) and force-cleaned the user's whole directory.
3. Stream Remover / Extractor waited silently for the header probe (up to 90 s),
   so a tap looked like nothing happened.
"""
import asyncio
import contextlib
import sys
from pathlib import Path

import pytest

from app.services.bulk import QueueItem
from tests.test_download_pipeline import make_bot
from tests.test_ui_semantics import FaithfulClient
from tests.test_user_reported_bugs import Event

main_mod = __import__("app.main", fromlist=["main"])
gofile_mod = __import__("app.services.gofile", fromlist=["gofile"])

STREAMS = [
    {"index": 0, "codec_type": "video", "codec_name": "hevc", "disposition": {}},
    {"index": 1, "codec_type": "audio", "codec_name": "eac3", "disposition": {}},
    {"index": 2, "codec_type": "subtitle", "codec_name": "subrip", "disposition": {}},
]
PROBE = {"format": {"format_name": "matroska", "duration": "1.0"}, "streams": STREAMS}


def buttons_of(bot, uid, chat=1):
    st = bot.state(uid)
    msg = bot.client.messages.get((chat, st.card_message_id))
    return [[getattr(b, "text", "") for b in row] for row in (msg.buttons or [])]


class FakeText:
    """A plain text message the user "sent" while the rename prompt was open."""

    def __init__(self, text, mid=500):
        self.id = mid
        self.raw_text = text
        self.text = text
        self.photo = self.video = self.audio = self.voice = self.video_note = None
        self.file = None


def _patched_gofile(monkeypatch):
    """Stand in for the GoFile API, capturing what was uploaded."""
    calls = []

    async def fake_upload(path, token, folder, cb=None, cancel_event=None, **kw):
        calls.append(Path(path).name)
        if cb:
            size = Path(path).stat().st_size
            await cb(size, size)
        return {"downloadPage": "https://gofile.io/d/U9Py5uAR", "parentFolder": "abc123"}

    async def fake_create_folder(parent, name, token):
        return {"folderId": "f1"}

    async def fake_delete_content(content_id, token):
        return None

    # main.py binds upload_gofile/create_folder in its own namespace; the
    # bootstrap delete_content is imported inside the helper.
    monkeypatch.setattr(main_mod, "upload_gofile", fake_upload)
    monkeypatch.setattr(main_mod, "create_folder", fake_create_folder)
    monkeypatch.setattr(gofile_mod, "delete_content", fake_delete_content)
    return calls


# ----------------------------------------------------------------------
# 1 + 2. The rename prompt, and what Cancel did to the delivered result
# ----------------------------------------------------------------------

def _rename_then_upload(tmp_path, monkeypatch, uid=51, keep_files=False):
    async def run():
        client = FaithfulClient()
        bot = make_bot(tmp_path, client)
        bot.touch = lambda u: None
        bot.db.register_user(uid, "u", "U", None)
        bot.db.set_rename(uid, True)
        bot.db.set_keep_files(uid, keep_files)
        target = tmp_path / "work" / str(uid) / "movie.mkv"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * 4096)
        st = bot.state(uid)
        st.path = target
        st.outputs = [target]
        st.source_path = target

        # 1. The user picks a destination and asks to rename.
        await bot.callback_router(Event(bot, uid, "upload:gofile"), uid, "upload:gofile")
        assert "Rename before upload?" in client.rendered[-1], client.rendered[-1]
        # 2. Taps "Rename", then sends the new name.
        await bot.callback_router(Event(bot, uid, "rename:yes"), uid, "rename:yes")
        assert bot.state(uid).pending == "rename_input"
        calls = _patched_gofile(monkeypatch)
        await bot.on_new_message(Event(bot, uid, message=FakeText("My Movie.mkv")))
        return bot, client, calls, uid

    return asyncio.run(run())


def test_rename_prompt_is_replaced_after_the_upload_succeeds(tmp_path, monkeypatch):
    """The screenshot: the rename card was still on screen after delivery."""
    bot, client, calls, uid = _rename_then_upload(tmp_path, monkeypatch)
    assert calls, "the GoFile upload must actually have run"
    st = bot.state(uid)
    card = client.messages.get((1, st.card_message_id))
    assert card is not None
    assert "Rename file" not in card.text, card.text
    assert "Send the new filename" not in card.text, card.text
    labels = [t for row in buttons_of(bot, uid) for t in row]
    assert not any(t == "Cancel" for t in labels), labels


def test_cancel_after_delivery_keeps_the_link_and_the_files(tmp_path, monkeypatch):
    """Cancel on the leftover menu must not delete a delivered result."""
    # "Keep Files" on: a successful delivery leaves the output on disk, so the
    # regression cannot hide behind the ordinary post-upload cleanup.
    bot, client, calls, uid = _rename_then_upload(tmp_path, monkeypatch, keep_files=True)
    st = bot.state(uid)
    target = next(
        p for p in (tmp_path / "work" / str(uid)).iterdir() if p.suffix == ".mkv"
    )
    # The GoFile result is its own message, no longer the session's *status*
    # message - that is what used to let a later Cancel delete it.
    delivered = next(m for m in client.messages.values() if "gofile.io" in m.text)
    assert st.status_message_id is None, "the result must not stay cancellable"
    assert st.delivered is True

    asyncio.run(bot.cancel(uid, 1))

    assert delivered.deleted is False, "the delivered link message was deleted"
    assert client.messages.get((1, delivered.id)) is delivered
    assert target.exists(), "the delivered output file was deleted"


def test_cancel_still_cleans_up_while_a_job_is_running(tmp_path):
    """The fix must not weaken a real cancellation."""
    async def run():
        client = FaithfulClient()
        bot = make_bot(tmp_path, client)
        bot.touch = lambda u: None
        uid = 61
        target = tmp_path / "work" / str(uid) / "movie.mkv"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * 2048)
        st = bot.state(uid)
        st.path = target
        st.outputs = [target]
        job = asyncio.create_task(asyncio.sleep(30))
        st.task = job
        try:
            await bot.cancel(uid, 1)
        finally:
            job.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await job
        return target

    target = asyncio.run(run())
    assert not target.exists(), "a cancelled job must still drop its temporary files"


def test_cancel_before_a_delivery_is_unaffected(tmp_path):
    """A plain cancel still clears the session and returns to the start screen."""
    async def run():
        client = FaithfulClient()
        bot = make_bot(tmp_path, client)
        bot.touch = lambda u: None
        uid = 62
        target = tmp_path / "work" / str(uid) / "movie.mkv"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * 2048)
        st = bot.state(uid)
        st.path = target
        st.outputs = [target]
        await bot.cancel(uid, 1)
        return bot, target, uid

    bot, target, uid = asyncio.run(run())
    assert not target.exists()
    assert bot.state(uid).path is None
    assert bot.state(uid).view == "start"


# ----------------------------------------------------------------------
# 3. Stream menus must appear at once, or say they are working
# ----------------------------------------------------------------------

def test_stream_menu_appears_immediately_when_metadata_is_known(tmp_path):
    """No waiting screen at all when the header probe already finished."""
    async def run():
        client = FaithfulClient()
        bot = make_bot(tmp_path, client)
        bot.touch = lambda u: None
        uid = 71
        st = bot.state(uid)
        item = st.queue.add(
            QueueItem(kind="url", label="movie.mkv", chat_id=uid, url="https://x/movie.mkv")
        )
        item.probed, item.streams, item.probe_data = True, STREAMS, PROBE
        st.current_item = item
        st.probe_data = PROBE
        st.streams = list(STREAMS)
        await bot.show_file_menu(1, uid, None)
        await bot.callback_router(Event(bot, uid, "video:streams"), uid, "video:streams")
        return client, bot, uid

    client, bot, uid = asyncio.run(run())
    assert not any("Getting track information" in t for t in client.rendered), client.rendered
    assert "video" in client.rendered[-1], client.rendered[-1]


def test_stream_menu_shows_a_working_state_while_the_probe_runs(tmp_path):
    """A tap must never look ignored: something visible appears at once."""
    async def run():
        client = FaithfulClient()
        bot = make_bot(tmp_path, client)
        bot.touch = lambda u: None
        uid = 72
        st = bot.state(uid)
        item = st.queue.add(
            QueueItem(kind="url", label="movie.mkv", chat_id=uid, url="https://x/movie.mkv")
        )
        st.current_item = item
        await bot.show_file_menu(1, uid, None)

        release = asyncio.Event()
        finished = asyncio.Event()

        async def slow_probe(u, it):
            await release.wait()
            # Mirror the real probe: the item and the on-screen state both get
            # the header metadata.
            it.probed = True
            it.streams = STREAMS
            it.probe_data = PROBE
            state = bot.state(u)
            state.probe_data = PROBE
            state.streams = list(STREAMS)
            finished.set()

        bot._probe_item_metadata = slow_probe
        bot._ensure_metadata(uid, item)

        # The tap arrives while the probe is still running.
        task = asyncio.create_task(
            bot.callback_router(Event(bot, uid, "video:streams"), uid, "video:streams")
        )
        await asyncio.sleep(0.2)
        waiting = client.rendered[-1]
        # Then the probe lands and the real menu replaces the waiting state.
        release.set()
        await asyncio.wait_for(task, timeout=5)
        return client, waiting, finished

    client, waiting, finished = asyncio.run(run())
    assert "Getting track information" in waiting, waiting
    assert finished.is_set()
    assert "Getting track information" not in client.rendered[-1], client.rendered[-1]
    assert "video" in client.rendered[-1], client.rendered[-1]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))