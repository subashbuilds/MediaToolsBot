from __future__ import annotations

import asyncio
import contextlib
import json
import mimetypes
import shutil
import time
from pathlib import Path
from urllib.parse import urlparse

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, Message

from ..config import Config
from ..db import Database
from ..job import Job
from ..keyboards import action_menu, audio_menu, destination_menu, main_menu, stream_menu, video_menu
from ..services.download import download_url
from ..services.gofile import upload as gofile_upload
from ..services.media import (
    archive_extract, audio_convert, audio_filter, audio_info, audio_trim, convert_video,
    extract_stream, generate_sample, media_info, optimize_video, remove_audio,
    remux_remove_streams, screenshot, screenshots, split_video, trim, video_to_audio,
)
from ..services.shortener import shorten, unshorten
from ..services.telegram_files import download_telegram, upload_telegram
from ..services.thumbnail import thumbnail_from_media, thumbnail_from_url
from ..utils.files import human_size, safe_filename
from ..utils.progress import render
from ..services.process import ProcessCancelled, stream_summary

router = Router(name="bot")


class InputState(StatesGroup):
    waiting = State()


class SessionManager:
    def __init__(self, config: Config):
        self.config = config
        self.jobs: dict[int, Job] = {}
        self.tasks: dict[int, asyncio.Task] = {}
        self.lock = asyncio.Lock()

    async def new(self, user_id: int, chat_id: int) -> Job:
        async with self.lock:
            old = self.jobs.get(user_id)
            if old:
                old.cancel.set()
                old.cleanup()
            root = self.config.work_dir / str(user_id) / str(int(time.time() * 1000))
            root.mkdir(parents=True, exist_ok=True)
            job = Job(user_id, chat_id, root)
            self.jobs[user_id] = job
            return job

    def get(self, user_id: int) -> Job | None:
        return self.jobs.get(user_id)

    async def clear(self, user_id: int):
        job = self.jobs.pop(user_id, None)
        task = self.tasks.pop(user_id, None)
        if task and not task.done():
            task.cancel()
        if job and not self.config.keep_files:
            job.cleanup()

    async def cancel(self, user_id: int):
        job = self.jobs.get(user_id)
        if job:
            job.cancel.set()
        task = self.tasks.get(user_id)
        if task and not task.done():
            task.cancel()


SESSIONS: SessionManager | None = None
DB: Database | None = None
CFG: Config | None = None


def init(config: Config, db: Database, sessions: SessionManager):
    global CFG, DB, SESSIONS
    CFG, DB, SESSIONS = config, db, sessions


def user_id(message: Message | CallbackQuery) -> int:
    return message.from_user.id


def current_job(uid: int) -> Job:
    job = SESSIONS.get(uid) if SESSIONS else None
    if not job or not job.source:
        raise RuntimeError("No media file is active. Send a file or URL first.")
    return job


async def show_actions(message: Message, job: Job, intro: str | None = None):
    info = job.info or {}
    fmt = info.get("format", {})
    size = human_size(int(fmt.get("size", 0) or 0))
    name = job.source.name if job.source else "file"
    text = f"{intro + chr(10) + chr(10) if intro else ''}📁 <b>{name}</b>\nSize: <b>{size}</b>\n\nPlease select your preferred action below 👇"
    await message.answer(text, reply_markup=action_menu(), parse_mode="HTML")


async def progress_message(message: Message, text: str):
    try:
        await message.edit_text(text)
    except Exception:
        pass


async def run_and_send(message: Message, job: Job, operation, output: Path, label: str):
    status = await message.answer(f"⏳ {label}...\n\nCancel with /cancel")
    try:
        code, out, err = await operation()
        if code != 0 or not output.exists():
            raise RuntimeError(err[-3000:] or f"{label} failed")
        await status.edit_text(f"✅ {label} completed.\n📦 {human_size(output.stat().st_size)}")
        await send_result(message, output)
    except ProcessCancelled:
        await status.edit_text("❌ Process cancelled.")
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            await status.edit_text("❌ Process cancelled.")
    except Exception as exc:
        with contextlib.suppress(Exception):
            await status.edit_text(f"❌ {type(exc).__name__}: {str(exc)[:3000]}")


