from __future__ import annotations

import json
import subprocess
from pathlib import Path


def probe(path: Path) -> dict:
    cp = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
    )
    return json.loads(cp.stdout)


def stream_label(stream: dict) -> str:
    typ = stream.get("codec_type", "unknown").title()
    tags = stream.get("tags", {}) or {}
    lang = tags.get("language", "und")
    codec = stream.get("codec_name", "unknown")
    title = tags.get("title", "")
    details = []
    if stream.get("width") and stream.get("height"):
        details.append(f"{stream['width']}x{stream['height']}")
    if stream.get("channels"):
        details.append(f"{stream['channels']}ch")
    extra = " - " + " ".join([*details, title]).strip() if details or title else ""
    return f"{stream.get('index', '?')} - {typ} - {lang} - {codec}{extra}"


def stream_packet_sizes(path: Path) -> dict[int, int]:
    """Return exact muxed packet payload bytes per stream index.

    This is intentionally only called by the explicit Media Information
    action. ffprobe has to inspect packets to calculate exact payload sizes,
    which is more expensive than normal stream probing.
    """
    import subprocess
    cp = subprocess.run(
        ["ffprobe", "-v", "error", "-show_packets", "-show_entries", "packet=stream_index,size", "-of", "csv=p=0", str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True,
    )
    result: dict[int, int] = {}
    for line in cp.stdout.splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) != 2:
            continue
        try:
            idx = int(parts[0]); size = int(parts[1])
        except ValueError:
            continue
        result[idx] = result.get(idx, 0) + max(0, size)
    return result
