from __future__ import annotations

import aiohttp
from urllib.parse import quote


async def shorten(url: str) -> str:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
        async with s.get("https://is.gd/create.php", params={"format": "simple", "url": url}) as r:
            text = (await r.text()).strip()
            if r.status != 200 or not text.startswith("http"):
                raise RuntimeError(text[:300])
            return text


async def unshorten(url: str) -> str:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
        async with s.get(url, allow_redirects=True) as r:
            return str(r.url)
