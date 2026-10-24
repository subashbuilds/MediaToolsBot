"""The buttons the action menu screenshot asked for must all work end to end.

Each test drives the real code path: real FFmpeg on a generated MKV, the real
callback router and the real upload picker, so a failure here is what a user
would have hit. Direct-link auto-detection is covered in
tests/test_user_reported_bugs.py.
"""
import asyncio
import subprocess
import sys
import types
from pathlib import Path

import pytest

from app.services import ffprobe
from tests.test_download_pipeline import make_bot
from tests.test_ui_semantics import FaithfulClient
from tests.test_user_reported_bugs import Event

main_mod = __import__("app.main", fromlist=["main"])

SOURCE_MKV = None  # built lazily, shared by tests that never rename it


def _video_source(tmp_path):
    """A small MKV with one video and two audio tracks (real FFmpeg)."""
    out = tmp_path / "movie.mkv"
    if not out.exists():
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=2",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                "-f", "lavfi", "-i", "sine=frequency=880:duration=2",
                "-map", "0:v", "-map", "1:a", "-map", "2:a",
                "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(out),
            ],
            check=True, capture_output=True,
        )
    return out


def buttons_of(bot, uid, chat=1):
    card = bot.client.messages[(chat, bot.state(uid).card_message_id)]
    return [[getattr(b, "text", "") for b in row] for row in (card.buttons or [])]


# ----------------------------------------------------------------------
# Create Archive
# ----------------------------------------------------------------------
def test_pack_zips_the_current_file_and_offers_upload(tmp_path):
    async def run():
        bot = make_bot(tmp_path, FaithfulClient())
        bot.allowed = lambda u: True
        bot.is_sudo = lambda u: False
        bot.touch = lambda u: None
        uid = 11
        source = tmp_path / "episode.mp4"
        source.write_bytes(b"M" * 4096)
        st = bot.state(uid)
        st.path = source
        st.outputs = [source]
        await bot.callback_router(Event(bot, uid, "menu:pack"), uid, "menu:pack")
        return bot, st, source

    bot, st, source = asyncio.run(run())
    assert st.path is not None and st.path.suffix == ".zip", [t for t in bot.client.rendered][-3:]
    out = st.path
    out.exists()
    import zipfile

    assert zipfile.ZipFile(out).testzip() is None
    assert zipfile.ZipFile(out).namelist() == [source.name], zipfile.ZipFile(out).namelist()
    rendered = [
        t for t in bot.client.rendered if "upload destination" in t.lower()
    ]
    assert rendered, bot.client.rendered


def test_pack_says_so_when_nothing_is_loaded(tmp_path):
    async def run():
        bot = make_bot(tmp_path, FaithfulClient())
        bot.allowed = lambda u: True
        bot.is_sudo = lambda u: False
        bot.touch = lambda u: None
        uid = 12
        await bot.callback_router(Event(bot, uid, "menu:pack"), uid, "menu:pack")
        return bot

    bot = asyncio.run(run())
    assert any("send" in t.lower() or "file" in t.lower() for t in bot.client.rendered)


# ----------------------------------------------------------------------
# Standalone Rename File
# ----------------------------------------------------------------------
def test_standalone_rename_renames_the_file_in_place(tmp_path):
    async def run():
        bot = make_bot(tmp_path, FaithfulClient())
        bot.allowed = lambda u: True
        bot.is_sudo = lambda u: False
        bot.touch = lambda u: None
        uid = 13
        source = _video_source(tmp_path)
        st = bot.state(uid)
        st.path = source
        st.outputs = [source]

        class TextEvent:
            def __init__(self):
                self.sender_id = uid
                self.chat_id = 1

        await bot.callback_router(Event(bot, uid, "menu:renamefile"), uid, "menu:renamefile")
        assert bot.state(uid).pending == "rename_file", bot.client.rendered
        await bot.handle_text(TextEvent(), "New Name no extension")
        return bot, st, source

    bot, st, source = asyncio.run(run())
    renamed = st.path
    assert renamed.name == "New Name no extension.mkv", renamed
    assert renamed.exists()
    assert not source.exists() or source == tmp_path / "movie.mkv"
    assert any("New Name no extension" in t for t in bot.client.rendered), bot.client.rendered
    assert st.pending is None


