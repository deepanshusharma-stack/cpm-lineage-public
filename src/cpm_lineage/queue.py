"""Unified ranked action queue for an account.

Merges the account-level findings (gated structural bleed, budget tighten /
scale / release moves, Pareto draggers) into one worklist ranked by
``amount x confidence weight``, so that realised waste outranks
hypothetical upside.

Rule for consumers: **never sum amounts across rows**.  The units differ
(cuttable waste, hypothetical upside, freeable cash, drag versus the
account ROAS); each row carries its ``unit``.

The queue sizes cuts as "step, then verify" inputs from the budget scan;
the one-row prescription (``cpm_lineage.prescribe``) uses the breakeven
slice instead.  Two surfaces, two policies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .config import Economics
from .liveness import liveness_class
from .schema import LivenessClass

__all__ = ["QueueConfig", "QueueRow", "ActionQueue", "build_action_queue",
           "GROUP_BLEED", "GROUP_SCALE", "GROUP_STRUCTURE"]

GROUP_BLEED = "STOP THE BLEED"
GROUP_SCALE = "SCALE WINNERS"
GROUP_STRUCTURE = "FIX STRUCTURE"


@dataclass(frozen=True)
class QueueConfig:
    """Ranking and filtering thresholds of the action queue."""

    weight_high: float = 1.0               # realised waste counts in full
    weight_med: float = 0.6                # structural estimates
    weight_low: float = 0.3                # hypothetical upside
    period_days: int = 30                  # daily amounts are scaled to this period
    scale_fallback_mult: float = 1.5       # raise target when the curve gave none: 1.5 x the cap
    test_raise_pct: float = 20.0           # unverified scale: raise as a small test ...
    test_recheck_days: int = 7             # ... and re-check after this many days
    over_invest_ms_sov: float = 0.5        # market share : share of voice under this = over-invested in ads
    over_invest_damper: float = 0.5        # ... every scale row's weight is halved
    floor_ref: float = 2000.0              # anti-dump floor per 30 days (reference money) ...
    floor_ref_days: int = 30               # ... scaled by period_days / this
    floor_share_of_top: float = 0.01       # ... or 1% of the largest row, whichever is larger
    max_rows: int = 15                     # list length; the tail is collapsed into a count and a sum

    def weight(self, conf: str) -> float:
        return {"high": self.weight_high, "med": self.weight_med, "low": self.weight_low}.get(conf, self.weight_low)


_QC = QueueConfig()
_ECON = Economics()


@dataclass
class QueueRow:
    """One line of the worklist."""

    group: str
    verb: str
    amount: float                          # money over the period, in ``unit`` terms
    unit: str                              # what the amount means
    conf: str                              # "high" | "med" | "low"
    confw: float
    entity_id: str | None = None
    detail: str = ""
    estimator: str | None = None           # which money estimator produced the amount
    automation_hint: str | None = None     # optional: the kind of automation rule that would hold the fix

    @property
    def score(self) -> float:
        """Ranking key: ``amount x confw``."""
        return self.amount * self.confw


@dataclass
class ActionQueue:
    """Ranked rows plus the collapsed tail."""

    rows: list[QueueRow] = field(default_factory=list)
    n_more: int = 0
    more_amount: float = 0.0               # sum of the collapsed tail (same caveat: mixed units)
    period_days: int = 30
    floor: float = 0.0
    reason: str | None = None              # set when the queue is empty


def _g(obj: Any, *keys: str, default: Any = None) -> Any:
    for key in keys:
        v = obj.get(key) if isinstance(obj, Mapping) else getattr(obj, key, None)
        if v is not None:
            return v
    return default


def build_action_queue(
    *,
    bleed: Any = None,
    moves: Any = None,
    draggers: Iterable[Any] = (),
    ms_sov: float | None = None,
    period_days: int | None = None,
    status_map: Mapping[str, Any] | None = None,
    cfg: QueueConfig | None = None,
    economics: Economics | None = None,
) -> ActionQueue:
    """Build the account worklist.

    Inputs (objects or mappings; each optional):

    * ``bleed``: the gated structural bleed, read for ``cuttable`` (and
      ``n_cut``);
    * ``moves``: the budget scan with ``tighten`` (``entity_id,
      daily_waste``), ``scale`` (``entity_id, daily_budget,
      extra_daily_sales, sized, raise_to``) and ``release`` (``entity_id,
      freeable``) lists;
    * ``draggers``: Pareto drag rows (``key`` or ``entity_id``, ``drag``,
      optional ``status`` directly or under ``extras``);
    * ``ms_sov``: account market-share : share-of-voice ratio.

    Rows, in build order (``per`` = period days):

    1. STOP THE BLEED: one row for the whole cuttable bleed (high);
    2. STOP THE BLEED: per tighten entity ``round(daily_waste x per)``
       (high), skipped when <= 0;
    3. SCALE WINNERS: per scale entity ``round(extra_daily_sales x per)``,
       high when curve-sized else low, target ``raise_to`` or ``1.5 x
       cap``; unverified rows advise a small test raise and a re-check;
    4. FIX STRUCTURE: per release entity ``round(freeable x per)`` (med),
       skipped when <= 0;
    5. FIX STRUCTURE: draggers not already covered above and not in a
       hard-dead status, ``round(drag)`` (med), skipped when <= 0.

    Then: over-investment damper (``ms_sov < 0.5`` halves every scale
    row's weight, sets it to low and appends a warning); anti-dump floor
    ``max(money(floor_ref) x per / 30, 1% of the largest row)`` drops rows
    under it; sort by ``amount x confw`` descending; keep ``max_rows``
    (15) and report the tail as ``n_more`` / ``more_amount``.

    Returns an :class:`ActionQueue`.
    """
    cfg, econ = cfg or _QC, economics or _ECON
    per = int(period_days or cfg.period_days)
    rows: list[QueueRow] = []
    covered: set[Any] = set()

    def add(group: str, verb: str, amount: float, unit: str, conf: str, entity_id: Any = None,
            detail: str = "", estimator: str | None = None, hint: str | None = None) -> None:
        rows.append(QueueRow(group=group, verb=verb, amount=float(amount), unit=unit, conf=conf,
                             confw=cfg.weight(conf), entity_id=entity_id, detail=detail, estimator=estimator,
                             automation_hint=hint))
        if entity_id is not None:
            covered.add(entity_id)

    cuttable = _g(bleed, "cuttable") if bleed is not None else None
    if cuttable is not None and cuttable > 0:
        n_cut = _g(bleed, "n_cut")
        add(GROUP_BLEED, "Cut or negative the gated bleeding keywords", round(cuttable), "cuttable waste", "high",
            detail=(f"{n_cut} keywords below breakeven after halo and acquisition spares" if n_cut is not None
                    else "keywords below breakeven after halo and acquisition spares"),
            estimator="realised_waste", hint="keyword pause / negative rule")

    for t in (_g(moves, "tighten", default=()) if moves is not None else ()):
        amt = round((_g(t, "daily_waste", default=0.0) or 0.0) * per)
        if amt <= 0:
            continue
        add(GROUP_BLEED, "Tighten the daily budget", amt, "waste", "high", _g(t, "entity_id"),
            f"binding cap funds spend below breakeven (ROAS {_fmt(_g(t, 'roas'))})", "realised_waste",
            "budget cap rule")

    for s in (_g(moves, "scale", default=()) if moves is not None else ()):
        amt = round((_g(s, "extra_daily_sales", default=0.0) or 0.0) * per)
        sized = _g(s, "sized")
        conf = "high" if sized == "curve" else "low"
        cap = _g(s, "daily_budget", default=0.0) or 0.0
        target = _g(s, "raise_to") or round(cfg.scale_fallback_mult * cap)
        detail = f"raise the daily budget to about {target:,.0f}"
        if conf != "high":
            detail += (f"; marginal return unverified: raise as a small test (+{cfg.test_raise_pct:.0f}%) "
                       f"and re-check in {cfg.test_recheck_days} days")
        add(GROUP_SCALE, "Raise the daily budget", amt, "hypothetical upside (sales)", conf, _g(s, "entity_id"),
            detail, _g(s, "estimator") or ("saturation_curve" if sized == "curve" else "linear_average"),
            "budget top-up rule")

    for r in (_g(moves, "release", default=()) if moves is not None else ()):
        amt = round((_g(r, "freeable", default=0.0) or 0.0) * per)
        if amt <= 0:
            continue
        add(GROUP_STRUCTURE, "Release parked budget", amt, "freeable budget", "med", _g(r, "entity_id"),
            "low utilisation and below breakeven: the cap can fund a winner instead", "freeable_budget")

    for d in draggers:
        eid = _g(d, "entity_id", "key")
        if eid in covered:
            continue
        status = _g(d, "status") or _g(_g(d, "extras", default={}) or {}, "status")
        if status is not None and liveness_class(status, status_map) is LivenessClass.HARD_DEAD:
            continue
        amt = round(_g(d, "drag", default=0.0) or 0.0)
        if amt <= 0:
            continue
        add(GROUP_STRUCTURE, "Restructure a dragging campaign", amt, "drag vs account ROAS", "med", eid,
            f"returns below the account average (ROAS {_fmt(_g(d, 'roas'))})", "pareto_drag")

    if ms_sov is not None and ms_sov < cfg.over_invest_ms_sov:
        for row in rows:
            if row.group == GROUP_SCALE:
                row.confw *= cfg.over_invest_damper
                row.conf = "low"
                row.detail += (f"; warning: market share is under {cfg.over_invest_ms_sov:g}x share of voice "
                               "(over-invested in ads): scaling adds to an already heavy position")

    if not rows:
        return ActionQueue(period_days=per, reason="no findings to queue")
    floor = max(econ.money(cfg.floor_ref) * per / cfg.floor_ref_days,
                cfg.floor_share_of_top * max(r.amount for r in rows))
    rows = [r for r in rows if r.amount >= floor]
    rows.sort(key=lambda r: -r.score)          # stable: build order breaks ties
    tail = rows[cfg.max_rows:]
    return ActionQueue(rows=rows[: cfg.max_rows], n_more=len(tail), more_amount=sum(r.amount for r in tail),
                       period_days=per, floor=floor,
                       reason=None if rows else "every finding is below the materiality floor")


def _fmt(x: Any) -> str:
    return "n/a" if x is None else f"{float(x):.2f}"
