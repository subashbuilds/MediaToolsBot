from __future__ import annotations

import sqlite3
from pathlib import Path


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self):
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init(self):
        with self._connect() as c:
            c.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, gofile_token TEXT, destination TEXT DEFAULT 'telegram')")

    def get(self, user_id: int) -> dict:
        with self._connect() as c:
            row = c.execute("SELECT gofile_token, destination FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            return {"gofile_token": None, "destination": "telegram"}
        return {"gofile_token": row[0], "destination": row[1] or "telegram"}

    def set_gofile(self, user_id: int, token: str | None):
        with self._connect() as c:
            c.execute("INSERT INTO users(user_id,gofile_token) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET gofile_token=excluded.gofile_token", (user_id, token))

    def set_destination(self, user_id: int, destination: str):
        if destination not in {"telegram", "gofile"}:
            raise ValueError(destination)
        with self._connect() as c:
            c.execute("INSERT INTO users(user_id,destination) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET destination=excluded.destination", (user_id, destination))
