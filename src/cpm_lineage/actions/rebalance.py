"""Budget-neutral rebalancer: fund the plan's raises from its weakest earners.

When the operator wants total budget unchanged, every unit of raise in the
plan that is not already paid for by the plan's own cuts must be found
somewhere.  This module mints extra budget-cut cards ("donors") on the
entities whose ROAS sits furthest below their own bar, never taking more
than a bounded share of any one budget per step.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Sequence

from ..schema import ActionCard, Change, make_card
from .pipeline_config import PlanContext, change_bounds, is_live

__all__ = ["BoardEntry", "RebalanceResult", "funding_need", "rebalance"]


@dataclass(frozen=True)
class BoardEntry:
    """One entity on the run's scoreboard: its ROAS, its bar and its daily cap."""

    entity_id: str
    roas: float | None = None
    threshold: float | None = None   # the ROAS bar this entity is judged on
    cap: float | None = None         # current daily budget
    segment: str | None = None
    bucket: str | None = None
    spend: float | None = None
    name: str | None = None


@dataclass
class RebalanceResult:
    """Donor cards minted and the money they cover."""

    donors: list[ActionCard] = field(default_factory=list)
    raise_total: float = 0.0     # sum of live raises, per day
    cut_total: float = 0.0       # sum of live cuts before donors, per day
    funded: float = 0.0          # covered by donors
    short: float = 0.0           # raise money no donor could fund
    note: str = ""

    def telemetry(self) -> dict[str, float]:
        return {"raise": round(self.raise_total, 2), "cut": round(self.cut_total, 2),
                "donors": len(self.donors), "short": round(self.short, 2)}


def funding_need(cards: Sequence[ActionCard]) -> tuple[float, float, float]:
    """Net daily budget the plan adds.

    Over actionable budget cards with ``frm`` and ``to``:
    ``up = sum(to - frm)`` on raises, ``down = sum(frm - to)`` on cuts,
    ``need = up - down``.  Returns ``(up, down, need)``.
    """
    up = down = 0.0
    for c in cards:
        b = change_bounds(c)
        if not is_live(c) or c.type != "budget" or b is None:
            continue
        if c.direction == "up":
            up += b[1] - b[0]
        elif c.direction == "down":
            down += b[0] - b[1]
    return up, down, up - down


def rebalance(
    cards: Sequence[ActionCard],
    board: Sequence[BoardEntry],
    ctx: PlanContext,
    *,
    excluded: frozenset[str] = frozenset(),
    demote: Callable[[ActionCard], bool] | None = None,
) -> RebalanceResult:
    """Mint donor cuts so the plan's raises net to zero extra budget.

    1. ``need`` from ``funding_need``; if ``up <= 0`` or ``need <= 0`` mint
       nothing (existing cuts already fund the raises).
    2. Never a donor: an entity that already has a budget card, an entity
       with a live bid-down (mid-probe), and anything in ``excluded``
       (e.g. a recently applied experiment or defense move).
    3. Donors = board entries with ``cap > 0``, ``threshold > 0`` and a known
       ROAS, sorted ascending by ``roas / threshold`` (worst first).
    4. Per donor while ``need > 0``:
       ``pct = round(100 * min(need, 0.30 * cap) / cap)``; skip if
       ``pct < 5``; ``give = cap * pct / 100``.  The donor is an
       ``efficiency-cut / budget / down`` card with
       ``change = {pct: -pct, frm: cap, to: cap * (1 - pct / 100)}``,
       ``stake = give``, flat confidence 66, wave 3.  If ``demote(card)``
       returns true (a downstream verdict stage refused it) the donor funds
       nothing and the walk continues.

    Returns a ``RebalanceResult``; donor cards are not appended to ``cards``.
    """
    cfg = ctx.cfg
    up, down, need = funding_need(cards)
    res = RebalanceResult(raise_total=up, cut_total=down)
    if up <= 0 or need <= 0:
        return res
    skip = set(excluded)
    for c in cards:
        if c.entity_id is None:
            continue
        if c.type == "budget" or (c.type == "bid" and c.direction == "down" and is_live(c)):
            skip.add(c.entity_id)
    pool = [b for b in board if b.entity_id not in skip and (b.cap or 0) > 0
            and (b.threshold or 0) > 0 and b.roas is not None]
    pool.sort(key=lambda b: b.roas / b.threshold)
    for b in pool:
        if need <= 0:
            break
        pct = round(100.0 * min(need, cfg.rebalance_max_bleed * b.cap) / b.cap)
        if pct < cfg.rebalance_min_pct:
            continue
        give = b.cap * pct / 100.0
        card = make_card(
            "efficiency-cut", "budget", "Decrease Budget (rebalance)",
            segment=b.segment, entity_id=b.entity_id, direction="down",
            change=Change(pct=-pct, frm=b.cap, to=b.cap * (1 - pct / 100.0)),
            trigger=(f"budget-neutral swap: ROAS {b.roas:.2f} against a bar of {b.threshold:.2f} is the "
                     f"weakest earner available; frees {give:,.0f} per day for the plan's raises"),
            targets=f"lower the daily budget by {pct}%",
            evidence={"roas": b.roas, "threshold": b.threshold, "bucket": b.bucket, "funds": give,
                      "spend": b.spend},
            stake=give, rule_profile=ctx.rule_profile,
        )
        card.confidence = cfg.rebalance_confidence
        card.conf_parts = [("budget-neutral swap", float(cfg.rebalance_confidence))]
        card.wave = 3
        card.entity_name = b.name
        card.estimator = "stake"
        card.extras["rebalance_donor"] = True
        if demote is not None and demote(card):
            continue
        res.donors.append(card)
        res.funded += give
        need -= give
    res.short = max(0.0, need)
    res.note = (f"budget-neutral: raises {up:,.0f}/day, funded {down + res.funded:,.0f}/day"
                + (f", SHORT by {res.short:,.0f}/day" if res.short > 0.005 else ""))
    return res
