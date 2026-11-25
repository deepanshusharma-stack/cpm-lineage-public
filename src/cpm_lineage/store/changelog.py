"""The change log: what was changed on which entity, and what moved afterwards.

Three pieces:

* ``diff_versions``: turn stored configuration snapshots (status, daily
  budget, bid per entity over time) into a clean list of input changes,
  netted within a day and grouped across days;
* ``input_change_ledger``: put a before / after spend and ROAS read next to
  each change and rank the changes by how much spend moved.  This is a
  before / after mean split, not a causal model, and is labelled as such;
* ``ChangeLog``: persistence for ``ChangeEvent`` records and configuration
  snapshots behind a ``StorageBackend``.  The outcome evaluator reads it to
  check whether a recommendation was actually executed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence

from ..liveness import canonical_status
from ..schema import CanonicalStatus, ChangeEvent, PerfRow
from .base import StorageBackend
from .memory import MemoryBackend

__all__ = [
    "ChangeLogConfig", "ConfigVersion", "diff_versions", "input_change_ledger", "ChangeLog",
    "CHANGES", "VERSIONS",
]

CHANGES = "change_events"
VERSIONS = "config_versions"


@dataclass(frozen=True)
class ChangeLogConfig:
    """Fields diffed and the before / after read around each change."""

    fields: tuple[str, ...] = ("status", "daily_budget", "bid")   # configuration inputs tracked
    status_field: str = "status"                                  # the field whose platform-set values are ignored
    platform_set_statuses: tuple[str, ...] = (                    # states the platform sets by itself: not an input change
        CanonicalStatus.BUDGET_HELD.value, CanonicalStatus.PLATFORM_PAUSED.value)
    before_days: int = 7             # read days [-7, 0) before the change
    after_start: int = 1             # skip the change day itself ...
    after_days: int = 7              # ... then read days [1, 8)
    material_change: float = 0.10    # a non-status change must move the value by >= 10% to make the shortlist
    top_n: int = 3                   # shortlist size, one per entity


@dataclass(frozen=True)
class ConfigVersion:
    """One stored snapshot of an entity's configuration."""

    entity_id: str
    synced_at: datetime
    status: str | None = None
    daily_budget: float | None = None
    bid: float | None = None
    name: str | None = None


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def diff_versions(
    versions: Iterable[ConfigVersion],
    since: date,
    status_map: Mapping[str, str] | None = None,
    cfg: ChangeLogConfig | None = None,
) -> list[dict[str, Any]]:
    """Derive input changes from configuration snapshots.

    1. Per entity, snapshots are ordered by sync time.  For consecutive
       snapshots where the later one is on or after ``since`` (earlier ones
       only seed the baseline), emit ``{entity_id, name, date, field, frm,
       to}`` for each tracked field whose value changed and is non-null on
       both sides.  Status changes where either side is a platform-set state
       (budget-held, platform-paused; resolved through ``status_map``) are
       skipped: the platform did that, not an operator.
    2. Same-day netting per ``(entity, field, day)``: first ``frm`` to last
       ``to``; a same-day revert emits nothing.
    3. Grouping: identical ``(entity, field, frm, to)`` on several days
       collapse to one event with a count ``n`` and the latest date (a
       day-parting rhythm is grouped, not hidden).
    4. Sorted by date.

    Returns the list of event dicts.
    """
    cfg = cfg or ChangeLogConfig()
    by_entity: dict[str, list[ConfigVersion]] = {}
    for v in versions:
        by_entity.setdefault(v.entity_id, []).append(v)
    platform_set = set(cfg.platform_set_statuses)
    netted: dict[tuple[str, str, date], list[Any]] = {}
    for eid, vs in by_entity.items():
        vs.sort(key=lambda v: v.synced_at)
        for prev, cur in zip(vs, vs[1:]):
            day = cur.synced_at.date()
            if day < since:
                continue
            for f in cfg.fields:
                a, b = getattr(prev, f, None), getattr(cur, f, None)
                if a is None or b is None or a == b:
                    continue
                if f == cfg.status_field and (
                        canonical_status(a, status_map).value in platform_set
                        or canonical_status(b, status_map).value in platform_set):
                    continue
                slot = netted.get((eid, f, day))
                if slot is None:
                    netted[(eid, f, day)] = [a, b, cur.name or prev.name]
                else:
                    slot[1] = b
    grouped: dict[tuple[str, str, Any, Any], dict[str, Any]] = {}
    for (eid, f, day), (a, b, name) in sorted(netted.items(), key=lambda kv: kv[0][2]):
        if a == b:
            continue
        g = grouped.get((eid, f, a, b))
        if g is None:
            grouped[(eid, f, a, b)] = {"entity_id": eid, "name": name, "date": day, "field": f,
                                       "frm": a, "to": b, "n": 1}
        else:
            g["n"] += 1
            g["date"] = max(g["date"], day)
    return sorted(grouped.values(), key=lambda e: (e["date"], e["entity_id"], e["field"]))


