from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from .files import format_bytes, format_duration, progress_bar

Callback = Callable[[str], Awaitable[None] | None]


class ProgressReporter:
    """Rate-limited Telegram progress-message editor.

    The transfer callbacks can fire hundreds of times per second. Editing a
    Telegram message for every callback is both slow and likely to hit flood
    limits, so updates are throttled and duplicate text is suppressed.
    """

    def __init__(self, callback: Callback, interval: float = 3.0, progress_hook=None, on_finish=None):
        self.callback = callback
        self.progress_hook = progress_hook
        self.interval = max(0.5, float(interval))
        self.last = 0.0
        self.last_text = ""
        self.started = time.monotonic()
        self.lock = asyncio.Lock()
        self._finished = False
        self._on_finish = on_finish
        # FFmpeg progress arrives from a reader thread, so it is only recorded
        # here and converted into message edits by the owning coroutine.
        self._pending: tuple[int, int] | None = None

    def queue_progress(self, current: int, total: int) -> None:
        """Record a progress position. Safe to call from any thread."""
        self._pending = (max(0, int(current or 0)), max(0, int(total or 0)))

    def take_pending(self) -> tuple[int | None, int]:
        """Return and clear the last queued position."""
        pending = self._pending
        self._pending = None
        return pending if pending else (None, 0)

    def _should_emit(self, now: float, total: int, current: int, force: bool) -> bool:
        if force or not total or current >= total:
            return True
        # The previous guard required a known `total`, so a server that streams
        # without Content-Length edited the message on *every* chunk and
        # reliably tripped Telegram's flood-wait. Throttle unknown sizes too.
        return (now - self.last) >= self.interval

    async def update(self, current: int, total: int, label: str, extra: str = "", force: bool = False) -> None:
        current = max(0, int(current or 0))
        total = max(0, int(total or 0))
        if not self._should_emit(time.monotonic(), total, current, force):
            return
        async with self.lock:
            now = time.monotonic()
            if not self._should_emit(now, total, current, force):
                return
            elapsed = max(0.001, now - self.started)
            speed = current / elapsed
            pct = (current / total * 100) if total else 0.0
            eta = ((total - current) / speed) if total and speed > 0 else None
            if self.progress_hook:
                result = self.progress_hook(current, total, label)
                if asyncio.iscoroutine(result):
                    await result
            bar = progress_bar(current, total)
            if total:
                text = (
                    f"{label}\n"
                    f"{bar} {pct:5.1f}%\n"
                    f"{format_bytes(current)} / {format_bytes(total)}\n"
                    f"⚡ {format_bytes(speed)}/s"
                )
                if eta is not None and current < total:
                    text += f"  •  ETA {format_duration(eta)}"
            else:
                text = (
                    f"{label}\n"
                    f"{bar}\n"
                    f"{format_bytes(current)} downloaded\n"
                    f"⚡ {format_bytes(speed)}/s"
                )
            if extra:
                text += f"\n{extra}"
            if text == self.last_text and not force:
                return
            self.last = now
            self.last_text = text
            result = self.callback(text)
            if asyncio.iscoroutine(result):
                await result

    async def finish(self, total: int, label: str, extra: str = "") -> None:
        self._finished = True
        await self.update(total, total, label, extra, force=True)
        if self._on_finish:
            result = self._on_finish()
            if asyncio.iscoroutine(result):
                await result
