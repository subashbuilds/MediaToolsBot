from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import os
import socket
from pathlib import Path
from urllib.parse import unquote, urlparse

import aiohttp

from ..utils.files import safe_filename, unique_path

CHUNK = 1024 * 1024
# Default ceiling for a single URL download. Without it a malicious or broken
# link could fill the container's disk and take the whole bot down.
MAX_DOWNLOAD_BYTES = int(os.environ.get("MAX_DOWNLOAD_BYTES", str(2048 * 1024 * 1024)))
MAX_REDIRECTS = 5

CONTENT_TYPE_EXT = {
    "video/mp4": ".mp4", "video/x-matroska": ".mkv", "video/webm": ".webm",
    "video/quicktime": ".mov", "video/x-msvideo": ".avi", "video/mpeg": ".mpeg",
    "video/3gpp": ".3gp", "video/x-ms-wmv": ".wmv", "video/3gpp2": ".3g2",
    "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/aac": ".aac",
    "audio/ogg": ".ogg", "audio/opus": ".opus", "audio/flac": ".flac",
    "audio/x-wav": ".wav", "audio/wav": ".wav", "audio/x-m4a": ".m4a",
    "application/zip": ".zip", "application/x-7z-compressed": ".7z",
    "application/x-rar-compressed": ".rar", "application/gzip": ".tar.gz",
    "application/x-tar": ".tar", "application/pdf": ".pdf",
    "application/octet-stream": ".bin", "image/jpeg": ".jpg", "image/png": ".png",
}


class DownloadError(RuntimeError):
    """Raised for user-facing download failures with a readable message."""


def _is_blocked_host(host: str) -> bool:
    """Reject loopback/private/link-local targets (SSRF protection).

    The bot downloads URLs on behalf of a user and then serves or re-uploads the
    bytes, so without this check anybody could ask the bot to fetch internal
    services (cloud metadata, databases, the direct-link server itself).
    """
    if not host:
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return True
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return True
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return True
    return False


def _validate_url(url: str, allow_private: bool = False) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise DownloadError("Only http:// and https:// links are supported.")
    if not parsed.hostname:
        raise DownloadError("The link does not contain a valid host.")
    if not allow_private and _is_blocked_host(parsed.hostname):
        raise DownloadError("Private, local or internal addresses cannot be downloaded.")
    return url


def _filename_from_response(r, url: str) -> str:
    cd = r.headers.get("Content-Disposition", "")
    if "filename*=" in cd:
        name = cd.split("filename*=", 1)[1].split(";", 1)[0].strip().strip('"').split("''", 1)[-1]
        name = unquote(name)
    elif "filename=" in cd:
        name = cd.split("filename=", 1)[1].split(";", 1)[0].strip().strip('"')
    else:
        name = unquote(Path(urlparse(str(r.url)).path).name)
    name = safe_filename(name)
    # A URL like /download?id=9 has no usable name; fall back to the content
    # type so the resulting file still has a meaningful extension.
    if not name or name in {"download", "download.bin", "index.html"} or "." not in name:
        ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        ext = CONTENT_TYPE_EXT.get(ctype, ".bin")
        name = f"{name or 'download'}{ext}"
    return name


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
    *,
    max_bytes: int | None = None,
    attempts: int = 3,
    allow_private: bool | None = None,
) -> Path:
    """Download ``url`` into ``dest_dir`` and return the created path.

    Retries transient network failures with backoff, enforces a size ceiling,
    and removes the partial file on any error or cancellation.
    """
    limit = max_bytes if max_bytes is not None else MAX_DOWNLOAD_BYTES
    if allow_private is None:
        allow_private = os.environ.get("ALLOW_PRIVATE_DOWNLOADS", "").strip().lower() in {"1", "on", "true", "yes"}
    _validate_url(url, allow_private)
    timeout = aiohttp.ClientTimeout(total=None, connect=60, sock_read=300)
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; MediaToolsBot/3.0; +https://telegram.org)",
        "Accept": "*/*",
    }
    last_error: Exception | None = None
    for attempt in range(1, max(1, attempts) + 1):
        if cancel_event and cancel_event.is_set():
            raise asyncio.CancelledError
        try:
            return await _download_once(url, dest_dir, progress, cancel_event, limit, timeout, headers, allow_private)
        except asyncio.CancelledError:
            raise
        except DownloadError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            last_error = exc
            if attempt >= max(1, attempts):
                break
            await asyncio.sleep(min(8, 1.5 * attempt))
    raise DownloadError(f"Download failed after {attempts} attempts: {last_error}")


async def _download_once(url, dest_dir, progress, cancel_event, limit, timeout, headers, allow_private=False) -> Path:
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(url, allow_redirects=True, max_redirects=MAX_REDIRECTS) as r:
            # Re-check the final host: a redirect can point at an internal IP.
            if r.url:
                _validate_url(str(r.url), allow_private)
            r.raise_for_status()
            name = _filename_from_response(r, url)
            out = unique_path(dest_dir, name)
            declared = r.headers.get("Content-Length")
            total = int(declared) if declared and declared.isdigit() else 0
            if total and limit and total > limit:
                raise DownloadError(
                    f"File is too large ({total / 1024**3:.2f} GiB). The limit is {limit / 1024**3:.0f} GiB."
                )
            current = 0
            try:
                with out.open("wb") as f:
                    while True:
                        if cancel_event and cancel_event.is_set():
                            raise asyncio.CancelledError
                        chunk = await _read_chunk_or_cancel(r.content, CHUNK, cancel_event)
                        if not chunk:
                            break
                        current += len(chunk)
                        if limit and current > limit:
                            raise DownloadError(
                                f"Download exceeded the {limit / 1024**3:.0f} GiB limit and was stopped."
                            )
                        f.write(chunk)
                        if progress:
                            await progress(current, total)
                if current == 0:
                    raise DownloadError("The server returned an empty response.")
                if progress:
                    await progress(current, current)
                return out
            except BaseException:
                out.unlink(missing_ok=True)
                raise
