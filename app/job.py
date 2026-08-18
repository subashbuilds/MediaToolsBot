from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Job:
    user_id: int
    chat_id: int
    root: Path
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    source: Path | None = None
    info: dict[str, Any] | None = None
    selected_streams: set[int] = field(default_factory=set)
    state: str = "idle"

    def cleanup(self):
        if self.root.exists():
            import shutil
            shutil.rmtree(self.root, ignore_errors=True)
