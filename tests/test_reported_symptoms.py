"""Reproductions of the symptoms reported from the running deployment.

The production log showed:

    RuntimeWarning: coroutine 'MediaToolsBot._sweep_orphans' was never awaited

and the screenshot showed a "Downloading…" panel with a bare filename and no
progress bar, plus a menu that snapped back to the main menu after every tap.
"""
import asyncio
import shutil
import sys
from pathlib import Path

import pytest

from tests.test_download_pipeline import make_bot
from tests.test_ui_semantics import FaithfulClient, FakeMessage

main_mod = __import__("app.main", fromlist=["main"])


def test_sweep_orphans_actually_runs(tmp_path):
    """to_thread(async_def) returns a coroutine that is never awaited.

    The sweep therefore never deleted anything, so every restart left the
    previous run's downloads on disk until the volume filled up.
    """
    downloads = tmp_path / "downloads"
    (downloads / "111").mkdir(parents=True)
    (downloads / "222").mkdir(parents=True)
    (downloads / "keep").mkdir(parents=True)
    (downloads / "111" / "big.mkv").write_bytes(b"x" * 1024)

    bot = make_bot(tmp_path, FaithfulClient())
    bot.cfg.download_dir = downloads
    # No user is active, so both numbered directories are orphans.
    asyncio.run(bot._sweep_orphans())
    assert not (downloads / "111").exists(), "orphaned directory was not removed"
    assert not (downloads / "222").exists()
    assert (downloads / "keep").exists(), "non-user directory must be preserved"


def test_sweep_orphans_is_not_a_bare_coroutine():
    """Regression guard: _sweep_orphans must be a coroutine function that awaits."""
    import inspect

    assert inspect.iscoroutinefunction(main_mod.MediaToolsBot._sweep_orphans)
    assert not inspect.iscoroutinefunction(main_mod.MediaToolsBot._sweep_orphans_sync)


def test_download_panel_shows_a_progress_bar(tmp_path):
    """The panel used to print only the filename, so a live download looked dead."""
    from app.services.bulk import DOWNLOADING, QueueItem

    async def run():
        bot = make_bot(tmp_path, FaithfulClient())
        uid = 4
        st = bot.state(uid)
        st.view = "queue"
        item = QueueItem(
            kind="telegram", label="1080p.mkv", chat_id=1,
            status=DOWNLOADING, current=5 * 1024**2, total=10 * 1024**2,
        )
        # Two readings with a gap, so the smoothed speed is a real number.
        item.advance(2 * 1024**2, 10 * 1024**2)
        await asyncio.sleep(0.2)
        item.advance(5 * 1024**2, 10 * 1024**2)
        st.queue.items.append(item)
        await bot._render_queue_panel(uid, 1, force=True)
        return bot.client.rendered[-1]

    text = asyncio.run(run())
    assert "1080p.mkv" in text
    # A bar, the byte counts, the speed and an ETA in minutes are what make it
    # visibly alive. The ETA used to be raw seconds ("ETA 2461s").
    assert "5.0 MiB" in text and "10.00 MiB" in text, text
    assert "50.0%" in text, text
    assert "⚡" in text, text
    assert "/s" in text, text
    assert "left" in text, text
    assert "ETA 2461s" not in text


