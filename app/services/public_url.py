"""Resolve the public origin used by Direct/Stream links, with no configuration.

A direct link is only useful when it points at a host the user's browser can
reach. The explicit ``PUBLIC_BASE_URL`` always wins; when it is not set the bot
previously refused and the button looked broken. It now first asks what the
managed host reports about itself, and as a last resort uses the public egress
IP, so a plain VPS or Docker deployment works without any setting.
"""
from __future__ import annotations

import os
import time

import aiohttp

# The egress address belongs to the deployment, not to a job, so it is resolved
# once and reused for every link until it expires.
_CACHE_TTL = 600.0
_ip_cache: tuple[float, str | None] = (0.0, None)

_SERVERS = (
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
)


def from_env() -> str | None:
    """The public origin a hosting platform reports, if any."""
    for name in ("PUBLIC_BASE_URL", "PUBLIC_URL", "RENDER_EXTERNAL_URL", "APP_URL"):
        raw = (os.getenv(name) or "").strip().rstrip("/")
        if raw:
            return raw
    fly = (os.getenv("FLY_APP_NAME") or "").strip()
    if fly:
        return f"https://{fly}.fly.dev"
    koyeb = (os.getenv("KOYEB_APP_NAME") or "").strip()
    if koyeb:
        return f"https://{koyeb}.koyeb.app"
    railway = (os.getenv("RAILWAY_PUBLIC_DOMAIN") or "").strip().rstrip("/")
    if railway:
        return railway if railway.startswith(("http://", "https://")) else f"https://{railway}"
    heroku = (os.getenv("HEROKU_APP_NAME") or "").strip()
    if heroku:
        return f"https://{heroku}.herokuapp.com"
    slug = (os.getenv("REPL_SLUG") or "").strip()
    owner = (os.getenv("REPL_OWNER") or "").strip()
    if slug and owner:
        return f"https://{slug}.{owner}.repl.co"
    return None


def origin_from_ip(cfg, ip: str) -> str:
    port = int(getattr(cfg, "web_port", 8080) or 8080)
    suffix = "" if port in (80, 443) else f":{port}"
    return f"http://{ip}{suffix}"


async def public_ip(timeout: float = 8.0) -> str | None:
    """The deployment's public egress address, cached for ten minutes.

    A response is only accepted when it really looks like an IP address; a
    service that is gone, slow or behind a captive portal must not turn a
    nonsense string into the link the user receives. A failure is reported as
    ``None`` so the caller can say exactly what is missing instead of handing
    out a dead URL.
    """
    global _ip_cache
    at, cached = _ip_cache
    now = time.monotonic()
    if cached and now - at < _CACHE_TTL:
        return cached
    result: str | None = None
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
        for url in _SERVERS:
            try:
                async with session.get(url, headers={"User-Agent": "curl/8"}) as resp:
                    if resp.status != 200:
                        continue
                    text = (await resp.text()).strip()
                    if _looks_like_ip(text):
                        result = text
                        break
            except Exception:
                continue
    _ip_cache = (now, result)
    return result


def _looks_like_ip(text: str) -> bool:
    return 7 <= len(text) <= 45 and text.count(".") >= 3 and all(p for p in text.split("."))


async def detect_public_origin(cfg) -> str | None:
    """Return a usable public origin, or ``None`` when none can be found."""
    env = from_env()
    if env:
        return env
    ip = await public_ip()
    return origin_from_ip(cfg, ip) if ip else None


def clear_cache() -> None:
    """Forget the cached address (used by tests)."""
    global _ip_cache
    _ip_cache = (0.0, None)
