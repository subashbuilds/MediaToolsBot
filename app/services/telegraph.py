from __future__ import annotations

import json
from html import escape

from ..utils.files import format_bitrate, format_duration, format_bytes

import aiohttp


API = "https://api.telegra.ph"


def _nodes_for_info(data: dict, filename: str, size_text: str, packet_sizes: dict[int, int] | None = None) -> list[dict]:
    fmt = data.get("format", {}) or {}
    streams = data.get("streams", []) or []
    packet_sizes = packet_sizes or {}
    nodes: list[dict] = [
        {"tag": "h3", "children": [filename]},
        {"tag": "p", "children": [f"Size: {size_text}"]},
        {"tag": "p", "children": [f"Format: {fmt.get('format_long_name') or fmt.get('format_name') or 'Unknown'}"]},
    ]
    duration = fmt.get("duration")
    if duration:
        try:
            duration_f = float(duration)
            nodes.append({"tag": "p", "children": [f"Duration: {format_duration(duration_f)} ({duration_f:.3f} s)"]})
        except (TypeError, ValueError):
            nodes.append({"tag": "p", "children": [f"Duration: {duration}"]})
    bitrate = fmt.get("bit_rate")
    if bitrate:
        try:
            nodes.append({"tag": "p", "children": [f"Overall bitrate: {format_bitrate(int(bitrate))}"]})
        except (TypeError, ValueError):
            nodes.append({"tag": "p", "children": [f"Overall bitrate: {bitrate} bps"]})
    nodes.append({"tag": "hr"})
    nodes.append({"tag": "h3", "children": ["Streams"]})
    for s in streams:
        tags = s.get("tags", {}) or {}
        disp = s.get("disposition", {}) or {}
        parts = [
            f"#{s.get('index', '?')} {str(s.get('codec_type', 'unknown')).title()}",
            f"Codec: {s.get('codec_name') or 'unknown'}",
        ]
        if s.get("codec_long_name"): parts.append(f"Codec name: {s['codec_long_name']}")
        if s.get("profile"): parts.append(f"Profile: {s['profile']}")
        parts.append(f"Language: {tags.get('language', 'und')}")
        if tags.get("title"): parts.append(f"Title: {tags['title']}")
        if s.get("width") and s.get("height"): parts.append(f"Resolution: {s['width']}x{s['height']}")
        for key, label in (("pix_fmt", "Pixel format"), ("bits_per_raw_sample", "Bit depth"), ("r_frame_rate", "FPS"), ("sample_aspect_ratio", "SAR"), ("color_space", "Color space"), ("color_transfer", "Transfer"), ("color_primaries", "Primaries"), ("channels", "Channels"), ("channel_layout", "Layout"), ("sample_rate", "Sample rate")):
            if s.get(key) and s.get(key) not in {"0/0", "0:1"}:
                value = f"{s[key]} Hz" if key == "sample_rate" else s[key]
                parts.append(f"{label}: {value}")
        if s.get("bit_rate"):
            try:
                parts.append(f"Bitrate: {format_bitrate(int(s['bit_rate']))}")
            except (TypeError, ValueError):
                parts.append(f"Bitrate: {s['bit_rate']} bps")
        exact = packet_sizes.get(int(s["index"])) if str(s.get("index", "")).isdigit() else None
        if exact is not None:
            parts.append(f"Packet payload size: {format_bytes(exact)}")
        flags = [name for name in ("default", "forced", "hearing_impaired", "visual_impaired", "original") if disp.get(name)]
        if flags: parts.append("Flags: " + ", ".join(flags))
        nodes.append({"tag": "p", "children": [" • ".join(parts)]})
    return nodes


async def create_info_page(data: dict, filename: str, size_text: str, access_token: str | None = None, packet_sizes: dict[int, int] | None = None) -> str:
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        token = access_token
        if not token:
            payload = {"short_name": "MediaToolsBot", "author_name": "Media Tools Bot"}
            async with session.post(f"{API}/createAccount", data=payload) as resp:
                obj = await resp.json(content_type=None)
                if not obj.get("ok"):
                    raise RuntimeError(f"Telegraph account creation failed: {obj.get('error', obj)}")
                token = obj["result"]["access_token"]
        content = _nodes_for_info(data, filename, size_text, packet_sizes)
        payload = {
            "access_token": token,
            "title": f"Media Information - {filename[:220]}",
            "author_name": "Media Tools Bot",
            "content": json.dumps(content, ensure_ascii=False),
            "return_content": "false",
        }
        async with session.post(f"{API}/createPage", data=payload) as resp:
            obj = await resp.json(content_type=None)
            if not obj.get("ok"):
                raise RuntimeError(f"Telegraph page creation failed: {obj.get('error', obj)}")
            return obj["result"]["url"]
