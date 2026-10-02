"""Faster Telegram media transfers by fetching several byte ranges at once.

Telegram exposes a document through a ranged ``upload.getFile`` API. Telethon's
``download_media`` walks it with one request in flight, so throughput is capped
by a single round trip - on a 3 GiB file that is the difference between a few
minutes and the 40 minutes the ETA in the bug report showed.

Every ``upload.getFile`` is an independent, idempotent read, so several ranges
can be requested concurrently and written into the same pre-allocated file.
That is what this module does. It is deliberately conservative:

* the split is aligned to the request size Telethon already validates against;
* every worker is bounded, and the total is verified before the file is used;
* any failure (including a Telegram flood wait) falls back to the ordinary
  single-stream ``download_media`` path, so behaviour can never get worse than
  before;
* cancellation is checked between requests.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path

log = logging.getLogger("media-tools")

# upload.getFile returns at most 512 KiB per request and the offset must be a
# multiple of 4 KiB. This is Telethon's own maximum request size.
REQUEST_SIZE = 512 * 1024
MIN_CHUNK_SIZE = 4096
# More than a handful of parallel reads stops helping and starts to look like
# abuse to Telegram.
MAX_PARTS = 8


class MultipartFailed(Exception):
    """The ranged download did not complete; the caller must use the fallback."""


# ``os.pwrite`` writes at an offset without moving the file position, which is
# what lets the workers share one descriptor. It does not exist on Windows, so
# the fallback seeks and writes in the same (await-free) step.
_HAS_PWRITE = hasattr(os, "pwrite")


def _write_at(fd: int, data: bytes, offset: int) -> None:
    if _HAS_PWRITE:
        os.pwrite(fd, data, offset)
    else:  # pragma: no cover - Windows only
        os.lseek(fd, offset, os.SEEK_SET)
        os.write(fd, data)


def plan_parts(file_size: int, parts: int, request_size: int = REQUEST_SIZE) -> list[tuple[int, int]]:
    """Split ``file_size`` into ``(offset, length)`` ranges for parallel reads.

    Offsets are multiples of ``request_size`` because Telegram only serves
    aligned ranges; every worker therefore gets the same number of requests, so
    no straggler decides how long the whole transfer takes.
    """
    if file_size <= 0 or parts <= 1:
        return [(0, max(0, file_size))]
    request_size = max(MIN_CHUNK_SIZE, request_size - request_size % MIN_CHUNK_SIZE)
    requests_total = (file_size + request_size - 1) // request_size
    parts = min(parts, requests_total, MAX_PARTS)
    if parts <= 1:
        return [(0, file_size)]
    # Spread the remainder instead of giving the last worker a short range:
    # the slowest worker decides the total time, so they must be even.
    base, extra = divmod(requests_total, parts)
    ranges: list[tuple[int, int]] = []
    first = 0
    for index in range(parts):
        count = base + (1 if index < extra else 0)
        ranges.append((first * request_size, count * request_size))
        first += count
    return ranges


async def download_ranged(
    client,
    media,
    dest: Path,
    file_size: int,
    *,
    parts: int = 4,
    request_size: int = REQUEST_SIZE,
    progress=None,
    cancel_event: asyncio.Event | None = None,
) -> Path:
    """Download ``media`` into ``dest`` using several concurrent ranges.

    Raises :class:`MultipartFailed` on any problem, including a short or
    oversized result, so the caller can retry with the standard path.
    """
    ranges = plan_parts(file_size, parts, request_size)
    if len(ranges) <= 1:
        raise MultipartFailed("file is too small for a ranged download")

    request_size = max(MIN_CHUNK_SIZE, request_size - request_size % MIN_CHUNK_SIZE)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Truncate up front: each worker pwrites into its own region, so the file
    # is never extended by a racing writer and gaps read as zeros.
    fd = os.open(dest, os.O_RDWR | os.O_CREAT, 0o644)
    # Shared by the workers: the aggregate byte count behind the progress bar.
    written = 0
    lock = asyncio.Lock()

    async def worker(offset: int, length: int) -> None:
        nonlocal written
        got = 0
        limit = max(1, length // request_size)
        stream = client.iter_download(
            media,
            offset=offset,
            limit=limit,
            chunk_size=request_size,
            request_size=request_size,
            file_size=file_size,
        )
        async for chunk in stream:
            if cancel_event is not None and cancel_event.is_set():
                raise asyncio.CancelledError
            data = bytes(chunk)
            if not data:
                break
            _write_at(fd, data, offset + got)
            got += len(data)
            async with lock:
                written += len(data)
                total = written
            if progress is not None:
                await progress(total, file_size)
            if got >= length:
                break

    tasks = [asyncio.create_task(worker(offset, length)) for offset, length in ranges]
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        for task in tasks:
            task.cancel()
        with contextlib.suppress(Exception):
            await asyncio.gather(*tasks, return_exceptions=True)
        raise
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)

    expected = min(file_size, sum(length for _, length in ranges))
    if written != expected:
        raise MultipartFailed(f"expected {expected} bytes, received {written}")
    if dest.stat().st_size != file_size:
        raise MultipartFailed(f"expected {file_size} bytes on disk, found {dest.stat().st_size}")
    return dest


async def download_media_multipart(
    client,
    message,
    dest: Path,
    *,
    file_size: int,
    parts: int = 4,
    progress=None,
    cancel_event: asyncio.Event | None = None,
    min_bytes: int = 0,
) -> Path:
    """Try a ranged download, then the plain single-stream path.

    ``parts <= 1``, a small file or a missing size all skip straight to
    ``download_media``; so does any ranged failure.
    """
    media = getattr(message, "media", None)
    eligible = parts > 1 and file_size > 0 and file_size >= max(1, min_bytes) and media is not None
    if eligible:
        try:
            return await download_ranged(
                client, media, dest, file_size,
                parts=parts, progress=progress, cancel_event=cancel_event,
            )
        except asyncio.CancelledError:
            # The ranged downloader pre-allocates the whole file up front, so a
            # cancelled transfer leaves a near-full-size partial file behind.
            # Nothing else removes it - the queue only unlinks ``item.path``,
            # which is never assigned on a cancelled transfer - and repeated
            # cancels would silently eat the disk.
            with contextlib.suppress(OSError):
                dest.unlink()
            raise
        except MultipartFailed as exc:
            log.info("ranged download failed (%s); using a single stream", exc)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("ranged download errored (%s); using a single stream", exc)
        with contextlib.suppress(OSError):
            dest.unlink()
    # ``download_media`` returns the path as a string; callers want a Path.
    await client.download_media(message, file=str(dest))
    return dest
