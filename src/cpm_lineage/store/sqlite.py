"""SQLite storage backend: one file, standard library only."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterator, Mapping

from .base import Key, decode_key, dumps, encode_key, loads

__all__ = ["SQLiteBackend"]


class SQLiteBackend:
    """``StorageBackend`` over a single SQLite database.

    ``path`` is a file path or ``":memory:"``.  One connection is kept open
    and every call goes through a re-entrant lock, so the object can be
    shared between threads; ``timeout`` seconds are allowed for another
    process holding the file.  ``transaction()`` takes the write lock up
    front (``BEGIN IMMEDIATE``), so a check-then-write inside it cannot race
    another writer, and rolls back on an exception.

    All tables of the stores live in one physical table keyed by
    ``(namespace, key)``; rows are JSON text.
    """

    def __init__(self, path: str = ":memory:", timeout: float = 30.0) -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._depth = 0
        self._con = sqlite3.connect(self.path, timeout=timeout, check_same_thread=False, isolation_level=None)
        with self._lock:
            self._con.execute(
                "CREATE TABLE IF NOT EXISTS documents ("
                "namespace TEXT NOT NULL, doc_key TEXT NOT NULL, body TEXT NOT NULL, "
                "PRIMARY KEY (namespace, doc_key))")

    def get(self, table: str, key: Key) -> dict[str, Any] | None:
        with self._lock:
            row = self._con.execute(
                "SELECT body FROM documents WHERE namespace = ? AND doc_key = ?", (table, encode_key(key))).fetchone()
        return None if row is None else loads(row[0])

    def put(self, table: str, key: Key, row: Mapping[str, Any]) -> None:
        with self._lock:
            self._con.execute(
                "INSERT OR REPLACE INTO documents (namespace, doc_key, body) VALUES (?, ?, ?)",
                (table, encode_key(key), dumps(dict(row))))

    def put_if_absent(self, table: str, key: Key, row: Mapping[str, Any]) -> bool:
        with self._lock:
            cur = self._con.execute(
                "INSERT OR IGNORE INTO documents (namespace, doc_key, body) VALUES (?, ?, ?)",
                (table, encode_key(key), dumps(dict(row))))
            return cur.rowcount > 0

    def delete(self, table: str, key: Key) -> bool:
        with self._lock:
            cur = self._con.execute(
                "DELETE FROM documents WHERE namespace = ? AND doc_key = ?", (table, encode_key(key)))
            return cur.rowcount > 0

    def scan(self, table: str, prefix: Key = ()) -> list[tuple[Key, dict[str, Any]]]:
        # An encoded key is a JSON array; dropping the closing bracket of the
        # encoded prefix gives a string every matching key starts with.
        head = encode_key(prefix)[:-1]
        with self._lock:
            rows = self._con.execute(
                "SELECT doc_key, body FROM documents WHERE namespace = ? AND substr(doc_key, 1, ?) = ?",
                (table, len(head), head)).fetchall()
        p = tuple(map(str, prefix))
        out = [(decode_key(k), loads(b)) for k, b in rows]
        return sorted((kv for kv in out if kv[0][: len(p)] == p), key=lambda kv: kv[0])

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._lock:
            outer = self._depth == 0
            if outer:
                self._con.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield
            except BaseException:
                self._depth -= 1
                if outer:
                    self._con.execute("ROLLBACK")
                raise
            else:
                self._depth -= 1
                if outer:
                    self._con.execute("COMMIT")

    def close(self) -> None:
        """Close the connection."""
        with self._lock:
            self._con.close()
