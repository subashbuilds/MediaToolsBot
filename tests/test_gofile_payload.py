import asyncio
from pathlib import Path
from aiohttp import web, ClientSession, MultipartReader
from app.services.gofile import ProgressFilePayload


def test_progress_payload_streams(tmp_path):
    path = tmp_path / "payload.bin"
    data = b"x" * (3 * 1024 * 1024 + 123)
    path.write_bytes(data)
    seen = []

    async def main():
        async def handler(request):
            reader = await request.multipart()
            part = await reader.next()
            got = bytearray()
            while True:
                chunk = await part.read_chunk()
                if not chunk:
                    break
                got.extend(chunk)
            return web.json_response({"size": len(got)})

        app = web.Application()
        app.router.add_post("/upload", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            mp = __import__('aiohttp').MultipartWriter("form-data")
            async def cb(cur, total):
                seen.append((cur, total))
            mp.append_payload(ProgressFilePayload(path, cb, 1024 * 1024))
            async with ClientSession() as s:
                async with s.post(f"http://127.0.0.1:{port}/upload", data=mp) as r:
                    assert r.status == 200
                    result = await r.json()
                    assert result["size"] == len(data)
        finally:
            await runner.cleanup()

    asyncio.run(main())
    assert seen and seen[-1][0] == len(data)
