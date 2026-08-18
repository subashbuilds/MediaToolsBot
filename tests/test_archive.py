import tarfile, zipfile
from pathlib import Path
from app.services.archive import extract_archive


def test_zip_extract(tmp_path):
    z = tmp_path / "a.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("hello.txt", "hello")
    out = tmp_path / "out"
    extract_archive(z, out)
    assert (out / "hello.txt").read_text() == "hello"


def test_zip_path_traversal(tmp_path):
    z = tmp_path / "bad.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("../../evil.txt", "bad")
    try:
        extract_archive(z, tmp_path / "out")
    except RuntimeError:
        pass
    else:
        raise AssertionError("path traversal was not rejected")
