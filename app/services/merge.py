from __future__ import annotations

import subprocess
from pathlib import Path

from .ffprobe import probe


def merge_tracks(inputs: list[Path], output: Path) -> Path:
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
    args += ["-map_metadata", "0", "-c", "copy", "-max_interleave_delta", "0", str(output)]
    try:
        subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(exc.stderr[-4000:] or "FFmpeg merge failed") from exc
    return output
