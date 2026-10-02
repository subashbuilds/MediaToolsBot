"""Reproductions of the bugs reported from the live bot.

Screenshots from the deployment showed:

1. "Something went wrong: TypeError: MediaToolsBot.new_status_message() got
   multiple values for argument 'buttons'" the instant the user chose GoFile
   as the upload destination.
2. A link posted as text - very often with the URL hidden behind "Click Here" -
   never worked: every action answered "Send or download a media file first."
3. The main action menu stayed on screen underneath the progress bar.
4. "Make Direct/Stream Link" on a freshly sent file answered "Send or download
   a media file first."

Each test drives the real code path with the real rendering and the real
message router, so it fails the same way the deployment did.
"""
import asyncio
import sys
import types
from pathlib import Path

import pytest

from tests.test_download_pipeline import make_bot
from tests.test_ui_semantics import FaithfulClient

main_mod = __import__("app.main", fromlist=["main"])


class Event:
    """A Telethon callback query / new message, as the handlers receive it."""

    def __init__(self, bot, uid, data=None, message=None):
        self.sender_id = uid
        self.chat_id = 1
        self.data = data.encode() if isinstance(data, str) else data
        self.message = message
        self.answers = []
        self.responses = []
        self.is_private = True
        self.id = getattr(message, "id", 0)
        self.raw_text = getattr(message, "raw_text", "")
        self.card = bot.client.messages.get((1, bot.state(uid).card_message_id))

    async def answer(self, text=None, alert=False):
        self.answers.append((text, alert))

    async def respond(self, text=None, **kwargs):
        self.responses.append(text)

    async def reply(self, text=None, **kwargs):
        self.responses.append(text)

    async def edit(self, text, buttons=None, **kwargs):
        return await self.card.edit(text, buttons=buttons, **kwargs)

    async def get_sender(self):
        return types.SimpleNamespace(username="tester", first_name="T")


def telegram_message(mid=1, name="movie.mkv", size=4096, caption=""):
    """A message carrying a real, downloadable Telegram document."""
    return types.SimpleNamespace(
        id=mid, out=False, raw_text=caption, text=caption,
        media=object(), document=object(),
        file=types.SimpleNamespace(name=name, size=size, mime_type="video/x-matroska"),
        photo=None, video=None, audio=None, voice=None, video_note=None,
        entities=None,
    )


def link_post(mid=2, caption="Sardar 2 (2026) 1.6GB ESub.mkv\nFast Download Link", url="https://cdn.example/fast.mkv"):
    """A text message whose only link is hidden behind anchor text.

    Telegram sets ``media`` for the invisible webpage preview, which is what
    made the bot queue this as a phantom media file.
    """
    return types.SimpleNamespace(
        id=mid, out=False, raw_text=caption, text=caption,
        media=object(), file=None,
        document=None, photo=None, video=None, audio=None, voice=None, video_note=None,
        entities=[types.SimpleNamespace(url=url)],
    )


def buttons_of(client, uid, bot):
    card = client.messages.get((1, bot.state(uid).card_message_id))
    return [getattr(b, "text", "") for row in (getattr(card, "buttons", None) or []) for b in row]


# ----------------------------------------------------------------------
# 1. GoFile upload
# ----------------------------------------------------------------------

def test_gofile_upload_does_not_raise_on_the_buttons_argument(tmp_path):
    """``uid`` was passed positionally, colliding with the ``buttons`` kwarg."""
    async def run():
        bot = make_bot(tmp_path, FaithfulClient())
        uid = 1
        payload = tmp_path / "movie.mkv"
        payload.write_bytes(b"x" * 2048)
        st = bot.state(uid)
        st.path = st.source_path = st.root_path = payload
        st.outputs = [payload]

        uploaded = {}

        async def fake_upload(path, token=None, folder_id=None, progress=None, cancel_event=None, attempts=3):
            uploaded["name"] = Path(path).name
            return {"downloadPage": "https://gofile.io/d/abc", "parentFolder": "gid"}

        original = main_mod.upload_gofile
        main_mod.upload_gofile = fake_upload
        try:
            await bot.run_gofile(1, uid)
        finally:
            main_mod.upload_gofile = original
        return bot.client.rendered, uploaded

    rendered, uploaded = asyncio.run(run())
    assert uploaded.get("name") == "movie.mkv", "the GoFile upload never ran"
    assert any("gofile.io/d/abc" in t for t in rendered), rendered
    assert not any("multiple values for argument" in t for t in rendered), rendered


def test_gofile_status_message_is_built_for_the_right_user(tmp_path):
    """The status message must be keyed on the user, not the chat."""
    async def run():
        bot = make_bot(tmp_path, FaithfulClient())
        uid = 4242
        msg = await bot.new_status_message(
            1, "Uploading to GoFile", buttons=[[types.SimpleNamespace(text="Cancel", data=b"cancel")]], uid=uid,
        )
        return bot.state(uid), msg

    st, msg = asyncio.run(run())
    assert st.status_message_id == msg.id
    assert st.status_chat_id == 1


