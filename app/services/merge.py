from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable

from .ffprobe import probe
from .process_control import run_command
from .ffmpeg import PROGRESS_ARGS


def merge_tracks(inputs: list[Path], output: Path, on_progress: Callable[[float], None] | None = None) -> Path:
    if len(inputs) < 2:
        raise ValueError("At least two media files are required")
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for path in inputs:
        args += ["-i", str(path)]
    for i, path in enumerate(inputs):
        streams = probe(path).get("streams", [])
        if not streams:
            raise ValueError(f"No streams found in {path.name}")
        for s in streams:
            args += ["-map", f"{i}:{s['index']}"]
    args += ["-map_metadata", "0", "-c", "copy", "-max_interleave_delta", "0"]
    if on_progress:
        args += list(PROGRESS_ARGS)
    args += [str(output)]
    try:
        run_command(args, text=True, on_progress=on_progress)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip()[-4000:] or "FFmpeg merge failed"
        raise RuntimeError(detail) from exc
    return output
