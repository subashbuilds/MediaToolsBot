from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import os
import re
import shutil
import subprocess
import time
import secrets
from aiohttp import web
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from telethon import Button, TelegramClient, events
from telethon.errors import MessageNotModifiedError
from telethon.tl.custom.message import Message

from .config import Config
from .storage.db import DB
from .ui.keyboards import (
    admin_menu, archive_collect_menu, archive_mode_menu, archive_password_menu,
    audio_menu, bulk_menu, bulk_upload_menu, cancel_menu, main_menu, merge_menu,
    merge_order_menu, merge_pick_menu, rename_menu, settings_menu, upload_menu, video_menu,
)
from .services import ffmpeg, ffprobe
from .services.archive import extract_archive, is_archive, archive_requires_password, multipart_info
from .services.bulk import DownloadQueue, QueueItem, render_bulk_prompt, render_queue
from .services.downloader import download_url
from .services.telegram_download import download_media_multipart
from .services.gofile import upload_gofile, create_folder
from .services.telegraph import create_info_page
from .services.merge import merge_tracks
from .utils.files import error_text, esc, format_bytes, safe_filename, unique_path
from .utils.progress import ProgressReporter
from .services.system import system_stats_text
from .services.telegram_upload import chunk_file
from telethon import functions, types
from .services import process_control

log = logging.getLogger("media-tools")

# Sentinel: Telegram accepted the edit request but the content was identical.
UNCHANGED = object()

URL_RE = re.compile(r"https?://[^\s<>\"'()\[\]{}]+", re.I)
PHOTO_SUFFIX = ".jpg"
REACTION_INTERVAL = 8.0
PANEL_INTERVAL = 2.0

# Telegram cannot probe a document remotely, so reading the track list of an
# uploaded file means fetching the first few MiB of it: that prefix carries the
# container header ffprobe needs. Without it a 40 GiB upload would have to be
# downloaded in full just to show its audio tracks.
HEAD_PROBE_BYTES = int(os.environ.get("PROBE_HEAD_BYTES", 8 * 1024 * 1024))
HEAD_PROBE_CHUNK = 256 * 1024
# Upper bound for an action that triggered a transfer and is waiting for it.
DOWNLOAD_WAIT = 4 * 60 * 60


def _invoke_job(func, reporter):
    """Call a blocking job, passing the reporter only if it accepts one.

    ``execute`` runs the callable through ``asyncio.to_thread``. Some job
    callables want the progress reporter and some take no arguments at all;
    blindly calling ``func(reporter)`` made every zero-argument job fail with
    "takes 0 positional arguments but 1 was given", which is how "Removing
    stream failed" reached the user.
    """
    try:
        parameters = inspect.signature(func).parameters.values()
    except (TypeError, ValueError):
        return func(reporter)
    for parameter in parameters:
        if parameter.kind is parameter.VAR_POSITIONAL:
            return func(reporter)
        if parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD):
            return func(reporter)
    return func()


def extract_url(text: str) -> str | None:
    """Pull the first http(s) URL out of a free-form message.

    Users routinely send "download this https://… please" instead of a bare
    link; treating that as a media input is far friendlier than a rejection.
    """
    match = URL_RE.search(text or "")
    if not match:
        return None
    return match.group(0).rstrip(".,;:!?'\"»")


def guess_media_name(message, fallback: str) -> str:
    """Derive a sensible filename for an incoming Telegram message."""
    file = getattr(message, "file", None)
    name = getattr(file, "name", None) if file else None
    if name:
        return safe_filename(name)
    if getattr(message, "photo", None):
        return f"photo_{getattr(message, 'id', 0)}{PHOTO_SUFFIX}"
    if getattr(message, "video", None):
        return f"video_{getattr(message, 'id', 0)}.mp4"
    if getattr(message, "audio", None):
        return f"audio_{getattr(message, 'id', 0)}.mp3"
    if getattr(message, "voice", None):
        return f"voice_{getattr(message, 'id', 0)}.ogg"
    if getattr(message, "video_note", None):
        return f"video_note_{getattr(message, 'id', 0)}.mp4"
    # A photo/message without a file part still has a usable MIME type.
    mime = (getattr(file, "mime_type", "") or "") if file else ""
    ext = {
        "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif",
        "video/mp4": ".mp4", "audio/ogg": ".ogg", "audio/mpeg": ".mp3",
    }.get(mime, "")
    if ext:
        return f"media_{getattr(message, 'id', 0)}{ext}"
    return fallback


@dataclass
class UserState:
    path: Path | None = None
    source_path: Path | None = None  # logical source for stream operations
    root_path: Path | None = None   # immutable first input for recovery after extraction
    original_name: str | None = None
    outputs: list[Path] = field(default_factory=list)
    streams: list[dict] = field(default_factory=list)
    pending: str | None = None
    custom_remove: set[int] = field(default_factory=set)
    task: asyncio.Task | None = None
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    merge_inputs: list[Path] = field(default_factory=list)
    # Index of the track the user is currently reordering in the merge screen.
    merge_selected: int = 0
    # Duration in seconds per merge input, filled in by a background probe.
    merge_durations: dict = field(default_factory=dict)
    ui_message_id: int | None = None
    ui_chat_id: int | None = None
    last_activity: float = field(default_factory=time.monotonic)
    timeout_task: asyncio.Task | None = None
    operation: str | None = None
    progress_current: int = 0
    progress_total: int = 0
    started_at: float | None = None
    status_message_id: int | None = None
    status_chat_id: int | None = None
    pending_upload_destination: str | None = None
    pending_admin_action: str | None = None
    archive_parts: list[Path] = field(default_factory=list)
    archive_series: str | None = None
    archive_password: str | None = None
    archive_mode: str | None = None
    start_message_id: int | None = None
    start_chat_id: int | None = None
    menu_message_id: int | None = None
    menu_chat_id: int | None = None
    output_root: Path | None = None
    # -- background download pipeline -----------------------------------
    queue: DownloadQueue = field(default_factory=DownloadQueue)
    download_tasks: set = field(default_factory=set)
    probe_tasks: set = field(default_factory=set)
    worker_task: asyncio.Task | None = None
    panel_task: asyncio.Task | None = None
    queue_cancel: asyncio.Event = field(default_factory=asyncio.Event)
    view: str = "start"
    # The queued item the on-screen menu describes, plus the raw container
    # metadata read from a link header or a short file prefix. Together they
    # let Media Information and the track menus work before anything is
    # transferred.
    current_item: object | None = None
    probe_data: dict | None = None
    # True while a submenu (Video, Audio, Stream Remover, …) is on screen.
    submenu: bool = False
    # "remove" | "extract": which stream screen the user is working in, so the
    # custom-selection screen survives a round trip through a button.
    view_mode: str | None = None
    # Shared throttle for the progress repaint, so the panel edits a message
    # at most once per PROGRESS_INTERVAL however many renderers are running.
    last_panel_at: float = 0.0
    last_panel_text: str | None = None
    last_reaction: float = 0.0
    # The single interactive "card" message: always the newest bot message.
    card_message_id: int | None = None
    card_chat_id: int | None = None

    @property
    def busy(self) -> bool:
        return bool(self.task and not self.task.done())


