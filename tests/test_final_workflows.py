from pathlib import Path
import asyncio

from app.storage.db import DB
from app.ui.keyboards import main_menu, settings_menu, rename_menu, admin_menu, merge_menu


def labels(rows):
    return [b.text for row in rows for b in row]


def test_media_menu_has_no_url_uploader():
    assert not any("Url Uploader" in x or "URL Uploader" in x for x in labels(main_menu()))


def test_settings_shows_selected_values():
    on = labels(settings_menu(True, "gofile"))
    assert any("Rename: ON" in x and "✅" in x for x in on)
    assert any("GoFile" in x and "✅" in x for x in on)
    choose = labels(settings_menu(False, "choose"))
    assert any("Rename: OFF" in x and "✅" in x for x in choose)
    assert any("Choose" in x and "✅" in x for x in choose)


def test_rename_prompt_has_rename_and_skip():
    assert labels(rename_menu())[:2] == ["✏️ Rename", "⏭️ Skip"]


def test_admin_menu_is_sudo_controls():
    names = labels(admin_menu())
    assert "🧭 Ongoing Processes" in names
    assert "🛑 Cancel User Job" in names
    assert "📢 Broadcast" in names


def test_db_upload_mode_and_user_lookup(tmp_path):
    db = DB(tmp_path / "db.sqlite3")
    assert db.register_user(123, "Alice", "Alice", None) is True
    assert db.get_upload_mode(123) == "choose"
    db.set_upload_mode(123, "gofile")
    assert db.get_upload_mode(123) == "gofile"
    assert db.find_user("123") == 123
    assert db.find_user("@Alice") == 123
    assert db.all_users() == [123]
    db.close()


def test_main_contains_admin_broadcast_and_cancel_commands():
    source = Path("app/main.py").read_text()
    assert 'cmd == "/canceluser"' in source
    assert 'cmd == "/broadcast"' in source
    assert 'async def admin_broadcast' in source
    assert 'async def admin_cancel_user' in source


def test_main_contains_rename_upload_flow():
    source = Path("app/main.py").read_text()
    assert 'st.pending_upload_destination = destination' in source
    assert 'async def apply_rename_and_continue' in source
    assert 'rename:yes' in source
    assert 'rename:skip' in source


def test_screenshot_limits_and_multiple_timestamps_are_implemented():
    source = Path("app/main.py").read_text()
    assert 'st.pending == "shots_count"' in source
    assert '1 <= count <= 20' in source
    assert 're.split(r"[,\\n]+", text)' in source
    assert '1 <= len(timestamps) <= 20' in source


def test_gofile_completion_does_not_emit_folder_link():
    source = Path("app/main.py").read_text()
    assert '📁 <b>GoFile folder:' not in source
    assert 'downloadPage' in source


def test_cancel_keeps_file_cleanup_separate():
    source = Path("app/main.py").read_text()
    cancel_block = source[source.index('async def cancel('):source.index('async def start_web_server', source.index('async def cancel('))]
    assert 'cleanup_user_files' not in cancel_block
    assert 'clear_status_message(uid, delete=True)' in cancel_block


def test_timeout_deletes_server_files_and_returns_start_button():
    source = Path("app/main.py").read_text()
    assert 'self.cleanup_user_files(uid)' in source
    assert 'Button.inline("🏠 Start", b"start:home")' in source


def test_admin_process_view_is_simple():
    source = Path("app/main.py").read_text()
    assert 'User — Processes' in source
    assert 'merge:' not in source[source.index('def active_processes_text'):source.index('async def new_status_message')]


def test_process_control_is_used_for_ffmpeg_and_merge_cancellation():
    ff = Path("app/services/ffmpeg.py").read_text()
    mg = Path("app/services/merge.py").read_text()
    main = Path("app/main.py").read_text()
    assert "from .process_control import run_command" in ff
    assert "from .process_control import run_command" in mg
    assert "process_control.cancel_job(uid)" in main
    assert "process_control.set_job(uid)" in main


def test_process_control_can_interrupt_registered_process():
    import threading
    import time
    from app.services import process_control

    result = {}
    token = process_control.set_job(424242)
    try:
        def worker():
            worker_token = process_control.set_job(424242)
            try:
                process_control.run_command(["bash", "-lc", "sleep 30"], text=True)
                result["ok"] = True
            except Exception as exc:
                result["exc"] = exc
            finally:
                process_control.reset_job(worker_token)
        t = threading.Thread(target=worker)
        t.start()
        for _ in range(50):
            if process_control.cancel_job(424242):
                break
            time.sleep(0.02)
        t.join(timeout=3)
        assert not t.is_alive()
        assert "ok" not in result
    finally:
        process_control.reset_job(token)
