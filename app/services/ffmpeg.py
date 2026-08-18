from __future__ import annotations

import json
import signal
import subprocess
from pathlib import Path
from typing import Iterable

from .ffprobe import probe


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)


def duration(path: Path) -> float:
    data = probe(path)
    try:
        return float(data.get("format", {}).get("duration") or 0)
    except (TypeError, ValueError):
        return 0.0


def media_info(path: Path) -> str:
    data = probe(path)
    fmt = data.get("format", {})
    streams = data.get("streams", [])
    size = path.stat().st_size
    lines = [
        f"📁 {path.name}",
        f"Size: {size / 1024**3:.2f} GiB",
        f"Duration: {fmt.get('duration', 'N/A')} s",
        f"Format: {fmt.get('format_name', 'N/A')}",
        "",
        "Index | Type | Codec | Language | Details",
    ]
    for s in streams:
        tags = s.get("tags", {}) or {}
        details = []
        if s.get("width") and s.get("height"):
            details.append(f"{s['width']}x{s['height']}")
        if s.get("channels"):
            details.append(f"{s['channels']}ch")
        if tags.get("title"):
            details.append(tags["title"])
        lines.append(
            f"{s.get('index')} | {s.get('codec_type')} | {s.get('codec_name')} | "
            f"{tags.get('language','und')} | {' '.join(details)}"
        )
    return "\n".join(lines)


def _map_args(indices: Iterable[int]) -> list[str]:
    args: list[str] = []
    for i in indices:
        args += ["-map", f"0:{int(i)}"]
    return args


def remux(path: Path, output: Path, keep_indices: list[int]) -> Path:
    if not keep_indices:
        raise ValueError("At least one stream must be kept")
    _run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
        *_map_args(keep_indices), "-map_metadata", "0", "-c", "copy", str(output)
    ])
    return output


def trim(path: Path, output: Path, start: str, end: str | None = None) -> Path:
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", start, "-i", str(path)]
    if end:
        args += ["-to", end]
    args += ["-map", "0", "-c", "copy", str(output)]
    _run(args)
    return output


def remove_audio(path: Path, output: Path) -> Path:
    return remux(path, output, [s["index"] for s in probe(path)["streams"] if s.get("codec_type") != "audio"])


def convert_video(path: Path, output: Path, fmt: str) -> Path:
    if fmt not in {"mp4", "mkv"}:
        raise ValueError("format must be mp4 or mkv")
    args = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
        "-map", "0:v?", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "copy",
    ]
    if fmt == "mp4":
        # MP4 only gets text subtitles that FFmpeg can convert to mov_text.
        # Image subtitles/attachments are deliberately omitted instead of
        # making an otherwise valid conversion fail.
        data = probe(path)
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "subtitle" and (stream.get("codec_name") or "").lower() in {"subrip", "ass", "ssa", "webvtt", "mov_text"}:
                args += ["-map", f"0:{stream['index']}"]
        args += ["-c:s", "mov_text", "-movflags", "+faststart"]
    else:
        args += ["-map", "0:s?", "-c:s", "copy"]
    args += [str(output)]
    _run(args)
    return output


def video_to_audio(path: Path, output: Path) -> Path:
    if not any(s.get("codec_type") == "audio" for s in probe(path).get("streams", [])):
        raise ValueError("No audio stream found")
    _run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
        "-vn", "-map", "0:a:0", "-c:a", "libmp3lame", "-q:a", "2", str(output)
    ])
    return output


def audio_convert(path: Path, output: Path, codec: str) -> Path:
    _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path), "-vn", "-map", "0:a:0", "-c:a", codec, str(output)])
    return output


def audio_filter(path: Path, output: Path, filt: str, codec: str = "aac") -> Path:
    _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path), "-vn", "-map", "0:a:0", "-af", filt, "-c:a", codec, str(output)])
    return output


def screenshots(path: Path, outdir: Path, count: int = 5) -> list[Path]:
    count = max(1, min(30, int(count)))
    outdir.mkdir(parents=True, exist_ok=True)
    dur = duration(path)
    if dur <= 0:
        raise RuntimeError("Unable to determine media duration")
    paths = []
    for i in range(count):
        t = dur * (i + 1) / (count + 1)
        out = outdir / f"shot_{i+1:02d}.jpg"
        _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1", "-q:v", "2", str(out)])
        paths.append(out)
    return paths


def manual_shot(path: Path, output: Path, timestamp: str) -> Path:
    _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", timestamp, "-i", str(path), "-frames:v", "1", "-q:v", "2", str(output)])
    return output


def sample(path: Path, output: Path, seconds: int = 30) -> Path:
    seconds = max(1, min(3600, int(seconds)))
    _run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path), "-t", str(seconds),
        "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
        "-c:a", "aac", str(output)
    ])
    return output


def split_video(path: Path, outdir: Path, segment_seconds: int) -> list[Path]:
    segment_seconds = max(1, int(segment_seconds))
    outdir.mkdir(parents=True, exist_ok=True)
    pattern = outdir / "part_%03d.mkv"
    _run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path), "-map", "0", "-c", "copy",
        "-f", "segment", "-segment_time", str(segment_seconds), "-break_non_keyframes", "1",
        "-reset_timestamps", "1", str(pattern)
    ])
    return sorted(outdir.glob("part_*.mkv"))


def optimize(path: Path, output: Path) -> Path:
    _run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-fflags", "+genpts", "-i", str(path),
        "-map", "0", "-c", "copy", "-avoid_negative_ts", "make_zero", str(output)
    ])
    return output


def _stream_output_extension(stream: dict) -> str:
    typ = stream.get("codec_type")
    codec = (stream.get("codec_name") or "").lower()
    if typ == "subtitle":
        return {"subrip": ".srt", "ass": ".ass", "ssa": ".ass", "webvtt": ".vtt"}.get(codec, ".mkv")
    if typ == "audio":
        return {"aac": ".m4a", "mp3": ".mp3", "opus": ".opus", "flac": ".flac", "vorbis": ".ogg", "eac3": ".eac3", "ac3": ".ac3"}.get(codec, ".mka")
    return ".mkv"


def extract_stream(path: Path, output: Path, stream_index: int) -> Path:
    data = probe(path)
    stream = next((s for s in data.get("streams", []) if s.get("index") == stream_index), None)
    if not stream:
        raise ValueError("Stream not found")
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path), "-map", f"0:{stream_index}", "-c", "copy"]
    if stream.get("codec_type") == "subtitle" and output.suffix.lower() in {".srt", ".ass", ".vtt"}:
        args += ["-map_metadata", "-1"]
    args += [str(output)]
    _run(args)
    return output


def cancel_process(proc: subprocess.Popen) -> None:
    try:
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
