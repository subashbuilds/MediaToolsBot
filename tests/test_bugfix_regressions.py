"""Regression tests for the bugs fixed in this pass."""
import asyncio
import subprocess
import time
from pathlib import Path

import pytest

from app.services import ffmpeg, ffprobe
from app.utils.progress import ProgressReporter


def make_video(path: Path, seconds: int = 30, gop: int | None = None):
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=320x180:rate=10", "-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast"]
    if gop:
        args += ["-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0"]
    subprocess.run(args + [str(path)], check=True)
    return path


def make_av(path: Path, audio_codec: str, ext: str = ".mkv"):
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc=size=320x180:rate=10",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
        "-t", "2", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", audio_codec,
        str(path),
    ], check=True)
    return path


def duration_of(path: Path) -> float:
    return float(ffprobe.probe(path)["format"]["duration"])


# ------------------------------------------------------------------- trim bug
def test_trim_honours_both_start_and_end(tmp_path):
    """A 10s..20s request must return ~10s, not the 30s the old code produced."""
    src = make_video(tmp_path / "src.mkv", seconds=30, gop=10)
    out = tmp_path / "trim.mkv"
    ffmpeg.trim(src, out, "00:00:10", "00:00:20")
    assert 9.0 <= duration_of(out) <= 11.0


def test_trim_accepts_plain_seconds(tmp_path):
    src = make_video(tmp_path / "src.mkv", seconds=30, gop=10)
    out = tmp_path / "trim.mkv"
    ffmpeg.trim(src, out, "10", "20")
    assert 9.0 <= duration_of(out) <= 11.0


def test_trim_clamps_end_past_duration(tmp_path):
    src = make_video(tmp_path / "src.mkv", seconds=5, gop=10)
    out = tmp_path / "trim.mkv"
    ffmpeg.trim(src, out, "00:00:01", "00:10:00")
    assert 3.0 <= duration_of(out) <= 5.0


def test_trim_rejects_invalid_and_inverted_bounds(tmp_path):
    src = make_video(tmp_path / "src.mkv", seconds=5)
    with pytest.raises(ValueError):
        ffmpeg.trim(src, tmp_path / "a.mkv", "not-a-time", None)
    with pytest.raises(ValueError):
        ffmpeg.trim(src, tmp_path / "b.mkv", "10", "5")


# ------------------------------------------------------------------ mp4 fix
@pytest.mark.parametrize("codec", ["flac", "libopus", "aac"])
def test_mp4_conversion_handles_non_muxable_audio(tmp_path, codec):
    """-c:a copy failed outright for FLAC/Opus; the muxer refused the header."""
    src = make_av(tmp_path / f"src_{codec}.mkv", codec)
    out = tmp_path / f"out_{codec}.mp4"
    ffmpeg.convert_video(src, out, "mp4")
    assert out.exists() and out.stat().st_size > 0
    assert any(s["codec_type"] == "audio" for s in ffprobe.probe(out)["streams"])


def test_mp4_conversion_picks_a_single_video_track(tmp_path):
    """Multi-angle input previously aborted the whole MP4 conversion."""
    a = tmp_path / "a.mkv"
    b = tmp_path / "b.mkv"
    make_video(a, seconds=2, gop=10)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(a),
                    "-c", "copy", "-map", "0:v", str(b)], check=True)
    out = tmp_path / "merged_in.mkv"
    from app.services.merge import merge_tracks
    merge_tracks([a, b], out)
    assert sum(s["codec_type"] == "video" for s in ffprobe.probe(out)["streams"]) == 2
    mp4 = tmp_path / "out.mp4"
    ffmpeg.convert_video(out, mp4, "mp4")
    assert sum(s["codec_type"] == "video" for s in ffprobe.probe(mp4)["streams"]) == 1


def test_convert_video_rejects_audio_only_input(tmp_path):
    audio = tmp_path / "a.m4a"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "sine=frequency=440", "-t", "1", "-c:a", "aac", str(audio)], check=True)
    with pytest.raises(ValueError):
        ffmpeg.convert_video(audio, tmp_path / "out.mp4", "mp4")


# ------------------------------------------------------------------- timecode
def test_parse_timecode_variants():
    assert ffmpeg.parse_timecode("12") == 12
    assert ffmpeg.parse_timecode("1:30") == 90
    assert ffmpeg.parse_timecode("01:02:03") == 3723
    for bad in ("", "abc", "1:2:3:4", "1:a", "-5"):
        with pytest.raises(ValueError):
            ffmpeg.parse_timecode(bad)


def test_manual_shot_clamps_past_end_of_video(tmp_path):
    """A late timestamp used to leave a 0-byte JPEG that failed to upload."""
    src = make_video(tmp_path / "src.mkv", seconds=2, gop=10)
    out = tmp_path / "shot.jpg"
    ffmpeg.manual_shot(src, out, "00:50:00")
    assert out.exists() and out.stat().st_size > 0


def test_manual_shot_rejects_garbage(tmp_path):
    src = make_video(tmp_path / "src.mkv", seconds=2)
    with pytest.raises(ValueError):
        ffmpeg.manual_shot(src, tmp_path / "shot.jpg", "later please")


def test_screenshots_error_when_frame_is_empty(tmp_path):
    src = make_video(tmp_path / "src.mkv", seconds=2)
    shots = ffmpeg.screenshots(src, tmp_path / "shots", 3)
    assert len(shots) == 3
    assert all(p.stat().st_size > 0 for p in shots)


