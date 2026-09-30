from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..utils.files import esc, format_bytes, progress_bar

QUEUED = "queued"
DOWNLOADING = "downloading"
DONE = "done"
FAILED = "failed"

_ids = itertools.count(1)
MAX_VISIBLE_ITEMS = 12


@dataclass
class QueueItem:
    """One user-supplied input (Telegram media or an HTTP(S) URL)."""

    kind: str  # "telegram" | "url"
    label: str
    chat_id: int
    message_id: int | None = None
    url: str | None = None
    suggested_name: str | None = None
    expected_size: int = 0
    # The originating Telethon message is kept so a background worker can
    # download it later without a second get_messages round trip.
    message: object | None = None
    item_id: int = field(default_factory=lambda: next(_ids))
    status: str = QUEUED
    current: int = 0
    total: int = 0
    error: str | None = None
    path: Path | None = None
    started_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    # Container metadata fetched up front for links, so the audio/video track
    # menus have real entries while the file is still transferring.
    streams: list = field(default_factory=list)
    duration: float | None = None
    probed: bool = False
    # Background remote-metadata probe, cancelled with the download.
    probe_task: object | None = None

    @property
    def audio_count(self) -> int:
        return sum(1 for s in self.streams if s.get("codec_type") == "audio")

    @property
    def video_count(self) -> int:
        return sum(1 for s in self.streams if s.get("codec_type") == "video")

    @property
    def key(self) -> str:
        """Stable identity used to ignore the same file arriving twice."""
        if self.kind == "url":
            return f"url:{self.url}"
        return f"tg:{self.chat_id}:{self.message_id}"

    @property
    def percent(self) -> float:
        if self.status == DONE:
            return 100.0
        if self.total > 0:
            return min(100.0, self.current / self.total * 100.0)
        return 0.0

    @property
    def eta(self) -> float | None:
        if self.status != DOWNLOADING or self.total <= 0 or self.current <= 0:
            return None
        elapsed = max(0.001, time.monotonic() - self.started_at)
        speed = self.current / elapsed
        if speed <= 0:
            return None
        return max(0.0, (self.total - self.current) / speed)

    @property
    def display_name(self) -> str:
        if self.label:
            return self.label
        if self.url:
            return self.url.rsplit("/", 1)[-1] or self.url
        return "media"


class DownloadQueue:
    """Per-user queue of pending and completed downloads.

    Downloading happens in the background so the action menu is available the
    moment a file or link arrives, and several items can transfer in parallel.
    """

    def __init__(self, max_parallel: int = 3):
        self.items: list[QueueItem] = []
        self.max_parallel = max(1, int(max_parallel))
        self.active = 0
        self.paused = False

    # -- membership ----------------------------------------------------
    def __contains__(self, key: str) -> bool:
        return any(i.key == key for i in self.items)

    def __len__(self) -> int:
        return len(self.items)

    def get(self, item_id: int) -> QueueItem | None:
        return next((i for i in self.items if i.item_id == item_id), None)

    def add(self, item: QueueItem, allow_duplicate: bool = True) -> QueueItem:
        if not allow_duplicate:
            existing = next((i for i in self.items if i.key == item.key), None)
            if existing is not None:
                return existing
        self.items.append(item)
        return item

    # -- selection -----------------------------------------------------
    def pending(self) -> list[QueueItem]:
        return [i for i in self.items if i.status == QUEUED]

    def running(self) -> list[QueueItem]:
        return [i for i in self.items if i.status == DOWNLOADING]

    def finished(self) -> list[QueueItem]:
        return [i for i in self.items if i.status == DONE]

    def failed(self) -> list[QueueItem]:
        return [i for i in self.items if i.status == FAILED]

    def has_work(self) -> bool:
        return bool(self.pending() or self.running())

    def ready_paths(self) -> list[Path]:
        """Files that finished downloading, oldest first, without duplicates."""
        seen: set[str] = set()
        result: list[Path] = []
        for item in self.items:
            if item.status != DONE or item.path is None:
                continue
            if not item.path.exists():
                continue
            key = str(item.path.resolve())
            if key in seen:
                continue
            seen.add(key)
            result.append(item.path)
        return result

    def claim(self) -> QueueItem | None:
        """Mark and return the next item to download, honouring the limit."""
        if self.paused or self.active >= self.max_parallel:
            return None
        item = next((i for i in self.items if i.status == QUEUED), None)
        if item is None:
            return None
        item.status = DOWNLOADING
        item.started_at = time.monotonic()
        self.active += 1
        return item

    def release(self, item: QueueItem) -> None:
        if item.status == DOWNLOADING:
            self.active = max(0, self.active - 1)
        item.status = DONE if item.path is not None else FAILED
        item.finished_at = time.monotonic()

    def mark_failed(self, item: QueueItem, error: str) -> None:
        self.release(item)
        item.status = FAILED
        item.error = error

    # -- bulk actions --------------------------------------------------
    def clear(self) -> None:
        self.items.clear()
        self.active = 0
        self.paused = False

    def drop_finished(self) -> None:
        self.items = [i for i in self.items if i.status in (QUEUED, DOWNLOADING)]

    def summary(self) -> tuple[int, int, int, int]:
        """(total, done, active, failed) counts for the header line."""
        return (len(self.items), len(self.finished()), len(self.running()), len(self.failed()))


