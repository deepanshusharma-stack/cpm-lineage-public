"""Checkpoint scorecard: a closed loop on recommendations.

Each report *arms* typed, machine-gradable checkpoints against the values
it saw; the next report *grades* them MET / MISSED / PENDING against the
values it sees.  The engine is generic: a checkpoint is a mapping with a
``kind``, a grader is a function registered for that kind.

Storage is pluggable (``BaselineStore``); this module does no file I/O.
One baseline is kept per (account, scope) - there is no history.

Built-in kinds
--------------
``budget_roas`` and ``waste_bleed`` are the generic built-ins.  The other
four (``availability_floor``, ``rival_share``, ``tier_share``,
``discount_hold``) are retail-media examples shipped for convenience;
register your own with ``register_grader``.

Units: shares and discounts are in percentage points on a 0..100 scale;
availability uses whatever scale the checkpoint's ``target`` uses.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping, Protocol, Sequence

__all__ = [
    "MET", "MISSED", "PENDING", "ScorecardConfig", "Baseline", "GradeRow", "BaselineStore",
    "InMemoryBaselineStore", "Grader", "GRADERS", "register_grader", "arm", "grade",
    "grade_checkpoint", "budget_roas_checkpoint", "waste_bleed_checkpoint",
]

MET, MISSED, PENDING = "MET", "MISSED", "PENDING"


@dataclass(frozen=True)
class ScorecardConfig:
    """Grading thresholds (every field has a default)."""

    min_age_days: float = 3.0          # a baseline younger than this is not overwritten: re-runs must not wipe the week's yardstick
    too_soon_days: float = 5.0         # time-dependent checkpoints cannot be MISSED against a baseline younger than this
    bleed_met_ratio: float = 0.5       # waste halved = met
    spend_raised_ratio: float = 1.05   # spend up at least 5% = the raise landed
    roas_hold_ratio: float = 0.9       # ROAS held at >= 90% of baseline after the raise
    rival_gain_pp: float = 0.5         # a watched rival gaining >= 0.5 pp of share = missed
    tier_met_pp: float = 1.0           # own share of a price tier up >= 1.0 pp = met
    tier_pending_pp: float = 0.25      # ... up >= 0.25 pp = still on track
    discount_hold_pp: float = 1.5      # discount held within 1.5 pp of baseline
    time_dependent_kinds: tuple[str, ...] = ("waste_bleed", "tier_share")  # kinds covered by the too-soon guard


_CFG = ScorecardConfig()


@dataclass(frozen=True)
class Baseline:
    """The armed yardstick: when it was armed, the checkpoints and the values seen then."""

    ts: datetime
    checkpoints: tuple[Mapping[str, Any], ...]
    vals: Mapping[str, Any]


@dataclass(frozen=True)
class GradeRow:
    """One graded checkpoint."""

    desc: str
    then: Any
    now: Any
    status: str
    note: str = ""
    kind: str = ""


class BaselineStore(Protocol):
    """Where baselines live.  Implementations may raise ``OSError`` on failure."""

    def get(self, account: str, scope: str) -> Baseline | None: ...

    def put(self, account: str, scope: str, baseline: Baseline) -> None: ...


class InMemoryBaselineStore:
    """Dictionary-backed ``BaselineStore`` for tests and single-process use."""

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], Baseline] = {}

    def get(self, account: str, scope: str) -> Baseline | None:
        return self._data.get((account, scope))

    def put(self, account: str, scope: str, baseline: Baseline) -> None:
        self._data[(account, scope)] = baseline


def arm(store: BaselineStore, account: str, scope: str,
        checkpoints: Sequence[Mapping[str, Any]], vals: Mapping[str, Any] | None,
        now: datetime, cfg: ScorecardConfig | None = None) -> bool:
    """Arm a new baseline, unless a fresh one must be protected.

    No checkpoints -> ``False``.  An existing baseline younger than
    ``min_age_days`` is kept -> ``False``.  Otherwise the baseline is
    written -> ``True``.  A store failure (``OSError``) -> ``False``.
    """
    cfg = cfg or _CFG
    if not checkpoints:
        return False
    try:
        prior = store.get(account, scope)
        if prior is not None and (now - prior.ts) < timedelta(days=cfg.min_age_days):
            return False
        store.put(account, scope, Baseline(ts=now, checkpoints=tuple(dict(c) for c in checkpoints),
                                           vals=dict(vals or {})))
        return True
    except OSError:
        return False


# A grader returns (then, now, status, note).
Grader = Callable[[Mapping[str, Any], Mapping[str, Any], ScorecardConfig], "tuple[Any, Any, str, str]"]


def _num(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _lookup(cur: Mapping[str, Any], cp: Mapping[str, Any], default_key: str,
            sub: str | None = None) -> float | None:
    v = cur.get(cp.get("key", default_key))
    if sub is not None:
        v = v.get(cp.get(sub)) if isinstance(v, Mapping) else None
    return _num(v)


def _grade_availability(cp, cur, cfg):
    target, now = _num(cp.get("target")), _lookup(cur, cp, "availability")
    if now is None or target is None:
        return target, now, PENDING, "no current value"
    return target, now, (MET if now >= target else MISSED), ""


def _grade_waste(cp, cur, cfg):
    base, now = _num(cp.get("baseline")), _lookup(cur, cp, "waste")
    if now is None or base is None:
        return base, now, PENDING, "no current value"
    if now <= cfg.bleed_met_ratio * base:
        return base, now, MET, "waste at least halved"
    if now < base:
        return base, now, PENDING, "falling, not yet halved"
    return base, now, MISSED, "waste did not fall"


def _grade_budget_roas(cp, cur, cfg):
    bs, br = _num(cp.get("baseline_spend")), _num(cp.get("baseline_roas"))
    spend = _num(cur.get(cp.get("spend_key", "spend")))
    roas = _num(cur.get(cp.get("roas_key", "roas")))
    then, now = {"spend": bs, "roas": br}, {"spend": spend, "roas": roas}
    if bs is None or br is None or spend is None or roas is None:
        return then, now, PENDING, "no current value"
    if spend < cfg.spend_raised_ratio * bs:
        return then, now, PENDING, "spend not yet raised"
    if roas >= cfg.roas_hold_ratio * br:
        return then, now, MET, "spend raised and return held"
    return then, now, MISSED, "spend raised but return sagged"


def _grade_rival(cp, cur, cfg):
    base, now = _num(cp.get("baseline_share")), _lookup(cur, cp, "share", "brand")
    if now is None or base is None:
        return base, now, PENDING, "no current value"
    return base, now, (MET if (now - base) < cfg.rival_gain_pp else MISSED), ""


def _grade_tier(cp, cur, cfg):
    base, now = _num(cp.get("baseline")), _lookup(cur, cp, "tier_share", "tier")
    if now is None or base is None:
        return base, now, PENDING, "no current value"
    d = now - base
    if d >= cfg.tier_met_pp:
        return base, now, MET, ""
    if d >= cfg.tier_pending_pp:
        return base, now, PENDING, "moving, not yet there"
    return base, now, MISSED, "share did not move"


def _grade_discount(cp, cur, cfg):
    base, now = _num(cp.get("baseline")), _lookup(cur, cp, "discount")
    if now is None or base is None:
        return base, now, PENDING, "no current value"
    return base, now, (MET if abs(now - base) <= cfg.discount_hold_pp else MISSED), ""


GRADERS: dict[str, Grader] = {
    "budget_roas": _grade_budget_roas,
    "waste_bleed": _grade_waste,
    "availability_floor": _grade_availability,
    "rival_share": _grade_rival,
    "tier_share": _grade_tier,
    "discount_hold": _grade_discount,
}


def register_grader(kind: str, grader: Grader) -> None:
    """Register (or replace) the grader for a checkpoint kind.

    ``grader(checkpoint, current_values, cfg)`` returns ``(then, now,
    status, note)`` with status one of MET / MISSED / PENDING.
    """
    GRADERS[kind] = grader


def grade_checkpoint(checkpoint: Mapping[str, Any], cur: Mapping[str, Any], age_days: float,
                     cfg: ScorecardConfig | None = None,
                     graders: Mapping[str, Grader] | None = None) -> GradeRow:
    """Grade one checkpoint against current values.

    Built-in rules (``cur`` keys are overridable per checkpoint via ``key``):

    =====================  =====================================================
    kind                   MET / PENDING / MISSED
    =====================  =====================================================
    ``budget_roas``        spend >= 1.05 x baseline AND roas >= 0.9 x baseline /
                           spend not yet raised / raised but ROAS sagged
    ``waste_bleed``        now <= 0.5 x baseline / now < baseline / otherwise
    ``availability_floor``  now >= target / value missing / below target
    ``rival_share``        share change < +0.5 pp / missing / otherwise
    ``tier_share``         change >= +1.0 pp / >= +0.25 pp / otherwise
    ``discount_hold``      within 1.5 pp of baseline / missing / otherwise
    =====================  =====================================================

    Fairness rule: a time-dependent kind cannot be MISSED against a baseline
    younger than ``too_soon_days``; it reads PENDING.  Unknown kinds and
    grader errors read PENDING.

    Returns a ``GradeRow``.
    """
    cfg = cfg or _CFG
    kind = str(checkpoint.get("kind", ""))
    desc = str(checkpoint.get("desc", kind))
    grader = (graders or GRADERS).get(kind)
    if grader is None:
        return GradeRow(desc, None, None, PENDING, "no grader for this kind", kind)
    try:
        then, now, status, note = grader(checkpoint, cur, cfg)
    except Exception as exc:  # a broken grader must not break the report
        return GradeRow(desc, None, None, PENDING, f"grader failed: {type(exc).__name__}", kind)
    if status == MISSED and kind in cfg.time_dependent_kinds and age_days < cfg.too_soon_days:
        status, note = PENDING, "too soon to judge"
    return GradeRow(desc, then, now, status, note, kind)


def grade(prior: Baseline | None, cur: Mapping[str, Any], now: datetime,
          cfg: ScorecardConfig | None = None,
          graders: Mapping[str, Grader] | None = None) -> list[GradeRow]:
    """Grade every checkpoint of the armed baseline against current values.

    ``age_days`` is the time since the baseline was armed.  Returns one
    ``GradeRow`` per checkpoint, in armed order (empty without a baseline).
    """
    if prior is None:
        return []
    age = (now - prior.ts).total_seconds() / 86400.0
    return [grade_checkpoint(c, cur, age, cfg, graders) for c in prior.checkpoints]


def budget_roas_checkpoint(baseline_spend: float, baseline_roas: float, desc: str = "",
                           spend_key: str = "spend", roas_key: str = "roas") -> dict[str, Any]:
    """Checkpoint for a recommended raise: did spend rise without the return sagging."""
    return {"kind": "budget_roas", "desc": desc or "budget raise landed without the return sagging",
            "baseline_spend": baseline_spend, "baseline_roas": baseline_roas,
            "spend_key": spend_key, "roas_key": roas_key}


def waste_bleed_checkpoint(baseline: float, desc: str = "", key: str = "waste") -> dict[str, Any]:
    """Checkpoint for a waste cut: did sub-breakeven spend at least halve."""
    return {"kind": "waste_bleed", "desc": desc or "sub-breakeven spend halved",
            "baseline": baseline, "key": key}
