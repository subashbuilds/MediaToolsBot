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
