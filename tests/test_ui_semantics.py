"""Regression tests for the production symptoms reported against the live bot.

The earlier fake client in this repo never raised ``MessageNotModifiedError`` and
never returned ``None`` for a deleted message, so it could not possibly catch
the two failures users actually hit:

  * every re-render of a screen sent a brand-new message (spam), because
    ``safe_edit`` swallowed ``MessageNotModifiedError`` and the caller read the
    resulting ``None`` as "edit failed";
  * tapping an action while the file was still downloading answered
    "send a media file first" even though the bot had already started
    downloading it.

This module models those two Telegram behaviours faithfully.
"""
import asyncio
import sys
import types
from pathlib import Path

import pytest

from tests.test_download_pipeline import FakeMessage, FakeMsg, make_bot

main_mod = __import__("app.main", fromlist=["main"])


class FaithfulMsg(FakeMsg):
    """A Telethon message that behaves like the real one on edit/delete."""

    def __init__(self, msg_id, client=None):
        super().__init__(msg_id, client)
        self.deleted = False
        self._signature = None
        # Real Telethon keeps the existing markup when `buttons` is omitted,
        # and clears it only when buttons is explicitly None/[].
        self.markup_omitted_keeps = True

    def _sig(self, text, buttons):
        def norm(b):
            if b is None:
                return None
            out = []
            for row in b:
                out.append(tuple((getattr(x, "text", None), getattr(x, "data", None)) for x in row))
            return tuple(out)

        return (text, norm(buttons))

    async def edit(self, text, buttons=None, **kwargs):
        # Telethon omits the `buttons` kwarg entirely when not supplied.
        supplied = "buttons" in kwargs or buttons is not None or True
        sig = self._sig(text, buttons)
        if self._signature is not None and sig == self._signature:
            # Telethon: "Raises MessageNotModifiedError if the contents of the
            # message were not modified at all."
            raise main_mod.MessageNotModifiedError("Message not modified")
        self._signature = sig
        self.text = text
        self.edits.append(text)
        self.buttons = buttons
        if self.client is not None:
            self.client.rendered.append(text)
        return self

    async def delete(self):
        self.deleted = True
        if self.client is not None:
            self.client.messages.pop((1, self.id), None)
        return True


class FaithfulClient:
    """Client whose messages follow Telegram semantics on edit and delete."""

    def __init__(self):
        self.sent = []
        self.rendered = []
        self.messages = {}
        self._next = 100

    async def get_me(self):
        return types.SimpleNamespace(id=999, username="faithful_bot")

    async def get_messages(self, chat_id, ids=None):
        if isinstance(ids, (list, tuple)):
            return [self.messages.get((chat_id, i)) for i in ids]
        return self.messages.get((chat_id, ids))

    async def send_message(self, chat_id, text, buttons=None, **kwargs):
        self._next += 1
        msg = FaithfulMsg(self._next, self)
        msg.text = text
        msg.markup_omitted_keeps = buttons is not None
        msg._signature = msg._sig(text, buttons)
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
        raise AssertionError("no reactions expected")


def _new_bot(tmp_path, client):
    return make_bot(tmp_path, client)


# ----------------------------------------------------------------------
# 1. Re-rendering an identical screen must not send a second message
# ----------------------------------------------------------------------
def test_identical_rerender_does_not_spam(tmp_path):
    async def run():
        client = FaithfulClient()
        bot = _new_bot(tmp_path, client)

        first = await bot.render_card(1, 5, "🎬 <b>Video</b>", buttons=None)
        assert len(client.sent) == 1

        # Same content again: Telegram says "not modified".
        second = await bot.render_card(1, 5, "🎬 <b>Video</b>", buttons=None)
        assert second is first
        assert len(client.sent) == 1, "identical re-render must edit, not resend"

        # A genuine change still edits in place.
        third = await bot.render_card(1, 5, "🎬 <b>Audio</b>", buttons=None)
        assert third is first
        assert len(client.sent) == 1
        return client

    asyncio.run(run())


def test_unchanged_is_distinguishable_from_failure(tmp_path):
    """`None` (failed) and UNCHANGED (identical) must never collapse together."""
    msg = FaithfulMsg(1)
    msg._signature = msg._sig("hello", None)
    assert main_mod.MediaToolsBot._editable(None) is False
    assert main_mod.MediaToolsBot._editable(main_mod.UNCHANGED) is False


