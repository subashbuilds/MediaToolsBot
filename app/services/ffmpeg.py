from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Callable, Iterable

from .ffprobe import probe, safe_probe
from .process_control import run_command
from ..utils.files import safe_filename

# FFmpeg reports machine-readable progress on stdout when these flags are set.
# The real log still goes to stderr, so error handling is unaffected.
PROGRESS_ARGS = ["-progress", "pipe:1", "-nostats"]

# Codecs that MP4 can legally carry without re-encoding. Anything else (FLAC,
# Opus, Vorbis, PCM ...) has to be transcoded or the muxer refuses to write the
# header, which is why MP4 output no longer uses "-c:a copy".
MP4_SAFE_AUDIO = {"aac", "mp3", "ac3", "eac3", "alac"}

ProgressSink = Callable[[float], None] | None


def _run(args: list[str], timeout: int | None = None, on_progress: ProgressSink = None) -> subprocess.CompletedProcess:
    return run_command(args, text=True, timeout=timeout, on_progress=on_progress)


def _progress_args(on_progress: ProgressSink) -> list[str]:
    return list(PROGRESS_ARGS) if on_progress else []


def safe_stem(stem: str) -> str:
    return safe_filename(stem, "media")


def parse_timecode(value: str) -> float:
    """Parse ``12``, ``90``, ``1:30`` or ``01:02:03`` into seconds.

    Raises ValueError for anything else so user input is validated before it
    reaches the FFmpeg command line.
    """
    raw = str(value).strip()
    if not raw:
        raise ValueError("empty timestamp")
    if re.fullmatch(r"\d+(\.\d+)?", raw):
        return float(raw)
    parts = raw.split(":")
    if len(parts) > 3:
        raise ValueError(f"invalid timestamp: {value}")
    total = 0.0
    for part in parts:
        part = part.strip()
        if not re.fullmatch(r"\d+(\.\d+)?", part):
            raise ValueError(f"invalid timestamp: {value}")
        total = total * 60 + float(part)
    return total


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


def remux(path: Path, output: Path, keep_indices: list[int], on_progress: ProgressSink = None) -> Path:
    if not keep_indices:
        raise ValueError("At least one stream must be kept")
    _run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
        *_map_args(keep_indices), "-map_metadata", "0", "-c", "copy",
        *_progress_args(on_progress), str(output)
    ], on_progress=on_progress)
    return output


def trim(path: Path, output: Path, start: str, end: str | None = None, on_progress: ProgressSink = None) -> Path:
    """Cut ``[start, end)`` out of a media file.

    ``-ss`` is an *input* option and ``-to`` is an *output* option, so mixing
    them (as this function previously did) made FFmpeg seek to ``start`` and
    then still copy until the *original* timeline reached ``end`` - turning a
    10s/20s request into a 30s clip. Both bounds are now computed explicitly
    and expressed as an input ``-ss`` plus an output ``-t`` duration.
    """
    try:
        start_s = max(0.0, parse_timecode(start))
    except ValueError as exc:
        raise ValueError(f"Invalid start time: {start}") from exc
    total = duration(path)
    if end in (None, "", "0"):
        length = max(1.0, total - start_s) if total else None
    else:
        try:
            end_s = parse_timecode(end)
        except ValueError as exc:
            raise ValueError(f"Invalid end time: {end}") from exc
        if end_s <= start_s:
            raise ValueError("End time must be greater than start time")
        if total and end_s > total:
            end_s = total
        length = max(0.1, end_s - start_s)
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start_s:.3f}", "-i", str(path)]
    if length:
        args += ["-t", f"{length:.3f}"]
    args += ["-map", "0", "-c", "copy", "-avoid_negative_ts", "make_zero"]
    args += _progress_args(on_progress)
    args += [str(output)]
    _run(args, on_progress=on_progress)
    return output


def remove_audio(path: Path, output: Path, on_progress: ProgressSink = None) -> Path:
    keep = [s["index"] for s in probe(path)["streams"] if s.get("codec_type") != "audio"]
    if not keep:
        raise ValueError("This file has no non-audio stream to keep")
    return remux(path, output, keep, on_progress=on_progress)