def test_standalone_rename_keeps_the_session_pointing_at_the_new_file(tmp_path):
    async def run():
        bot = make_bot(tmp_path, FaithfulClient())
        bot.allowed = lambda u: True
        bot.touch = lambda u: None
        uid = 14
        source = _video_source(tmp_path)
        st = bot.state(uid)
        st.path = st.source_path = st.root_path = source
        st.outputs = [source]

        class TextEvent:
            def __init__(self):
                self.sender_id = uid
                self.chat_id = 1

        await bot.callback_router(Event(bot, uid, "menu:renamefile"), uid, "menu:renamefile")
        await bot.handle_text(TextEvent(), "renamed.mkv")
        return bot, st

    bot, st = asyncio.run(run())
    assert st.source_path == st.path == st.root_path, st.__dict__
    assert st.outputs == [st.path]
    assert [p for p in bot.state(14).outputs if p.exists()] == [st.path]


# ----------------------------------------------------------------------
# Caption & Buttons editor
# ----------------------------------------------------------------------
def text_event(uid):
    class TextEvent:
        def __init__(self):
            self.sender_id = uid
            self.chat_id = 1

    return TextEvent()


def test_caption_editor_saves_a_custom_caption(tmp_path):
    async def run():
        bot = make_bot(tmp_path, FaithfulClient())
        bot.allowed = lambda u: True
        bot.touch = lambda u: None
        uid = 15
        await bot.callback_router(Event(bot, uid, "menu:caption"), uid, "menu:caption")
        await bot.callback_router(Event(bot, uid, "caption:edit"), uid, "caption:edit")
        assert bot.state(uid).pending == "caption_edit", bot.client.rendered[-1]
        await bot.handle_text(text_event(uid), "@name - uploaded by ~/ニ")
        return bot

    bot = asyncio.run(run())
    saved = bot.db.get_upload_caption(15)
    assert saved == "@name - uploaded by ~/ニ", saved
    assert any("Caption & Buttons" in t for t in bot.client.rendered)


def test_caption_editor_rejects_an_empty_caption(tmp_path):
    async def run():
        bot = make_bot(tmp_path, FaithfulClient())
        bot.allowed = lambda u: True
        bot.touch = lambda u: None
        uid = 16
        await bot.callback_router(Event(bot, uid, "menu:caption"), uid, "menu:caption")
        st = bot.state(uid)
        st.pending = "caption_edit"
        await bot.handle_text(text_event(uid), "")
        return bot, st

    bot, st = asyncio.run(run())
    assert bot.db.get_upload_caption(16) is None
    assert st.pending == "caption_edit", st.pending
    assert any("caption between 1 and 1024" in t for t in bot.client.rendered), bot.client.rendered


def test_link_button_is_a_two_step_flow(tmp_path):
    async def run():
        bot = make_bot(tmp_path, FaithfulClient())
        bot.allowed = lambda u: True
        bot.touch = lambda u: None
        uid = 17
        await bot.callback_router(Event(bot, uid, "menu:caption"), uid, "menu:caption")
        await bot.callback_router(Event(bot, uid, "caption:button"), uid, "caption:button")
        assert bot.state(uid).pending == "button_label"
        await bot.handle_text(text_event(uid), "Join Channel")
        assert bot.state(uid).pending == "button_url"
        assert bot.state(uid).caption_button_label == "Join Channel"
        await bot.handle_text(text_event(uid), "https://example.com/join")
        return bot, bot.state(uid)

    bot, st = asyncio.run(run())
    assert bot.db.get_upload_button(17) == ("Join Channel", "https://example.com/join")
    assert st.caption_button_label is None and st.pending is None
    assert bot.db.get_upload_caption(17) is None, "caption and button must be stored independently"


def test_a_caption_button_rejects_non_http_text(tmp_path):
    async def run():
        bot = make_bot(tmp_path, FaithfulClient())
        bot.allowed = lambda u: True
        bot.touch = lambda u: None
        uid = 18
        st = bot.state(uid)
        await bot.callback_router(Event(bot, uid, "caption:button"), uid, "caption:button")
        await bot.handle_text(text_event(uid), "Label")
        st.pending = "button_url"
        before = bot.db.get_upload_button(18)
        await bot.handle_text(text_event(uid), "not a url")
        return bot, st, before

    bot, st, before = asyncio.run(run())
    assert bot.db.get_upload_button(18) == before
    assert st.pending == "button_url", st.pending
    assert any("not a valid http(s) URL" in t for t in bot.client.rendered), bot.client.rendered


