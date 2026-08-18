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

    def __init__(self, callback: Callback, interval: float = 3.0, progress_hook=None):
        self.callback = callback
        self.progress_hook = progress_hook
        self.interval = max(1.0, float(interval))
        self.last = 0.0
        self.last_text = ""
        self.started = time.monotonic()
        self.lock = asyncio.Lock()

    async def update(self, current: int, total: int, label: str, extra: str = "", force: bool = False) -> None:
        current = max(0, int(current or 0))
        total = max(0, int(total or 0))
        now = time.monotonic()
        if not force and total and current < total and now - self.last < self.interval:
            return
        async with self.lock:
            now = time.monotonic()
            if not force and total and current < total and now - self.last < self.interval:
                return
            elapsed = max(0.001, now - self.started)
            speed = current / elapsed
            pct = (current / total * 100) if total else 0.0
            eta = ((total - current) / speed) if total and speed > 0 else None
            if self.progress_hook:
                result = self.progress_hook(current, total, label)
                if asyncio.iscoroutine(result):
                    await result
            text = (
                f"{label}\n"
                f"{progress_bar(current, total)} {pct:5.1f}%\n"
                f"{format_bytes(current)} / {format_bytes(total) if total else 'Unknown'}\n"
                f"⚡ {format_bytes(speed)}/s"
            )
            if eta is not None and current < total:
                text += f"  •  ETA {format_duration(eta)}"
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
        await self.update(total, total, label, extra, force=True)