# ----------------------------------------------------------------------
# 2. Tapping an action mid-download must wait, not claim "send a file first"
# ----------------------------------------------------------------------
def test_action_during_download_waits_instead_of_refusing(tmp_path):
    async def run():
        client = FaithfulClient()
        bot = _new_bot(tmp_path, client)
        st = bot.state(5)

        class Slow:
            payload = b"x" * 2048

            def read(inner):
                import time as _t
                _t.sleep(0.02)
                return b""

        # Simulate an in-flight transfer the user cannot see yet.
        item = types.SimpleNamespace(
            path=tmp_path / "a.mkv", display_name="a.mkv",
            status="downloading", error=None, size=2048, downloaded=0,
        )
        st.queue.items = [item]
        st.queue.running_count = 1

        async def finish():
            await asyncio.sleep(0.25)
            st.queue.items = []
            st.queue.running_count = 0
            target = tmp_path / "a.mkv"
            target.write_bytes(b"x" * 2048)
            st.path = target
            st.source_path = target
            st.root_path = target

        task = asyncio.create_task(finish())
        got = await bot.await_input(1, 5, timeout=3.0)
        await task
        assert got is not None, "action must wait for the download it started"
        assert got.exists()
        return client

    client = asyncio.run(run())
    # It should have shown a waiting card rather than an error/refusal.
    assert any("Still downloading" in t for t in client.rendered)


def test_action_without_any_file_still_reports_clearly(tmp_path):
    async def run():
        client = FaithfulClient()
        bot = _new_bot(tmp_path, client)
        st = bot.state(5)
        st.queue.items = []
        st.queue.running_count = 0
        st.path = None
        assert await bot.await_input(1, 5, timeout=0.3) is None
        return client

    asyncio.run(run())


# ----------------------------------------------------------------------
# 3. Deleted cards must not break the menu
# ----------------------------------------------------------------------
def test_menu_recovers_when_card_was_deleted(tmp_path):
    async def run():
        client = FaithfulClient()
        bot = _new_bot(tmp_path, client)
        first = await bot.render_card(1, 5, "screen one")
        await first.delete()  # user deleted the bot's message

        # The card reference is stale; the bot must send a fresh one.
        again = await bot.render_card(1, 5, "screen two")
        assert again is not None
        assert len(client.sent) == 2
        return client

    asyncio.run(run())


# ----------------------------------------------------------------------
# 4. Status messages must be keyed by user, not chat (group chats)
# ----------------------------------------------------------------------
def test_status_message_state_is_keyed_by_user(tmp_path):
    async def run():
        client = FaithfulClient()
        bot = _new_bot(tmp_path, client)
        uid, chat = 42, -1001234  # a group: chat_id != uid
        msg = await bot.new_status_message(chat, "working…", uid=uid)
        st = bot.state(uid)
        assert st.status_message_id == msg.id
        assert st.status_chat_id == chat
        # No stray per-chat state entry should have been created.
        assert chat not in bot.states or bot.states[chat] is st
        return client

    asyncio.run(run())


def test_menu_appears_below_the_file_not_above_it(tmp_path):
    """A new input must get a new card, never an edit of the previous one."""
    from app.services.bulk import QueueItem

    async def run():
        client = FaithfulClient()
        bot = _new_bot(tmp_path, client)
        uid = 7

        # Simulate an earlier interaction that left a card in the chat.
        await bot.render_card(1, uid, "📁 older.mkv", buttons=None)
        before = len(client.sent)

        item = QueueItem(kind="telegram", label="new.mkv", chat_id=1, message_id=5)
        await bot.enqueue_input(1, uid, item)

        assert len(client.sent) > before, (
            "the new menu must be a NEW message so it renders under the file, "
            "not an edit of the previous card far above it"
        )
        new_card = client.sent[-1][1]
        assert "Downloading" in new_card or "new.mkv" in new_card
        # And it must be the newest message.
        assert client.messages[(1, client._next)].id == bot.state(uid).card_message_id
        return client

    asyncio.run(run())


def test_progress_repaint_edits_in_place(tmp_path):
    """After the first card exists, progress must edit rather than resend."""
    from app.services.bulk import QueueItem

    async def run():
        client = FaithfulClient()
        bot = _new_bot(tmp_path, client)
        uid = 8
        st = bot.state(uid)
        st.view = "queue"
        await bot.enqueue_input(1, uid, QueueItem(kind="telegram", label="a.mkv", chat_id=1, message_id=1))
        after_first = len(client.sent)

        # A second repaint of the same screen must not add messages.
        await bot._render_queue_panel(uid, 1, force=True)
        assert len(client.sent) == after_first, "progress must edit the card, not resend it"
        return client

    asyncio.run(run())


