from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def ik(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows])


def main_menu():
    return ik([
        [("🖼️ Thumbnail Downloader", "main:thumb")],
        [("🔗 Make Direct/Stream Link", "main:link")],
        [("Extract Archive", "main:extract"), ("🔗 Url Uploader", "main:urlupload")],
        [("🔗 Link Short & Unshort", "main:short")],
        [("🎵 Audio", "menu:audio"), ("🎥 Video", "menu:video")],
        [("Cancel", "job:cancel")],
    ])


def video_menu():
    return ik([
        [("Media Information", "video:info")],
        [("🎵 Stream Remover", "video:stream_remove"), ("🎵 Stream Extractor", "video:stream_extract")],
        [("✂️ Video Trimmer", "video:trim"), ("🔇 Remove Audio", "video:remove_audio")],
        [("🎥 Video Optimize", "video:optimize"), ("🎥 Videos Splitter", "video:split")],
        [("🖼️ Screenshots", "video:screenshots"), ("🖼️ Manual Shots", "video:manual_shot")],
        [("🎥 Generate Sample", "video:sample"), ("🎵 Video To Audio", "video:to_audio")],
        [("🎥 Video To MP4", "video:mp4"), ("🎥 Video To MKV", "video:mkv")],
        [("Cancel ❌", "job:cancel")],
    ])


def audio_menu():
    return ik([
        [("🎵 Audio Converter", "audio:convert")],
        [("♬ 8D Converter", "audio:8d"), ("♬ Music Equalizer", "audio:eq")],
        [("🔊 Bass Booster", "audio:bass"), ("🔊 Treble Booster", "audio:treble")],
        [("✂️ Audio Trimmer", "audio:trim"), ("🔪 Auto Trimmer", "audio:auto")],
        [("🎵 Speed Change", "audio:speed"), ("🔊 Volume Change", "audio:volume")],
        [("Media Information", "audio:info"), ("🔊 Compress Audio", "audio:compress")],
        [("Cancel ❌", "job:cancel")],
    ])


def destination_menu(current: str):
    return ik([
        [(f"{'✅ ' if current == 'telegram' else ''}Upload to Telegram", "dest:telegram")],
        [(f"{'✅ ' if current == 'gofile' else ''}Upload to GoFile", "dest:gofile")],
        [("Cancel", "job:cancel")],
    ])


def action_menu():
    return ik([
        [("🖼️ Thumbnail Downloader", "main:thumb")],
        [("🔗 Make Direct/Stream Link", "main:link")],
        [("Extract Archive", "main:extract"), ("🔗 Url Uploader", "main:urlupload")],
        [("🔗 Link Short & Unshort", "main:short")],
        [("🎵 Audio", "menu:audio"), ("🎥 Video", "menu:video")],
        [("Cancel", "job:cancel")],
    ])


def stream_menu(streams: list[dict], selected: set[int] | None = None):
    selected = selected or set()
    rows = []
    for s in streams:
        idx = s.get("index")
        typ = (s.get("codec_type") or "?").capitalize()
        lang = (s.get("tags") or {}).get("language", "und")
        codec = s.get("codec_name", "unknown")
        mark = "❌ " if idx in selected else ""
        rows.append([(f"{mark}{idx} - {typ} - {lang} - {codec}", f"stream:toggle:{idx}")])
    rows += [
        [("All Audios", "stream:all_audio"), ("All Subtitles", "stream:all_sub")],
        [("Custom Streams", "stream:custom"), ("All Streams", "stream:all")],
        [("Cancel Process", "job:cancel")],
    ]
    return ik(rows)
