"""Restatement detection: did a day's number change since it was first seen?

Feeds restate late days.  The detector keeps one small snapshot per calendar
day and compares today's reading of a day with the earliest reading on
record.  Persistence is behind the ``SnapshotStore`` interface; this module
performs no file I/O (an in-memory store is provided, a durable one belongs
in a storage backend).
"""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from ..schema import MetricCell
from .config import CpmConfig

__all__ = [
    "TOTAL_KEY", "SnapshotStore", "InMemorySnapshotStore", "snapshot_payload",
    "take_snapshot", "first_seen", "restated", "restated_days",
]

TOTAL_KEY = "~total"  # scope key of the grand total inside a snapshot

Reading = tuple[float, float]  # (spend, impressions)


def _reading(value: Any) -> Reading | None:
    if value is None:
        return None
    if isinstance(value, MetricCell):
        return (value.spend, value.impressions)
    if isinstance(value, Mapping):
        return (float(value.get("spend", 0.0)), float(value.get("impressions", 0.0)))
    spend, impressions = value[0], value[1]
    return (float(spend), float(impressions))


def _day_key(day: Any) -> str:
    return day.isoformat() if isinstance(day, date) else str(day)


def snapshot_payload(
    taken: date,
    cells: Mapping[str, Mapping[str, Mapping[Any, Any]]],
    last: date | None = None,
) -> dict:
    """Build one snapshot record.

    ``cells[scope][series][day]`` is a ``MetricCell``, a ``(spend,
    impressions)`` pair or a mapping with those keys.  Stored form:
    ``{taken, last, cells: {scope: {series: {iso day: [round(spend, 2),
    round(impressions)]}}}}`` (JSON-friendly).
    """
    out: dict[str, dict[str, dict[str, list[float]]]] = {}
    for scope, series_map in cells.items():
        for series, days in series_map.items():
            for day, value in days.items():
                reading = _reading(value)
                if reading is None:
                    continue
                out.setdefault(scope, {}).setdefault(series, {})[_day_key(day)] = [
                    round(reading[0], 2), float(round(reading[1])),
                ]
    return {"taken": taken.isoformat(), "last": last.isoformat() if last else None, "cells": out}


@runtime_checkable
class SnapshotStore(Protocol):
    """Where snapshots live.  Implementations must write atomically."""

    def has(self, taken: date) -> bool: ...
    def save(self, payload: Mapping[str, Any]) -> None: ...
    def load(self) -> Sequence[Mapping[str, Any]]: ...


class InMemorySnapshotStore:
    """Process-local snapshot store (tests, notebooks)."""

    def __init__(self) -> None:
        self._snaps: dict[str, Mapping[str, Any]] = {}

    def has(self, taken: date) -> bool:
        return taken.isoformat() in self._snaps

    def save(self, payload: Mapping[str, Any]) -> None:
        self._snaps[str(payload["taken"])] = payload

    def load(self) -> list[Mapping[str, Any]]:
        return [self._snaps[k] for k in sorted(self._snaps)]


def take_snapshot(
    store: SnapshotStore,
    taken: date,
    cells: Mapping[str, Mapping[str, Mapping[Any, Any]]],
    last: date | None = None,
) -> bool:
    """Persist today's snapshot once.  Idempotent: when a snapshot for
    ``taken`` already exists nothing is written.  Returns True if written."""
    if store.has(taken):
        return False
    store.save(snapshot_payload(taken, cells, last))
    return True


def first_seen(
    snapshots: Sequence[Mapping[str, Any]], scope: str = TOTAL_KEY
) -> dict[tuple[str, str], tuple[str, float, float]]:
    """Earliest reading on record per (series, day) for one scope.

    Snapshots are read oldest first (by ``taken``); the first reading of
    each (series, day) wins.  Returns ``{(series, iso day): (snapshot date,
    spend, impressions)}``.
    """
    out: dict[tuple[str, str], tuple[str, float, float]] = {}
    for snap in sorted(snapshots, key=lambda s: str(s.get("taken", ""))):
        for series, days in (snap.get("cells", {}).get(scope, {}) or {}).items():
            for day, value in days.items():
                key = (series, str(day))
                if key not in out:
                    out[key] = (str(snap.get("taken")), float(value[0]), float(value[1]))
    return out


def restated(first: Any, now: Any, tol: float | None = None, cfg: CpmConfig | None = None) -> bool:
    """Decide whether a day's figures were restated.

    True when ``abs(now.spend - first.spend) > tol * max(abs(first.spend),
    floor)`` or the same test holds on impressions (``tol`` 0.01, ``floor``
    1).  False when either reading is missing.  ``first`` may be a
    ``first_seen`` triple ``(taken, spend, impressions)``; both sides may be
    a cell, a pair or a mapping.
    """
    cfg = cfg or CpmConfig()
    tol = cfg.restate_tol if tol is None else tol
    if first is not None and not isinstance(first, (MetricCell, Mapping)) and len(first) == 3:
        first = first[1:]
    a, b = _reading(first), _reading(now)
    if a is None or b is None:
        return False
    return any(abs(n - f) > tol * max(abs(f), cfg.restate_floor) for f, n in zip(a, b))


def restated_days(
    first: Mapping[tuple[str, str], tuple[str, float, float]],
    now: Mapping[str, Mapping[Any, Any]],
    tol: float | None = None,
    cfg: CpmConfig | None = None,
) -> dict[tuple[str, str], dict]:
    """Compare current readings ``now[series][day]`` with ``first_seen``
    output.  Returns ``{(series, iso day): {first_taken, first, now}}`` for
    the restated days only."""
    out: dict[tuple[str, str], dict] = {}
    for series, days in now.items():
        for day, value in days.items():
            key = (series, _day_key(day))
            f = first.get(key)
            if f is not None and restated(f, value, tol, cfg):
                out[key] = {"first_taken": f[0], "first": (f[1], f[2]), "now": _reading(value)}
    return out
