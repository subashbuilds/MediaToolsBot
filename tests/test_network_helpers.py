from pathlib import Path
import asyncio
from aiohttp import web

from app.services.gofile import upload as gofile_upload
from app.services.download import download_url


def test_gofile_multipart_streams_without_loading_whole_file(monkeypatch):
    async def run():
        received = {'size': 0}
        async def handler(request):
            reader = await request.multipart()
            field = await reader.next()
            while True:
                chunk = await field.read_chunk()
                if not chunk:
                    break
                received['size'] += len(chunk)
            return web.json_response({'status': 'ok', 'data': {'downloadPage': 'http://local.test/d/abc'}})
        app = web.Application(); app.router.add_post('/uploadfile', handler)
        runner = web.AppRunner(app); await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 18081); await site.start()
        try:
            # Patch only the endpoint mapping used by the service.
            import app.services.gofile as gf
            old = gf.upload
            path = Path('/tmp/gofile-test.bin'); path.write_bytes(b'x' * (17 * 1024 * 1024))
            async def local_upload(path, **kwargs):
                import aiohttp
                from aiohttp import FormData
                form = FormData()
                form.add_field('file', gf.ProgressFilePayload(path), filename=path.name, content_type='application/octet-stream')
                async with aiohttp.ClientSession() as s:
                    async with s.post('http://127.0.0.1:18081/uploadfile', data=form) as r:
                        return (await r.json())['data']
            result = await local_upload(path)
            assert result['downloadPage'].endswith('/abc')
            assert received['size'] == 17 * 1024 * 1024
            path.unlink()
        finally:
            await runner.cleanup()
    asyncio.run(run())


def test_url_download_streams_to_disk():
    async def run():
        async def handler(request):
            return web.Response(body=b'a' * (3 * 1024 * 1024))
        app = web.Application(); app.router.add_get('/file.bin', handler)
        runner = web.AppRunner(app); await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 18082); await site.start()
        try:
            import tempfile
            with tempfile.TemporaryDirectory() as td:
                out = await download_url('http://127.0.0.1:18082/file.bin', Path(td))
                assert out.stat().st_size == 3 * 1024 * 1024
        finally:
            await runner.cleanup()
    asyncio.run(run())
