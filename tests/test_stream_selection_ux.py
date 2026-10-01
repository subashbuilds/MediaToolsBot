"""The Stream Remover / Extractor screens must behave like a real menu.

Reported: tapping "Custom Streams" in the audio/stream remover did not open the
"pick the tracks to remove" screen - it jumped back to the main menu and started
a download, and the download progress bar kept repainting the main menu over
whatever the user had open.
"""
import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from app.services.bulk import QueueItem
from tests.test_download_pipeline import FakeMessage, make_bot
from tests.test_ui_semantics import FaithfulClient

main_mod = __import__("app.main", fromlist=["main"])

STREAMS = [
    {"index": 0, "codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080, "tags": {"language": "und"}},
    {"index": 1, "codec_type": "audio", "codec_name": "aac", "tags": {"language": "eng"}},
    {"index": 2, "codec_type": "audio", "codec_name": "aac", "tags": {"language": "jpn"}},
    {"index": 3, "codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "eng"}},
]
PROBE = {"format": {"format_name": "matroska", "duration": "2.0"}, "streams": STREAMS}


class Event:
    def __init__(self, bot, uid, data, chat=1):
        self.sender_id, self.chat_id, self.data = uid, chat, data.encode()
        self.answers, self.responses = [], []
        self.message = bot.client.messages.get((chat, bot.state(uid).card_message_id))

    async def answer(self, text=None, alert=False):
        self.answers.append((text, alert))

    async def respond(self, text=None, **kw):
        self.responses.append(text)

    async def edit(self, text, buttons=None, **kw):
        return await self.message.edit(text, buttons=buttons, **kw)


def buttons_of(bot, uid, chat=1):
    card = bot.client.messages[(chat, bot.state(uid).card_message_id)]
    return [[getattr(b, "text", "") for b in row] for row in (card.buttons or [])]


def prepared_bot(tmp_path, uid=31):
    """A bot with a link queued, metadata known and nothing downloaded yet."""
    client = FaithfulClient()
    bot = make_bot(tmp_path, client)
    bot.allowed = lambda u: True
    bot.is_sudo = lambda u: False
    bot.touch = lambda u: None
    item = QueueItem(kind="url", label="movie.mkv", chat_id=uid, url="https://x/movie.mkv")
    item.probed, item.streams, item.probe_data = True, STREAMS, PROBE
    return bot, client, item


def test_custom_streams_opens_a_selection_screen_without_downloading(tmp_path):
    """The tap that starts a transfer when nothing is needed was the bug."""
    async def run():
        bot, client, item = prepared_bot(tmp_path)
        uid = 31
        st = bot.state(uid)
        st.current_item = item
        st.queue.add(item)
        st.probe_data = PROBE
        downloaded = []

        async def never(u, it):
            downloaded.append(it)
            return None

        bot._download_url_item = never
        await bot.show_file_menu(1, uid, None)
        ev = Event(bot, uid, "stream:remove:custom")
        await bot.callback_router(ev, uid, "stream:remove:custom")
        return bot, st, downloaded, ev

    bot, st, downloaded, ev = asyncio.run(run())
    assert downloaded == [], "choosing tracks must not start a transfer"
    assert st.path is None, "the media must not be fetched to show the list"
    rendered = bot.client.rendered[-1]
    assert "Custom Streams" in rendered, rendered
    assert "Selected:" in rendered, rendered
    rows = buttons_of(bot, 31)
    labels = [t for row in rows for t in row]
    # Every track is offered, and the screen is not the main menu.
    assert any("aac" in t for t in labels), labels
    assert any("Apply" in t for t in labels), labels
    assert not any("Thumbnail Downloader" in t for t in labels), labels
    assert not any("out of date" in t for t, _ in ev.answers), ev.answers


def test_tapping_tracks_keeps_the_selection_on_screen(tmp_path):
    """Each tap re-renders the same screen with the choice visible."""
    async def run():
        bot, client, item = prepared_bot(tmp_path)
        uid = 31
        st = bot.state(uid)
        st.current_item = item
        st.queue.add(item)
        st.probe_data = PROBE
        await bot.show_file_menu(1, uid, None)
        ev = Event(bot, uid, "stream:remove:custom")
        await bot.callback_router(ev, uid, "stream:remove:custom")
        for data in ("streamtoggle:2", "streamtoggle:3", "streamtoggle:3"):
            st.view = "menu"
            ev = Event(bot, uid, data)
            await bot.callback_router(ev, uid, data)
        return bot, st

    bot, st = asyncio.run(run())
    assert st.custom_remove == {2}, st.custom_remove
    rendered = bot.client.rendered[-1]
    assert "Selected:" in rendered and "<b>1</b>" in rendered, rendered
    rows = buttons_of(bot, 31)
    marked = [t for row in rows for t in row if t.startswith("❌")]
    assert len(marked) == 1 and "jpn" in marked[0], rows
    assert st.path is None, "still no transfer while choosing"


def test_apply_without_a_selection_says_so_instead_of_guessing(tmp_path):
    async def run():
        bot, client, item = prepared_bot(tmp_path)
        uid = 31
        st = bot.state(uid)
        st.current_item = item
        st.queue.add(item)
        st.probe_data = PROBE
        await bot.show_file_menu(1, uid, None)
        ev = Event(bot, uid, "stream:remove:custom")
        await bot.callback_router(ev, uid, "stream:remove:custom")
        st.view = "menu"
        ev = Event(bot, uid, "streamapply")
        await bot.callback_router(ev, uid, "streamapply")
        return bot, st, ev

    bot, st, ev = asyncio.run(run())
    assert st.path is None, "nothing to apply means nothing to download"
    toasts = [t for t, _ in ev.answers] + ev.responses
    assert any("Select at least one" in t for t in toasts), toasts


def test_apply_downloads_the_file_and_removes_the_chosen_streams(tmp_path):
    """Apply is the point where the transfer starts and FFmpeg runs."""
    source = tmp_path / "movie.mkv"
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=880:duration=2",
            "-map", "0:v", "-map", "1:a", "-map", "2:a",
            "-c:v", "mpeg4", "-c:a", "aac", str(source),
        ],
        check=True,
    )

    async def run():
        bot, client, item = prepared_bot(tmp_path)
        uid = 31
        st = bot.state(uid)
        st.current_item = item
        st.queue.add(item)
        st.probe_data = PROBE
        await bot.show_file_menu(1, uid, None)

        async def fake_download(u, it):
            it.path = source
            return source

        bot._download_url_item = fake_download
        st.custom_remove = {2}
        st.streams = (await asyncio.to_thread(main_mod.ffprobe.safe_probe, source)).get("streams", [])
        ev = Event(bot, uid, "streamapply")
        await bot.callback_router(ev, uid, "streamapply")
        return bot, st

    bot, st = asyncio.run(run())
    assert st.path is not None and st.path.exists(), bot.client.rendered[-3:]
    assert "custom" in st.path.name, st.path.name
    remaining = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(st.path)],
        check=True, capture_output=True, text=True,
    ).stdout.split()
    assert remaining.count("audio") == 1, remaining


