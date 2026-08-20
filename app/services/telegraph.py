from __future__ import annotations

import json
from html import escape

import aiohttp

from ..utils.files import format_bitrate, format_duration, format_bytes

API = "https://api.telegra.ph"


def _p(label: str, value: str) -> dict:
    return {"tag": "p", "children": [f"{label}: {value}"]}


def _nodes_for_info(data: dict, filename: str, size_text: str, packet_sizes: dict[int, int] | None = None) -> list[dict]:
    fmt = data.get("format", {}) or {}
    streams = data.get("streams", []) or []
    packet_sizes = packet_sizes or {}
    nodes: list[dict] = [
        {"tag": "h3", "children": [filename]},
        {"tag": "h4", "children": ["📦 Container"]},
        _p("Size", size_text),
        _p("Format", fmt.get("format_long_name") or fmt.get("format_name") or "Unknown"),
    ]
    duration = fmt.get("duration")
    if duration:
        try:
            duration_f = float(duration)
            nodes.append(_p("Duration", f"{format_duration(duration_f)} ({duration_f:.3f} s)"))
        except (TypeError, ValueError):
            nodes.append(_p("Duration", str(duration)))
    bitrate = fmt.get("bit_rate")
    if bitrate:
        try: nodes.append(_p("Overall bitrate", format_bitrate(int(bitrate))))
        except (TypeError, ValueError): nodes.append(_p("Overall bitrate", f"{bitrate} bps"))
    if fmt.get("nb_streams") is not None:
        nodes.append(_p("Streams", str(fmt.get("nb_streams"))))

    for s in streams:
        idx = s.get("index", "?")
        typ = str(s.get("codec_type", "unknown")).title()
        nodes.append({"tag": "h4", "children": [f"#{idx} — {typ}"]})
        tags = s.get("tags", {}) or {}
        disp = s.get("disposition", {}) or {}
        nodes.append(_p("Codec", s.get("codec_name") or "Unknown"))
        if s.get("codec_long_name"): nodes.append(_p("Codec name", s["codec_long_name"]))
        if s.get("profile"): nodes.append(_p("Profile", str(s["profile"])))
        nodes.append(_p("Language", str(tags.get("language", "und"))))
        if tags.get("title"): nodes.append(_p("Title", str(tags["title"])))
        if s.get("width") and s.get("height"): nodes.append(_p("Resolution", f"{s['width']} × {s['height']}"))
        if s.get("display_aspect_ratio"): nodes.append(_p("Aspect ratio", str(s["display_aspect_ratio"])))
        fields = (
            ("pix_fmt", "Pixel format"), ("bits_per_raw_sample", "Bit depth"),
            ("r_frame_rate", "Frame rate"), ("avg_frame_rate", "Average frame rate"),
            ("sample_aspect_ratio", "Sample aspect ratio"), ("color_space", "Color space"),
            ("color_transfer", "Color transfer"), ("color_primaries", "Color primaries"),
            ("channels", "Channels"), ("channel_layout", "Channel layout"),
        )
        for key, label in fields:
            value = s.get(key)
            if value and value not in {"0/0", "0:1"}: nodes.append(_p(label, str(value)))
        if s.get("sample_rate"):
            nodes.append(_p("Sample rate", f"{s['sample_rate']} Hz"))
        if s.get("bit_rate"):
            try: nodes.append(_p("Bitrate", format_bitrate(int(s["bit_rate"]))))
            except (TypeError, ValueError): nodes.append(_p("Bitrate", f"{s['bit_rate']} bps"))
        if s.get("start_time") is not None: nodes.append(_p("Start time", f"{s['start_time']} s"))
        if s.get("duration") is not None:
            try: nodes.append(_p("Stream duration", format_duration(float(s["duration"]))))
            except (TypeError, ValueError): pass
        exact = packet_sizes.get(int(idx)) if str(idx).isdigit() else None
        if exact is not None: nodes.append(_p("Packet payload", format_bytes(exact)))
        flags = [name for name in ("default", "forced", "hearing_impaired", "visual_impaired", "original") if disp.get(name)]
        if flags: nodes.append(_p("Flags", ", ".join(flags)))
        nodes.append({"tag": "hr"})
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
