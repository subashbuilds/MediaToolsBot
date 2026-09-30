"""End-to-end tests of the background download pipeline with a fake client."""
import asyncio
import contextlib
import sys
import types
from pathlib import Path

import pytest


def _install_telethon_stub():
    if "telethon" in sys.modules:
        return
    try:
        import telethon  # noqa: F401
        return
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


_install_telethon_stub()
import app.main as main_mod  # noqa: E402


class FakeMessage:
    """Stand-in for a Telethon message that streams bytes with delays."""

    def __init__(self, message_id=1, name="clip.mkv", payload=b"data" * 512, chunk_delay=0.0):
        self.id = message_id
        self.photo = self.video = self.audio = self.voice = self.video_note = None
        self.file = types.SimpleNamespace(name=name, size=len(payload), mime_type="video/x-matroska")
        self.payload = payload
        self.chunk_delay = chunk_delay
        self._written = 0

    def read(self):
        if self._written >= len(self.payload):
            return b""
        block = self.payload[self._written: self._written + 1024]
        self._written += len(block)
        if self.chunk_delay:
            import time as _t
            _t.sleep(self.chunk_delay)
        return block


class FakeMsg:
    def __init__(self, msg_id, client=None):
        self.id = msg_id
        self.texts = []
        self.edits = []
        self.buttons = None
        self.client = client

    async def edit(self, text, buttons=None, **kwargs):
        self.edits.append(text)
        self.buttons = buttons
        if self.client is not None:
            self.client.rendered.append(text)
        return self

    async def delete(self):
        self.deleted = True
        return True


class FakeClient:
    def __init__(self, messages=None):
        self.sent = []
        # Every text the bot put on screen, whether sent or edited in place.
        self.rendered = []
        self.messages = {}
        for m in (messages or []):
            self.messages[(1, m.id)] = m
        self._next = 100

    async def get_me(self):
        return types.SimpleNamespace(id=999, username="media_tools_test_bot")

    async def get_messages(self, chat_id, ids=None):
        key = (chat_id, ids)
        if key in self.messages:
            return self.messages[key]
        if isinstance(ids, (list, tuple)) and ids:
            return [self.messages.get((chat_id, i)) for i in ids]
        return None

    async def send_message(self, chat_id, text, buttons=None, **kwargs):
        self._next += 1
        msg = FakeMsg(self._next, self)
        self.messages[(chat_id, self._next)] = msg
        self.sent.append((chat_id, text))
        self.rendered.append(text)
        return msg

    async def download_media(self, message, file=None, progress_callback=None):
        if progress_callback:
            await progress_callback(0, len(message.payload))
        data = b""
        while True:
            block = message.read()
            if not block:
                break
            data += block
            if progress_callback:
                await progress_callback(len(data), len(message.payload))
        Path(file).write_bytes(data)
        return file

    def __call__(self, *args, **kwargs):
        raise AssertionError("no reactions expected in these tests")


def make_bot(tmp_path, client, max_parallel=2):
    from app.storage.db import DB

    cfg = types.SimpleNamespace(
        db_path=tmp_path / "db.sqlite3",
        work_dir=tmp_path / "work",
        download_dir=tmp_path / "dl",
        max_concurrent_jobs=4,
        max_ffmpeg_jobs=1,
        max_download_mb=16,
        max_parallel_downloads=max_parallel,
        progress_interval=0.5,
        session_timeout=3600,
        sudo_users=frozenset(),
        allowed_users=frozenset(),
        public_base_url=None,
        direct_link_ttl=3600,
        telegraph_access_token=None,
        reactions_enabled=False,
    )
    cfg.download_dir.mkdir(parents=True, exist_ok=True)
    cfg.work_dir.mkdir(parents=True, exist_ok=True)

    bot = object.__new__(main_mod.MediaToolsBot)
    bot.cfg = cfg
    bot.db = DB(cfg.db_path)
    bot.client = client
    bot.states = {}
    bot.semaphore = asyncio.Semaphore(4)
    bot.ffmpeg_semaphore = asyncio.Semaphore(1)
    bot.web_runner = None
    bot.direct_cleanup_task = None
    bot.sweeper_task = None
    bot.download_limit = 16 * 1024 * 1024
    return bot


