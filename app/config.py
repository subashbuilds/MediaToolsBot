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
            max_concurrent_jobs=max(1, _int("MAX_CONCURRENT_JOBS", 2)),
            progress_interval=max(1.0, float(os.getenv("PROGRESS_INTERVAL", "3"))),
            sudo_users=frozenset(sudo),
        )
        cfg.download_dir.mkdir(parents=True, exist_ok=True)
        cfg.work_dir.mkdir(parents=True, exist_ok=True)
        cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
        return cfg