def test_pressing_a_button_is_not_overwritten_by_the_panel(tmp_path):
    """Tapping a submenu button must not snap back to the main menu."""
    async def run():
        bot = make_bot(tmp_path, FaithfulClient())
        uid = 6
        bot.allowed = lambda u: True
        bot.is_sudo = lambda u: False
        bot.touch = lambda u: None

        from app.services.bulk import DOWNLOADING, QueueItem

        st = bot.state(uid)
        item = QueueItem(
            kind="telegram", label="movie.mkv", chat_id=1,
            status=DOWNLOADING, current=1024, total=2048,
        )
        st.queue.items.append(item)
        st.view = "queue"
        await bot.show_file_menu(1, uid, None)

        class Ev:
            sender_id = uid
            chat_id = 1
            data = b"menu:video"

            def __init__(self):
                self.answers, self.responses = [], []
                self.message = bot.client.messages.get((1, st.card_message_id))

            async def answer(self, text=None, alert=False):
                self.answers.append((text, alert))

            async def respond(self, text=None, **kw):
                self.responses.append(text)

            async def edit(self, text, buttons=None, **kw):
                return await self.message.edit(text, buttons=buttons, **kw)

        ev = Ev()
        await bot.callback_router(ev, uid, "menu:video")
        after_tap = bot.client.rendered[-1]

        # The panel loop wakes up while the download is still running.
        await bot._render_queue_panel(uid, 1)
        after_panel = bot.client.rendered[-1]

        return after_tap, after_panel

    after_tap, after_panel = asyncio.run(run())
    assert "select your preferred action" in after_tap.lower()
    assert after_panel == after_tap, (
        "the download panel overwrote the submenu the user just opened"
    )


def test_merge_estimate_reports_size_and_duration(tmp_path):
    bot = make_bot(tmp_path, FaithfulClient())
    st = bot.state(1)
    a, b = tmp_path / "video.mkv", tmp_path / "audio.mkv"
    a.write_bytes(b"x" * 1000)
    b.write_bytes(b"y" * 2000)
    st.merge_inputs = [a.resolve(), b.resolve()]
    st.merge_durations = {str(a.resolve()): 125.0, str(b.resolve()): 125.5}

    text = bot.merge_status_text(st)
    assert "Total input" in text
    assert "Estimated output" in text
    # 3000 bytes input -> ~3.0 KiB estimated output (3% container overhead).
    assert "KiB" in text, text
    assert "Longest track" in text
    assert "2:05" in text, text


def test_merge_duration_probe_caches(tmp_path):
    src = tmp_path / "clip.mkv"
    src.write_bytes(b"data")

    async def run():
        bot = make_bot(tmp_path, FaithfulClient())
        bot._probe = None
        # Restore it afterwards: leaving the stub installed leaks into every
        # later test in the session and makes the suite order-dependent.
        original = main_mod.ffprobe.safe_probe
        main_mod.ffprobe.safe_probe = lambda p: {"format": {"duration": "42.5"}}
        try:
            await bot._probe_merge_duration(1, src.resolve())
        finally:
            main_mod.ffprobe.safe_probe = original
        return bot.state(1).merge_durations

    durations = asyncio.run(run())
    assert durations[str(src.resolve())] == 42.5


def test_merge_refuses_when_output_cannot_fit(tmp_path):
    """A merge that obviously will not fit must fail before FFmpeg starts."""
    bot = make_bot(tmp_path, FaithfulClient())
    uid = 9
    st = bot.state(uid)
    a, b = tmp_path / "a.mkv", tmp_path / "b.mkv"
    a.write_bytes(b"x" * 2048)
    b.write_bytes(b"y" * 2048)
    st.merge_inputs = [a.resolve(), b.resolve()]

    # Pretend the work volume is full.
    full = type("U", (), {"free": 1024})()
    original = shutil.disk_usage
    shutil.disk_usage = lambda p: full
    original_probe = main_mod.ffprobe.safe_probe
    main_mod.ffprobe.safe_probe = lambda p: {"streams": [{"codec_type": "video"}]}
    started = []

    async def fake_execute(chat_id, u, label, func, **kw):
        started.append(label)

    bot.execute = fake_execute
    try:
        asyncio.run(bot.finish_merge(1, uid))
    finally:
        shutil.disk_usage = original
        main_mod.ffprobe.safe_probe = original_probe

    assert not started, "merge ran despite insufficient disk space"
    assert any("disk space" in t.lower() for t in bot.client.rendered), bot.client.rendered


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))