"""Confidence, expected value and marginal sizing of action cards.

Every number here is additive or a one-line product so that a reviewer can
re-derive it by hand: no sigmoid, no learned model.  Confidence is a bounded
sum of labelled parts; expected value is an estimated daily effect times a
success probability (a realised family win rate when enough outcomes exist,
otherwise a flat prior).
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from ..budget.config import BudgetConfig
from ..budget.estimators import finite_difference_marginal
from ..schema import ActionCard
from .pipeline_config import PipelineConfig, PlanContext, R_MARGINAL, change_bounds, is_live, veto_card

__all__ = [
    "confidence", "expected_value", "score_cards", "marginal_roas", "marginal_roas_by_entity",
    "apply_marginal",
]


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _win_rate(ctx: PlanContext, family: str) -> float | None:
    wr = ctx.winrates.get(family)
    if wr is None:
        return None
    rate, n = wr
    return float(rate) if n >= ctx.cfg.winrate_min_n else None


def confidence(card: ActionCard, ctx: PlanContext) -> int:
    """Score how much the evidence behind a card can be trusted (25..90).

    ``score = clamp(40 + sum(parts), 25, 90)`` with these parts:

    * evidence window: +15 if the lookback is >= 30 days, else +8;
    * spend depth: ``+min(10, int(evidence.spend / money(20000)))``;
    * exactly one of (first match wins): budget card with known ROAS,
      ``d = |roas - breakeven|`` -> +15 (d >= 0.5) / +8 (d >= 0.25) / +3;
      keyword down +12; bid +8; experiment +5; geo +10; collision family
      +12; watch +4;
    * availability (only while the supply gate is on): below caution -8,
      below floor -5, unmeasured on an up-card with a segment -8;
    * young entity (live days known and < 14): -10;
    * realised family win rate when n >= 5:
      ``clamp(round((rate - 0.5) * 20), -10, 10)``;
    * precedent on this (family, entity) when n > 0:
      ``clamp(round((won / n - 0.5) * 16), -8, 8)`` (also sets
      ``card.precedent``).

    Sets ``card.confidence`` and ``card.conf_parts`` (the non-zero
    ``(label, delta)`` pairs) and returns the score.
    """
    cfg = ctx.cfg
    parts: list[tuple[str, float]] = []
    if ctx.lookback_days >= cfg.conf_long_window_days:
        parts.append((f"{ctx.lookback_days}-day evidence window", cfg.conf_long_window))
    else:
        parts.append((f"short evidence window ({ctx.lookback_days} days)", cfg.conf_short_window))

    spend = float(card.evidence.get("spend") or 0.0)
    unit = ctx.money(cfg.conf_spend_per_point)
    depth = min(cfg.conf_spend_max, int(spend / unit)) if unit > 0 else 0
    parts.append(("spend depth", float(depth)))

    roas = card.evidence.get("roas")
    if card.type == "budget" and roas is not None:
        d = abs(float(roas) - ctx.be)
        if d >= cfg.conf_clarity_far:
            parts.append(("effect clarity: far from breakeven", cfg.conf_clarity_far_pts))
        elif d >= cfg.conf_clarity_mid:
            parts.append(("effect clarity: clear of breakeven", cfg.conf_clarity_mid_pts))
        else:
            parts.append(("effect clarity: near breakeven", cfg.conf_clarity_near_pts))
    elif card.type == "keyword" and card.direction == "down":
        parts.append(("pure waste", cfg.conf_keyword_down))
    elif card.type == "bid":
        parts.append(("rank-economics signal", cfg.conf_bid))
    elif card.type == "experiment":
        parts.append(("a test, not a bet", cfg.conf_experiment))
    elif card.type == "geo":
        parts.append(("arithmetic, not inference", cfg.conf_geo))
    elif card.family == "collision":
        parts.append(("structural", cfg.conf_collision))
    elif card.type == "watch":
        parts.append(("informational", cfg.conf_watch))

    if ctx.supply_gate_on:
        avail = ctx.availability(card.segment)
        if avail is not None:
            if avail < ctx.availability_policy.caution:
                parts.append(("availability broken", cfg.conf_avail_broken))
            elif avail < ctx.availability_policy.floor:
                parts.append(("availability shaky", cfg.conf_avail_shaky))
        elif card.direction == "up" and card.segment is not None:
            parts.append(("availability unmeasured", cfg.conf_avail_unmeasured))

    live = ctx.live_days(card.entity_id)
    if live is not None and live < cfg.conf_young_days:
        parts.append((f"young entity ({live} days)", cfg.conf_young))

    rate = _win_rate(ctx, card.family)
    if rate is not None:
        parts.append(("realised family win rate", _clamp(
            round((rate - 0.5) * cfg.conf_winrate_scale), -cfg.conf_winrate_max, cfg.conf_winrate_max)))

    prec = ctx.precedent.get((card.family, card.entity_id))
    if prec and (prec.get("n") or 0) > 0:
        n, won = prec["n"], prec.get("won") or 0
        parts.append(("precedent on this entity", _clamp(
            round((won / n - 0.5) * cfg.conf_precedent_scale), -cfg.conf_precedent_max, cfg.conf_precedent_max)))
        card.precedent = dict(prec)

    score = int(_clamp(cfg.conf_base + sum(v for _, v in parts), cfg.conf_min, cfg.conf_max))
    card.confidence = score
    card.conf_parts = [(label, v) for label, v in parts if v != 0]
    return score


def expected_value(card: ActionCard, ctx: PlanContext) -> float:
    """Estimate the expected daily value of applying a card.

    ``days = max(lookback, 1)``; ``ev_roas = evidence.mroas`` if present,
    else ``evidence.roas``.

    1. Budget card with ``frm``, ``to`` and a ROAS: ``delta = to - frm``;
       up ``est = max(delta, 0) * max(ev_roas, 0)`` (revenue the added budget
       buys per day); down ``est = |delta| * max(1 - ev_roas, 0)`` (bleed
       avoided net of lost sales).  The literal 1 is the default;
       ``ev_use_effective_breakeven`` swaps in the account's
       effective breakeven.
    2. Else keyword card with ``evidence.leak``: ``est = leak / days``.
    3. Else ``est = stake / days`` and ``ev_kind = "opportunity"`` (a
       market-opportunity number that must never outrank real money).
    4. ``p`` = realised family win rate when n >= 5, else 0.5.

    Sets ``est_day = round(est)``, ``p_success = round(p, 2)``,
    ``ev_day = round(est * p)``, ``ev_kind``, ``estimator`` (which money
    estimator produced the number) and, for a budget card with
    ``to != frm``, ``ev_density = round(est * p / |to - frm|, 2)``.
    Returns ``est * p`` unrounded.
    """
    cfg = ctx.cfg
    days = max(ctx.lookback_days, 1)
    ev = card.evidence
    has_m = ev.get("mroas") is not None
    ev_roas = ev.get("mroas") if has_m else ev.get("roas")
    bounds = change_bounds(card)
    kind = "money"
    if card.type == "budget" and bounds is not None and ev_roas is not None:
        delta = bounds[1] - bounds[0]
        if card.direction == "down" or (card.direction != "up" and delta < 0):
            line = ctx.be if cfg.ev_use_effective_breakeven else 1.0
            est = abs(delta) * max(line - float(ev_roas), 0.0)
        else:
            est = max(delta, 0.0) * max(float(ev_roas), 0.0)
        estimator = "marginal_fd" if has_m else "average_roas"
    elif card.type == "keyword" and ev.get("leak"):
        est = float(ev["leak"]) / days
        estimator = "leak"
    else:
        est = float(card.stake or 0.0) / days
        kind = "opportunity"
        estimator = "stake"
    rate = _win_rate(ctx, card.family)
    p = rate if rate is not None else cfg.ev_prior
    card.est_day = round(est)
    card.p_success = round(p, 2)
    card.ev_day = round(est * p)
    card.ev_kind = kind
    card.estimator = estimator
    card.ev_density = None
    if card.type == "budget" and bounds is not None and bounds[1] != bounds[0]:
        card.ev_density = round(est * p / abs(bounds[1] - bounds[0]), 2)
    return est * p


def score_cards(cards: Iterable[ActionCard], ctx: PlanContext) -> None:
    """Stamp confidence then expected value on every card (any status)."""
    for c in cards:
        confidence(c, ctx)
        expected_value(c, ctx)


# --------------------------------------------------------------------------
# Marginal sizing
# --------------------------------------------------------------------------

def marginal_roas(
    day_points: Iterable[tuple[float, float]],
    cfg: PipelineConfig | None = None,
    min_spend_gap: float | None = None,
) -> float | None:
    """Finite-difference marginal ROAS from one entity's ``(spend, sales)`` days.

    Keep days with spend > 0 (at least ``marginal_min_days`` = 8); sort by
    spend; split at the midpoint; ``dsp = mean(high spend) - mean(low
    spend)``, ``drv = mean(high sales) - mean(low sales)``.  When
    ``dsp > min_spend_gap`` return ``max(0, drv / dsp)``, else ``None`` (no
    reading).  ``min_spend_gap`` defaults to the unscaled config value;
    pass ``economics.money(cfg.marginal_min_spend_gap)`` to scale it.
    Crude by design: it reads day-of-week variation as spend response.
    """
    cfg = cfg or PipelineConfig()
    gap = cfg.marginal_min_spend_gap if min_spend_gap is None else min_spend_gap
    pts = [(float(s), float(v)) for s, v in day_points if s and s > 0]
    # One implementation of the estimator: budget.estimators (estimator "finite_difference").
    return finite_difference_marginal(
        [p[0] for p in pts], [p[1] for p in pts],
        BudgetConfig(fd_min_days=cfg.marginal_min_days, fd_min_spend_gap=gap))


def marginal_roas_by_entity(rows: Iterable[Any], ctx: PlanContext | None = None) -> dict[str, float]:
    """Marginal ROAS per entity from dated rows.

    ``rows`` are any records with ``date``, ``entity_id``, ``spend`` and
    ``sales`` (``PerfRow`` or ``BudgetDayRow``); they are summed to entity x
    day first.  Entities without a reading are absent from the result.
    """
    cfg = ctx.cfg if ctx is not None else PipelineConfig()
    gap = ctx.money(cfg.marginal_min_spend_gap) if ctx is not None else cfg.marginal_min_spend_gap
    daily: dict[str, dict[Any, list[float]]] = {}
    for r in rows:
        cell = daily.setdefault(r.entity_id, {}).setdefault(r.date, [0.0, 0.0])
        cell[0] += r.spend or 0.0
        cell[1] += r.sales or 0.0
    out: dict[str, float] = {}
    for entity_id, days in daily.items():
        m = marginal_roas(((s, v) for s, v in days.values()), cfg, gap)
        if m is not None:
            out[entity_id] = m
    return out


def apply_marginal(cards: Sequence[ActionCard], readings: Mapping[str, float], ctx: PlanContext) -> int:
    """Re-size budget cards on what the NEXT unit of budget earns.

    Applies to actionable budget cards with an entity, ``family !=
    "defense"`` and a marginal reading ``m`` (deflated by the ad type's
    attribution deflator).  With ``be`` the effective breakeven:

    * records ``evidence.mroas = round(m, 2)``;
    * up, ``m < 0.8 * be``: veto;
    * up, ``m < 1.5 * be`` and ``pct`` set: ``pct = max(5, int(pct / 2))``,
      ``to = frm * (1 + pct / 100)``;
    * down, ``m < 0.5 * be`` and ``pct`` set: ``pct = min(-25, pct - 5)``,
      ``to`` recomputed.

    Expected value is recomputed for every touched card (a
    resized card never keeps a stale value).  Returns the
    number of cards vetoed.
    """
    cfg = ctx.cfg
    be = ctx.be
    vetoed = 0
    for c in cards:
        if not is_live(c) or c.type != "budget" or c.entity_id is None or c.family == "defense":
            continue
        m = readings.get(c.entity_id)
        if m is None:
            continue
        m = float(m) * ctx.profile.capability(ctx.ad_type(c.entity_id)).roas_deflator
        c.evidence["mroas"] = round(m, 2)
        ch = c.change
        if c.direction == "up":
            if m < cfg.marginal_veto_mult * be:
                veto_card(c, R_MARGINAL, f"marginal gate: the NEXT unit of budget earns ~{m:.2f}, "
                                         f"below {cfg.marginal_veto_mult:g}x breakeven")
                vetoed += 1
            elif m < cfg.marginal_halve_mult * be and ch is not None and ch.pct is not None:
                if ch.pct_requested is None:
                    ch.pct_requested = ch.pct
                ch.pct = max(int(cfg.marginal_min_pct), int(ch.pct / 2))
                if ch.frm is not None:
                    ch.to = ch.frm * (1 + ch.pct / 100.0)
                c.trigger += f" · step halved to +{ch.pct}%: marginal ROAS ~{m:.2f} is thinner than the average"
        elif c.direction == "down":
            if m < cfg.marginal_deepen_mult * be and ch is not None and ch.pct is not None:
                if ch.pct_requested is None:
                    ch.pct_requested = ch.pct
                ch.pct = min(-cfg.marginal_deepen_floor, ch.pct - cfg.marginal_deepen_step)
                if ch.frm is not None:
                    ch.to = ch.frm * (1 + ch.pct / 100.0)
                c.trigger += f" · cut deepened to {ch.pct:g}%: marginal ROAS ~{m:.2f}"
        expected_value(c, ctx)
    return vetoed