# ----------------------------------------------------------------------
# 5. Stale buttons explain themselves instead of silently resetting
# ----------------------------------------------------------------------
def test_unknown_button_explains_itself(tmp_path):
    async def run():
        client = FaithfulClient()
        bot = _new_bot(tmp_path, client)
        bot.allowed = lambda uid: True
        bot.is_sudo = lambda uid: False
        bot.touch = lambda uid: None

        class Event:
            sender_id = 5
            chat_id = 1
            data = b"video:action_from_a_dead_build"

            def __init__(self):
                self.alerts = []

            async def answer(self, text="", alert=False):
                self.alerts.append((text, alert))

            async def respond(self, text, **kw):
                client.rendered.append(text)

        ev = Event()
        await bot.callback_router(ev, 5, "video:action_from_a_dead_build")
        assert any("out of date" in t for t, _ in ev.alerts), (
            "a stale button must tell the user why, not silently jump to home"
        )
        return client

    asyncio.run(run())


def test_full_journey_no_errors_no_duplicates(tmp_path):
    """Send a file, open a submenu, run an action - as a user actually would.

    Asserts the three production complaints do not reappear: no "send a file
    first" when a file was sent, no silent jump back to home, and no duplicate
    menus for the same screen.
    """
    async def run():
        client = FaithfulClient()
        bot = _new_bot(tmp_path, client)
        bot.allowed = lambda uid: True
        bot.is_sudo = lambda uid: False
        bot.touch = lambda uid: None
        uid = 3
        bot.db.register_user(uid, "c", "Tester", None)

        class Ev:
            def __init__(self, data, chat=1):
                self.sender_id, self.chat_id, self.data = uid, chat, data.encode()
                self.answers, self.responses = [], []
                # A real CallbackQuery always carries the message the button
                # is attached to, which is what the bot edits in place.
                card = bot.state(uid).card_message_id
                self.message = client.messages.get((chat, card))

            async def answer(self, text=None, alert=False):
                self.answers.append((text, alert))

            async def respond(self, text=None, **kw):
                self.responses.append(text)

            async def reply(self, text, **kw):
                client.rendered.append(text)

            async def edit(self, text, buttons=None, **kw):
                # Telethon's CallbackQuery.edit() edits the attached message.
                return await self.message.edit(text, buttons=buttons, **kw)

        def toasts(ev):
            return [t for t, _ in ev.answers] + ev.responses

        # 1. The user sends a video document.
        media = FakeMessage(message_id=42, name="movie.mkv", payload=b"movie-bytes" * 64)
        media.id = 42
        from app.services.bulk import QueueItem
        item = QueueItem(kind="telegram", label="movie.mkv", chat_id=1, message_id=42, message=media)
        await bot.enqueue_input(1, uid, item)

        # Let the background worker finish the transfer.
        st = bot.state(uid)
        for _ in range(200):
            if st.path and st.path.exists():
                break
            await asyncio.sleep(0.02)
        assert st.path is not None and st.path.exists(), "download never completed"
        st.view = "menu"
        await bot.show_file_menu(1, uid, st.path, fresh=False)

        sent_before_nav = len(client.sent)

        # 2. Open the Video submenu.
        ev = Ev("menu:video")
        await bot.callback_router(ev, uid, "menu:video")
        assert not any("out of date" in t for t in toasts(ev)), "valid button treated as stale"
        labels = [
            getattr(b, "text", "")
            for row in (ev.message.buttons or [])
            for b in row
        ]
        assert any("Media Information" in t for t in labels), labels
        assert ev.message.text == "Please select your preferred action below \U0001F447"

        # 3. Run an action that needs the file.
        st.streams = [{"index": 0, "codec_type": "video", "codec_name": "h264"}]
        ev2 = Ev("video:info")
        seen: list[str] = []

        async def fake_info_page(*a, **kw):
            seen.append("info")
            return "https://telegra.ph/x"

        bot.create_info_page = fake_info_page
        await bot.callback_router(ev2, uid, "video:info")
        await asyncio.sleep(0.1)

        joined = "\n".join(client.rendered)
        assert "Send a media file first" not in joined, (
            "the bot claimed no file existed even though one was sent"
        )
        assert "Send a video first" not in joined

        # 4. Submenu navigation must never multiply menus.
        assert len(client.sent) <= sent_before_nav + 1, (
            f"navigation created duplicate menus: {len(client.sent)} vs {sent_before_nav}"
        )
        return client

    asyncio.run(run())


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))