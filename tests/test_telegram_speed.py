"""Telegram transfer speed, and the progress line that has to show it.

The production report was a 2.95 GiB file crawling with ``ETA 2461s`` and no
speed at all, and a download panel that repainted the main menu over an open
submenu. These tests pin the faster ranged downloader, the progress text and
the repaint cadence.
"""
import asyncio
import sys
import time
from pathlib import Path

import pytest

from app.services.bulk import DOWNLOADING, DownloadQueue, QueueItem, render_queue
from app.services.telegram_download import (
    REQUEST_SIZE,
    MultipartFailed,
    download_media_multipart,
    download_ranged,
    plan_parts,
)
from app.utils.files import format_bytes_short, format_eta

main_mod = __import__("app.main", fromlist=["main"])


def test_iter_download_kwargs_match_the_installed_telethon():
    """The ranged downloader calls a real library; a signature change must fail here."""
    import inspect

    from telethon import TelegramClient

    try:
        params = inspect.signature(TelegramClient.iter_download).parameters
    except (AttributeError, TypeError, ValueError):  # pragma: no cover
        pytest.skip("Telethon is not installed")
    for name in ("offset", "limit", "chunk_size", "request_size", "file_size"):
        assert name in params, f"Telethon.iter_download no longer accepts {name}: {params}"
    assert "file" in params, "the media is still the first positional argument"


def test_plan_parts_covers_the_file_exactly():
    """Every byte belongs to exactly one worker, on a Telegram-aligned range."""
    size = 10 * 1024 * 1024 + 777
    ranges = plan_parts(size, 4)
    assert len(ranges) == 4
    assert ranges[0][0] == 0
    for offset, _length in ranges:
        assert offset % REQUEST_SIZE == 0, "Telegram only serves aligned ranges"
    covered = []
    for offset, length in ranges:
        covered.append((offset, offset + length))
    for previous, current in zip(covered, covered[1:]):
        assert previous[1] == current[0], covered
    assert covered[0][0] == 0
    assert covered[-1][1] >= size, covered
    # Evenly loaded: the slowest worker decides how long the whole file takes.
    lengths = [length for _, length in ranges]
    assert max(lengths) - min(lengths) <= REQUEST_SIZE, lengths


def test_plan_parts_degrades_gracefully():
    assert plan_parts(0, 4) == [(0, 0)]
    assert len(plan_parts(10 * 1024 * 1024, 1)) == 1
    # A file with fewer requests than parts gets one worker per request.
    assert len(plan_parts(2 * REQUEST_SIZE, 8)) == 2
    # Never more than the hard cap.
    assert len(plan_parts(1024 * REQUEST_SIZE, 64)) == 8


class FakeMessage:
    """Just enough of a Telethon message for the ranged downloader."""

    def __init__(self, media=None):
        self.media = media if media is not None else object()


