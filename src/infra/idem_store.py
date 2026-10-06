"""SQLite-backed idempotency store. Prevents duplicate task creation."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

log = logging.getLogger("idem_store")

DEFAULT_DB_PATH = "config/.idem.db"

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS seen_items (
    idem_key TEXT PRIMARY KEY,
    recording_id TEXT NOT NULL,
    tw_uuid TEXT,
    title TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    status TEXT DEFAULT 'created'
);
"""


class IdemStore:
    def __init__(self, db_path: str | Path | None = None):
        if db_path is None:
            db_path = Path(__file__).parent.parent.parent / DEFAULT_DB_PATH
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path))
        self._conn.execute(_CREATE_SQL)
        self._conn.commit()

    def exists(self, idem_key: str) -> bool:
        row = self._conn.execute(
            "SELECT status FROM seen_items WHERE idem_key = ?", (idem_key,)
        ).fetchone()
        return row is not None

    def get_status(self, idem_key: str) -> str | None:
        row = self._conn.execute(
            "SELECT status FROM seen_items WHERE idem_key = ?", (idem_key,)
        ).fetchone()
        return row[0] if row else None

    def record(
        self,
        idem_key: str,
        recording_id: str,
        title: str,
        tw_uuid: str | None = None,
        status: str = "created",
    ) -> bool:
        try:
            self._conn.execute(
                "INSERT OR IGNORE INTO seen_items (idem_key, recording_id, tw_uuid, title, status) "
                "VALUES (?, ?, ?, ?, ?)",
                (idem_key, recording_id, tw_uuid, title, status),
            )
            self._conn.commit()
            return True
        except sqlite3.Error as e:
            log.error(f"Failed to record idem_key {idem_key}: {e}")
            return False

    def update_status(self, idem_key: str, status: str, tw_uuid: str | None = None) -> None:
        if tw_uuid:
            self._conn.execute(
                "UPDATE seen_items SET status = ?, tw_uuid = ? WHERE idem_key = ?",
                (status, tw_uuid, idem_key),
            )
        else:
            self._conn.execute(
                "UPDATE seen_items SET status = ? WHERE idem_key = ?",
                (status, idem_key),
            )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
