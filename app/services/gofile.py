from __future__ import annotations

import asyncio
from pathlib import Path

import aiohttp
from aiohttp import payload


UPLOAD_ENDPOINT = "https://upload.gofile.io/uploadfile"
CREATE_FOLDER_ENDPOINT = "https://api.gofile.io/contents/createFolder"


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


async def _json_request(session, method: str, url: str, *, token: str | None, payload_data: dict) -> dict:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with session.request(method, url, json=payload_data, headers=headers) as r:
        text = await r.text()
        if r.status >= 400:
            raise RuntimeError(f"GoFile HTTP {r.status}: {text[:500]}")
        try:
            obj = await r.json(content_type=None)
        except Exception as exc:
            raise RuntimeError(f"GoFile returned invalid JSON: {text[:500]}") from exc
        if obj.get("status") != "ok":
            raise RuntimeError(str(obj))
        return obj.get("data", obj)


async def create_folder(parent_folder_id: str, folder_name: str, token: str | None = None) -> dict:
    """Create a child folder using the documented GoFile contents API."""
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        return await _json_request(
            session, "POST", CREATE_FOLDER_ENDPOINT, token=token,
            payload_data={"parentFolderId": parent_folder_id, "folderName": folder_name, "public": True},
        )


async def upload_gofile(
    path: Path,
    token: str | None = None,
    folder_id: str | None = None,
    progress=None,
    cancel_event: asyncio.Event | None = None,
    attempts: int = 3,
) -> dict:
    """Upload one file to GoFile, retrying transient network failures.

    Uploads move multiple gigabytes, so a single dropped connection used to
    abort the whole batch and force the user to start over.
    """
    if not path.is_file():
        raise FileNotFoundError(path)
    last: Exception | None = None
    for attempt in range(1, max(1, attempts) + 1):
        if cancel_event and cancel_event.is_set():
            raise asyncio.CancelledError
        try:
            return await _upload_once(path, token, folder_id, progress, cancel_event)
        except asyncio.CancelledError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            last = exc
            if attempt >= max(1, attempts):
                break
            await asyncio.sleep(min(8, 1.5 * attempt))
        except RuntimeError as exc:
            # Most 4xx responses are deterministic (bad token, folder missing)
            # and are surfaced immediately rather than retried pointlessly.
            # 408 and 429 are the server asking us to slow down or try again
            # later, and GoFile rate-limits bursts, so those must be retried -
            # the blanket "HTTP 4" test failed every rate-limited upload.
            message = str(exc)
            permanent = "HTTP 4" in message and not any(f"HTTP {code}" in message for code in (408, 429))
            if permanent:
                raise
            last = exc
            if attempt >= max(1, attempts):
                break
            await asyncio.sleep(min(8, 1.5 * attempt))
    raise RuntimeError(f"GoFile upload failed after {attempts} attempts: {last}")


async def _upload_once(path, token, folder_id, progress, cancel_event) -> dict:
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

async def delete_content(content_id: str, token: str) -> dict:
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        return await _json_request(
            session, "DELETE", "https://api.gofile.io/contents", token=token,
            payload_data={"contentsId": content_id},
        )


async def move_content(content_id: str, folder_id: str, token: str) -> dict:
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        return await _json_request(
            session, "PUT", "https://api.gofile.io/contents/move", token=token,
            payload_data={"contentsId": content_id, "folderId": folder_id},
        )