async def send_result(message: Message, path: Path):
    assert DB and CFG
    pref = DB.get(message.from_user.id)
    dest = pref["destination"]
    token = pref["gofile_token"] or CFG.gofile_api_token
    active_job = SESSIONS.get(message.from_user.id) if SESSIONS else None
    cancel_event = active_job.cancel if active_job else None
    if dest == "gofile":
        status = await message.answer("☁️ Uploading to GoFile...")
        try:
            async def upload_progress(done, total, started):
                if done == total or done % (64 * 1024 * 1024) < (8 * 1024 * 1024):
                    await progress_message(status, render("☁️ Uploading to GoFile", done, total, started))
            data = await gofile_upload(path, token=token, region=CFG.gofile_region, progress_cb=upload_progress, cancel_event=cancel_event)
            link = data.get("downloadPage") or data.get("downloadpage") or data.get("link") or data.get("directLink")
            if not link:
                link = json.dumps(data, ensure_ascii=False)[:3500]
            await status.edit_text(f"✅ Uploaded to GoFile\n\n🔗 {link}")
        except Exception as exc:
            await status.edit_text(f"❌ GoFile upload failed: {str(exc)[:3000]}")
    else:
        status = await message.answer("☁️ Uploading to Telegram...")
        try:
            await upload_telegram(message.bot, message.chat.id, path)
            await status.delete()
        except Exception as exc:
            await status.edit_text(f"❌ Telegram upload failed.\n\n{str(exc)[:3000]}\n\nFor large files, enable the Local Bot API Server.")


