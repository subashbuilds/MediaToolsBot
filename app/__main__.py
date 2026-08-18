from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.fsm.storage.memory import MemoryStorage

from .config import Config
from .db import Database
from .handlers.bot import SessionManager, init, router
from .services.process import run_cmd


async def dependency_check():
    # FFmpeg is required for media operations. Archive tools are optional at startup
    # because ZIP/TAR formats use Python stdlib and RAR/7Z are checked lazily.
    for cmd in ("ffmpeg", "ffprobe"):
        code, _, err = await run_cmd([cmd, "-version"])
        if code != 0:
            raise RuntimeError(f"Required executable missing or broken: {cmd}: {err[-500:]}")


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = Config.from_env()
    await dependency_check()
    db = Database(config.state_db)
    sessions = SessionManager(config)
    init(config, db, sessions)

    session = None
    if config.use_local_bot_api and config.telegram_api_base:
        server = TelegramAPIServer.from_base(config.telegram_api_base, is_local=True)
        session = AiohttpSession(api=server)
    bot = Bot(config.bot_token, session=session)
    await bot.set_my_commands([
        BotCommand(command="start", description="Open main menu"),
        BotCommand(command="upload", description="Choose Telegram or GoFile upload"),
        BotCommand(command="setgofile", description="Set your GoFile API token"),
        BotCommand(command="cleargofile", description="Use guest GoFile uploads"),
        BotCommand(command="settings", description="Show upload settings"),
        BotCommand(command="cancel", description="Cancel current process"),
        BotCommand(command="help", description="Show help"),
    ])
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    logging.info("Bot starting; local API=%s", bool(session))
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
