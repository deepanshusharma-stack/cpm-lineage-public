"""Liveness: can an entity be acted on, and should it appear in an analysis.

One definition for the whole library.  Three questions, three functions:

* ``is_dead`` -- "can I act on this today?"  Used by every action surface.
* ``stale_stopped`` -- "should this appear in an analysis at all?"
* ``dead_for_window`` -- retrospective reports only: anything that spent
  inside the reported window is analysed whatever its status is now.  The
  action engine must never call it.  ``stopped_since_window`` marks entities
  that were live in the window but are dead now (analyse, flag, do not act).

Statuses are classed three ways (``schema.LivenessClass``):

* hard dead (operator paused, ended, removed): dead regardless of spend;
* soft dead (budget-held today, platform-reported pause, pre-launch): dead
  **only if spend in the last few days is zero** -- a budget-held entity
  resumes tomorrow and is exactly the budget-bound winner, and a pre-launch
  state that is spending was approved without the status catching up;
* anything else, including unknown strings: live.

The raw status vocabulary belongs to the adapter.  Each function accepts a
``CanonicalStatus``, a ``LivenessClass``, or a raw string resolved through
``status_map`` (falling back to ``DEFAULT_STATUS_MAP``, a generic vocabulary).
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable, Mapping

from .config import LivenessConfig
from .schema import CanonicalStatus, Entity, LivenessClass, PerfRow

__all__ = [
    "DEFAULT_STATUS_MAP", "HARD_DEAD_STATUSES", "SOFT_DEAD_STATUSES",
    "normalise_status", "canonical_status", "liveness_class", "is_dead",
    "stale_stopped", "dead_for_window", "stopped_since_window",
    "recent_spend", "entity_is_dead",
]

HARD_DEAD_STATUSES: frozenset[CanonicalStatus] = frozenset(
    {CanonicalStatus.OPERATOR_PAUSED, CanonicalStatus.ENDED, CanonicalStatus.REMOVED}
)
SOFT_DEAD_STATUSES: frozenset[CanonicalStatus] = frozenset(
    {CanonicalStatus.BUDGET_HELD, CanonicalStatus.PLATFORM_PAUSED, CanonicalStatus.PRE_LAUNCH}
)

# Generic status words.  Adapters override or extend this for their platform.
DEFAULT_STATUS_MAP: Mapping[str, CanonicalStatus] = {
    "active": CanonicalStatus.ACTIVE,
    "live": CanonicalStatus.ACTIVE,
    "running": CanonicalStatus.ACTIVE,
    "enabled": CanonicalStatus.ACTIVE,
    "on_hold": CanonicalStatus.BUDGET_HELD,
    "budget_held": CanonicalStatus.BUDGET_HELD,
    "budget_exhausted": CanonicalStatus.BUDGET_HELD,
    "out_of_budget": CanonicalStatus.BUDGET_HELD,
    "platform_paused": CanonicalStatus.PLATFORM_PAUSED,
    "platform_stopped": CanonicalStatus.PLATFORM_PAUSED,
    "under_review": CanonicalStatus.PRE_LAUNCH,
    "draft": CanonicalStatus.PRE_LAUNCH,
    "scheduled": CanonicalStatus.PRE_LAUNCH,
    "pending": CanonicalStatus.PRE_LAUNCH,
    "pre_launch": CanonicalStatus.PRE_LAUNCH,
    "paused": CanonicalStatus.OPERATOR_PAUSED,
    "stopped": CanonicalStatus.OPERATOR_PAUSED,
    "operator_paused": CanonicalStatus.OPERATOR_PAUSED,
    "disabled": CanonicalStatus.OPERATOR_PAUSED,
    "completed": CanonicalStatus.ENDED,
    "ended": CanonicalStatus.ENDED,
    "archived": CanonicalStatus.REMOVED,
    "removed": CanonicalStatus.REMOVED,
    "deleted": CanonicalStatus.REMOVED,
    "unknown": CanonicalStatus.UNKNOWN,
}

StatusLike = CanonicalStatus | LivenessClass | str | None


def normalise_status(raw: str | None) -> str:
    """Lower-case, trim and fold spaces / hyphens to underscores."""
    return (raw or "").strip().lower().replace("-", "_").replace(" ", "_")


def canonical_status(
    status: StatusLike, status_map: Mapping[str, CanonicalStatus | str] | None = None
) -> CanonicalStatus:
    """Resolve a raw or canonical status to ``CanonicalStatus``.

    Lookup order for a raw string: the adapter's ``status_map`` (exact, then
    normalised), a ``CanonicalStatus`` value, then ``DEFAULT_STATUS_MAP``.
    Unmapped or missing statuses return ``UNKNOWN`` (which is live).
    """
    if isinstance(status, CanonicalStatus):
        return status
    if isinstance(status, LivenessClass):
        return {
            LivenessClass.LIVE: CanonicalStatus.ACTIVE,
            LivenessClass.SOFT_DEAD: CanonicalStatus.PLATFORM_PAUSED,
            LivenessClass.HARD_DEAD: CanonicalStatus.OPERATOR_PAUSED,
        }[status]
    if status is None:
        return CanonicalStatus.UNKNOWN
    key = normalise_status(status)
    if status_map:
        hit = status_map.get(status)
        if hit is None:
            hit = status_map.get(key)
        if hit is None:
            folded = {normalise_status(k): v for k, v in status_map.items()}
            hit = folded.get(key)
        if hit is not None:
            return CanonicalStatus(hit)
    try:
        return CanonicalStatus(key)
    except ValueError:
        pass
    return DEFAULT_STATUS_MAP.get(key, CanonicalStatus.UNKNOWN)


def liveness_class(
    status: StatusLike, status_map: Mapping[str, CanonicalStatus | str] | None = None
) -> LivenessClass:
    """Class a status as live, soft dead or hard dead."""
    if isinstance(status, LivenessClass):
        return status
    c = canonical_status(status, status_map)
    if c in HARD_DEAD_STATUSES:
        return LivenessClass.HARD_DEAD
    if c in SOFT_DEAD_STATUSES:
        return LivenessClass.SOFT_DEAD
    return LivenessClass.LIVE


def is_dead(
    status: StatusLike,
    spend_3d: float | None,
    *,
    status_map: Mapping[str, CanonicalStatus | str] | None = None,
) -> bool:
    """Decide whether an entity can be acted on today.

    Rule: hard-dead status -> dead; soft-dead status -> dead iff
    ``spend_3d <= 0`` (missing spend counts as 0); anything else -> live.
    ``spend_3d`` is spend over the last ``LivenessConfig.soft_dead_days``
    days of the feed.  Returns ``True`` when dead.
    """
    cls = liveness_class(status, status_map)
    if cls is LivenessClass.HARD_DEAD:
        return True
    if cls is LivenessClass.SOFT_DEAD:
        return (spend_3d or 0.0) <= 0.0
    return False


def stale_stopped(
    status: StatusLike,
    spend_recent: float | None,
    spend_3d: float | None = None,
    *,
    status_map: Mapping[str, CanonicalStatus | str] | None = None,
) -> bool:
    """Decide whether an entity should be dropped from an analysis.

    Rule: soft-dead status -> ``is_dead(status, spend_3d)`` (falling back to
    ``spend_recent`` when ``spend_3d`` is not given); hard-dead status ->
    stale only when ``spend_recent`` (spend over the last
    ``LivenessConfig.stale_days`` days) is 0; live -> never stale.
    Returns ``True`` when the entity should be hidden.
    """
    cls = liveness_class(status, status_map)
    if cls is LivenessClass.SOFT_DEAD:
        return is_dead(cls, spend_recent if spend_3d is None else spend_3d)
    if cls is LivenessClass.HARD_DEAD:
        return (spend_recent or 0.0) <= 0.0
    return False


def dead_for_window(
    status: StatusLike,
    spend_in_window: float | None,
    spend_3d: float | None,
    *,
    status_map: Mapping[str, CanonicalStatus | str] | None = None,
) -> bool:
    """Liveness for a retrospective report over a fixed window.

    Rule: any spend inside the reported window -> live, whatever the status
    is now; otherwise ``is_dead(status, spend_3d)``.  Never use this to
    decide whether to act.  Returns ``True`` when dead for the window.
    """
    if (spend_in_window or 0.0) > 0.0:
        return False
    return is_dead(status, spend_3d, status_map=status_map)


def stopped_since_window(
    status: StatusLike,
    spend_in_window: float | None,
    spend_3d: float | None,
    *,
    status_map: Mapping[str, CanonicalStatus | str] | None = None,
) -> bool:
    """True when the entity was live in the window but is dead now.

    ``not dead_for_window(...) and is_dead(...)``: analyse it, flag it, do
    not recommend an action on it.
    """
    return (not dead_for_window(status, spend_in_window, spend_3d, status_map=status_map)) and is_dead(
        status, spend_3d, status_map=status_map
    )


def recent_spend(
    rows: Iterable[PerfRow], anchor: date, days: int, entity_id: str | None = None
) -> float | dict[str, float]:
    """Spend over the last ``days`` days ending at ``anchor`` (inclusive).

    With ``entity_id`` returns that entity's spend as a float; without it
    returns ``{entity_id: spend}`` for every entity seen in ``rows`` (zero
    for entities with rows but none in range).
    """
    start = anchor - timedelta(days=days - 1)
    out: dict[str, float] = {}
    for r in rows:
        if entity_id is not None and r.entity_id != entity_id:
            continue
        add = r.spend if start <= r.date <= anchor else 0.0
        out[r.entity_id] = out.get(r.entity_id, 0.0) + add
    if entity_id is not None:
        return out.get(entity_id, 0.0)
    return out


def entity_is_dead(
    entity: Entity,
    *,
    status_map: Mapping[str, CanonicalStatus | str] | None = None,
    cfg: LivenessConfig | None = None,
) -> bool:
    """``is_dead`` on an ``Entity`` record using its ``status`` and ``spend_3d``.

    ``cfg`` is accepted for symmetry; the day count it carries is applied
    by whoever computes ``spend_3d`` (see ``recent_spend``).
    """
    return is_dead(entity.status, entity.spend_3d, status_map=status_map)
