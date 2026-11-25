"""Storage interface shared by the action ledger, the history store and the change log.

The three stores keep their logic in plain Python and talk to a very small
keyed-document interface, so a backend only has to implement six methods.
Two backends ship: ``MemoryBackend`` (tests, notebooks, one-off runs) and
``SQLiteBackend`` (one local file, standard library only).  Anything else
(a warehouse table, a document database) can be added by implementing
``StorageBackend``.

A *table* is a namespace; a *key* is a tuple of strings; a *row* is a
JSON-serialisable dict.  Rows are copied on the way in and on the way out,
so callers can never mutate stored state by accident, and both backends
behave identically.
"""

from __future__ import annotations

import json
from contextlib import AbstractContextManager
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any, Mapping, Protocol, runtime_checkable

__all__ = ["Key", "StorageBackend", "encode_key", "decode_key", "dumps", "loads", "to_plain"]

Key = tuple[str, ...]


def _default(o: Any) -> Any:
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, Enum):
        return o.value
    if isinstance(o, (set, frozenset)):
        return sorted(o, key=str)
    if is_dataclass(o) and not isinstance(o, type):
        return asdict(o)
    return str(o)


def dumps(row: Any) -> str:
    """Serialise a row to compact JSON (dates as ISO strings, enums as values, sets as sorted lists)."""
    return json.dumps(row, default=_default, separators=(",", ":"))


def loads(text: str) -> Any:
    """Inverse of ``dumps``."""
    return json.loads(text)


def to_plain(obj: Any) -> Any:
    """Turn a dataclass / mapping / object with ``as_dict`` into plain JSON-ready data."""
    if hasattr(obj, "as_dict") and callable(obj.as_dict):
        obj = obj.as_dict()
    elif is_dataclass(obj) and not isinstance(obj, type):
        obj = asdict(obj)
    elif isinstance(obj, Mapping):
        obj = dict(obj)
    return loads(dumps(obj))


def encode_key(key: Key) -> str:
    """Encode a key tuple as a JSON array string (stable, prefix-comparable)."""
    return json.dumps([str(k) for k in key], separators=(",", ":"))


def decode_key(text: str) -> Key:
    return tuple(json.loads(text))


@runtime_checkable
class StorageBackend(Protocol):
    """Keyed-document storage.  All methods are synchronous and thread-safe."""

    def get(self, table: str, key: Key) -> dict[str, Any] | None:
        """The row at ``key`` or ``None``."""
        ...

    def put(self, table: str, key: Key, row: Mapping[str, Any]) -> None:
        """Insert or replace the row at ``key``."""
        ...

    def put_if_absent(self, table: str, key: Key, row: Mapping[str, Any]) -> bool:
        """Insert only when ``key`` is free.  Returns True when the row was written."""
        ...

    def delete(self, table: str, key: Key) -> bool:
        """Remove the row at ``key``.  Returns True when a row existed."""
        ...

    def scan(self, table: str, prefix: Key = ()) -> list[tuple[Key, dict[str, Any]]]:
        """All ``(key, row)`` pairs whose key starts with ``prefix``, sorted by key."""
        ...

    def transaction(self) -> AbstractContextManager[Any]:
        """A re-entrant scope inside which reads and writes are atomic with respect to other writers."""
        ...
