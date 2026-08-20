from __future__ import annotations

from telethon import Button


def main_menu():
    return [
        [Button.inline("🖼️ Thumbnail Downloader", b"menu:thumb")],
        [Button.inline("🔗 Make Direct/Stream Link", b"menu:direct")],
        [Button.inline("📤 URL Uploader", b"menu:urlupload")],
        [Button.inline("📦 Extract Archive", b"menu:archive")],
        [Button.inline("🎵 Audio", b"menu:audio"), Button.inline("🎥 Video", b"menu:video")],
        [Button.inline("📤 Upload", b"menu:upload")],
        [Button.inline("Cancel ❌", b"cancel")],
    ]


def video_menu():
    return [
        [Button.inline("📋 Media Information", b"video:info")],
        [Button.inline("🎵 Stream Remover", b"video:streams"), Button.inline("🎵 Stream Extractor", b"video:extract")],
        [Button.inline("✂️ Video Trimmer", b"video:trim"), Button.inline("📕 Remove Audio", b"video:remove_audio")],
        [Button.inline("🎥 Video Optimize", b"video:optimize"), Button.inline("🎥 Videos Splitter", b"video:split")],
        [Button.inline("🔀 Merge Tracks", b"video:merge")],
        [Button.inline("🖼️ Screenshots", b"video:shots"), Button.inline("🖼️ Manual Shots", b"video:manual")],
        [Button.inline("🎥 Generate Sample", b"video:sample"), Button.inline("🎵 Video To Audio", b"video:toaudio")],
        [Button.inline("🎥 Video To MP4", b"video:mp4"), Button.inline("🎥 Video To MKV", b"video:mkv")],
        [Button.inline("⬅️ Back", b"video:back"), Button.inline("Cancel ❌", b"cancel")],
    ]


def audio_menu():
    return [
        [Button.inline("🎵 Audio Converter", b"audio:convert")],
        [Button.inline("🎶 8D Converter", b"audio:8d"), Button.inline("🎼 Music Equalizer", b"audio:eq")],
        [Button.inline("🔊 Bass Booster", b"audio:bass"), Button.inline("🔊 Treble Booster", b"audio:treble")],
        [Button.inline("✂️ Audio Trimmer", b"audio:trim"), Button.inline("🗡️ Auto Trimmer", b"audio:auto")],
        [Button.inline("🎵 Speed Change", b"audio:speed"), Button.inline("🔊 Volume Change", b"audio:volume")],
        [Button.inline("📋 Media Information", b"audio:info"), Button.inline("🔊 Compress Audio", b"audio:compress")],
        [Button.inline("⬅️ Back", b"audio:back"), Button.inline("Cancel ❌", b"cancel")],
    ]


def upload_menu():
    return [
        [Button.inline("📤 Telegram (MTProto)", b"upload:telegram"), Button.inline("☁️ GoFile", b"upload:gofile")],
        [Button.inline("⬅️ Back", b"upload:back"), Button.inline("Cancel", b"cancel")],
    ]


def rename_menu():
    return [
        [Button.inline("✏️ Rename", b"rename:yes"), Button.inline("⏭️ Skip", b"rename:skip")],
        [Button.inline("Cancel", b"cancel")],
    ]


def archive_mode_menu():
    return [
        [Button.inline("📦 Normal Extract", b"archive:normal")],
        [Button.inline("🧩 Multi-Part Extract", b"archive:multi")],
        [Button.inline("Cancel", b"cancel")],
    ]


def archive_collect_menu():
    return [
        [Button.inline("🔓 Extract Parts", b"archive:finish")],
        [Button.inline("❌ Cancel", b"cancel")],
    ]


def archive_password_menu():
    return [[Button.inline("🔓 No Password", b"archive:nopass"), Button.inline("Cancel", b"cancel")]]


def settings_menu(rename_enabled: bool = False, upload_mode: str = "choose", telegram_mode: str = "document", has_thumbnail: bool = False):
    rename_yes = "✏️ Rename: ON ✅" if rename_enabled else "✏️ Rename: ON"
    rename_no = "✏️ Rename: OFF ✅" if not rename_enabled else "✏️ Rename: OFF"
    telegram = "📤 Upload: Telegram ✅" if upload_mode == "telegram" else "📤 Upload: Telegram"
    gofile = "☁️ Upload: GoFile ✅" if upload_mode == "gofile" else "☁️ Upload: GoFile"
    choose = "❓ Upload: Choose ✅" if upload_mode == "choose" else "❓ Upload: Choose"
    doc = "📄 Telegram as Document ✅" if telegram_mode == "document" else "📄 Telegram as Document"
    media = "🎬 Telegram as Media ✅" if telegram_mode == "media" else "🎬 Telegram as Media"
    thumb = "🖼️ Custom Thumbnail: Set" if has_thumbnail else "🖼️ Set Custom Thumbnail"
    return [
        [Button.inline(rename_yes, b"settings:rename:on"), Button.inline(rename_no, b"settings:rename:off")],
        [Button.inline(telegram, b"settings:upload:telegram")],
        [Button.inline(gofile, b"settings:upload:gofile")],
        [Button.inline(choose, b"settings:upload:choose")],
        [Button.inline(doc, b"settings:telegram_mode:document"), Button.inline(media, b"settings:telegram_mode:media")],
        [Button.inline(thumb, b"settings:thumbnail:set")],
        [Button.inline("🗑️ Remove Custom Thumbnail", b"settings:thumbnail:remove")],
        [Button.inline("⬅️ Back", b"start:home")],
    ]


def admin_menu():
    return [
        [Button.inline("👥 Users", b"admin:users")],
        [Button.inline("🧭 Ongoing Processes", b"admin:processes")],
        [Button.inline("🛑 Cancel User Job", b"admin:cancel_user")],
        [Button.inline("📢 Broadcast", b"admin:broadcast")],
        [Button.inline("⬅️ Back", b"start:home")],
    ]


def cancel_menu():
    return [[Button.inline("Cancel Process", b"cancel")]]


def merge_menu():
    return [
        [Button.inline("✅ Finish Merge", b"merge:finish")],
        [Button.inline("❌ Cancel Merge", b"merge:cancel")],
        [Button.inline("⬅️ Back", b"merge:back")],
    ]
