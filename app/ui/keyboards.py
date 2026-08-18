from __future__ import annotations

from telethon import Button


def main_menu():
    return [
        [Button.inline("🖼️ Thumbnail Downloader", b"menu:thumb")],
        [Button.inline("🔗 Make Direct/Stream Link", b"menu:direct")],
        [Button.inline("Extract Archive", b"menu:archive"), Button.inline("🔗 Url Uploader", b"menu:urlupload")],
        [Button.inline("🔗 Link Short & Unshort", b"menu:links")],
        [Button.inline("🎵 Audio", b"menu:audio"), Button.inline("🎥 Video", b"menu:video")],
        [Button.inline("Cancel", b"cancel")],
    ]


def video_menu():
    return [
        [Button.inline("Media Information", b"video:info")],
        [Button.inline("🎵 Stream Remover", b"video:streams"), Button.inline("🎵 Stream Extractor", b"video:extract")],
        [Button.inline("✂️ Video Trimmer", b"video:trim"), Button.inline("📕 Remove Audio", b"video:remove_audio")],
        [Button.inline("🎥 Video Optimize", b"video:optimize"), Button.inline("🎥 Videos Splitter", b"video:split")],
        [Button.inline("🔀 Merge Tracks", b"video:merge")],
        [Button.inline("🖼️ Screenshots", b"video:shots"), Button.inline("🖼️ Manual Shots", b"video:manual")],
        [Button.inline("🎥 Generate Sample", b"video:sample"), Button.inline("🎵 Video To Audio", b"video:toaudio")],
        [Button.inline("🎥 Video To MP4", b"video:mp4"), Button.inline("🎥 Video To MKV", b"video:mkv")],
        [Button.inline("Cancel ❌", b"cancel")],
    ]


def audio_menu():
    return [
        [Button.inline("🎵 Audio Converter", b"audio:convert")],
        [Button.inline("🎶 8D Converter", b"audio:8d"), Button.inline("🎼 Music Equalizer", b"audio:eq")],
        [Button.inline("🔊 Bass Booster", b"audio:bass"), Button.inline("🔊 Treble Booster", b"audio:treble")],
        [Button.inline("✂️ Audio Trimmer", b"audio:trim"), Button.inline("🗡️ Auto Trimmer", b"audio:auto")],
        [Button.inline("🎵 Speed Change", b"audio:speed"), Button.inline("🔊 Volume Change", b"audio:volume")],
        [Button.inline("Media Information", b"audio:info"), Button.inline("🔊 Compress Audio", b"audio:compress")],
        [Button.inline("Cancel ❌", b"cancel")],
    ]


def upload_menu():
    return [[Button.inline("Telegram (MTProto)", b"upload:telegram")], [Button.inline("GoFile", b"upload:gofile")], [Button.inline("Cancel", b"cancel")]]


def cancel_menu():
    return [[Button.inline("Cancel Process", b"cancel")]]


def merge_menu():
    return [
        [Button.inline("➕ Add More Files", b"merge:add")],
        [Button.inline("✅ Finish Merge", b"merge:finish")],
        [Button.inline("❌ Cancel Merge", b"merge:cancel")],
    ]
