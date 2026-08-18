from pathlib import Path
import tempfile
from app.services.direct import make_token, verify_token


def test_direct_token_roundtrip(tmp_path):
    p = tmp_path / "video.mkv"
    p.write_bytes(b"x")
    token, exp = make_token("secret", p, 3600)
    assert exp > 0
    assert verify_token("secret", p, token)
    assert not verify_token("wrong", p, token)

import asyncio
from aiohttp import web, ClientSession


async def _range_roundtrip(tmp_path):
    p = tmp_path / "range.bin"
    p.write_bytes(bytes(range(256)) * 100)
    app = web.Application()

    async def serve(request):
        return web.FileResponse(p, headers={"Content-Disposition": f'inline; filename="{p.name}"'})

    app.router.add_get("/f/test/range.bin", serve, allow_head=True)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        async with ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{port}/f/test/range.bin", headers={"Range": "bytes=10-29"}) as r:
                body = await r.read()
                assert r.status == 206
                assert len(body) == 20
                assert body == p.read_bytes()[10:30]
    finally:
        await runner.cleanup()


def test_direct_stream_supports_http_range(tmp_path):
    asyncio.run(_range_roundtrip(tmp_path))
