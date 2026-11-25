"""Run-level guardrail and the brand-equity gate.

The operator may state one machine-enforced check per run: a ROAS floor
(no scale-up below it) or a total-spend ceiling (the plan's net budget delta
must fit under it).  Any other check is recorded and disclosed as NOT
machine-enforced; the plan never silently claims enforcement.

The equity gate stops cuts on an entity that carries the brand's own-brand
demand while the brand is already losing share in a live segment.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ..schema import ActionCard
from .pipeline_config import PlanContext, R_EQUITY, R_GUARDRAIL, change_bounds, is_live, veto_card

__all__ = [
    "GuardrailResult", "roas_fence", "spend_base", "apply_guardrail", "sov_headroom",
    "annotate_equity", "equity_gate",
]

_ROAS_METRICS = ("roas", "direct_roas")


@dataclass(frozen=True)
class GuardrailResult:
    """Outcome of the run guardrail: disclosure note, enforcement kind, vetoes."""

    note: str = ""
    enforced: str = "none"   # "none" | "recorded" | "roas_floor" | "spend_ceiling" | "not_enforced"
    vetoed: int = 0


def _field(check: Any, name: str, default: Any = None) -> Any:
    if check is None:
        return default
    if isinstance(check, Mapping):
        return check.get(name, default)
    return getattr(check, name, default)


def roas_fence(check: Any) -> float | None:
    """The ROAS multiple a check demands: ``value / 100`` in pct mode, else ``value``.

    "ROAS >= 200%" means 2.0x.  ``None`` when the check is not a ROAS check
    or has no value.
    """
    if _field(check, "metric") not in _ROAS_METRICS or _field(check, "value") is None:
        return None
    value = float(_field(check, "value"))
    return value / 100.0 if _field(check, "mode", "abs") == "pct" else value


def spend_base(ctx: PlanContext) -> float:
    """Current total daily budget: sum of telemetry ``avg_budget`` (scoped when
    ``ctx.scope_entity_ids`` is set), or ``spend_base_floor`` when none."""
    total = 0.0
    for entity_id, tel in ctx.telemetry.items():
        if ctx.scope_entity_ids is not None and entity_id not in ctx.scope_entity_ids:
            continue
        total += tel.avg_budget or 0.0
    return total or ctx.cfg.spend_base_floor


def apply_guardrail(cards: Sequence[ActionCard], check: Any, ctx: PlanContext) -> GuardrailResult:
    """Enforce the run's check metric on the cards.

    ``check`` has ``metric``, ``op``, ``value``, ``mode`` (a ``RunCheck`` or
    a mapping; a missing ``op`` means the natural one for the metric).

    * No check / no metric: nothing.  No value: recorded only.
    * ROAS floor (metric roas / direct_roas, op > or >=): veto every
      actionable card (pending or tension) with ``direction == "up"``,
      ``family != "defense"`` and a known ``evidence.roas < fence``.  Under
      the verdict profile, bid cards and cards carrying
      ``evidence.threshold`` are exempt (already judged on a bar that
      includes the check).
    * Spend ceiling (metric ad_spend, op < or <=): ``cap = value`` or
      ``base * value / 100`` (pct).  Raises with stated ``frm``/``to`` are
      sorted weakest first by ``(ev_density or confidence / 100,
      confidence)``; while ``base + net delta > cap`` the next pending raise
      is vetoed.  The net delta counts concurrent pending cuts.
    * Anything else: recorded, disclosed as not machine-enforced.

    Must run after confidence and expected value.  Returns a
    ``GuardrailResult``; vetoes are applied in place with reason code
    ``guardrail``.
    """
    metric = _field(check, "metric")
    if check is None or not metric:
        return GuardrailResult()
    value = _field(check, "value")
    label = f"check {metric} {_field(check, 'op') or ''} {value if value is not None else ''}".strip()
    if value is None:
        return GuardrailResult(f"{label} (no value - recorded only)", "recorded")
    op = _field(check, "op")
    pct = _field(check, "mode", "abs") == "pct"
    suffix = "%" if pct else ""

    if metric in _ROAS_METRICS and (op or ">=") in (">", ">="):
        fence = roas_fence(check)
        n = 0
        for c in cards:
            if not is_live(c) or c.direction != "up" or c.family == "defense":
                continue
            if ctx.verdict and (c.type == "bid" or c.evidence.get("threshold") is not None):
                continue
            r = c.evidence.get("roas")
            if r is not None and fence is not None and float(r) < fence:
                veto_card(c, R_GUARDRAIL, f"guardrail: ROAS {float(r):.2f} is below the run floor {fence:.2f}")
                n += 1
        return GuardrailResult(f"{label}{suffix} (enforced on scale-ups)", "roas_floor", n)

    if metric == "ad_spend" and (op or "<=") in ("<", "<="):
        base = spend_base(ctx)
        cap = base * float(value) / 100.0 if pct else float(value)
        ups = [c for c in cards if c.status == "pending" and c.direction == "up" and change_bounds(c)]
        ups.sort(key=lambda c: (c.ev_density if c.ev_density is not None else (c.confidence or 0) / 100.0,
                                c.confidence or 0))

        def delta() -> float:
            total = 0.0
            for c in cards:
                b = change_bounds(c)
                if c.status == "pending" and c.direction in ("up", "down") and b:
                    total += b[1] - b[0]
            return total

        n = 0
        for c in ups:
            if base + delta() <= cap:
                break
            veto_card(c, R_GUARDRAIL, f"guardrail: the plan's net budget would exceed the spend ceiling "
                                      f"{cap:,.0f}; this raise has the weakest return per unit")
            n += 1
        return GuardrailResult(f"{label}{suffix} (enforced on the net budget delta)", "spend_ceiling", n)

    return GuardrailResult(f"{label}{suffix} (recorded; NOT machine-enforced)", "not_enforced")


def sov_headroom(card: ActionCard, ctx: PlanContext) -> dict[str, Any] | None:
    """First-order share-of-voice effect of a budget move (annotation only).

    For a budget card with ``frm``, ``to`` and a segment share-of-voice
    reading ``sov`` (percent): ``own_daily = segment own spend / lookback
    days`` (skip if <= 0); ``x = (to - frm) / own_daily``;
    ``dsov_pp = sov * (1 - sov / 100) * x`` - an impression-share proxy at
    constant CPM, not a forecast.  Bucket: own-brand share >= 0.4 and up ->
    "brand bucket protected"; >= 0.4 and down -> "brand bucket at risk";
    else "generic/conquest bucket".

    Returns ``{dsov_pp (2 dp), bucket, own_share (pct int)}`` or ``None``.
    """
    b = change_bounds(card)
    seg = ctx.segments.get(card.segment) if card.segment is not None else None
    if card.type != "budget" or b is None or seg is None or seg.sov is None:
        return None
    own_daily = (seg.own_spend or 0.0) / max(ctx.lookback_days, 1)
    if own_daily <= 0:
        return None
    x = (b[1] - b[0]) / own_daily
    sov = float(seg.sov)
    ent = ctx.entities.get(card.entity_id) if card.entity_id is not None else None
    own = (ent.own_brand_share if ent is not None else None) or 0.0
    if own >= ctx.cfg.equity_own_share_min:
        bucket = "brand bucket protected" if card.direction == "up" else "brand bucket at risk"
    else:
        bucket = "generic/conquest bucket"
    return {"dsov_pp": round(sov * (1 - sov / 100.0) * x, 2), "bucket": bucket, "own_share": int(round(100 * own))}


def annotate_equity(cards: Sequence[ActionCard], ctx: PlanContext) -> None:
    """Stamp ``card.sov_headroom`` on every card (see ``sov_headroom``)."""
    for c in cards:
        c.sov_headroom = sov_headroom(c, ctx)


def equity_gate(cards: Sequence[ActionCard], ctx: PlanContext) -> int:
    """Veto cuts that trade long-term brand equity for short-term efficiency.

    Applies to actionable cards with ``direction == "down"`` and type budget
    or keyword, on an entity whose own-brand keyword spend share is >= 0.4,
    in a segment with total and own sales for both windows.  The gate stands
    down when the segment is in freefall
    (``100 * (total / total_prior - 1) < -10``).  Otherwise
    ``dpp = 100 * own / total - 100 * own_prior / total_prior``; veto when
    ``dpp < -0.2``.  Returns the number vetoed.
    """
    cfg = ctx.cfg
    n = 0
    for c in cards:
        if not is_live(c) or c.direction != "down" or c.type not in ("budget", "keyword"):
            continue
        ent = ctx.entities.get(c.entity_id) if c.entity_id is not None else None
        if ent is None or (ent.own_brand_share or 0.0) < cfg.equity_own_share_min:
            continue
        seg = ctx.segments.get(c.segment) if c.segment is not None else None
        if seg is None or not seg.total_sales or not seg.total_sales_prior:
            continue
        if seg.own_sales is None or seg.own_sales_prior is None:
            continue
        if 100.0 * (seg.total_sales / seg.total_sales_prior - 1.0) < cfg.equity_freefall_pct:
            continue
        dpp = 100.0 * seg.own_sales / seg.total_sales - 100.0 * seg.own_sales_prior / seg.total_sales_prior
        if dpp < cfg.equity_share_loss_pp:
            veto_card(c, R_EQUITY, f"equity gate: this entity carries own-brand demand and the brand lost "
                                   f"{abs(dpp):.1f}pp of share - the cut buys short-term efficiency with "
                                   "long-term equity")
            n += 1
    return n