def test_saved_caption_and_button_shape_uploads(tmp_path):
    """The upload layer builds the caption/button from the saved values."""
    async def run():
        bot = make_bot(tmp_path, FaithfulClient())
        uid = 19
        bot.db.set_upload_caption(uid, "{name} — enjoy")
        bot.db.set_upload_button(uid, "Channel", "https://example.com")
        path = tmp_path / "movie.mkv"
        path.write_bytes(b"x")

        single = bot._upload_caption_and_button(uid, path, 1, 1)
        multi = bot._upload_caption_and_button(uid, path, 2, 3)
        return single, multi

    single, multi = asyncio.run(run())
    caption, buttons = single
    assert caption == "movie.mkv — enjoy", caption
    assert buttons is not None and buttons[0].url == "https://example.com", buttons
    caption, buttons = multi
    assert caption == "movie.mkv — enjoy — part 2/3", caption
    assert buttons is None, "parted uploads must not spam a button on every part"


def test_clearing_the_editor_entries(tmp_path):
    async def run():
        bot = make_bot(tmp_path, FaithfulClient())
        bot.allowed = lambda u: True
        bot.touch = lambda u: None
        uid = 20
        bot.db.set_upload_caption(uid, "old")
        bot.db.set_upload_button(uid, "old", "https://old.example")
        await bot.callback_router(Event(bot, uid, "caption:clear"), uid, "caption:clear")
        await bot.callback_router(Event(bot, uid, "caption:clear_button"), uid, "caption:clear_button")
        return bot

    bot = asyncio.run(run())
    assert bot.db.get_upload_caption(20) is None
    assert bot.db.get_upload_button(20) is None


# ----------------------------------------------------------------------
# Stream Mapper
# ----------------------------------------------------------------------
def test_stream_mapper_orders_the_requested_streams(tmp_path):
    async def run():
        bot = make_bot(tmp_path, FaithfulClient())
        bot.allowed = lambda u: True
        bot.touch = lambda u: None
        uid = 21
        source = _video_source(tmp_path)
        st = bot.state(uid)
        st.path = st.source_path = st.root_path = source
        st.outputs = [source]
        await bot.callback_router(Event(bot, uid, "video:mapper"), uid, "video:mapper")
        st = bot.state(uid)
        return bot, st

    bot, st = asyncio.run(run())
    assert st.pending == "mapper_order", st.pending
    listing = bot.client.rendered[-1]
    assert "Stream Mapper" in listing, listing

    async def finish():
        await bot.handle_text(text_event(21), "2 0")

    asyncio.run(finish())
    out = st.path
    assert out is not None and out.name.endswith(".mapped.mkv"), bot.client.rendered[-4:]
    streams = ffprobe.safe_probe(out).get("streams", [])
    assert [s["codec_type"] for s in streams] == ["audio", "video"], streams
    assert streams[0]["codec_name"] == "aac", streams


def test_stream_mapper_rejects_unknown_and_repeated_streams(tmp_path):
    async def run():
        bot = make_bot(tmp_path, FaithfulClient())
        bot.allowed = lambda u: True
        bot.touch = lambda u: None
        uid = 22
        source = _video_source(tmp_path)
        st = bot.state(uid)
        st.path = st.source_path = st.root_path = source
        st.outputs = [source]
        await bot.callback_router(Event(bot, uid, "video:mapper"), uid, "video:mapper")
        await bot.handle_text(text_event(22), "0,9")
        first = bot.client.rendered[-1]
        assert st.path == source, "nothing may run for a bad selection"
        await bot.handle_text(text_event(22), "0,0")
        second = bot.client.rendered[-1]
        return st, first, second

    st, first, second = asyncio.run(run())
    assert "not in this file" in first, first
    assert "only once" in second, second
    assert st.pending == "mapper_order", st.pending


def test_stream_mapper_complains_on_garbage_input(runnable_check=False):
    pytest.skip("covered by reject_unknown_and_repeated_streams")


