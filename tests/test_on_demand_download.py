"""Media must be fetched only when the user picks an action.

Two production complaints are pinned here:

  * the bot started downloading a file the moment it arrived, so a link or a
    large upload was transferred whether or not the user wanted it, and Media
    Information had to wait for the whole file;
  * "Removing stream failed: TypeError: ...<lambda>() takes 0 positional
    arguments but 1 was given" - ``execute`` calls its job with the progress
    reporter, and several jobs took no arguments at all.
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


def make_media(path: Path) -> Path:
    """A tiny MKV with one video and two audio tracks."""
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=880:duration=2",
            "-map", "0:v", "-map", "1:a", "-map", "2:a",
            "-c:v", "mpeg4", "-c:a", "aac", str(path),
        ],
        check=True,
    )
    return path


def audio_streams(path: Path) -> int:
    import json
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout
    return sum(1 for s in json.loads(out)["streams"] if s.get("codec_type") == "audio")


class Event:
    """CallbackQuery stand-in that edits the card it is attached to."""

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


# ----------------------------------------------------------------------
# 1. The reported TypeError
# ----------------------------------------------------------------------
def test_invoke_job_accepts_both_job_arities():
    """``execute`` hands every job a reporter; jobs may or may not want one."""
    reporter = object()

    assert main_mod._invoke_job(lambda: "no arguments", reporter) == "no arguments"

    def with_reporter(cb=None):
        return cb

    assert main_mod._invoke_job(with_reporter, reporter) is reporter

    # ...and through the same thread call execute() uses.
    assert asyncio.run(asyncio.to_thread(main_mod._invoke_job, lambda: 7, reporter)) == 7


def test_removing_a_stream_succeeds(tmp_path):
    """The exact production path: tap a track in "Stream Remover"."""
    source = make_media(tmp_path / "movie.mkv")
    assert audio_streams(source) == 2

    async def run():
        client = FaithfulClient()
        bot = make_bot(tmp_path, client)
        uid = 11
        st = bot.state(uid)
        st.path = st.source_path = st.root_path = source
        st.outputs = [source]
        st.streams = (await asyncio.to_thread(main_mod.ffprobe.safe_probe, source)).get("streams", [])
        assert st.streams, "the probe must find the tracks to offer them"
        await bot.show_file_menu(1, uid, source, fresh=True)

        ev = Event(bot, uid, "stream:remove:2")
        await bot.callback_router(ev, uid, "stream:remove:2")
        return bot, st

    bot, st = asyncio.run(run())
    failed = [t for t in bot.client.rendered if "failed" in t.lower()]
    assert not failed, bot.client.rendered
    assert not any("TypeError" in t for t in bot.client.rendered)
    out = st.path
    assert out is not None and out.exists(), "removing a track must produce a file"
    assert out != source
    assert audio_streams(out) == 1, "exactly one audio track should have been removed"


# ----------------------------------------------------------------------
# 2. Nothing is transferred until an action asks for it
# ----------------------------------------------------------------------
def test_input_does_not_start_a_transfer(tmp_path):
    async def run():
        client = FaithfulClient()
        bot = make_bot(tmp_path, client)
        uid = 12
        started = []

        async def never(u, item):
            started.append(item)
            return None

        bot._download_url_item = never
        await bot.enqueue_input(1, uid, QueueItem(kind="url", label="big.mkv", chat_id=1, url="https://x/big.mkv"))
        await asyncio.sleep(0.2)
        card = client.rendered[-1]
        return bot, started, card

    bot, started, card = asyncio.run(run())
    assert started == [], "arrival must not start a download"
    assert "big.mkv" in card
    assert "Nothing has been downloaded yet" in card
    assert bot.state(12).path is None


def test_media_info_needs_no_download(tmp_path):
    """A link's header already describes the file well enough for the page."""
    async def run():
        client = FaithfulClient()
        bot = make_bot(tmp_path, client)
        uid = 13
        started = []

        async def never(u, item):
            started.append(item)
            return None

        bot._download_url_item = never
        probe = {
            "format": {"format_name": "matroska", "duration": "2.000000", "size": "64087"},
            "streams": [
                {"index": 0, "codec_type": "video", "codec_name": "mpeg4"},
                {"index": 1, "codec_type": "audio", "codec_name": "aac", "tags": {"language": "eng"}},
            ],
        }
        item = QueueItem(kind="url", label="movie.mkv", chat_id=1, url="https://x/movie.mkv")
        await bot.enqueue_input(1, uid, item)
        st = bot.state(uid)
        st.current_item = item
        item.probed = True
        st.probe_data = probe

        pages = []

        async def fake_page(data, name, size_text, token=None, packet_sizes=None):
            pages.append((data, name, size_text))
            return "https://telegra.ph/test"

        original = main_mod.create_info_page
        main_mod.create_info_page = fake_page
        try:
            await bot.run_info(1, uid)
        finally:
            main_mod.create_info_page = original
        return started, pages, bot

    started, pages, bot = asyncio.run(run())
    assert not started, "Media Information must not download the media"
    assert pages, "the page must still be produced"
    data, name, size_text = pages[0]
    assert name == "movie.mkv"
    assert size_text.endswith("B"), size_text
    assert [s["codec_type"] for s in data["streams"]] == ["video", "audio"]
    assert any("telegra.ph/test" in t for t in bot.client.rendered), bot.client.rendered


