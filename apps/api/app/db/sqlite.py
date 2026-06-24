"""SQLite store (WAL mode) — small transactional app state where DuckDB's
single-writer model is awkward: watchlist, dashboard layout, scraper cursors,
HTTP ETag cache metadata, and news-dedupe hashes.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Sequence


class SqliteStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        # check_same_thread=False: we guard every access with our own lock and
        # call across the scheduler thread / executor pool.
        self._con = sqlite3.connect(str(path), check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        self._con.execute("PRAGMA journal_mode=WAL;")
        self._con.execute("PRAGMA synchronous=NORMAL;")
        self._con.execute("PRAGMA foreign_keys=ON;")

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> None:
        with self._lock:
            self._con.execute(sql, tuple(params) if params else ())
            self._con.commit()

    def execute_returning_id(self, sql: str, params: Sequence[Any] | None = None) -> int:
        """INSERT and return its rowid atomically. cursor.lastrowid is read
        WHILE the lock is still held, so a concurrent INSERT on the shared
        connection (from another thread) can never poison the id — unlike a
        separate ``SELECT last_insert_rowid()`` which re-acquires the lock."""
        with self._lock:
            cur = self._con.execute(sql, tuple(params) if params else ())
            rowid = cur.lastrowid
            self._con.commit()
            return int(rowid)

    def executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        if not rows:
            return
        with self._lock:
            self._con.executemany(sql, [tuple(r) for r in rows])
            self._con.commit()

    def fetchall(self, sql: str, params: Sequence[Any] | None = None) -> list[sqlite3.Row]:
        with self._lock:
            return self._con.execute(sql, tuple(params) if params else ()).fetchall()

    def fetchone(self, sql: str, params: Sequence[Any] | None = None) -> sqlite3.Row | None:
        with self._lock:
            return self._con.execute(sql, tuple(params) if params else ()).fetchone()

    def close(self) -> None:
        with self._lock:
            self._con.close()