class RangeClient:
    """A client whose ``iter_download`` serves real byte ranges.

    Mirrors Telethon's contract: the first argument is the media, ``offset``
    and ``limit`` (in requests) select the range, and it yields chunks.
    """

    def __init__(self, payload: bytes, drop_last_request: bool = False, stall: float = 0.0):
        self.payload = payload
        self.drop_last_request = drop_last_request
        self.stall = stall
        self.offsets: list[int] = []
        self.fallbacks = 0
        self._in_flight = 0
        self.max_parallel = 0

    async def download_media(self, message, file=None, progress_callback=None, **kw):
        self.fallbacks += 1
        if progress_callback:
            await progress_callback(0, len(self.payload))
        Path(file).write_bytes(self.payload)
        if progress_callback:
            await progress_callback(len(self.payload), len(self.payload))
        return file

    def iter_download(self, media, *, offset=0, limit=None, chunk_size=None,
                      request_size=REQUEST_SIZE, file_size=None, dc_id=None):
        self.offsets.append(offset)
        self._in_flight += 1
        self.max_parallel = max(self.max_parallel, self._in_flight)

        async def generator():
            try:
                requests = limit if limit is not None else (len(self.payload) // request_size) + 1
                if self.drop_last_request and offset:
                    requests = max(0, requests - 1)
                start = offset
                end = min(len(self.payload), offset + requests * request_size)
                while start < end:
                    if self.stall:
                        await asyncio.sleep(self.stall)
                    else:
                        await asyncio.sleep(0)
                    chunk = self.payload[start: start + request_size]
                    if not chunk:
                        break
                    start += len(chunk)
                    yield chunk
            finally:
                self._in_flight -= 1

        return generator()


def test_ranged_download_reassembles_the_file_byte_for_byte(tmp_path):
    payload = bytes((i * 7 + 3) % 256 for i in range(5 * 1024 * 1024 + 12345))
    dest = tmp_path / "out.bin"

    async def run():
        client = RangeClient(payload)
        seen: list[tuple[int, int]] = []

        async def progress(current, total):
            seen.append((current, total))

        await download_media_multipart(
            client, FakeMessage(), dest, file_size=len(payload), parts=4,
            progress=progress, min_bytes=0,
        )
        return client, seen

    client, seen = asyncio.run(run())
    assert dest.read_bytes() == payload, "ranged download must reproduce the file"
    assert len(set(client.offsets)) > 1, "the file must be read in several ranges"
    assert client.max_parallel > 1, "several requests must be in flight at once"
    assert client.fallbacks == 0, "the fast path should have been enough"
    # The reported progress is the aggregate and never goes backwards.
    assert seen, "progress must be reported"
    assert [c for c, _ in seen] == sorted(c for c, _ in seen)
    assert seen[-1][0] == len(payload)
    assert all(total == len(payload) for _, total in seen)


def test_ranged_download_falls_back_to_a_single_stream(tmp_path):
    """Any ranged problem must end in a correct file, not a corrupt one."""
    payload = b"z" * (3 * 1024 * 1024)
    dest = tmp_path / "out.bin"

    async def run():
        client = RangeClient(payload, drop_last_request=True)
        await download_media_multipart(
            client, FakeMessage(), dest, file_size=len(payload), parts=4, min_bytes=0,
        )
        return client

    client = asyncio.run(run())
    assert dest.read_bytes() == payload
    assert client.fallbacks == 1, "the ranged attempt must hand over to download_media"


def test_ranged_download_refuses_a_short_result(tmp_path):
    payload = b"q" * (2 * 1024 * 1024)
    dest = tmp_path / "out.bin"

    async def run():
        client = RangeClient(payload, drop_last_request=True)
        with pytest.raises(MultipartFailed):
            await            download_ranged(client, FakeMessage().media, dest, len(payload), parts=4)

    asyncio.run(run())


def test_small_files_skip_the_ranged_path(tmp_path):
    payload = b"tiny"
    dest = tmp_path / "out.bin"
    client = RangeClient(payload)

    asyncio.run(
        download_media_multipart(client, FakeMessage(), dest, file_size=len(payload), parts=4, min_bytes=24 * 1024 * 1024)
    )
    assert client.fallbacks == 1
    assert client.offsets == []
    assert dest.read_bytes() == payload


def test_cancellation_stops_the_ranged_download(tmp_path):
    payload = b"c" * (4 * 1024 * 1024)
    dest = tmp_path / "out.bin"
    cancel = asyncio.Event()

    async def run():
        # Each worker stalls between requests, so the cancel lands mid-transfer.
        client = RangeClient(payload, stall=0.05)
        task = asyncio.create_task(
            download_media_multipart(
                client, FakeMessage(), dest, file_size=len(payload), parts=4,
                progress=None, cancel_event=cancel, min_bytes=0,
            )
        )
        await asyncio.sleep(0.03)
        cancel.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not dest.exists() or dest.stat().st_size != len(payload)

    asyncio.run(run())


# ----------------------------------------------------------------------
# Progress line
# ----------------------------------------------------------------------
def test_eta_is_shown_in_minutes_not_seconds():
    assert format_eta(30) == "30s"
    assert format_eta(2461) == "41m"
    assert format_eta(120) == "2m"
    assert format_eta(3600) == "1h 00m"
    assert format_eta(4500) == "1h 15m"
    assert format_eta(None) == "--"


def test_short_sizes_stay_on_one_line():
    assert format_bytes_short(598 * 1024**2) == "598 MiB"
    assert format_bytes_short(5 * 1024**2) == "5.0 MiB"
    assert format_bytes_short(512) == "512 B"


def test_progress_line_has_speed_sizes_and_eta():
    queue = DownloadQueue()
    item = queue.add(QueueItem(kind="telegram", label="1080p.mkv", chat_id=1, status=DOWNLOADING))
    item.advance(1 * 1024**2, 64 * 1024**2)
    time.sleep(0.2)
    item.advance(20 * 1024**2, 64 * 1024**2)

    text = render_queue(queue, bulk=False)
    assert "1080p.mkv" in text
    assert "20 MiB / 64.00 MiB" in text, text
    assert "⚡" in text and "/s" in text, text
    # ETA in minutes: at ~95 MiB/s the remaining 44 MiB is under a minute.
    assert "left" in text and "s left" in text, text
    assert "ETA 2461s" not in text


def test_panel_does_not_repaint_more_often_than_the_interval(tmp_path):
    """Two renderers + a short interval must not turn into a flood of edits."""
    from tests.test_ui_semantics import FaithfulClient
    from tests.test_download_pipeline import make_bot

    async def run():
        bot = make_bot(tmp_path, FaithfulClient())
        uid = 21
        bot.cfg.progress_interval = 3.0
        st = bot.state(uid)
        st.view = "queue"
        item = st.queue.add(QueueItem(kind="url", label="a.mkv", chat_id=1, url="https://x/a"))
        st.queue.claim()
        for _ in range(6):
            # Both the waiting loop and the bulk ticker would paint here.
            await bot._render_queue_panel(uid, 1, force=True)
        return bot.client.rendered

    rendered = asyncio.run(run())
    assert len(rendered) == 1, f"expected one repaint, got {len(rendered)}: {rendered}"


def test_progress_panel_keeps_an_open_submenu(tmp_path):
    """Downloading must not replace a submenu with the main menu."""
    from tests.test_ui_semantics import FaithfulClient
    from tests.test_download_pipeline import make_bot

    async def run():
        bot = make_bot(tmp_path, FaithfulClient())
        uid = 22
        st = bot.state(uid)
        st.submenu = True  # e.g. the Stream Remover screen is open
        st.queue.add(QueueItem(kind="url", label="a.mkv", chat_id=1, url="https://x/a"))
        st.queue.claim()
        await bot._render_queue_panel(uid, 1, force=True)
        return bot.state(uid), bot.client.messages[(1, st.card_message_id)]

    st, card = asyncio.run(run())
    labels = [getattr(b, "text", "") for row in (card.buttons or []) for b in row]
    assert not any("Thumbnail" in t for t in labels), labels
    assert any("Cancel" in t for t in labels), labels


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