def convert_video(path: Path, output: Path, fmt: str, on_progress: ProgressSink = None) -> Path:
    if fmt not in {"mp4", "mkv"}:
        raise ValueError("format must be mp4 or mkv")
    data = probe(path)
    streams = data.get("streams", [])
    videos = [s for s in streams if s.get("codec_type") == "video"]
    if not videos:
        raise ValueError("No video stream found in this file")
    # Pick a single video track: MP4 cannot carry more than one, and mapping
    # every stream used to abort the whole conversion on multi-angle files.
    args = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
        "-map", f"0:{videos[0]['index']}",
    ]
    if fmt == "mp4":
        args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]
        audios = [s for s in streams if s.get("codec_type") == "audio"]
        if audios:
            args += ["-map", f"0:{audios[0]['index']}"]
            codec = (audios[0].get("codec_name") or "").lower()
            # "-c:a copy" is only valid when the codec is muxable into MP4;
            # FLAC/Opus/Vorbis otherwise fail with "Could not write header".
            args += ["-c:a", "copy" if codec in MP4_SAFE_AUDIO else "aac", "-b:a", "192k"]
        # MP4 only gets text subtitles that FFmpeg can convert to mov_text.
        # Image subtitles/attachments are deliberately omitted instead of
        # making an otherwise valid conversion fail.
        for stream in streams:
            if stream.get("codec_type") == "subtitle" and (stream.get("codec_name") or "").lower() in {"subrip", "ass", "ssa", "webvtt", "mov_text"}:
                args += ["-map", f"0:{stream['index']}"]
        args += ["-c:s", "mov_text", "-movflags", "+faststart"]
    else:
        args += ["-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "copy"]
        args += ["-map", "0:s?", "-c:s", "copy"]
    args += _progress_args(on_progress)
    args += [str(output)]
    _run(args, on_progress=on_progress)
    return output


def video_to_audio(path: Path, output: Path, on_progress: ProgressSink = None) -> Path:
    if not any(s.get("codec_type") == "audio" for s in probe(path).get("streams", [])):
        raise ValueError("No audio stream found")
    _run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
        "-vn", "-map", "0:a:0", "-c:a", "libmp3lame", "-q:a", "2",
        *_progress_args(on_progress), str(output)
    ], on_progress=on_progress)
    return output


def audio_convert(path: Path, output: Path, codec: str, on_progress: ProgressSink = None) -> Path:
    data = safe_probe(path)
    if not any(s.get("codec_type") == "audio" for s in data.get("streams", [])):
        raise ValueError("This file has no audio stream to convert")
    _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path), "-vn", "-map", "0:a:0", "-c:a", codec, *_progress_args(on_progress), str(output)], on_progress=on_progress)
    return output


def audio_filter(path: Path, output: Path, filt: str, codec: str = "aac", on_progress: ProgressSink = None) -> Path:
    data = safe_probe(path)
    if not any(s.get("codec_type") == "audio" for s in data.get("streams", [])):
        raise ValueError("This file has no audio stream to process")
    _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path), "-vn", "-map", "0:a:0", "-af", filt, "-c:a", codec, *_progress_args(on_progress), str(output)], on_progress=on_progress)
    return output


def screenshots(path: Path, outdir: Path, count: int = 5) -> list[Path]:
    count = max(1, min(30, int(count)))
    outdir.mkdir(parents=True, exist_ok=True)
    dur = duration(path)
    if dur <= 0:
        raise RuntimeError("Unable to determine media duration")
    paths = []
    for i in range(count):
        # Sample inside the media, avoiding frame 0 which is frequently black.
        t = dur * (i + 1) / (count + 1)
        out = outdir / f"shot_{i+1:02d}.jpg"
        out.unlink(missing_ok=True)
        _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1", "-q:v", "2", str(out)])
        if not out.exists() or out.stat().st_size == 0:
            raise RuntimeError(f"Could not extract a frame at {t:.1f}s")
        paths.append(out)
    return paths


