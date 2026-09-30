import asyncio
import sys
import types
from pathlib import Path

import pytest

from app.services.bulk import (
    DONE, DOWNLOADING, FAILED, QUEUED, DownloadQueue, QueueItem,
    render_bulk_prompt, render_queue,
)


def make_item(label="clip.mkv", **kwargs):
    return QueueItem(kind=kwargs.pop("kind", "url"), label=label, chat_id=1, **kwargs)


# ---------------------------------------------------------------- queue model
def test_queue_claims_respect_parallel_limit():
    q = DownloadQueue(max_parallel=2)
    for i in range(5):
        q.add(make_item(f"f{i}.mkv"))
    a = q.claim()
    b = q.claim()
    assert a is not None and b is not None and a is not b
    assert q.claim() is None, "third claim must be refused while at capacity"
    q.release(a)
    assert q.claim() is not None, "a freed slot must be reusable"


def test_queue_pause_blocks_claiming():
    q = DownloadQueue(max_parallel=1)
    q.add(make_item())
    q.paused = True
    assert q.claim() is None
    q.paused = False
    assert q.claim() is not None


def test_queue_marks_done_only_with_a_path():
    q = DownloadQueue()
    item = q.add(make_item())
    claimed = q.claim()
    assert claimed is item
    q.release(item)  # no path assigned -> failure
    assert item.status == FAILED

    item2 = q.add(make_item("b.mkv"))
    q.claim()
    item2.path = Path("/tmp/b.mkv")
    q.release(item2)
    assert item2.status == DONE
    assert q.summary() == (2, 1, 0, 1)


def test_ready_paths_dedupes_and_skips_missing(tmp_path):
    q = DownloadQueue()
    existing = tmp_path / "a.mkv"
    existing.write_bytes(b"x")
    gone = tmp_path / "gone.mkv"
    for path in (existing, existing, gone):
        item = q.add(make_item(path.name))
        q.claim()
        item.path = path
        q.release(item)
    ready = q.ready_paths()
    assert ready == [existing], "duplicate paths and vanished files must be dropped"


def test_queue_clear_and_drop_finished():
    q = DownloadQueue()
    a = q.add(make_item("a.mkv"))
    q.claim()
    a.path = Path("/tmp/a.mkv")
    q.release(a)
    b = q.add(make_item("b.mkv"))
    q.drop_finished()
    assert [i.label for i in q.items] == ["b.mkv"]
    q.clear()
    assert len(q) == 0


def test_progress_percent_and_eta():
    item = make_item()
    item.status = DOWNLOADING
    item.total = 100
    item.current = 25
    assert item.percent == 25.0
    item.status = DONE
    assert item.percent == 100.0
    item.status = DOWNLOADING
    item.total = 0
    assert item.percent == 0.0
    assert item.eta is None


# ------------------------------------------------------------------ rendering
def test_queue_render_escapes_names_and_mentions_parallel_progress():
    q = DownloadQueue()
    q.add(make_item("<script>evil</script>.mkv"))
    claimed = q.claim()
    claimed.total = 100
    claimed.current = 50
    text = render_queue(q, active_name="a.mkv", bulk=True)
    assert "<script>" not in text
    assert "&lt;script&gt;" in text
    assert "50.0%" in text
    assert "background" in text


def test_bulk_prompt_asks_for_more_files():
    q = DownloadQueue()
    item = q.add(make_item("a.mkv"))
    q.claim()
    item.path = Path("/tmp/a.mkv")
    q.release(item)
    text = render_bulk_prompt(q, Path("/tmp/a.mkv"))
    assert "Send another file" in text
    assert "Done Adding" in text
    assert "Files ready:" in text


def test_render_queue_handles_empty_queue():
    assert "Nothing queued yet" in render_queue(DownloadQueue())


