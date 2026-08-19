import asyncio
import sys
import types
from pathlib import Path

import aiohttp
from aiohttp import web

from app.services.downloader import download_url
from app.config import _detect_public_base_url


def test_plain_url_is_recognized_before_telegram_webpage_media():
    source = Path("app/main.py").read_text()
    assert 're.match(r"^https?://\\S+$", text, re.I)' in source
    assert source.index('elif re.match(r"^https?://\\S+$", text, re.I):') < source.index('elif event.message.media:')


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
        out = await download_url(f"http://127.0.0.1:{port}/file", tmp_path)
        assert out.name == "example.mkv"
        assert out.read_bytes() == data
    finally:
        await runner.cleanup()


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

    monkeypatch.setattr(main_mod.ffprobe, "probe", lambda p: {"streams": [{"codec_type": "video"}] if p == video else [{"codec_type": "subtitle"}]})
    assert main_mod.MediaToolsBot.video_media(None, State()) == video


def test_cancel_clears_pending_and_keyboard():
    source = Path("app/main.py").read_text()
    assert 'async def cancel(self, uid, chat_id, source_message=None, admin: bool = False)' in source
    assert 'await self.clear_status_message(uid, delete=True)' in source
    assert 'send_start_info(chat_id, uid' in source
    assert 'st.pending = None' in source


def test_pending_workflows_win_over_generic_url_handler():
    source = Path("app/main.py").read_text()
    pending = 'elif self.state(uid).pending:'
    generic = 'elif re.match(r"^https?://\\S+$", text, re.I):'
    assert pending in source
    assert source.index(pending) < source.index(generic)