def test_stream_menu_lists_tracks_without_downloading(tmp_path):
    async def run():
        client = FaithfulClient()
        bot = make_bot(tmp_path, client)
        uid = 14
        started = []

        async def never(u, item):
            started.append(item)
            return None

        bot._download_url_item = never
        item = QueueItem(kind="url", label="movie.mkv", chat_id=1, url="https://x/movie.mkv")
        await bot.enqueue_input(1, uid, item)
        st = bot.state(uid)
        st.probe_data = {
            "format": {"format_name": "matroska"},
            "streams": [
                {"index": 0, "codec_type": "video", "codec_name": "mpeg4"},
                {"index": 1, "codec_type": "audio", "codec_name": "aac", "tags": {"language": "eng"}},
                {"index": 2, "codec_type": "audio", "codec_name": "aac", "tags": {"language": "jpn"}},
            ],
        }
        await bot.stream_menu(1, uid, "remove")
        return bot, started, item

    bot, started, item = asyncio.run(run())
    assert not started, "opening the track list must not download anything"
    labels = [
        getattr(b, "text", "")
        for row in (bot.client.messages[(1, bot.state(14).card_message_id)].buttons or [])
        for b in row
    ]
    assert any("aac" in t for t in labels), labels
    assert bot.state(14).current_item is item


def test_stream_menu_downloads_only_when_there_is_no_header(tmp_path):
    """An unreadable header still has to produce a working menu."""
    source = make_media(tmp_path / "movie.mkv")

    async def run():
        client = FaithfulClient()
        bot = make_bot(tmp_path, client)
        uid = 15
        st = bot.state(uid)
        item = QueueItem(kind="url", label="movie.mkv", chat_id=1, url="https://x/movie.mkv")
        st.current_item = item
        st.queue.add(item)

        async def fake_download(u, it):
            it.path = source
            return source

        bot._download_url_item = fake_download
        await bot.stream_menu(1, uid, "extract")
        return bot, st

    bot, st = asyncio.run(run())
    assert st.path == source, "the action must have fetched the file"
    assert len(st.streams) == 3
    labels = [
        getattr(b, "text", "")
        for row in (bot.client.messages[(1, st.card_message_id)].buttons or [])
        for b in row
    ]
    assert any("Extract" in t or "audio" in t for t in labels), labels


# ----------------------------------------------------------------------
# 3. A Telegram file is described from a short prefix, not in full
# ----------------------------------------------------------------------
class HeadClient(FaithfulClient):
    """Client that can only stream a bounded prefix, like Telethon can.

    ``TelegramClient.iter_download(media, ...)`` yields chunks; the caller is
    responsible for writing them, and this fake mirrors that exactly.
    """

    def __init__(self, payload: bytes):
        super().__init__()
        self.payload = payload
        self.requested_limit = None
        self.requested_size = None
        self.requested_media = None

    # The parameter names/order mirror TelegramClient.iter_download exactly, so
    # calling it the way Telethon does not raise "multiple values for 'file'".
    async def iter_download(self, file, *, offset=0, stride=None, limit=None,
                            chunk_size=None, request_size=524288, file_size=None, dc_id=None):
        self.requested_media = file
        self.requested_limit = limit
        self.requested_size = request_size
        step = request_size or len(self.payload)
        remaining = step * (limit or 0)
        for start in range(0, min(len(self.payload), remaining), step):
            yield self.payload[start: start + step]


def test_head_probe_reads_a_prefix_and_cleans_up(tmp_path):
    source = make_media(tmp_path / "movie.mkv")
    payload = source.read_bytes()
    message = FakeMessage(message_id=4, name="movie.mkv", payload=payload)

    async def run():
        client = HeadClient(payload)
        bot = make_bot(tmp_path, client)
        uid = 16
        item = QueueItem(
            kind="telegram", label="movie.mkv", chat_id=1, message_id=4,
            message=message, expected_size=len(payload),
        )
        st = bot.state(uid)
        st.current_item = item
        data = await bot._probe_head(uid, item)
        leftovers = list((bot.cfg.work_dir / str(uid) / "probe").glob("*"))
        return bot, client, item, data, st, leftovers

    bot, client, item, data, st, leftovers = asyncio.run(run())
    assert client.requested_limit is not None
    assert client.requested_size == main_mod.HEAD_PROBE_CHUNK
    assert client.requested_media is message, "the message itself must be the source"
    assert client.requested_limit * client.requested_size <= main_mod.HEAD_PROBE_BYTES
    assert [s["codec_type"] for s in data["streams"]] == ["video", "audio", "audio"]
    # The prefix is a temporary artefact and must not be left behind.
    assert leftovers == [], leftovers
    # The same data feeds the live menu without a download.
    item.probed, item.streams = True, data["streams"]
    assert item.audio_count == 2 and item.video_count == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