def test_download_panel_keeps_the_submenu_open(tmp_path):
    """While a transfer runs, an open submenu must not turn into the main menu."""
    async def run():
        bot, client, item = prepared_bot(tmp_path)
        uid = 31
        st = bot.state(uid)
        st.current_item = item
        st.queue.add(item)
        st.probe_data = PROBE
        await bot.show_file_menu(1, uid, None)
        # The user opens Stream Remover, then taps a track: the submenu is on
        # screen and the transfer starts.
        ev = Event(bot, uid, "video:streams")
        await bot.callback_router(ev, uid, "video:streams")
        assert st.submenu is True

        async def slow_download(u, it):
            for step in range(1, 4):
                it.advance(step * 512, 2048)
                await asyncio.sleep(0.05)
            target = tmp_path / "movie.mkv"
            target.write_bytes(b"x" * 2048)
            return target

        bot._download_url_item = slow_download
        bot.cfg.progress_interval = 0.5
        ev = Event(bot, uid, "stream:remove:1")
        await bot.callback_router(ev, uid, "stream:remove:1")
        # While it downloads the card must offer Cancel, not the main menu.
        during = [t for t in client.rendered if "Still downloading" in t]
        return bot, st, during, client

    bot, st, during, client = asyncio.run(run())
    assert during, client.rendered
    last = during[-1]
    assert "Thumbnail Downloader" not in last, last


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
