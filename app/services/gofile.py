from __future__ import annotations

import asyncio
from pathlib import Path

import aiohttp
from aiohttp import payload


UPLOAD_ENDPOINT = "https://upload.gofile.io/uploadfile"


class ProgressFilePayload(payload.Payload):
    def __init__(self, path: Path, callback=None, chunk_size: int = 1024 * 1024,
                 cancel_event: asyncio.Event | None = None):
        super().__init__(path, content_type="application/octet-stream")
        self.path = path
        self.callback = callback
        self.chunk_size = max(64 * 1024, int(chunk_size))
        self.cancel_event = cancel_event
        self._size = path.stat().st_size
        safe_name = path.name.replace('"', "_").replace("\r", "_").replace("\n", "_")
        self.headers["Content-Disposition"] = f'form-data; name="file"; filename="{safe_name}"'

    def decode(self, data):
        return data

    async def write(self, writer):
        sent = 0
        with self.path.open("rb") as f:
            while True:
                if self.cancel_event and self.cancel_event.is_set():
                    raise asyncio.CancelledError
                chunk = f.read(self.chunk_size)
                if not chunk:
                    break
                await writer.write(chunk)
                sent += len(chunk)
                if self.callback:
                    result = self.callback(sent, self._size)
                    if asyncio.iscoroutine(result):
                        await result


async def upload_gofile(
    path: Path,
    token: str | None = None,
    folder_id: str | None = None,
    progress=None,
    cancel_event: asyncio.Event | None = None,
) -> dict:
    """Upload one file to GoFile, optionally reusing an existing folder.

    Per the current GoFile API documentation, omitting folderId creates a new
    public folder. Reusing the returned parentFolder/folderId puts subsequent
    uploads into the same folder. A returned guestToken can likewise be reused
    for guest-account uploads.
    """
    if not path.is_file():
        raise FileNotFoundError(path)
    timeout = aiohttp.ClientTimeout(total=None, connect=60, sock_read=600)
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        mp = aiohttp.MultipartWriter("form-data")
        if folder_id:
            folder_payload = payload.StringPayload(folder_id)
            folder_payload.headers["Content-Disposition"] = 'form-data; name="folderId"'
            mp.append_payload(folder_payload)
        mp.append_payload(ProgressFilePayload(path, progress, cancel_event=cancel_event))
        async with session.post(UPLOAD_ENDPOINT, data=mp) as r:
            text = await r.text()
            if r.status >= 400:
                raise RuntimeError(f"GoFile HTTP {r.status}: {text[:500]}")
            try:
                data = await r.json(content_type=None)
            except Exception as exc:
                raise RuntimeError(f"GoFile returned invalid JSON: {text[:500]}") from exc
            if data.get("status") != "ok":
                raise RuntimeError(str(data))
            result = data.get("data", data)
            if not isinstance(result, dict):
                raise RuntimeError(f"Unexpected GoFile response: {result!r}")
            return result
