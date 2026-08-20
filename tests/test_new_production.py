import asyncio
import subprocess
from pathlib import Path

from app.services.merge import merge_tracks
from app.services.ffprobe import probe
from app.storage.db import DB
from app.ui.keyboards import video_menu, audio_menu, merge_menu


def _make_test_media(tmp_path):
    video = tmp_path / 'video.mkv'
    audio = tmp_path / 'audio.mka'
    subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-f','lavfi','-i','testsrc=size=640x360:rate=24','-t','2','-c:v','libx264','-preset','ultrafast',str(video)], check=True)
    subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-t','2','-c:a','aac','-b:a','192k',str(audio)], check=True)
    return video, audio


def test_merge_queue_validation_does_not_duplicate_inputs(tmp_path):
    video, audio = _make_test_media(tmp_path)
    out = tmp_path / 'merged.mkv'
    merge_tracks([video, audio], out)
    assert out.stat().st_size < video.stat().st_size + audio.stat().st_size + 1024 * 1024
    streams = probe(out)['streams']
    assert sum(s.get('codec_type') == 'video' for s in streams) == 1
    assert sum(s.get('codec_type') == 'audio' for s in streams) == 1


def test_merge_ui_has_finish_cancel_back_only():
    labels = [b.text for row in merge_menu() for b in row]
    assert labels == ['✅ Finish Merge', '❌ Cancel Merge', '⬅️ Back']


def test_functionality_menus_have_back_button():
    assert any(b.text == '⬅️ Back' for row in video_menu() for b in row)
    assert any(b.text == '⬅️ Back' for row in audio_menu() for b in row)


def test_config_defaults_global_concurrency_to_ten(monkeypatch):
    for k in ['API_ID','API_HASH','BOT_TOKEN']:
        monkeypatch.setenv(k, '1')
    monkeypatch.setenv('API_HASH','x')
    monkeypatch.setenv('BOT_TOKEN','1:token')
    monkeypatch.delenv('MAX_CONCURRENT_JOBS', raising=False)
    from app.config import Config
    cfg = Config.from_env()
    assert cfg.max_concurrent_jobs == 10


def test_user_registration_first_seen(tmp_path):
    db = DB(tmp_path / 'bot.sqlite3')
    assert db.register_user(100, 'alice', 'Alice', None) is True
    assert db.register_user(100, 'alice2', 'Alice', None) is False
    assert db.get_username(100) == 'alice2'
    db.close()


def test_start_has_sudo_ongoing_button_and_same_message_rendering_source():
    source = Path('app/main.py').read_text()
    assert 'b"admin:processes"' in source
    assert 'async def send_ongoing_processes' in source
    assert 'source=source' in source
    assert 'self.render_ui' in source


def test_gofile_completion_is_rendered_into_existing_progress_message():
    source = Path('app/main.py').read_text()
    assert 'await self.safe_edit(status, "\\n".join(lines)' in source
    assert 'await self.client.send_message(chat_id, "\\n".join(lines)' not in source

def test_timeout_cleanup_contract():
    source = Path('app/main.py').read_text()
    assert 'SESSION_TIMEOUT' not in source or 'self.cfg.session_timeout' in source
    assert 'async def timeout_user' in source
    assert 'Session timed out' in source
    assert 'self.cleanup_user_files(uid)' in source

def test_finish_merge_does_not_duplicate_validated_queue(tmp_path, monkeypatch):
    import app.main as main_mod
    video, audio = _make_test_media(tmp_path)

    class State:
        merge_inputs = [video, audio]
        pending = 'merge_collect'
        path = video
        source_path = video
        root_path = video
        task = None
        cancel_event = None
        outputs = []
        streams = []
        operation = None
        progress_current = 0
        progress_total = 0
        last_activity = 0
        timeout_task = None
        ui_message_id = None
        ui_chat_id = None
        started_at = None
        @property
        def busy(self): return False

    class FakeDB:
        def active_direct_paths(self, now): return set()

    bot = object.__new__(main_mod.MediaToolsBot)
    bot.states = {1: State()}
    bot.db = FakeDB()
    bot.cfg = type('Cfg', (), {'work_dir': tmp_path, 'download_dir': tmp_path, 'session_timeout': 21600})()
    captured = {}
    bot.state = lambda uid: bot.states[uid]
    bot.touch = lambda uid: None
    async def fake_execute(chat_id, uid, label, func, upload=False, set_source=True):
        captured['inputs'] = func.__closure__[0].cell_contents if func.__closure__ else None
    bot.execute = fake_execute
    async def fake_render(*args, **kwargs): pass
    bot.render_ui = fake_render
    bot.merge_status_text = lambda st: ''
    awaitable = bot.finish_merge(123, 1)
    asyncio.run(awaitable)
    assert bot.states[1].merge_inputs == [video.resolve(), audio.resolve()]
