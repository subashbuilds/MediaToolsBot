from __future__ import annotations

import time


def bar(percent: float, width: int = 12) -> str:
    percent = max(0.0, min(100.0, percent))
    filled = round(width * percent / 100)
    return "█" * filled + "░" * (width - filled)


def render(label: str, done: int, total: int | None, started: float) -> str:
    elapsed = max(time.monotonic() - started, 0.001)
    speed = done / elapsed
    if total:
        pct = done * 100 / total
        return f"{label}\n[{bar(pct)}] {pct:.1f}%\n{done/1024/1024:.2f} MiB / {total/1024/1024:.2f} MiB · {speed/1024/1024:.2f} MiB/s"
    return f"{label}\n{done/1024/1024:.2f} MiB · {speed/1024/1024:.2f} MiB/s"
