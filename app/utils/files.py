from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path


def safe_filename(name: str, fallback: str = "file.bin") -> str:
    name = os.path.basename(name or fallback).replace("\x00", "")
    name = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", name).strip(" .")
    return (name[:240] or fallback)


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


def progress_bar(current: int, total: int, width: int = 14) -> str:
    if total <= 0:
        return "░" * width
    ratio = max(0.0, min(1.0, current / total))
    filled = int(round(width * ratio))
    return "█" * filled + "░" * (width - filled)


def which_any(names: tuple[str, ...]) -> str | None:
    for name in names:
        p = shutil.which(name)
        if p:
            return p
    return None


def run_checked(args: list[str], timeout: int | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=True, text=True)
