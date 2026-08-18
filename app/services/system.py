from __future__ import annotations

import os
import platform
import shutil
import time
from html import escape


def _meminfo() -> tuple[int, int]:
    total = available = 0
    try:
        for line in open('/proc/meminfo', encoding='utf-8'):
            key, value, *_ = line.split()
            if key == 'MemTotal:':
                total = int(value) * 1024
            elif key == 'MemAvailable:':
                available = int(value) * 1024
    except Exception:
        pass
    return total, available


def _fmt(n: int) -> str:
    units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
    x = float(max(0, n))
    for u in units:
        if x < 1024 or u == units[-1]:
            return f'{x:.2f} {u}'
        x /= 1024
    return f'{x:.2f} TiB'


def _uptime() -> str:
    try:
        seconds = int(float(open('/proc/uptime', encoding='utf-8').read().split()[0]))
    except Exception:
        return 'unknown'
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    return f'{d}d {h}h {m}m'


def system_stats_text(cfg, state) -> str:
    total, available = _meminfo()
    used = max(0, total - available)
    disk = shutil.disk_usage(cfg.download_dir)
    try:
        load = os.getloadavg()
        load_text = ' / '.join(f'{x:.2f}' for x in load)
    except Exception:
        load_text = 'unavailable'
    cpu = os.cpu_count() or 1
    ram_pct = (used / total * 100) if total else 0
    disk_pct = (disk.used / disk.total * 100) if disk.total else 0
    return (
        '<b>🖥️ Host System Statistics</b>\n\n'
        f'<b>OS:</b> {escape(platform.platform())}\n'
        f'<b>Kernel:</b> {escape(platform.release())}\n'
        f'<b>Architecture:</b> {escape(platform.machine())}\n'
        f'<b>CPU cores:</b> {cpu}\n'
        f'<b>Load average:</b> {load_text}\n'
        f'<b>RAM:</b> {_fmt(used)} / {_fmt(total)} ({ram_pct:.1f}% used)\n'
        f'<b>Disk:</b> {_fmt(disk.used)} / {_fmt(disk.total)} ({disk_pct:.1f}% used)\n'
        f'<b>Free disk:</b> {_fmt(disk.free)}\n'
        f'<b>Uptime:</b> {_uptime()}\n\n'
        f'<b>Bot process:</b> {"Running job" if state.busy else "Idle"}\n'
        f'<b>Current file:</b> {escape(state.path.name) if state.path else "None"}\n'
        f'<b>Merge queue:</b> {len(state.merge_inputs)}'
    )
