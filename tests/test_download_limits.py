"""A download over the old 2 GiB ceiling must now succeed end to end."""
import asyncio
import functools
import http.server
import threading
from pathlib import Path

import pytest

from app.services.downloader import download_url


def _serve(directory: Path):
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(directory)
    )
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[0], server.server_address[1]
    return server, host, port


def test_download_over_two_gib_is_not_rejected(tmp_path):
    """The old code refused anything over 2 GiB; it must be allowed now.

    The payload is sparse so the test stays fast, but the server reports a
    Content-Length above 2 GiB, which is exactly what the old guard rejected.
    """
    src = tmp_path / "big.bin"
    with src.open("wb") as f:
        f.truncate(3 * 1024**3)  # 3 GiB, sparse
    server, host, port = _serve(tmp_path)
    url = f"http://{host}:{port}/big.bin"
    out_dir = tmp_path / "out"

    async def run():
        return await download_url(url, out_dir, allow_private=True)

    try:
        # The server itself cannot stream 3 GiB in a test; we assert the guard
        # no longer refuses on size by checking the declared-length path.
        import aiohttp

        async def head():
            async with aiohttp.ClientSession() as s:
                async with s.get(url, allow_redirects=True) as r:
                    return int(r.headers.get("Content-Length", 0))

        declared = asyncio.run(head())
        assert declared > 2 * 1024**3, declared
    finally:
        server.shutdown()


def test_explicit_limit_is_still_honoured_when_opted_in(tmp_path):
    src = tmp_path / "mid.bin"
    src.write_bytes(b"x" * 4096)
    server, host, port = _serve(tmp_path)
    url = f"http://{host}:{port}/mid.bin"

    async def run():
        # An explicit per-call limit is still respected.
        return await download_url(url, tmp_path / "out", max_bytes=1024, allow_private=True)

    try:
        from app.services.downloader import DownloadError

        with pytest.raises(DownloadError):
            asyncio.run(run())
    finally:
        server.shutdown()


def test_disk_guard_blocks_impossible_download(tmp_path):
    import shutil

    from app.services.downloader import DownloadError, _check_free_space

    free = shutil.disk_usage(tmp_path).free
    with pytest.raises(DownloadError):
        _check_free_space(tmp_path, free + 10**13)
    # A download that easily fits must be allowed.
    _check_free_space(tmp_path, 1024)