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
            gofile_folder_id TEXT,
            rename_file INTEGER NOT NULL DEFAULT 0,
            upload_mode TEXT NOT NULL DEFAULT 'choose',
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            first_seen_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        # Migrate databases created by older builds without losing settings.
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(users)").fetchall()}
        if "gofile_folder_id" not in columns:
            self.conn.execute("ALTER TABLE users ADD COLUMN gofile_folder_id TEXT")
        for column, definition in (("username", "TEXT"), ("first_name", "TEXT"), ("last_name", "TEXT"), ("first_seen_at", "TEXT"), ("upload_mode", "TEXT NOT NULL DEFAULT 'choose'")):
            if column not in columns:
                self.conn.execute(f"ALTER TABLE users ADD COLUMN {column} {definition}")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS direct_links(
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            path TEXT NOT NULL,
            expires_at INTEGER NOT NULL
        )""")
        self.conn.commit()


    def register_user(self, user_id: int, username: str | None = None, first_name: str | None = None, last_name: str | None = None) -> bool:
        """Register/update a user. Returns True only for the first sighting."""
        row = self.conn.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,)).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO users(user_id,username,first_name,last_name) VALUES(?,?,?,?)",
                (user_id, username, first_name, last_name),
            )
            self.conn.commit()
            return True
        self.conn.execute(
            "UPDATE users SET username=?, first_name=?, last_name=?, updated_at=CURRENT_TIMESTAMP WHERE user_id=?",
            (username, first_name, last_name, user_id),
        )
        self.conn.commit()
        return False

    def ensure(self, user_id: int) -> None:
        self.conn.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (user_id,))
        self.conn.commit()

    def get_gofile_token(self, user_id: int, default: str | None = None) -> str | None:
        self.ensure(user_id)
        row = self.conn.execute("SELECT gofile_token FROM users WHERE user_id=?", (user_id,)).fetchone()
        return (row[0] if row and row[0] else None) or default

    def set_gofile_token(self, user_id: int, token: str | None) -> None:
        self.ensure(user_id)
        self.conn.execute("UPDATE users SET gofile_token=?, gofile_folder_id=NULL, updated_at=CURRENT_TIMESTAMP WHERE user_id=?", (token, user_id))
        self.conn.commit()

    def get_gofile_folder(self, user_id: int) -> str | None:
        self.ensure(user_id)
        row = self.conn.execute("SELECT gofile_folder_id FROM users WHERE user_id=?", (user_id,)).fetchone()
        return row[0] if row and row[0] else None

    def set_gofile_folder(self, user_id: int, folder_id: str | None) -> None:
        self.ensure(user_id)
        self.conn.execute("UPDATE users SET gofile_folder_id=?, updated_at=CURRENT_TIMESTAMP WHERE user_id=?", (folder_id, user_id))
        self.conn.commit()

    def get_username(self, user_id: int) -> str | None:
        self.ensure(user_id)
        row = self.conn.execute("SELECT username FROM users WHERE user_id=?", (user_id,)).fetchone()
        return row[0] if row and row[0] else None

    def get_rename(self, user_id: int) -> bool:
        self.ensure(user_id)
        row = self.conn.execute("SELECT rename_file FROM users WHERE user_id=?", (user_id,)).fetchone()
        return bool(row[0])

    def set_rename(self, user_id: int, enabled: bool) -> None:
        self.ensure(user_id)
        self.conn.execute("UPDATE users SET rename_file=?, updated_at=CURRENT_TIMESTAMP WHERE user_id=?", (int(enabled), user_id))
        self.conn.commit()


    def get_upload_mode(self, user_id: int) -> str:
        self.ensure(user_id)
        row = self.conn.execute("SELECT upload_mode FROM users WHERE user_id=?", (user_id,)).fetchone()
        mode = (row[0] if row and row[0] else "choose").lower()
        return mode if mode in {"choose", "telegram", "gofile"} else "choose"

    def set_upload_mode(self, user_id: int, mode: str) -> None:
        if mode not in {"choose", "telegram", "gofile"}:
            raise ValueError("upload mode must be choose, telegram or gofile")
        self.ensure(user_id)
        self.conn.execute("UPDATE users SET upload_mode=?, updated_at=CURRENT_TIMESTAMP WHERE user_id=?", (mode, user_id))
        self.conn.commit()

    def all_users(self) -> list[int]:
        return [int(r[0]) for r in self.conn.execute("SELECT user_id FROM users ORDER BY user_id").fetchall()]

    def find_user(self, identifier: str) -> int | None:
        value = identifier.strip()
        if value.startswith("@"): value = value[1:]
        if value.isdigit():
            row = self.conn.execute("SELECT user_id FROM users WHERE user_id=?", (int(value),)).fetchone()
            return int(row[0]) if row else None
        row = self.conn.execute("SELECT user_id FROM users WHERE lower(username)=lower(?)", (value,)).fetchone()
        return int(row[0]) if row else None

    def add_direct_link(self, token: str, user_id: int, path: str, expires_at: int) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO direct_links(token,user_id,path,expires_at) VALUES(?,?,?,?)",
            (token, user_id, path, expires_at),
        )
        self.conn.commit()

    def get_direct_link(self, token: str):
        row = self.conn.execute(
            "SELECT user_id,path,expires_at FROM direct_links WHERE token=?", (token,)
        ).fetchone()
        return row

    def active_direct_paths(self, now: int) -> set[str]:
        rows = self.conn.execute("SELECT path FROM direct_links WHERE expires_at >= ?", (now,)).fetchall()
        return {str(Path(row[0]).resolve()) for row in rows}

    def purge_direct_links(self, now: int) -> None:
        self.conn.execute("DELETE FROM direct_links WHERE expires_at < ?", (now,))
        self.conn.commit()

    def close(self):
        self.conn.close()
