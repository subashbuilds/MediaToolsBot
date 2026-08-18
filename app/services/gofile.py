from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import aiohttp
from aiohttp import payload


class GoFileError(RuntimeError):
    pass


class ProgressFilePayload(payload.Payload):
    def __init__(self, path: Path, callback=None, cancel_event: asyncio.Event | None = None):
        super().__init__(None, content_type="application/octet-stream")
        self.path = path
        self._size = path.stat().st_size
        self.callback = callback
        self.cancel_event = cancel_event

    def decode(self, encoding: str = "utf-8", errors: str = "strict") -> str:
        return self.path.name

    async def write(self, writer):
        done = 0
        started = time.monotonic()
        last_report = started
        with self.path.open("rb") as f:
            while True:
                if self.cancel_event and self.cancel_event.is_set():
                    raise asyncio.CancelledError
                chunk = f.read(8 * 1024 * 1024)
                if not chunk:
                    break
                await writer.write(chunk)
                done += len(chunk)
                if self.callback and (time.monotonic() - last_report >= 1 or done == self.size):
                    await self.callback(done, self.size, started)
                    last_report = time.monotonic()


async def upload(path: Path, token: str | None = None, region: str = "auto", progress_cb=None, cancel_event: asyncio.Event | None = None) -> dict:
    hosts = {
        "auto": "https://upload.gofile.io/uploadfile",
        "eu": "https://upload-eu-par.gofile.io/uploadfile",
        "na": "https://upload-na-phx.gofile.io/uploadfile",
        "ap-sgp": "https://upload-ap-sgp.gofile.io/uploadfile",
        "ap-hkg": "https://upload-ap-hkg.gofile.io/uploadfile",
        "ap-tyo": "https://upload-ap-tyo.gofile.io/uploadfile",
        "sa-sao": "https://upload-sa-sao.gofile.io/uploadfile",
    }
    endpoint = hosts.get(region, hosts["auto"])
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=60, sock_read=600)
    form = aiohttp.FormData()
    fp = ProgressFilePayload(path, callback=progress_cb, cancel_event=cancel_event)
    form.add_field("file", fp, filename=path.name, content_type="application/octet-stream")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.post(endpoint, data=form) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise GoFileError(f"GoFile HTTP {resp.status}: {text[:500]}")
            data = await resp.json(content_type=None)
            if data.get("status") != "ok":
                raise GoFileError(str(data))
            return data.get("data", data)
