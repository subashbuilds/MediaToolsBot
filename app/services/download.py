from __future__ import annotations

import asyncio
import time
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

from ..utils.files import safe_filename
from ..utils.progress import render


async def download_url(url: str, dest_dir: Path, progress_cb=None, cancel_event: asyncio.Event | None = None) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=60, sock_read=300)
    headers = {"User-Agent": "Mozilla/5.0 VideoConverterBot/1.0"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers, raise_for_status=True) as session:
        async with session.get(url, allow_redirects=True) as resp:
            cd = resp.headers.get("Content-Disposition", "")
            name = None
            if "filename=" in cd:
                name = cd.split("filename=", 1)[1].strip(' \"\'')
            if not name:
                name = Path(urlparse(str(resp.url)).path).name or "download.bin"
            name = safe_filename(name)
            target = dest_dir / name
            if target.exists():
                target = dest_dir / f"{target.stem}_{int(time.time())}{target.suffix}"
            total = int(resp.headers.get("Content-Length", "0") or 0) or None
            done = 0
            started = time.monotonic()
            with target.open("wb") as f:
                async for chunk in resp.content.iter_chunked(4 * 1024 * 1024):
                    if cancel_event and cancel_event.is_set():
                        raise asyncio.CancelledError
                    f.write(chunk)
                    done += len(chunk)
                    if progress_cb and (time.monotonic() - started > 1):
                        await progress_cb(render("Downloading", done, total, started))
            return target
