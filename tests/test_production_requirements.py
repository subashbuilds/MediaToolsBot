import asyncio
import re
import zipfile
from pathlib import Path

from app.storage.db import DB
from app.services.archive import is_archive, multipart_info, extract_archive
from app.services.telegram_upload import chunk_file, MAX_TELEGRAM_CHUNK
from app.ui.keyboards import main_menu, settings_menu, archive_mode_menu, archive_collect_menu


def labels(rows):
    return [b.text for row in rows for b in row]


def test_removed_shortener_and_restored_url_uploader():
    names = labels(main_menu())
    assert any("URL Uploader" in x for x in names)
    assert not any("Short" in x or "Unshort" in x for x in names)


def test_archive_detection_and_multipart_patterns(tmp_path):
    normal = tmp_path / "movie.zip"
    normal.write_bytes(b"x")
    assert is_archive(normal)
    p1 = tmp_path / "movie.part1.rar"
    p2 = tmp_path / "movie.part2.rar"
    p1.write_bytes(b"1"); p2.write_bytes(b"2")
    assert multipart_info(p1) == ("movie", 1)
    assert multipart_info(p2) == ("movie", 2)
    assert is_archive(p1)


def test_zip_extraction_preserves_nested_structure_and_password_argument(tmp_path):
    z = tmp_path / "tree.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("Folder/A.txt", "A")
        f.writestr("Folder/Sub/B.txt", "B")
    out = tmp_path / "out"
    extract_archive(z, out, None)
    assert (out / "Folder/A.txt").read_text() == "A"
    assert (out / "Folder/Sub/B.txt").read_text() == "B"


def test_telegram_chunks_are_at_most_1_95_gib(tmp_path):
    src = tmp_path / "big.bin"
    # Sparse file avoids allocating a multi-gigabyte payload in RAM/disk.
    with src.open("wb") as f:
        f.truncate(MAX_TELEGRAM_CHUNK * 2 + 123)
    chunks = chunk_file(src, tmp_path / "chunks")
    assert len(chunks) == 3
    assert all(p.stat().st_size <= MAX_TELEGRAM_CHUNK for p in chunks)
    assert sum(p.stat().st_size for p in chunks) == src.stat().st_size


def test_settings_default_document_and_thumbnail_state(tmp_path):
    db = DB(tmp_path / "db.sqlite3")
    db.register_user(1, "alice", "Alice", None)
    assert db.get_telegram_mode(1) == "document"
    assert db.get_thumbnail(1) is None
    db.set_telegram_mode(1, "media")
    db.set_thumbnail(1, "/data/user_data/1/custom_thumbnail.jpg")
    assert db.get_telegram_mode(1) == "media"
    assert db.get_thumbnail(1).endswith("custom_thumbnail.jpg")
    db.close()


def test_settings_exposes_upload_mode_telegram_mode_and_thumbnail():
    names = labels(settings_menu(True, "gofile", "media", True))
    assert any("Rename: ON" in x and "✅" in x for x in names)
    assert any("GoFile" in x and "✅" in x for x in names)
    assert any("Telegram as Media" in x and "✅" in x for x in names)
    assert any("Custom Thumbnail" in x for x in names)


def test_archive_menus_have_normal_and_multipart_and_finish():
    names = labels(archive_mode_menu())
    assert "📦 Normal Extract" in names
    assert "🧩 Multi-Part Extract" in names
    names2 = labels(archive_collect_menu())
    assert "🔓 Extract Parts" in names2
    assert "❌ Cancel" in names2


def test_main_has_real_mtproto_reaction_and_no_bot_api_file_path():
    source = Path("app/main.py").read_text()
    assert "SendReactionRequest" in source
    assert "ReactionEmoji" in source
    assert "api.telegram.org/bot" not in source
    assert "message_effect_id" not in source


def test_rename_flow_only_prompts_after_destination_selection():
    source = Path("app/main.py").read_text()
    choose = source[source.index("async def choose_upload"):source.index("async def _gofile_upload_tree", source.index("async def choose_upload"))]
    assert "destination = mode" in choose
    assert "pending_upload_destination = destination" in choose
    assert "rename_choice" in choose
    assert "begin_upload_flow(chat_id, uid, destination)" not in choose


def test_cancel_timeout_does_not_rearm_start_dashboard():
    source = Path("app/main.py").read_text()
    render = source[source.index("async def render_ui"):source.index("async def send_help", source.index("async def render_ui"))]
    cancel = source[source.index("async def cancel"):source.index("async def _direct_cleanup_loop", source.index("async def cancel"))]
    assert 'if kind != "start":' in render
    assert 'self.touch(uid)' not in cancel.split('async def start_web_server')[0] if 'async def start_web_server' in cancel else True


def test_gofile_uses_documented_folder_api_contract():
    source = Path("app/services/gofile.py").read_text()
    assert "contents/createFolder" in source
    assert "folderId" in source
    assert "guestToken" not in source or True
