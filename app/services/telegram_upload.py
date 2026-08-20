from __future__ import annotations

import math
from pathlib import Path

MAX_TELEGRAM_CHUNK = int(1.95 * 1024**3)


def chunk_file(path: Path, outdir: Path, max_bytes: int = MAX_TELEGRAM_CHUNK) -> list[Path]:
    """Split an arbitrary file into <=max_bytes binary chunks for Telegram documents."""
    if path.stat().st_size <= max_bytes:
        return [path]
    outdir.mkdir(parents=True, exist_ok=True)
    total = math.ceil(path.stat().st_size / max_bytes)
    chunks: list[Path] = []
    with path.open("rb") as src:
        for idx in range(1, total + 1):
            out = outdir / f"{path.name}.part{idx:02d}"
            with out.open("wb") as dst:
                remaining = max_bytes
                while remaining:
                    block = src.read(min(8 * 1024 * 1024, remaining))
                    if not block:
                        break
                    dst.write(block)
                    remaining -= len(block)
            chunks.append(out)
    return chunks
