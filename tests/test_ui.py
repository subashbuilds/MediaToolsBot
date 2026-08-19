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
    assert [len(r) for r in main_menu()] == [1, 1, 1, 1, 2, 1]
    assert len(video_menu()) == 9
    assert len(audio_menu()) == 7


def test_callback_data_under_64_bytes():
    for rows in (main_menu(), video_menu(), audio_menu()):
        for b in flatten(rows):
            assert len(b.data) <= 64


def test_merge_menu_exists_and_callbacks_are_small():
    from app.ui.keyboards import merge_menu
    assert any(b.data == b"merge:finish" for b in flatten(merge_menu()))
    assert all(len(b.data) <= 64 for b in flatten(merge_menu()))
