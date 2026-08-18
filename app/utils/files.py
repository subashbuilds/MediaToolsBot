from __future__ import annotations

import mimetypes
import re
from pathlib import Path


def safe_filename(name: str, default: str = "file") -> str:
    name = Path(name or default).name
    name = re.sub(r"[\x00-\x1f<>:\"/\\|?*]", "_", name).strip(" .")
    return (name or default)[:240]


def human_size(value: int | float | None) -> str:
    if value is None:
        return "Unknown"
    value = float(value)
    units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]
    for unit in units:
        if abs(value) < 1024 or unit == units[-1]:
            return f"{value:.2f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.2f} PiB"


def mime_for(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"
