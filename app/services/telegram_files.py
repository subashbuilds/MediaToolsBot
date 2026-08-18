from __future__ import annotations

import asyncio
from pathlib import Path
import shutil

from aiogram import Bot
from aiogram.types import FSInputFile

from ..utils.files import safe_filename


async def download_telegram(bot: Bot, file_id: str, dest: Path, cancel: asyncio.Event | None = None) -> Path:
    tg_file = await bot.get_file(file_id)
    if not tg_file.file_path:
        raise RuntimeError("Telegram did not return a file path")
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Local Bot API returns an absolute local path. Standard Bot API returns a remote path.
    if tg_file.file_path.startswith("/") and Path(tg_file.file_path).exists():
        src = Path(tg_file.file_path)
        if src.resolve() != dest.resolve():
            with src.open("rb") as rf, dest.open("wb") as wf:
                shutil.copyfileobj(rf, wf, length=8 * 1024 * 1024)
        return dest
    await bot.download_file(tg_file.file_path, destination=dest)
    if cancel and cancel.is_set():
        raise asyncio.CancelledError
    return dest


async def upload_telegram(bot: Bot, chat_id: int, path: Path, caption: str | None = None):
    return await bot.send_document(chat_id, FSInputFile(path), caption=caption)