# ----------------------------------------------------------------------
# 2. URL functionality
# ----------------------------------------------------------------------

def test_link_only_text_message_is_queued_as_a_url(tmp_path):
    """A webpage preview must not be mistaken for a media file."""
    async def run():
        bot = make_bot(tmp_path, FaithfulClient())
        bot.cfg.reactions_enabled = False
        uid = 1
        bot.db.register_user(uid, "u", "U", None)
        message = link_post()
        await bot.on_new_message(Event(bot, uid, message=message))
        return bot.state(uid)

    st = asyncio.run(run())
    assert [i.kind for i in st.queue.items] == ["url"], [i.kind for i in st.queue.items]
    assert st.queue.items[0].url == "https://cdn.example/fast.mkv"


def test_real_telegram_file_is_still_queued_as_media(tmp_path):
    async def run():
        bot = make_bot(tmp_path, FaithfulClient())
        bot.cfg.reactions_enabled = False
        uid = 1
        bot.db.register_user(uid, "u", "U", None)
        await bot.on_new_message(Event(bot, uid, message=telegram_message()))
        return bot.state(uid)

    st = asyncio.run(run())
    assert [i.kind for i in st.queue.items] == ["telegram"], [i.kind for i in st.queue.items]


def test_captioned_media_still_goes_to_the_merge_collector(tmp_path):
    """A file with a caption is media, even during a merge session."""
    async def run():
        bot = make_bot(tmp_path, FaithfulClient())
        bot.cfg.reactions_enabled = False
        uid = 1
        bot.db.register_user(uid, "u", "U", None)
        bot.state(uid).pending = "merge_collect"
        bot.state(uid).merge_inputs = [tmp_path / "a.mkv"]
        await bot.on_new_message(Event(bot, uid, message=telegram_message(caption="part 2")))
        st = bot.state(uid)
        return st, len(st.merge_inputs)

    st, count = asyncio.run(run())
    assert st.pending == "merge_collect"
    assert count == 1, "the captioned file was not routed to the merge collector"


# ----------------------------------------------------------------------
# 3. The menu during a download
# ----------------------------------------------------------------------

def test_no_action_menu_while_a_transfer_is_running(tmp_path):
    """The main menu under the progress bar read as "the tap was ignored"."""
    async def run():
        from app.services.bulk import QueueItem

        bot = make_bot(tmp_path, FaithfulClient())
        uid = 5
        st = bot.state(uid)
        item = QueueItem(kind="url", label="big.mkv", chat_id=uid, url="https://x/big.mkv")
        st.queue.add(item)
        claimed = st.queue.claim()
        claimed.current, claimed.total = 512, 2048
        st.view = "queue"
        await bot._render_queue_panel(uid, 1, force=True)
        return bot.client.rendered[-1], buttons_of(bot.client, uid, bot)

    text, buttons = asyncio.run(run())
    assert "Still downloading" in text, text
    assert not any("Thumbnail" in b for b in buttons), buttons
    assert not any("Direct/Stream" in b for b in buttons), buttons
    assert any("Cancel" in b for b in buttons), buttons


def test_the_menu_returns_once_the_file_has_landed(tmp_path):
    """Hiding the menu during the transfer must not hide it forever."""
    async def run():
        bot = make_bot(tmp_path, FaithfulClient())
        uid = 6
        payload = tmp_path / "movie.mkv"
        payload.write_bytes(b"x" * 1024)
        st = bot.state(uid)
        st.path = st.source_path = st.root_path = payload
        st.outputs = [payload]
        await bot.show_file_menu(1, uid, payload)
        return buttons_of(bot.client, uid, bot)

    buttons = asyncio.run(run())
    assert any("Thumbnail" in b for b in buttons), buttons


# ----------------------------------------------------------------------
# 4. Make Direct/Stream Link
# ----------------------------------------------------------------------

def test_direct_link_works_on_a_queued_url(tmp_path):
    """The reported case: a link was sent, Direct Link said "send a file"."""
    async def run():
        bot = make_bot(tmp_path, FaithfulClient())
        bot.cfg.public_base_url = "https://files.example.com"
        bot.cfg.reactions_enabled = False
        uid = 1
        bot.db.register_user(uid, "u", "U", None)
        await bot.on_new_message(Event(bot, uid, message=link_post()))

        target = tmp_path / "fast.mkv"
        target.write_bytes(b"x" * 4096)

        async def fake_download(u, item):
            item.advance(len(target.read_bytes()), len(target.read_bytes()))
            return target

        bot._download_url_item = fake_download
        ev = Event(bot, uid, "menu:direct")
        await bot.callback_router(ev, uid, "menu:direct")
        return bot.state(uid), bot.client.rendered, ev.responses

    st, rendered, responses = asyncio.run(run())
    assert any("Direct/Stream Link" in t for t in rendered), rendered
    assert not any("Send or download a media file first" in t for t in rendered), rendered
    assert st.path is not None and st.path.exists()