class MediaToolsBot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.db = DB(cfg.db_path)
        self.client = TelegramClient(str(cfg.work_dir / "media_tools_bot"), cfg.api_id, cfg.api_hash)
        self.states: dict[int, UserState] = {}
        self.semaphore = asyncio.Semaphore(cfg.max_concurrent_jobs)
        # FFmpeg gets its own, much smaller budget: a handful of parallel
        # 1080p transcodes is enough to OOM-kill a 2 GiB container, and a
        # killed container takes every in-flight job with it.
        self.ffmpeg_semaphore = asyncio.Semaphore(cfg.max_ffmpeg_jobs)
        self.client.add_event_handler(self.on_new_message, events.NewMessage)
        self.client.add_event_handler(self.on_callback, events.CallbackQuery)
        self.web_runner: web.AppRunner | None = None
        self.direct_cleanup_task: asyncio.Task | None = None
        self.sweeper_task: asyncio.Task | None = None
        # 0 = unlimited. Telegram's 2 GiB ceiling applies to uploads only, so it must
        # never gate a download.
        self.download_limit = cfg.max_download_mb * 1024 * 1024 if cfg.max_download_mb > 0 else 0

    def state(self, uid: int) -> UserState:
        if uid not in self.states:
            st = UserState()
            st.queue.max_parallel = max(1, self.cfg.max_parallel_downloads)
            self.states[uid] = st
        return self.states[uid]

    async def stop_timeout(self, uid: int) -> None:
        st = self.state(uid)
        if st.timeout_task and not st.timeout_task.done():
            st.timeout_task.cancel()
        st.timeout_task = None

    async def react_to_message(self, event) -> None:
        """React to incoming private messages using a small rotating set.

        This uses Telegram's real MTProto messages.sendReaction method. The
        reaction is rate limited per user because it costs one API call per
        message and quickly earns a flood-wait otherwise.
        """
        if not self.cfg.reactions_enabled:
            return
        st = self.state(int(event.sender_id or 0))
        now = time.monotonic()
        if now - st.last_reaction < REACTION_INTERVAL:
            return
        st.last_reaction = now
        reactions = ("👍", "🔥", "🎉", "❤️")
        try:
            idx = (int(event.message.id) + int(event.sender_id or 0)) % len(reactions)
            await self.client(functions.messages.SendReactionRequest(
                peer=await event.get_input_chat(),
                msg_id=event.message.id,
                reaction=[types.ReactionEmoji(emoticon=reactions[idx])],
                big=False,
                add_to_recent=False,
            ))
        except Exception as exc:
            # Reactions are cosmetic. A missing reaction permission must never
            # break media processing.
            log.debug("reaction failed for message %s: %s", getattr(event.message, "id", "?"), exc)

    async def delete_menu_message(self, uid: int) -> None:
        """Forget the card reference without deleting anything from chat.

        Messages are no longer removed on navigation: deleting the message the
        user is currently tapping is what produced the "menu jumped back to
        home" behaviour.
        """
        st = self.state(uid)
        st.menu_message_id = None
        st.menu_chat_id = None
        st.card_message_id = None
        st.card_chat_id = None
        st.ui_message_id = None
        st.ui_chat_id = None

    def allowed(self, uid: int) -> bool:
        # SUDO_USERS grants elevated controls; it is not a whitelist.
        # ALLOWED_USERS, when set, is an explicit opt-in whitelist.
        if self.cfg.allowed_users:
            return uid in self.cfg.allowed_users or uid in self.cfg.sudo_users
        return True

    def is_sudo(self, uid: int) -> bool:
        return uid in self.cfg.sudo_users

    async def register_user(self, event, uid: int) -> None:
        try:
            sender = await event.get_sender()
            username = getattr(sender, "username", None)
            first_name = getattr(sender, "first_name", None)
            last_name = getattr(sender, "last_name", None)
            is_new = self.db.register_user(uid, username, first_name, last_name)
            if is_new:
                await self.notify_sudo_new_user(uid, username, first_name, last_name)
        except Exception:
            log.exception("failed to register user %s", uid)

    async def notify_sudo_new_user(self, uid: int, username: str | None, first_name: str | None, last_name: str | None) -> None:
        if not self.cfg.sudo_users:
            return
        display = " ".join(x for x in (first_name, last_name) if x).strip() or "Unknown"
        handle = f"@{username}" if username else "No username"
        text = (
            "👤 <b>New user started the bot</b>\n\n"
            f"<b>Name:</b> {display}\n"
            f"<b>Username:</b> {handle}\n"
            f"<b>Telegram ID:</b> <code>{uid}</code>"
        )
        for sudo in self.cfg.sudo_users:
            try:
                await self.client.send_message(sudo, text, parse_mode="html", link_preview=False)
            except Exception:
                log.exception("failed to notify sudo %s about new user", sudo)

    def touch(self, uid: int) -> None:
        st = self.state(uid)
        st.last_activity = time.monotonic()
        if st.timeout_task and not st.timeout_task.done():
            st.timeout_task.cancel()
        st.timeout_task = asyncio.create_task(self._timeout_watch(uid, st.last_activity))

    async def _timeout_watch(self, uid: int, marker: float) -> None:
        try:
            await asyncio.sleep(self.cfg.session_timeout)
        except asyncio.CancelledError:
            return
        st = self.state(uid)
        if st.last_activity != marker:
            return
        await self.timeout_user(uid)

    async def timeout_user(self, uid: int) -> None:
        st = self.state(uid)
        if st.last_activity and time.monotonic() - st.last_activity < self.cfg.session_timeout:
            return
        chat_id = st.ui_chat_id or uid
        if st.busy and st.task is not asyncio.current_task():
            st.cancel_event.set()
            st.task.cancel()
        self.cancel_downloads(uid)
        try:
            await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            pass
        await self.clear_status_message(uid, delete=True)
        self.cleanup_user_files(uid)
        st.path = st.source_path = st.root_path = None
        st.outputs.clear(); st.streams.clear(); st.merge_inputs.clear(); st.archive_parts.clear(); st.output_root = None; st.pending = None
        st.archive_series = st.archive_password = st.archive_mode = None
        st.pending_upload_destination = None; st.pending_admin_action = None
        st.queue.clear()
        st.view = "start"
        st.operation = None; st.progress_current = st.progress_total = 0; st.started_at = None
        try:
            idle = max(1, self.cfg.session_timeout // 3600)
            unit = "hour" if idle == 1 else "hours"
            timeout_text = (
                "⏰ <b>Session timed out</b>\n\nTemporary files and the unfinished workflow were "
                f"removed after {idle} {unit} of inactivity. Valid direct-link files remain until "
                "their link expires. Send the media/URL again to redo the task."
            )
            ui = await self._get_ui_message(uid)
            if ui:
                await self.safe_edit(ui, timeout_text, buttons=[[Button.inline("🏠 Start", b"start:home")]], parse_mode="html")
            else:
                await self.client.send_message(chat_id, timeout_text, parse_mode="html", buttons=[[Button.inline("🏠 Start", b"start:home")]])
        except Exception:
            pass

    def cleanup_user_files(self, uid: int, force: bool = False) -> None:
        """Remove a user's temporary files.

        Files referenced by a valid Direct/Stream Link are protected and removed
        by the link-expiry task instead. Users who enabled "Keep Files" only
        get their directory cleared when a cleanup is explicitly forced.
        """
        if not force and self.db.get_keep_files(int(uid)):
            return
        protected = self.db.active_direct_paths(int(time.time()))
        for root in (self.cfg.download_dir / str(uid), self.cfg.work_dir / str(uid)):
            try:
                if not root.exists():
                    continue
                for item in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
                    if item.is_file() and str(item.resolve()) in protected:
                        continue
                    if item.is_file() or item.is_symlink():
                        item.unlink(missing_ok=True)
                    elif item.is_dir():
                        try:
                            item.rmdir()
                        except OSError:
                            pass
                try:
                    root.rmdir()
                except OSError:
                    pass
            except Exception:
                log.exception("failed to clean user directory %s", root)

    def active_processes_text(self) -> str:
        rows = []
        for uid, st in self.states.items():
            if st.busy:
                username = self.db.get_username(uid) or str(uid)
                running = int(st.task is not None and not st.task.done())
                rows.append((st.started_at or 0, username, running))
        rows.sort(reverse=True)
        if not rows:
            return "🧭 <b>Ongoing Processes</b>\n\nNo ongoing processes."
        lines = ["🧭 <b>Ongoing Processes</b>", "", "<b>User — Processes</b>"]
        for _, username, count in rows:
            handle = f"@{username.lstrip('@')}" if not username.lstrip("@").isdigit() else username
            lines.append(f"• {esc(handle)} — {count}")
        lines.append("\nOnly one process is allowed per normal user.")
        return "\n".join(lines)

    async def new_status_message(self, chat_id: int, text: str, buttons=None, uid: int | None = None, **kwargs):
        # Keyed by the *user*, matching clear_status_message/progress_message.
        # Using chat_id here silently split the status state in group chats.
        st = self.state(uid if uid is not None else chat_id)
        # Delete an older process status before creating another one. This is
        # what prevents multiple stale Cancel Process messages accumulating.
        if st.status_message_id and st.status_chat_id:
            try:
                old = await self.client.get_messages(st.status_chat_id, ids=st.status_message_id)
                if old:
                    await old.delete()
            except Exception:
                pass
        msg = await self.client.send_message(chat_id, text, buttons=buttons, **kwargs)
        st.status_chat_id = chat_id
        st.status_message_id = msg.id
        return msg

    async def cancelled_status(self, uid: int, msg, text="❌ Process cancelled."):
        st = self.state(uid)
        if st.status_message_id == getattr(msg, "id", None):
            await self.safe_edit(msg, text, buttons=None)

    async def clear_status_message(self, uid: int, delete: bool = False, replacement: str | None = None):
        st = self.state(uid)
        if not st.status_message_id or not st.status_chat_id:
            return
        try:
            msg = await self.client.get_messages(st.status_chat_id, ids=st.status_message_id)
            if msg:
                if delete:
                    await msg.delete()
                elif replacement is not None:
                    await self.safe_edit(msg, replacement, buttons=None)
        except Exception:
            pass
        if delete or replacement is not None:
            st.status_message_id = None
            st.status_chat_id = None

    async def safe_edit(self, msg: Message, text: str, buttons=None, **kwargs):
        """Edit a message and report the outcome precisely.

        Returns:
            The edited message on success, ``UNCHANGED`` when Telegram said the
            content was already identical, or ``None`` when the edit genuinely
            failed (deleted message, not our own, no rights...).

        Collapsing "unchanged" and "failed" into ``None`` was the cause of a
        large amount of duplicated menu messages: an unchanged screen made the
        bot send a fresh copy instead of doing nothing.
        """
        try:
            return await msg.edit(text, buttons=buttons, **kwargs)
        except MessageNotModifiedError:
            return UNCHANGED
        except Exception as exc:
            log.debug("edit failed: %s", exc)
            return None

    @staticmethod
    def _editable(result) -> bool:
        """True when the message is still usable as the interactive card."""
        return result is not UNCHANGED and result is not None

    async def answer_event(self, chat_id: int, text: str) -> None:
        try:
            await self.client.send_message(chat_id, text)
        except Exception:
            pass

    async def answer(self, event, text: str, alert: bool = False) -> None:
        """Show a toast above the chat, falling back to a chat reply.

        ``event.respond()`` has no ``alert`` parameter. Passing one raised
        TypeError, which the blanket except swallowed, so every toast in the
        bot silently disappeared and users got no feedback on bulk actions.
        """
        try:
            await event.answer(text, alert=alert)
            return
        except TypeError:
            pass
        except Exception:
            log.debug("toast failed", exc_info=True)
        try:
            await event.respond(text)
        except Exception:
            pass

    def _set_busy(self, uid: int) -> asyncio.Event:
        st = self.state(uid)
        if st.busy:
            raise RuntimeError("Another process is already running for you. Press Cancel first.")
        st.cancel_event = asyncio.Event()
        st.task = asyncio.current_task()
        return st.cancel_event

    def _clear_busy(self, uid: int) -> None:
        st = self.state(uid)
        if st.task is asyncio.current_task():
            st.task = None

    def begin_job(self, uid: int, label: str) -> asyncio.Event:
        """Mark a media job as running for a user.

        ``st.task`` is the coroutine that owns the job so Cancel can interrupt
        it, and the download queue is paused so a freshly finished download
        cannot replace the input file mid-operation.
        """
        st = self.state(uid)
        st.cancel_event = asyncio.Event()
        st.task = asyncio.current_task()
        st.operation = label
        st.progress_current = 0
        st.progress_total = 0
        st.started_at = time.monotonic()
        st.queue.paused = True
        return st.cancel_event

    def end_job(self, uid: int) -> None:
        st = self.state(uid)
        st.queue.paused = False
        if st.task is asyncio.current_task():
            st.task = None
        st.operation = None
        st.progress_current = st.progress_total = 0
        st.started_at = None

    def refresh_activity(self, uid: int) -> None:
        """Keep the idle timer alive while a long job is making progress."""
        st = self.state(uid)
        st.last_activity = time.monotonic()
        if st.timeout_task and not st.timeout_task.done():
            st.timeout_task.cancel()
        st.timeout_task = asyncio.create_task(self._timeout_watch(uid, st.last_activity))

    async def progress_message(self, msg: Message, uid: int | None = None, operation: str | None = None):
        def hook(current, total, label):
            if uid is not None:
                self.refresh_activity(uid)
                st = self.state(uid)
                st.operation = operation or label
                st.progress_current = int(current or 0)
                st.progress_total = int(total or 0)
        return ProgressReporter(
            lambda text: self.safe_edit(msg, text, buttons=cancel_menu()),
            self.cfg.progress_interval,
            progress_hook=hook,
        )

    def make_ffmpeg_job(self, uid: int, fn, source: Path | None = None):
        """Build a job callable for ``execute`` that reports FFmpeg progress.

        FFmpeg emits progress from a reader thread, so the position is only
        recorded here; ``execute`` owns the coroutine that turns it into
        throttled Telegram message edits.
        """
        reporter_holder: dict[str, ProgressReporter] = {}

        def job(reporter=None) -> Path:
            if reporter is not None:
                reporter_holder["r"] = reporter
            total = 0.0
            if source is not None:
                try:
                    total = ffmpeg.duration(source)
                except Exception:
                    total = 0.0

            def sink(seconds: float) -> None:
                rep = reporter_holder.get("r")
                if rep is None:
                    return
                try:
                    rep.queue_progress(int(seconds * 1000), int(total * 1000) if total > 0 else 0)
                except Exception:
                    pass

            try:
                return fn(sink)
            except TypeError:
                return fn()

        return job

    async def _flush_ffmpeg_progress(self, reporter: ProgressReporter, label: str, stop: asyncio.Event) -> None:
        """Turn queued FFmpeg positions into throttled message edits."""
        try:
            while not stop.is_set():
                await asyncio.sleep(2.0)
                current, total = reporter.take_pending()
                if current is None:
                    continue
                await reporter.update(current, total, label)
        except asyncio.CancelledError:
            return
        except Exception:
            log.debug("progress flush stopped", exc_info=True)

    async def _get_ui_message(self, uid: int):
        st = self.state(uid)
        if not st.ui_message_id or not st.ui_chat_id:
            return None
        try:
            return await self.client.get_messages(st.ui_chat_id, ids=st.ui_message_id)
        except Exception:
            return None

    async def render_card(
        self,
        chat_id: int,
        uid: int,
        text: str,
        buttons=None,
        parse_mode: str | None = "html",
        link_preview: bool = False,
        fresh: bool = False,
    ):
        """Render the user's single interactive card.

        The card is always the newest bot message. ``fresh=True`` sends a new
        one, which is what happens after the user supplies a new input so the
        menu ends up *below* their file instead of far above it. Everything
        else edits the existing card in place, so the chat never fills up with
        near-identical menus.
        """
        st = self.state(uid)
        self.touch(uid)
        # Telethon clears an inline keyboard only when buttons is explicitly
        # None/[]; omitting the argument silently keeps the old markup.
        markup = buttons if buttons else []
        if not fresh:
            msg = await self._current_card(st, chat_id)
            if msg is not None:
                result = await self.safe_edit(msg, text, buttons=markup, parse_mode=parse_mode, link_preview=link_preview)
                if self._editable(result) or result is UNCHANGED:
                    st.ui_chat_id = chat_id
                    st.ui_message_id = msg.id
                    return msg
        try:
            msg = await self.client.send_message(
                chat_id, text, buttons=markup or None, parse_mode=parse_mode, link_preview=link_preview,
            )
        except Exception as exc:
            log.warning("could not send card: %s", exc)
            return None
        st.ui_chat_id = chat_id
        st.ui_message_id = msg.id
        st.card_chat_id = chat_id
        st.card_message_id = msg.id
        return msg

    async def _current_card(self, st: UserState, chat_id: int):
        """Fetch the card message, returning None if it is gone."""
        for cid, mid in self._card_refs(st):
            try:
                msg = await self.client.get_messages(cid, ids=mid)
            except Exception:
                continue
            if msg:
                return msg
        return None

    @staticmethod
    def _card_refs(st: UserState):
        refs = []
        if st.card_message_id and st.card_chat_id:
            refs.append((st.card_chat_id, st.card_message_id))
        if st.ui_message_id and st.ui_chat_id:
            refs.append((st.ui_chat_id, st.ui_message_id))
        if st.menu_message_id and st.menu_chat_id:
            refs.append((st.menu_chat_id, st.menu_message_id))
        return refs

    async def render_ui(self, chat_id, uid, text, buttons=None, parse_mode=None, link_preview=False, source=None, kind="menu"):
        """Backwards-compatible wrapper around the card renderer."""
        if kind != "start":
            return await self.render_card(chat_id, uid, text, buttons, parse_mode or "html", link_preview)
        # Start/help screens are their own stable message.
        st = self.state(uid)
        if source is not None and getattr(source, "id", None):
            msg = source
        elif st.start_message_id and st.start_chat_id:
            try:
                msg = await self.client.get_messages(st.start_chat_id, ids=st.start_message_id)
            except Exception:
                msg = None
        else:
            msg = None
        markup = buttons if buttons else []
        if msg is not None:
            result = await self.safe_edit(msg, text, buttons=markup, parse_mode=parse_mode or "html", link_preview=link_preview)
            if self._editable(result) or result is UNCHANGED:
                st.start_chat_id = chat_id
                st.start_message_id = msg.id
                st.view = kind
                return msg
        msg = await self.client.send_message(chat_id, text, buttons=markup or None, parse_mode=parse_mode or "html", link_preview=link_preview)
        st.start_chat_id = chat_id
        st.start_message_id = msg.id
        st.view = kind
        return msg

    async def send_help(self, entity, uid: int, source=None):
        text = (
            "<b>📚 Media Tools Bot — Help</b>\n\n"
            "<b>📥 Input</b>\n"
            "• Send a Telegram media/document or an HTTP/HTTPS URL — a link inside a longer message works too.\n"
            "• The action menu appears <b>immediately</b> and files keep downloading in the background.\n\n"
            "<b>📥 Bulk mode</b> (Settings → Bulk Mode)\n"
            "• Turn it on and the bot asks you to keep sending files after every one it receives.\n"
            "• Several files transfer in parallel while you pick an action.\n"
            "• <b>Done Adding</b> opens the normal menu; <b>Upload All</b> sends everything at once.\n\n"
            "<b>🛠️ Processing</b>\n"
            "• Video/audio stream remove and extract\n"
            "• Detailed Media Information → Telegraph\n"
            "• Trim, optimize, split, sample, screenshots and conversions\n"
            "• Merge Tracks muxes streams; it does not concatenate timelines\n"
            "• Archives: ZIP/TAR/7z/RAR where the host extractor supports them\n"
            "• Password-protected and multi-part archives\n\n"
            "<b>📤 Upload</b>\n"
            "• Telegram uses MTProto/Telethon, not the HTTP Bot API file-transfer path\n"
            "• Files larger than 1.95 GiB are uploaded as numbered Telegram parts\n"
            "• Telegram upload mode: Document or Media\n"
            "• GoFile uses a reusable folder; guest uploads work without a token\n"
            "• URL Uploader can send the current file or download a fresh URL and then ask for the destination\n\n"
            "<b>🖼️ Personal settings</b>\n"
            "• Rename before upload\n"
            "• Default upload destination\n"
            "• Document/Media upload mode\n"
            "• Bulk mode, delete-after-upload, keep-files\n"
            "• Permanent custom Telegram thumbnail\n\n"
            "<b>🔗 Direct links</b>\n"
            "• The bot can serve a downloaded file over HTTP with browser playback, downloads and Range requests.\n"
            "• Direct-linked files are retained for 24 hours by default.\n\n"
            "<b>🧹 Cleanup</b>\n"
            "• Temporary files are removed when a task completes or is cancelled.\n"
            "• Idle media sessions expire automatically.\n\n"
            "<b>Commands</b>\n"
            "<code>/start</code> <code>/help</code> <code>/settings</code> <code>/upload</code> <code>/urlupload</code> <code>/merge</code> <code>/direct</code> <code>/cancel</code> <code>/status</code>\n"
            "<code>/rename on|off</code> <code>/uploadmode telegram|gofile|choose</code>\n"
            "<code>/bulk on|off</code> <code>/setgofile TOKEN</code> <code>/cleargofile</code>\n\n"
            "Use Back to return to the previous dashboard."
        )
        buttons=[[Button.inline("⬅️ Back", b"start:home")]]
        return await self.render_ui(entity, uid, text, buttons=buttons, parse_mode="html", source=source, kind="start")

    async def send_start_info(self, entity, uid: int, source=None):
        st = self.state(uid)
        # Never delete the card here: when the user taps Back/Cancel the card is
        # the very message being clicked, and deleting it left the user staring
        # at a fresh "home" screen with no explanation of what happened.
        me = await self.client.get_me()
        username = f"@{me.username}" if getattr(me, "username", None) else str(getattr(me, "id", "unknown"))
        buttons = [
            [Button.inline("📊 Bot Status", b"start:status"), Button.inline("🖥️ System Stats", b"start:stats")],
            [Button.inline("📚 Help", b"start:help"), Button.inline("⚙️ Settings", b"start:settings")],
        ]
        if self.is_sudo(uid):
            buttons.append([Button.inline("🛡️ Admin Controls", b"admin:menu")])
        text = (
            "<b>🎬 Media Tools Bot</b>\n\n"
            f"<b>Status:</b> 🟢 Online\n"
            f"<b>Account:</b> {username}\n"
            "<b>Transport:</b> Telegram MTProto (Telethon)\n"
            f"<b>FFmpeg:</b> {'available' if shutil.which('ffmpeg') else 'missing'}\n"
            f"<b>FFprobe:</b> {'available' if shutil.which('ffprobe') else 'missing'}\n"
            f"<b>Your process:</b> {'Running' if st.busy else 'Idle'}\n"
            f"<b>Global limit:</b> {self.cfg.max_concurrent_jobs}\n\n"
            "Send a <b>Telegram media file</b> or an <b>HTTP/HTTPS media URL</b> to begin.\n"
            "The functionality menu appears only after an input is available."
        )
        return await self.render_ui(entity, uid, text, buttons=buttons, parse_mode="html", source=source, kind="start")

    async def send_settings(self, entity, uid: int, source=None):
        rename = self.db.get_rename(uid)
        mode = self.db.get_upload_mode(uid)
        telegram_mode = self.db.get_telegram_mode(uid)
        bulk = self.db.get_bulk_mode(uid)
        auto_delete = self.db.get_auto_delete(uid)
        keep_files = self.db.get_keep_files(uid)
        mode_label = {"telegram": "Telegram", "gofile": "GoFile", "choose": "Choose before upload"}[mode]
        tg_label = "Document" if telegram_mode == "document" else "Media"
        thumb = self.db.get_thumbnail(uid)
        parallel = max(1, self.cfg.max_parallel_downloads)
        text = (
            "⚙️ <b>Settings</b>\n\n"
            f"✏️ <b>Rename File:</b> {'Yes' if rename else 'No'}\n"
            f"📤 <b>Upload Destination:</b> {mode_label}\n"
            f"📄 <b>Telegram Upload:</b> {tg_label}\n"
            f"📥 <b>Bulk Mode:</b> {'On — you will be asked to send more files' if bulk else 'Off'}\n"
            f"🧹 <b>Delete After Upload:</b> {'On' if auto_delete else 'Off'}\n"
            f"📦 <b>Keep Files:</b> {'On — temporary files are preserved' if keep_files else 'Off'}\n"
            f"🖼️ <b>Custom Thumbnail:</b> {'Set' if thumb else 'Not set'}\n\n"
            f"<i>Parallel downloads: {parallel} • Max download: "
            f"{f'{self.cfg.max_download_mb} MiB' if self.cfg.max_download_mb else 'Unlimited'}</i>"
        )
        return await self.render_ui(
            entity, uid, text,
            buttons=settings_menu(rename, mode, telegram_mode, bool(thumb), bulk, auto_delete, keep_files),
            parse_mode="html", source=source, kind="start",
        )

    async def send_bot_status(self, entity, uid: int, source=None):
        st = self.state(uid)
        total, done, running, failed = st.queue.summary()
        text = (
            "<b>📊 Bot Status</b>\n\n"
            "<b>Transport:</b> MTProto / Telethon\n"
            "<b>Authorization:</b> Telegram bot authorization over MTProto\n"
            f"<b>FFmpeg:</b> {'OK' if shutil.which('ffmpeg') else 'MISSING'}\n"
            f"<b>FFprobe:</b> {'OK' if shutil.which('ffprobe') else 'MISSING'}\n"
            f"<b>Direct link server:</b> {'Configured' if self.cfg.public_base_url else 'Needs PUBLIC_BASE_URL'}\n"
            f"<b>Your job:</b> {'Running' if st.busy else 'Idle'}\n"
            f"<b>Pending workflow:</b> {esc(st.pending) or 'None'}\n"
            f"<b>Current operation:</b> {esc(st.operation) or 'None'}\n"
            f"<b>Current file:</b> {esc(st.path.name) if st.path else 'None'}\n"
            f"<b>Download queue:</b> {total} total / {done} ready / {running} active"
            + (f" / {failed} failed" if failed else "") + "\n"
            f"<b>Merge inputs:</b> {len(st.merge_inputs)}\n"
            f"<b>Global concurrency:</b> {self.cfg.max_concurrent_jobs} (FFmpeg: {self.cfg.max_ffmpeg_jobs})"
        )
        buttons=[[Button.inline("🖥️ System Stats", b"start:stats"), Button.inline("🏠 Start", b"start:home")]]
        if self.is_sudo(uid): buttons.append([Button.inline("🛡️ Admin Controls", b"admin:menu")])
        return await self.render_ui(entity, uid, text, buttons=buttons, parse_mode="html", source=source, kind="start")

    async def send_system_stats(self, entity, uid: int, source=None):
        st = self.state(uid)
        text = system_stats_text(self.cfg, st)
        buttons=[[Button.inline("🔄 Refresh", b"start:stats"), Button.inline("📊 Bot Status", b"start:status")], [Button.inline("🏠 Start", b"start:home")]]
        if self.is_sudo(uid): buttons.insert(1, [Button.inline("🛡️ Admin Controls", b"admin:menu")])
        return await self.render_ui(entity, uid, text, buttons=buttons, parse_mode="html", source=source, kind="start")

    async def send_main(self, entity, uid: int, prefix: str | None = None, source=None):
        self.state(uid).submenu = False
        text = (prefix + "\n\n" if prefix else "") + "Please select your preferred action below 👇"
        return await self.render_ui(entity, uid, text, buttons=main_menu(), source=source)

    async def send_ongoing_processes(self, entity, uid: int, source=None):
        if not self.is_sudo(uid):
            return await self.send_start_info(entity, uid, source=source)
        text = self.active_processes_text()
        return await self.render_ui(entity, uid, text, buttons=[[Button.inline("🔄 Refresh", b"admin:processes"), Button.inline("🏠 Start", b"start:home")]], parse_mode="html", source=source, kind="start")

    async def on_new_message(self, event):
        if not event.is_private:
            return
        if getattr(event.message, "out", False):
            return
        uid = event.sender_id
        if uid is None or not self.allowed(uid):
            return
        await self.react_to_message(event)
        await self.register_user(event, uid)
        self.touch(uid)
        text = (event.raw_text or "").strip()
        if text.startswith("/"):
            await self.handle_command(event, text)
        elif self.state(uid).pending == "archive_collect" and event.message.media:
            await self.receive_archive_part(event)
        elif self.state(uid).pending == "custom_thumbnail" and event.message.media:
            await self.receive_custom_thumbnail(event)
        elif self.state(uid).pending == "merge_collect" and event.message.media:
            # Media messages (including audio/document messages with captions)
            # must be routed to the merge collector before the generic pending
            # text handler.
            await self.receive_media(event)
        elif self.state(uid).pending == "archive_collect" and re.match(r"^https?://\S+$", text, re.I):
            await self.process_archive_part_url(event.chat_id, uid, text)
        elif self.state(uid).pending == "merge_collect" and re.match(r"^https?://\S+$", text, re.I):
            # Merge collection accepts both Telegram files and HTTP(S) URLs.
            await self.process_merge_url(event.chat_id, uid, text)
        elif self.state(uid).pending:
            # Pending workflows (trim, URL uploader, archive, upload, etc.)
            # must receive the text before the generic URL handler.
            await self.handle_text(event, text)
        elif re.match(r"^https?://\S+$", text, re.I):
            # Telegram may attach a WebPage preview to a plain URL. Treat the
            # URL as the actual input instead of passing the preview to
            # download_media(), which can legitimately return None.
            await self.process_url(event.chat_id, uid, text)
        elif event.message.media:
            await self.receive_media(event)
        elif extract_url(text):
            # A link embedded in a sentence is still a download request.
            await self.process_url(event.chat_id, uid, extract_url(text))
        elif text:
            await self.handle_text(event, text)

    async def handle_command(self, event, text: str):
        parts = text.split(maxsplit=1)
        cmd = parts[0].split("@", 1)[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        uid = event.sender_id
        if cmd == "/start":
            await self.send_start_info(event.chat_id, uid)
        elif cmd == "/menu":
            await self.send_main(event.chat_id, uid)
        elif cmd == "/help":
            await self.send_help(event.chat_id, uid)
        elif cmd == "/cancel":
            await self.cancel(uid, event.chat_id)
        elif cmd == "/upload":
            if arg.lower() in ("telegram", "gofile"):
                await self.choose_upload(event.chat_id, uid, arg.lower())
            else:
                await self.choose_upload(event.chat_id, uid)
        elif cmd == "/setgofile":
            if not arg:
                await event.reply("Usage: /setgofile YOUR_GOFILE_API_TOKEN")
            else:
                self.db.set_gofile_token(uid, arg)
                await event.reply("✅ GoFile API token saved for your account.")
        elif cmd == "/cleargofile":
            self.db.set_gofile_token(uid, None)
            await event.reply("✅ GoFile token cleared. Uploads will use the guest account.")
        elif cmd == "/settings":
            await self.send_settings(event.chat_id, uid)
        elif cmd == "/uploadmode":
            if arg.lower() not in ("telegram", "gofile", "choose"):
                await event.reply("Usage: /uploadmode telegram | gofile | choose")
            else:
                self.db.set_upload_mode(uid, arg.lower())
                await self.send_settings(event.chat_id, uid)
        elif cmd == "/canceluser":
            if not self.is_sudo(uid):
                await event.reply("❌ Sudo only.")
            elif not arg:
                await event.reply("Usage: /canceluser USER_ID or @username")
            else:
                await self.admin_cancel_user(event.chat_id, uid, arg)
        elif cmd == "/broadcast":
            if not self.is_sudo(uid):
                await event.reply("Sudo only.")
            elif not arg:
                self.state(uid).pending_admin_action = "broadcast"
                await self.render_ui(
                    event.chat_id, uid,
                    "📢 <b>Broadcast</b>\n\nSend the message to broadcast to all registered users.",
                    buttons=[[Button.inline("Cancel", b"cancel")]], parse_mode="html",
                )
            else:
                await self.admin_broadcast(event.chat_id, uid, arg)
        elif cmd == "/bulk":
            if arg.lower() not in ("on", "off", "yes", "no", "true", "false", "1", "0"):
                await event.reply("Usage: /bulk on or /bulk off")
            else:
                self.db.set_bulk_mode(uid, arg.lower() in ("on", "yes", "true", "1"))
                await self.send_settings(event.chat_id, uid)
        elif cmd == "/rename":
            if arg.lower() not in ("on", "off", "yes", "no", "true", "false", "1", "0"):
                await event.reply("Usage: /rename on or /rename off")
            else:
                value = arg.lower() in ("on", "yes", "true", "1")
                self.db.set_rename(uid, value)
                await self.send_settings(event.chat_id, uid)
        elif cmd == "/direct":
            await self.run_direct(event.chat_id, uid)
        elif cmd == "/merge":
            await self.start_merge(event.chat_id, uid)
        elif cmd == "/urlupload":
            if not arg and self.state(uid).path and self.state(uid).path.exists():
                await self.choose_upload(event.chat_id, uid)
            elif not arg:
                self.state(uid).pending = "urlupload"
                await self.render_ui(event.chat_id, uid, "🔗 <b>URL Uploader</b>\n\nSend the file URL to download and upload.", buttons=cancel_menu(), parse_mode="html")
            else:
                await self.process_url(event.chat_id, uid, arg)
        elif cmd == "/status":
            await self.send_bot_status(event.chat_id, uid)
        else:
            await event.reply("Unknown command. Use /start.")

    # ------------------------------------------------------------------
    # Background download pipeline
    # ------------------------------------------------------------------
    def cancel_downloads(self, uid: int) -> None:
        """Stop every in-flight/queued download for a user."""
        st = self.state(uid)
        st.queue_cancel.set()
        st.queue.clear()
        st.current_item = None
        st.probe_data = None
        for task in list(st.download_tasks):
            task.cancel()
        st.download_tasks.clear()
        if st.panel_task and not st.panel_task.done():
            st.panel_task.cancel()
        st.panel_task = None
        if st.worker_task and not st.worker_task.done():
            st.worker_task.cancel()
        st.worker_task = None
        st.queue_cancel = asyncio.Event()

    def _ensure_panel(self, uid: int) -> None:
        """Keep the queue panel ticking while downloads are in flight."""
        st = self.state(uid)
        if st.panel_task and not st.panel_task.done():
            return
        st.panel_task = asyncio.create_task(self._panel_loop(uid))

    async def _panel_loop(self, uid: int) -> None:
        st = self.state(uid)
        try:
            while st.queue.has_work():
                # Only repaint while the user is actually looking at the queue;
                # if they navigated into a submenu the progress is still
                # tracked, it just must not overwrite their current screen.
                if st.view == "queue":
                    await self._render_queue_panel(uid, chat_id=st.ui_chat_id or uid)
                await asyncio.sleep(self._panel_interval())
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("queue panel failed for user %s", uid)
        finally:
            st.panel_task = None

    async def _render_queue_panel(self, uid: int, chat_id: int, source=None, force: bool = False, fresh: bool = False):
        """Repaint the screen that belongs to the current input.

        Three states share one card:

        * an input is waiting for the user to pick something - the action menu
          plus whatever the header probe already knows about the file;
        * a transfer is running - the real progress bar, with the familiar
          buttons still in place so the user is never stuck;
        * the file is here - the normal per-file menu.

        ``fresh`` sends a brand new card. That is required for the very first
        panel after a new input: editing the previous card left the menu
        stranded far *above* the file the user had just sent.
        """
        st = self.state(uid)
        if not force and st.view != "queue":
            return None
        bulk = self.db.get_bulk_mode(uid)
        total, done, running, _failed = st.queue.summary()
        if total == 0 and st.path is None:
            return await self.show_file_menu(chat_id, uid, None, source=source)
        if not bulk:
            # Only a *running* transfer means there is progress to show. A
            # queued item is still waiting for the user to pick an action, and
            # painting a download bar for it would be a lie.
            inflight = st.queue.running()
            if inflight:
                # Reuse the shared renderer so the running item shows a real
                # bar, byte counts, speed and ETA. It used to print only the
                # filename, which made a healthy download look completely frozen.
                if not self._panel_due(st, fresh):
                    return None
                text = render_queue(st.queue, None, bulk=False)
                buttons = cancel_menu() if st.submenu else main_menu()
                return await self._render_panel(chat_id, uid, st, text, buttons)
            if st.path is not None:
                return await self.show_file_menu(chat_id, uid, st.path, fresh=fresh)
            return await self.render_card(
                chat_id, uid, self._pending_card_text(uid), buttons=main_menu(), fresh=fresh,
            )
        active = st.path.name if st.path else None
        if running and not self._panel_due(st, fresh):
            return None
        text = render_queue(st.queue, active, bulk=True)
        return await self._render_panel(chat_id, uid, st, text, bulk_menu(done, running))

    def _panel_interval(self) -> float:
        """Cadence of the progress repaint, in seconds.

        Driven by PROGRESS_INTERVAL (3s by default). Telegram limits how often
        one message can be edited, so the panel must not repaint faster than
        the operator asked for, no matter how many renderers are active.
        """
        try:
            # The floor only guards against a nonsensical 0; an operator who
            # asks for a 1s panel gets a 1s panel.
            return max(0.5, float(self.cfg.progress_interval))
        except (AttributeError, TypeError, ValueError):
            return PANEL_INTERVAL

    def _panel_due(self, st: UserState, fresh: bool) -> bool:
        """True when enough time passed to repaint the progress card again.

        Two renderers can be active at once - the loop that waits for the
        transfer and the bulk panel ticker - and each used to repaint on its own
        schedule. One shared timestamp keeps the total rate at the configured
        interval.
        """
        if fresh:
            return True
        now = time.monotonic()
        if now - st.last_panel_at < self._panel_interval():
            return False
        st.last_panel_at = now
        return True

    async def _render_panel(self, chat_id, uid, st, text: str, buttons) -> None:
        """Repaint the progress card, skipping edits that would change nothing."""
        if text == st.last_panel_text and st.card_message_id:
            return None
        st.last_panel_text = text
        return await self.render_card(chat_id, uid, text, buttons=buttons)

    def _pending_card_text(self, uid: int) -> str:
        """The card for an input that has not been downloaded yet."""
        st = self.state(uid)
        item = st.current_item
        name = esc(item.display_name[:70]) if item is not None else "media"
        size = 0
        if item is not None:
            size = int(item.expected_size or item.total or 0)
        lines = ["🕒 <b>Ready</b>", "", f"📁 <b>{name}</b>" + (f" — {format_bytes(size)}" if size else "")]
        streams = st.streams or (item.streams if item is not None else None) or []
        if streams:
            counts = [
                f"{sum(1 for s in streams if s.get('codec_type') == t)} {label}"
                for t, label in (("video", "video"), ("audio", "audio"), ("subtitle", "subtitle"))
            ]
            counts = [c for c in counts if not c.startswith("0 ")]
            if counts:
                lines.append("🎞 " + " • ".join(counts) + "  <i>(read from the file header)</i>")
        lines += [
            "",
            "⬇️ <i>Nothing has been downloaded yet. The file is fetched only when you "
            "pick an action, and the progress is shown here.</i>",
            "",
            "Please select your preferred action below 👇",
        ]
        return "\n".join(lines)

    def _start_worker(self, uid: int) -> None:
        """Begin transferring the queued inputs, with live progress."""
        st = self.state(uid)
        st.queue.paused = False
        if not st.worker_task or st.worker_task.done():
            st.worker_task = asyncio.create_task(self._queue_worker(uid))
        if self.db.get_bulk_mode(uid):
            # Bulk users watch the queue itself, so it needs its own ticker.
            self._ensure_panel(uid)

    async def enqueue_input(self, chat_id: int, uid: int, item: QueueItem, source=None) -> QueueItem:
        """Accept a new input and show the action menu - without downloading it.

        The transfer used to start here, so a link or a large upload was
        fetched whether or not the user ever wanted it. Now the file is only
        transferred once an action is chosen, and ``await_input`` performs the
        download with progress. Container metadata is still read up front (a
        header request for links, a short prefix for Telegram files) so Media
        Information and the track menus do not need the whole media.
        """
        st = self.state(uid)
        st.queue.add(item)
        st.current_item = item
        st.path = st.source_path = st.root_path = None
        st.outputs.clear()
        st.streams.clear()
        st.probe_data = None
        st.view = "menu"
        st.submenu = False
        st.last_panel_text = None
        st.last_panel_at = 0.0
        st.queue_cancel.clear()
        st.card_message_id = None
        st.card_chat_id = None
        # Drop the old card references too, otherwise render_card would edit
        # the previous screen and the new menu would appear above the file.
        st.ui_message_id = None
        st.ui_chat_id = None
        st.menu_message_id = None
        st.menu_chat_id = None
        await self._render_queue_panel(uid, chat_id, source=source, force=True, fresh=True)
        self._ensure_metadata(uid, item)
        return item

    async def _queue_worker(self, uid: int) -> None:
        st = self.state(uid)
        try:
            while True:
                item = st.queue.claim()
                if item is None:
                    if not st.queue.has_work():
                        break
                    # Either the parallel limit is reached or a media job
                    # paused the queue; wait for a slot instead of spinning.
                    await asyncio.sleep(PANEL_INTERVAL)
                    continue
                task = asyncio.create_task(self._run_queue_item(uid, item))
                st.download_tasks.add(task)
                task.add_done_callback(st.download_tasks.discard)
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("download worker failed for user %s", uid)

    async def _run_queue_item(self, uid: int, item: QueueItem) -> None:
        st = self.state(uid)
        chat_id = st.ui_chat_id or uid
        try:
            async with self.semaphore:
                if item.kind == "url":
                    path = await self._download_url_item(uid, item)
                else:
                    path = await self._download_telegram_item(uid, item)
            if not path or not path.exists() or path.stat().st_size == 0:
                raise RuntimeError("The transfer produced an empty file")
            item.path = path
            st.queue.release(item)
            self._promote_item(st, path, item)
            # Fill in the track list for anything the remote probe missed.
            st.probe_tasks.add(asyncio.create_task(self._probe_downloaded_metadata(uid, item)))
            st.probe_tasks = {t for t in st.probe_tasks if not t.done()}
        except asyncio.CancelledError:
            task = getattr(item, "probe_task", None)
            if task is not None and not task.done():
                task.cancel()
            if item.path is not None:
                item.path.unlink(missing_ok=True)
            st.queue.mark_failed(item, "cancelled")
            raise
        except Exception as exc:
            log.warning("download failed for %s: %s", item.display_name, exc)
            st.queue.mark_failed(item, error_text(exc, 160))
        finally:
            with contextlib.suppress(Exception):
                if st.view == "queue" and st.queue.has_work():
                    await self._render_queue_panel(uid, chat_id, force=True)
            with contextlib.suppress(Exception):
                if not st.queue.has_work() and st.view == "queue":
                    await self.show_file_menu(chat_id, uid, st.path)

    def _promote_item(self, st: UserState, path: Path, item: QueueItem) -> None:
        """Make a freshly downloaded file the active one, if nothing is running."""
        if st.busy:
            return
        st.path = path
        st.source_path = path
        st.root_path = path
        st.original_name = item.display_name
        st.outputs = [path]
        # The promoted file is the item we already probed, so its track list
        # still applies. Blanking it here lost the user's Stream Remover
        # selection in the moment the download completed.
        st.streams = list(item.streams)
        st.probe_data = item.probe_data

    def _ensure_metadata(self, uid: int, item: QueueItem) -> None:
        """Kick off the header probe for a freshly queued input."""
        st = self.state(uid)
        task = asyncio.create_task(self._probe_item_metadata(uid, item))
        item.probe_task = task
        st.probe_tasks.add(task)
        task.add_done_callback(st.probe_tasks.discard)

    async def _wait_for_metadata(self, uid: int, timeout: float = 90.0) -> None:
        """Let the background header probe finish before giving up on it."""
        st = self.state(uid)
        tasks = [t for t in st.probe_tasks if not t.done()]
        if not tasks:
            return
        with contextlib.suppress(Exception):
            await asyncio.wait(tasks, timeout=timeout)

    async def _probe_item_metadata(self, uid: int, item: QueueItem) -> None:
        """Fill in ``item.streams`` from a header read, not the whole file.

        For a link ffprobe reads the container header over HTTP. For a Telegram
        document only a short prefix is fetched, which is all a track list or a
        Media Information page needs. Best effort: any failure leaves the item
        unprobed and the menus fall back to probing the downloaded file later.
        """
        if item.probed:
            return
        item.probed = True
        try:
            if item.url:
                data = await asyncio.to_thread(ffprobe.safe_probe_remote, item.url)
            else:
                data = await self._probe_head(uid, item)
        except Exception:
            log.debug("metadata probe failed for %s", item.display_name, exc_info=True)
            return
        if not data:
            return
        item.probe_data = data
        fmt = data.get("format") or {}
        streams = data.get("streams") or []
        if streams:
            item.streams = streams
        duration = fmt.get("duration")
        try:
            item.duration = float(duration) if duration else None
        except (TypeError, ValueError):
            item.duration = None
        try:
            declared = int(fmt.get("size") or 0)
        except (TypeError, ValueError):
            declared = 0
        if declared and not item.expected_size:
            item.expected_size = declared
        # If this item is the one on screen, refresh the menu so the user can
        # pick an audio track - and read the file size - straight away.
        st = self.state(uid)
        if st.current_item is not item or st.busy:
            return
        st.probe_data = data
        if not st.path:
            st.streams = streams
        with contextlib.suppress(Exception):
            if st.view == "menu" and st.card_message_id:
                await self._render_queue_panel(uid, st.ui_chat_id or st.card_chat_id or uid, force=True)

    async def _probe_head(self, uid: int, item: QueueItem) -> dict:
        """Probe only the first few MiB of a Telegram file, then discard them.

        ``TelegramClient.iter_download`` yields the raw chunks; the prefix is
        written here and probed as a stand-in container. The whole file is never
        fetched, and the prefix is removed as soon as it has been read.
        """
        message = item.message
        if message is None or not hasattr(self.client, "iter_download"):
            return {}
        suffix = Path(item.display_name).suffix or ".bin"
        dest = self.cfg.work_dir / str(uid) / "probe"
        dest.mkdir(parents=True, exist_ok=True)
        out = dest / f"head_{item.item_id}{suffix}"
        chunks = max(1, HEAD_PROBE_BYTES // HEAD_PROBE_CHUNK)
        try:
            written = 0
            with open(out, "wb") as fh:
                # ``limit`` bounds the iterator, so it finishes on its own.
                async for chunk in self.client.iter_download(
                    message, request_size=HEAD_PROBE_CHUNK, limit=chunks,
                ):
                    fh.write(chunk)
                    written += len(chunk)
            if not written or not out.exists():
                return {}
            return await asyncio.to_thread(ffprobe.safe_probe, out)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug("head probe failed for %s", item.display_name, exc_info=True)
            return {}
        finally:
            out.unlink(missing_ok=True)

    async def _probe_downloaded_metadata(self, uid: int, item: QueueItem) -> None:
        """Probe a finished local file so the track menus are pre-populated."""
        if item.probed and item.streams:
            return
        path = item.path
        if path is None or not path.exists():
            return
        try:
            data = await asyncio.to_thread(ffprobe.safe_probe, path)
        except Exception:
            log.debug("local probe failed for %s", path, exc_info=True)
            return
        streams = data.get("streams") or []
        if not streams:
            return
        item.streams = streams
        item.probed = True
        st = self.state(uid)
        if not st.busy and st.path is not None and st.path == path:
            st.streams = streams
            st.probe_data = data

    async def _download_url_item(self, uid: int, item: QueueItem) -> Path:
        st = self.state(uid)
        dest = self.cfg.download_dir / str(uid)

        async def cb(current: int, total: int) -> None:
            if st.queue_cancel.is_set():
                raise asyncio.CancelledError
            item.advance(int(current or 0), int(total or 0))
            self.refresh_activity(uid)

        return await download_url(
            item.url, dest, cb, st.queue_cancel, max_bytes=self.download_limit,
        )

    async def _download_telegram_item(self, uid: int, item: QueueItem) -> Path:
        st = self.state(uid)
        dest = self.cfg.download_dir / str(uid)
        message = item.message
        if message is None:
            message = await self.client.get_messages(item.chat_id, ids=item.message_id)
        if message is None:
            raise RuntimeError("The original Telegram message is no longer available")
        file = getattr(message, "file", None)
        if file is None:
            raise RuntimeError("This message no longer contains a file")
        out = unique_path(dest, item.display_name)
        size = int(getattr(file, "size", 0) or item.expected_size or 0)

        async def cb(current: int, total: int) -> None:
            if st.queue_cancel.is_set():
                raise asyncio.CancelledError
            item.advance(int(current or 0), int(total or size or 0))
            self.refresh_activity(uid)

        result = await download_media_multipart(
            self.client, message, out,
            file_size=size,
            parts=self.cfg.telegram_parts,
            progress=cb,
            cancel_event=st.queue_cancel,
            min_bytes=self.cfg.telegram_parts_min_mb * 1024 * 1024,
        )
        if not result:
            raise RuntimeError("Telegram returned no downloaded file")
        return out

    async def show_file_menu(self, chat_id: int, uid: int, path: Path | None, source=None, plain: bool = False, fresh: bool = True):
        st = self.state(uid)
        if path is None:
            if st.current_item is not None or st.queue.has_work():
                # An input is waiting for the user to choose something; it has
                # not been downloaded, so the menu describes it instead of
                # asking for a file that is already there.
                return await self.render_card(
                    chat_id, uid, self._pending_card_text(uid),
                    buttons=main_menu(), fresh=fresh, parse_mode="html",
                )
            st.path = st.source_path = st.root_path = None
            st.outputs.clear()
            st.streams.clear()
            return await self.send_main(chat_id, uid, source=source)
        st.view = "menu"
        st.submenu = False
        bulk = self.db.get_bulk_mode(uid)
        archive_note = "\n📦 <b>Archive detected:</b> Extract Archive is available." if is_archive(path) else ""
        inflight = st.queue.pending() + st.queue.running()
        hint = f"\n⬇️ <i>{len(inflight)} more file(s) still downloading in the background.</i>" if inflight else ""
        # In bulk mode the prompt tells the user to press "Done Adding", so the
        # matching bulk keyboard has to be shown with it. "Done Adding" passes
        # plain=True to leave bulk collection behind.
        bulk_active = bool(bulk and not plain and (st.queue.finished() or inflight))
        if bulk_active:
            text = render_bulk_prompt(st.queue, path)
            if inflight:
                text += hint
            buttons = bulk_menu(len(st.queue.finished()), len(st.queue.running()))
        else:
            text = (
                f"📁 <b>{esc(path.name)}</b>\n{format_bytes(self._safe_size(path))}{archive_note}{hint}"
                "\n\nPlease select your preferred action below 👇"
            )
            buttons = main_menu()
        # A new input always gets a brand new card, so the menu appears
        # directly *below* the file the user just sent.
        return await self.render_card(chat_id, uid, text, buttons=buttons, fresh=bool(fresh))

    async def receive_media(self, event):
        uid = event.sender_id
        st = self.state(uid)
        if st.pending == "merge_collect":
            await self.download_merge_input(event)
            return
        if st.pending == "archive_collect":
            await self.receive_archive_part(event)
            return
        if st.pending == "custom_thumbnail":
            await self.receive_custom_thumbnail(event)
            return
        if st.busy:
            await event.reply("⚠️ A process is already running. Press Cancel first.", buttons=cancel_menu())
            return
        message = event.message
        name = guess_media_name(message, f"telegram_{getattr(message, 'id', 0)}.bin")
        file = getattr(message, "file", None)
        item = QueueItem(
            kind="telegram",
            label=name,
            chat_id=event.chat_id,
            message_id=getattr(message, "id", None),
            expected_size=int(getattr(file, "size", 0) or 0) if file else 0,
            message=message,
        )
        await self.enqueue_input(event.chat_id, uid, item)

    async def receive_custom_thumbnail(self, event):
        uid = event.sender_id
        st = self.state(uid)
        if st.busy:
            await event.reply("⚠️ Finish or cancel the current process first.")
            return
        file = event.message.file
        mime = getattr(file, "mime_type", "") or ""
        if not getattr(event.message, "photo", None) and not mime.startswith("image/"):
            await event.reply("❌ Send an image (JPG/PNG/WebP) for the custom thumbnail.")
            return
        thumb_dir = self.cfg.work_dir.parent / "user_data" / str(uid)
        thumb_dir.mkdir(parents=True, exist_ok=True)
        ext = (Path(file.name).suffix if file and file.name else ".jpg") or ".jpg"
        if ext.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
            ext = ".jpg"
        target = thumb_dir / f"custom_thumbnail{ext.lower()}"
        old = self.db.get_thumbnail(uid)
        try:
            result = await self.client.download_media(event.message, file=str(target))
            if not result:
                raise RuntimeError("Telegram returned no thumbnail file")
            # Normalize/validate through Pillow and store a stable JPEG path.
            from PIL import Image
            normalized = thumb_dir / "custom_thumbnail.jpg"
            with Image.open(target) as im:
                im.convert("RGB").save(normalized, "JPEG", quality=92)
            if target != normalized:
                target.unlink(missing_ok=True)
            if old and old != str(normalized):
                Path(old).unlink(missing_ok=True)
            self.db.set_thumbnail(uid, str(normalized))
            st.pending = None
            await self.send_settings(event.chat_id, uid)
        except Exception as exc:
            target.unlink(missing_ok=True)
            await event.reply(f"Custom thumbnail failed: {esc(error_text(exc))}", parse_mode="html")

    async def process_url(self, chat_id: int, uid: int, url: str):
        st = self.state(uid)
        if st.busy:
            await self.render_card(chat_id, uid, "⚠️ A process is already running. Press Cancel first.", buttons=cancel_menu())
            return
        st.pending = None
        name = url.rsplit("/", 1)[-1].split("?")[0] or url
        item = QueueItem(kind="url", label=safe_filename(name, "download.bin"), chat_id=chat_id, url=url)
        await self.enqueue_input(chat_id, uid, item)

    async def run_archive(self, chat_id, uid, source=None):
        st = self.state(uid)
        await self.await_input(chat_id, uid)
        path = st.path
        if not path or not path.exists() or not is_archive(path):
            await self.render_ui(chat_id, uid, "❌ Send an archive file first.", buttons=main_menu(), source=source)
            return
        st.archive_parts = [path.resolve()]
        st.archive_series = multipart_info(path)[0] if multipart_info(path) else None
        st.archive_password = None
        st.archive_mode = None
        await self.render_ui(
            chat_id, uid,
            f"📦 <b>Extract Archive</b>\n\n<b>File:</b> {safe_filename(path.name)}\n<b>Size:</b> {format_bytes(path.stat().st_size)}\n\nChoose extraction mode.",
            buttons=archive_mode_menu(), parse_mode="html", source=source,
        )

    @staticmethod
    def _part_sort_key(path: Path) -> int:
        """Order archive parts numerically, falling back to 1 for single files."""
        info = multipart_info(path)
        return info[1] if info else 1

    async def _begin_archive_extract(self, chat_id: int, uid: int, password: str | None = None):
        st = self.state(uid)
        parts = [p for p in st.archive_parts if p.exists() and p.is_file()]
        if not parts:
            await self.render_ui(chat_id, uid, "No archive parts are available.", buttons=main_menu())
            return
        first = sorted(parts, key=self._part_sort_key)[0]
        # A timestamped directory is not unique enough: two extractions started
        # in the same second reused the same folder and mixed their contents.
        outdir = self.cfg.work_dir / str(uid) / f"extracted_{int(time.time())}_{secrets.token_hex(4)}"
        st.pending = None
        st.archive_password = password
        status = await self.new_status_message(chat_id, "Extracting archive\u2026", uid=uid, buttons=cancel_menu())
        self.begin_job(uid, "Extracting archive")
        try:
            # Extraction is as CPU/disk heavy as a transcode, so it shares the
            # FFmpeg budget instead of the much larger transfer budget.
            async with self.ffmpeg_semaphore:
                token = process_control.set_job(uid)
                try:
                    out = await asyncio.to_thread(extract_archive, first, outdir, password)
                finally:
                    process_control.reset_job(token)
            if st.cancel_event.is_set():
                raise asyncio.CancelledError
            files = sorted(p for p in out.rglob("*") if p.is_file())
            if not files:
                raise RuntimeError("The archive was extracted but contained no files")
            st.outputs = files
            st.output_root = outdir
            st.path = files[0]
            st.source_path = first
            st.root_path = first
            st.streams = []
            total = sum(self._safe_size(p) for p in files)
            st.archive_parts.clear()
            st.archive_series = None
            st.archive_password = None
            st.archive_mode = None
            st.task = None
            # Keep the confirmation on screen: deleting it immediately meant
            # the user never saw how many files were extracted.
            await self.safe_edit(
                status,
                f"<b>Archive extracted</b>\n<b>Files:</b> {len(files)}\n<b>Total:</b> {format_bytes(total)}",
                buttons=[[Button.inline("Continue", b"upload:back")]], parse_mode="html",
            )
            st.status_message_id = None
            st.status_chat_id = None
            st.queue.paused = False
            # Extraction is complete; now choose where the extracted tree goes.
            await self.choose_upload(chat_id, uid)
        except asyncio.CancelledError:
            shutil.rmtree(outdir, ignore_errors=True)
            await self.cancelled_status(uid, status, "Extraction cancelled.")
        except Exception as exc:
            log.exception("archive extraction failed")
            shutil.rmtree(outdir, ignore_errors=True)
            text = str(exc)
            if "password" in text.lower() or "encrypt" in text.lower():
                st.pending = "archive_password"
                await self.safe_edit(
                    status,
                    "<b>Archive password required</b>\n\nSend the password or choose No Password.",
                    buttons=archive_password_menu(), parse_mode="html",
                )
                st.status_message_id = None
                st.status_chat_id = None
            else:
                await self.safe_edit(
                    status, f"Extraction failed:\n{esc(error_text(exc))}",
                    buttons=[[Button.inline("Back", b"menu:back")]], parse_mode="html",
                )
        finally:
            self.end_job(uid)

    async def begin_archive_normal(self, chat_id: int, uid: int):
        st = self.state(uid)
        st.archive_mode = "normal"
        st.archive_parts = [st.path.resolve()] if st.path and st.path.exists() else []
        if not st.archive_parts:
            await self.render_ui(chat_id, uid, "❌ Archive is no longer available.", buttons=main_menu())
            return
        try:
            needs = await asyncio.to_thread(archive_requires_password, st.archive_parts[0])
        except Exception:
            needs = False
        if needs:
            st.pending = "archive_password"
            await self.render_ui(chat_id, uid, "🔐 <b>Password-protected archive</b>\n\nSend the archive password or choose No Password.", buttons=archive_password_menu(), parse_mode="html")
        else:
            await self._begin_archive_extract(chat_id, uid, None)

    async def begin_archive_multi(self, chat_id: int, uid: int):
        st = self.state(uid)
        if not st.archive_parts:
            return
        st.archive_mode = "multi"
        st.pending = "archive_collect"
        key = st.archive_series or (multipart_info(st.archive_parts[0])[0] if multipart_info(st.archive_parts[0]) else None)
        st.archive_series = key
        await self.render_ui(chat_id, uid, self.archive_collect_text(st), buttons=archive_collect_menu(), parse_mode="html")

    def archive_collect_text(self, st: UserState) -> str:
        rows = []
        for p in sorted(st.archive_parts, key=lambda x: multipart_info(x)[1] if multipart_info(x) else 1):
            info = multipart_info(p)
            suffix = f" — part {info[1]}" if info else ""
            rows.append(f"{len(rows)+1}. {safe_filename(p.name)}{suffix} — {format_bytes(p.stat().st_size)}")
        return (
            "🧩 <b>Multi-Part Archive</b>\n\n"
            f"<b>Parts received:</b> {len(st.archive_parts)}\n" + "\n".join(rows) +
            "\n\nSend the remaining parts as Telegram files or HTTP/HTTPS URLs.\nWhen all parts are present, press <b>Extract Parts</b>."
        )

    async def _accept_archive_part(self, chat_id: int, uid: int, path: Path):
        st = self.state(uid)
        info = multipart_info(path)
        if not info or (st.archive_series and info[0] != st.archive_series):
            path.unlink(missing_ok=True)
            await self.render_ui(chat_id, uid, "❌ This file does not belong to the current multi-part archive.", buttons=archive_collect_menu())
            return
        numbers = {multipart_info(p)[1] for p in st.archive_parts if multipart_info(p)}
        if info[1] in numbers:
            path.unlink(missing_ok=True)
            await self.render_ui(chat_id, uid, f"⚠️ Part {info[1]} is already queued.", buttons=archive_collect_menu())
            return
        st.archive_parts.append(path.resolve())
        await self.render_ui(chat_id, uid, self.archive_collect_text(st), buttons=archive_collect_menu(), parse_mode="html")
        self.touch(uid)

    async def receive_archive_part(self, event):
        uid = event.sender_id
        st = self.state(uid)
        if st.busy:
            await event.reply("⚠️ A process is already running. Wait for it to finish or cancel it.")
            return
        message = event.message
        file = getattr(message, "file", None)
        if file is None:
            await event.reply("❌ That message does not contain a file.")
            return
        name = safe_filename(getattr(file, "name", None) or f"archive_part_{getattr(message, 'id', 0)}.bin")
        out = unique_path(self.cfg.download_dir / str(uid) / "archive_parts", name)
        status = await self.new_status_message(event.chat_id, f"📥 Downloading archive part…\n{esc(name)}", uid=uid, buttons=cancel_menu(), parse_mode="html")
        reporter = await self.progress_message(status, uid=uid)
        self.begin_job(uid, "Downloading archive part")
        try:
            async def cb(cur, total):
                if st.cancel_event.is_set():
                    raise asyncio.CancelledError
                await reporter.update(cur, int(total or getattr(file, "size", 0) or 0), "📥 Downloading archive part")
            result = await self.client.download_media(message, file=str(out), progress_callback=cb)
            if not result:
                raise RuntimeError("Telegram returned no downloaded file")
            await self.clear_status_message(uid, delete=True)
            await self._accept_archive_part(event.chat_id, uid, out)
        except asyncio.CancelledError:
            out.unlink(missing_ok=True)
            await self.cancelled_status(uid, status)
        except Exception as exc:
            out.unlink(missing_ok=True)
            await self.safe_edit(status, f"❌ Archive part download failed:\n{esc(error_text(exc))}", buttons=None, parse_mode="html")
        finally:
            self.end_job(uid)

    async def process_archive_part_url(self, chat_id: int, uid: int, url: str):
        st = self.state(uid)
        if st.busy:
            await self.render_card(chat_id, uid, "A process is already running. Wait for it to finish or cancel it.", buttons=cancel_menu())
            return
        status = await self.new_status_message(chat_id, "Downloading archive part URL\u2026", uid=uid, buttons=cancel_menu())
        reporter = await self.progress_message(status, uid=uid)
        out = None
        self.begin_job(uid, "Downloading archive part")
        try:
            async def cb(cur, total):
                if st.cancel_event.is_set():
                    raise asyncio.CancelledError
                await reporter.update(cur, total, "Downloading archive part URL")
            out = await download_url(
                url, self.cfg.download_dir / str(uid) / "archive_parts", cb, st.cancel_event,
                max_bytes=self.download_limit,
            )
            await self.clear_status_message(uid, delete=True)
            await self._accept_archive_part(chat_id, uid, out)
        except asyncio.CancelledError:
            if isinstance(out, Path):
                out.unlink(missing_ok=True)
            await self.cancelled_status(uid, status)
        except Exception as exc:
            if isinstance(out, Path):
                out.unlink(missing_ok=True)
            await self.safe_edit(status, f"Archive part download failed:\n{esc(error_text(exc))}", buttons=None, parse_mode="html")
        finally:
            self.end_job(uid)

    async def process_merge_url(self, chat_id: int, uid: int, url: str):
        """Download an HTTP(S) URL as an additional merge input."""
        st = self.state(uid)
        if st.busy:
            await self.render_card(chat_id, uid, "A process is already running. Press Cancel first.", buttons=cancel_menu())
            return
        status = await self.new_status_message(chat_id, "Downloading merge URL\u2026", uid=uid, buttons=cancel_menu())
        reporter = await self.progress_message(status, uid=uid)
        out = None
        self.begin_job(uid, "Downloading merge URL")
        try:
            async def cb(cur, total):
                if st.cancel_event.is_set():
                    raise asyncio.CancelledError
                await reporter.update(cur, total, "Downloading merge URL")
            async with self.semaphore:
                out = await download_url(
                    url, self.cfg.download_dir / str(uid) / "merge", cb, st.cancel_event,
                    max_bytes=self.download_limit,
                )
            st.merge_inputs.append(out.resolve())
            st.probe_tasks.add(asyncio.create_task(self._probe_merge_duration(uid, out.resolve())))
            st.probe_tasks = {t for t in st.probe_tasks if not t.done()}
            await self.safe_edit(status, f"Added merge URL: {esc(out.name)}\nTracks queued: {len(st.merge_inputs)}", buttons=None, parse_mode="html")
            st.status_message_id = None; st.status_chat_id = None
            await self.render_ui(chat_id, uid, self.merge_status_text(st), buttons=self._merge_buttons(st), parse_mode="html")
            self.touch(uid)
        except asyncio.CancelledError:
            if isinstance(out, Path):
                out.unlink(missing_ok=True)
            await self.cancelled_status(uid, status)
        except Exception as exc:
            if isinstance(out, Path):
                out.unlink(missing_ok=True)
            await self.safe_edit(status, f"Merge URL download failed:\n{esc(error_text(exc))}", buttons=None, parse_mode="html")
        finally:
            self.end_job(uid)

    async def handle_text(self, event, text: str):
        uid = event.sender_id
        st = self.state(uid)
        text = (text or "").strip()
        if st.pending_admin_action == "cancel_user" and self.is_sudo(uid):
            st.pending_admin_action = None
            await self.admin_cancel_user(event.chat_id, uid, text)
            return
        if st.pending_admin_action == "broadcast" and self.is_sudo(uid):
            st.pending_admin_action = None
            await self.admin_broadcast(event.chat_id, uid, text)
            return
        if st.pending == "archive_password":
            await self._begin_archive_extract(event.chat_id, uid, text)
            return
        if st.pending == "rename_input":
            await self.apply_rename_and_continue(event.chat_id, uid, text)
            return
        if st.pending == "urlupload":
            url = extract_url(text)
            if url and re.match(r"^https?://", url, re.I):
                await self.process_url(event.chat_id, uid, url)
            else:
                await event.reply("Please send a valid http(s) link.")
            return
        if st.pending == "trim":
            await self.run_trim(event.chat_id, uid, text, audio=False); return
        if st.pending == "audio_trim":
            await self.run_trim(event.chat_id, uid, text, audio=True); return
        if st.pending == "manual_shot":
            await self.run_manual_shot(event.chat_id, uid, text); return
        if st.pending == "shots_count":
            await self.run_shots(event.chat_id, uid, text); return
        if st.pending == "split":
            await self.run_split(event.chat_id, uid, text); return
        if st.pending == "sample":
            await self.run_sample(event.chat_id, uid, text); return
        if st.pending == "audio_speed":
            # Validate before starting the job: a ValueError raised by FFmpeg
            # later used to be reported as "invalid speed", which is confusing.
            try:
                factor = float(text)
            except ValueError:
                await event.reply("Enter a speed between 0.5 and 2.0, for example 1.25")
                return
            if not 0.5 <= factor <= 2.0:
                await event.reply("Enter a speed between 0.5 and 2.0, for example 1.25")
                return
            await self.run_audio_filter(event.chat_id, uid, f"atempo={factor:g}", "speed")
            return
        if st.pending == "audio_volume":
            volume = self._parse_volume(text)
            if volume is None:
                await event.reply("Enter a volume like 1.5, 0.5 or -3dB.")
                return
            await self.run_audio_filter(event.chat_id, uid, f"volume={volume}", "volume")
            return
        if st.pending == "merge_collect":
            # Re-render the queue screen instead of posting yet another copy
            # of the same merge status into the chat.
            await self.render_ui(event.chat_id, uid, self.merge_status_text(st), buttons=self._merge_buttons(st), parse_mode="html")
            return
        if st.pending in {"archive_collect", "rename_choice"}:
            await self.answer(event, "Send a file or link, or press Cancel.")
            return
        await event.reply("Send a media file or an http(s) link, or use /help to see everything I can do.")

    @staticmethod
    def _parse_volume(text: str) -> str | None:
        """Validate a volume expression before it reaches the filter graph."""
        value = (text or "").strip()
        if not value or len(value) > 16:
            return None
        if re.fullmatch(r"[+-]?\d+(\.\d+)?dB", value, re.I):
            number = float(value[:-2])
            if not -100 <= number <= 100:
                return None
            return value
        try:
            number = float(value)
        except ValueError:
            return None
        if not 0.0 <= number <= 10.0:
            return None
        return f"{number:g}"

    async def on_callback(self, event):
        uid = event.sender_id
        if uid is None or not self.allowed(uid):
            await event.answer("Not authorized", alert=True)
            return
        self.touch(uid)
        try:
            await event.answer()
        except Exception:
            pass
        try:
            await self.callback_router(event, uid, event.data.decode("utf-8", "ignore"))
        except Exception as exc:
            log.exception("callback failed")
            try:
                await event.respond(f"Something went wrong: {esc(error_text(exc, 200))}")
            except Exception:
                pass

    @staticmethod
    def _opens_submenu(data: str) -> bool:
        """True when a callback leads to a screen below the main menu.

        The download panel keeps the user's place: it shows only a Cancel
        button while a submenu is open, instead of yanking them back to the
        main menu mid-operation.
        """
        if data.endswith(":back") or data == "cancel":
            return False
        return data.startswith((
            "menu:", "video:", "audio:", "stream:", "streamtoggle:",
            "aconv:", "archive", "merge", "upload:", "bulkupload:",
        ))

    async def callback_router(self, event, uid: int, data: str):
        st = self.state(uid)
        # Any button press other than the queue's own refresh means the user has
        # navigated away from the download panel. Without this the background
        # panel kept repainting over the submenu, so tapping a button appeared
        # to open the next menu and then snap straight back to the main menu.
        if not data.startswith("bulk:"):
            st.view = "menu"
            # Remember that the card now shows a submenu, not the main menu.
            # The download panel used to repaint over an open submenu with the
            # main menu, so tapping "Custom Streams" looked like a jump home.
            st.submenu = self._opens_submenu(data)
        if data == "cancel":
            await self.cancel(uid, event.chat_id, event); return
        if data == "start:home":
            await self.send_start_info(event.chat_id, uid, source=event); return
        if data == "start:status":
            await self.send_bot_status(event.chat_id, uid, source=event); return
        if data == "start:stats":
            await self.send_system_stats(event.chat_id, uid, source=event); return
        if data == "start:help":
            await self.send_help(event.chat_id, uid, source=event); return
        if data == "start:settings":
            await self.send_settings(event.chat_id, uid, source=event); return
        if data == "admin:menu":
            if not self.is_sudo(uid):
                await self.send_start_info(event.chat_id, uid, source=event); return
            await self.render_ui(event.chat_id, uid, f"🛡️ <b>Admin Controls</b>\n\n👥 <b>Total users:</b> {self.db.user_count()}", buttons=admin_menu(), parse_mode="html", source=event, kind="start"); return
        if data == "admin:users":
            if self.is_sudo(uid):
                await self.render_ui(event.chat_id, uid, f"👥 <b>Users</b>\n\nTotal registered users: <b>{self.db.user_count()}</b>", buttons=[[Button.inline("🔄 Refresh", b"admin:users"), Button.inline("⬅️ Back", b"admin:menu")]], parse_mode="html", source=event, kind="start")
            return
        if data == "admin:processes":
            await self.send_ongoing_processes(event.chat_id, uid, source=event); return
        if data == "admin:cancel_user":
            if self.is_sudo(uid):
                st.pending_admin_action = "cancel_user"
                await self.render_ui(event.chat_id, uid, "🛑 <b>Cancel User Job</b>\n\nSend a Telegram user ID or @username.", buttons=[[Button.inline("Cancel", b"cancel")]], parse_mode="html", source=event, kind="start")
            return
        if data == "admin:broadcast":
            if self.is_sudo(uid):
                st.pending_admin_action = "broadcast"
                await self.render_ui(event.chat_id, uid, "📢 <b>Broadcast</b>\n\nSend the message to broadcast to all registered users.", buttons=[[Button.inline("Cancel", b"cancel")]], parse_mode="html", source=event, kind="start")
            return
        if data.startswith("settings:rename:"):
            value = data.rsplit(":", 1)[1] == "on"
            self.db.set_rename(uid, value)
            await self.send_settings(event.chat_id, uid, source=event); return
        if data.startswith("settings:upload:"):
            mode = data.rsplit(":", 1)[1]
            self.db.set_upload_mode(uid, mode)
            await self.send_settings(event.chat_id, uid, source=event); return
        if data.startswith("settings:telegram_mode:"):
            mode = data.rsplit(":", 1)[1]
            self.db.set_telegram_mode(uid, mode)
            await self.send_settings(event.chat_id, uid, source=event); return
        if data == "settings:thumbnail:set":
            st.pending = "custom_thumbnail"
            await self.render_ui(event.chat_id, uid, "🖼️ <b>Custom Thumbnail</b>\n\nSend the image you want to store permanently for your Telegram media uploads.", buttons=[[Button.inline("⬅️ Back", b"start:settings"), Button.inline("Cancel", b"cancel")]], parse_mode="html", source=event, kind="start"); return
        if data == "settings:thumbnail:remove":
            old = self.db.get_thumbnail(uid)
            if old: Path(old).unlink(missing_ok=True)
            self.db.set_thumbnail(uid, None)
            await self.send_settings(event.chat_id, uid, source=event); return
        if data == "help:main":
            await self.send_main(event.chat_id, uid, source=event); return
        if data == "menu:video":
            await self.safe_edit(event, "Please select your preferred action below 👇", buttons=video_menu()); return
        if data == "menu:audio":
            await self.safe_edit(event, "Please select your preferred action below 👇", buttons=audio_menu()); return
        if data == "menu:back":
            await self.send_start_info(event.chat_id, uid, source=event); return
        if data == "video:back" or data == "audio:back":
            await self.safe_edit(event, "Please select your preferred action below 👇", buttons=main_menu()); return
        if data == "menu:thumb":
            await self.run_thumbnail(event.chat_id, uid); return
        if data == "menu:direct":
            await self.run_direct(event.chat_id, uid, source=event); return
        if data == "menu:urlupload":
            if st.path and st.path.exists():
                await self.render_ui(event.chat_id, uid, "📤 <b>URL/File Uploader</b>\n\nChoose where to upload the current file.", buttons=upload_menu(), parse_mode="html", source=event)
            else:
                st.pending = "urlupload"
                await self.safe_edit(event, "🔗 <b>URL Uploader</b>\n\nSend the HTTP/HTTPS file URL. After it downloads, choose Telegram or GoFile.", buttons=cancel_menu(), parse_mode="html")
            return
        if data == "menu:upload":
            await self.choose_upload(event.chat_id, uid); return
        if data == "menu:archive":
            await self.run_archive(event.chat_id, uid, source=event); return
        if data == "archive:normal":
            await self.begin_archive_normal(event.chat_id, uid); return
        if data == "archive:multi":
            # 7z only discovers a multi-volume set when every part sits in the
            # same directory. Move the first part there instead of copying it:
            # copying a multi-gigabyte archive doubled the disk usage and made
            # the extraction fail whenever the copy was still running.
            if st.path and st.path.exists():
                part_dir = self.cfg.download_dir / str(uid) / "archive_parts"
                part_dir.mkdir(parents=True, exist_ok=True)
                target = unique_path(part_dir, st.path.name)
                if target.resolve() != st.path.resolve():
                    shutil.move(str(st.path), str(target))
                st.archive_parts = [target.resolve()]
                st.path = target
            await self.begin_archive_multi(event.chat_id, uid); return
        if data == "archive:finish":
            if st.archive_mode != "multi" or not st.archive_parts:
                await self.render_ui(event.chat_id, uid, "❌ No multi-part archive is queued.", buttons=main_menu())
                return
            try:
                needs = await asyncio.to_thread(archive_requires_password, st.archive_parts[0])
            except Exception:
                needs = False
            if needs:
                st.pending = "archive_password"
                await self.render_ui(event.chat_id, uid, "🔐 <b>Password-protected archive</b>\n\nSend the password or choose No Password.", buttons=archive_password_menu(), parse_mode="html")
            else:
                await self._begin_archive_extract(event.chat_id, uid, None)
            return
        if data == "archive:nopass":
            await self._begin_archive_extract(event.chat_id, uid, None); return
        if data == "upload:back":
            await self.send_main(event.chat_id, uid, source=event); return
        if data == "rename:yes":
            st.pending = "rename_input"
            await self.safe_edit(event, "✏️ <b>Rename file</b>\n\nSend the new filename, including extension if you want to change it.", buttons=[[Button.inline("Cancel", b"cancel")]], parse_mode="html"); return
        if data == "rename:skip":
            st.pending = None
            dest = st.pending_upload_destination
            st.pending_upload_destination = None
            if dest:
                await self.choose_upload(event.chat_id, uid, dest, skip_rename=True)
            else:
                await self.choose_upload(event.chat_id, uid, None, skip_rename=True)
            return
        if data.startswith("upload:"):
            await self.choose_upload(event.chat_id, uid, data.split(":", 1)[1]); return
        if data == "video:info" or data == "audio:info":
            await self.run_info(event.chat_id, uid, source=event); return
        if data == "video:streams":
            await self.stream_menu(event.chat_id, uid, "remove", source=event); return
        if data == "video:merge":
            await self.start_merge(event.chat_id, uid, source=event); return
        if data == "video:extract":
            await self.stream_menu(event.chat_id, uid, "extract", source=event); return
        if data == "video:trim":
            st.pending = "trim"; await self.safe_edit(event, "✂️ Send `start end` in seconds or HH:MM:SS values.\nExample: `00:05 00:30`", buttons=cancel_menu(), parse_mode="markdown"); return
        if data == "video:remove_audio":
            await self.run_media_job(event.chat_id, uid, "remove_audio"); return
        if data == "video:optimize":
            await self.run_media_job(event.chat_id, uid, "optimize"); return
        if data == "video:split":
            st.pending = "split"; await self.safe_edit(event, "🎥 Send segment length in seconds. Example: `600`", buttons=cancel_menu(), parse_mode="markdown"); return
        if data == "video:shots":
            st.pending = "shots_count"
            await self.safe_edit(event, "🖼️ <b>Screenshots</b>\n\nHow many screenshots do you want? Enter a number from <b>1 to 20</b>.", buttons=cancel_menu(), parse_mode="html"); return
        if data == "video:manual":
            st.pending = "manual_shot"
            await self.safe_edit(event, "🖼️ <b>Manual Shots</b>\n\nSend one or more timestamps separated by commas. Example: <code>00:05:30, 00:12:00, 00:25:10</code>\nMaximum: <b>20</b> screenshots.", buttons=cancel_menu(), parse_mode="html"); return
        if data == "video:sample":
            st.pending = "sample"; await self.safe_edit(event, "🎥 Send sample duration in seconds. Default: 30", buttons=cancel_menu()); return
        if data == "video:toaudio":
            await self.run_media_job(event.chat_id, uid, "toaudio"); return
        if data in ("video:mp4", "video:mkv"):
            await self.run_media_job(event.chat_id, uid, data.split(":")[1]); return
        if data == "merge:add":
            # Kept as a backwards-compatible callback for old messages. The
            # current UI no longer shows an Add More Files button; users simply
            # send another file/URL while the merge session is active.
            st.pending = "merge_collect"
            await self.safe_edit(event, self.merge_status_text(st), buttons=self._merge_buttons(st), parse_mode="html")
            return
        if data == "merge:order":
            await self.show_merge_order(event.chat_id, uid, source=event); return
        if data == "merge:clear":
            st.merge_inputs.clear()
            await self.show_merge_order(event.chat_id, uid, source=event); return
        if data.startswith("merge:up:") or data.startswith("merge:down:"):
            delta = -1 if data.startswith("merge:up:") else 1
            try:
                index = int(data.rsplit(":", 1)[1])
            except ValueError:
                index = -1
            moved = self._move_merge_item(st, index, delta)
            await self.answer(event, "Moved." if moved else "Already at the end.")
            await self.show_merge_order(event.chat_id, uid, source=event)
            return
        if data.startswith("merge:pick:"):
            try:
                index = int(data.rsplit(":", 1)[1])
            except ValueError:
                index = -1
            if not (0 <= index < len(st.merge_inputs)):
                await self.answer(event, "That track is no longer in the list.", alert=True)
                await self.show_merge_order(event.chat_id, uid, source=event)
                return
            st.merge_selected = index
            name = esc(Path(st.merge_inputs[index]).name[:50])
            await self.render_card(
                event.chat_id, uid,
                f"\U0001F501 <b>Track {index + 1}: {name}</b>\n\nWhat would you like to do?",
                buttons=merge_pick_menu(index, len(st.merge_inputs)),
            )
            return
        if data.startswith("merge:shift:"):
            delta = int(data.rsplit(":", 1)[1])
            index = getattr(st, "merge_selected", 0)
            moved = self._move_merge_item(st, index, delta)
            await self.answer(event, "Moved." if moved else "Already at the edge.")
            await self.show_merge_order(event.chat_id, uid, source=event)
            return
        if data == "merge:top" or data == "merge:bottom":
            index = getattr(st, "merge_selected", 0)
            if 0 <= index < len(st.merge_inputs):
                item = st.merge_inputs.pop(index)
                st.merge_inputs.insert(0 if data == "merge:top" else len(st.merge_inputs), item)
            await self.show_merge_order(event.chat_id, uid, source=event)
            return
        if data == "merge:drop":
            index = getattr(st, "merge_selected", 0)
            if 0 <= index < len(st.merge_inputs):
                removed = st.merge_inputs.pop(index)
                Path(removed).unlink(missing_ok=True)
                await self.answer(event, "Removed from the merge.")
            st.merge_selected = 0
            await self.show_merge_order(event.chat_id, uid, source=event)
            return
        if data == "merge:finish":
            await self.finish_merge(event.chat_id, uid); return
        if data == "merge:cancel":
            await self.cancel(uid, event.chat_id, event); return
        if data == "merge:back":
            st.pending = None; st.merge_inputs.clear(); await self.safe_edit(event, "Please select your preferred action below 👇", buttons=video_menu()); return
        if data.startswith("streamtoggle:"):
            idx = int(data.split(":", 1)[1])
            if idx in st.custom_remove: st.custom_remove.remove(idx)
            else: st.custom_remove.add(idx)
            # Re-render the same screen so the selection stays visible instead
            # of collapsing into a single line of text.
            await self.stream_custom_menu(event.chat_id, uid, st.view_mode or "remove", source=event)
            return
        if data == "streamapply":
            if not st.custom_remove:
                await self.stream_custom_menu(event.chat_id, uid, st.view_mode or "remove", source=event)
                await self.answer(event, "Select at least one track first.", alert=True)
                return
            # Resolve the selection *before* the transfer: the track list belongs
            # to the input the user chose from, not to whatever the background
            # probe happens to have produced by the time it finishes.
            chosen = set(st.custom_remove)
            keep = [s.get("index") for s in st.streams if s.get("index") not in chosen]
            if not keep:
                await self.answer(event, "That selection would leave no streams.", alert=True)
                return
            # Applying rewrites the file, so this is where the media is fetched.
            await self.await_input(event.chat_id, uid)
            target = self.source_media(st)
            if not target:
                await self.answer(event, "That file is not available any more.", alert=True)
                return
            if not keep:
                await self.answer(event, "You cannot remove every stream.", alert=True)
                return
            out = unique_path(self.cfg.work_dir / str(uid), f"{target.stem}.custom.mkv")
            await self.execute(event.chat_id, uid, "🧹 Removing custom streams", lambda: ffmpeg.remux(target, out, keep), upload=True, set_source=True); return
        if data.startswith("aconv:"):
            await self.callback_aconv(event, uid, data.split(":", 1)[1]); return
        if data.startswith("stream:"):
            await self.handle_stream_callback(event, uid, data); return
        if data == "audio:convert":
            await self.audio_convert_menu(event); return
        audio_filters = {
            "audio:8d": ("apulsator=hz=0.125:width=1:type=sine", "8D"),
            "audio:eq": ("equalizer=f=1000:t=q:w=1:g=2", "equalizer"),
            "audio:bass": ("bass=g=8", "bass"),
            "audio:treble": ("treble=g=6", "treble"),
            "audio:auto": ("silenceremove=start_periods=1:start_duration=0.5:start_threshold=-50dB:stop_periods=-1:stop_duration=0.5:stop_threshold=-50dB", "auto trim"),
            "audio:compress": ("acompressor=threshold=-18dB:ratio=3:attack=20:release=250", "compress"),
        }
        if data in audio_filters:
            filt, name = audio_filters[data]
            await self.run_audio_filter(event.chat_id, uid, filt, name); return
        if data == "audio:trim":
            st.pending = "audio_trim"; await self.safe_edit(event, "✂️ Send `start end`. Example: `00:00 00:30`", buttons=cancel_menu(), parse_mode="markdown"); return
        if data == "audio:speed":
            st.pending = "audio_speed"; await self.safe_edit(event, "🎵 Send speed factor between 0.5 and 2.0. Example: `1.25`", buttons=cancel_menu()); return
        if data == "audio:volume":
            st.pending = "audio_volume"; await self.safe_edit(event, "🔊 Send volume expression. Example: `1.5` or `-3dB`", buttons=cancel_menu()); return
        if data.startswith("bulk:"):
            await self.handle_bulk_callback(event, uid, data); return
        if data.startswith("bulkupload:"):
            await self.handle_bulk_upload(event, uid, data.split(":", 1)[1]); return
        if data == "upload:back":
            await self.send_main(event.chat_id, uid, source=event); return
        if data.startswith("settings:bulk:"):
            self.db.set_bulk_mode(uid, data.rsplit(":", 1)[1] == "on")
            await self.send_settings(event.chat_id, uid, source=event); return
        if data.startswith("settings:auto_delete:"):
            self.db.set_auto_delete(uid, data.rsplit(":", 1)[1] == "on")
            await self.send_settings(event.chat_id, uid, source=event); return
        if data.startswith("settings:keep_files:"):
            self.db.set_keep_files(uid, data.rsplit(":", 1)[1] == "on")
            await self.send_settings(event.chat_id, uid, source=event); return
        # An unrecognised button is almost always a stale keyboard from before a
        # restart or a completed job. Saying nothing leaves the user tapping a
        # dead button, so point them back to a working screen.
        await self.answer(event, "This button is out of date. Sending a fresh menu…", alert=True)
        await self.send_start_info(event.chat_id, uid, source=event)

    # ------------------------------------------------------------------
    # Bulk mode callbacks
    # ------------------------------------------------------------------
    async def handle_bulk_callback(self, event, uid: int, data: str) -> None:
        st = self.state(uid)
        action = data.split(":", 1)[1]
        if action == "done":
            # Leave bulk collection: the files gathered so far become the
            # working set, so Upload sends all of them and the action menu
            # operates on the most recent one. This is where the collection
            # actually starts transferring - nothing was fetched on arrival.
            await self._await_queue_drained(event.chat_id, uid)
            ready = st.queue.ready_paths()
            if ready:
                st.outputs = ready
                st.path = ready[-1]
                st.source_path = ready[-1]
                st.root_path = ready[-1]
            st.queue.drop_finished()
            st.view = "menu"
            await self.stop_timeout(uid)
            await self.show_file_menu(event.chat_id, uid, st.path, plain=True, fresh=False)
            if len(ready) > 1:
                await self.answer(event, f"{len(ready)} files ready — Upload sends all of them")
            return
        if action == "refresh":
            st.view = "queue"
            await self._render_queue_panel(uid, event.chat_id, source=event, force=True)
            return
        if action == "clear":
            self.cancel_downloads(uid)
            await self.answer(event, "Queue cleared", alert=True)
            st.view = "menu"
            await self.show_file_menu(event.chat_id, uid, None, source=event)
            return
        if action == "upload":
            await self._await_queue_drained(event.chat_id, uid)
            ready = st.queue.ready_paths()
            if not ready:
                await self.answer(event, "No finished files to upload yet", alert=True)
                return
            st.view = "queue"
            await self.render_card(
                event.chat_id, uid,
                f"📤 <b>Upload all</b>\n\n<b>Files ready:</b> {len(ready)}\n\nChoose a destination.",
                buttons=bulk_upload_menu(len(ready)),
            )

    async def handle_bulk_upload(self, event, uid: int, destination: str) -> None:
        st = self.state(uid)
        ready = st.queue.ready_paths()
        if not ready:
            await self.answer(event, "No finished files to upload yet", alert=True)
            return
        if destination not in {"telegram", "gofile"}:
            await self.answer(event, "Unknown upload destination", alert=True)
            return
        # The queue becomes the upload set, so the shared upload path (rename
        # prompt, chunking, GoFile folders) works unchanged.
        st.outputs = ready
        st.path = ready[-1]
        st.output_root = None
        st.view = "menu"
        await self.choose_upload(event.chat_id, uid, destination)

    async def audio_convert_menu(self, event):
        await self.safe_edit(event, "Choose output format", buttons=[
            [Button.inline("MP3", b"aconv:mp3"), Button.inline("AAC", b"aconv:aac")],
            [Button.inline("OPUS", b"aconv:opus"), Button.inline("FLAC", b"aconv:flac")],
            [Button.inline("⬅️ Back", b"audio:back"), Button.inline("Cancel", b"cancel")],
        ])

    async def callback_aconv(self, event, uid, fmt):
        codecs = {"mp3": ("libmp3lame", ".mp3"), "aac": ("aac", ".m4a"), "opus": ("libopus", ".opus"), "flac": ("flac", ".flac")}
        if fmt not in codecs:
            return
        codec, ext = codecs[fmt]
        await self.run_audio_convert(event.chat_id, uid, codec, ext)

    async def stream_menu(self, chat_id: int, uid: int, mode: str, source=None):
        st = self.state(uid)
        # The track list comes from the container header, so the file itself is
        # not needed to show this menu - only to run the chosen operation.
        await self._streams_for_menu(chat_id, uid)
        if not st.streams:
            # The helper already explained why there is nothing to show.
            return
        action = "extract" if mode == "extract" else "remove"
        rows = [[Button.inline(ffprobe.stream_label(s)[:60], f"stream:{mode}:{s.get('index')}".encode())] for s in st.streams]
        rows += [
            [Button.inline("🔇 Remove all audio", f"stream:{mode}:drop_audio"), Button.inline("\U0001F50A Keep default audio", f"stream:{mode}:keep_audio")],
            [Button.inline("All Audios", f"stream:{mode}:all_audio"), Button.inline("All Subtitles", f"stream:{mode}:all_sub")],
            [Button.inline("Custom Streams", f"stream:{mode}:custom"), Button.inline("All Streams", f"stream:{mode}:all")],
            [Button.inline("⬅️ Back", b"video:back"), Button.inline("Cancel Process", b"cancel")],
        ]
        counts = {
            "video": sum(1 for s in st.streams if s.get("codec_type") == "video"),
            "audio": sum(1 for s in st.streams if s.get("codec_type") == "audio"),
            "subtitle": sum(1 for s in st.streams if s.get("codec_type") == "subtitle"),
        }
        summary = f"\U0001F3A0 <b>{counts['video']} video • {counts['audio']} audio • {counts['subtitle']} subtitle</b>"
        header = (
            f"{summary}\n\nTap a track to <b>{action}</b> it, or use a quick action below."
        )
        await self.render_ui(chat_id, uid, header, buttons=rows, source=source)

    async def handle_stream_callback(self, event, uid: int, data: str):
        _, mode, value = data.split(":", 2)
        st = self.state(uid)
        # The track list comes from the container header, so the selection
        # screens work without the media. Downloading here is what made
        # "Custom Streams" look like it jumped back to the main menu: the tap
        # started a transfer and the progress panel replaced the submenu.
        streams = await self._resolve_streams(uid)
        if not streams:
            await self.answer(event, "This file has no audio or video tracks.", alert=True)
            return
        if value == "custom":
            st.custom_remove.clear()
            await self.stream_custom_menu(event.chat_id, uid, mode)
            return
        # Everything else rewrites the file, so this is where the transfer
        # starts - with progress, and without losing the open submenu.
        await self.await_input(event.chat_id, uid)
        target = self.source_media(st)
        if not target:
            await self.answer(event, "That file is not available any more.", alert=True)
            return
        if value.isdigit():
            idx = int(value)
            s = next((x for x in streams if x.get("index") == idx), None)
            if not s: return
            if mode == "extract":
                ext = ffmpeg._stream_output_extension(s)
                out = unique_path(self.cfg.work_dir / str(uid), f"{target.stem}.stream{idx}{ext}")
                await self.execute(event.chat_id, uid, "🎵 Extracting stream", lambda: ffmpeg.extract_stream(target, out, idx), upload=True, set_source=False)
            else:
                keep = [x.get("index") for x in streams if x.get("index") != idx]
                out = unique_path(self.cfg.work_dir / str(uid), f"{target.stem}.streams_removed.mkv")
                await self.execute(event.chat_id, uid, "🧹 Removing stream", lambda: ffmpeg.remux(target, out, keep), upload=True, set_source=True)
            return
        if value in {"drop_audio", "keep_audio"}:
            audio = [s for s in streams if s.get("codec_type") == "audio"]
            if not audio:
                await self.answer(event, "This file has no audio tracks.", alert=True)
                return
            if value == "drop_audio":
                if mode != "remove":
                    await self.answer(event, "Extraction works on one track at a time.", alert=True)
                    return
                keep = [s.get("index") for s in streams if s.get("codec_type") != "audio"]
                if not keep:
                    await self.answer(event, "This file is audio-only.", alert=True)
                    return
                await self.stream_remux(event.chat_id, uid, keep)
            else:
                default_audio = [s for s in audio if s.get("disposition", {}).get("default")]
                chosen = (default_audio or audio)[:1]
                keep = [s.get("index") for s in streams if s.get("codec_type") != "audio"]
                keep += [s.get("index") for s in chosen]
                await self.stream_remux(event.chat_id, uid, keep)
            return
        if value == "all_audio":
            audio = [s for s in streams if s.get("codec_type") == "audio"]
            default_audio = [s for s in audio if s.get("disposition", {}).get("default")]
            keep_audio = default_audio[:1] or audio[:1]
            keep = [s.get("index") for s in streams if s.get("codec_type") != "audio"] + [s.get("index") for s in keep_audio]
            await self.stream_remux(event.chat_id, uid, keep); return
        if value == "all_sub":
            subs = [s for s in streams if s.get("codec_type") == "subtitle"]
            default_sub = [s for s in subs if s.get("disposition", {}).get("default")]
            keep_sub = default_sub[:1] or subs[:1]
            keep = [s.get("index") for s in streams if s.get("codec_type") != "subtitle"] + [s.get("index") for s in keep_sub]
            await self.stream_remux(event.chat_id, uid, keep); return
        if value == "all":
            # Preserve the default stream(s) and the first video stream so the
            # resulting media remains usable even if the default is audio.
            keep = [s.get("index") for s in streams if s.get("disposition", {}).get("default")]
            first_video = next((s.get("index") for s in streams if s.get("codec_type") == "video"), None)
            if first_video is not None and first_video not in keep: keep.append(first_video)
            if not keep: keep = [streams[0].get("index")] if streams else []
            await self.stream_remux(event.chat_id, uid, keep)

    @staticmethod
    def _is_nondefault(s: dict) -> bool:
        return not bool(s.get("disposition", {}).get("default"))

    async def _resolve_streams(self, uid: int) -> list[dict]:
        """The current input's track list, from the header or a known file.

        Never downloads: if nothing is known yet it returns an empty list and
        the caller decides whether fetching the file is worth it.
        """
        st = self.state(uid)
        if st.streams:
            return st.streams
        if st.probe_data is None and not st.busy:
            await self._wait_for_metadata(uid)
        st = self.state(uid)
        if st.probe_data:
            streams = st.probe_data.get("streams") or []
            if streams:
                st.streams = streams
                return streams
        target = self.source_media(st)
        if not target or not target.exists():
            return []
        data = await asyncio.to_thread(ffprobe.safe_probe, target)
        if data:
            st.probe_data = data
        st.streams = (data or {}).get("streams") or []
        return st.streams

    async def _streams_for_menu(self, chat_id: int, uid: int) -> list[dict]:
        """Return the current input's track list, downloading only if forced to.

        Metadata normally comes from the header read that happened when the
        input arrived, so opening Stream Remover/Extractor never transfers the
        media. The full file is only fetched when no header could be read, and
        even then the transfer runs with the usual progress.
        """
        st = self.state(uid)
        streams = await self._resolve_streams(uid)
        if streams:
            return streams
        # No header could be read: fetch the file, then probe it locally.
        target = await self.await_input(chat_id, uid) or self.source_media(st)
        if not target or not target.exists():
            await self.render_card(chat_id, uid, "Send a media file first.", buttons=main_menu())
            return []
        data = await asyncio.to_thread(ffprobe.safe_probe, target)
        if data:
            st.probe_data = data
        streams = (data or {}).get("streams") or []
        st.streams = streams
        if not streams:
            await self.render_card(
                chat_id, uid,
                "\U0001F50E No audio or video tracks were found in this file.",
                buttons=main_menu(),
            )
        return streams

    async def stream_custom_menu(self, chat_id: int, uid: int, mode: str, source=None):
        """The "tap the tracks to remove" screen.

        It is a full screen with its own state, not a one-line edit: tapping a
        track used to replace the whole submenu with a bare sentence, and the
        selection was invisible on the way back.
        """
        st = self.state(uid)
        streams = st.streams
        mode = mode if mode in {"remove", "extract"} else "remove"
        st.view_mode = mode
        action = "extract" if mode == "extract" else "remove"
        rows = []
        for s in streams:
            idx = s.get("index")
            mark = "❌ " if idx in st.custom_remove else "✅ "
            rows.append([Button.inline((mark + ffprobe.stream_label(s))[:58], f"streamtoggle:{idx}".encode())])
        rows.append([
            Button.inline("✅ Apply", b"streamapply"),
            Button.inline("Cancel", b"cancel"),
            Button.inline("⬅️ Back", b"video:back"),
        ])
        chosen = len(st.custom_remove)
        header = (
            f"\U0001F9F9 <b>Custom Streams</b>\n"
            f"Tap every track you want to <b>{action}</b>, then press <b>Apply</b>.\n"
            f"Selected: <b>{chosen}</b> of {len(streams)}  •  the file is downloaded only when you apply"
        )
        return await self.render_ui(chat_id, uid, header, buttons=rows, source=source)

    async def stream_remux(self, chat_id, uid, keep):
        st = self.state(uid)
        if not keep:
            return
        await self.await_input(chat_id, uid)
        target = self.source_media(st)
        if not target: return
        out = unique_path(self.cfg.work_dir / str(uid), f"{target.stem}.remux.mkv")
        await self.execute(chat_id, uid, "🧹 Removing streams", lambda: ffmpeg.remux(target, out, keep), upload=True, set_source=True)

    def source_media(self, st: UserState) -> Path | None:
        for candidate in (st.source_path, st.root_path, st.path):
            if candidate and candidate.exists():
                return candidate
        return None

    def _has_video(self, path: Path) -> bool:
        return any(s.get("codec_type") == "video" for s in ffprobe.safe_probe(path).get("streams", []))

    async def video_media(self, st: UserState) -> Path | None:
        """Pick a video track without blocking the event loop.

        ffprobe used to run inline here. On a large MKV that froze the whole
        bot for every user, and any probe error collapsed into a None that made
        the bot claim "send a video first" for a perfectly good file.
        """
        for candidate in (st.path, st.source_path, st.root_path):
            if candidate and candidate.exists():
                try:
                    if await asyncio.to_thread(self._has_video, candidate):
                        return candidate
                except Exception:
                    log.debug("video probe failed for %s", candidate, exc_info=True)
        return None

    async def await_input(self, chat_id: int, uid: int, timeout: float = DOWNLOAD_WAIT):
        """Return the current input, starting the transfer when it is missing.

        The bot no longer downloads a file just because it arrived, so this is
        where an action gets the media it needs: the queued transfer starts
        here, real progress is rendered while it runs, and the finished file is
        returned. That way the first tap works instead of reporting "send a
        media file first", and nothing is downloaded for an action the user
        never chose.
        """
        st = self.state(uid)
        if st.path and st.path.exists():
            return st.path
        if not st.queue.has_work():
            return None
        self._start_worker(uid)
        # Yield once so the worker has claimed an item before the first repaint;
        # otherwise a fast transfer finishes without the user ever seeing it.
        await asyncio.sleep(0)
        deadline = time.monotonic() + timeout
        interval = self._panel_interval()
        while True:
            st = self.state(uid)
            if st.path and st.path.exists():
                return st.path
            if st.cancel_event.is_set():
                return None
            if not st.queue.has_work():
                # Every item settled. The file is normally promoted before the
                # queue reports itself empty, so a missing path means a failure.
                await self._render_queue_failure(chat_id, uid)
                return None
            with contextlib.suppress(Exception):
                await self._render_queue_panel(uid, chat_id, force=True)
            if time.monotonic() >= deadline:
                log.warning("user %s: gave up waiting for the download", uid)
                with contextlib.suppress(Exception):
                    await self.render_card(
                        chat_id, uid,
                        "⏱️ <b>The download is taking too long.</b>\nIt is still running in the "
                        "background — press Cancel to stop it, or try again in a moment.",
                        buttons=cancel_menu(), parse_mode="html",
                    )
                return None
            await asyncio.sleep(interval)

    async def _render_queue_failure(self, chat_id: int, uid: int) -> None:
        """Explain a failed transfer instead of claiming no file was sent."""
        st = self.state(uid)
        failed = st.queue.failed()
        if not failed:
            return
        last = failed[-1]
        with contextlib.suppress(Exception):
            await self.render_card(
                chat_id, uid,
                f"❌ <b>Download failed</b>\n\n📁 {esc(last.display_name[:60])}\n"
                f"{esc(last.error or 'unknown error')}",
                buttons=main_menu(), parse_mode="html",
            )

    async def _await_queue_drained(self, chat_id: int, uid: int, timeout: float = DOWNLOAD_WAIT) -> None:
        """Transfer every queued input, with progress, before a bulk action.

        "Done Adding" and "Upload All" need the actual files, so they are the
        point where a bulk collection finally starts moving.
        """
        st = self.state(uid)
        if not st.queue.has_work():
            return
        self._start_worker(uid)
        await asyncio.sleep(0)
        deadline = time.monotonic() + timeout
        interval = self._panel_interval()
        while time.monotonic() < deadline:
            st = self.state(uid)
            if not st.queue.has_work() or st.cancel_event.is_set():
                break
            with contextlib.suppress(Exception):
                await self._render_queue_panel(uid, chat_id, force=True)
            await asyncio.sleep(interval)
        st = self.state(uid)
        if st.queue.failed():
            await self._render_queue_failure(chat_id, uid)

    async def download_merge_input(self, event):
        uid = event.sender_id
        st = self.state(uid)
        message = event.message
        file = getattr(message, "file", None)
        if file is None:
            await event.reply("That message does not contain a file.")
            return
        name = safe_filename(getattr(file, "name", None) or f"merge_{getattr(message, 'id', 0)}.bin")
        out = unique_path(self.cfg.download_dir / str(uid) / "merge", name)
        status = await self.new_status_message(event.chat_id, f"Adding merge track\u2026\n{esc(name)}", uid=uid, buttons=cancel_menu(), parse_mode="html")
        reporter = await self.progress_message(status, uid=uid)
        self.begin_job(uid, "Downloading merge track")
        try:
            async def cb(cur, total):
                if st.cancel_event.is_set():
                    raise asyncio.CancelledError
                await reporter.update(int(cur), int(total or getattr(file, "size", 0) or 0), "Downloading merge track")
            async with self.semaphore:
                result = await self.client.download_media(message, file=str(out), progress_callback=cb)
            if not result or not out.exists() or out.stat().st_size == 0:
                raise RuntimeError("Telegram returned no downloaded merge track")
            st.merge_inputs.append(out.resolve())
            st.probe_tasks.add(asyncio.create_task(self._probe_merge_duration(uid, out.resolve())))
            st.probe_tasks = {t for t in st.probe_tasks if not t.done()}
            await self.safe_edit(status, f"Added: {esc(name)}\nTracks queued: {len(st.merge_inputs)}", buttons=None, parse_mode="html")
            st.status_message_id = None; st.status_chat_id = None
            await self.render_ui(event.chat_id, uid, self.merge_status_text(st), buttons=self._merge_buttons(st), parse_mode="html")
            self.touch(uid)
        except asyncio.CancelledError:
            out.unlink(missing_ok=True)
            await self.cancelled_status(uid, status)
        except Exception as exc:
            out.unlink(missing_ok=True)
            await self.safe_edit(status, f"Failed to add merge track: {esc(error_text(exc))}", buttons=None, parse_mode="html")
        finally:
            self.end_job(uid)

    @staticmethod
    def _describe_merge_inputs(inputs, durations=None) -> str:
        """Render the merge queue as an explicit, numbered order."""
        if not inputs:
            return "<i>No files queued yet.</i>"
        durations = durations or {}
        lines = []
        for idx, raw in enumerate(inputs, 1):
            path = Path(raw)
            try:
                size = format_bytes(path.stat().st_size)
            except OSError:
                size = "missing"
            line = f"{idx}. <b>{esc(path.name[:60])}</b> <i>({size})</i>"
            dur = durations.get(str(path))
            if dur:
                from .utils.files import format_duration
                line += f" \u2014 {format_duration(dur)}"
            lines.append(line)
        return "\n".join(lines)

    def _merge_estimate(self, st: UserState) -> list[str]:
        """Size and duration estimates shown after every added track.

        Stream muxing copies packets without re-encoding, so the output is
        very close to the sum of the inputs. Knowing that up front stops a
        user queueing eight tracks and only discovering afterwards that the
        result is larger than the disk they have left.
        """
        rows: list[str] = []
        total = 0
        missing = 0
        for raw in st.merge_inputs:
            try:
                total += Path(raw).stat().st_size
            except OSError:
                missing += 1
        if st.merge_inputs:
            rows.append(f"\U0001F4E6 <b>Total input:</b> {format_bytes(total)}")
            # Muxing re-muxes rather than re-encodes; allow a few percent for
            # container overhead so the estimate is not misleadingly exact.
            estimate = int(total * 1.03)
            rows.append(f"\U0001F5C2\uFE0F <b>Estimated output:</b> ~{format_bytes(estimate)}")
            if missing:
                rows.append(f"⚠️ <i>{missing} file(s) are missing and will be skipped.</i>")
        durations = [d for d in st.merge_durations.values() if d]
        if durations:
            from .utils.files import format_duration
            longest = max(durations)
            total_dur = sum(durations)
            rows.append(
                f"⏱ <b>Longest track:</b> {format_duration(longest)}"
                f"  <i>(all tracks: {format_duration(total_dur)})</i>"
            )
        return rows

    async def _probe_merge_duration(self, uid: int, path: Path) -> None:
        """Cache a merge input's duration in the background."""
        key = str(path)
        if key in self.state(uid).merge_durations:
            return
        try:
            data = await asyncio.to_thread(ffprobe.safe_probe, path)
        except Exception:
            return
        raw = (data.get("format") or {}).get("duration")
        try:
            value = float(raw) if raw else None
        except (TypeError, ValueError):
            value = None
        if value:
            self.state(uid).merge_durations[key] = value

    def merge_status_text(self, st: UserState) -> str:
        """The merge screen: the exact order the tracks will be joined in.

        This method was called from five call sites but had no definition, so
        every merge action raised AttributeError and the feature never worked.
        """
        count = len(st.merge_inputs)
        lines = [
            "\U0001F500 <b>Merge Tracks</b>",
            "",
            f"<b>Files queued:</b> {count}",
            "",
            self._describe_merge_inputs(st.merge_inputs, st.merge_durations),
        ]
        estimate = self._merge_estimate(st)
        if estimate:
            lines += ["", *estimate]
        lines.append("")
        if count < 2:
            lines.append("➕ <b>Send another media file or URL</b> to add a track.")
        else:
            lines.append("Use <b>Merge order</b> to reorder or remove tracks before merging.")
        lines.append("Press <b>Finish Merge</b> when the order is right.")
        return "\n".join(lines)

    def _merge_buttons(self, st: UserState):
        """Order controls once there is something to order, plain menu before."""
        count = len(st.merge_inputs)
        return merge_order_menu(count) if count >= 2 else merge_menu(count)

    async def show_merge_order(self, chat_id, uid, source=None) -> None:
        """Show the numbered merge order with per-track reorder controls."""
        st = self.state(uid)
        count = len(st.merge_inputs)
        text = (
            "\U0001F501 <b>Merge Order</b>\n\n"
            "Tracks are joined top to bottom. Use the arrows to fix the order.\n\n"
            f"{self._describe_merge_inputs(st.merge_inputs, st.merge_durations)}"
        )
        estimate = self._merge_estimate(st)
        if estimate:
            text += "\n\n" + "\n".join(estimate)
        if count < 2:
            text += "\n\n<i>Add at least two tracks to merge.</i>"
            buttons = merge_menu(count)
        else:
            buttons = merge_order_menu(count)
        await self.render_card(chat_id, uid, text, buttons=buttons, parse_mode="html")

    @staticmethod
    def _move_merge_item(st: UserState, index: int, delta: int) -> bool:
        """Move the track at ``index`` by ``delta`` positions. True if moved."""
        items = st.merge_inputs
        target = index + delta
        if not (0 <= index < len(items)) or not (0 <= target < len(items)):
            return False
        items[index], items[target] = items[target], items[index]
        return True

    async def start_merge(self, chat_id, uid, source=None):
        st = self.state(uid)
        if st.busy:
            await self.render_card(chat_id, uid, "⚠️ A process is already running for you. Press Cancel first.", buttons=cancel_menu())
            return
        await self.await_input(chat_id, uid)
        base = self.source_media(st)
        if not base or not base.exists():
            await self.render_card(chat_id, uid, "Send or download a media file first.", buttons=main_menu())
            return
        # Snapshot the first input immediately. Additional media messages are
        # appended by receive_media()/download_merge_input().
        st.pending = "merge_collect"
        st.merge_inputs = [base.resolve()]
        st.merge_durations.clear()
        st.probe_tasks.add(asyncio.create_task(self._probe_merge_duration(uid, base.resolve())))
        st.probe_tasks = {t for t in st.probe_tasks if not t.done()}
        await self.render_ui(chat_id, uid, self.merge_status_text(st), buttons=self._merge_buttons(st), parse_mode="html", source=source)
        self.touch(uid)

    async def finish_merge(self, chat_id, uid):
        st = self.state(uid)
        # Validate a snapshot of the queue. Never append to the list being
        # iterated: the previous implementation duplicated every validated
        # input, which doubled the final MKV size (e.g. 2.51 GiB became ~5.03 GiB).
        candidates = list(st.merge_inputs)
        inputs: list[Path] = []
        seen: set[str] = set()
        for raw in candidates:
            p = Path(raw)
            if not p.exists() or not p.is_file() or p.stat().st_size <= 0:
                continue
            resolved = str(p.resolve())
            if resolved in seen:
                continue
            try:
                if (await asyncio.to_thread(ffprobe.safe_probe, p)).get("streams"):
                    inputs.append(p.resolve())
                    seen.add(resolved)
            except Exception:
                continue
        st.merge_inputs = inputs
        # Keep the word "Queued:" in logs/messages for compatibility with older
        # clients that recognize the merge queue label. The UI now uses the
        # clearer "Files queued:" count and no longer exposes Add More Files.
        if len(inputs) < 2:
            st.pending = "merge_collect"
            await self.render_ui(
                chat_id, uid,
                f"❌ Merge needs at least 2 valid files.\n\n<b>Files queued: {len(inputs)}</b>\n\n"
                "Send another media file/URL now, then press <b>Finish Merge</b>.",
                buttons=self._merge_buttons(st), parse_mode="html"
            )
            self.touch(uid)
            return
        # Freeze the queue before starting FFmpeg so later messages cannot
        # mutate the input list used by the worker.
        snapshot = tuple(inputs)
        # The merged output is written next to the inputs, so refuse early if
        # it obviously will not fit. Muxing needs roughly the sum of inputs.
        needed = sum(self._safe_size(p) for p in snapshot)
        try:
            free = shutil.disk_usage(self.cfg.work_dir).free
        except OSError:
            free = None
        if free is not None and needed + (256 * 1024 * 1024) > free:
            await self.render_ui(
                chat_id, uid,
                f"❌ <b>Not enough disk space to merge.</b>\n\n"
                f"Estimated output: ~{format_bytes(int(needed * 1.03))}\n"
                f"Free space: {format_bytes(free)}\n\n"
                "Remove a track from the merge order and try again.",
                buttons=self._merge_buttons(st), parse_mode="html",
            )
            self.touch(uid)
            return
        st.pending = None
        out = unique_path(self.cfg.work_dir / str(uid), f"{safe_filename(snapshot[0].stem)}.merged.mkv")
        await self.execute(chat_id, uid, "🔀 Merging tracks", lambda: merge_tracks(list(snapshot), out), upload=True)
        # execute() leaves st.path pointing at the merged output on success.
        if st.path == out and out.exists():
            st.merge_inputs.clear()

    async def run_direct(self, chat_id, uid, source=None):
        st = self.state(uid)
        await self.await_input(chat_id, uid)
        path = st.path
        if not path or not path.exists():
            await self.render_card(chat_id, uid, "📦 Send or download a media file first.", buttons=main_menu())
            return
        if not self.cfg.public_base_url:
            await self.client.send_message(
                chat_id,
                "I cannot safely invent a public URL for this server.\n\n"
                "Set <code>PUBLIC_BASE_URL</code> to your public HTTPS origin. "
                "Railway/Render domains are detected automatically when their "
                "standard environment variable is available.\n\n"
                "The service must expose <code>WEB_PORT</code> (default 8080) publicly.",
                parse_mode="html",
            )
            return
        self.db.purge_direct_links(int(time.time()))
        token = secrets.token_urlsafe(18)
        expires = int(time.time()) + self.cfg.direct_link_ttl
        self.db.add_direct_link(token, uid, str(path.resolve()), expires)
        link = f"{self.cfg.public_base_url}/f/{token}/{quote(path.name)}"
        hours = max(1, self.cfg.direct_link_ttl // 3600)
        await self.render_ui(
            chat_id, uid,
            f"\U0001F517 <b>Direct/Stream Link</b>\n\n<a href=\"{link}\">{esc(link)}</a>\n\n"
            f"Expires in {hours} hours. The link supports browser playback, download "
            "and HTTP Range requests.",
            buttons=[[Button.inline("\u2B05\uFE0F Back", b"menu:back")]],
            parse_mode="html", link_preview=True, source=source,
        )
        # Expiry is handled by _direct_cleanup_loop, which also survives a bot
        # restart. Spawning one sleeping task per link used to leak thousands of
        # tasks for an active deployment.

    async def run_info(self, chat_id, uid, source=None):
        st = self.state(uid)
        path = self.source_media(st)
        if not path or not path.exists():
            # Media Information is built from the container header, so a link -
            # or a Telegram file whose header was already read - is described
            # without transferring the media at all. Only when no header could
            # be read does this fall back to downloading the file.
            if st.probe_data is None:
                await self._wait_for_metadata(uid)
            st = self.state(uid)
            path = self.source_media(st)
            if not path or not path.exists():
                if not st.probe_data:
                    path = await self.await_input(chat_id, uid) or path
                    if path and not path.exists():
                        path = None
        if not path and not st.probe_data:
            await self.render_ui(chat_id, uid, "Send a media file first.", buttons=main_menu())
            return
        status = await self.render_ui(chat_id, uid, "Collecting detailed media information\u2026", buttons=cancel_menu(), source=source)
        self.begin_job(uid, "Reading media information")
        try:
            if path:
                data = await asyncio.to_thread(ffprobe.probe, path)
                packet_sizes = await asyncio.to_thread(ffprobe.stream_packet_sizes, path)
                name = path.name
                size_text = format_bytes(path.stat().st_size)
            else:
                # Header-only metadata: exact packet payload sizes are only
                # available by scanning the media itself, which is exactly what
                # this path avoids, so that one figure is left out.
                data = st.probe_data or {}
                packet_sizes = {}
                item = st.current_item
                name = item.display_name if item is not None else "media"
                try:
                    size = int((data.get("format") or {}).get("size") or 0)
                except (TypeError, ValueError):
                    size = 0
                if not size and item is not None:
                    size = int(getattr(item, "expected_size", 0) or 0)
                size_text = format_bytes(size) if size else "unknown (not downloaded)"
            page = await create_info_page(
                data, name, size_text,
                self.cfg.telegraph_access_token, packet_sizes=packet_sizes,
            )
            text = f"\U0001F4CB <b>{esc(name)}</b>\n\n\U0001F517 <a href=\"{page}\">Open detailed Media Information</a>"
            await self.safe_edit(status, text, buttons=[[Button.inline("\u2B05\uFE0F Back", b"video:back")]], parse_mode="html", link_preview=False)
            self.state(uid).status_message_id = None
            self.state(uid).status_chat_id = None
        except Exception as exc:
            log.exception("media information failed")
            await self.safe_edit(
                status, f"Media information failed:\n{esc(error_text(exc))}",
                buttons=[[Button.inline("\u2B05\uFE0F Back", b"video:back")]], parse_mode="html",
            )
        finally:
            self.end_job(uid)

    async def run_thumbnail(self, chat_id, uid):
        st = self.state(uid)
        await self.await_input(chat_id, uid)
        path = await self.video_media(st)
        if not path:
            await self.render_card(chat_id, uid, "\U0001F4FC Send a video file first.", buttons=main_menu())
            return
        out = unique_path(self.cfg.work_dir / str(uid), f"{path.stem}.jpg")
        self.begin_job(uid, "Extracting thumbnail")
        status = await self.new_status_message(chat_id, "Extracting thumbnail\u2026", uid=uid, buttons=cancel_menu())
        try:
            async with self.ffmpeg_semaphore:
                job_token = process_control.set_job(uid)
                try:
                    await asyncio.to_thread(ffmpeg.manual_shot, path, out, "00:00:01")
                finally:
                    process_control.reset_job(job_token)
            await self.client.send_file(chat_id, str(out), caption="Thumbnail")
            st.status_message_id = None
            st.status_chat_id = None
        except Exception as exc:
            out.unlink(missing_ok=True)
            await self.safe_edit(status, f"Thumbnail failed:\n{esc(error_text(exc))}", parse_mode="html")
        finally:
            self.end_job(uid)

    async def admin_cancel_user(self, chat_id: int, sudo_uid: int, identifier: str):
        if not self.is_sudo(sudo_uid):
            return
        target = self.db.find_user(identifier)
        if target is None:
            await self.render_ui(chat_id, sudo_uid, f"User <code>{esc(identifier)}</code> is not registered.", buttons=admin_menu(), parse_mode="html")
            return
        if target == sudo_uid:
            await self.render_ui(chat_id, sudo_uid, "You cannot cancel your own admin session from this control.", buttons=admin_menu(), parse_mode="html")
            return
        st = self.state(target)
        if not st.busy and not st.pending and not st.merge_inputs and not st.queue.has_work():
            await self.render_ui(chat_id, sudo_uid, f"User <code>{target}</code> has no active process.", buttons=admin_menu(), parse_mode="html")
            return
        await self.cancel(target, target, admin=True)
        await self.render_ui(chat_id, sudo_uid, f"Cancelled job for <code>{target}</code>.", buttons=admin_menu(), parse_mode="html")
        try:
            await self.client.send_message(target, "🛑 <b>Your process was cancelled by an administrator.</b>", parse_mode="html")
        except Exception:
            pass

    async def admin_broadcast(self, chat_id: int, sudo_uid: int, text: str):
        """Send a message to every registered user, politely.

        Telegram punishes rapid-fire sends with a flood-wait, so the loop is
        throttled, honours a server-requested wait and reports progress instead
        of running silently to completion.
        """
        if not self.is_sudo(sudo_uid):
            return
        users = self.db.all_users()
        if not users:
            await self.render_ui(chat_id, sudo_uid, "No users are registered yet.", buttons=admin_menu(), parse_mode="html")
            return
        status = await self.render_ui(
            chat_id, sudo_uid,
            f"Broadcasting to {len(users)} users\u2026\n\nSent: 0\nFailed: 0",
            buttons=admin_menu(), parse_mode="html",
        )
        sent = failed = 0
        cancelled = False
        for index, target in enumerate(users, 1):
            if self.state(sudo_uid).pending_admin_action == "__broadcast_stop__":
                cancelled = True
                self.state(sudo_uid).pending_admin_action = None
                break
            try:
                await self.client.send_message(target, text, link_preview=False)
                sent += 1
            except Exception as exc:
                failed += 1
                wait = getattr(exc, "seconds", None)
                if wait:
                    # A flood-wait is not a hard failure: back off and retry.
                    failed -= 1
                    with contextlib.suppress(Exception):
                        await asyncio.sleep(min(60, float(wait)))
                    try:
                        await self.client.send_message(target, text, link_preview=False)
                        sent += 1
                    except Exception:
                        failed += 1
            await asyncio.sleep(0.05)
            if index % 25 == 0 or index == len(users):
                await self.safe_edit(
                    status,
                    f"Broadcasting\u2026\n\n<b>Total:</b> {len(users)}\n<b>Sent:</b> {sent}\n<b>Failed:</b> {failed}",
                    parse_mode="html",
                )
        state = "stopped" if cancelled else "complete"
        await self.safe_edit(
            status,
            f"Broadcast {state}\n\n<b>Total:</b> {len(users)}\n<b>Sent:</b> {sent}\n<b>Failed:</b> {failed}",
            buttons=admin_menu(), parse_mode="html",
        )

    async def begin_upload_flow(self, chat_id: int, uid: int, destination: str | None = None):
        # Kept as a compatibility wrapper. Destination is selected first; only
        # then is the optional rename prompt shown, so rename can never be asked twice.
        await self.choose_upload(chat_id, uid, destination)

    async def apply_rename_and_continue(self, chat_id: int, uid: int, text: str):
        st = self.state(uid)
        dest = st.pending_upload_destination
        st.pending_upload_destination = None
        st.pending = None
        if not st.path or not st.path.exists():
            await self.render_ui(chat_id, uid, "❌ Current file is no longer available.", buttons=main_menu())
            return
        new_name = safe_filename(text.strip(), st.path.name)
        old = st.path
        if not Path(new_name).suffix and old.suffix:
            new_name += old.suffix
        target = old.with_name(new_name)
        if target != old and target.exists():
            target = unique_path(old.parent, new_name)
        try:
            old.rename(target)
        except OSError as exc:
            await self.render_ui(chat_id, uid, f"Rename failed: {esc(error_text(exc))}", buttons=main_menu(), parse_mode="html")
            return
        st.path = target
        st.original_name = target.name
        st.outputs = [target] if len(st.outputs) <= 1 else [target if p == old else p for p in st.outputs]
        if st.source_path == old: st.source_path = target
        if st.root_path == old: st.root_path = target
        await self.choose_upload(chat_id, uid, dest, skip_rename=True)

    async def choose_upload(self, chat_id, uid, destination: str | None = None, skip_rename: bool = False):
        st = self.state(uid)
        if not st.path or not st.path.exists():
            files = [p for p in st.outputs if p.exists()] if st.outputs else []
            if not files:
                await self.render_ui(chat_id, uid, "❌ No current file to upload.", buttons=main_menu())
                return
            st.path = files[0]
        # A fixed setting is used only when no explicit destination was chosen.
        if destination is None:
            mode = self.db.get_upload_mode(uid)
            if mode in {"telegram", "gofile"}:
                destination = mode
            else:
                await self.render_ui(chat_id, uid, "📤 <b>Choose upload destination</b>", buttons=upload_menu(), parse_mode="html")
                return
        if destination not in {"telegram", "gofile"}:
            await self.render_ui(chat_id, uid, "❌ Invalid upload destination.", buttons=upload_menu())
            return
        if not skip_rename and self.db.get_rename(uid) and len(self._upload_files(st)) == 1:
            st.pending_upload_destination = destination
            st.pending = "rename_choice"
            await self.render_ui(
                chat_id, uid,
                "✏️ <b>Rename before upload?</b>\n\nChoose Rename to enter a new filename, or Skip to upload with the current name.",
                buttons=rename_menu(), parse_mode="html"
            )
            return
        st.pending_upload_destination = None
        st.pending = None
        if destination == "telegram":
            await self.upload_telegram(chat_id, uid)
        else:
            await self.run_gofile(chat_id, uid)

    def _upload_files(self, st: UserState) -> list[Path]:
        files = [p for p in st.outputs if p and p.is_file() and p.exists()]
        if not files and st.path and st.path.exists():
            files = [st.path]
        # Avoid duplicate filesystem paths, which was the cause of the old
        # 2.35 GiB -> 5.03 GiB merge/upload regression.
        seen = set(); result = []
        for p in files:
            key = str(p.resolve())
            if key not in seen:
                seen.add(key); result.append(p)
        return result

    async def _gofile_upload_tree(self, chat_id: int, uid: int, files: list[Path], reporter, total_bytes: int, st: UserState):
        from .services.gofile import delete_content
        token = self.db.get_gofile_token(uid, self.cfg.gofile_api_token)
        folder_id = self.db.get_gofile_folder(uid)
        root = st.output_root
        preserve_tree = bool(root and root.exists() and len(files) > 1)
        if preserve_tree and root:
            # Create a dedicated archive folder. If a guest has no persistent
            # folder yet, bootstrap one with a tiny file, then remove it.
            if not folder_id:
                bootstrap = self.cfg.work_dir / str(uid) / ".gofile_bootstrap"
                bootstrap.parent.mkdir(parents=True, exist_ok=True)
                bootstrap.write_bytes(b"0")
                data = await upload_gofile(bootstrap, token, None, cancel_event=st.cancel_event)
                bootstrap.unlink(missing_ok=True)
                returned_guest = data.get("guestToken")
                if returned_guest and not token:
                    token = str(returned_guest); self.db.set_gofile_token(uid, token)
                folder_id = str(data.get("parentFolder") or data.get("folderId") or "") or None
                content_id = data.get("fileId") or data.get("id") or data.get("contentId")
                if content_id and token:
                    try: await delete_content(str(content_id), token)
                    except Exception: log.warning("unable to remove GoFile bootstrap content", exc_info=True)
                if folder_id:
                    self.db.set_gofile_folder(uid, folder_id)
            if not folder_id:
                raise RuntimeError("GoFile did not return a destination folder")
            root_data = await create_folder(folder_id, root.name, token)
            archive_root_id = str(root_data.get("folderId") or root_data.get("id") or root_data.get("contentId") or "")
            if not archive_root_id:
                raise RuntimeError(f"GoFile did not return the created folder id: {root_data}")
            base_folder = archive_root_id
        else:
            base_folder = folder_id
        # If no persistent folder exists for a normal upload, the first upload
        # creates one; persist its guest token/folder for the rest of the batch.
        folder_cache = {"": base_folder}
        uploaded = []
        offset = 0
        for path in files:
            if st.cancel_event.is_set(): raise asyncio.CancelledError
            if preserve_tree and root:
                rel_parent = path.parent.relative_to(root).parts
                key = ""
                current = base_folder
                for part in rel_parent:
                    key = f"{key}/{part}" if key else part
                    if key not in folder_cache:
                        data = await create_folder(current, part, token)
                        child = str(data.get("folderId") or data.get("id") or data.get("contentId") or "")
                        if not child: raise RuntimeError(f"GoFile did not return folder id for {part}")
                        folder_cache[key] = child
                    current = folder_cache[key]
                dest_folder = current
            else:
                dest_folder = base_folder
            size = path.stat().st_size
            async def cb(cur, file_total, *, base=offset):
                if st.cancel_event.is_set(): raise asyncio.CancelledError
                await reporter.update(base + int(cur), total_bytes, f"📤 Uploading {path.name}")
            data = await upload_gofile(path, token, dest_folder, cb, st.cancel_event)
            returned_guest = data.get("guestToken")
            if returned_guest and not token:
                token = str(returned_guest); self.db.set_gofile_token(uid, token)
            returned_folder = data.get("parentFolder") or data.get("folderId") or dest_folder
            if returned_folder and not folder_id and not preserve_tree:
                folder_id = str(returned_folder); self.db.set_gofile_folder(uid, folder_id)
            uploaded.append((path, data))
            offset += size
            await reporter.update(offset, total_bytes, f"📤 Uploaded {path.name}")
        return uploaded, base_folder if preserve_tree else folder_id

    async def run_gofile(self, chat_id, uid, label="Uploading to GoFile"):
        st = self.state(uid)
        if st.busy:
            await self.render_card(chat_id, uid, "A process is already running. Press Cancel first.", buttons=cancel_menu())
            return
        files = self._upload_files(st)
        if not files:
            await self.render_ui(chat_id, uid, "No current file(s) to upload.", buttons=main_menu())
            return
        self.begin_job(uid, "GoFile upload")
        total_bytes = sum(self._safe_size(p) for p in files)
        status = await self.new_status_message(
            chat_id, uid,
            f"{esc(label)}\n<b>Files:</b> {len(files)}\n<b>Total:</b> {format_bytes(total_bytes)}",
            buttons=cancel_menu(), parse_mode="html",
        )
        reporter = await self.progress_message(status, uid=uid)
        try:
            async with self.semaphore:
                uploaded, folder_id = await self._gofile_upload_tree(chat_id, uid, files, reporter, total_bytes, st)
            lines = ["<b>GoFile upload complete</b>"]
            if st.output_root and len(files) > 1 and folder_id:
                lines.append(f"<b>Folder:</b> https://gofile.io/d/{folder_id}")
            else:
                for path, data in uploaded:
                    link = data.get("downloadPage") or data.get("download_page") or data.get("directLink") or data.get("link")
                    lines.append(f"\u2022 <b>{esc(safe_filename(path.name))}</b> \u2014 {format_bytes(self._safe_size(path))}")
                    if link:
                        lines.append(f"  {esc(link)}")
            await self.safe_edit(status, "\n".join(lines), buttons=None, parse_mode="html", link_preview=False)
            await self.stop_timeout(uid)
            self.cleanup_user_files(uid)
            self._reset_session(st)
        except asyncio.CancelledError:
            await self.cancelled_status(uid, status)
            self.cleanup_user_files(uid)
        except Exception as exc:
            log.exception("GoFile upload failed")
            await self.safe_edit(status, f"GoFile upload failed:\n{esc(error_text(exc))}", buttons=None, parse_mode="html")
        finally:
            self.end_job(uid)

    async def upload_telegram(self, chat_id, uid):
        st = self.state(uid)
        if st.busy:
            await self.render_card(chat_id, uid, "A process is already running. Press Cancel first.", buttons=cancel_menu())
            return
        files = self._upload_files(st)
        if not files:
            await self.render_ui(chat_id, uid, "No current file.", buttons=main_menu())
            return
        self.begin_job(uid, "Telegram upload")
        status = await self.new_status_message(chat_id, "Uploading to Telegram using MTProto\u2026", uid=uid, buttons=cancel_menu())
        reporter = await self.progress_message(status, uid=uid)
        chunk_dir = self.cfg.work_dir / str(uid) / "telegram_chunks"
        try:
            telegram_mode = self.db.get_telegram_mode(uid)
            thumb = self.db.get_thumbnail(uid)
            total = sum(self._safe_size(p) for p in files)
            done = 0
            skipped = 0
            async with self.semaphore:
                for path in files:
                    if st.cancel_event.is_set():
                        raise asyncio.CancelledError
                    if not path.exists():
                        skipped += 1
                        continue
                    chunks = chunk_file(path, chunk_dir)
                    try:
                        for idx, item in enumerate(chunks, 1):
                            if st.cancel_event.is_set():
                                raise asyncio.CancelledError
                            item_size = self._safe_size(item)
                            caption = path.name if len(chunks) == 1 else f"{path.name} \u2014 part {idx}/{len(chunks)}"

                            async def cb(cur, file_total, *, base=done, label=item.name):
                                if st.cancel_event.is_set():
                                    raise asyncio.CancelledError
                                await reporter.update(base + int(cur), total, f"Uploading {label}")

                            await self.client.send_file(
                                chat_id, str(item), caption=caption,
                                force_document=(telegram_mode == "document" or len(chunks) > 1),
                                thumb=thumb if thumb and Path(thumb).exists() and len(chunks) == 1 else None,
                                progress_callback=cb,
                            )
                            done += item_size
                            await reporter.update(done, total, f"Uploaded {item.name}")
                    finally:
                        if len(chunks) > 1:
                            for c in chunks:
                                c.unlink(missing_ok=True)
            summary = f"<b>Telegram upload complete</b>\n<b>Files:</b> {len(files) - skipped}\n<b>Total:</b> {format_bytes(total)}"
            if skipped:
                summary += f"\n<i>{skipped} file(s) were no longer available and were skipped.</i>"
            await self.safe_edit(status, summary, buttons=None, parse_mode="html")
            await self.stop_timeout(uid)
            self.cleanup_user_files(uid)
            self._reset_session(st)
        except asyncio.CancelledError:
            await self.cancelled_status(uid, status)
            self.cleanup_user_files(uid)
        except Exception as exc:
            log.exception("Telegram MTProto upload failed")
            await self.safe_edit(status, f"Telegram upload failed:\n{esc(error_text(exc))}", buttons=None, parse_mode="html")
        finally:
            self.end_job(uid)

    @staticmethod
    def _safe_size(path: Path) -> int:
        try:
            return path.stat().st_size
        except OSError:
            return 0

    @staticmethod
    def _reset_session(st: UserState) -> None:
        """Clear the working set after a successful delivery."""
        st.path = st.source_path = st.root_path = None
        st.outputs.clear()
        st.streams.clear()
        st.merge_inputs.clear()
        st.output_root = None
        st.queue.drop_finished()
        st.pending = None
        st.view = "menu"

    async def execute(self, chat_id, uid, label, func, upload=False, set_source=True, ffmpeg=True):
        """Run a blocking media job in a thread with live progress.

        FFmpeg work is additionally capped by a dedicated semaphore so a burst
        of users cannot start more transcodes than the host can survive.
        """
        st = self.state(uid)
        if st.busy:
            await self.render_card(chat_id, uid, "A process is already running. Press Cancel first.", buttons=cancel_menu())
            return
        status = await self.new_status_message(chat_id, label + "…", buttons=cancel_menu(), uid=uid)
        reporter = await self.progress_message(status, uid=uid, operation=label)
        self.begin_job(uid, label)
        guard = self.ffmpeg_semaphore if ffmpeg else self.semaphore
        stop = asyncio.Event()
        flusher = asyncio.create_task(self._flush_ffmpeg_progress(reporter, label, stop))
        try:
            async with guard:
                job_token = process_control.set_job(uid)
                try:
                    out = await asyncio.to_thread(_invoke_job, func, reporter)
                finally:
                    process_control.reset_job(job_token)
            if st.cancel_event.is_set():
                raise asyncio.CancelledError
            st.path = Path(out)
            if not st.path.exists():
                raise RuntimeError("FFmpeg reported success but produced no file")
            st.original_name = st.path.name
            st.outputs = [st.path]
            if set_source:
                st.source_path = st.path
                st.root_path = st.path
            # Probing can take seconds on a large file; keep it off the event
            # loop and never let a probe failure fail a successful job.
            st.streams = (await asyncio.to_thread(ffprobe.safe_probe, st.path)).get("streams", [])
            await self.safe_edit(status, "Processing complete", buttons=None)
            if upload:
                await self.clear_status_message(uid, delete=True)
                st.task = None
                st.operation = None
                st.progress_current = st.progress_total = 0
                st.queue.paused = False
                await self.choose_upload(chat_id, uid)
        except asyncio.CancelledError:
            await self.cancelled_status(uid, status)
            self.cleanup_user_files(uid)
        except subprocess.TimeoutExpired:
            log.warning("job timed out for user %s: %s", uid, label)
            await self.safe_edit(status, "The job took too long and was stopped. Please try a smaller file.", buttons=[[Button.inline("Home", b"start:home")]])
            self.cleanup_user_files(uid)
        except Exception as exc:
            log.exception("media job failed")
            await self.safe_edit(status, f"{esc(label)} failed:\n{esc(error_text(exc))}", parse_mode="html")
            self.cleanup_user_files(uid)
        finally:
            stop.set()
            flusher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await flusher
            self.end_job(uid)

    async def run_media_job(self, chat_id, uid, operation):
        st = self.state(uid)
        await self.await_input(chat_id, uid)
        path = await self.video_media(st)
        if not path:
            await self.render_card(chat_id, uid, "\U0001F3AC This operation requires a video file.", buttons=video_menu())
            return
        suffix = {"mp4": ".mp4", "toaudio": ".mp3"}.get(operation, ".mkv")
        out = unique_path(self.cfg.work_dir / str(uid), f"{path.stem}.{operation}{suffix}")
        fn = {
            "remove_audio": lambda cb: ffmpeg.remove_audio(path, out, on_progress=cb),
            "optimize": lambda cb: ffmpeg.optimize(path, out, on_progress=cb),
            "mp4": lambda cb: ffmpeg.convert_video(path, out, "mp4", on_progress=cb),
            "mkv": lambda cb: ffmpeg.convert_video(path, out, "mkv", on_progress=cb),
            "toaudio": lambda cb: ffmpeg.video_to_audio(path, out, on_progress=cb),
        }[operation]
        label = f"\U0001F3AC {operation.replace('_', ' ').title()}"
        await self.execute(chat_id, uid, label, self.make_ffmpeg_job(uid, fn, path), upload=True)

    async def run_audio_convert(self, chat_id, uid, codec, ext):
        st = self.state(uid)
        await self.await_input(chat_id, uid)
        path = self.source_media(st)
        if not path:
            await self.render_card(chat_id, uid, "\U0001F3B5 Send an audio or video file first.", buttons=main_menu())
            return
        out = unique_path(self.cfg.work_dir / str(uid), f"{path.stem}.converted{ext}")
        fn = lambda cb: ffmpeg.audio_convert(path, out, codec, on_progress=cb)  # noqa: E731
        await self.execute(chat_id, uid, "\U0001F3B5 Converting audio", self.make_ffmpeg_job(uid, fn, path), upload=True)

    async def run_audio_filter(self, chat_id, uid, filt, name):
        st = self.state(uid)
        await self.await_input(chat_id, uid)
        path = self.source_media(st)
        if not path:
            await self.render_card(chat_id, uid, "\U0001F3B5 Send an audio or video file first.", buttons=main_menu())
            return
        out = unique_path(self.cfg.work_dir / str(uid), f"{path.stem}.{safe_filename(name)}.m4a")
        fn = lambda cb: ffmpeg.audio_filter(path, out, filt, on_progress=cb)  # noqa: E731
        await self.execute(chat_id, uid, f"\U0001F3B5 Applying {esc(name)}", self.make_ffmpeg_job(uid, fn, path), upload=True)

    async def run_trim(self, chat_id, uid, text, audio=False):
        st = self.state(uid)
        st.pending = None
        parts = text.split()
        if len(parts) not in (1, 2):
            await self.render_card(chat_id, uid, "Use: <code>start end</code> (for example <code>00:05 00:30</code>).", buttons=audio_menu() if audio else video_menu())
            return
        end = parts[1] if len(parts) == 2 else None
        # Validate the input format *before* waiting, but never consult st.path
        # until the download the user already started has had a chance to land.
        await self.await_input(chat_id, uid)
        path = self.source_media(st) if audio else await self.video_media(st)
        if not path:
            await self.render_card(chat_id, uid, "No suitable media stream was found.", buttons=audio_menu() if audio else video_menu())
            return
        ext = path.suffix or ".mkv"
        out = unique_path(self.cfg.work_dir / str(uid), f"{path.stem}.trim{ext}")
        fn = lambda cb: ffmpeg.trim(path, out, parts[0], end, on_progress=cb)  # noqa: E731
        label = "Trimming audio" if audio else "Trimming video"
        await self.execute(chat_id, uid, label, self.make_ffmpeg_job(uid, fn, path), upload=True)

    async def run_manual_shot(self, chat_id, uid, text):
        st = self.state(uid)
        st.pending = None
        await self.await_input(chat_id, uid)
        path = await self.video_media(st)
        if not path:
            await self.render_ui(chat_id, uid, "Screenshots require a video file.", buttons=video_menu())
            return
        timestamps = [x.strip() for x in re.split(r"[,\n]+", text) if x.strip()]
        if not 1 <= len(timestamps) <= 20:
            await self.render_ui(chat_id, uid, "Provide between 1 and 20 timestamps.", buttons=video_menu())
            return
        for timestamp in timestamps:
            try:
                ffmpeg.parse_timecode(timestamp)
            except ValueError:
                await self.render_ui(chat_id, uid, f"Invalid timestamp: {esc(timestamp)}", buttons=video_menu(), parse_mode="html")
                return
        outdir = self.cfg.work_dir / str(uid) / "manual_shots"
        outdir.mkdir(parents=True, exist_ok=True)
        shots = []
        self.begin_job(uid, "Capturing frames")
        status = await self.new_status_message(chat_id, "Capturing frames…", uid=uid, buttons=cancel_menu())
        try:
            for i, timestamp in enumerate(timestamps, 1):
                if st.cancel_event.is_set():
                    raise asyncio.CancelledError
                out = unique_path(outdir, f"{path.stem}.shot_{i:02d}.jpg")
                await asyncio.to_thread(ffmpeg.manual_shot, path, out, timestamp)
                shots.append(out)
                await self.safe_edit(status, f"Captured {i}/{len(timestamps)} frames…", parse_mode="html")
            await self.client.send_file(chat_id, [str(p) for p in shots], caption=f"Manual Shots ({len(shots)})")
            st.status_message_id = None
            st.status_chat_id = None
            await self.show_file_menu(chat_id, uid, st.path)
        except asyncio.CancelledError:
            for shot in shots:
                shot.unlink(missing_ok=True)
            await self.cancelled_status(uid, status)
        except Exception as exc:
            for shot in shots:
                shot.unlink(missing_ok=True)
            await self.safe_edit(status, f"Screenshot failed:\n{esc(error_text(exc))}", parse_mode="html")
        finally:
            self.end_job(uid)

    async def run_split(self, chat_id, uid, text):
        st = self.state(uid)
        st.pending = None
        await self.await_input(chat_id, uid)
        path = await self.video_media(st)
        if not path:
            await self.render_card(chat_id, uid, "📦 Splitting requires a video file.", buttons=video_menu())
            return
        try:
            seconds = int(str(text).strip())
        except ValueError:
            await self.render_card(chat_id, uid, "🔢 Enter a whole number of seconds.", buttons=video_menu())
            return
        if seconds < 1:
            await self.render_card(chat_id, uid, "🔢 Segment length must be at least 1 second.", buttons=video_menu())
            return
        outdir = self.cfg.work_dir / str(uid) / "split"
        self.begin_job(uid, "Splitting video")
        status = await self.new_status_message(chat_id, "Splitting video…", uid=uid, buttons=cancel_menu())
        try:
            job_token = process_control.set_job(uid)
            try:
                async with self.ffmpeg_semaphore:
                    parts = await asyncio.to_thread(ffmpeg.split_video, path, outdir, seconds)
            finally:
                process_control.reset_job(job_token)
            if st.cancel_event.is_set():
                raise asyncio.CancelledError
            st.outputs = parts
            if parts:
                st.path = parts[0]
            st.source_path = path
            st.root_path = path
            st.output_root = outdir
            rows = "\n".join(
                f"\u2022 <code>{esc(p.name)}</code> ({format_bytes(p.stat().st_size)})" for p in parts[:20]
            )
            more = f"\n\u2026 and {len(parts) - 20} more" if len(parts) > 20 else ""
            text_out = (
                f"Created {len(parts)} segment(s).\n\n{rows}{more}\n\n"
                "Use Upload to send them to Telegram or GoFile."
            )
            await self.safe_edit(status, text_out, buttons=upload_menu(), parse_mode="html")
            st.status_message_id = None
            st.status_chat_id = None
            st.view = "menu"
        except asyncio.CancelledError:
            await self.cancelled_status(uid, status)
            self.cleanup_user_files(uid)
        except Exception as exc:
            await self.safe_edit(status, f"Split failed:\n{esc(error_text(exc))}", parse_mode="html")
        finally:
            self.end_job(uid)

    async def run_sample(self, chat_id, uid, text):
        st = self.state(uid)
        st.pending = None
        await self.await_input(chat_id, uid)
        path = await self.video_media(st)
        if not path:
            await self.render_card(chat_id, uid, "🎬 Generate Sample requires a video file.", buttons=video_menu())
            return
        try:
            seconds = int(str(text).strip() or "30")
        except ValueError:
            seconds = 30
        out = unique_path(self.cfg.work_dir / str(uid), f"{path.stem}.sample.mkv")
        fn = lambda cb: ffmpeg.sample(path, out, seconds, on_progress=cb)  # noqa: E731
        await self.execute(chat_id, uid, "Generating sample", self.make_ffmpeg_job(uid, fn, path), upload=True)

    async def run_shots(self, chat_id, uid, text):
        st = self.state(uid)
        st.pending = None
        await self.await_input(chat_id, uid)
        path = await self.video_media(st)
        if not path:
            await self.render_ui(chat_id, uid, "Screenshot generation requires a video file.", buttons=video_menu())
            return
        try:
            count = int(text.strip())
            if not 1 <= count <= 20:
                raise ValueError
        except ValueError:
            await self.render_ui(chat_id, uid, "Screenshot count must be a number from 1 to 20.", buttons=video_menu())
            return
        self.begin_job(uid, "Extracting screenshots")
        status = await self.new_status_message(chat_id, "Extracting screenshots…", uid=uid, buttons=cancel_menu())
        shots: list[Path] = []
        try:
            async with self.ffmpeg_semaphore:
                job_token = process_control.set_job(uid)
                try:
                    shots = await asyncio.to_thread(ffmpeg.screenshots, path, self.cfg.work_dir / str(uid) / "shots", count)
                finally:
                    process_control.reset_job(job_token)
            if st.cancel_event.is_set():
                raise asyncio.CancelledError
            await self.client.send_file(chat_id, [str(p) for p in shots], caption=f"Screenshots ({len(shots)})")
            st.status_message_id = None
            st.status_chat_id = None
            await self.show_file_menu(chat_id, uid, st.path)
        except asyncio.CancelledError:
            for shot in shots:
                shot.unlink(missing_ok=True)
            await self.cancelled_status(uid, status)
        except Exception as exc:
            for shot in shots:
                shot.unlink(missing_ok=True)
            await self.safe_edit(status, f"Screenshot generation failed:\n{esc(error_text(exc))}", parse_mode="html")
        finally:
            self.end_job(uid)

    async def cancel(self, uid, chat_id, source_message=None, admin: bool = False):
        st = self.state(uid)
        was_busy = st.busy
        st.cancel_event.set()
        st.pending = None
        st.pending_admin_action = None
        st.pending_upload_destination = None
        st.custom_remove.clear()
        st.merge_inputs.clear()
        st.archive_parts.clear()
        st.archive_series = st.archive_password = st.archive_mode = None
        # Stop background downloads too, otherwise a cancelled session keeps
        # pulling gigabytes the user no longer wants.
        self.cancel_downloads(uid)
        # Cancel the owning coroutine so network waits stop immediately. FFmpeg
        # workers are also guarded by cleanup; no user file is deleted here.
        if was_busy and st.task is not asyncio.current_task():
            process_control.cancel_job(uid)
            try:
                st.task.cancel()
            except Exception:
                pass
        # Remove the active progress message/keyboard and immediately remove
        # temporary server files. Valid direct-link files are protected by DB
        # expiry and therefore survive until their link expires.
        await self.clear_status_message(uid, delete=True)
        await self.stop_timeout(uid)
        self.cleanup_user_files(uid, force=True)
        st.path = st.source_path = st.root_path = None
        st.outputs.clear()
        st.streams.clear()
        st.output_root = None
        st.view = "start"
        st.operation = None
        st.progress_current = st.progress_total = 0
        st.started_at = None
        st.task = None
        # Return the existing UI message to the start screen. This removes all
        # operation menus while keeping exactly one stable start menu.
        with contextlib.suppress(Exception):
            await self.send_start_info(chat_id, uid, source=source_message)

    async def _direct_cleanup_loop(self):
        try:
            while True:
                await asyncio.sleep(300)
                now = int(time.time())
                rows = []
                try:
                    rows = self.db.conn.execute("SELECT token,path,expires_at FROM direct_links WHERE expires_at < ?", (now,)).fetchall()
                except Exception:
                    log.exception("failed to read expired direct links")
                self.db.purge_direct_links(now)
                active = self.db.active_direct_paths(now)
                for _, raw_path, _ in rows:
                    p = Path(raw_path)
                    if str(p.resolve()) not in active:
                        p.unlink(missing_ok=True)
        except asyncio.CancelledError:
            return

    def _content_disposition(self, name: str, inline: bool) -> str:
        """Build a valid Content-Disposition for arbitrary file names.

        Non-ASCII names must use RFC 6266's ``filename*`` form; putting raw
        UTF-8 in ``filename`` makes some browsers mangle or reject the header.
        """
        disposition = "inline" if inline else "attachment"
        try:
            name.encode("ascii")
        except UnicodeEncodeError:
            from urllib.parse import quote as _quote
            ascii_name = safe_filename(name.encode("ascii", "ignore").decode() or "file")
            return f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{_quote(name)}"
        return f'{disposition}; filename="{name}"'

    async def start_web_server(self):
        app = web.Application(client_max_size=0)

        async def serve(request):
            token = request.match_info["token"]
            row = self.db.get_direct_link(token)
            if not row:
                raise web.HTTPNotFound(text="Link not found or expired")
            _, raw_path, expires = row
            if int(expires) < int(time.time()):
                self.db.purge_direct_links(int(time.time()))
                raise web.HTTPGone(text="Link expired")
            path = Path(raw_path).resolve()
            allowed_roots = [self.cfg.download_dir.resolve(), self.cfg.work_dir.resolve()]
            if not path.is_file() or not any(path == root or root in path.parents for root in allowed_roots):
                raise web.HTTPNotFound(text="File not found")
            filename = safe_filename(request.match_info.get("filename") or path.name)
            disposition = self._content_disposition(filename, inline=True)
            if request.query.get("download") is not None:
                disposition = self._content_disposition(filename, inline=False)
            return web.FileResponse(
                path,
                headers={
                    "Content-Disposition": disposition,
                    "X-Content-Type-Options": "nosniff",
                },
            )

        async def index(request):
            return web.Response(text="Media Tools Bot direct-link server", content_type="text/plain")

        app.router.add_get("/f/{token}/{filename}", serve, allow_head=True)
        app.router.add_get("/", index)
        app.router.add_get("/health", lambda request: web.json_response({
            "ok": True,
            "transport": "mtproto",
            "ffmpeg": bool(shutil.which("ffmpeg")),
            "active_jobs": process_control.active_job_count(),
        }))
        self.web_runner = web.AppRunner(app)
        await self.web_runner.setup()
        site = web.TCPSite(self.web_runner, self.cfg.web_host, self.cfg.web_port)
        await site.start()
        log.info("Direct-link server listening on %s:%s", self.cfg.web_host, self.cfg.web_port)

    async def stop_web_server(self):
        if self.web_runner:
            await self.web_runner.cleanup()
            self.web_runner = None

    def _sweep_orphans_sync(self) -> None:
        """Remove leftover files from a previous run at start-up.

        A crash or a hard container restart leaves whole download/work
        directories behind. Nothing references them any more, so they are
        deleted once on boot instead of slowly filling the disk.

        This is deliberately *synchronous*: it was declared ``async def`` and
        then passed to ``asyncio.to_thread``, which accepts the coroutine and
        throws it away. The sweep therefore never ran, orphan directories
        accumulated across restarts, and they eventually filled the disk.
        """
        try:
            if not self.cfg.download_dir.exists():
                return
            known_users = set(self.db.all_users())
            removed = 0
            for entry in self.cfg.download_dir.iterdir():
                if not entry.is_dir():
                    continue
                if not entry.name.isdigit():
                    continue
                if int(entry.name) in known_users and self.state(int(entry.name)).queue.has_work():
                    continue
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
            if removed:
                log.info("removed %d orphaned download director%s", removed, "y" if removed == 1 else "ies")
        except Exception:
            log.exception("orphan sweep failed")

    async def _sweep_orphans(self) -> None:
        """Run the start-up orphan sweep off the event loop."""
        await asyncio.to_thread(self._sweep_orphans_sync)
        try:
            if not self.cfg.download_dir.exists():
                return
            known_users = set(self.db.all_users())
            removed = 0
            for entry in self.cfg.download_dir.iterdir():
                if not entry.is_dir():
                    continue
                if not entry.name.isdigit():
                    continue
                if int(entry.name) in known_users and self.state(int(entry.name)).queue.has_work():
                    continue
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
            if removed:
                log.info("removed %d orphaned download director%s", removed, "y" if removed == 1 else "ies")
        except Exception:
            log.exception("orphan sweep failed")

    async def run(self):
        await self.start_web_server()
        self.direct_cleanup_task = asyncio.create_task(self._direct_cleanup_loop())
        await self._sweep_orphans()
        await self.client.start(bot_token=self.cfg.bot_token)
        me = await self.client.get_me()
        log.info("Bot started via Telegram MTProto: @%s id=%s", me.username, me.id)
        log.info("Transport: MTProto only; HTTP Bot API/Local Bot API is NOT used")
        log.info("FFmpeg=%s FFprobe=%s cryptg=%s", shutil.which("ffmpeg"), shutil.which("ffprobe"), self._cryptg_status())
        log.info(
            "Limits: jobs=%s ffmpeg=%s parallel_downloads=%s max_download=%s",
            self.cfg.max_concurrent_jobs, self.cfg.max_ffmpeg_jobs,
            self.cfg.max_parallel_downloads,
            f"{self.cfg.max_download_mb}MiB" if self.cfg.max_download_mb else "unlimited",
        )
        try:
            await self.client.run_until_disconnected()
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        """Stop background work and release resources cleanly."""
        log.info("shutting down")
        for task in (self.direct_cleanup_task, self.sweeper_task):
            if task and not task.done():
                task.cancel()
        for uid, st in list(self.states.items()):
            st.cancel_event.set()
            with contextlib.suppress(Exception):
                self.cancel_downloads(uid)
        await self.stop_web_server()
        with contextlib.suppress(Exception):
            self.db.close()

    @staticmethod
    def _cryptg_status() -> str:
        try:
            import cryptg  # noqa: F401
            return "enabled"
        except Exception:
            return "optional-not-installed"


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = Config.from_env()
    bot = MediaToolsBot(cfg)
    await bot.run()
