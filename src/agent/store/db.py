"""Connection + low-level repo helpers for the SQLite state store.

Plain sqlite3 with parameterized queries (no heavy ORM, per spec §2). This
module owns the connection and schema bootstrap; higher-level adapters
(internal CRM, approvals, audit) build on top of it.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def now_iso() -> str:
    """UTC ISO-8601 timestamp — stored as TEXT for Postgres portability."""
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return uuid.uuid4().hex


class Database:
    """Thin wrapper around a sqlite3 connection with schema bootstrap."""

    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: the v1 loop is synchronous but the web view
        # may read from another thread. Writes remain serialized by SQLite.
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._bootstrap()

    def _bootstrap(self) -> None:
        self.conn.executescript(_SCHEMA_PATH.read_text())
        self.conn.commit()

    # ── generic helpers ──

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def query_all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def close(self) -> None:
        self.conn.close()