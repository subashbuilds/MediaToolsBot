"""Scripted walk-through of a realistic user session against the fake client."""
import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from test_download_pipeline import FakeClient, FakeMessage, make_bot  # noqa: E402

import app.main as main_mod  # noqa: E402


class FakeEvent:
    """CallbackQuery stand-in."""

    def __init__(self, chat_id, data, message=None):
        self.chat_id = chat_id
        self.data = data.encode() if isinstance(data, str) else data
        self.message = message or FakeMsg(1)
        self.answers = []
        self.responses = []

    async def answer(self, text=None, alert=False):
        self.answers.append((text, alert))

    async def respond(self, text=None, alert=False, **kw):
        self.responses.append(text)


class FakeMsg:
    def __init__(self, msg_id):
        self.id = msg_id
        self.deleted = False

    async def edit(self, text, buttons=None, **kwargs):
        self.last_text = text
        self.last_buttons = buttons
        return self

    async def delete(self):
        self.deleted = True
        return True


def drain(bot, uid, rounds=200):
    """Wait for the queue to go quiet (used after an action starts it)."""
    async def run():
        st = bot.state(uid)
        for _ in range(rounds):
            if not st.queue.has_work() and st.worker_task is None:
                return
            await asyncio.sleep(0.01)
    return run()


def test_bulk_session_end_to_end(tmp_path):
    """User enables bulk mode, sends three files, then picks Done Adding."""
    client = FakeClient()
    bot = make_bot(tmp_path, client)
    uid = 42
    bot.db.register_user(uid, "alice", "Alice", None)

    async def scenario():
        # Settings toggle turns bulk mode on.
        event = FakeEvent(uid, "settings:bulk:on")
        await bot.callback_router(event, uid, "settings:bulk:on")
        assert bot.db.get_bulk_mode(uid) is True

        # Three Telegram files arrive in a row without waiting for transfers.
        for i in range(3):
            message = FakeMessage(10 + i, f"clip{i}.mkv", b"a" * 4096)
            client.messages[(uid, 10 + i)] = message
            ev = types.SimpleNamespace(
                sender_id=uid, chat_id=uid, message=message, raw_text="", is_private=True, id=10 + i,
            )
            ev.message.out = False
            await bot.receive_media(ev)

        st = bot.state(uid)
        assert len(st.queue) == 3, "all three inputs must be accepted immediately"
        # A queue panel is on screen, and nothing has been transferred yet.
        assert any("Bulk Mode" in t for t in client.rendered), client.rendered
        assert any("Nothing is downloading yet" in t for t in client.rendered), client.rendered
        await drain(bot, uid)
        assert st.queue.summary()[1] == 0, "a queued input must not download by itself"
        assert st.path is None

        # "Done Adding" is where the collection actually transfers.
        done = FakeEvent(uid, "bulk:done")
        await bot.callback_router(done, uid, "bulk:done")
        assert len(st.outputs) == 3, "all three downloads should complete"
        assert st.path is not None and st.path.exists()
        # The bulk queue stayed on screen, with the transfer it started.
        assert any("Bulk Mode" in t and "clip" in t for t in client.rendered), client.rendered

        # ...and it switches to the regular action menu.
        assert st.view == "menu"
        assert st.path is not None
        assert any("select your preferred action" in t.lower() for t in client.rendered)

    asyncio.run(scenario())


def test_bulk_clear_queue_removes_pending_work(tmp_path):
    client = FakeClient()
    bot = make_bot(tmp_path, client)
    uid = 11
    bot.db.register_user(uid, "bob", "Bob", None)
    bot.db.set_bulk_mode(uid, True)

    async def scenario():
        st = bot.state(uid)
        for i in range(4):
            st.queue.add(main_mod.QueueItem(kind="url", label=f"f{i}.mkv", chat_id=uid, url=f"https://x/{i}"))
        st.path = None
        await bot.handle_bulk_callback(FakeEvent(uid, "bulk:clear"), uid, "bulk:clear")
        assert len(st.queue) == 0
        assert st.view == "menu"

    asyncio.run(scenario())


def test_bulk_upload_all_sets_the_output_set(tmp_path):
    client = FakeClient()
    bot = make_bot(tmp_path, client)
    uid = 12
    bot.db.register_user(uid, "c", "C", None)

    async def scenario():
        st = bot.state(uid)
        for i in range(3):
            f = tmp_path / f"out{i}.mkv"
            f.write_bytes(b"x" * 16)
            item = st.queue.add(main_mod.QueueItem(kind="url", label=f.name, chat_id=uid, url=f"u{i}"))
            st.queue.claim()
            item.path = f
            st.queue.release(item)
        assert len(st.queue.ready_paths()) == 3

        # "Upload All" first asks for a destination.
        await bot.handle_bulk_callback(FakeEvent(uid, "bulk:upload"), uid, "bulk:upload")
        assert any("Upload all" in t for t in client.rendered), client.rendered
        # Then choose Telegram; the normal upload path is reused.
        captured = {}
        async def fake_upload_telegram(chat_id, u):
            captured["files"] = bot._upload_files(st)
        bot.upload_telegram = fake_upload_telegram
        await bot.handle_bulk_upload(FakeEvent(uid, "bulkupload:telegram"), uid, "telegram")
        assert len(captured["files"]) == 3

    asyncio.run(scenario())


def test_unknown_callback_falls_back_to_a_working_screen(tmp_path):
    client = FakeClient()
    bot = make_bot(tmp_path, client)
    uid = 13

    async def scenario():
        event = FakeEvent(uid, "video:stale_button")
        await bot.callback_router(event, uid, "video:stale_button")
        # A toast (`event.answer`) is the right channel here: it appears above
        # the chat without adding another message to the conversation.
        assert event.answers or event.responses, "the user must be told the button expired"
        assert client.sent, "a fresh menu is sent"

    asyncio.run(scenario())


def test_cancel_stops_downloads_and_clears_state(tmp_path):
    client = FakeClient()
    bot = make_bot(tmp_path, client)
    uid = 14

    async def scenario():
        st = bot.state(uid)
        for i in range(3):
            st.queue.add(main_mod.QueueItem(kind="url", label=f"f{i}.mkv", chat_id=uid, url=f"https://x/{i}"))
        st.path = tmp_path / "leftover.mkv"
        st.pending = "trim"
        await bot.cancel(uid, uid)
        assert len(st.queue) == 0
        assert st.pending is None
        assert st.path is None
        assert st.view == "start"

    asyncio.run(scenario())
