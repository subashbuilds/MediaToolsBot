from __future__ import annotations

import asyncio
import json
import math
import os
import re
import shlex
import shutil
import tarfile
import zipfile
from pathlib import Path
from typing import Iterable

from .process import ProcessCancelled, ffprobe_json, run_cmd


def _ext(path: Path, default: str) -> str:
    return path.suffix.lower() or default


async def media_info(path: Path) -> dict:
    return await ffprobe_json(path)


async def remux_remove_streams(src: Path, keep: list[int], out: Path, cancel: asyncio.Event):
    args = ["ffmpeg", "-hide_banner", "-y", "-i", str(src)]
    for i in keep:
        args += ["-map", f"0:{i}"]
    args += ["-map_metadata", "0", "-c", "copy", str(out)]
    return await run_cmd(args, cancel)


async def extract_stream(src: Path, stream_index: int, out: Path, cancel: asyncio.Event):
    info = await ffprobe_json(src)
    stream = next((s for s in info.get("streams", []) if s.get("index") == stream_index), None)
    if not stream:
        raise ValueError("Stream index not found")
    typ = stream.get("codec_type")
    if typ == "audio":
        args = ["ffmpeg", "-hide_banner", "-y", "-i", str(src), "-map", f"0:{stream_index}", "-c", "copy", str(out)]
    elif typ == "subtitle":
        args = ["ffmpeg", "-hide_banner", "-y", "-i", str(src), "-map", f"0:{stream_index}", "-c", "copy", str(out)]
    else:
        args = ["ffmpeg", "-hide_banner", "-y", "-i", str(src), "-map", f"0:{stream_index}", "-c", "copy", str(out)]
    return await run_cmd(args, cancel)


async def trim(src: Path, start: str, duration: str, out: Path, cancel: asyncio.Event):
    args = ["ffmpeg", "-hide_banner", "-y", "-ss", start, "-i", str(src), "-t", duration, "-map", "0", "-c", "copy", str(out)]
    return await run_cmd(args, cancel)


async def remove_audio(src: Path, out: Path, cancel: asyncio.Event):
    return await run_cmd(["ffmpeg", "-hide_banner", "-y", "-i", str(src), "-map", "0:v?", "-map", "0:s?", "-c", "copy", str(out)], cancel)


async def video_to_audio(src: Path, out: Path, codec: str, cancel: asyncio.Event):
    return await run_cmd(["ffmpeg", "-hide_banner", "-y", "-i", str(src), "-vn", "-map", "0:a:0", "-c:a", codec, str(out)], cancel)


async def convert_video(src: Path, out: Path, fmt: str, cancel: asyncio.Event):
    if fmt == "mp4":
        args = ["ffmpeg", "-hide_banner", "-y", "-i", str(src), "-map", "0:v:0", "-map", "0:a?", "-map", "0:s?", "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-c:a", "aac", "-c:s", "copy", str(out)]
    else:
        args = ["ffmpeg", "-hide_banner", "-y", "-i", str(src), "-map", "0", "-c", "copy", str(out)]
    return await run_cmd(args, cancel)


async def optimize_video(src: Path, out: Path, crf: int, preset: str, cancel: asyncio.Event):
    return await run_cmd(["ffmpeg", "-hide_banner", "-y", "-i", str(src), "-map", "0", "-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-c:a", "copy", "-c:s", "copy", str(out)], cancel)


async def split_video(src: Path, out_dir: Path, segment: int, cancel: asyncio.Event):
    out_dir.mkdir(parents=True, exist_ok=True)
    pattern = out_dir / f"{src.stem}_%03d.mp4"
    # Re-encode video so segment boundaries are honored even when the source has sparse keyframes.
    return await run_cmd(["ffmpeg", "-hide_banner", "-y", "-i", str(src), "-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-force_key_frames", f"expr:gte(t,n_forced*{segment})", "-c:a", "aac", "-f", "segment", "-segment_time", str(segment), "-reset_timestamps", "1", str(pattern)], cancel)


async def screenshot(src: Path, out: Path, timestamp: str, cancel: asyncio.Event):
    return await run_cmd(["ffmpeg", "-hide_banner", "-y", "-ss", timestamp, "-i", str(src), "-frames:v", "1", "-q:v", "2", str(out)], cancel)


