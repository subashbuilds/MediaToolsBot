import sys
import types


class FakeButton:
    def __init__(self, text, data):
        self.text = text
        self.data = data

    @classmethod
    def inline(cls, text, data):
        return cls(text, data)


sys.modules.setdefault("telethon", types.SimpleNamespace(Button=FakeButton))

from app.ui.keyboards import main_menu, video_menu, audio_menu  # noqa: E402


def flatten(rows):
    return [b for row in rows for b in row]


def test_ui_layout_counts():
    assert [len(r) for r in main_menu()] == [1, 1, 2, 1, 2, 1]
    assert len(video_menu()) == 8
    assert len(audio_menu()) == 7


def test_callback_data_under_64_bytes():
    for rows in (main_menu(), video_menu(), audio_menu()):
        for b in flatten(rows):
            assert len(b.data) <= 64
