from __future__ import annotations

import asyncio
import json
from pathlib import Path

from .process import run_cmd


async def thumbnail_from_media(src: Path, out: Path, cancel: asyncio.Event):
    code, _, err = await run_cmd(["ffmpeg", "-hide_banner", "-y", "-i", str(src), "-map", "0:v:0", "-frames:v", "1", "-q:v", "2", str(out)], cancel)
    if code != 0:
        raise RuntimeError(err[-2000:])
    return out


async def thumbnail_from_url(url: str, out_dir: Path, cancel: asyncio.Event):
    out_dir.mkdir(parents=True, exist_ok=True)
    template = str(out_dir / "%(title).120s.%(ext)s")
    code, out, err = await run_cmd(["yt-dlp", "--skip-download", "--write-thumbnail", "--convert-thumbnails", "jpg", "-o", template, url], cancel)
    if code != 0:
        raise RuntimeError(err[-3000:])
    files = sorted(out_dir.glob("*.jpg"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        raise RuntimeError("No thumbnail was returned by yt-dlp")
    return files[0]