async def screenshots(src: Path, out_dir: Path, count: int, cancel: asyncio.Event):
    info = await ffprobe_json(src)
    dur = float((info.get("format") or {}).get("duration") or 0)
    if dur <= 0:
        raise ValueError("Unable to determine duration")
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for n in range(1, count + 1):
        if cancel.is_set():
            raise ProcessCancelled()
        t = dur * n / (count + 1)
        out = out_dir / f"shot_{n:02d}.jpg"
        code, _, err = await screenshot(src, out, f"{t:.3f}", cancel)
        if code != 0:
            raise RuntimeError(err[-2000:])
        results.append(out)
    return results


async def generate_sample(src: Path, out: Path, duration: int, cancel: asyncio.Event):
    return await run_cmd(["ffmpeg", "-hide_banner", "-y", "-i", str(src), "-t", str(duration), "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-c:a", "aac", str(out)], cancel)


async def audio_convert(src: Path, out: Path, codec: str, bitrate: str, cancel: asyncio.Event):
    return await run_cmd(["ffmpeg", "-hide_banner", "-y", "-i", str(src), "-vn", "-map", "0:a:0", "-c:a", codec, "-b:a", bitrate, str(out)], cancel)


async def audio_filter(src: Path, out: Path, filter_expr: str, codec: str = "libmp3lame", cancel: asyncio.Event | None = None):
    return await run_cmd(["ffmpeg", "-hide_banner", "-y", "-i", str(src), "-vn", "-af", filter_expr, "-c:a", codec, "-q:a", "2", str(out)], cancel)


async def audio_trim(src: Path, start: str, duration: str, out: Path, cancel: asyncio.Event):
    return await run_cmd(["ffmpeg", "-hide_banner", "-y", "-ss", start, "-i", str(src), "-t", duration, "-vn", "-c:a", "copy", str(out)], cancel)


async def audio_info(src: Path) -> dict:
    return await ffprobe_json(src)


def _safe_member_path(root: Path, member: str) -> Path:
    # Prevent archive path traversal (e.g. ../../outside).
    target = (root / member).resolve()
    root_resolved = root.resolve()
    if target != root_resolved and root_resolved not in target.parents:
        raise RuntimeError(f"Unsafe archive member: {member}")
    return target


def _extract_zip(src: Path, out_dir: Path, cancel: asyncio.Event) -> None:
    with zipfile.ZipFile(src) as zf:
        for info in zf.infolist():
            if cancel.is_set():
                raise ProcessCancelled()
            target = _safe_member_path(out_dir, info.filename)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as rf, target.open("wb") as wf:
                while True:
                    if cancel.is_set():
                        raise ProcessCancelled()
                    chunk = rf.read(8 * 1024 * 1024)
                    if not chunk:
                        break
                    wf.write(chunk)


def _extract_tar(src: Path, out_dir: Path, cancel: asyncio.Event) -> None:
    with tarfile.open(src, "r:*") as tf:
        for member in tf.getmembers():
            if cancel.is_set():
                raise ProcessCancelled()
            target = _safe_member_path(out_dir, member.name)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                rf = tf.extractfile(member)
                if rf is None:
                    continue
                with rf, target.open("wb") as wf:
                    while True:
                        if cancel.is_set():
                            raise ProcessCancelled()
                        chunk = rf.read(8 * 1024 * 1024)
                        if not chunk:
                            break
                        wf.write(chunk)
            else:
                # Do not materialize symlinks/devices from untrusted archives.
                continue


def _archive_cli() -> str | None:
    # Different Debian/Ubuntu releases expose either 7zz or 7z.
    for name in ("7zz", "7z", "7za"):
        path = shutil.which(name)
        if path:
            return path
    return None


async def archive_extract(src: Path, out_dir: Path, cancel: asyncio.Event):
    out_dir.mkdir(parents=True, exist_ok=True)
    name = src.name.lower()

    if name.endswith(".zip"):
        await asyncio.to_thread(_extract_zip, src, out_dir, cancel)
        return 0, "", ""

    if name.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")):
        await asyncio.to_thread(_extract_tar, src, out_dir, cancel)
        return 0, "", ""

    if name.endswith(".rar"):
        unar = shutil.which("unar")
        if unar:
            return await run_cmd([unar, "-f", "-o", str(out_dir), str(src)], cancel)

    tool = _archive_cli()
    if not tool:
        raise RuntimeError(
            "No archive extractor is installed. Install 7zip/7zz (and unar for RAR) in the runtime image."
        )
    return await run_cmd([tool, "x", "-y", f"-o{out_dir}", str(src)], cancel)