def test_direct_link_starts_the_transfer_for_a_telegram_file(tmp_path):
    async def run():
        bot = make_bot(tmp_path, FaithfulClient())
        bot.cfg.public_base_url = "https://files.example.com"
        bot.cfg.reactions_enabled = False
        uid = 1
        bot.db.register_user(uid, "u", "U", None)
        await bot.on_new_message(Event(bot, uid, message=telegram_message()))
        target = tmp_path / "movie.mkv"
        target.write_bytes(b"x" * 4096)
        started = asyncio.Event()

        async def fake_download(u, item):
            started.set()
            return target

        bot._download_telegram_item = fake_download
        await bot.callback_router(Event(bot, uid, "menu:direct"), uid, "menu:direct")
        return bot.state(uid), bot.client.rendered, started.is_set()

    st, rendered, started = asyncio.run(run())
    assert started, "Direct Link never started the queued transfer"
    assert any("Direct/Stream Link" in t for t in rendered), rendered
    assert not any("Send or download a media file first" in t for t in rendered), rendered


def test_a_failed_download_is_not_reported_as_a_missing_file(tmp_path):
    """A real error must survive; the generic message used to hide it."""
    async def run():
        bot = make_bot(tmp_path, FaithfulClient())
        bot.cfg.public_base_url = "https://files.example.com"
        bot.cfg.reactions_enabled = False
        uid = 1
        bot.db.register_user(uid, "u", "U", None)
        await bot.on_new_message(Event(bot, uid, message=link_post(url="https://cdn.example/gone.mkv")))

        async def failing(u, item):
            raise RuntimeError("The server returned an empty response.")

        bot._download_url_item = failing
        await bot.callback_router(Event(bot, uid, "menu:direct"), uid, "menu:direct")
        return bot.client.rendered

    rendered = asyncio.run(run())
    assert any("empty response" in t for t in rendered), rendered
    assert not any("Send or download a media file first" in t for t in rendered), rendered


def test_direct_link_reports_a_missing_public_url_before_downloading(tmp_path):
    """No point spending a 3 GiB transfer on a link the server cannot serve."""
    async def run():
        bot = make_bot(tmp_path, FaithfulClient())
        bot.cfg.public_base_url = None
        bot.cfg.reactions_enabled = False
        uid = 1
        bot.db.register_user(uid, "u", "U", None)
        await bot.on_new_message(Event(bot, uid, message=link_post()))
        started = []

        async def spy(u, item):
            started.append(item)
            return None

        bot._download_url_item = spy
        await bot.callback_router(Event(bot, uid, "menu:direct"), uid, "menu:direct")
        return bot.client.rendered, started

    rendered, started = asyncio.run(run())
    assert not started, "the file was downloaded even though no link can be made"
    assert any("PUBLIC_BASE_URL" in t for t in rendered), rendered


# ----------------------------------------------------------------------
# Uploading a freshly sent link/file
# ----------------------------------------------------------------------

def test_upload_of_a_not_yet_transferred_link_fetches_it_first(tmp_path):
    async def run():
        bot = make_bot(tmp_path, FaithfulClient())
        bot.cfg.reactions_enabled = False
        uid = 1
        bot.db.register_user(uid, "u", "U", None)
        await bot.on_new_message(Event(bot, uid, message=link_post()))
        target = tmp_path / "fast.mkv"
        target.write_bytes(b"x" * 2048)
        fetched = []

        async def fake_download(u, item):
            fetched.append(item.url)
            return target

        async def fake_upload(path, token=None, folder_id=None, progress=None, cancel_event=None, attempts=3):
            fetched.append(Path(path).name)
            return {"downloadPage": "https://gofile.io/d/abc"}

        bot._download_url_item = fake_download
        original = main_mod.upload_gofile
        main_mod.upload_gofile = fake_upload
        try:
            await bot.callback_router(Event(bot, uid, "upload:gofile"), uid, "upload:gofile")
        finally:
            main_mod.upload_gofile = original
        return fetched, bot.client.rendered

    fetched, rendered = asyncio.run(run())
    assert "https://cdn.example/fast.mkv" in fetched, "the queued link was never fetched"
    assert "fast.mkv" in fetched, "the fetched file was never handed to the uploader"
    assert not any("No current file" in t for t in rendered), rendered


def test_url_uploader_does_not_ask_for_another_link(tmp_path):
    """The link is already queued; asking for one is the reported dead end."""
    async def run():
        bot = make_bot(tmp_path, FaithfulClient())
        bot.cfg.reactions_enabled = False
        uid = 1
        bot.db.register_user(uid, "u", "U", None)
        await bot.on_new_message(Event(bot, uid, message=link_post()))
        await bot.callback_router(Event(bot, uid, "menu:urlupload"), uid, "menu:urlupload")
        return bot.state(uid), bot.client.rendered

    st, rendered = asyncio.run(run())
    assert st.pending != "urlupload", "the bot asked for a second link"
    assert not any("Send the HTTP/HTTPS file URL" in t for t in rendered), rendered


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))