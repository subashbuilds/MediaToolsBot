import asyncio
from aiohttp import web

from app.services.downloader import download_url


def test_downloader_cleans_partial_file_on_cancel(tmp_path):
    async def run():
        async def handler(request):
            resp = web.StreamResponse(status=200, headers={"Content-Length": str(10 * 1024 * 1024)})
            await resp.prepare(request)
            await resp.write(b"x" * (1024 * 1024))
            await asyncio.sleep(10)
            return resp

        app = web.Application()
        app.router.add_get("/file.bin", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        cancel = asyncio.Event()
        task = asyncio.create_task(download_url(f"http://127.0.0.1:{port}/file.bin", tmp_path, cancel_event=cancel))
        await asyncio.sleep(0.2)
        cancel.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert not list(tmp_path.glob("*"))
        await runner.cleanup()

    asyncio.run(run())
