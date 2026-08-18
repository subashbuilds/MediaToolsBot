from __future__ import annotations

import aiohttp


async def unshorten(url: str) -> str:
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": "Mozilla/5.0"}) as s:
        try:
            async with s.head(url, allow_redirects=True) as r:
                return str(r.url)
        except Exception:
            async with s.get(url, allow_redirects=True) as r:
                return str(r.url)


async def shorten(url: str) -> str:
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.get("https://tinyurl.com/api-create.php", params={"url": url}) as r:
            r.raise_for_status()
            return (await r.text()).strip()
