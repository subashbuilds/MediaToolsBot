"""Metadata-before-download and merge ordering, tested against real tools."""
import asyncio
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

from tests.test_download_pipeline import make_bot
from tests.test_ui_semantics import FaithfulClient

main_mod = __import__("app.main", fromlist=["main"])

ffmpeg = shutil.which("ffmpeg")
ffprobe = shutil.which("ffprobe")
requires_ffmpeg = pytest.mark.skipif(
    not (ffmpeg and ffprobe), reason="ffmpeg/ffprobe not installed"
)


@pytest.fixture(scope="module")
def multitrack_file(tmp_path_factory):
    """A real MKV with 2 video and 2 audio tracks."""
    out = tmp_path_factory.mktemp("media") / "multi.mkv"
    cp = subprocess.run(
        [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=duration=1:size=160x120:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-f", "lavfi", "-i", "sine=frequency=880:duration=1",
            "-map", "0:v", "-map", "1:a", "-map", "2:a",
            "-c:v", "libx264", "-c:a", "aac", "-shortest",
            str(out),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert cp.returncode == 0, cp.stderr
    return out


@pytest.fixture
def http_file(multitrack_file):
    """Serve the fixture over HTTP so the remote probe has something to read."""
    import functools
    import http.server
    import threading

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(multitrack_file.parent))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[0], server.server_address[1]
    yield f"http://{host}:{port}/{multitrack_file.name}"
    server.shutdown()


# ---------------------------------------------------------------- downloads
def test_download_has_no_size_cap_by_default():
    """Telegram's 2 GiB limit is an *upload* limit and must not gate downloads."""
    from app.services import downloader

    assert downloader.MAX_DOWNLOAD_BYTES == 0, (
        "downloads must be unrestricted unless an operator opts in"
    )


def test_config_default_allows_unlimited_downloads(monkeypatch):
    from app.config import DEFAULT_MAX_DOWNLOAD_MB

    assert DEFAULT_MAX_DOWNLOAD_MB == 0


def test_telegram_upload_chunks_above_2gib(tmp_path):
    from app.services.telegram_upload import MAX_TELEGRAM_CHUNK, chunk_file

    # Everything below the Telegram ceiling stays a single upload.
    small = tmp_path / "small.bin"
    small.write_bytes(b"x" * 1024)
    assert chunk_file(small, tmp_path / "chunks") == [small]

    # The split threshold matches Telegram's documented 2 GiB bot limit.
    assert 1.9 * 1024**3 < MAX_TELEGRAM_CHUNK < 2.0 * 1024**3


def test_gofile_upload_does_not_split():
    """GoFile has no 2 GiB ceiling, so nothing may be chunked for it."""
    from app.services import gofile

    src = Path("app/main.py").read_text()
    gofile_tree = src[src.index("async def _gofile_upload_tree"):]
    gofile_tree = gofile_tree[: gofile_tree.index("async def run_gofile")]
    assert "chunk_file" not in gofile_tree, "GoFile uploads must not be split"


# ---------------------------------------------------------------- metadata
@requires_ffmpeg
def test_remote_metadata_available_before_download_finishes(http_file):
    """Track metadata must be known from the link, not only after the transfer."""
    async def run():
        from app.services.bulk import QueueItem

        bot = make_bot(tmp_path := Path("/tmp/mdtest"), FaithfulClient())
        item = QueueItem(kind="url", label="multi.mkv", chat_id=1, url=http_file)
        await bot._probe_item_metadata(1, item)

        assert item.streams, "no streams were read from the URL"
        assert item.video_count == 1
        assert item.audio_count == 2, item.streams
        assert item.probed is True
        return item

    item = asyncio.run(run())
    assert item.audio_count == 2


@requires_ffmpeg
def test_local_probe_populates_tracks_after_download(multitrack_file):
    async def run():
        from app.services.bulk import QueueItem

        bot = make_bot(Path("/tmp/mdtest2"), FaithfulClient())
        item = QueueItem(kind="telegram", label="multi.mkv", chat_id=1)
        item.path = multitrack_file
        await bot._probe_downloaded_metadata(1, item)
        return item

    item = asyncio.run(run())
    assert item.audio_count == 2
    assert item.video_count == 1


def test_queue_item_reports_track_counts():
    from app.services.bulk import QueueItem

    item = QueueItem(
        kind="url", label="a.mkv", chat_id=1,
        streams=[
            {"codec_type": "video"}, {"codec_type": "audio"},
            {"codec_type": "audio"}, {"codec_type": "subtitle"},
        ],
    )
    assert item.video_count == 1
    assert item.audio_count == 2


def test_queue_panel_shows_track_counts_while_downloading():
    from app.services.bulk import DOWNLOADING, QueueItem, render_queue
    from app.services.bulk import DownloadQueue

    q = DownloadQueue()
    item = QueueItem(
        kind="url", label="movie.mkv", chat_id=1, status=DOWNLOADING,
        streams=[{"codec_type": "video"}, {"codec_type": "audio"}],
    )
    q.items.append(item)
    text = render_queue(q)
    assert "[1v+1a]" in text, text


# ---------------------------------------------------------------- merge
def test_merge_status_text_exists_and_lists_order(tmp_path):
    """merge_status_text was called from five places and never defined."""
    bot = make_bot(tmp_path, FaithfulClient())
    st = bot.state(1)
    a, b = tmp_path / "first.mkv", tmp_path / "second.mkv"
    a.write_bytes(b"a" * 10)
    b.write_bytes(b"b" * 20)
    st.merge_inputs = [a.resolve(), b.resolve()]

    text = bot.merge_status_text(st)
    assert "Merge Tracks" in text
    assert "first.mkv" in text and "second.mkv" in text
    # The order must be explicit and positional.
    assert text.index("1.") < text.index("2.")


def test_merge_reorder_moves_tracks(tmp_path):
    bot = make_bot(tmp_path, FaithfulClient())
    st = bot.state(1)
    a, b, c = (tmp_path / f"{n}.mkv" for n in "abc")
    for f in (a, b, c):
        f.write_bytes(b"x" * 4)
    st.merge_inputs = [a.resolve(), b.resolve(), c.resolve()]

    assert bot._move_merge_item(st, 2, -1) is True
    assert st.merge_inputs == [a.resolve(), c.resolve(), b.resolve()]
    assert bot._move_merge_item(st, 2, 1) is False, "cannot move past the end"
    assert bot._move_merge_item(st, -1, 1) is False


def test_merge_order_keyboard_offers_controls():
    from app.ui.keyboards import merge_order_menu, merge_pick_menu

    rows = merge_order_menu(3)
    payloads = [getattr(b, "data", b"") for row in rows for b in row]
    assert b"merge:up:0" in payloads
    assert b"merge:down:1" in payloads
    assert b"merge:finish" in payloads

    pick = merge_pick_menu(1, 3)
    pick_payloads = [getattr(b, "data", b"") for row in pick for b in row]
    for expected in (b"merge:top", b"merge:bottom", b"merge:drop", b"merge:order"):
        assert expected in pick_payloads


def test_merge_screen_offers_order_once_two_tracks(tmp_path):
    bot = make_bot(tmp_path, FaithfulClient())
    st = bot.state(1)
    a, b = tmp_path / "a.mkv", tmp_path / "b.mkv"
    for f in (a, b):
        f.write_bytes(b"x" * 4)

    st.merge_inputs = [a.resolve()]
    single = [getattr(b, "data", b"") for row in bot._merge_buttons(st) for b in row]
    assert b"merge:finish" in single
    assert b"merge:up:0" not in single

    st.merge_inputs = [a.resolve(), b.resolve()]
    both = [getattr(x, "data", b"") for row in bot._merge_buttons(st) for x in row]
    assert b"merge:up:0" in both and b"merge:down:0" in both


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))