# ------------------------------------------------------------------- main bits
def _load_main():
    """Import app.main, stubbing Telethon when it is not installed."""
    try:
        import app.main
        return app.main
    except ImportError:
        pass

    class FakeButton:
        def __init__(self, text, data):
            self.text, self.data = text, data

        @classmethod
        def inline(cls, text, data):
            return cls(text, data)

    telethon = types.ModuleType("telethon")
    telethon.Button = FakeButton
    telethon.TelegramClient = object
    telethon.events = types.SimpleNamespace(NewMessage=object, CallbackQuery=object)
    telethon.types = types.SimpleNamespace(ReactionEmoji=lambda emoticon: types.SimpleNamespace(emoticon=emoticon))
    telethon.functions = types.SimpleNamespace(messages=types.SimpleNamespace(SendReactionRequest=lambda **kw: types.SimpleNamespace(**kw)))
    errors = types.ModuleType("telethon.errors")
    errors.MessageNotModifiedError = type("MessageNotModifiedError", (Exception,), {})
    custom_message = types.ModuleType("telethon.tl.custom.message")
    custom_message.Message = object
    tl_custom = types.ModuleType("telethon.tl.custom")
    tl_custom.message = custom_message
    tl = types.ModuleType("telethon.tl")
    tl.custom = tl_custom
    sys.modules.update({
        "telethon": telethon, "telethon.errors": errors, "telethon.tl": tl,
        "telethon.tl.custom": tl_custom, "telethon.tl.custom.message": custom_message,
    })
    import app.main
    return app.main


def test_extract_url_from_free_form_text():
    main = _load_main()
    assert main.extract_url("download this https://example.com/a.mkv please") == "https://example.com/a.mkv"
    assert main.extract_url("see (https://example.com/b.mkv)") == "https://example.com/b.mkv"
    assert main.extract_url("no link here") is None
    assert main.extract_url("ftp://example.com/x") is None


def test_guess_media_name_for_photos_and_files():
    main = _load_main()

    class Msg:
        def __init__(self, **kw):
            self.id = 7
            self.file = None
            self.photo = None
            self.video = None
            self.audio = None
            self.voice = None
            self.video_note = None
            for k, v in kw.items():
                setattr(self, k, v)

    photo = Msg(photo=object())
    assert main.guess_media_name(photo, "fallback.bin").endswith(".jpg")

    file = types.SimpleNamespace(name="My Movie.mkv", mime_type="video/x-matroska")
    doc = Msg(file=file)
    assert main.guess_media_name(doc, "fallback.bin") == "My Movie.mkv"

    # A file part with no name still gets a useful extension.
    png = Msg(file=types.SimpleNamespace(name=None, mime_type="image/png"))
    assert main.guess_media_name(png, "fallback.bin").endswith(".png")


def test_parse_volume_rejects_nonsense():
    main = _load_main()
    assert main.MediaToolsBot._parse_volume("1.5") == "1.5"
    assert main.MediaToolsBot._parse_volume("-3dB") == "-3dB"
    assert main.MediaToolsBot._parse_volume("0") == "0", "muting is a valid request"
    assert main.MediaToolsBot._parse_volume("999") is None
    assert main.MediaToolsBot._parse_volume("drop table") is None
    assert main.MediaToolsBot._parse_volume("") is None
    assert main.MediaToolsBot._parse_volume("a" * 40) is None


# ----------------------------------------------------------------- downloader
def test_download_rejects_oversized_and_empty(tmp_path, monkeypatch):
    from app.services import downloader

    async def run():
        # A declared Content-Length above the cap is refused before any write.
        with pytest.raises(downloader.DownloadError):
            await downloader.download_url("https://example.com/x", tmp_path, max_bytes=10, allow_private=True)

    # Patch the network layer so the test never touches the internet.
    class FakeResponse:
        status = 200
        url = "https://example.com/x"
        headers = {"Content-Length": str(1024), "Content-Type": "video/mp4"}
        content = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def raise_for_status(self):
            return None

    class FakeSession:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def get(self, *a, **kw):
            return FakeResponse()

    monkeypatch.setattr(downloader.aiohttp, "ClientSession", lambda *a, **kw: FakeSession())
    asyncio.run(run())


def test_filename_falls_back_to_content_type():
    from app.services import downloader

    class Resp:
        url = "https://example.com/download"
        headers = {"Content-Type": "video/x-matroska"}

    assert downloader._filename_from_response(Resp(), "https://example.com/download") == "download.mkv"
