from __future__ import annotations

import asyncio
import json
import os
import signal
from pathlib import Path
from typing import Sequence


class ProcessCancelled(Exception):
    pass


async def run_cmd(args: Sequence[str], cancel_event: asyncio.Event | None = None, cwd: Path | None = None) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(*map(str, args), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=str(cwd) if cwd else None)
    async def wait_process():
        return await proc.communicate()
    task = asyncio.create_task(wait_process())
    try:
        while not task.done():
            if cancel_event and cancel_event.is_set():
                try:
                    proc.send_signal(signal.SIGINT)
                    await asyncio.wait_for(task, timeout=3)
                except Exception:
                    proc.kill()
                    await task
                raise ProcessCancelled()
            await asyncio.sleep(0.15)
        out, err = await task
        return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")
    finally:
        if not task.done():
            task.cancel()


async def ffprobe_json(path: Path) -> dict:
    code, out, err = await run_cmd(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)])
    if code != 0:
        raise RuntimeError(err[-3000:] or "ffprobe failed")
    return json.loads(out)


def stream_summary(stream: dict) -> str:
    typ = stream.get("codec_type", "unknown").capitalize()
    lang = (stream.get("tags") or {}).get("language", "und")
    codec = stream.get("codec_name", "unknown")
    title = (stream.get("tags") or {}).get("title", "")
    idx = stream.get("index", "?")
    extra = f" · {title}" if title else ""
    return f"{idx} - {typ} - {lang} - {codec}{extra}"
