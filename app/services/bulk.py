from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..utils.files import esc, format_bytes, format_bytes_short, format_eta, progress_bar

QUEUED = "queued"
DOWNLOADING = "downloading"
DONE = "done"
FAILED = "failed"

_ids = itertools.count(1)
MAX_VISIBLE_ITEMS = 12
# A transfer that has not delivered a byte for this long is reported as
# stalled rather than left looking like a healthy 0% bar.
STALL_AFTER = 45.0


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
    # menus have real entries before - or instead of - the full transfer.
    streams: list = field(default_factory=list)
    # The full ffprobe payload behind ``streams``; Media Information is built
    # from it so the media itself never has to be downloaded for it.
    probe_data: dict | None = None
    duration: float | None = None
    probed: bool = False
    # Background remote-metadata probe, cancelled with the download.
    probe_task: object | None = None
    # Smoothed transfer speed in bytes/second, kept up to date by ``advance``.
    speed: float = 0.0
    _last_current: int = 0
    _last_at: float | None = None

    def advance(self, current: int, total: int) -> None:
        """Record progress and update the smoothed speed.

        Averages taken over the whole transfer either lag badly or jump on the
        first chunk, so the speed is measured over the last interval and
        smoothed - that is the number a user reads as "how fast is this".
        """
        current = int(current or 0)
        now = time.monotonic()
        if self._last_at is not None and now > self._last_at:
            delta = current - self._last_current
            if delta > 0:
                instant = delta / (now - self._last_at)
                self.speed = instant if self.speed <= 0 else self.speed * 0.6 + instant * 0.4
        self._last_current = current
        self._last_at = now
        self.current = current
        self.total = int(total or 0)

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
        speed = self.speed
        if speed <= 0:
            # No interval measured yet: fall back to the overall average.
            elapsed = max(0.001, time.monotonic() - self.started_at)
            speed = self.current / elapsed
        if speed <= 0:
            return None
        return max(0.0, (self.total - self.current) / speed)

    @property
    def elapsed(self) -> float:
        """Seconds this transfer has been running."""
        return max(0.0, time.monotonic() - self.started_at)

    @property
    def idle_for(self) -> float:
        """Seconds since the transfer last delivered bytes.

        The download panel used to repaint only when the text changed, and a
        transfer that is not moving produces identical text. The card then
        stayed frozen on its first frame for as long as the wait lasted, which
        is indistinguishable from a dead bot. This is what turns that silence
        into an explicit "no data for ..." line.
        """
        if self.status != DOWNLOADING:
            return 0.0
        mark = self._last_at if self._last_at is not None else self.started_at
        return max(0.0, time.monotonic() - mark)

    @property
    def stalled(self) -> bool:
        return self.status == DOWNLOADING and self.idle_for >= STALL_AFTER

    @property
    def display_name(self) -> str:
        if self.label:
            return self.label
        if self.url:
            return self.url.rsplit("/", 1)[-1] or self.url
        return "media"


class DownloadQueue:
    """Per-user queue of waiting and completed downloads.

    Nothing is transferred when an input arrives. The queue is filled, the
    action menu is shown, and the worker only starts once the user picks an
    action - from then on several items can transfer in parallel.
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
        # The size is already known for a queued input, so the first frame can
        # say "0 B / 2.53 GiB". Waiting for the first byte to arrive left the
        # bar with no total and nothing for the user to judge the wait by.
        if not item.total:
            item.total = int(item.expected_size or 0)
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
            # Short for the running figure, exact for the total: the total is
            # fixed and worth the two decimals, the current one changes every
            # few seconds and would push the line into a wrap.
            sizes = f"{format_bytes_short(item.current)} / {format_bytes(item.total)}"
        else:
            sizes = f"{format_bytes_short(item.current)} downloaded"
        # Speed and ETA share the second line so the bar stays full width and
        # the line never wraps in the middle of the numbers.
        line = f"    {bar} {item.percent:5.1f}% · {sizes}"
        if item.speed > 0:
            line += f"\n    ⚡ {format_bytes_short(item.speed)}/s · ⏳ {format_eta(item.eta)} left"
        elif item.eta is not None:
            line += f"\n    ⏳ {format_eta(item.eta)} left"
        # How long this has been running, always. It changes on every repaint,
        # so the card keeps moving even while no bytes arrive.
        line += f"\n    ⏱ {format_eta(item.elapsed)} elapsed"
        if item.stalled:
            line += f" · ⚠️ no data for {format_eta(item.idle_for)}"
        return f"⬇️ <b>{name}</b>\n{line}"
    return f"⏳ <b>{name}</b> — ready to download"


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
    if running:
        lines += ["", "<i>Still downloading — your chosen action starts as soon as the transfer finishes.</i>"]
    elif queue.has_work():
        lines += ["", "<i>Nothing is downloading yet. The transfer starts when you pick an action below.</i>"]
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
        "nothing is downloaded until you choose what to do with a file.",
        "",
        "Press <b>Done Adding</b> when you are finished, or choose an action below 👇",
    ]
    return "\n".join(lines)
