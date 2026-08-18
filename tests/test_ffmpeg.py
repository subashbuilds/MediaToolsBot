import subprocess
from pathlib import Path
from app.services.ffprobe import probe
from app.services import ffmpeg


def make_media(path: Path):
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc=size=320x180:rate=10",
        "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000",
        "-t", "2", "-c:v", "libx264", "-c:a", "aac", str(path)
    ], check=True)


def test_probe_and_remux(tmp_path):
    src = tmp_path / "in.mkv"
    out = tmp_path / "out.mkv"
    make_media(src)
    data = probe(src)
    assert any(s["codec_type"] == "video" for s in data["streams"])
    ffmpeg.remux(src, out, [s["index"] for s in data["streams"]])
    assert out.exists() and out.stat().st_size > 0


def test_audio_conversion_and_thumbnail(tmp_path):
    src = tmp_path / "in.mkv"
    make_media(src)
    mp3 = tmp_path / "out.mp3"
    jpg = tmp_path / "shot.jpg"
    ffmpeg.video_to_audio(src, mp3)
    ffmpeg.manual_shot(src, jpg, "00:00:01")
    assert mp3.exists() and mp3.stat().st_size > 0
    assert jpg.exists() and jpg.stat().st_size > 0
