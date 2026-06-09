"""DuckDB store — columnar home for ALL time-series (prices, macro, sentiment,
retail mentions, trades).

DuckDB allows a single read-write connection per process. We hold exactly one
and serialize every access behind a threading.Lock so the API event loop, the
scheduler thread, and the inference thread-pool can never corrupt a write.
Heavy/blocking calls should be dispatched via ``run_in_executor`` from async
code so the event loop is never blocked on the lock.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Sequence

import duckdb


class DuckStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._con = duckdb.connect(str(path))

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> None:
        with self._lock:
            self._con.execute(sql, list(params) if params else None)

    def executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        if not rows:
            return
        with self._lock:
            self._con.executemany(sql, [list(r) for r in rows])

    def fetchall(self, sql: str, params: Sequence[Any] | None = None) -> list[tuple]:
        with self._lock:
            return self._con.execute(sql, list(params) if params else None).fetchall()

    def fetchone(self, sql: str, params: Sequence[Any] | None = None) -> tuple | None:
        with self._lock:
            return self._con.execute(sql, list(params) if params else None).fetchone()

    def fetchdf(self, sql: str, params: Sequence[Any] | None = None):
        """Return a pandas DataFrame (used by the correlation/cookbook engine)."""
        with self._lock:
            return self._con.execute(sql, list(params) if params else None).fetchdf()

    def table_counts(self) -> dict[str, int]:
        """Row counts per user table — handy for the /health snapshot."""
        with self._lock:
            tables = [
                r[0]
                for r in self._con.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'main' ORDER BY table_name"
                ).fetchall()
            ]
            out: dict[str, int] = {}
            for t in tables:
                out[t] = self._con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            return out

    def close(self) -> None:
        with self._lock:
            self._con.close()
