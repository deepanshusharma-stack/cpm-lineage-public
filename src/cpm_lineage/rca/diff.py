"""Run-over-run diff: what changed since the last time this scope was analysed.

Snapshots are plain records; history is a list the caller stores wherever
it likes (this module does no I/O).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Any, Mapping, Sequence

from .attribute import CAUSE_TITLES, Attribution
from .config import DEFAULT_RCA_CONFIG, RcaConfig
from .facts import Facts

AXIS_LABELS: tuple[tuple[str, str], ...] = (
    ("own_pct", "own sales move (%)"),
    ("segment_pct", "segment move (%)"),
    ("share_c", "share (%)"),
    ("availability", "availability"),
    ("sov", "ad SOV (%)"),
    ("roas", "direct ROAS"),
)


@dataclass(frozen=True)
class Snapshot:
    """The comparable summary of one run."""

    feed_max: date
    days: int
    own_pct: float
    segment_pct: float
    share_c: float
    availability: float | None
    sov: float | None
    roas: float | None
    quadrant: str
    causes: tuple[str, ...]      # sorted, de-duplicated cause keys

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["feed_max"] = self.feed_max.isoformat()
        d["causes"] = list(self.causes)
        return d

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Snapshot":
        fm = d["feed_max"]
        return cls(
            feed_max=fm if isinstance(fm, date) else date.fromisoformat(str(fm)), days=int(d["days"]),
            own_pct=d["own_pct"], segment_pct=d["segment_pct"], share_c=d["share_c"],
            availability=d.get("availability"), sov=d.get("sov"), roas=d.get("roas"),
            quadrant=d["quadrant"], causes=tuple(sorted(set(d.get("causes", ())))),
        )


def snapshot(facts: Facts, attr: Attribution) -> Snapshot:
    """Summarise a run for later comparison: headline moves, share, availability, SOV, ROAS, quadrant, causes."""
    f = facts.funnel
    return Snapshot(
        feed_max=facts.scope.feed_max, days=facts.scope.days, own_pct=attr.own_pct,
        segment_pct=attr.segment_pct, share_c=attr.share_c,
        availability=facts.availability.own_c if facts.availability else None,
        sov=facts.voice.sov_c if facts.voice else None,
        roas=f.roas_c if f is not None and not f.stale else None,
        quadrant=attr.quadrant, causes=tuple(sorted(set(attr.cause_keys))),
    )


def previous_snapshot(history: Sequence[Snapshot], current: Snapshot) -> Snapshot | None:
    """The latest stored snapshot with an EARLIER feed date than the current run, or ``None``."""
    older = [s for s in history if s.feed_max < current.feed_max]
    return max(older, key=lambda s: s.feed_max) if older else None


def diff_snapshots(previous: Snapshot, current: Snapshot, config: RcaConfig | None = None) -> list[str]:
    """List what changed between two runs.

    Lines, in order: each axis (own move, segment move, share,
    availability, SOV, ROAS) whose value differs at ``diff_decimals``
    places; newly fired causes; resolved causes; a quadrant change.
    Identical snapshots produce an empty list (silent).
    """
    c = config or DEFAULT_RCA_CONFIG
    nd = c.diff_decimals
    lines: list[str] = []
    for name, label in AXIS_LABELS:
        a, b = getattr(previous, name), getattr(current, name)
        if a is None and b is None:
            continue
        if a is None or b is None:
            lines.append(f"{label}: {'n/a' if a is None else f'{a:.{nd}f}'} -> {'n/a' if b is None else f'{b:.{nd}f}'}")
        elif round(a, nd) != round(b, nd):
            lines.append(f"{label}: {a:.{nd}f} -> {b:.{nd}f}")
    new = [k for k in current.causes if k not in previous.causes]
    gone = [k for k in previous.causes if k not in current.causes]
    if new:
        lines.append("newly fired: " + ", ".join(CAUSE_TITLES.get(k, k) for k in new))
    if gone:
        lines.append("resolved: " + ", ".join(CAUSE_TITLES.get(k, k) for k in gone))
    if previous.quadrant != current.quadrant:
        lines.append(f"quadrant: {previous.quadrant} -> {current.quadrant}")
    return lines


def since_last(history: Sequence[Snapshot], current: Snapshot, config: RcaConfig | None = None) -> list[str]:
    """Diff the current run against the last earlier run.  First run, or nothing changed: an empty list."""
    prev = previous_snapshot(history, current)
    return diff_snapshots(prev, current, config) if prev is not None else []


def update_history(history: Sequence[Snapshot], current: Snapshot, config: RcaConfig | None = None) -> list[Snapshot]:
    """Return a new history with the current snapshot added.

    One snapshot per feed date (a re-run on the same date replaces the old
    one), sorted by feed date, trimmed to the last ``history_depth``.  The
    input is not mutated.
    """
    c = config or DEFAULT_RCA_CONFIG
    kept = [s for s in history if s.feed_max != current.feed_max] + [current]
    kept.sort(key=lambda s: s.feed_max)
    return kept[-c.history_depth:]
