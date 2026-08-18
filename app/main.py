from __future__ import annotations

import asyncio
import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from telethon import Button, TelegramClient, events
from telethon.errors import MessageNotModifiedError
from telethon.tl.custom.message import Message

from .config import Config
from .storage.db import DB
from .ui.keyboards import audio_menu, cancel_menu, main_menu, upload_menu, video_menu
from .services import ffmpeg, ffprobe
from .services.archive import extract_archive
from .services.downloader import download_url
from .services.gofile import upload_gofile
from .services.links import shorten, unshorten
from .utils.files import format_bytes, safe_filename, unique_path
from .utils.progress import ProgressReporter

log = logging.getLogger("media-tools")


@dataclass
class UserState:
    path: Path | None = None
    original_name: str | None = None
    outputs: list[Path] = field(default_factory=list)
    streams: list[dict] = field(default_factory=list)
    pending: str | None = None
    custom_remove: set[int] = field(default_factory=set)
    task: asyncio.Task | None = None
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)

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

    def state(self, uid: int) -> UserState:
        if uid not in self.states:
            self.states[uid] = UserState()
        return self.states[uid]

    def allowed(self, uid: int) -> bool:
        return not self.cfg.sudo_users or uid in self.cfg.sudo_users

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

    async def progress_message(self, msg: Message):
        return ProgressReporter(
            lambda text: self.safe_edit(msg, text, buttons=cancel_menu()),
            self.cfg.progress_interval,
        )

    async def send_main(self, entity, prefix: str | None = None):
        text = (prefix + "\n\n" if prefix else "") + "Please select your preferred action below 👇"
        return await self.client.send_message(entity, text, buttons=main_menu())

    async def on_new_message(self, event):
        if not event.is_private:
            return
        uid = event.sender_id
        if uid is None or not self.allowed(uid):
            return
        text = (event.raw_text or "").strip()
        if text.startswith("/"):
            await self.handle_command(event, text)
        elif event.message.media:
            await self.receive_media(event)
        elif text:
            await self.handle_text(event, text)

    async def handle_command(self, event, text: str):
        parts = text.split(maxsplit=1)
        cmd = parts[0].split("@", 1)[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        uid = event.sender_id
        if cmd in ("/start", "/menu", "/help"):
            await self.send_main(event.chat_id)
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
            rename = self.db.get_rename(uid)
            await event.reply(f"⚙️ Settings\nRename File: {'Yes' if rename else 'No'}\n\nUse /rename on or /rename off")
        elif cmd == "/rename":
            if arg.lower() not in ("on", "off", "yes", "no", "true", "false", "1", "0"):
                await event.reply("Usage: /rename on or /rename off")
            else:
                value = arg.lower() in ("on", "yes", "true", "1")
                self.db.set_rename(uid, value)
                await event.reply(f"✏️ Rename File: {'Yes' if value else 'No'}")
        elif cmd == "/urlupload":
            if not arg:
                self.state(uid).pending = "urlupload"
                await event.reply("🔗 Send the file URL to download.", buttons=cancel_menu())
            else:
                await self.process_url(event.chat_id, uid, arg)
        elif cmd == "/status":
            st = self.state(uid)
            await event.reply(f"Path: {st.path}\nRunning: {st.busy}\nOutputs: {len(st.outputs)}")
        else:
            await event.reply("Unknown command. Use /start.")

    async def receive_media(self, event):
        uid = event.sender_id
        st = self.state(uid)
        if st.busy:
            await event.reply("⚠️ A process is already running. Press Cancel first.", buttons=cancel_menu())
            return
        st.cancel_event = asyncio.Event()
        st.task = asyncio.current_task()
        file = event.message.file
        name = safe_filename(file.name if file and file.name else f"telegram_{event.message.id}.bin")
        user_dir = self.cfg.download_dir / str(uid)
        out = unique_path(user_dir, name)
        status = await event.reply(f"📥 Preparing download...\n{name}", buttons=cancel_menu())
        reporter = await self.progress_message(status)
        try:
            async with self.semaphore:
                async def cb(cur, total):
                    if st.cancel_event.is_set():
                        raise asyncio.CancelledError
                    await reporter.update(int(cur), int(total or file.size or 0), "📥 Downloading from Telegram (MTProto)")

                result = await self.client.download_media(event.message, file=str(out), progress_callback=cb)
                if not result:
                    raise RuntimeError("Telegram returned no downloaded file")
            st.path = out
            st.original_name = name
            st.outputs = [out]
            st.streams = ffprobe.probe(out).get("streams", [])
            await reporter.finish(out.stat().st_size, "✅ Telegram download complete")
            await self.show_file_menu(event.chat_id, uid, out)
        except asyncio.CancelledError:
            out.unlink(missing_ok=True)
            await self.safe_edit(status, "❌ Process cancelled.")
        except Exception as exc:
            out.unlink(missing_ok=True)
            log.exception("Telegram media download failed")
            await self.safe_edit(status, f"❌ Telegram download failed:\n{type(exc).__name__}: {exc}")
        finally:
            self._clear_busy(uid)

    async def process_url(self, chat_id: int, uid: int, url: str):
        st = self.state(uid)
        if st.busy:
            await self.client.send_message(chat_id, "⚠️ A process is already running. Press Cancel first.")
            return
        st.pending = None
        st.cancel_event = asyncio.Event()
        st.task = asyncio.current_task()
        status = await self.client.send_message(chat_id, "📥 Downloading URL...", buttons=cancel_menu())
        reporter = await self.progress_message(status)
        try:
            async def cb(cur, total):
                if st.cancel_event.is_set():
                    raise asyncio.CancelledError
                await reporter.update(cur, total, "📥 Downloading URL")

            async with self.semaphore:
                out = await download_url(url, self.cfg.download_dir / str(uid), cb, st.cancel_event)
            st.path = out
            st.original_name = out.name
            st.outputs = [out]
            st.streams = ffprobe.probe(out).get("streams", [])
            await reporter.finish(out.stat().st_size, "✅ URL download complete")
            await self.show_file_menu(chat_id, uid, out)
        except asyncio.CancelledError:
            await self.safe_edit(status, "❌ Process cancelled.")
        except Exception as exc:
            log.exception("URL download failed")
            await self.safe_edit(status, f"❌ URL download failed:\n{type(exc).__name__}: {exc}")
        finally:
            self._clear_busy(uid)

    async def show_file_menu(self, chat_id: int, uid: int, path: Path):
        await self.client.send_message(
            chat_id,
            f"📁 **{path.name}**\n{format_bytes(path.stat().st_size)}\n\nPlease select your preferred action below 👇",
            buttons=main_menu(),
            parse_mode="markdown",
        )

    async def handle_text(self, event, text: str):
        uid = event.sender_id
        st = self.state(uid)
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
        if st.pending == "split":
            await self.run_split(event.chat_id, uid, text); return
        if st.pending == "sample":
            await self.run_sample(event.chat_id, uid, text); return
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
        if st.pending == "link_action":
            try:
                parts = text.split(maxsplit=1)
                if len(parts) != 2 or parts[0].lower() not in {"short", "unshort"}:
                    raise ValueError("Use: short https://... or unshort https://...")
                result = await (shorten(parts[1]) if parts[0].lower() == "short" else unshorten(parts[1]))
                await event.reply("🔗 " + result)
            except Exception as exc:
                await event.reply(f"❌ Link operation failed: {exc}")
            st.pending = None
            return
        await event.reply("Send a media file or URL, or use /start.")

    async def on_callback(self, event):
        uid = event.sender_id
        if uid is None or not self.allowed(uid):
            await event.answer("Not authorized", alert=True)
            return
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
            await self.cancel(uid, event.chat_id); return
        if data == "menu:video":
            await self.safe_edit(event, "Please select your preferred action below 👇", buttons=video_menu()); return
        if data == "menu:audio":
            await self.safe_edit(event, "Please select your preferred action below 👇", buttons=audio_menu()); return
        if data == "menu:thumb":
            await self.run_thumbnail(event.chat_id, uid); return
        if data == "menu:direct":
            await self.run_gofile(event.chat_id, uid, "🔗 Creating Direct/Stream Link"); return
        if data == "menu:archive":
            await self.run_archive(event.chat_id, uid); return
        if data == "menu:urlupload":
            st.pending = "urlupload"
            await self.safe_edit(event, "🔗 Send the file URL to download.", buttons=cancel_menu()); return
        if data == "menu:links":
            st.pending = "link_action"
            await self.safe_edit(event, "🔗 Send:\n`short https://example.com`\nor\n`unshort https://short.url/...`", buttons=cancel_menu(), parse_mode="markdown"); return
        if data.startswith("upload:"):
            await self.choose_upload(event.chat_id, uid, data.split(":", 1)[1]); return
        if data == "video:info" or data == "audio:info":
            await self.run_info(event.chat_id, uid); return
        if data == "video:streams":
            await self.stream_menu(event.chat_id, uid, "remove"); return
        if data == "video:extract":
            await self.stream_menu(event.chat_id, uid, "extract"); return
        if data == "video:trim":
            st.pending = "trim"; await self.safe_edit(event, "✂️ Send `start end` in seconds or HH:MM:SS values.\nExample: `00:05 00:30`", buttons=cancel_menu(), parse_mode="markdown"); return
        if data == "video:remove_audio":
            await self.run_media_job(event.chat_id, uid, "remove_audio"); return
        if data == "video:optimize":
            await self.run_media_job(event.chat_id, uid, "optimize"); return
        if data == "video:split":
            st.pending = "split"; await self.safe_edit(event, "🎥 Send segment length in seconds. Example: `600`", buttons=cancel_menu(), parse_mode="markdown"); return
        if data == "video:shots":
            await self.run_shots(event.chat_id, uid); return
        if data == "video:manual":
            st.pending = "manual_shot"; await self.safe_edit(event, "🖼️ Send timestamp. Example: `00:05:30`", buttons=cancel_menu(), parse_mode="markdown"); return
        if data == "video:sample":
            st.pending = "sample"; await self.safe_edit(event, "🎥 Send sample duration in seconds. Default: 30", buttons=cancel_menu()); return
        if data == "video:toaudio":
            await self.run_media_job(event.chat_id, uid, "toaudio"); return
        if data in ("video:mp4", "video:mkv"):
            await self.run_media_job(event.chat_id, uid, data.split(":")[1]); return
        if data.startswith("streamtoggle:"):
            idx = int(data.split(":", 1)[1])
            if idx in st.custom_remove: st.custom_remove.remove(idx)
            else: st.custom_remove.add(idx)
            rows = [[Button.inline(("❌ " if s.get("index") in st.custom_remove else "") + ffprobe.stream_label(s)[:58], f"streamtoggle:{s.get('index')}".encode())] for s in st.streams]
            rows += [[Button.inline("✅ Apply", b"streamapply"), Button.inline("Cancel", b"cancel")]]
            await self.safe_edit(event, "Custom Streams: select streams to remove, then Apply.", buttons=rows); return
        if data == "streamapply":
            if not st.path: return
            keep = [s.get("index") for s in st.streams if s.get("index") not in st.custom_remove]
            if not keep:
                await self.safe_edit(event, "❌ You cannot remove every stream.", buttons=cancel_menu()); return
            out = unique_path(self.cfg.work_dir / str(uid), f"{st.path.stem}.custom.mkv")
            await self.execute(event.chat_id, uid, "🧹 Removing custom streams", lambda: ffmpeg.remux(st.path, out, keep), upload=True); return
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
            [Button.inline("Cancel", b"cancel")],
        ])

    async def callback_aconv(self, event, uid, fmt):
        codecs = {"mp3": ("libmp3lame", ".mp3"), "aac": ("aac", ".m4a"), "opus": ("libopus", ".opus"), "flac": ("flac", ".flac")}
        if fmt not in codecs:
            return
        codec, ext = codecs[fmt]
        await self.run_audio_convert(event.chat_id, uid, codec, ext)

    async def stream_menu(self, chat_id: int, uid: int, mode: str):
        st = self.state(uid)
        if not st.path or not st.path.exists():
            await self.client.send_message(chat_id, "❌ Send a media file first."); return
        st.streams = ffprobe.probe(st.path).get("streams", [])
        rows = [[Button.inline(ffprobe.stream_label(s)[:60], f"stream:{mode}:{s.get('index')}".encode())] for s in st.streams]
        rows += [
            [Button.inline("All Audios", f"stream:{mode}:all_audio"), Button.inline("All Subtitles", f"stream:{mode}:all_sub")],
            [Button.inline("Custom Streams", f"stream:{mode}:custom"), Button.inline("All Streams", f"stream:{mode}:all")],
            [Button.inline("Cancel Process", b"cancel")],
        ]
        await self.client.send_message(chat_id, "Select Your Required Option 👇", buttons=rows)

    async def handle_stream_callback(self, event, uid: int, data: str):
        _, mode, value = data.split(":", 2)
        st = self.state(uid)
        if not st.path: return
        streams = st.streams or ffprobe.probe(st.path).get("streams", [])
        if value.isdigit():
            idx = int(value)
            s = next((x for x in streams if x.get("index") == idx), None)
            if not s: return
            if mode == "extract":
                ext = ffmpeg._stream_output_extension(s)
                out = unique_path(self.cfg.work_dir / str(uid), f"{st.path.stem}.stream{idx}{ext}")
                await self.execute(event.chat_id, uid, "🎵 Extracting stream", lambda: ffmpeg.extract_stream(st.path, out, idx), upload=True)
            else:
                keep = [x.get("index") for x in streams if x.get("index") != idx]
                out = unique_path(self.cfg.work_dir / str(uid), f"{st.path.stem}.streams_removed.mkv")
                await self.execute(event.chat_id, uid, "🧹 Removing stream", lambda: ffmpeg.remux(st.path, out, keep), upload=True)
            return
        if value == "custom":
            st.custom_remove.clear()
            rows = [[Button.inline(ffprobe.stream_label(s)[:60], f"streamtoggle:{s.get('index')}")] for s in streams]
            rows += [[Button.inline("✅ Apply", b"streamapply"), Button.inline("Cancel", b"cancel")]]
            await self.safe_edit(event, "Custom Streams: select streams to remove, then Apply.", buttons=rows); return
        if value == "all_audio":
            keep = [s.get("index") for s in streams if s.get("codec_type") != "audio" or not self._is_nondefault(s)]
            await self.stream_remux(event.chat_id, uid, keep); return
        if value == "all_sub":
            keep = [s.get("index") for s in streams if s.get("codec_type") != "subtitle" or not self._is_nondefault(s)]
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
        if not st.path or not keep: return
        out = unique_path(self.cfg.work_dir / str(uid), f"{st.path.stem}.remux.mkv")
        await self.execute(chat_id, uid, "🧹 Removing streams", lambda: ffmpeg.remux(st.path, out, keep), upload=True)

    async def run_info(self, chat_id, uid):
        st = self.state(uid)
        if not st.path:
            await self.client.send_message(chat_id, "❌ Send a media file first."); return
        await self.client.send_message(chat_id, ffmpeg.media_info(st.path))

    async def run_thumbnail(self, chat_id, uid):
        st = self.state(uid)
        if not st.path: await self.client.send_message(chat_id, "❌ Send a video first."); return
        out = unique_path(self.cfg.work_dir / str(uid), f"{st.path.stem}.jpg")
        try:
            await asyncio.to_thread(ffmpeg.manual_shot, st.path, out, "00:00:01")
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

    async def choose_upload(self, chat_id, uid, destination: str | None = None):
        if destination == "telegram":
            await self.upload_telegram(chat_id, uid)
        elif destination == "gofile":
            await self.run_gofile(chat_id, uid)
        else:
            await self.client.send_message(chat_id, "📤 Choose upload destination", buttons=upload_menu())

    async def run_gofile(self, chat_id, uid, label="📤 Uploading to GoFile"):
        st = self.state(uid)
        if st.busy:
            await self.client.send_message(chat_id, "⚠️ A process is already running. Press Cancel first."); return
        if not st.path or not st.path.exists():
            await self.client.send_message(chat_id, "❌ No current file."); return
        st.cancel_event = asyncio.Event(); st.task = asyncio.current_task()
        status = await self.client.send_message(chat_id, label, buttons=cancel_menu())
        reporter = await self.progress_message(status)
        try:
            token = self.db.get_gofile_token(uid, self.cfg.gofile_api_token)
            async def cb(cur, total):
                if st.cancel_event.is_set(): raise asyncio.CancelledError
                await reporter.update(cur, total, label)
            data = await upload_gofile(st.path, token, cb, st.cancel_event)
            link = data.get("downloadPage") or data.get("download_page") or data.get("directLink") or data.get("link")
            await reporter.finish(st.path.stat().st_size, "✅ GoFile upload complete")
            await self.client.send_message(chat_id, f"🔗 {link or data}")
        except asyncio.CancelledError:
            await self.safe_edit(status, "❌ Process cancelled.")
        except Exception as exc:
            log.exception("GoFile upload failed")
            await self.safe_edit(status, f"❌ GoFile upload failed:\n{type(exc).__name__}: {exc}")
        finally:
            self._clear_busy(uid)

    async def upload_telegram(self, chat_id, uid):
        st = self.state(uid)
        if st.busy:
            await self.client.send_message(chat_id, "⚠️ A process is already running. Press Cancel first."); return
        if not st.path or not st.path.exists():
            await self.client.send_message(chat_id, "❌ No current file."); return
        st.cancel_event = asyncio.Event(); st.task = asyncio.current_task()
        status = await self.client.send_message(chat_id, "📤 Uploading to Telegram using MTProto...", buttons=cancel_menu())
        reporter = await self.progress_message(status)
        try:
            async def cb(cur, total):
                if st.cancel_event.is_set(): raise asyncio.CancelledError
                await reporter.update(int(cur), int(total or st.path.stat().st_size), "📤 Uploading to Telegram (MTProto)")
            await self.client.send_file(
                chat_id,
                str(st.path),
                caption=st.path.name,
                force_document=True,
                progress_callback=cb,
            )
            await reporter.finish(st.path.stat().st_size, "✅ Telegram upload complete")
        except asyncio.CancelledError:
            await self.safe_edit(status, "❌ Process cancelled.")
        except Exception as exc:
            log.exception("Telegram MTProto upload failed")
            await self.safe_edit(status, f"❌ Telegram MTProto upload failed:\n{type(exc).__name__}: {exc}\n\nTry /upload gofile for a share link.")
        finally:
            self._clear_busy(uid)

    async def execute(self, chat_id, uid, label, func, upload=False):
        st = self.state(uid)
        if st.busy:
            await self.client.send_message(chat_id, "⚠️ A process is already running. Press Cancel first."); return
        st.cancel_event = asyncio.Event(); st.task = asyncio.current_task()
        status = await self.client.send_message(chat_id, label + "...", buttons=cancel_menu())
        try:
            async with self.semaphore:
                out = await asyncio.to_thread(func)
            if st.cancel_event.is_set(): raise asyncio.CancelledError
            st.path = Path(out)
            st.original_name = st.path.name
            st.outputs = [st.path]
            st.streams = ffprobe.probe(st.path).get("streams", [])
            await self.safe_edit(status, "✅ Processing complete")
            if upload: await self.choose_upload(chat_id, uid)
        except asyncio.CancelledError:
            await self.safe_edit(status, "❌ Process cancelled.")
        except Exception as exc:
            log.exception("media job failed")
            await self.safe_edit(status, f"❌ {label} failed:\n{type(exc).__name__}: {exc}")
        finally:
            self._clear_busy(uid)

    async def run_media_job(self, chat_id, uid, operation):
        st = self.state(uid)
        if not st.path: await self.client.send_message(chat_id, "❌ Send a media file first."); return
        suffix = {"mp4": ".mp4", "toaudio": ".mp3"}.get(operation, ".mkv")
        out = unique_path(self.cfg.work_dir / str(uid), f"{st.path.stem}.{operation}{suffix}")
        fn = {
            "remove_audio": lambda: ffmpeg.remove_audio(st.path, out),
            "optimize": lambda: ffmpeg.optimize(st.path, out),
            "mp4": lambda: ffmpeg.convert_video(st.path, out, "mp4"),
            "mkv": lambda: ffmpeg.convert_video(st.path, out, "mkv"),
            "toaudio": lambda: ffmpeg.video_to_audio(st.path, out),
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
        ext = st.path.suffix or ".mkv"
        out = unique_path(self.cfg.work_dir / str(uid), f"{st.path.stem}.trim{ext}")
        await self.execute(chat_id, uid, "✂️ Trimming audio" if audio else "✂️ Trimming video", lambda: ffmpeg.trim(st.path, out, parts[0], end), upload=True)

    async def run_manual_shot(self, chat_id, uid, text):
        st = self.state(uid); st.pending = None
        if not st.path: return
        out = unique_path(self.cfg.work_dir / str(uid), f"{st.path.stem}.manual.jpg")
        try:
            await asyncio.to_thread(ffmpeg.manual_shot, st.path, out, text)
            await self.client.send_file(chat_id, str(out), caption=f"🖼️ {text}")
        except Exception as exc:
            await self.client.send_message(chat_id, f"❌ Screenshot failed: {exc}")

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
        if not st.path: return
        try: seconds = int(text or "30")
        except ValueError: seconds = 30
        out = unique_path(self.cfg.work_dir / str(uid), f"{st.path.stem}.sample.mp4")
        await self.execute(chat_id, uid, "🎥 Generating sample", lambda: ffmpeg.sample(st.path, out, seconds), upload=True)

    async def run_shots(self, chat_id, uid):
        st = self.state(uid)
        if not st.path: await self.client.send_message(chat_id, "❌ Send a video first."); return
        try:
            shots = await asyncio.to_thread(ffmpeg.screenshots, st.path, self.cfg.work_dir / str(uid) / "shots", 5)
            await self.client.send_file(chat_id, [str(p) for p in shots], caption="🖼️ Screenshots")
        except Exception as exc:
            await self.client.send_message(chat_id, f"❌ Screenshot generation failed: {exc}")

    async def cancel(self, uid, chat_id):
        st = self.state(uid)
        st.pending = None
        if st.busy and st.task is not asyncio.current_task():
            st.cancel_event.set()
            st.task.cancel()
            return
        await self.client.send_message(chat_id, "❌ No active process.")

    async def run(self):
        await self.client.start(bot_token=self.cfg.bot_token)
        me = await self.client.get_me()
        log.info("Bot started via Telegram MTProto: @%s id=%s", me.username, me.id)
        log.info("Transport: MTProto only; HTTP Bot API/Local Bot API is NOT used")
        log.info("FFmpeg=%s FFprobe=%s cryptg=%s", shutil.which("ffmpeg"), shutil.which("ffprobe"), self._cryptg_status())
        await self.client.run_until_disconnected()

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
