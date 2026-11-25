"""In-memory storage backend: a dict of dicts behind a re-entrant lock."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any, Iterator, Mapping

from .base import Key, dumps, loads

__all__ = ["MemoryBackend"]


class MemoryBackend:
    """Process-local ``StorageBackend``.

    Rows are held as JSON text, so what comes back is exactly what a durable
    backend would return (no shared references, tuples become lists).
    ``transaction`` is a lock scope; there is no rollback, which is safe here
    because the stores validate before they write.
    """

    def __init__(self) -> None:
        self._tables: dict[str, dict[Key, str]] = {}
        self._lock = threading.RLock()

    def get(self, table: str, key: Key) -> dict[str, Any] | None:
        with self._lock:
            text = self._tables.get(table, {}).get(tuple(map(str, key)))
        return None if text is None else loads(text)

    def put(self, table: str, key: Key, row: Mapping[str, Any]) -> None:
        with self._lock:
            self._tables.setdefault(table, {})[tuple(map(str, key))] = dumps(dict(row))

    def put_if_absent(self, table: str, key: Key, row: Mapping[str, Any]) -> bool:
        k = tuple(map(str, key))
        with self._lock:
            t = self._tables.setdefault(table, {})
            if k in t:
                return False
            t[k] = dumps(dict(row))
            return True

    def delete(self, table: str, key: Key) -> bool:
        with self._lock:
            return self._tables.get(table, {}).pop(tuple(map(str, key)), None) is not None

    def scan(self, table: str, prefix: Key = ()) -> list[tuple[Key, dict[str, Any]]]:
        p = tuple(map(str, prefix))
        with self._lock:
            items = [(k, v) for k, v in self._tables.get(table, {}).items() if k[: len(p)] == p]
        return [(k, loads(v)) for k, v in sorted(items)]

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._lock:
            yield

    def close(self) -> None:
        """Nothing to release."""