async def ensure_source(message: Message) -> Job:
    assert SESSIONS
    uid = message.from_user.id
    job = SESSIONS.get(uid)
    if job and job.source:
        return job
    job = await SESSIONS.new(uid, message.chat.id)
    if message.document:
        name = safe_filename(message.document.file_name or "input.bin")
        job.source = await download_telegram(message.bot, message.document.file_id, job.root / name, job.cancel)
    elif message.video:
        name = safe_filename(message.video.file_name or "video.mp4")
        job.source = await download_telegram(message.bot, message.video.file_id, job.root / name, job.cancel)
    elif message.audio:
        name = safe_filename(message.audio.file_name or "audio.mp3")
        job.source = await download_telegram(message.bot, message.audio.file_id, job.root / name, job.cancel)
    elif message.text and message.text.strip().startswith(("http://", "https://")):
        job.source = await download_url(message.text.strip(), job.root, cancel_event=job.cancel)
    else:
        raise RuntimeError("Send a media/document file or an HTTP(S) URL.")
    lower = job.source.name.lower()
    archive = lower.endswith((".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".tar.gz", ".tar.bz2", ".tar.xz"))
    job.info = {} if archive else await media_info(job.source)
    return job


@router.message(CommandStart())
async def start(message: Message):
    await message.answer("<b>Video Converter Bot</b>\n\nSend a media file or URL, or choose an action below.", reply_markup=main_menu(), parse_mode="HTML")


@router.message(Command("help"))
async def help_cmd(message: Message):
    await message.answer("""<b>Commands</b>\n\n/upload — choose Telegram or GoFile output\n/upload telegram — Telegram output\n/upload gofile — GoFile output\n/setgofile TOKEN — save your GoFile API token\n/cleargofile — remove your saved token (guest uploads)\n/settings — show current upload destination\n/cancel — cancel current job\n\nSend a file or HTTP(S) URL to begin.""", parse_mode="HTML")


@router.message(Command("settings"))
async def settings(message: Message):
    assert DB and CFG
    p = DB.get(message.from_user.id)
    token = p["gofile_token"] or CFG.gofile_api_token
    token_state = "authenticated token configured" if token else "guest uploads"
    await message.answer(f"Upload destination: <b>{p['destination']}</b>\nGoFile: <b>{token_state}</b>", reply_markup=destination_menu(p["destination"]), parse_mode="HTML")


@router.message(Command("upload"))
async def upload_cmd(message: Message):
    assert DB
    args = (message.text or "").split(maxsplit=1)
    if len(args) == 2 and args[1].lower() in {"telegram", "gofile"}:
        DB.set_destination(message.from_user.id, args[1].lower())
        await message.answer(f"✅ Upload destination set to <b>{args[1].lower()}</b>.", reply_markup=main_menu(), parse_mode="HTML")
        return
    p = DB.get(message.from_user.id)
    await message.answer("Select your preferred upload destination:", reply_markup=destination_menu(p["destination"]))


@router.message(Command("setgofile"))
async def set_gofile(message: Message):
    assert DB
    args = (message.text or "").split(maxsplit=1)
    if len(args) != 2 or not args[1].strip():
        await message.answer("Usage: /setgofile YOUR_GOFILE_API_TOKEN")
        return
    DB.set_gofile(message.from_user.id, args[1].strip())
    await message.answer("✅ GoFile API token saved for your account.")


@router.message(Command("cleargofile"))
async def clear_gofile(message: Message):
    assert DB
    DB.set_gofile(message.from_user.id, None)
    await message.answer("✅ Personal GoFile token removed. GoFile uploads will use the configured bot token, or guest mode if none is configured.")


@router.message(Command("cancel"))
async def cancel_cmd(message: Message):
    assert SESSIONS
    await SESSIONS.cancel(message.from_user.id)
    await message.answer("❌ Cancellation requested.")


@router.callback_query(F.data.startswith("dest:"))
async def destination_callback(query: CallbackQuery):
    assert DB
    dest = query.data.split(":", 1)[1]
    DB.set_destination(query.from_user.id, dest)
    await query.answer("Destination updated")
    if query.message:
        await query.message.edit_text(f"✅ Upload destination: <b>{dest}</b>", parse_mode="HTML", reply_markup=main_menu())


@router.callback_query(F.data == "job:cancel")
async def cancel_callback(query: CallbackQuery):
    assert SESSIONS
    await SESSIONS.cancel(query.from_user.id)
    await query.answer("Cancellation requested")
    if query.message:
        await query.message.edit_text("❌ Process cancelled.", reply_markup=main_menu())


@router.callback_query(F.data == "menu:video")
async def video_menu_cb(query: CallbackQuery):
    await query.answer()
    if query.message:
        await query.message.edit_text("🎥 <b>Video</b>\n\nSelect your preferred action:", reply_markup=video_menu(), parse_mode="HTML")


@router.callback_query(F.data == "menu:audio")
async def audio_menu_cb(query: CallbackQuery):
    await query.answer()
    if query.message:
        await query.message.edit_text("🎵 <b>Audio</b>\n\nSelect your preferred action:", reply_markup=audio_menu(), parse_mode="HTML")


@router.callback_query(F.data.startswith("main:"))
async def main_action(query: CallbackQuery, state: FSMContext):
    await query.answer()
    action = query.data.split(":", 1)[1]
    if action == "link":
        try:
            job = current_job(query.from_user.id)
            token = (DB.get(query.from_user.id)["gofile_token"] if DB else None) or (CFG.gofile_api_token if CFG else None)
            status = await query.message.answer("☁️ Creating direct/stream link...")
            data = await gofile_upload(job.source, token=token, region=CFG.gofile_region if CFG else "auto", cancel_event=job.cancel)
            link = data.get("downloadPage") or data.get("downloadpage") or data.get("link") or data.get("directLink")
            await status.edit_text(f"🔗 {link or json.dumps(data)[:3500]}")
        except Exception as e:
            await query.message.answer(str(e))
        return
    prompts = {
        "thumb": "Send a media URL or file for thumbnail extraction.",
        "extract": "Send a ZIP/RAR/7Z/TAR archive.",
        "urlupload": "Send an HTTP(S) URL to download and upload.",
        "short": "Send a URL. Prefix with <code>unshort:</code> to expand it, otherwise it will be shortened.",
    }
    if action in {"short", "thumb", "extract", "urlupload"}:
        await state.set_state(InputState.waiting)
        await state.update_data(op=action)
    await query.message.answer(prompts.get(action, "Send the required input."), parse_mode="HTML")


@router.callback_query(F.data.startswith("video:"))
async def video_action(query: CallbackQuery, state: FSMContext):
    await query.answer()
    op = query.data.split(":", 1)[1]
    try:
        job = current_job(query.from_user.id)
    except Exception as exc:
        await query.message.answer(str(exc)); return
    if op == "info":
        await query.message.answer(format_info(job.info or {}), parse_mode="HTML")
    elif op == "stream_remove":
        await query.message.answer("Select streams to remove. Checked streams are marked ❌.", reply_markup=stream_menu(job.info.get("streams", []), set()))
        await state.update_data(op="stream_remove", selected=[])
    elif op == "stream_extract":
        await query.message.answer("Select a stream by pressing its stream button, then use <b>Custom Streams</b>.", reply_markup=stream_menu(job.info.get("streams", []), set()), parse_mode="HTML")
        await state.update_data(op="stream_extract", selected=[])
    else:
        prompts = {
            "trim": "Send trim values as: <code>START DURATION</code> (example: 00:02:00 00:00:30).",
            "optimize": "Send CRF and preset as: <code>22 medium</code>.",
            "split": "Send segment length in seconds (example: <code>600</code>).",
            "manual_shot": "Send screenshot timestamp (example: <code>00:10:30</code>).",
            "sample": "Send sample duration in seconds (example: <code>60</code>).",
        }
        await state.set_state(InputState.waiting); await state.update_data(op=op)
        await query.message.answer(prompts.get(op, "Send the requested value."), parse_mode="HTML")
    if op == "remove_audio":
        out = job.root / f"{job.source.stem}_no_audio{job.source.suffix or '.mkv'}"
        await run_and_send(query.message, job, lambda: remove_audio(job.source, out, job.cancel), out, "Removing audio")
    elif op == "mp4":
        out = job.root / (f"{job.source.stem}_converted.mp4" if job.source.suffix.lower() == ".mp4" else f"{job.source.stem}.mp4"); await run_and_send(query.message, job, lambda: convert_video(job.source, out, "mp4", job.cancel), out, "Converting to MP4")
    elif op == "mkv":
        out = job.root / (f"{job.source.stem}_converted.mkv" if job.source.suffix.lower() == ".mkv" else f"{job.source.stem}.mkv"); await run_and_send(query.message, job, lambda: convert_video(job.source, out, "mkv", job.cancel), out, "Converting to MKV")
    elif op == "screenshots":
        await state.set_state(InputState.waiting); await state.update_data(op="screenshots")
        await query.message.answer("How many screenshots? Example: <code>5</code>", parse_mode="HTML")
    elif op == "to_audio":
        await state.set_state(InputState.waiting); await state.update_data(op="to_audio")
        await query.message.answer("Audio codec? Use <code>mp3</code>, <code>aac</code>, or <code>flac</code>.")
    

@router.callback_query(F.data.startswith("audio:"))
async def audio_action(query: CallbackQuery, state: FSMContext):
    await query.answer()
    op = query.data.split(":", 1)[1]
    try: job = current_job(query.from_user.id)
    except Exception as exc: await query.message.answer(str(exc)); return
    if op == "info":
        await query.message.answer(format_info(job.info or {}), parse_mode="HTML"); return
    await state.set_state(InputState.waiting); await state.update_data(op=op)
    prompts = {
        "convert": "Send output codec/format: <code>mp3 192k</code>, <code>aac 192k</code>, or <code>flac 0</code>.",
        "8d": "Send 8D intensity as a percentage (example: <code>100</code>).",
        "eq": "Send equalizer bands as <code>low mid high</code> dB (example: <code>3 0 2</code>).",
        "bass": "Send bass gain in dB (example: <code>6</code>).",
        "treble": "Send treble gain in dB (example: <code>5</code>).",
        "trim": "Send <code>START DURATION</code>.",
        "auto": "Send silence threshold and minimum duration: <code>-35dB 0.5</code>.",
        "speed": "Send speed between 0.5 and 2.0 (example: <code>1.25</code>).",
        "volume": "Send volume multiplier (example: <code>1.5</code>).",
        "compress": "Send bitrate (example: <code>128k</code>).",
    }
    await query.message.answer(prompts.get(op, "Send the requested value."), parse_mode="HTML")


@router.callback_query(F.data.startswith("stream:"))
async def stream_callback(query: CallbackQuery, state: FSMContext):
    await query.answer()
    data = await state.get_data()
    job = current_job(query.from_user.id)
    streams = job.info.get("streams", [])
    action = data.get("op")
    if query.data.startswith("stream:toggle:"):
        idx = int(query.data.rsplit(":", 1)[1]); selected = set(data.get("selected", []))
        if action == "stream_extract":
            stream = next((s for s in streams if s.get("index") == idx), None)
            if not stream:
                await query.message.answer("Stream not found."); return
            suffix = ".mka" if stream.get("codec_type") == "audio" else ".mks" if stream.get("codec_type") == "subtitle" else ".mkv"
            out = job.root / f"{job.source.stem}_stream_{idx}{suffix}"
            await state.clear()
            await run_and_send(query.message, job, lambda: extract_stream(job.source, idx, out, job.cancel), out, f"Extracting stream {idx}")
            return
        if idx in selected: selected.remove(idx)
        else: selected.add(idx)
        await state.update_data(selected=list(selected))
        await query.message.edit_reply_markup(reply_markup=stream_menu(streams, selected)); return
    if query.data == "stream:all":
        await state.clear()
        # Match the screenshot behavior: keep the default stream of each media type.
        keep = []
        for typ in {s.get("codec_type") for s in streams}:
            candidates = [s for s in streams if s.get("codec_type") == typ]
            default = next((s for s in candidates if s.get("disposition", {}).get("default")), candidates[0] if candidates else None)
            if default: keep.append(default["index"])
        out = job.root / f"{job.source.stem}_default_streams{job.source.suffix or '.mkv'}"
        await run_and_send(query.message, job, lambda: remux_remove_streams(job.source, keep, out, job.cancel), out, "Keeping default streams")
    elif query.data == "stream:all_audio":
        await state.clear(); keep = [s["index"] for s in streams if s.get("codec_type") != "audio"] + [s["index"] for s in streams if s.get("codec_type") == "audio" and s.get("disposition", {}).get("default")]
        out = job.root / f"{job.source.stem}_audio_default{job.source.suffix or '.mkv'}"; await run_and_send(query.message, job, lambda: remux_remove_streams(job.source, keep, out, job.cancel), out, "Keeping default audio")
    elif query.data == "stream:all_sub":
        await state.clear(); keep = [s["index"] for s in streams if s.get("codec_type") != "subtitle"] + [s["index"] for s in streams if s.get("codec_type") == "subtitle" and s.get("disposition", {}).get("default")]
        out = job.root / f"{job.source.stem}_default_sub{job.source.suffix or '.mkv'}"; await run_and_send(query.message, job, lambda: remux_remove_streams(job.source, keep, out, job.cancel), out, "Keeping default subtitle")
    elif query.data == "stream:custom":
        selected = set(data.get("selected", []))
        if not selected:
            await query.message.answer("Select at least one stream first."); return
        await state.clear()
        if action == "stream_extract":
            idx = next(iter(selected)); stream = next((s for s in streams if s["index"] == idx), None)
            suffix = ".mka" if stream and stream.get("codec_type") == "audio" else ".mks" if stream and stream.get("codec_type") == "subtitle" else ".mkv"
            out = job.root / f"{job.source.stem}_stream_{idx}{suffix}"; await run_and_send(query.message, job, lambda: extract_stream(job.source, idx, out, job.cancel), out, f"Extracting stream {idx}")
        else:
            keep = [s["index"] for s in streams if s["index"] not in selected]
            out = job.root / f"{job.source.stem}_streams_removed{job.source.suffix or '.mkv'}"; await run_and_send(query.message, job, lambda: remux_remove_streams(job.source, keep, out, job.cancel), out, "Removing selected streams")


@router.message(InputState.waiting)
async def state_input(message: Message, state: FSMContext):
    data = await state.get_data(); op = data.get("op")
    if op == "short":
        text = (message.text or "").strip(); await state.clear()
        if text.lower().startswith("unshort:"):
            result = await unshorten(text.split(":", 1)[1].strip()); await message.answer(f"🔗 {result}")
        else:
            result = await shorten(text); await message.answer(f"🔗 {result}")
        return
    if op == "thumb" and (message.text or "").strip().startswith(("http://", "https://")):
        try:
            job = await SESSIONS.new(message.from_user.id, message.chat.id)
            out = await thumbnail_from_url(message.text.strip(), job.root, job.cancel)
            await message.answer_photo(FSInputFile(out))
        except Exception as exc:
            await message.answer(f"❌ {type(exc).__name__}: {str(exc)[:2500]}")
        finally:
            await state.clear()
        return
    try:
        job = await ensure_source(message) if op in {"thumb", "extract", "urlupload"} else current_job(message.from_user.id)
    except Exception as exc:
        await message.answer(str(exc)); return
    text = (message.text or "").strip()
    try:
        if op == "urlupload":
            if not (message.text or "").strip().startswith(("http://", "https://")):
                raise ValueError("Send an HTTP(S) URL")
            await SESSIONS.new(message.from_user.id, message.chat.id) if False else None
            newjob = await SESSIONS.new(message.from_user.id, message.chat.id)
            newjob.source = await download_url(message.text.strip(), newjob.root, cancel_event=newjob.cancel)
            try: newjob.info = await media_info(newjob.source)
            except Exception: newjob.info = {}
            await send_result(message, newjob.source)
        elif op == "thumb":
            out = job.root / "thumbnail.jpg"
            await thumbnail_from_media(job.source, out, job.cancel)
            await message.answer_photo(FSInputFile(out))
        elif op == "extract":
            outdir = job.root / "extracted"; status = await message.answer("⏳ Extracting archive...")
            code, _, err = await archive_extract(job.source, outdir, job.cancel)
            if code != 0: raise RuntimeError(err[-3000:] or "Archive extraction failed")
            files = [p for p in outdir.rglob("*") if p.is_file()]
            await status.edit_text(f"✅ Extracted {len(files)} files.")
            for p in files:
                await send_result(message, p)
        elif op == "trim":
            a, b = text.split(maxsplit=1); out=job.root/f"{job.source.stem}_trim{job.source.suffix}"; await run_and_send(message,job,lambda:trim(job.source,a,b,out,job.cancel),out,"Trimming video/audio")
        elif op == "optimize":
            a=text.split(); crf=int(a[0]); preset=a[1] if len(a)>1 else "medium"; out=job.root/f"{job.source.stem}_optimized.mkv"; await run_and_send(message,job,lambda:optimize_video(job.source,out,crf,preset,job.cancel),out,"Optimizing video")
        elif op == "split":
            seconds=int(text); outdir=job.root/"split"; status=await message.answer("⏳ Splitting..."); code,_,err=await split_video(job.source,outdir,seconds,job.cancel); 
            if code: raise RuntimeError(err[-2000:])
            await status.edit_text(f"✅ Split into {len(list(outdir.glob('*')))} files.")
            for p in sorted(outdir.iterdir()): await send_result(message,p)
        elif op == "manual_shot":
            out=job.root/"manual.jpg"; await run_and_send(message,job,lambda:screenshot(job.source,out,text,job.cancel),out,"Taking screenshot")
        elif op == "screenshots":
            n=max(1,min(20,int(text))); outdir=job.root/"shots"; status=await message.answer("⏳ Creating screenshots..."); files=await screenshots(job.source,outdir,n,job.cancel); await status.edit_text(f"✅ Created {len(files)} screenshots.");
            for p in files: await message.answer_photo(FSInputFile(p))
        elif op == "sample":
            sec=max(1,min(600,int(text))); out=job.root/f"{job.source.stem}_sample.mp4"; await run_and_send(message,job,lambda:generate_sample(job.source,out,sec,job.cancel),out,"Generating sample")
        elif op == "to_audio":
            codec=text.lower(); ext={"mp3":"mp3","aac":"m4a","flac":"flac"}.get(codec)
            if not ext: raise ValueError("Codec must be mp3, aac or flac")
            out=job.root/f"{job.source.stem}.{ext}"; await run_and_send(message,job,lambda:video_to_audio(job.source,out,codec,job.cancel),out,"Extracting audio")
        elif op == "convert":
            a=text.split(); codec=a[0].lower(); bitrate=a[1] if len(a)>1 else "192k"; ext={"mp3":"mp3","aac":"m4a","flac":"flac","opus":"opus"}.get(codec,codec); out=job.root/f"{job.source.stem}.{ext}"; await run_and_send(message,job,lambda:audio_convert(job.source,out,codec,bitrate,job.cancel),out,"Converting audio")
        elif op in {"8d","eq","bass","treble","auto","speed","volume"}:
            if op=="8d": expr=f"apulsator=hz=0.09:width={max(0.1,min(2,float(text)/100*2))}:mode=sine"
            elif op=="eq": low,mid,high=map(float,text.split()); expr=f"equalizer=f=100:t=q:w=1:g={low},equalizer=f=1000:t=q:w=1:g={mid},equalizer=f=10000:t=q:w=1:g={high}"
            elif op=="bass": expr=f"bass=g={max(-20,min(20,float(text)))}"
            elif op=="treble": expr=f"treble=g={max(-20,min(20,float(text)))}"
            elif op=="auto": threshold,dur=text.split(); expr=f"silenceremove=stop_periods=-1:stop_duration={float(dur)}:stop_threshold={threshold}"
            elif op=="speed": expr=f"atempo={max(0.5,min(2,float(text)))}"
            else: expr=f"volume={max(0.1,min(4,float(text)))}"
            out=job.root/f"{job.source.stem}_{op}.mp3"; await run_and_send(message,job,lambda:audio_filter(job.source,out,expr,cancel=job.cancel),out,f"Applying {op}")
        elif op == "compress":
            out=job.root/f"{job.source.stem}_compressed.mp3"; await run_and_send(message,job,lambda:audio_convert(job.source,out,"libmp3lame",text,job.cancel),out,"Compressing audio")
        elif op == "thumb":
            out=job.root/"thumbnail.jpg"; await run_and_send(message,job,lambda:thumbnail_from_media(job.source,out,job.cancel),out,"Creating thumbnail")
        else:
            await message.answer("This action requires a different input flow.")
    except Exception as exc:
        await message.answer(f"❌ {type(exc).__name__}: {str(exc)[:2500]}")
    finally:
        await state.clear()


def format_info(info: dict) -> str:
    fmt = info.get("format", {})
    lines = [f"<b>Media Information</b>", f"Format: {fmt.get('format_name','unknown')}", f"Duration: {fmt.get('duration','unknown')} s", f"Size: {human_size(int(fmt.get('size',0) or 0))}", "", "<b>Streams</b>"]
    for s in info.get("streams", []):
        lines.append(stream_summary(s))
    return "\n".join(lines)


@router.message(F.document | F.video | F.audio | F.text)
async def incoming(message: Message, state: FSMContext):
    if message.text and message.text.startswith("/"):
        return
    try:
        job = await ensure_source(message)
        await show_actions(message, job)
    except asyncio.CancelledError:
        await message.answer("❌ Download cancelled.")
    except Exception as exc:
        await message.answer(f"❌ {type(exc).__name__}: {str(exc)[:3000]}")
