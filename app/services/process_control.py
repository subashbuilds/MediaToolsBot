from __future__ import annotations

import contextvars
import os
import re
import signal
import subprocess
import threading
import time
from collections.abc import Callable

_current_job = contextvars.ContextVar("media_tools_job_uid", default=None)
_lock = threading.RLock()
_active: dict[int, subprocess.Popen] = {}

# FFmpeg normally terminates on SIGINT, but a broken input (a truncated file, a
# network mount, a broken pipe on a full disk) can leave it running. Without a
# hard ceiling the worker thread stays pinned forever and asyncio.to_thread()
# can never reclaim it, so the default wall-clock budget is enforced.
DEFAULT_TIMEOUT = int(os.environ.get("FFMPEG_TIMEOUT", "14400"))

_TIME_RE = re.compile(r"^out_time=(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)$")


def set_job(uid: int):
    return _current_job.set(int(uid))


def reset_job(token) -> None:
    _current_job.reset(token)


def current_job() -> int | None:
    return _current_job.get()


def _parse_progress(line: str) -> float | None:
    line = line.strip()
    if line.startswith("out_time_us="):
        try:
            return int(line.split("=", 1)[1]) / 1_000_000.0
        except ValueError:
            return None
    if line.startswith("out_time_ms="):
        # Note: FFmpeg's out_time_ms is actually microseconds despite the name.
        try:
            return int(line.split("=", 1)[1]) / 1_000_000.0
        except ValueError:
            return None
    m = _TIME_RE.match(line)
    if m:
        hours, minutes, seconds = m.groups()
        return int(hours or 0) * 3600 + int(minutes) * 60 + float(seconds)
    return None


def _kill_tree(proc: subprocess.Popen) -> None:
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
        if proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, sig)
        except Exception:
            try:
                proc.send_signal(sig)
            except Exception:
                return
        time.sleep(1.5)


def _drain(stream, sink: list[str], on_line: Callable[[str], None] | None = None) -> None:
    try:
        for chunk in iter(lambda: stream.readline(), ""):
            sink.append(chunk)
            if on_line is not None and chunk:
                on_line(chunk)
    except (ValueError, OSError):
        pass


def run_command(
    args: list[str],
    *,
    text: bool = True,
    timeout: int | None = DEFAULT_TIMEOUT,
    check: bool = True,
    on_progress: Callable[[float], None] | None = None,
) -> subprocess.CompletedProcess:
    """Run a cancellable child process in its own process group.

    The process is registered against the active job so a user pressing Cancel
    can interrupt it, and it is always bounded by ``timeout`` seconds. When
    ``on_progress`` is given the caller is expected to have added
    ``-progress pipe:1`` and receives the encoded position in seconds.
    """
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
        timed_out = False
        if on_progress is None:
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_tree(proc)
                stdout, stderr = proc.communicate(timeout=10)
                timed_out = True
        else:
            # Both pipes must be drained concurrently: a full stderr buffer
            # would otherwise block the child forever while we wait on stdout.
            out_chunks: list[str] = []
            err_chunks: list[str] = []

            def handler(chunk: str) -> None:
                value = _parse_progress(chunk)
                if value is not None:
                    on_progress(value)
            t_out = threading.Thread(target=_drain, args=(proc.stdout, out_chunks, handler), daemon=True)
            t_err = threading.Thread(target=_drain, args=(proc.stderr, err_chunks), daemon=True)
            t_out.start()
            t_err.start()
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_tree(proc)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
                timed_out = True
            t_out.join(timeout=5)
            t_err.join(timeout=5)
            stdout = "".join(out_chunks)
            stderr = "".join(err_chunks)
        if timed_out:
            err = f"Process exceeded the {timeout}s limit and was stopped"
            if check:
                raise subprocess.TimeoutExpired(args, timeout or 0, output=stdout, stderr=err)
            return subprocess.CompletedProcess(args, -9, stdout, err)
        if proc.returncode != 0 and check:
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