def _item_line(item: QueueItem) -> str:
    name = esc(item.display_name[:44])
    # Surface the track count as soon as metadata is known, so the user can
    # tell a multi-angle video from a single-track file before it finishes.
    if item.streams and item.status in (DOWNLOADING, QUEUED):
        bits = []
        if item.video_count:
            bits.append(f"{item.video_count}v")
        if item.audio_count:
            bits.append(f"{item.audio_count}a")
        if bits:
            name += f" <i>[{'+'.join(bits)}]</i>"
    if item.status == DONE:
        size = format_bytes(item.path.stat().st_size) if item.path and item.path.exists() else "ready"
        return f"✅ <b>{name}</b> — {size}"
    if item.status == FAILED:
        return f"❌ <b>{name}</b> — {esc((item.error or 'failed')[:80])}"
    if item.status == DOWNLOADING:
        bar = progress_bar(int(item.current), int(item.total))
        if item.total:
            text = f"{bar} {item.percent:5.1f}%  {format_bytes(item.current)} / {format_bytes(item.total)}"
            if item.eta is not None:
                text += f"  • ETA {int(item.eta)}s"
        else:
            text = f"{bar} {format_bytes(item.current)}"
        return f"⬇️ <b>{name}</b>\n    {text}"
    return f"⏳ <b>{name}</b> — queued"


def render_queue(queue: DownloadQueue, active_name: str | None = None, bulk: bool = True) -> str:
    """Build the HTML queue panel shown while files transfer."""
    total, done, running, failed = queue.summary()
    lines = ["📥 <b>Bulk Mode</b>" if bulk else "📥 <b>Downloads</b>", ""]
    if total == 0:
        lines.append("<i>Nothing queued yet.</i>")
    else:
        lines.append(f"<b>Queued:</b> {total}   <b>Ready:</b> {done}   <b>Downloading:</b> {running}" + (f"   <b>Failed:</b> {failed}" if failed else ""))
        lines.append("")
        for item in queue.items[:MAX_VISIBLE_ITEMS]:
            lines.append(_item_line(item))
        if total > MAX_VISIBLE_ITEMS:
            lines.append(f"<i>… and {total - MAX_VISIBLE_ITEMS} more</i>")
    if active_name:
        lines += ["", f"📁 <b>Active file:</b> {esc(active_name[:70])}"]
    if queue.has_work():
        lines += ["", "<i>Downloads continue in the background — you can already pick an action below.</i>"]
    elif done:
        lines += ["", "➕ <b>Send another file or URL to add more</b>, or pick an action below 👇"]
    return "\n".join(lines)


def render_bulk_prompt(queue: DownloadQueue, active: Path | None = None) -> str:
    """The 'send more files?' prompt shown after each file in bulk mode."""
    total, done, running, failed = queue.summary()
    lines = ["✅ <b>File received</b>", ""]
    if active is not None:
        try:
            size = format_bytes(active.stat().st_size)
        except OSError:
            size = "unknown size"
        lines.append(f"📁 <b>{esc(active.name[:70])}</b> — {size}")
    lines.append(f"<b>Files ready:</b> {done}" + (f"   <b>Downloading:</b> {running}" if running else ""))
    if failed:
        lines.append(f"<b>Failed:</b> {failed}")
    lines += [
        "",
        "➕ <b>Send another file or link to add more</b> — the menu updates instantly and "
        "downloads keep running in the background.",
        "",
        "Press <b>Done Adding</b> when you are finished, or choose an action below 👇",
    ]
    return "\n".join(lines)
