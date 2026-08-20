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


def make_multitrack(path: Path, subtitle: Path):
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc=size=320x180:rate=10",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
        "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000",
        "-i", str(subtitle), "-t", "2",
        "-map", "0:v", "-map", "1:a", "-map", "2:a", "-map", "3:0",
        "-metadata:s:a:0", "language=eng", "-metadata:s:a:1", "language=tam",
        "-metadata:s:s:0", "language=eng", "-c:v", "libx264", "-c:a", "aac", "-c:s", "srt",
        str(path)
    ], check=True)


def test_multitrack_streams_and_merge(tmp_path):
    a = tmp_path / "a.mkv"
    b = tmp_path / "b.mkv"
    sub = tmp_path / "sub.srt"
    make_multitrack(a, sub)
    make_media(b)
    data = probe(a)
    types = [s["codec_type"] for s in data["streams"]]
    assert types.count("video") == 1
    assert types.count("audio") == 2
    assert types.count("subtitle") == 1
    out = tmp_path / "merged.mkv"
    from app.services.merge import merge_tracks
    merge_tracks([a, b], out)
    merged = probe(out)
    assert sum(s.get("codec_type") == "video" for s in merged["streams"]) >= 2
    assert sum(s.get("codec_type") == "audio" for s in merged["streams"]) >= 3


def test_sample_uses_stream_copy_and_is_valid(tmp_path):
    src = tmp_path / "source.mkv"
    out = tmp_path / "sample.mkv"
    make_media(src)
    from app.services.ffmpeg import sample
    sample(src, out, 1)
    data = probe(out)
    assert out.exists() and out.stat().st_size > 0
    assert any(s.get("codec_type") == "video" for s in data.get("streams", []))