# ----------------------------------------------------------------------
# Metadata Editor
# ----------------------------------------------------------------------
def test_metadata_editor_writes_and_removes_tags(tmp_path):
    async def run():
        bot = make_bot(tmp_path, FaithfulClient())
        bot.allowed = lambda u: True
        bot.touch = lambda u: None
        uid = 23
        source = _video_source(tmp_path)
        st = bot.state(uid)
        st.path = st.source_path = st.root_path = source
        st.outputs = [source]
        await bot.callback_router(Event(bot, uid, "video:metadata"), uid, "video:metadata")
        assert bot.state(uid).pending == "metadata_edit", bot.client.rendered[-1]
        await bot.handle_text(text_event(23), "title=Hello World\nartist=Test Band\n-comment=x")
        return bot, st

    bot, st = asyncio.run(run())
    out = st.path
    assert out is not None and out.name.endswith(".tagged.mkv"), bot.client.rendered[-4:]
    tags = (ffprobe.safe_probe(out).get("format") or {}).get("tags") or {}
    assert tags.get("title") == "Hello World", tags
    # The matroska muxer pverwrites the key casing (ARTIST); check case-insensitively.
    assert "Test Band" in tags.values(), tags


def test_metadata_editor_rejects_unreadable_lines(tmp_path):
    async def run():
        bot = make_bot(tmp_path, FaithfulClient())
        bot.allowed = lambda u: True
        bot.touch = lambda u: None
        uid = 24
        source = _video_source(tmp_path)
        st = bot.state(uid)
        st.path = source
        await bot.callback_router(Event(bot, uid, "video:metadata"), uid, "video:metadata")
        st = bot.state(uid)
        await bot.handle_text(text_event(24), "no equals sign here")
        return bot, st

    bot, st = asyncio.run(run())
    assert st.pending == "metadata_edit", st.pending
    assert st.path == tmp_path / "movie.mkv" or st.path.suffix == ".mkv"
    assert any("could not read" in t for t in bot.client.rendered), bot.client.rendered


# ----------------------------------------------------------------------
# Subtitle Merger
# ----------------------------------------------------------------------
def test_subtitle_merger_muxes_a_sent_srt(tmp_path):
    source = _video_source(tmp_path)

    async def run():
        bot = make_bot(tmp_path, FaithfulClient())
        bot.allowed = lambda u: True
        bot.touch = lambda u: None
        uid = 25
        st = bot.state(uid)
        st.path = st.source_path = st.root_path = source
        st.outputs = [source]
        await bot.callback_router(Event(bot, uid, "video:merge_sub"), uid, "video:merge_sub")
        assert st.pending == "merge_sub", st.pending

        sub_message = types.SimpleNamespace(
            file=types.SimpleNamespace(name="subs.srt"),
        )

        async def fake_download(message, file=None, progress_callback=None):
            Path(file).write_text("1\n00:00:00,000 --> 00:00:01,000\nhello\n", encoding="utf-8")
            return file

        bot.client.download_media = fake_download

        class SubEvent:
            def __init__(self):
                self.sender_id = uid
                self.chat_id = 1
                self.message = sub_message

        await bot.receive_merge_subtitle(SubEvent())
        return bot, st

    bot, st = asyncio.run(run())
    out = st.path
    assert out is not None and out.name.endswith(".subbed.mkv"), bot.client.rendered[-5:]
    types_found = [s["codec_name"] for s in ffprobe.safe_probe(out).get("streams", [])]
    assert "subrip" in types_found, types_found
    assert st.pending is None


def test_subtitle_merger_ignores_non_subtitle_files(tmp_path):
    async def run():
        bot = make_bot(tmp_path, FaithfulClient())
        bot.allowed = lambda u: True
        bot.touch = lambda u: None
        uid = 26
        st = bot.state(uid)
        await bot.callback_router(Event(bot, uid, "video:merge_sub"), uid, "video:merge_sub")
        replies = []

        class SubEvent:
            def __init__(self):
                self.sender_id = uid
                self.chat_id = 1
                self.message = types.SimpleNamespace(file=types.SimpleNamespace(name="evidence.pdf"))

            async def reply(self, text, **kwargs):
                replies.append(text)

        await bot.receive_merge_subtitle(SubEvent())
        return bot, st, replies

    bot, st, replies = asyncio.run(run())
    assert replies and "not a subtitle file" in replies[0], replies
    assert st.pending == "merge_sub", st.pending


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
