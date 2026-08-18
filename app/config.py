from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    bot_token: str
    sudo_users: set[int]
    work_dir: Path
    max_concurrent_jobs: int
    keep_files: bool
    gofile_api_token: str | None
    gofile_region: str
    public_base_url: str | None
    telegram_api_base: str | None
    use_local_bot_api: bool
    state_db: Path

    @classmethod
    def from_env(cls) -> "Config":
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError("BOT_TOKEN is required")
        sudo = {int(x) for x in os.getenv("SUDO_USERS", "").split(",") if x.strip().isdigit()}
        work = Path(os.getenv("WORK_DIR", "/data/work")).resolve()
        work.mkdir(parents=True, exist_ok=True)
        state = work.parent / "state"
        state.mkdir(parents=True, exist_ok=True)
        base = os.getenv("TELEGRAM_API_BASE", "").strip() or None
        return cls(
            bot_token=token,
            sudo_users=sudo,
            work_dir=work,
            max_concurrent_jobs=max(1, int(os.getenv("MAX_CONCURRENT_JOBS", "2"))),
            keep_files=_bool("KEEP_FILES"),
            gofile_api_token=os.getenv("GOFILE_API_TOKEN", "").strip() or None,
            gofile_region=os.getenv("GOFILE_UPLOAD_REGION", "auto").strip().lower(),
            public_base_url=os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/") or None,
            telegram_api_base=base,
            use_local_bot_api=_bool("USE_LOCAL_BOT_API"),
            state_db=state / "bot.sqlite3",
        )