def input_change_ledger(
    events: Sequence[Mapping[str, Any]],
    rows: Iterable[PerfRow],
    cfg: ChangeLogConfig | None = None,
) -> dict[str, Any]:
    """Attach a before / after read to each input change and shortlist the biggest movers.

    For each event (as produced by ``diff_versions``) on day ``d``:
    ``before`` = the entity's days in ``[d - 7, d)``, ``after`` = its days in
    ``[d + 1, d + 8)``.  For each side, ``spend`` is the mean daily spend
    over days with data and ``roas = sum(sales) / sum(spend)``;
    ``moved = |spend_after - spend_before| * 7`` (a missing side counts as
    zero spend).

    ``events`` come back sorted by ``moved`` descending.  ``top`` holds at
    most ``cfg.top_n`` of them: one per entity, and a non-status change only
    qualifies when it is material (``|to - frm| >= 10% of |frm|``).

    Stated limit: a before / after mean split with no causal model; other
    things moved in the same days.  Returns ``{"events", "top", "method"}``.
    """
    cfg = cfg or ChangeLogConfig()
    daily: dict[str, dict[date, list[float]]] = {}
    for r in rows:
        t = daily.setdefault(r.entity_id, {}).setdefault(r.date, [0.0, 0.0])
        t[0] += r.spend or 0.0
        t[1] += r.sales or 0.0

    def read(eid: str, start: date, n: int) -> tuple[float | None, float | None]:
        days = [daily.get(eid, {}).get(start + timedelta(days=i)) for i in range(n)]
        have = [d for d in days if d is not None]
        if not have:
            return None, None
        sp = sum(d[0] for d in have)
        return sp / len(have), (sum(d[1] for d in have) / sp if sp > 0 else None)

    out = []
    for e in events:
        d = _as_date(e["date"])
        sb, rb = read(e["entity_id"], d - timedelta(days=cfg.before_days), cfg.before_days)
        sa, ra = read(e["entity_id"], d + timedelta(days=cfg.after_start), cfg.after_days)
        out.append({**e, "spend_before": sb, "spend_after": sa, "roas_before": rb, "roas_after": ra,
                    "moved": abs((sa or 0.0) - (sb or 0.0)) * cfg.after_days})
    out.sort(key=lambda e: -e["moved"])
    top: list[dict[str, Any]] = []
    seen: set[str] = set()
    for e in out:
        if len(top) >= cfg.top_n:
            break
        if e["entity_id"] in seen:
            continue
        if e["field"] != cfg.status_field:
            try:
                frm, to = float(e["frm"]), float(e["to"])
            except (TypeError, ValueError):
                continue
            if abs(to - frm) < cfg.material_change * abs(frm):
                continue
        seen.add(e["entity_id"])
        top.append(e)
    return {"events": out, "top": top, "method": "before/after mean split, not causal"}


class ChangeLog:
    """Persistence for change events and configuration snapshots."""

    def __init__(self, backend: StorageBackend | None = None, cfg: ChangeLogConfig | None = None) -> None:
        self.backend: StorageBackend = backend if backend is not None else MemoryBackend()
        self.cfg = cfg or ChangeLogConfig()

    def append(self, account: str, platform: str, events: Iterable[ChangeEvent]) -> int:
        """Store change events; an identical event (same time, entity, field, target, new value) is stored once.

        Returns the number of new events written.
        """
        n = 0
        with self.backend.transaction():
            for e in events:
                key = (account, platform, e.entity_id, e.timestamp.isoformat(), e.field,
                       e.targeting_unit or "", str(e.new))
                n += self.backend.put_if_absent(CHANGES, key, {
                    "timestamp": e.timestamp.isoformat(), "entity_id": e.entity_id, "field": e.field,
                    "old": e.old, "new": e.new, "source": e.source, "automated": e.automated,
                    "verified": e.verified, "targeting_unit": e.targeting_unit})
        return n

    def events(
        self,
        account: str,
        platform: str,
        entity_id: str | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> list[ChangeEvent]:
        """Stored events, oldest first, optionally for one entity and with ``start <= day < end``."""
        prefix = (account, platform) if entity_id is None else (account, platform, entity_id)
        out = []
        for _k, r in self.backend.scan(CHANGES, prefix):
            ts = datetime.fromisoformat(r["timestamp"])
            if (start is not None and ts.date() < start) or (end is not None and ts.date() >= end):
                continue
            out.append(ChangeEvent(timestamp=ts, entity_id=r["entity_id"], field=r["field"], old=r.get("old"),
                                   new=r.get("new"), source=r.get("source"), automated=bool(r.get("automated")),
                                   verified=r.get("verified"), targeting_unit=r.get("targeting_unit")))
        out.sort(key=lambda e: e.timestamp)
        return out

    def record_versions(self, account: str, platform: str, versions: Iterable[ConfigVersion]) -> int:
        """Store configuration snapshots (one per entity and sync time).  Returns the new rows written."""
        n = 0
        with self.backend.transaction():
            for v in versions:
                n += self.backend.put_if_absent(
                    VERSIONS, (account, platform, v.entity_id, v.synced_at.isoformat()),
                    {"entity_id": v.entity_id, "synced_at": v.synced_at.isoformat(), "status": v.status,
                     "daily_budget": v.daily_budget, "bid": v.bid, "name": v.name})
        return n

    def versions(self, account: str, platform: str, entity_id: str | None = None) -> list[ConfigVersion]:
        """Stored snapshots, oldest first per entity."""
        prefix = (account, platform) if entity_id is None else (account, platform, entity_id)
        out = [ConfigVersion(entity_id=r["entity_id"], synced_at=datetime.fromisoformat(r["synced_at"]),
                             status=r.get("status"), daily_budget=r.get("daily_budget"), bid=r.get("bid"),
                             name=r.get("name"))
               for _k, r in self.backend.scan(VERSIONS, prefix)]
        out.sort(key=lambda v: (v.entity_id, v.synced_at))
        return out

    def changes_since(self, account: str, platform: str, since: date,
                      status_map: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
        """``diff_versions`` over the stored snapshots."""
        return diff_versions(self.versions(account, platform), since, status_map, self.cfg)
