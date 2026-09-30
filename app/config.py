from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# 2 GiB of RAM per concurrent FFmpeg job is a conservative rule of thumb for
# 1080p transcodes. The FFmpeg semaphore is sized from the container limit when
# /sys/fs/cgroup is readable so a small VPS does not get OOM-killed.
DEFAULT_FFMPEG_JOBS = 2
# 0 means "no artificial ceiling". The 2 GiB Bot API upload limit applies only
# to *sending* to Telegram, never to downloading, so downloads are unrestricted
# unless the operator opts in with MAX_DOWNLOAD_MB. Disk exhaustion is still
# guarded at request time by a free-space check in the downloader.
DEFAULT_MAX_DOWNLOAD_MB = 0


def _int(name: str, default: int) -> int:
    """Parse an integer env var, falling back to the default when unusable.

    A malformed value must never take the whole bot down at start-up; the
    operator gets the default and a log-friendly fallback instead.
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(float(raw.strip()))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except (TypeError, ValueError):
        return default


def _str_ids(name: str) -> frozenset[int]:
    """Parse a comma separated list of numeric ids, skipping bad entries."""
    result: set[int] = set()
    for item in (os.getenv(name, "") or "").replace(";", ",").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            result.add(int(item))
        except ValueError:
            continue
    return frozenset(result)


def _mem_based_ffmpeg_jobs(requested: int) -> int:
    """Clamp the FFmpeg worker count to what the container can actually hold."""
    try:
        raw = Path("/sys/fs/cgroup/memory.max").read_text().strip()
        limit = int(raw) if raw != "max" else 0
    except (OSError, ValueError):
        try:
            raw = Path("/proc/meminfo").read_text()
            for line in raw.splitlines():
                if line.startswith("MemTotal:"):
                    limit = int(line.split()[1]) * 1024
                    break
        except (OSError, ValueError, IndexError):
            limit = 0
    if limit <= 0:
        return max(1, requested)
    affordable = max(1, int(limit / (2 * 1024**3)))
    return max(1, min(requested, affordable))


def _detect_public_base_url() -> str | None:
    """Resolve the public origin used by direct/stream links.

    Explicit PUBLIC_BASE_URL always wins. Common managed hosts expose their
    public URL through environment variables; supporting those makes the
    direct-link feature work without requiring a second configuration value.
    If none is available, a public URL cannot be invented safely.
    """
    explicit = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if explicit:
        return explicit
    render = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
    if render:
        return render
    railway = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip().rstrip("/")
    if railway:
        return railway if railway.startswith(("http://", "https://")) else f"https://{railway}"
    app_url = os.getenv("APP_URL", "").strip().rstrip("/")
    if app_url:
        return app_url
    heroku = os.getenv("HEROKU_APP_NAME", "").strip()
    if heroku:
        return f"https://{heroku}.herokuapp.com"
    return None


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    bot_token: str
    gofile_api_token: str | None
    download_dir: Path
    work_dir: Path
    db_path: Path
    max_concurrent_jobs: int
    max_ffmpeg_jobs: int
    max_download_mb: int
    max_parallel_downloads: int
    progress_interval: float
    sudo_users: frozenset[int]
    allowed_users: frozenset[int]
    public_base_url: str | None
    web_host: str
    web_port: int
    direct_link_ttl: int
    telegraph_access_token: str | None
    session_timeout: int
    reactions_enabled: bool
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Config":
        api_id = _int("API_ID", 0)
        api_hash = os.getenv("API_HASH", "").strip()
        bot_token = os.getenv("BOT_TOKEN", "").strip()
        if not api_id or not api_hash or not bot_token:
            raise RuntimeError("API_ID, API_HASH and BOT_TOKEN are required")
        ffmpeg_jobs = _mem_based_ffmpeg_jobs(max(1, _int("MAX_CONCURRENT_FFMPEG_JOBS", DEFAULT_FFMPEG_JOBS)))
        cfg = cls(
            api_id=api_id,
            api_hash=api_hash,
            bot_token=bot_token,
            gofile_api_token=os.getenv("GOFILE_API_TOKEN", "").strip() or None,
            download_dir=Path(os.getenv("DOWNLOAD_DIR", "/data/downloads")),
            work_dir=Path(os.getenv("WORK_DIR", "/data/work")),
            db_path=Path(os.getenv("DB_PATH", "/data/bot.sqlite3")),
            max_concurrent_jobs=max(1, _int("MAX_CONCURRENT_JOBS", 10)),
            max_ffmpeg_jobs=ffmpeg_jobs,
            max_download_mb=max(0, _int("MAX_DOWNLOAD_MB", DEFAULT_MAX_DOWNLOAD_MB)),
            max_parallel_downloads=max(1, min(8, _int("MAX_PARALLEL_DOWNLOADS", 3))),
            progress_interval=max(1.0, _float("PROGRESS_INTERVAL", 3)),
            sudo_users=_str_ids("SUDO_USERS"),
            allowed_users=_str_ids("ALLOWED_USERS"),
            public_base_url=_detect_public_base_url(),
            web_host=os.getenv("WEB_HOST", "0.0.0.0").strip(),
            web_port=_int("WEB_PORT", _int("PORT", 8080)),
            direct_link_ttl=max(300, _int("DIRECT_LINK_TTL", 86400)),
            telegraph_access_token=os.getenv("TELEGRAPH_ACCESS_TOKEN", "").strip() or None,
            session_timeout=max(60, _int("SESSION_TIMEOUT", 21600)),
            reactions_enabled=os.getenv("BOT_REACTIONS", "on").strip().lower() not in {"0", "off", "false", "no"},
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
        )
        cfg.download_dir.mkdir(parents=True, exist_ok=True)
        cfg.work_dir.mkdir(parents=True, exist_ok=True)
        cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
        return cfg
