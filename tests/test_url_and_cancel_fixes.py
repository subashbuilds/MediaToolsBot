import asyncio
import sys
import types
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web

from app.services.downloader import download_url
from app.config import _detect_public_base_url


def test_plain_url_is_recognized_before_telegram_webpage_media():
    """A URL must be read as a URL even when Telegram attaches a preview.

    ``message.media`` is set for the invisible WebPage preview on a text
    message, so using it as the "is this media?" test made the bot queue a
    phantom ``telegram_<id>.bin`` instead of the link the user sent.
    """
    main_mod = __import__("app.main", fromlist=["main"])
    assert main_mod.is_media_message(_message_with_preview()) is False
    assert main_mod.is_media_message(_message_with_file()) is True


def test_public_base_url_auto_detection(monkeypatch):
    for key in ("PUBLIC_BASE_URL", "RENDER_EXTERNAL_URL", "RAILWAY_PUBLIC_DOMAIN", "APP_URL", "HEROKU_APP_NAME"):
        monkeypatch.delenv(key, raising=False)
    assert _detect_public_base_url() is None

    monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "example.up.railway.app")
    assert _detect_public_base_url() == "https://example.up.railway.app"

    monkeypatch.setenv("PUBLIC_BASE_URL", "https://custom.example/")
    assert _detect_public_base_url() == "https://custom.example"


async def _url_download_roundtrip(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    data = b"media-data-" * 10000
    app = web.Application()

    async def handler(request):
        return web.Response(body=data, headers={"Content-Disposition": 'attachment; filename="example.mkv"'})

    app.router.add_get("/file", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        # 127.0.0.1 is a private address, which the downloader blocks by
        # default so a user cannot make the bot fetch internal services.
        # The local test server therefore opts in explicitly.
        out = await download_url(f"http://127.0.0.1:{port}/file", tmp_path, allow_private=True)
        assert out.name == "example.mkv"
        assert out.read_bytes() == data
    finally:
        await runner.cleanup()


def test_url_downloader_blocks_internal_addresses(tmp_path):
    from app.services.downloader import DownloadError, download_url

    for url in ("http://127.0.0.1:8080/secret", "http://169.254.169.254/latest/meta-data/", "file:///etc/passwd"):
        with pytest.raises(DownloadError):
            asyncio.run(download_url(url, tmp_path))


def test_url_downloader_real_local_http(tmp_path):
    asyncio.run(_url_download_roundtrip(tmp_path))


def test_video_source_falls_back_to_original_when_current_is_subtitle(tmp_path, monkeypatch):
    # Import main without requiring Telethon to be installed in this isolated test runner.
    class FakeButton:
        @classmethod
        def inline(cls, *args, **kwargs):
            return object()

    class FakeClient:
        def __init__(self, *args, **kwargs): pass
        def add_event_handler(self, *args, **kwargs): pass

    fake_telethon = types.ModuleType("telethon")
    fake_telethon.Button = FakeButton
    fake_telethon.TelegramClient = FakeClient
    fake_telethon.events = types.SimpleNamespace(NewMessage=object, CallbackQuery=object)
    errors = types.ModuleType("telethon.errors")
    errors.MessageNotModifiedError = type("MessageNotModifiedError", (Exception,), {})
    custom_message = types.ModuleType("telethon.tl.custom.message")
    custom_message.Message = object
    tl_custom = types.ModuleType("telethon.tl.custom")
    tl_custom.message = custom_message
    tl = types.ModuleType("telethon.tl")
    tl.custom = tl_custom
    sys.modules.update({"telethon": fake_telethon, "telethon.errors": errors, "telethon.tl": tl,
                        "telethon.tl.custom": tl_custom, "telethon.tl.custom.message": custom_message})

    import app.main as main_mod

    video = tmp_path / "source.mkv"
    subtitle = tmp_path / "source.srt"
    video.write_bytes(b"video")
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nhello\n")

    class State:
        path = subtitle
        source_path = video
        root_path = video

    monkeypatch.setattr(
        main_mod.ffprobe,
        "safe_probe",
        lambda p: {"streams": [{"codec_type": "video"}] if p == video else [{"codec_type": "subtitle"}]},
    )
    # video_media is async and probes off the event loop. Bind the helper to a
    # real instance, exactly as production does.
    bot = object.__new__(main_mod.MediaToolsBot)
    assert asyncio.run(bot.video_media(State())) == video


def test_cancel_clears_pending_and_keyboard():
    source = Path("app/main.py").read_text()
    assert 'async def cancel(self, uid, chat_id, source_message=None, admin: bool = False)' in source
    assert 'await self.clear_status_message(uid, delete=True)' in source
    assert 'send_start_info(chat_id, uid' in source
    assert 'st.pending = None' in source


def test_pending_workflows_win_over_generic_url_handler():
    """An active prompt (trim, rename, URL uploader, ...) owns the next message."""
    source = Path("app/main.py").read_text()
    pending = "elif pending:"
    generic = "elif links:"
    assert pending in source
    assert source.index(pending) < source.index(generic)


def _message_with_preview():
    """A text message that Telegram decorated with a webpage preview."""
    return types.SimpleNamespace(
        raw_text="Sardar 2 (2026) 1.6GB ESub.mkv\nClick Here",
        text="Sardar 2 (2026) 1.6GB ESub.mkv\nClick Here",
        media=object(), file=None,
        photo=None, video=None, audio=None, voice=None, video_note=None, document=None,
        entities=[types.SimpleNamespace(url="https://cdn.example/fast.mkv")],
    )


def _message_with_file():
    return types.SimpleNamespace(
        raw_text="", text="", media=object(),
        file=types.SimpleNamespace(name="movie.mkv", size=10),
        photo=None, video=None, audio=None, voice=None, video_note=None,
        document=object(), entities=None,
    )


def test_hidden_link_entities_are_extracted():
    """A 'Click Here' link is a real download request, not an empty message."""
    main_mod = __import__("app.main", fromlist=["main"])
    assert main_mod.message_urls(_message_with_preview()) == ["https://cdn.example/fast.mkv"]
    # A bare link in the text is still found, and not duplicated by entities.
    plain = types.SimpleNamespace(
        raw_text="https://cdn.example/a.mkv", text="https://cdn.example/a.mkv",
        media=None, file=None,
        photo=None, video=None, audio=None, voice=None, video_note=None, document=None,
        entities=[types.SimpleNamespace(url="https://cdn.example/a.mkv")],
    )
    assert main_mod.message_urls(plain) == ["https://cdn.example/a.mkv"]
