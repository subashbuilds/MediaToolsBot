import asyncio
import sqlite3
import subprocess
from pathlib import Path

from aiohttp import web, ClientSession

from app.storage.db import DB
from app.services.gofile import upload_gofile
from app.services import ffprobe
from app.ui.keyboards import merge_menu
from app.utils.files import format_bitrate, format_duration


def make_media(path: Path, kind: str):
    if kind == "video":
        args = ["-f", "lavfi", "-i", "testsrc=size=320x180:rate=10", "-t", "1", "-c:v", "libx264"]
    elif kind == "audio":
        args = ["-f", "lavfi", "-i", "sine=frequency=660:sample_rate=48000", "-t", "1", "-c:a", "aac"]
    else:
        raise ValueError(kind)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args, str(path)], check=True)


def test_merge_menu_has_only_finish_and_cancel():
    rows = merge_menu()
    labels = [row[0].text for row in rows]
    assert labels == ["✅ Finish Merge", "❌ Cancel Merge", "⬅️ Back"]


def test_merge_dispatch_routes_captioned_media_before_pending_text():
    source = Path("app/main.py").read_text()
    media_branch = 'elif self.state(uid).pending == "merge_collect" and event.message.media:'
    assert media_branch in source
    assert source.index(media_branch) < source.index('elif self.state(uid).pending:\n            # Pending workflows')
    assert "await self.receive_media(event)" in source[source.index(media_branch):source.index('elif self.state(uid).pending:\n            # Pending workflows')]


def test_merge_engine_video_audio_video(tmp_path):
    video1 = tmp_path / "v1.mkv"
    audio = tmp_path / "a.mka"
    video2 = tmp_path / "v2.mkv"
    make_media(video1, "video")
    make_media(audio, "audio")
    make_media(video2, "video")
    from app.services.merge import merge_tracks
    out = tmp_path / "merged.mkv"
    merge_tracks([video1, audio, video2], out)
    streams = ffprobe.probe(out)["streams"]
    assert sum(s["codec_type"] == "video" for s in streams) == 2
    assert sum(s["codec_type"] == "audio" for s in streams) == 1


def test_human_readable_helpers():
    assert format_duration(3661.25) == "1:01:01"
    assert format_bitrate(2_500_000) == "2.50 Mb/s"


def test_db_migrates_and_stores_gofile_folder(tmp_path):
    db_path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE users(user_id INTEGER PRIMARY KEY, gofile_token TEXT, rename_file INTEGER NOT NULL DEFAULT 0, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)")
    conn.commit(); conn.close()
    db = DB(db_path)
    db.set_gofile_token(1, "guest-token")
    db.set_gofile_folder(1, "folder-123")
    assert db.get_gofile_token(1) == "guest-token"
    assert db.get_gofile_folder(1) == "folder-123"
    db.set_gofile_token(1, "new-token")
    assert db.get_gofile_folder(1) is None
    db.close()


def test_gofile_payload_reuses_folder_id(tmp_path, monkeypatch):
    path = tmp_path / "one.bin"
    path.write_bytes(b"hello")
    seen = []

    async def main():
        async def handler(request):
            reader = await request.multipart()
            fields = {}
            while True:
                part = await reader.next()
                if part is None:
                    break
                body = await part.read()
                fields[part.name] = body.decode() if part.name != "file" else body
            seen.append(fields)
            if len(seen) == 1:
                data = {"parentFolder": "folder-xyz", "guestToken": "guest-xyz", "downloadPage": "https://gofile.io/d/folder-xyz"}
            else:
                data = {"parentFolder": "folder-xyz", "downloadPage": "https://gofile.io/d/folder-xyz"}
            return web.json_response({"status": "ok", "data": data})

        app = web.Application()
        app.router.add_post("/uploadfile", handler)
        runner = web.AppRunner(app); await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0); await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            import app.services.gofile as gf
            monkeypatch.setattr(gf, "UPLOAD_ENDPOINT", f"http://127.0.0.1:{port}/uploadfile")
            first = await upload_gofile(path)
            second = await upload_gofile(path, token=first["guestToken"], folder_id=first["parentFolder"])
            assert first["parentFolder"] == second["parentFolder"] == "folder-xyz"
            assert seen[0]["file"] == b"hello"
            assert "folderId" not in seen[0]
            assert seen[1]["folderId"] == "folder-xyz"
            assert seen[1]["file"] == b"hello"
        finally:
            await runner.cleanup()

    asyncio.run(main())


def test_gofile_batch_and_human_info_contracts():
    source = Path("app/main.py").read_text()
    assert "files = [p for p in st.outputs" in source
    assert "total_bytes = sum(p.stat().st_size for p in files)" in source
    assert "folder_id = self.db.get_gofile_folder(uid)" in source
    assert "self.db.set_gofile_folder(uid, folder_id)" in source
    assert "Open detailed Media Information" in source
    assert "stream_packet_sizes" in source
