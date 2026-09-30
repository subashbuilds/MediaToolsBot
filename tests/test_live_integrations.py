"""Opt-in checks that use the real credentials from the environment.

These hit the services the bot actually depends on - a local HTTP origin, real
FFmpeg/FFprobe, the real Telegraph API and the real GoFile API - with the
values from ``.env``, so an integration break shows up here instead of in
production.

They are skipped by default because they need the network:

    MEDIABOT_LIVE=1 pytest -q tests/test_live_integrations.py
"""
import asyncio
import contextlib
import os
import subprocess
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tests.test_download_pipeline import make_bot
from tests.test_ui_semantics import FaithfulClient

main_mod = __import__("app.main", fromlist=["main"])

pytestmark = pytest.mark.skipif(
    os.getenv("MEDIABOT_LIVE") != "1",
    reason="set MEDIABOT_LIVE=1 to run the checks that use the real services",
)


def _serve(directory: Path):
    handler = partial(SimpleHTTPRequestHandler, directory=str(directory))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def _make_media(path: Path) -> Path:
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


def _live_bot(tmp_path):
    """A bot wired to the real environment, with only Telegram faked out."""
    cfg = main_mod.Config.from_env()
    client = FaithfulClient()
    bot = make_bot(tmp_path, client)
    bot.cfg = type(cfg)(
        **{
            **{f.name: getattr(cfg, f.name) for f in cfg.__dataclass_fields__.values()},
            "download_dir": tmp_path / "downloads",
            "work_dir": tmp_path / "work",
            "db_path": tmp_path / "live.sqlite3",
            "session_timeout": 600,
        }
    )
    (tmp_path / "downloads").mkdir(parents=True, exist_ok=True)
    (tmp_path / "work").mkdir(parents=True, exist_ok=True)
    from app.storage.db import DB

    bot.db = DB(bot.cfg.db_path)
    return bot, client


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


def test_real_user_journey_from_a_link(tmp_path):
    """Send a link, pick actions, get real results - nothing downloaded early.

    Covers the three things that were reported broken: the download started by
    itself, Media Information needed the whole file, and removing a stream
    crashed with a TypeError.
    """
    origin = tmp_path / "origin"
    origin.mkdir()
    _make_media(origin / "movie.mkv")
    server, port = _serve(origin)
    url = f"http://127.0.0.1:{port}/movie.mkv"
    try:
        async def run():
            bot, client = _live_bot(tmp_path)
            uid = 4242
            bot.allowed = lambda u: True
            bot.is_sudo = lambda u: False
            # Loopback is blocked in production; this test serves the media
            # locally on purpose.
            from app.services import downloader

            original = main_mod.download_url

            async def patched(*args, **kwargs):
                kwargs.setdefault("allow_private", True)
                return await original(*args, **kwargs)

            main_mod.download_url = patched
            try:
                await bot.process_url(1, uid, url)
                st = bot.state(uid)
                card = client.rendered[-1]
                # 1. Nothing has been transferred yet.
                assert st.path is None
                assert "Nothing has been downloaded yet" in card, card

                # 2. Media Information works off the header, with no download.
                await bot._wait_for_metadata(uid)
                assert st.path is None, "Media Information must not download the media"
                assert len(st.streams) == 3, st.streams
                ev = Event(bot, uid, "video:info")
                await bot.callback_router(ev, uid, "video:info")
                info = client.rendered[-1]
                assert "telegra.ph" in info, info
                assert "TypeError" not in info

                # 3. The track list also comes from the header.
                st.view = "menu"
                ev = Event(bot, uid, "video:streams")
                await bot.callback_router(ev, uid, "video:streams")
                assert st.path is None, "opening the track list must not download"
                labels = [
                    getattr(b, "text", "")
                    for row in (bot.client.messages[(1, st.card_message_id)].buttons or [])
                    for b in row
                ]
                assert any("aac" in t for t in labels), labels

                # 4. Choosing the operation fetches the file, with progress,
                #    and produces a real result. This is the callback that used
                #    to raise "takes 0 positional arguments but 1 was given".
                ev = Event(bot, uid, "stream:remove:2")
                await bot.callback_router(ev, uid, "stream:remove:2")
                assert st.path is not None and st.path.exists(), client.rendered[-3:]
                assert st.path.stat().st_size > 0
                assert not any("failed" in t.lower() for t in client.rendered), client.rendered
                # The upload destination is offered afterwards, and GoFile
                # really accepts the result with the configured token.
                assert any("gofile" in t.lower() or "upload" in t.lower() for t in client.rendered)
                token = main_mod.Config.from_env().gofile_api_token
                if token:
                    from app.services.gofile import upload_gofile

                    data = await upload_gofile(st.path, token)
                    assert data.get("link") or data.get("downloadPage"), data
                return bot, st
            finally:
                main_mod.download_url = original

        bot, st = asyncio.run(run())
        # The result really is the source with one audio track fewer.
        assert st.path.name.endswith(".streams_removed.mkv") or "remux" in st.path.name, st.path.name
        remaining = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(st.path)],
            check=True, capture_output=True, text=True,
        ).stdout.split()
        assert remaining.count("audio") == 1, remaining
    finally:
        server.shutdown()


def test_real_telegraph_page(tmp_path):
    """The configured Telegraph account really accepts a generated page."""
    from app.services.telegraph import create_info_page

    media = _make_media(tmp_path / "movie.mkv")
    data = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(media)],
        check=True, capture_output=True, text=True,
    ).stdout
    import json

    cfg = main_mod.Config.from_env()
    url = asyncio.run(create_info_page(json.loads(data), media.name, "1.00 KiB", cfg.telegraph_access_token))
    assert url.startswith("https://telegra.ph/"), url


def test_real_gofile_upload(tmp_path):
    """The configured GoFile token really accepts an upload."""
    from app.services.gofile import upload_gofile

    payload = tmp_path / "probe.txt"
    payload.write_bytes(b"media-tools-bot live check")
    cfg = main_mod.Config.from_env()
    if not cfg.gofile_api_token:
        pytest.skip("GOFILE_API_TOKEN is not configured")
    data = asyncio.run(upload_gofile(payload, token=cfg.gofile_api_token))
    link = data.get("downloadPage") or data.get("link") or (data.get("children") or {}).get("link")
    assert link and str(link).startswith("http"), data


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
