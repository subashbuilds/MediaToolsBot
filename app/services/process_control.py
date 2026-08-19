from __future__ import annotations

import contextvars
import os
import signal
import subprocess
import threading
from pathlib import Path

_current_job = contextvars.ContextVar("media_tools_job_uid", default=None)
_lock = threading.RLock()
_active: dict[int, subprocess.Popen] = {}


def set_job(uid: int):
    return _current_job.set(int(uid))


def reset_job(token) -> None:
    _current_job.reset(token)


def current_job() -> int | None:
    return _current_job.get()


def run_command(args: list[str], *, text: bool = True) -> subprocess.CompletedProcess:
    uid = current_job()
    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
        start_new_session=True,
    )
    if uid is not None:
        with _lock:
            _active[uid] = proc
    try:
        stdout, stderr = proc.communicate()
        if proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, args, output=stdout, stderr=stderr)
        return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)
    finally:
        if uid is not None:
            with _lock:
                if _active.get(uid) is proc:
                    _active.pop(uid, None)


def cancel_job(uid: int) -> bool:
    with _lock:
        proc = _active.get(int(uid))
    if not proc or proc.poll() is not None:
        return False
    try:
        os.killpg(proc.pid, signal.SIGINT)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            return False
    return True


def active_job_count() -> int:
    with _lock:
        return sum(1 for p in _active.values() if p.poll() is None)