# ------------------------------------------------------------------ splitting
def test_split_parts_keep_the_source_name(tmp_path):
    src = make_video(tmp_path / "Holiday Clip.mkv", seconds=5, gop=10)
    parts = ffmpeg.split_video(src, tmp_path / "split", 1)
    assert len(parts) >= 2
    assert all(p.name.startswith("Holiday Clip.part") for p in parts)


def test_split_rerun_does_not_accumulate_stale_parts(tmp_path):
    src = make_video(tmp_path / "src.mkv", seconds=5, gop=10)
    outdir = tmp_path / "split"
    first = ffmpeg.split_video(src, outdir, 1)
    second = ffmpeg.split_video(src, outdir, 1)
    assert len(first) == len(second)


# ------------------------------------------------------------------- progress
def test_progress_throttles_unknown_totals():
    """An unknown Content-Length used to edit the message on every chunk."""
    seen = []

    async def cb(text):
        seen.append(text)

    async def run():
        p = ProgressReporter(cb, interval=60)
        for _ in range(200):
            await p.update(1024, 0, "Downloading")

    asyncio.run(run())
    assert len(seen) <= 2, f"expected throttling, got {len(seen)} edits"


def test_progress_queue_progress_is_thread_safe():
    p = ProgressReporter(lambda text: None, interval=0.5)
    for i in range(5):
        p.queue_progress(i, 10)
    current, total = p.take_pending()
    assert current == 4 and total == 10
    assert p.take_pending() == (None, 0)


# -------------------------------------------------------------------- archive
def test_archive_password_detection_uses_correct_precedence(tmp_path):
    import zipfile

    from app.services.archive import archive_requires_password, extract_archive

    plain = tmp_path / "plain.zip"
    with zipfile.ZipFile(plain, "w") as z:
        z.writestr("a.txt", "hello")
    assert archive_requires_password(plain) is False
    assert (extract_archive(plain, tmp_path / "out_plain", None) / "a.txt").read_text() == "hello"

    # A corrupt/non-zip file must not raise out of the detection helper.
    junk = tmp_path / "broken.zip"
    junk.write_bytes(b"not a zip at all")
    assert archive_requires_password(junk) is False


def test_tar_extraction_works(tmp_path):
    import tarfile

    tar = tmp_path / "tree.tar"
    with tarfile.open(tar, "w") as t:
        t.add(tmp_path / "plain.txt" if (tmp_path / "plain.txt").exists() else _make(tmp_path), arcname="plain.txt")
    out = extract = None
    from app.services.archive import extract_archive
    out = extract_archive(tar, tmp_path / "out_tar", None)
    assert (out / "plain.txt").exists()


def _make(tmp_path):
    f = tmp_path / "plain.txt"
    f.write_text("data")
    return f


# --------------------------------------------------------------------- config
def test_config_tolerates_bad_env_values(monkeypatch, tmp_path):
    """A typo in an env var must not stop the bot from starting."""
    monkeypatch.setenv("API_ID", "1")
    monkeypatch.setenv("API_HASH", "x")
    monkeypatch.setenv("BOT_TOKEN", "1:token")
    monkeypatch.setenv("MAX_CONCURRENT_JOBS", "not-a-number")
    monkeypatch.setenv("PROGRESS_INTERVAL", "abc")
    monkeypatch.setenv("SUDO_USERS", "1,notanumber, 2 ")
    monkeypatch.setenv("DOWNLOAD_DIR", str(tmp_path / "dl"))
    monkeypatch.setenv("WORK_DIR", str(tmp_path / "wk"))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "db.sqlite3"))

    from app.config import Config
    cfg = Config.from_env()
    assert cfg.max_concurrent_jobs == 10
    assert cfg.progress_interval == 3.0
    assert cfg.sudo_users == frozenset({1, 2})
    assert cfg.max_ffmpeg_jobs >= 1


def test_bulk_settings_persist(tmp_path):
    from app.storage.db import DB
    db = DB(tmp_path / "db.sqlite3")
    db.register_user(5, "u", "U", None)
    assert db.get_bulk_mode(5) is False
    db.set_bulk_mode(5, True)
    assert db.get_bulk_mode(5) is True
    db.set_auto_delete(5, True)
    assert db.get_auto_delete(5) is True
    db.set_keep_files(5, True)
    assert db.get_keep_files(5) is True
    db.close()


def test_settings_menu_exposes_bulk_toggles():
    from app.ui.keyboards import settings_menu
    on = [b.text for row in settings_menu(True, "gofile", "document", False, True, False, False) for b in row]
    off = [b.text for row in settings_menu(True, "gofile", "document", False, False, False, False) for b in row]
    # Exactly one of the ON/OFF buttons carries the check mark.
    assert any("Bulk Mode: ON" in x and "✅" in x for x in on)
    assert not any("Bulk Mode: OFF" in x and "✅" in x for x in on)
    assert any("Bulk Mode: OFF" in x and "✅" in x for x in off)
    assert not any("Bulk Mode: ON" in x and "✅" in x for x in off)
    for labels in (on, off):
        assert any("Delete After Upload" in x for x in labels)
        assert any("Keep Files" in x for x in labels)


def test_bulk_menu_buttons():
    from app.ui.keyboards import bulk_menu, bulk_upload_menu
    assert [b.text for row in bulk_menu(3, 1) for b in row] == [
        "✅ Done Adding (3)", "📤 Upload All", "🗑️ Clear Queue", "Cancel ❌",
    ]
    assert [b.text for row in bulk_upload_menu(2) for b in row] == [
        "📤 Telegram (MTProto)", "☁️ GoFile", "⬅️ Back to queue (2)", "Cancel",
    ]
