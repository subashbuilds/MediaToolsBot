from __future__ import annotations

import sqlite3
from pathlib import Path


class DB:
    def __init__(self, path: Path):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            gofile_token TEXT,
            rename_file INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        self.conn.commit()

    def ensure(self, user_id: int) -> None:
        self.conn.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (user_id,))
        self.conn.commit()

    def get_gofile_token(self, user_id: int, default: str | None = None) -> str | None:
        self.ensure(user_id)
        row = self.conn.execute("SELECT gofile_token FROM users WHERE user_id=?", (user_id,)).fetchone()
        return (row[0] if row and row[0] else None) or default

    def set_gofile_token(self, user_id: int, token: str | None) -> None:
        self.ensure(user_id)
        self.conn.execute("UPDATE users SET gofile_token=?, updated_at=CURRENT_TIMESTAMP WHERE user_id=?", (token, user_id))
        self.conn.commit()

    def get_rename(self, user_id: int) -> bool:
        self.ensure(user_id)
        row = self.conn.execute("SELECT rename_file FROM users WHERE user_id=?", (user_id,)).fetchone()
        return bool(row[0])

    def set_rename(self, user_id: int, enabled: bool) -> None:
        self.ensure(user_id)
        self.conn.execute("UPDATE users SET rename_file=?, updated_at=CURRENT_TIMESTAMP WHERE user_id=?", (int(enabled), user_id))
        self.conn.commit()

    def close(self):
        self.conn.close()