def manual_shot(path: Path, output: Path, timestamp: str) -> Path:
    try:
        seconds = parse_timecode(timestamp)
    except ValueError:
        raise ValueError(f"Invalid timestamp: {timestamp}")
    total = duration(path)
    # Clamp instead of failing: a slightly late timestamp should still return
    # the last available frame rather than an empty file.
    if total and seconds >= total:
        seconds = max(0.0, total - 0.5)
    output.unlink(missing_ok=True)
    _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{seconds:.3f}", "-i", str(path), "-frames:v", "1", "-q:v", "2", str(output)])
    if not output.exists() or output.stat().st_size == 0:
        raise RuntimeError(f"No frame could be extracted at {timestamp}")
    return output


def sample(path: Path, output: Path, seconds: int = 30, on_progress: ProgressSink = None) -> Path:
    """Create a low-overhead sample without re-encoding the source.

    Re-encoding a large 1080p/10-bit HEVC source just to make a short sample
    can consume enough RAM/CPU to get a constrained container OOM-killed.
    Matroska stream-copy is fast, preserves the original codecs, and is much
    safer for VPS/Railway deployments.
    """
    seconds = max(1, min(3600, int(seconds)))
    output = output.with_suffix(".mkv")
    _run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", "0", "-i", str(path), "-t", str(seconds),
        "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
        "-avoid_negative_ts", "make_zero", *_progress_args(on_progress), str(output)
    ], on_progress=on_progress)
    return output


def split_video(path: Path, outdir: Path, segment_seconds: int) -> list[Path]:
    segment_seconds = max(1, int(segment_seconds))
    outdir.mkdir(parents=True, exist_ok=True)
    # Name parts after the source so the user can tell what they are. The
    # segmenter is pinned to Matroska because it is the only muxer that can
    # start a new file on a non-keyframe boundary without corrupting the
    # stream-copy output.
    stem = safe_stem(path.stem)
    pattern = outdir / f"{stem}.part%03d.mkv"
    for stale in outdir.glob(f"{stem}.part*.mkv"):
        stale.unlink(missing_ok=True)
    _run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path), "-map", "0", "-c", "copy",
        "-f", "segment", "-segment_time", str(segment_seconds), "-break_non_keyframes", "1",
        "-reset_timestamps", "1", "-segment_format", "matroska", str(pattern)
    ])
    parts = sorted(outdir.glob(f"{stem}.part*.mkv"))
    if not parts:
        raise RuntimeError("The splitter produced no segments")
    return parts


def optimize(path: Path, output: Path, on_progress: ProgressSink = None) -> Path:
    _run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-fflags", "+genpts", "-i", str(path),
        "-map", "0", "-c", "copy", "-avoid_negative_ts", "make_zero",
        *_progress_args(on_progress), str(output)
    ], on_progress=on_progress)
    return output


def _stream_output_extension(stream: dict) -> str:
    typ = stream.get("codec_type")
    codec = (stream.get("codec_name") or "").lower()
    if typ == "subtitle":
        return {"subrip": ".srt", "ass": ".ass", "ssa": ".ass", "webvtt": ".vtt"}.get(codec, ".mkv")
    if typ == "audio":
        return {"aac": ".m4a", "mp3": ".mp3", "opus": ".opus", "flac": ".flac", "vorbis": ".ogg", "eac3": ".eac3", "ac3": ".ac3"}.get(codec, ".mka")
    return ".mkv"


def extract_stream(path: Path, output: Path, stream_index: int, on_progress: ProgressSink = None) -> Path:
    data = probe(path)
    stream = next((s for s in data.get("streams", []) if s.get("index") == stream_index), None)
    if not stream:
        raise ValueError("Stream not found")
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path), "-map", f"0:{stream_index}", "-c", "copy"]
    if stream.get("codec_type") == "subtitle" and output.suffix.lower() in {".srt", ".ass", ".vtt"}:
        args += ["-map_metadata", "-1"]
    args += _progress_args(on_progress)
    args += [str(output)]
    _run(args, on_progress=on_progress)
    return output
