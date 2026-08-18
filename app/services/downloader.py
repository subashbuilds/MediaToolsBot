from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from urllib.parse import unquote, urlparse

import aiohttp

from ..utils.files import safe_filename, unique_path


async def _read_chunk_or_cancel(stream, size: int, cancel_event: asyncio.Event | None):
    read_task = asyncio.create_task(stream.read(size))
    cancel_task = asyncio.create_task(cancel_event.wait()) if cancel_event else None
    tasks = {read_task}
    if cancel_task:
        tasks.add(cancel_task)
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    try:
        if cancel_task and cancel_task in done and cancel_event and cancel_event.is_set():
            read_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await read_task
            raise asyncio.CancelledError
        return read_task.result()
    finally:
        if cancel_task:
            cancel_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cancel_task
        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def download_url(
    url: str,
    dest_dir: Path,
    progress=None,
    cancel_event: asyncio.Event | None = None,
) -> Path:
    timeout = aiohttp.ClientTimeout(total=None, connect=60, sock_read=300)
    headers = {"User-Agent": "Mozilla/5.0 MediaToolsBot/3.0"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(url, allow_redirects=True) as r:
            r.raise_for_status()
            name = unquote(Path(urlparse(str(r.url)).path).name) or "download.bin"
            cd = r.headers.get("Content-Disposition", "")
            if "filename*=" in cd:
                name = cd.split("filename*=", 1)[1].split(";", 1)[0].strip().strip('"').split("''", 1)[-1]
                name = unquote(name)
            elif "filename=" in cd:
                name = cd.split("filename=", 1)[1].split(";", 1)[0].strip().strip('"')
            out = unique_path(dest_dir, safe_filename(name))
            total = int(r.headers.get("Content-Length") or 0)
            current = 0
            try:
                with out.open("wb") as f:
                    while True:
                        if cancel_event and cancel_event.is_set():
                            raise asyncio.CancelledError
                        chunk = await _read_chunk_or_cancel(r.content, 1024 * 1024, cancel_event)
                        if not chunk:
                            break
                        f.write(chunk)
                        current += len(chunk)
                        if progress:
                            await progress(current, total)
                if progress:
                    await progress(current, total)
                return out
            except BaseException:
                out.unlink(missing_ok=True)
                raise
