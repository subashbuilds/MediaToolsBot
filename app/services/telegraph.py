from __future__ import annotations

import json
from html import escape

import aiohttp


API = "https://api.telegra.ph"


def _nodes_for_info(data: dict, filename: str, size_text: str) -> list[dict]:
    fmt = data.get("format", {}) or {}
    streams = data.get("streams", []) or []
    nodes: list[dict] = [
        {"tag": "h3", "children": [filename]},
        {"tag": "p", "children": [f"Size: {size_text}"]},
        {"tag": "p", "children": [f"Format: {fmt.get('format_long_name') or fmt.get('format_name') or 'Unknown'}"]},
    ]
    duration = fmt.get("duration")
    if duration:
        nodes.append({"tag": "p", "children": [f"Duration: {duration} seconds"]})
    bitrate = fmt.get("bit_rate")
    if bitrate:
        nodes.append({"tag": "p", "children": [f"Overall bitrate: {bitrate} bps"]})
    nodes.append({"tag": "hr"})
    nodes.append({"tag": "h3", "children": ["Streams"]})
    for s in streams:
        tags = s.get("tags", {}) or {}
        disp = s.get("disposition", {}) or {}
        parts = [
            f"#{s.get('index', '?')} {str(s.get('codec_type', 'unknown')).title()}",
            f"Codec: {s.get('codec_name') or 'unknown'}",
            f"Language: {tags.get('language', 'und')}",
        ]
        if tags.get("title"):
            parts.append(f"Title: {tags['title']}")
        if s.get("width") and s.get("height"):
            parts.append(f"Resolution: {s['width']}x{s['height']}")
        if s.get("r_frame_rate") and s.get("r_frame_rate") != "0/0":
            parts.append(f"FPS: {s['r_frame_rate']}")
        if s.get("channels"):
            parts.append(f"Channels: {s['channels']}")
        if s.get("channel_layout"):
            parts.append(f"Layout: {s['channel_layout']}")
        if s.get("sample_rate"):
            parts.append(f"Sample rate: {s['sample_rate']} Hz")
        if s.get("bit_rate"):
            parts.append(f"Bitrate: {s['bit_rate']} bps")
        flags = []
        if disp.get("default"):
            flags.append("default")
        if disp.get("forced"):
            flags.append("forced")
        if flags:
            parts.append("Flags: " + ", ".join(flags))
        nodes.append({"tag": "p", "children": [" • ".join(parts)]})
    return nodes


async def create_info_page(data: dict, filename: str, size_text: str, access_token: str | None = None) -> str:
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
        content = _nodes_for_info(data, filename, size_text)
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
