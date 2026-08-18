from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    value = os.getenv(name, str(default)).strip()
    try:
        return int(value)
    except ValueError:
        raise RuntimeError(f"{name} must be an integer")


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
    progress_interval: float
    sudo_users: frozenset[int]
    public_base_url: str | None
    web_host: str
    web_port: int
    direct_link_ttl: int
    telegraph_access_token: str | None
    session_timeout: int

    @classmethod
    def from_env(cls) -> "Config":
        api_id = _int("API_ID", 0)
        api_hash = os.getenv("API_HASH", "").strip()
        bot_token = os.getenv("BOT_TOKEN", "").strip()
        if not api_id or not api_hash or not bot_token:
            raise RuntimeError("API_ID, API_HASH and BOT_TOKEN are required")
        sudo = set()
        for item in os.getenv("SUDO_USERS", "").split(","):
            item = item.strip()
            if item:
                sudo.add(int(item))
        cfg = cls(
            api_id=api_id,
            api_hash=api_hash,
            bot_token=bot_token,
            gofile_api_token=os.getenv("GOFILE_API_TOKEN", "").strip() or None,
            download_dir=Path(os.getenv("DOWNLOAD_DIR", "/data/downloads")),
            work_dir=Path(os.getenv("WORK_DIR", "/data/work")),
            db_path=Path(os.getenv("DB_PATH", "/data/bot.sqlite3")),
            max_concurrent_jobs=max(1, _int("MAX_CONCURRENT_JOBS", 10)),
            progress_interval=max(1.0, float(os.getenv("PROGRESS_INTERVAL", "3"))),
            sudo_users=frozenset(sudo),
            public_base_url=_detect_public_base_url(),
            web_host=os.getenv("WEB_HOST", "0.0.0.0").strip(),
            web_port=_int("WEB_PORT", _int("PORT", 8080)),
            direct_link_ttl=max(300, _int("DIRECT_LINK_TTL", 86400)),
            telegraph_access_token=os.getenv("TELEGRAPH_ACCESS_TOKEN", "").strip() or None,
            session_timeout=max(60, _int("SESSION_TIMEOUT", 21600)),
        )
        cfg.download_dir.mkdir(parents=True, exist_ok=True)
        cfg.work_dir.mkdir(parents=True, exist_ok=True)
        cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
        return cfg
