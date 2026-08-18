from pathlib import Path
from app.utils.files import safe_filename, progress_bar, format_bytes, unique_path


def test_safe_filename_and_unique(tmp_path):
    assert "/" not in safe_filename("../a/b?.mkv")
    p1 = unique_path(tmp_path, "x.txt")
    p1.write_text("x")
    p2 = unique_path(tmp_path, "x.txt")
    assert p1 != p2


def test_progress_and_bytes():
    assert progress_bar(50, 100, 10) == "█████░░░░░"
    assert format_bytes(1024) == "1.00 KiB"
