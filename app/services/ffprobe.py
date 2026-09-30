from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

# ffprobe is invoked from the asyncio event loop in several places. Without a
# timeout a corrupt or huge container can hang the process forever and take the
# whole bot down, so every subprocess call is bounded.
PROBE_TIMEOUT = int(os.environ.get("FFPROBE_TIMEOUT", "120"))
PACKET_SCAN_TIMEOUT = int(os.environ.get("FFPROBE_PACKET_TIMEOUT", "600"))

_cache: dict[tuple[str, int, int], tuple[float, dict]] = {}
_cache_lock = threading.Lock()
_CACHE_TTL = 30.0
_CACHE_MAX = 256


def _run(args: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        check=True,
        timeout=timeout,
    )


def _cache_get(key: tuple[str, int, int]) -> dict | None:
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        stamp, value = entry
        if time.monotonic() - stamp > _CACHE_TTL:
            _cache.pop(key, None)
            return None
        return value


def _cache_put(key: tuple[str, int, int], value: dict) -> None:
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX:
            # Cheap bounded eviction: drop the oldest entries first.
            for stale in sorted(_cache, key=lambda k: _cache[k][0])[: _CACHE_MAX // 2]:
                _cache.pop(stale, None)
        _cache[key] = (time.monotonic(), value)


def probe(path: Path) -> dict:
    """Return the ffprobe JSON for ``path`` (raises on failure, as before)."""
    key = (str(path), 0, 0)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    cp = _run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        PROBE_TIMEOUT,
    )
    data = json.loads(cp.stdout or "{}")
    _cache_put(key, data)
    return data


def safe_probe(path: Path) -> dict:
    """Non-raising probe used by menu rendering and background tasks.

    Rendering a menu must never raise because the user uploaded a file that is
    not decodable media, or because ffprobe is missing on the host.
    """
    try:
        return probe(path)
    except FileNotFoundError:
        return {}
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError, OSError, ValueError):
        return {}
    except Exception:
        return {}


def invalidate(path: Path) -> None:
    with _cache_lock:
        _cache.pop((str(path), 0, 0), None)


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
    cp = _run(
        ["ffprobe", "-v", "error", "-show_packets", "-show_entries", "packet=stream_index,size", "-of", "csv=p=0", str(path)],
        PACKET_SCAN_TIMEOUT,
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
