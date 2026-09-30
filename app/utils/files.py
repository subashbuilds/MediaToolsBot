from __future__ import annotations

import html
import os
import re
import shutil
import subprocess
from pathlib import Path


def safe_filename(name: str, fallback: str = "file.bin") -> str:
    name = os.path.basename(name or fallback).replace("\x00", "")
    name = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", name).strip(" .")
    return (name[:240] or fallback)


def esc(value: object) -> str:
    """Escape a value for Telegram HTML parse mode.

    Every externally sourced string (file names, FFmpeg/GoFile error text, URLs)
    must pass through this before being embedded in a parsed message, otherwise
    a single ``<`` or ``&`` in a file name makes Telegram reject the whole
    edit and the user never sees the message at all.
    """
    return html.escape(str(value if value is not None else ""), quote=False)


def truncate(value: str, limit: int = 900) -> str:
    """Shorten tool output so it always fits inside a Telegram message."""
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: limit - 20].rstrip() + "\n… (truncated)"


def error_text(exc: BaseException, limit: int = 600) -> str:
    """Render an exception as safe, bounded text for a Telegram message."""
    return truncate(f"{type(exc).__name__}: {exc}", limit)


def unique_path(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / safe_filename(name)
    if not p.exists():
        return p
    stem, suffix = p.stem, p.suffix
    for i in range(1, 100000):
        q = directory / f"{stem}.{i}{suffix}"
        if not q.exists():
            return q
    raise RuntimeError("Could not create a unique output path")


def human_time(seconds: float | None) -> str:
    return format_duration(seconds)


def format_bytes(n: int | float) -> str:
    n = float(n)
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    for unit in units:
        if abs(n) < 1024 or unit == units[-1]:
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} PiB"


def format_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "--:--"
    s = int(seconds)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def format_bitrate(bits_per_second: int | float | None) -> str:
    """Format a bitrate as human-readable decimal bits per second."""
    if bits_per_second is None:
        return "N/A"
    n = float(bits_per_second)
    if n < 1000:
        return f"{n:.0f} bps"
    units = ("kb/s", "Mb/s", "Gb/s", "Tb/s")
    n /= 1000.0
    for unit in units:
        if n < 1000 or unit == units[-1]:
            return f"{n:.2f} {unit}"
        n /= 1000.0
    return f"{n:.2f} Tb/s"


def progress_bar(current: int, total: int, width: int = 14) -> str:
    if total <= 0:
        return "░" * width
    ratio = max(0.0, min(1.0, current / total))
    filled = int(round(width * ratio))
    return "█" * filled + "░" * (width - filled)


def dir_size(path: Path) -> int:
    total = 0
    try:
        for item in path.rglob("*"):
            try:
                if item.is_file():
                    total += item.stat().st_size
            except OSError:
                continue
    except OSError:
        return 0
    return total


def which_any(names: tuple[str, ...]) -> str | None:
    for name in names:
        p = shutil.which(name)
        if p:
            return p
    return None


def run_checked(args: list[str], timeout: int | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=True, text=True)
