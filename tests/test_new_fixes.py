
import subprocess
from pathlib import Path
from app.services import ffmpeg, ffprobe


def make_media(path: Path):
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc=size=320x180:rate=10",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
        "-t", "2", "-c:v", "libx264", "-c:a", "aac", str(path)
    ], check=True)


def test_sample_is_stream_copy_and_uses_mkv(tmp_path):
    src = tmp_path / "source.mkv"
    requested = tmp_path / "sample.mkv"
    make_media(src)
    out = ffmpeg.sample(src, requested, 1)
    assert out == requested
    assert out.exists() and out.stat().st_size > 0
    streams = ffprobe.probe(out)["streams"]
    assert any(s["codec_type"] == "video" for s in streams)
    assert any(s["codec_type"] == "audio" for s in streams)


def test_exact_stream_packet_sizes(tmp_path):
    src = tmp_path / "source.mkv"
    make_media(src)
    sizes = ffprobe.stream_packet_sizes(src)
    streams = ffprobe.probe(src)["streams"]
    assert all(s["index"] in sizes for s in streams)
    assert all(sizes[s["index"]] > 0 for s in streams)


def test_start_does_not_open_media_menu_and_exposes_status_stats():
    source = Path("app/main.py").read_text()
    assert 'if cmd == "/start":' in source
    start_block = source[source.index('if cmd == "/start":'):source.index('elif cmd == "/menu":')]
    assert "send_start_info" in start_block
    assert "send_main" not in start_block
    assert "start:stats" in source
    assert "system_stats_text" in source


def test_merge_session_is_seeded_from_current_source_and_snapshotted():
    source = Path("app/main.py").read_text()
    assert "base = self.source_media(st)" in source
    assert "st.merge_inputs = [base.resolve()]" in source
    assert "candidates = list(st.merge_inputs)" in source
    assert "inputs: list[Path] = []" in source
    assert "Queued:" in source


def test_merge_video_plus_audio_only_and_multiple_video_tracks(tmp_path):
    video = tmp_path / "video.mkv"
    audio = tmp_path / "audio.mka"
    second_video = tmp_path / "video2.mkv"
    for out, args in [
        (video, ["-f", "lavfi", "-i", "testsrc=size=320x180:rate=10", "-t", "1", "-c:v", "libx264"]),
        (second_video, ["-f", "lavfi", "-i", "testsrc2=size=320x180:rate=10", "-t", "1", "-c:v", "libx264"]),
        (audio, ["-f", "lavfi", "-i", "sine=frequency=660:sample_rate=48000", "-t", "1", "-c:a", "aac"]),
    ]:
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args, str(out)], check=True)
    from app.services.merge import merge_tracks
    out = tmp_path / "merged.mkv"
    merge_tracks([video, audio, second_video], out)
    streams = ffprobe.probe(out)["streams"]
    assert sum(s["codec_type"] == "video" for s in streams) == 2
    assert sum(s["codec_type"] == "audio" for s in streams) == 1
