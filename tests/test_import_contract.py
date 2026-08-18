from pathlib import Path


def test_main_contains_mtproto_only_startup():
    source = Path("app/main.py").read_text()
    assert "start(bot_token=self.cfg.bot_token)" in source
    assert "api.telegram.org/bot" not in source
    assert "USE_LOCAL_BOT_API" not in source
    assert "TELEGRAM_API_BASE" not in source

def test_new_features_present():
    source = Path("app/main.py").read_text()
    assert "create_info_page" in source
    assert "merge_tracks" in source
    assert "PUBLIC_BASE_URL" in Path("app/config.py").read_text()
    assert "/f/{token}/{filename}" in source
