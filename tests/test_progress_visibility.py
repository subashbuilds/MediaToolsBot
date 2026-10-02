"""The download started by a tap must stay visible on screen.

Report: after marking a track in Custom Streams and pressing **Apply**, the
card "just stuck there not showing anything", and only much later did the bot
answer with the upload-destination question.

Cause: the progress panel is only re-sent to Telegram when its text changes
(`_render_panel` skips an identical repaint). A transfer that is not moving
produces identical text, so the very first frame was the last one: the screen
sat frozen for the whole wait - and `await_input` is prepared to wait up to
four hours - with no byte counter moving and nothing saying it was stuck.

These tests drive the real router and the real renderers.
"""
import asyncio
import sys
import time

import pytest

from tests.test_download_pipeline import make_bot
from tests.test_ui_semantics import FaithfulClient
from tests.test_user_reported_bugs import Event, telegram_message

main_mod = __import__("app.main", fromlist=["main"])

STREAMS = [
    {"index": 0, "codec_type": "video", "codec_name": "hevc", "disposition": {"default": True}},
    {"index": 1, "codec_type": "audio", "codec_name": "eac3", "disposition": {}},
    {"index": 2, "codec_type": "subtitle", "codec_name": "subrip", "disposition": {}},
    {"index": 3, "codec_type": "video", "codec_name": "mjpeg", "disposition": {}},
]


async def _at_custom_streams(tmp_path, size=2_723_415_680):
    """Send a 2.5 GB file, open Custom Streams and mark the audio track."""
    bot = make_bot(tmp_path, FaithfulClient())
    bot.cfg.reactions_enabled = False
    bot.cfg.progress_interval = 0.2
    uid = 1
    bot.db.register_user(uid, "u", "U", None)
    await bot.on_new_message(Event(bot, uid, message=telegram_message(size=size)))
    st = bot.state(uid)
    st.streams = list(STREAMS)
    st.probe_data = {"streams": STREAMS}
    await bot.callback_router(Event(bot, uid, "stream:remove:custom"), uid, "stream:remove:custom")
    await bot.callback_router(Event(bot, uid, "streamtoggle:1"), uid, "streamtoggle:1")
    return bot, uid


def _download_screens(client):
    return [t for t in client.rendered if "Downloads" in t]


def test_pressing_apply_repaints_the_screen_the_user_is_on(tmp_path):
    """The download must appear on the card that was just tapped."""
    async def run():
        bot, uid = await _at_custom_streams(tmp_path)
        st = bot.state(uid)
        card_before = st.card_message_id
        assert "Custom Streams" in bot.client.messages[(1, card_before)].edits[-1]

        target = tmp_path / "dl" / "movie.mkv"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * 16)

        async def instant(u, item):
            item.advance(8, 16)
            return target

        bot._download_telegram_item = instant
        original = main_mod.ffmpeg.remux
        main_mod.ffmpeg.remux = lambda *a, **k: target
        before = len(_download_screens(bot.client))
        try:
            await bot.callback_router(Event(bot, uid, "streamapply"), uid, "streamapply")
        finally:
            main_mod.ffmpeg.remux = original
        return bot, card_before, before

    bot, card_before, before = asyncio.run(run())
    screens = _download_screens(bot.client)
    assert len(screens) > before, "no download screen was ever shown"
    assert bot.state(1).card_message_id == card_before, "the progress went to a different message"
    assert "movie.mkv" in screens[0]


def test_a_stalled_transfer_keeps_repainting(tmp_path):
    """A transfer with no incoming bytes must not freeze the card."""
    async def run():
        bot, uid = await _at_custom_streams(tmp_path)
        release = asyncio.Event()

        async def stalling(u, item):
            # Telegram accepted the request but nothing comes back: the exact
            # shape of the reported stall on a multi-gigabyte transfer.
            await release.wait()
            return None

        bot._download_telegram_item = stalling
        waiter = asyncio.create_task(
            bot.callback_router(Event(bot, uid, "streamapply"), uid, "streamapply")
        )
        await asyncio.sleep(1.5)
        early = list(_download_screens(bot.client))
        await asyncio.sleep(2.5)
        try:
            return early, list(_download_screens(bot.client))
        finally:
            release.set()
            waiter.cancel()
            try:
                await waiter
            except (asyncio.CancelledError, Exception):
                pass

    early, late = asyncio.run(run())
    # Before the fix there was exactly one frame and then silence for as long
    # as the wait lasted, which reads as a dead bot.
    assert late, "no download screen at all"
    assert len(late) > len(early), (
        "the screen stopped being repainted while the transfer was still running: "
        f"{len(early)} -> {len(late)}"
    )
    elapsed = {line for s in late for line in s.splitlines() if "elapsed" in line}
    assert len(elapsed) >= 2, f"the elapsed counter never moved: {elapsed}"
    # And the user can see how much is being fetched.
    assert any("2.54 GiB" in s for s in late), late


def test_a_stalled_transfer_is_called_out(tmp_path):
    async def run():
        from app.services.bulk import QueueItem

        item = QueueItem(kind="telegram", label="movie.mkv", chat_id=1, expected_size=2_723_415_680)
        item.status = "downloading"
        item.started_at = time.monotonic() - 600
        item.advance(1024, 2_723_415_680)
        # No byte has arrived for ten minutes.
        item._last_at = time.monotonic() - 600
        return item

    item = asyncio.run(run())
    assert item.stalled
    from app.services.bulk import _item_line

    line = _item_line(item)
    assert "no data for" in line, line
    assert "10m" in line, line


def test_claiming_a_transfer_knows_its_size_before_the_first_byte(tmp_path):
    """The bar must show "0 B / 2.53 GiB", not a bar with no total."""
    from app.services.bulk import DownloadQueue, QueueItem

    item = QueueItem(kind="telegram", label="movie.mkv", chat_id=1, expected_size=2_723_415_680)
    assert item.total == 0
    assert item.status == "queued"

    queue = DownloadQueue(max_parallel=1)
    queue.add(item)
    claimed = queue.claim()
    assert claimed is item
    assert item.total == 2_723_415_680


def test_apply_never_leaves_the_submenu_on_screen(tmp_path):
    """The reported symptom, stated as an assertion on the visible card."""
    async def run():
        bot, uid = await _at_custom_streams(tmp_path)
        st = bot.state(uid)

        async def stalling(u, item):
            await asyncio.sleep(0.6)
            return None

        bot._download_telegram_item = stalling
        original = main_mod.ffmpeg.remux
        main_mod.ffmpeg.remux = lambda *a, **k: None
        before = len(_download_screens(bot.client))
        try:
            await bot.callback_router(Event(bot, uid, "streamapply"), uid, "streamapply")
        finally:
            main_mod.ffmpeg.remux = original
        card = bot.client.messages[(1, st.card_message_id)]
        return card.edits, _download_screens(bot.client)[before:]

    edits, screens = asyncio.run(run())
    assert screens, "Apply produced no download screen at all"
    assert not any("Custom Streams" in t for t in screens), screens
    assert "Custom Streams" not in edits[-1], edits[-1]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))