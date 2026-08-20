from pathlib import Path


def test_mtproto_transport_contract():
    source = Path("app/main.py").read_text()
    assert 'TelegramClient(str(cfg.work_dir / "media_tools_bot"), cfg.api_id, cfg.api_hash)' in source
    assert 'await self.client.start(bot_token=self.cfg.bot_token)' in source
    assert 'progress_callback=cb' in source
    assert 'download_media(event.message' in source
    assert 'await self.client.send_file(' in source
    assert 'api.telegram.org/bot' not in source
    assert 'telegram-bot-api' not in source
    assert 'USE_LOCAL_BOT_API' not in source
    assert 'TELEGRAM_API_BASE' not in source
