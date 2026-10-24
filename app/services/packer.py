"""Create archives of finished files.

Extraction lives in :mod:`app.services.archive`; this is its counterpart for
"Create Archive". Python's ``zipfile`` is used deliberately instead of a 7-Zip
subprocess: no new dependency, no path-injection surface and no compression-bomb
footguns. Media is already compressed, so entries are stored rather than
deflated — metering a 2 GiB video through DEFLATE costs minutes of CPU for no
worthwhile size win.
"""
from __future__ import annotations

import zipfile
from pathlib import Path


def make_archive(path: Path, output: Path) -> Path:
    """Zip ``path`` into ``output``, storing entries without recompression."""
    if not path.exists() or not path.is_file():
        raise ValueError("There is no file to archive")
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_STORED) as zf:
        # arcname without directories keeps "movie.zip -> movie.mkv" sane.
        zf.write(path, arcname=path.name)
    return output
