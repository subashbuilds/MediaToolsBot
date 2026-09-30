from __future__ import annotations

import sqlite3
import threading
from pathlib import Path


class DB:
    def __init__(self, path: Path):
        self.path = path
        # check_same_thread=False is required because FFmpeg/extraction work runs
        # in threads that read settings. A re-entrant lock serialises access so
        # concurrent readers and writers cannot interleave inside a transaction.
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=10000")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            gofile_token TEXT,
            gofile_folder_id TEXT,
            rename_file INTEGER NOT NULL DEFAULT 0,
            upload_mode TEXT NOT NULL DEFAULT 'choose',
            telegram_mode TEXT NOT NULL DEFAULT 'document',
            custom_thumbnail_path TEXT,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            bulk_mode INTEGER NOT NULL DEFAULT 0,
            auto_delete INTEGER NOT NULL DEFAULT 0,
            keep_files INTEGER NOT NULL DEFAULT 0,
            first_seen_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        # Migrate databases created by older builds without losing settings.
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(users)").fetchall()}
        if "gofile_folder_id" not in columns:
            self.conn.execute("ALTER TABLE users ADD COLUMN gofile_folder_id TEXT")
        for column, definition in (
            ("username", "TEXT"),
            ("first_name", "TEXT"),
            ("last_name", "TEXT"),
            ("first_seen_at", "TEXT"),
            ("upload_mode", "TEXT NOT NULL DEFAULT 'choose'"),
            ("telegram_mode", "TEXT NOT NULL DEFAULT 'document'"),
            ("custom_thumbnail_path", "TEXT"),
            ("bulk_mode", "INTEGER NOT NULL DEFAULT 0"),
            ("auto_delete", "INTEGER NOT NULL DEFAULT 0"),
            ("keep_files", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if column not in columns:
                self.conn.execute(f"ALTER TABLE users ADD COLUMN {column} {definition}")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS direct_links(
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            path TEXT NOT NULL,
            expires_at INTEGER NOT NULL
        )""")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_direct_links_expiry ON direct_links(expires_at)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_direct_links_path ON direct_links(path)")
        self.conn.commit()

    # ------------------------------------------------------------------
    # low level helpers
    # ------------------------------------------------------------------
    def _query(self, sql: str, params: tuple = ()):
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def _write(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            self.conn.execute(sql, params)
            self.conn.commit()

    # ------------------------------------------------------------------
    # users
    # ------------------------------------------------------------------
    def register_user(self, user_id: int, username: str | None = None, first_name: str | None = None, last_name: str | None = None) -> bool:
        """Register/update a user. Returns True only for the first sighting."""
        with self._lock:
            row = self.conn.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,)).fetchone()
            if row is None:
                self.conn.execute(
                    "INSERT INTO users(user_id,username,first_name,last_name) VALUES(?,?,?,?)",
                    (user_id, username, first_name, last_name),
                )
                self.conn.commit()
                return True
            # Avoid a pointless write on every single incoming message: only
            # touch the row when the profile data actually changed.
            current = self.conn.execute("SELECT username,first_name,last_name FROM users WHERE user_id=?", (user_id,)).fetchone()
            if tuple(current or ()) != (username, first_name, last_name):
                self.conn.execute(
                    "UPDATE users SET username=?, first_name=?, last_name=?, updated_at=CURRENT_TIMESTAMP WHERE user_id=?",
                    (username, first_name, last_name, user_id),
                )
                self.conn.commit()
            return False

    def ensure(self, user_id: int) -> None:
        self._write("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (user_id,))

    def _flag(self, user_id: int, column: str) -> bool:
        self.ensure(user_id)
        row = self._query(f"SELECT {column} FROM users WHERE user_id=?", (user_id,))
        return bool(row and row[0] and row[0][0])

    def _set_flag(self, user_id: int, column: str, value: bool) -> None:
        self.ensure(user_id)
        self._write(
            f"UPDATE users SET {column}=?, updated_at=CURRENT_TIMESTAMP WHERE user_id=?",
            (int(value), user_id),
        )

    def get_gofile_token(self, user_id: int, default: str | None = None) -> str | None:
        self.ensure(user_id)
        row = self._query("SELECT gofile_token FROM users WHERE user_id=?", (user_id,))
        return (row[0][0] if row and row[0][0] else None) or default

    def set_gofile_token(self, user_id: int, token: str | None) -> None:
        self.ensure(user_id)
        self._write(
            "UPDATE users SET gofile_token=?, gofile_folder_id=NULL, updated_at=CURRENT_TIMESTAMP WHERE user_id=?",
            (token, user_id),
        )

    def get_gofile_folder(self, user_id: int) -> str | None:
        self.ensure(user_id)
        row = self._query("SELECT gofile_folder_id FROM users WHERE user_id=?", (user_id,))
        return row[0][0] if row and row[0][0] else None

    def set_gofile_folder(self, user_id: int, folder_id: str | None) -> None:
        self.ensure(user_id)
        self._write(
            "UPDATE users SET gofile_folder_id=?, updated_at=CURRENT_TIMESTAMP WHERE user_id=?",
            (folder_id, user_id),
        )

    def get_username(self, user_id: int) -> str | None:
        self.ensure(user_id)
        row = self._query("SELECT username FROM users WHERE user_id=?", (user_id,))
        return row[0][0] if row and row[0][0] else None

    def get_rename(self, user_id: int) -> bool:
        return self._flag(user_id, "rename_file")

    def set_rename(self, user_id: int, enabled: bool) -> None:
        self._set_flag(user_id, "rename_file", enabled)

    def get_upload_mode(self, user_id: int) -> str:
        self.ensure(user_id)
        row = self._query("SELECT upload_mode FROM users WHERE user_id=?", (user_id,))
        mode = (row[0][0] if row and row[0][0] else "choose").lower()
        return mode if mode in {"choose", "telegram", "gofile"} else "choose"

    def set_upload_mode(self, user_id: int, mode: str) -> None:
        if mode not in {"choose", "telegram", "gofile"}:
            raise ValueError("upload mode must be choose, telegram or gofile")
        self.ensure(user_id)
        self._write("UPDATE users SET upload_mode=?, updated_at=CURRENT_TIMESTAMP WHERE user_id=?", (mode, user_id))

    def get_telegram_mode(self, user_id: int) -> str:
        self.ensure(user_id)
        row = self._query("SELECT telegram_mode FROM users WHERE user_id=?", (user_id,))
        mode = (row[0][0] if row and row[0][0] else "document").lower()
        return mode if mode in {"document", "media"} else "document"

    def set_telegram_mode(self, user_id: int, mode: str) -> None:
        if mode not in {"document", "media"}:
            raise ValueError("telegram mode must be document or media")
        self.ensure(user_id)
        self._write("UPDATE users SET telegram_mode=?, updated_at=CURRENT_TIMESTAMP WHERE user_id=?", (mode, user_id))

    def get_thumbnail(self, user_id: int) -> str | None:
        self.ensure(user_id)
        row = self._query("SELECT custom_thumbnail_path FROM users WHERE user_id=?", (user_id,))
        return row[0][0] if row and row[0][0] else None

    def set_thumbnail(self, user_id: int, path: str | None) -> None:
        self.ensure(user_id)
        self._write("UPDATE users SET custom_thumbnail_path=?, updated_at=CURRENT_TIMESTAMP WHERE user_id=?", (path, user_id))

    # ------------------------------------------------------------------
    # bulk / housekeeping preferences
    # ------------------------------------------------------------------
    def get_bulk_mode(self, user_id: int) -> bool:
        return self._flag(user_id, "bulk_mode")

    def set_bulk_mode(self, user_id: int, enabled: bool) -> None:
        self._set_flag(user_id, "bulk_mode", enabled)

    def get_auto_delete(self, user_id: int) -> bool:
        return self._flag(user_id, "auto_delete")

    def set_auto_delete(self, user_id: int, enabled: bool) -> None:
        self._set_flag(user_id, "auto_delete", enabled)

    def get_keep_files(self, user_id: int) -> bool:
        return self._flag(user_id, "keep_files")

    def set_keep_files(self, user_id: int, enabled: bool) -> None:
        self._set_flag(user_id, "keep_files", enabled)

    def user_count(self) -> int:
        return int(self._query("SELECT COUNT(*) FROM users")[0][0])

    def all_users(self) -> list[int]:
        return [int(r[0]) for r in self._query("SELECT user_id FROM users ORDER BY user_id")]

    def find_user(self, identifier: str) -> int | None:
        value = identifier.strip()
        if value.startswith("@"):
            value = value[1:]
        if value.lstrip("-").isdigit():
            row = self._query("SELECT user_id FROM users WHERE user_id=?", (int(value),))
            return int(row[0][0]) if row else None
        row = self._query("SELECT user_id FROM users WHERE lower(username)=lower(?)", (value,))
        return int(row[0][0]) if row else None

    # ------------------------------------------------------------------
    # direct links
    # ------------------------------------------------------------------
    def add_direct_link(self, token: str, user_id: int, path: str, expires_at: int) -> None:
        self._write(
            "INSERT OR REPLACE INTO direct_links(token,user_id,path,expires_at) VALUES(?,?,?,?)",
            (token, user_id, path, expires_at),
        )

    def get_direct_link(self, token: str):
        row = self._query("SELECT user_id,path,expires_at FROM direct_links WHERE token=?", (token,))
        return row[0] if row else None

    def active_direct_paths(self, now: int) -> set[str]:
        rows = self._query("SELECT path FROM direct_links WHERE expires_at >= ?", (now,))
        return {str(Path(row[0]).resolve()) for row in rows}

    def purge_direct_links(self, now: int) -> None:
        self._write("DELETE FROM direct_links WHERE expires_at < ?", (now,))

    def close(self):
        with self._lock:
            self.conn.close()
