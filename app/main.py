from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
import secrets
from aiohttp import web
from dataclasses import dataclass, field
from pathlib import Path

from telethon import Button, TelegramClient, events
from telethon.errors import MessageNotModifiedError
from telethon.tl.custom.message import Message

from .config import Config
from .storage.db import DB
from .ui.keyboards import audio_menu, cancel_menu, main_menu, upload_menu, video_menu, merge_menu, rename_menu, settings_menu, admin_menu
from .services import ffmpeg, ffprobe
from .services.archive import extract_archive, is_archive, archive_requires_password, multipart_info
from .services.downloader import download_url
from .services.gofile import upload_gofile, create_folder
from .services.telegraph import create_info_page
from .services.merge import merge_tracks
from .utils.files import format_bytes, format_duration, format_bitrate, safe_filename, unique_path
from .utils.progress import ProgressReporter
from .services.system import system_stats_text
from .services.telegram_upload import chunk_file, MAX_TELEGRAM_CHUNK
from telethon import functions, types
from .services import process_control

log = logging.getLogger("media-tools")


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
        self.client.add_event_handler(self.on_new_message, events.NewMessage)
        self.client.add_event_handler(self.on_callback, events.CallbackQuery)
        self.web_runner: web.AppRunner | None = None
        self.direct_cleanup_task: asyncio.Task | None = None

    def state(self, uid: int) -> UserState:
        if uid not in self.states:
            self.states[uid] = UserState()
        return self.states[uid]

    async def stop_timeout(self, uid: int) -> None:
        st = self.state(uid)
        if st.timeout_task and not st.timeout_task.done():
            st.timeout_task.cancel()
        st.timeout_task = None

    async def react_to_message(self, event) -> None:
        """React to every incoming private message using a small rotating set.

        This uses Telegram's real MTProto messages.sendReaction method. The
        Outgoing message effects are different from reactions on an existing
        user message, so this uses the real MTProto reaction request.
        """
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
        st = self.state(uid)
        if not st.menu_message_id or not st.menu_chat_id:
            return
        try:
            msg = await self.client.get_messages(st.menu_chat_id, ids=st.menu_message_id)
            if msg:
                await msg.delete()
        except Exception:
            pass
        st.menu_message_id = None
        st.menu_chat_id = None
        st.ui_message_id = None
        st.ui_chat_id = None

    def allowed(self, uid: int) -> bool:
        # SUDO_USERS grants elevated controls; it is not a whitelist.
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
        st.menu_message_id = st.menu_chat_id = None
        st.operation = None; st.progress_current = st.progress_total = 0; st.started_at = None
        try:
            ui = await self._get_ui_message(uid)
            timeout_text = "⏰ <b>Session timed out</b>\n\nTemporary files and the unfinished workflow were removed after 6 hours of inactivity. Valid direct-link files remain until their link expires. Send the media/URL again to redo the task."
            if ui:
                await self.safe_edit(ui, timeout_text, buttons=[[Button.inline("🏠 Start", b"start:home")]], parse_mode="html")
            else:
                await self.client.send_message(chat_id, timeout_text, parse_mode="html", buttons=[[Button.inline("🏠 Start", b"start:home")]])
        except Exception:
            pass

    def cleanup_user_files(self, uid: int) -> None:
        # Keep files still referenced by a valid Direct/Stream Link; those are
        # removed by the link-expiry task instead. Everything else belonging
        # to this user is safe to delete after the job completes.
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
                rows.append((st.started_at or 0, username, 1))
        rows.sort(reverse=True)
        if not rows:
            return "🧭 <b>Ongoing Processes</b>\n\nNo ongoing processes."
        lines = ["🧭 <b>Ongoing Processes</b>", "", "<b>User — Processes</b>"]
        for _, username, count in rows:
            handle = f"@{username.lstrip('@')}" if username and username != str(username).strip().isdigit() else username
            lines.append(f"• {handle} — {count}")
        lines.append("\nOnly one process is allowed per normal user.")
        return "\n".join(lines)

    async def new_status_message(self, chat_id: int, text: str, buttons=None, **kwargs):
        st = self.state(chat_id)
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
        try:
            return await msg.edit(text, buttons=buttons, **kwargs)
        except MessageNotModifiedError:
            return None

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

    async def progress_message(self, msg: Message, uid: int | None = None, operation: str | None = None):
        def hook(current, total, label):
            if uid is not None:
                st = self.state(uid)
                st.operation = operation or label
                st.progress_current = int(current or 0)
                st.progress_total = int(total or 0)
                st.last_activity = time.monotonic()
                if st.started_at is None:
                    st.started_at = time.monotonic()
                if st.timeout_task and not st.timeout_task.done():
                    st.timeout_task.cancel()
                st.timeout_task = asyncio.create_task(self._timeout_watch(uid, st.last_activity))
        return ProgressReporter(
            lambda text: self.safe_edit(msg, text, buttons=cancel_menu()),
            self.cfg.progress_interval,
            progress_hook=hook,
        )

    async def _get_ui_message(self, uid: int):
        st = self.state(uid)
        if not st.ui_message_id or not st.ui_chat_id:
            return None
        try:
            return await self.client.get_messages(st.ui_chat_id, ids=st.ui_message_id)
        except Exception:
            return None

    async def render_ui(self, chat_id: int, uid: int, text: str, buttons=None, parse_mode=None, link_preview=False, source=None, kind="menu"):
        st = self.state(uid)
        if kind != "start":
            self.touch(uid)
        if kind == "start":
            msg = source
            if msg is None and st.start_message_id and st.start_chat_id:
                try:
                    msg = await self.client.get_messages(st.start_chat_id, ids=st.start_message_id)
                except Exception:
                    msg = None
        else:
            msg = source
            if msg is None and st.menu_message_id and st.menu_chat_id:
                try:
                    msg = await self.client.get_messages(st.menu_chat_id, ids=st.menu_message_id)
                except Exception:
                    msg = None
        if msg is not None:
            try:
                await self.safe_edit(msg, text, buttons=buttons, parse_mode=parse_mode, link_preview=link_preview)
                if kind == "start":
                    st.start_chat_id = chat_id; st.start_message_id = msg.id
                else:
                    st.menu_chat_id = chat_id; st.menu_message_id = msg.id
                    st.ui_chat_id = chat_id; st.ui_message_id = msg.id
                return msg
            except Exception:
                pass
        msg = await self.client.send_message(chat_id, text, buttons=buttons, parse_mode=parse_mode, link_preview=link_preview)
        if kind == "start":
            st.start_chat_id = chat_id; st.start_message_id = msg.id
        else:
            st.menu_chat_id = chat_id; st.menu_message_id = msg.id
            st.ui_chat_id = chat_id; st.ui_message_id = msg.id
        return msg

    async def send_help(self, entity, uid: int, source=None):
        text = (
            "<b>📚 Media Tools Bot — Help</b>\n\n"
            "<b>📥 Input</b>\n"
            "• Send a Telegram media/document or an HTTP/HTTPS URL.\n"
            "• URL input uses the same processing workflow as Telegram media.\n\n"
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
            "• Permanent custom Telegram thumbnail\n\n"
            "<b>🔗 Direct links</b>\n"
            "• The bot can serve a downloaded file over HTTP with browser playback, downloads and Range requests.\n"
            "• Direct-linked files are retained for 24 hours by default.\n\n"
            "<b>🧹 Cleanup</b>\n"
            "• Temporary files are removed when a task completes or is cancelled.\n"
            "• Idle media sessions expire after 6 hours.\n\n"
            "<b>Commands</b>\n"
            "<code>/start</code> <code>/help</code> <code>/settings</code> <code>/upload</code> <code>/urlupload</code> <code>/merge</code> <code>/direct</code> <code>/cancel</code>\n"
            "<code>/rename on|off</code> <code>/uploadmode telegram|gofile|choose</code>\n"
            "<code>/setgofile TOKEN</code> <code>/cleargofile</code>\n\n"
            "Use Back to return to the previous dashboard."
        )
        buttons=[[Button.inline("⬅️ Back", b"start:home")]]
        return await self.render_ui(entity, uid, text, buttons=buttons, parse_mode="html", source=source, kind="start")

    async def send_start_info(self, entity, uid: int, source=None):
        st = self.state(uid)
        await self.delete_menu_message(uid)
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
        mode_label = {"telegram": "Telegram", "gofile": "GoFile", "choose": "Choose before upload"}[mode]
        tg_label = "Document" if telegram_mode == "document" else "Media"
        thumb = self.db.get_thumbnail(uid)
        text = (
            "⚙️ <b>Settings</b>\n\n"
            f"✏️ <b>Rename File:</b> {'Yes' if rename else 'No'}\n"
            f"📤 <b>Upload Destination:</b> {mode_label}\n"
            f"📄 <b>Telegram Upload:</b> {tg_label}\n"
            f"🖼️ <b>Custom Thumbnail:</b> {'Set' if thumb else 'Not set'}"
        )
        return await self.render_ui(entity, uid, text, buttons=settings_menu(rename, mode, telegram_mode, bool(thumb)), parse_mode="html", source=source, kind="start")

    async def send_bot_status(self, entity, uid: int, source=None):
        st = self.state(uid)
        text = (
            "<b>📊 Bot Status</b>\n\n"
            "<b>Transport:</b> MTProto / Telethon\n"
            "<b>Authorization:</b> Telegram bot authorization over MTProto\n"
            f"<b>FFmpeg:</b> {'OK' if shutil.which('ffmpeg') else 'MISSING'}\n"
            f"<b>FFprobe:</b> {'OK' if shutil.which('ffprobe') else 'MISSING'}\n"
            f"<b>Direct link server:</b> {'Configured' if self.cfg.public_base_url else 'Needs PUBLIC_BASE_URL'}\n"
            f"<b>Your job:</b> {'Running' if st.busy else 'Idle'}\n"
            f"<b>Pending workflow:</b> {st.pending or 'None'}\n"
            f"<b>Current operation:</b> {st.operation or 'None'}\n"
            f"<b>Current file:</b> {st.path.name if st.path else 'None'}\n"
            f"<b>Merge inputs:</b> {len(st.merge_inputs)}\n"
            f"<b>Global concurrency:</b> {self.cfg.max_concurrent_jobs}"
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
                await event.reply("❌ Sudo only.")
            elif not arg:
                st.pending_admin_action = "broadcast"
                await self.render_ui(event.chat_id, uid, "📢 <b>Broadcast</b>\n\nSend the message to broadcast to all registered users.", buttons=[[Button.inline("Cancel", b"cancel")]], parse_mode="html")
            else:
                await self.admin_broadcast(event.chat_id, uid, arg)
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
        st.cancel_event = asyncio.Event()
        st.task = asyncio.current_task()
        file = event.message.file
        name = safe_filename(file.name if file and file.name else f"telegram_{event.message.id}.bin")
        user_dir = self.cfg.download_dir / str(uid)
        out = unique_path(user_dir, name)
        status = await self.new_status_message(event.chat_id, f"📥 Preparing download...\n{name}", buttons=cancel_menu())
        reporter = await self.progress_message(status, uid=uid)
        try:
            async with self.semaphore:
                async def cb(cur, total):
                    if st.cancel_event.is_set():
                        raise asyncio.CancelledError
                    await reporter.update(int(cur), int(total or file.size or 0), "📥 Downloading from Telegram (MTProto)")
                result = await self.client.download_media(event.message, file=str(out), progress_callback=cb)
                if not result:
                    raise RuntimeError("Telegram returned no downloaded file")
            st.path = out; st.source_path = out; st.root_path = out
            st.original_name = name; st.outputs = [out]
            await self.safe_edit(status, f"✅ Telegram download complete\n{format_bytes(out.stat().st_size)}", buttons=None)
            await self.clear_status_message(uid, delete=True)
            await self.show_file_menu(event.chat_id, uid, out)
        except asyncio.CancelledError:
            out.unlink(missing_ok=True)
            await self.cancelled_status(uid, status)
        except Exception as exc:
            out.unlink(missing_ok=True)
            log.exception("Telegram media download failed")
            await self.safe_edit(status, f"❌ Telegram download failed:\n{type(exc).__name__}: {exc}", buttons=None)
        finally:
            self._clear_busy(uid)

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
            await event.reply(f"❌ Custom thumbnail failed: {type(exc).__name__}: {exc}")

    async def process_url(self, chat_id: int, uid: int, url: str):
        st = self.state(uid)
        if st.busy:
            await self.client.send_message(chat_id, "⚠️ A process is already running. Press Cancel first.")
            return
        st.pending = None
        st.cancel_event = asyncio.Event()
        st.task = asyncio.current_task()
        status = await self.new_status_message(chat_id, "📥 Downloading URL...", buttons=cancel_menu())
        reporter = await self.progress_message(status, uid=uid)
        try:
            async def cb(cur, total):
                if st.cancel_event.is_set():
                    raise asyncio.CancelledError
                await reporter.update(cur, total, "📥 Downloading URL")
            async with self.semaphore:
                out = await download_url(url, self.cfg.download_dir / str(uid), cb, st.cancel_event)
            st.path = out; st.source_path = out; st.root_path = out
            st.original_name = out.name; st.outputs = [out]
            await self.safe_edit(status, f"✅ URL download complete\n{format_bytes(out.stat().st_size)}", buttons=None)
            await self.clear_status_message(uid, delete=True)
            await self.show_file_menu(chat_id, uid, out)
        except asyncio.CancelledError:
            await self.cancelled_status(uid, status)
            out = locals().get("out")
            if isinstance(out, Path): out.unlink(missing_ok=True)
        except Exception as exc:
            log.exception("URL download failed")
            await self.safe_edit(status, f"❌ URL download failed:\n{type(exc).__name__}: {exc}", buttons=None)
        finally:
            self._clear_busy(uid)

    async def show_file_menu(self, chat_id: int, uid: int, path: Path):
        await self.delete_menu_message(uid)
        archive_note = "\n📦 <b>Archive detected:</b> Extract Archive is available." if is_archive(path) else ""
        await self.render_ui(
            chat_id, uid,
            f"📁 <b>{safe_filename(path.name)}</b>\n{format_bytes(path.stat().st_size)}{archive_note}\n\nPlease select your preferred action below 👇",
            buttons=main_menu(), parse_mode="html",
        )
        self.touch(uid)

    async def run_archive(self, chat_id, uid, source=None):
        st = self.state(uid)
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

    async def _begin_archive_extract(self, chat_id: int, uid: int, password: str | None = None):
        st = self.state(uid)
        parts = [p for p in st.archive_parts if p.exists() and p.is_file()]
        if not parts:
            await self.render_ui(chat_id, uid, "❌ No archive parts are available.", buttons=main_menu())
            return
        first = sorted(parts, key=lambda p: multipart_info(p)[1] if multipart_info(p) else 1)[0]
        outdir = self.cfg.work_dir / str(uid) / f"extracted_{int(time.time())}"
        st.pending = None
        st.archive_password = password
        st.cancel_event = asyncio.Event()
        st.task = asyncio.current_task()
        status = await self.new_status_message(chat_id, "📦 Extracting archive...", buttons=cancel_menu())
        try:
            async with self.semaphore:
                token = process_control.set_job(uid)
                try:
                    out = await asyncio.to_thread(extract_archive, first, outdir, password)
                finally:
                    process_control.reset_job(token)
            files = sorted(p for p in out.rglob("*") if p.is_file())
            if not files:
                raise RuntimeError("Archive extracted successfully but contains no files")
            st.outputs = files
            st.output_root = outdir
            st.path = files[0]
            st.source_path = first
            st.root_path = first
            st.streams = []
            await self.safe_edit(status, f"✅ Archive extracted\n<b>Files:</b> {len(files)}", buttons=None, parse_mode="html")
            await self.clear_status_message(uid, delete=True)
            st.archive_parts.clear(); st.archive_series = None; st.archive_password = None; st.archive_mode = None
            # Extraction is complete; now choose where the extracted tree goes.
            await self.choose_upload(chat_id, uid)
        except asyncio.CancelledError:
            shutil.rmtree(outdir, ignore_errors=True)
            await self.cancelled_status(uid, status, "❌ Extraction cancelled.")
        except Exception as exc:
            log.exception("archive extraction failed")
            shutil.rmtree(outdir, ignore_errors=True)
            text = str(exc)
            if "password" in text.lower() or "encrypted" in text.lower():
                st.pending = "archive_password"
                await self.safe_edit(status, "🔐 <b>Archive password required</b>\n\nSend the password or choose No Password.", buttons=archive_password_menu(), parse_mode="html")
            else:
                await self.safe_edit(status, f"❌ Extraction failed:\n{type(exc).__name__}: {exc}", buttons=[[Button.inline("⬅️ Back", b"menu:back")]])
        finally:
            self._clear_busy(uid)

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
        file = event.message.file
        name = safe_filename(file.name if file and file.name else f"archive_part_{event.message.id}.bin")
        out = unique_path(self.cfg.download_dir / str(uid) / "archive_parts", name)
        status = await self.new_status_message(event.chat_id, f"📥 Downloading archive part…\n{name}", buttons=cancel_menu())
        reporter = await self.progress_message(status, uid=uid)
        st.cancel_event = asyncio.Event(); st.task = asyncio.current_task()
        try:
            async def cb(cur, total):
                if st.cancel_event.is_set(): raise asyncio.CancelledError
                await reporter.update(cur, int(total or file.size or 0), "📥 Downloading archive part")
            result = await self.client.download_media(event.message, file=str(out), progress_callback=cb)
            if not result:
                raise RuntimeError("Telegram returned no downloaded file")
            await self.clear_status_message(uid, delete=True)
            await self._accept_archive_part(event.chat_id, uid, out)
        except asyncio.CancelledError:
            out.unlink(missing_ok=True)
            await self.cancelled_status(uid, status)
        except Exception as exc:
            out.unlink(missing_ok=True)
            await self.safe_edit(status, f"❌ Archive part download failed: {type(exc).__name__}: {exc}", buttons=None)
        finally:
            self._clear_busy(uid)

    async def process_archive_part_url(self, chat_id: int, uid: int, url: str):
        st = self.state(uid)
        if st.busy:
            await self.client.send_message(chat_id, "⚠️ A process is already running. Wait for it to finish or cancel it.")
            return
        st.cancel_event = asyncio.Event(); st.task = asyncio.current_task()
        status = await self.new_status_message(chat_id, "📥 Downloading archive part URL…", buttons=cancel_menu())
        reporter = await self.progress_message(status, uid=uid)
        try:
            async def cb(cur, total):
                if st.cancel_event.is_set(): raise asyncio.CancelledError
                await reporter.update(cur, total, "📥 Downloading archive part URL")
            out = await download_url(url, self.cfg.download_dir / str(uid) / "archive_parts", cb, st.cancel_event)
            await self.clear_status_message(uid, delete=True)
            await self._accept_archive_part(chat_id, uid, out)
        except asyncio.CancelledError:
            out = locals().get("out")
            if isinstance(out, Path): out.unlink(missing_ok=True)
            await self.cancelled_status(uid, status)
        except Exception as exc:
            out = locals().get("out")
            if isinstance(out, Path): out.unlink(missing_ok=True)
            await self.safe_edit(status, f"❌ Archive part download failed: {type(exc).__name__}: {exc}", buttons=None)
        finally:
            self._clear_busy(uid)

    async def process_merge_url(self, chat_id: int, uid: int, url: str):
        """Download an HTTP(S) URL as an additional merge input."""
        st = self.state(uid)
        if st.busy:
            await self.client.send_message(chat_id, "⚠️ A process is already running. Press Cancel first.")
            return
        st.cancel_event = asyncio.Event()
        st.task = asyncio.current_task()
        status = await self.new_status_message(chat_id, "📥 Downloading merge URL...", buttons=cancel_menu())
        reporter = await self.progress_message(status, uid=uid)
        try:
            async def cb(cur, total):
                if st.cancel_event.is_set():
                    raise asyncio.CancelledError
                await reporter.update(cur, total, "📥 Downloading merge URL")
            async with self.semaphore:
                out = await download_url(url, self.cfg.download_dir / str(uid) / "merge", cb, st.cancel_event)
            st.merge_inputs.append(out.resolve())
            await self.safe_edit(status, f"✅ Added merge URL: {out.name}\nTracks queued: {len(st.merge_inputs)}", buttons=None)
            st.status_message_id = None; st.status_chat_id = None
            await self.render_ui(chat_id, uid, self.merge_status_text(st), buttons=merge_menu(), parse_mode="html")
            self.touch(uid)
        except asyncio.CancelledError:
            await self.cancelled_status(uid, status)
        except Exception as exc:
            out = locals().get("out")
            if isinstance(out, Path):
                out.unlink(missing_ok=True)
            await self.safe_edit(status, f"❌ Merge URL download failed:\n{type(exc).__name__}: {exc}", buttons=None)
        finally:
            self._clear_busy(uid)

    async def handle_text(self, event, text: str):
        uid = event.sender_id
        st = self.state(uid)
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
            if re.match(r"^https?://", text, re.I):
                await self.process_url(event.chat_id, uid, text)
            else:
                await event.reply("Please send a valid http(s) URL.")
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
        if st.pending == "merge_collect":
            await event.reply(self.merge_status_text(st), buttons=merge_menu(), parse_mode="html")
            return
        if st.pending == "audio_speed":
            try:
                factor = float(text)
                if not 0.5 <= factor <= 2.0:
                    raise ValueError
                await self.run_audio_filter(event.chat_id, uid, f"atempo={factor:g}", "speed")
            except ValueError:
                await event.reply("❌ Enter a speed between 0.5 and 2.0.")
            return
        if st.pending == "audio_volume":
            await self.run_audio_filter(event.chat_id, uid, f"volume={text.strip()}", "volume"); return
        await event.reply("Send a media file or URL, or use /start.")

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
                await event.respond(f"❌ {type(exc).__name__}: {exc}")
            except Exception:
                pass

    async def callback_router(self, event, uid: int, data: str):
        st = self.state(uid)
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
            # Put the first part beside the remaining parts so 7z can discover the set.
            if st.path and st.path.exists():
                part_dir = self.cfg.download_dir / str(uid) / "archive_parts"
                part_dir.mkdir(parents=True, exist_ok=True)
                copied = part_dir / st.path.name
                if copied.resolve() != st.path.resolve():
                    shutil.copy2(st.path, copied)
                st.archive_parts = [copied.resolve()]
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
            await self.safe_edit(event, self.merge_status_text(st), buttons=merge_menu(), parse_mode="html")
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
            rows = [[Button.inline(("❌ " if s.get("index") in st.custom_remove else "") + ffprobe.stream_label(s)[:58], f"streamtoggle:{s.get('index')}".encode())] for s in st.streams]
            rows += [[Button.inline("✅ Apply", b"streamapply"), Button.inline("Cancel", b"cancel")]]
            await self.safe_edit(event, "Custom Streams: select streams to remove, then Apply.", buttons=rows); return
        if data == "streamapply":
            target = self.source_media(st)
            if not target: return
            keep = [s.get("index") for s in st.streams if s.get("index") not in st.custom_remove]
            if not keep:
                await self.safe_edit(event, "❌ You cannot remove every stream.", buttons=cancel_menu()); return
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
        target = self.source_media(st)
        if not target or not target.exists():
            await self.client.send_message(chat_id, "❌ Send a media file first."); return
        st.streams = ffprobe.probe(target).get("streams", [])
        rows = [[Button.inline(ffprobe.stream_label(s)[:60], f"stream:{mode}:{s.get('index')}".encode())] for s in st.streams]
        rows += [
            [Button.inline("All Audios", f"stream:{mode}:all_audio"), Button.inline("All Subtitles", f"stream:{mode}:all_sub")],
            [Button.inline("Custom Streams", f"stream:{mode}:custom"), Button.inline("All Streams", f"stream:{mode}:all")],
            [Button.inline("⬅️ Back", b"video:back"), Button.inline("Cancel Process", b"cancel")],
        ]
        await self.render_ui(chat_id, uid, "Select Your Required Option 👇", buttons=rows, source=source)

    async def handle_stream_callback(self, event, uid: int, data: str):
        _, mode, value = data.split(":", 2)
        st = self.state(uid)
        target = self.source_media(st)
        if not target: return
        streams = st.streams or ffprobe.probe(target).get("streams", [])
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
        if value == "custom":
            st.custom_remove.clear()
            rows = [[Button.inline(ffprobe.stream_label(s)[:60], f"streamtoggle:{s.get('index')}")] for s in streams]
            rows += [[Button.inline("✅ Apply", b"streamapply"), Button.inline("Cancel", b"cancel")]]
            await self.safe_edit(event, "Custom Streams: select streams to remove, then Apply.", buttons=rows); return
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

    async def stream_remux(self, chat_id, uid, keep):
        st = self.state(uid)
        target = self.source_media(st)
        if not target or not keep: return
        out = unique_path(self.cfg.work_dir / str(uid), f"{target.stem}.remux.mkv")
        await self.execute(chat_id, uid, "🧹 Removing streams", lambda: ffmpeg.remux(target, out, keep), upload=True, set_source=True)

    def source_media(self, st: UserState) -> Path | None:
        for candidate in (st.source_path, st.root_path, st.path):
            if candidate and candidate.exists():
                return candidate
        return None

    def video_media(self, st: UserState) -> Path | None:
        candidates = [st.path, st.source_path, st.root_path]
        for candidate in candidates:
            if candidate and candidate.exists():
                try:
                    if any(s.get("codec_type") == "video" for s in ffprobe.probe(candidate).get("streams", [])):
                        return candidate
                except Exception:
                    continue
        return None

    async def download_merge_input(self, event):
        uid = event.sender_id
        st = self.state(uid)
        file = event.message.file
        name = safe_filename(file.name if file and file.name else f"merge_{event.message.id}.bin")
        out = unique_path(self.cfg.download_dir / str(uid) / "merge", name)
        status = await self.new_status_message(event.chat_id, f"📥 Adding merge track...\n{name}", buttons=cancel_menu())
        reporter = await self.progress_message(status, uid=uid)
        st.cancel_event = asyncio.Event()
        st.task = asyncio.current_task()
        try:
            async def cb(cur, total):
                if st.cancel_event.is_set(): raise asyncio.CancelledError
                await reporter.update(int(cur), int(total or file.size or 0), "📥 Downloading merge track")
            async with self.semaphore:
                result = await self.client.download_media(event.message, file=str(out), progress_callback=cb)
            if not result or not out.exists() or out.stat().st_size == 0:
                raise RuntimeError("Telegram returned no downloaded merge track")
            st.merge_inputs.append(out.resolve())
            await self.safe_edit(status, f"✅ Added: {name}\nTracks queued: {len(st.merge_inputs)}", buttons=None)
            st.status_message_id = None; st.status_chat_id = None
            await self.render_ui(event.chat_id, uid, self.merge_status_text(st), buttons=merge_menu(), parse_mode="html")
            self.touch(uid)
        except asyncio.CancelledError:
            out.unlink(missing_ok=True)
            await self.cancelled_status(uid, status)
        except Exception as exc:
            out.unlink(missing_ok=True)
            await self.safe_edit(status, f"❌ Failed to add merge track: {exc}", buttons=None)
        finally:
            self._clear_busy(uid)

    def merge_status_text(self, st: UserState) -> str:
        valid = [Path(p) for p in st.merge_inputs if Path(p).exists()]
        rows = []
        for i, p in enumerate(valid, 1):
            try:
                size = format_bytes(p.stat().st_size)
            except OSError:
                size = "unknown size"
            rows.append(f"<b>{i}.</b> {safe_filename(p.name)} — {size}")
        listing = "\n".join(rows) if rows else "<i>No files queued yet.</i>"
        return (
            "📦 <b>Merge Tracks</b>\n\n"
            f"<b>Files queued: {len(valid)}</b>\n"
            f"{listing}\n\n"
            "Send another Telegram media file or HTTP/HTTPS URL to add it. "
            "When you are done, press <b>Finish Merge</b>.\n\n"
            "This combines tracks/streams into one MKV; it does not concatenate timelines."
        )

    async def start_merge(self, chat_id, uid, source=None):
        st = self.state(uid)
        if st.busy:
            await self.client.send_message(chat_id, "⚠️ A process is already running for you. Press Cancel first.")
            return
        base = self.source_media(st)
        if not base or not base.exists():
            await self.client.send_message(chat_id, "❌ Send or download a media file first.")
            return
        # Snapshot the first input immediately. Additional media messages are
        # appended by receive_media()/download_merge_input().
        st.pending = "merge_collect"
        st.merge_inputs = [base.resolve()]
        await self.render_ui(chat_id, uid, self.merge_status_text(st), buttons=merge_menu(), parse_mode="html", source=source)
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
                if ffprobe.probe(p).get("streams"):
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
                buttons=merge_menu(), parse_mode="html"
            )
            self.touch(uid)
            return
        # Freeze the queue before starting FFmpeg so later messages cannot
        # mutate the input list used by the worker.
        snapshot = tuple(inputs)
        st.pending = None
        out = unique_path(self.cfg.work_dir / str(uid), f"{safe_filename(snapshot[0].stem)}.merged.mkv")
        await self.execute(chat_id, uid, "🔀 Merging tracks", lambda: merge_tracks(list(snapshot), out), upload=True)
        # execute() leaves st.path pointing at the merged output on success.
        if st.path == out and out.exists():
            st.merge_inputs.clear()

    async def run_direct(self, chat_id, uid, source=None):
        st = self.state(uid)
        path = st.path
        if not path or not path.exists():
            await self.client.send_message(chat_id, "❌ Send or download a media file first."); return
        if not self.cfg.public_base_url:
            await self.client.send_message(
                chat_id,
                "❌ I cannot safely invent a public URL for this server.\n\nSet <code>PUBLIC_BASE_URL</code> to your public HTTPS origin. Railway/Render domains are detected automatically when their standard environment variable is available.\n\nThe service must expose <code>WEB_PORT</code> (default 8080) publicly.",
                parse_mode="html",
            )
            return
        self.db.purge_direct_links(int(time.time()))
        token = secrets.token_urlsafe(18)
        expires = int(time.time()) + self.cfg.direct_link_ttl
        self.db.add_direct_link(token, uid, str(path.resolve()), expires)
        from urllib.parse import quote
        link = f"{self.cfg.public_base_url}/f/{token}/{quote(path.name)}"
        await self.render_ui(
            chat_id, uid,
            f"🔗 <b>Direct/Stream Link</b>\n\n<a href=\"{link}\">{link}</a>\n\nExpires: {self.cfg.direct_link_ttl // 3600} hours\nThe link supports browser playback/download and HTTP Range requests.",
            buttons=[[Button.inline("⬅️ Back", b"menu:back")]], parse_mode="html", link_preview=True, source=source
        )
        asyncio.create_task(self._cleanup_direct_link_later(token, path, self.cfg.direct_link_ttl))

    async def _cleanup_direct_link_later(self, token: str, path: Path, ttl: int) -> None:
        try:
            await asyncio.sleep(max(1, ttl))
            row = self.db.get_direct_link(token)
            if row and int(row[2]) <= int(time.time()):
                self.db.purge_direct_links(int(time.time()))
                if str(path.resolve()) not in self.db.active_direct_paths(int(time.time())) and path.exists():
                    path.unlink(missing_ok=True)
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("direct-link cleanup failed")

    async def run_info(self, chat_id, uid, source=None):
        st = self.state(uid)
        path = self.source_media(st) or st.path
        if not path or not path.exists():
            await self.render_ui(chat_id, uid, "❌ Send a media file first.", buttons=main_menu())
            return
        status = await self.render_ui(chat_id, uid, "📋 Collecting detailed media information…", buttons=cancel_menu(), source=source)
        try:
            data = await asyncio.to_thread(ffprobe.probe, path)
            packet_sizes = await asyncio.to_thread(ffprobe.stream_packet_sizes, path)
            page = await create_info_page(
                data, path.name, format_bytes(path.stat().st_size),
                self.cfg.telegraph_access_token, packet_sizes=packet_sizes
            )
            text = f"📋 <b>{safe_filename(path.name)}</b>\n\n🔗 <a href=\"{page}\">Open detailed Media Information</a>"
            await self.safe_edit(status, text, buttons=[[Button.inline("⬅️ Back", b"video:back")]], parse_mode="html", link_preview=False)
            self.state(uid).status_message_id = None
            self.state(uid).status_chat_id = None
        except Exception as exc:
            log.exception("media information failed")
            await self.safe_edit(status, f"❌ Media information failed: {type(exc).__name__}: {exc}", buttons=[[Button.inline("⬅️ Back", b"video:back")]])

    async def run_thumbnail(self, chat_id, uid):
        st = self.state(uid)
        path = self.video_media(st)
        if not path: await self.client.send_message(chat_id, "❌ Send a video first."); return
        out = unique_path(self.cfg.work_dir / str(uid), f"{path.stem}.jpg")
        try:
            await asyncio.to_thread(ffmpeg.manual_shot, path, out, "00:00:01")
            await self.client.send_file(chat_id, str(out), caption="🖼️ Thumbnail")
        except Exception as exc:
            await self.client.send_message(chat_id, f"❌ Thumbnail failed: {exc}")

    async def run_archive(self, chat_id, uid):
        st = self.state(uid)
        if not st.path: await self.client.send_message(chat_id, "❌ Send an archive first."); return
        outdir = self.cfg.work_dir / str(uid) / "extracted"
        try:
            await asyncio.to_thread(extract_archive, st.path, outdir)
            files = [p for p in outdir.rglob("*") if p.is_file()]
            st.outputs = files
            if not files:
                await self.client.send_message(chat_id, "✅ Archive extracted, but it contained no files."); return
            preview = "\n".join(f"• {p.relative_to(outdir)} ({format_bytes(p.stat().st_size)})" for p in files[:30])
            more = f"\n… and {len(files)-30} more" if len(files) > 30 else ""
            await self.client.send_message(chat_id, f"✅ Extracted {len(files)} file(s).\n\n{preview}{more}\n\nUse /upload to choose a destination for the current output.")
            if files:
                st.path = files[0]
        except Exception as exc:
            await self.client.send_message(chat_id, f"❌ Extraction failed: {exc}")

    async def admin_cancel_user(self, chat_id: int, sudo_uid: int, identifier: str):
        if not self.is_sudo(sudo_uid):
            return
        target = self.db.find_user(identifier)
        if target is None:
            await self.render_ui(chat_id, sudo_uid, f"❌ User <code>{identifier}</code> is not registered.", buttons=admin_menu(), parse_mode="html")
            return
        if target == sudo_uid:
            await self.render_ui(chat_id, sudo_uid, "❌ You cannot cancel your own admin session from this control.", buttons=admin_menu(), parse_mode="html")
            return
        st = self.state(target)
        if not st.busy and not st.pending and not st.merge_inputs:
            await self.render_ui(chat_id, sudo_uid, f"ℹ️ User <code>{target}</code> has no active process.", buttons=admin_menu(), parse_mode="html")
            return
        await self.cancel(target, target, admin=True)
        await self.render_ui(chat_id, sudo_uid, f"✅ Cancelled job for <code>{target}</code>.", buttons=admin_menu(), parse_mode="html")
        try:
            await self.client.send_message(target, "🛑 <b>Your process was cancelled by an administrator.</b>", parse_mode="html")
        except Exception:
            pass

    async def admin_broadcast(self, chat_id: int, sudo_uid: int, text: str):
        if not self.is_sudo(sudo_uid):
            return
        users = self.db.all_users()
        sent = failed = 0
        for target in users:
            try:
                await self.client.send_message(target, text, link_preview=False)
                sent += 1
            except Exception:
                failed += 1
        await self.render_ui(chat_id, sudo_uid, f"📢 <b>Broadcast complete</b>\n\nSent: {sent}\nFailed: {failed}\nTotal users: {len(users)}", buttons=admin_menu(), parse_mode="html")

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
            await self.render_ui(chat_id, uid, f"❌ Rename failed: {exc}", buttons=main_menu())
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
        files = [p for p in st.outputs if p and p.exists() and p.is_file()]
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

    async def run_gofile(self, chat_id, uid, label="📤 Uploading to GoFile"):
        st = self.state(uid)
        if st.busy:
            await self.client.send_message(chat_id, "⚠️ A process is already running. Press Cancel first.")
            return
        files = self._upload_files(st)
        if not files:
            await self.render_ui(chat_id, uid, "❌ No current file(s) to upload.", buttons=main_menu())
            return
        st.cancel_event = asyncio.Event(); st.task = asyncio.current_task()
        st.operation = "GoFile upload"; st.progress_current = 0; st.progress_total = 0; st.started_at = time.monotonic()
        total_bytes = sum(p.stat().st_size for p in files)
        status = await self.new_status_message(chat_id, f"{label}\n<b>Files:</b> {len(files)}\n<b>Total:</b> {format_bytes(total_bytes)}", buttons=cancel_menu(), parse_mode="html")
        reporter = await self.progress_message(status, uid=uid)
        try:
            async with self.semaphore:
                uploaded, folder_id = await self._gofile_upload_tree(chat_id, uid, files, reporter, total_bytes, st)
            lines = ["✅ <b>GoFile upload complete</b>"]
            if st.output_root and len(files) > 1 and folder_id:
                lines.append(f"📁 <b>Folder:</b> https://gofile.io/d/{folder_id}")
            else:
                for path, data in uploaded:
                    link = data.get("downloadPage") or data.get("download_page") or data.get("directLink") or data.get("link")
                    lines.append(f"• <b>{safe_filename(path.name)}</b> — {format_bytes(path.stat().st_size)}")
                    if link: lines.append(f"  {link}")
            await self.safe_edit(status, "\n".join(lines), buttons=None, parse_mode="html", link_preview=False)
            await self.stop_timeout(uid)
            self.cleanup_user_files(uid)
            st.path = st.source_path = st.root_path = None; st.outputs.clear(); st.streams.clear(); st.merge_inputs.clear(); st.output_root = None
            st.pending = None; st.operation = None; st.progress_current = st.progress_total = 0
        except asyncio.CancelledError:
            await self.cancelled_status(uid, status)
            self.cleanup_user_files(uid)
        except Exception as exc:
            log.exception("GoFile upload failed")
            await self.safe_edit(status, f"❌ GoFile upload failed:\n{type(exc).__name__}: {exc}", buttons=None)
        finally:
            self._clear_busy(uid); st.operation = None; st.progress_current = st.progress_total = 0

    async def upload_telegram(self, chat_id, uid):
        st = self.state(uid)
        if st.busy:
            await self.client.send_message(chat_id, "⚠️ A process is already running. Press Cancel first."); return
        files = self._upload_files(st)
        if not files:
            await self.render_ui(chat_id, uid, "❌ No current file.", buttons=main_menu()); return
        st.cancel_event = asyncio.Event(); st.task = asyncio.current_task()
        st.operation = "Telegram upload"; st.progress_current = 0; st.progress_total = 0; st.started_at = time.monotonic()
        status = await self.new_status_message(chat_id, "📤 Uploading to Telegram using MTProto...", buttons=cancel_menu())
        reporter = await self.progress_message(status, uid=uid)
        try:
            telegram_mode = self.db.get_telegram_mode(uid)
            thumb = self.db.get_thumbnail(uid)
            sent_items = []
            total = sum(p.stat().st_size for p in files)
            done = 0
            async with self.semaphore:
                for path in files:
                    chunks = chunk_file(path, self.cfg.work_dir / str(uid) / "telegram_chunks")
                    for idx, item in enumerate(chunks, 1):
                        item_size = item.stat().st_size
                        async def cb(cur, file_total, *, base=done):
                            if st.cancel_event.is_set(): raise asyncio.CancelledError
                            await reporter.update(base + int(cur), total, f"📤 Uploading {item.name}")
                        await self.client.send_file(
                            chat_id, str(item), caption=(path.name if len(chunks) == 1 else f"{path.name} — part {idx}/{len(chunks)}"),
                            force_document=(telegram_mode == "document" or len(chunks) > 1),
                            thumb=thumb if thumb and Path(thumb).exists() and len(chunks) == 1 else None,
                            progress_callback=cb,
                        )
                        done += item_size
                        await reporter.update(done, total, f"📤 Uploaded {item.name}")
                    if len(chunks) > 1:
                        for c in chunks: c.unlink(missing_ok=True)
            await self.safe_edit(status, f"✅ <b>Telegram upload complete</b>\n<b>Files:</b> {len(files)}\n<b>Total:</b> {format_bytes(total)}", buttons=None, parse_mode="html")
            await self.stop_timeout(uid)
            self.cleanup_user_files(uid)
            st.path = st.source_path = st.root_path = None; st.outputs.clear(); st.streams.clear(); st.merge_inputs.clear(); st.output_root = None
            st.pending = None
        except asyncio.CancelledError:
            await self.cancelled_status(uid, status); self.cleanup_user_files(uid)
        except Exception as exc:
            log.exception("Telegram MTProto upload failed")
            await self.safe_edit(status, f"❌ Telegram MTProto upload failed:\n{type(exc).__name__}: {exc}", buttons=None)
        finally:
            self._clear_busy(uid); st.operation = None; st.progress_current = st.progress_total = 0

    async def execute(self, chat_id, uid, label, func, upload=False, set_source=True):
        st = self.state(uid)
        if st.busy:
            await self.client.send_message(chat_id, "⚠️ A process is already running. Press Cancel first."); return
        st.cancel_event = asyncio.Event(); st.task = asyncio.current_task()
        st.operation = label; st.progress_current = 0; st.progress_total = 0; st.started_at = time.monotonic()
        status = await self.new_status_message(chat_id, label + "...", buttons=cancel_menu())
        try:
            async with self.semaphore:
                job_token = process_control.set_job(uid)
                try:
                    out = await asyncio.to_thread(func)
                finally:
                    process_control.reset_job(job_token)
            if st.cancel_event.is_set(): raise asyncio.CancelledError
            st.path = Path(out)
            st.original_name = st.path.name
            st.outputs = [st.path]
            if set_source:
                st.source_path = st.path
                st.root_path = st.path
            st.streams = ffprobe.probe(st.path).get("streams", [])
            await self.safe_edit(status, "✅ Processing complete", buttons=None)
            if upload:
                await self.clear_status_message(uid, delete=True)
                st.task = None
                st.operation = None
                st.progress_current = st.progress_total = 0
                await self.choose_upload(chat_id, uid)
        except asyncio.CancelledError:
            await self.cancelled_status(uid, status)
            self.cleanup_user_files(uid)
        except Exception as exc:
            log.exception("media job failed")
            await self.safe_edit(status, f"❌ {label} failed:\n{type(exc).__name__}: {exc}")
            self.cleanup_user_files(uid)
        finally:
            self._clear_busy(uid)
            if not upload:
                st.operation = None
                st.progress_current = st.progress_total = 0

    async def run_media_job(self, chat_id, uid, operation):
        st = self.state(uid)
        path = self.video_media(st)
        if not path:
            await self.client.send_message(chat_id, "❌ This operation requires a video file."); return
        suffix = {"mp4": ".mp4", "toaudio": ".mp3"}.get(operation, ".mkv")
        out = unique_path(self.cfg.work_dir / str(uid), f"{path.stem}.{operation}{suffix}")
        fn = {
            "remove_audio": lambda: ffmpeg.remove_audio(path, out),
            "optimize": lambda: ffmpeg.optimize(path, out),
            "mp4": lambda: ffmpeg.convert_video(path, out, "mp4"),
            "mkv": lambda: ffmpeg.convert_video(path, out, "mkv"),
            "toaudio": lambda: ffmpeg.video_to_audio(path, out),
        }[operation]
        await self.execute(chat_id, uid, f"🎬 {operation.replace('_',' ').title()}", fn, upload=True)

    async def run_audio_convert(self, chat_id, uid, codec, ext):
        st = self.state(uid)
        if not st.path: await self.client.send_message(chat_id, "❌ Send audio/video first."); return
        out = unique_path(self.cfg.work_dir / str(uid), f"{st.path.stem}.converted{ext}")
        await self.execute(chat_id, uid, "🎵 Converting audio", lambda: ffmpeg.audio_convert(st.path, out, codec), upload=True)

    async def run_audio_filter(self, chat_id, uid, filt, name):
        st = self.state(uid)
        if not st.path: await self.client.send_message(chat_id, "❌ Send audio/video first."); return
        out = unique_path(self.cfg.work_dir / str(uid), f"{st.path.stem}.{safe_filename(name)}.m4a")
        await self.execute(chat_id, uid, f"🎵 Applying {name}", lambda: ffmpeg.audio_filter(st.path, out, filt), upload=True)

    async def run_trim(self, chat_id, uid, text, audio=False):
        st = self.state(uid); st.pending = None
        parts = text.split()
        if len(parts) not in (1, 2) or not st.path:
            await self.client.send_message(chat_id, "❌ Use `start end`."); return
        end = parts[1] if len(parts) == 2 else None
        path = st.path if audio else self.video_media(st)
        if not path:
            await self.client.send_message(chat_id, "❌ No suitable media stream found."); return
        ext = path.suffix or ".mkv"
        out = unique_path(self.cfg.work_dir / str(uid), f"{path.stem}.trim{ext}")
        await self.execute(chat_id, uid, "✂️ Trimming audio" if audio else "✂️ Trimming video", lambda: ffmpeg.trim(path, out, parts[0], end), upload=True)

    async def run_manual_shot(self, chat_id, uid, text):
        st = self.state(uid); st.pending = None
        path = self.video_media(st)
        if not path:
            await self.render_ui(chat_id, uid, "❌ Screenshot requires a video file.", buttons=video_menu())
            return
        timestamps = [x.strip() for x in re.split(r"[,\n]+", text) if x.strip()]
        if not 1 <= len(timestamps) <= 20:
            await self.render_ui(chat_id, uid, "❌ Provide between 1 and 20 timestamps.", buttons=video_menu())
            return
        outdir = self.cfg.work_dir / str(uid) / "manual_shots"
        outdir.mkdir(parents=True, exist_ok=True)
        shots = []
        try:
            for i, timestamp in enumerate(timestamps, 1):
                out = unique_path(outdir, f"{path.stem}.shot_{i:02d}.jpg")
                await asyncio.to_thread(ffmpeg.manual_shot, path, out, timestamp)
                shots.append(out)
            await self.client.send_file(chat_id, [str(p) for p in shots], caption=f"🖼️ Manual Shots — {len(shots)}")
            await self.send_main(chat_id, uid)
        except Exception as exc:
            await self.render_ui(chat_id, uid, f"❌ Manual screenshot failed: {exc}", buttons=video_menu())

    async def run_split(self, chat_id, uid, text):
        st = self.state(uid); st.pending = None
        if not st.path: return
        try: seconds = int(text)
        except ValueError:
            await self.client.send_message(chat_id, "❌ Enter an integer number of seconds."); return
        if seconds < 1:
            await self.client.send_message(chat_id, "❌ Segment length must be at least 1 second."); return
        outdir = self.cfg.work_dir / str(uid) / "split"
        try:
            parts = await asyncio.to_thread(ffmpeg.split_video, st.path, outdir, seconds)
            st.outputs = parts
            if parts: st.path = parts[0]
            preview = "\n".join(f"• {p.name} ({format_bytes(p.stat().st_size)})" for p in parts[:20])
            await self.client.send_message(chat_id, f"✅ Created {len(parts)} segment(s).\n\n{preview}\n\nUse /upload telegram or /upload gofile for the selected first part.")
        except Exception as exc:
            await self.client.send_message(chat_id, f"❌ Split failed: {exc}")

    async def run_sample(self, chat_id, uid, text):
        st = self.state(uid); st.pending = None
        path = self.video_media(st)
        if not path:
            await self.client.send_message(chat_id, "❌ Generate Sample requires a video file. Your current selection is not a video.")
            return
        try: seconds = int(text or "30")
        except ValueError: seconds = 30
        out = unique_path(self.cfg.work_dir / str(uid), f"{path.stem}.sample.mkv")
        await self.execute(chat_id, uid, "🎥 Generating sample", lambda: ffmpeg.sample(path, out, seconds), upload=True)

    async def run_shots(self, chat_id, uid, text):
        st = self.state(uid); st.pending = None
        path = self.video_media(st)
        if not path:
            await self.render_ui(chat_id, uid, "❌ Screenshot generation requires a video file.", buttons=video_menu())
            return
        try:
            count = int(text.strip())
            if not 1 <= count <= 20:
                raise ValueError
        except ValueError:
            await self.render_ui(chat_id, uid, "❌ Screenshot count must be a number from 1 to 20.", buttons=video_menu())
            return
        try:
            shots = await asyncio.to_thread(ffmpeg.screenshots, path, self.cfg.work_dir / str(uid) / "shots", count)
            await self.client.send_file(chat_id, [str(p) for p in shots], caption=f"🖼️ Screenshots — {len(shots)}")
            await self.send_main(chat_id, uid)
        except Exception as exc:
            await self.render_ui(chat_id, uid, f"❌ Screenshot generation failed: {exc}", buttons=video_menu())

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
        # expiry and therefore survive until their 24-hour link expires.
        await self.clear_status_message(uid, delete=True)
        await self.stop_timeout(uid)
        self.cleanup_user_files(uid)
        # Return the existing UI message to the start screen. This removes all
        # operation menus while keeping exactly one stable start menu.
        try:
            await self.send_start_info(chat_id, uid)
        except Exception:
            await self.send_start_info(chat_id, uid)
        st.operation = None
        st.progress_current = st.progress_total = 0
        st.started_at = None

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
            return web.FileResponse(path, headers={"Content-Disposition": f'inline; filename="{safe_filename(path.name)}"'})

        app.router.add_get("/f/{token}/{filename}", serve, allow_head=True)
        app.router.add_get("/health", lambda request: web.json_response({"ok": True, "transport": "mtproto"}))
        self.web_runner = web.AppRunner(app)
        await self.web_runner.setup()
        site = web.TCPSite(self.web_runner, self.cfg.web_host, self.cfg.web_port)
        await site.start()
        log.info("Direct-link server listening on %s:%s", self.cfg.web_host, self.cfg.web_port)

    async def stop_web_server(self):
        if self.web_runner:
            await self.web_runner.cleanup()
            self.web_runner = None

    async def run(self):
        await self.start_web_server()
        self.direct_cleanup_task = asyncio.create_task(self._direct_cleanup_loop())
        await self.client.start(bot_token=self.cfg.bot_token)
        me = await self.client.get_me()
        log.info("Bot started via Telegram MTProto: @%s id=%s", me.username, me.id)
        log.info("Transport: MTProto only; HTTP Bot API/Local Bot API is NOT used")
        log.info("FFmpeg=%s FFprobe=%s cryptg=%s", shutil.which("ffmpeg"), shutil.which("ffprobe"), self._cryptg_status())
        try:
            await self.client.run_until_disconnected()
        finally:
            if self.direct_cleanup_task:
                self.direct_cleanup_task.cancel()
            await self.stop_web_server()

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