def test_url_download_starts_on_the_action_and_sets_active_file(tmp_path):
    """A real HTTP download driven by the action the user picked."""
    from aiohttp import web

    from app.services.bulk import QueueItem

    payload = b"m" * 512 * 1024

    async def run():
        app = web.Application()

        async def handler(request):
            resp = web.StreamResponse(status=200, headers={
                "Content-Length": str(len(payload)),
                "Content-Disposition": 'attachment; filename="movie.mkv"',
            })
            await resp.prepare(request)
            for i in range(0, len(payload), 64 * 1024):
                await resp.write(payload[i: i + 64 * 1024])
                await asyncio.sleep(0)
            return resp

        app.router.add_get("/movie.mkv", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            client = FakeClient()
            bot = make_bot(tmp_path, client)
            uid = 1
            item = QueueItem(
                kind="url", label="movie.mkv", chat_id=uid,
                url=f"http://127.0.0.1:{port}/movie.mkv",
            )
            # Loopback is normally blocked; this test opts in explicitly.
            # main.py imports download_url into its own namespace, so that is
            # the reference the worker actually calls.
            original = main_mod.download_url

            async def patched(*args, **kwargs):
                kwargs.setdefault("allow_private", True)
                return await original(*args, **kwargs)

            main_mod.download_url = patched
            try:
                await bot.enqueue_input(uid, uid, item)
                assert client.sent, "a menu must be rendered immediately"
                st = bot.state(uid)
                # Nothing is transferred just because the link arrived.
                await asyncio.sleep(0.1)
                assert st.path is None, "arriving input must not trigger a download"
                dl_dir = bot.cfg.download_dir / str(uid)
                assert not dl_dir.exists() or not list(dl_dir.glob("*"))
                # Picking an action starts it and waits for the result.
                assert await bot.await_input(uid, uid, timeout=20) is not None
            finally:
                main_mod.download_url = original
            assert st.path is not None and st.path.exists()
            assert st.path.name == "movie.mkv"
            assert st.path.stat().st_size == len(payload)
            assert st.queue.summary()[1] == 1
            assert st.queue.failed() == []
        finally:
            await runner.cleanup()

    asyncio.run(run())


def test_non_bulk_users_get_the_regular_menu_while_downloading(tmp_path):
    """Without bulk mode the familiar action menu must stay on screen."""
    client = FakeClient()
    bot = make_bot(tmp_path, client)
    uid = 3
    st = bot.state(uid)
    st.queue.add(main_mod.QueueItem(kind="url", label="big.mkv", chat_id=uid, url="https://x/1"))

    async def run():
        claimed = st.queue.claim()
        claimed.total = 100
        claimed.current = 40
        await bot._render_queue_panel(uid, uid, force=True)
        text = client.sent[-1][1]
        assert "Downloading" in text
        assert "big.mkv" in text
        # The regular action menu is used, not the bulk keyboard.
        assert "Done Adding" not in text

    asyncio.run(run())


def test_bulk_users_get_the_queue_keyboard(tmp_path):
    client = FakeClient()
    bot = make_bot(tmp_path, client)
    uid = 4
    bot.db.register_user(uid, "u", "U", None)
    bot.db.set_bulk_mode(uid, True)
    st = bot.state(uid)
    st.queue.add(main_mod.QueueItem(kind="url", label="big.mkv", chat_id=uid, url="https://x/1"))

    async def run():
        claimed = st.queue.claim()
        claimed.total = 100
        claimed.current = 40
        await bot._render_queue_panel(uid, uid, force=True)
        text = client.sent[-1][1]
        assert "Bulk Mode" in text
        assert "40.0%" in text
        assert "Still downloading" in text
        assert "Queued:" in text and "Downloading:" in text

    asyncio.run(run())


def test_queue_worker_is_cancelled_by_cancel_downloads(tmp_path):
    client = FakeClient()
    bot = make_bot(tmp_path, client)
    uid = 7

    async def run():
        from app.services.bulk import QueueItem
        for i in range(3):
            bot.state(uid).queue.add(QueueItem(kind="url", label=f"f{i}.mkv", chat_id=uid, url=f"https://x/{i}"))
        st = bot.state(uid)
        assert st.queue.has_work()
        bot.cancel_downloads(uid)
        assert len(st.queue) == 0
        assert st.queue_cancel.is_set() is False, "a fresh cancel event is installed for reuse"
        assert st.worker_task is None

    asyncio.run(run())


def test_menu_appears_before_anything_is_downloaded(tmp_path):
    """The user's core request: the menu appears first, the transfer on demand."""
    client = FakeClient([FakeMessage(1, "a.mkv", b"z" * 2048)])
    bot = make_bot(tmp_path, client)
    uid = 1

    async def run():
        # Simulate what receive_media does.
        class Msg:
            id = 1
            photo = video = audio = voice = video_note = None
            file = types.SimpleNamespace(name="a.mkv", size=2048, mime_type="video/x-matroska")
            raw_text = ""
        from app.services.bulk import QueueItem
        item = QueueItem(kind="telegram", label="a.mkv", chat_id=uid, message_id=1, message=Msg())
        started = asyncio.Event()
        finish = asyncio.Event()

        async def slow_download(u, it):
            started.set()
            await finish.wait()
            return None

        bot._download_telegram_item = slow_download
        await bot.enqueue_input(uid, uid, item)
        # The action menu is on screen immediately...
        assert client.sent
        assert "a.mkv" in client.sent[0][1]
        # ...and no transfer has been started for it.
        await asyncio.sleep(0.1)
        assert not started.is_set(), "sending a file must not start a download"

        # Choosing an action starts the transfer and shows progress.
        wait = asyncio.create_task(bot.await_input(uid, uid, timeout=10))
        await asyncio.wait_for(started.wait(), timeout=2)
        await asyncio.sleep(0.05)
        assert any("Still downloading" in t for t in client.rendered), client.rendered
        finish.set()
        assert await wait is None  # the fake download produces no file
        assert any("failed" in t.lower() for t in client.rendered), client.rendered

    asyncio.run(run())


def test_promote_item_does_not_replace_active_file_while_busy(tmp_path):
    client = FakeClient()
    bot = make_bot(tmp_path, client)
    st = bot.state(1)
    sentinel = tmp_path / "current.mkv"
    sentinel.write_bytes(b"keep")
    st.path = sentinel
    st.outputs = [sentinel]

    class Dummy:
        status = "done"
        display_name = "new.mkv"

    new_file = tmp_path / "new.mkv"
    new_file.write_bytes(b"new")

    async def run():
        # A live task marks the state busy; a download finishing mid-job must
        # not swap out the input file the job is reading.
        st.task = asyncio.create_task(asyncio.sleep(5))
        try:
            bot._promote_item(st, new_file, Dummy())
            assert st.path == sentinel, "a running job must keep its input file"
        finally:
            st.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await st.task
            st.task = None
        bot._promote_item(st, new_file, Dummy())
        assert st.path == new_file
        assert st.outputs == [new_file]

    asyncio.run(run())
