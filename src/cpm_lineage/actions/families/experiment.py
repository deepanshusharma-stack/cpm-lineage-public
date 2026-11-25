"""Experiment family: propose a day-part test where the answer is uncertain."""

from __future__ import annotations

from ...schema import ActionCard, CapabilityClass
from ..primitives import (ActionContext, capability_class, fmt_roas, live_campaigns, new_card,
                          segments_in_scope, top_keywords)

__all__ = ["experiment"]

FAMILY = "experiment"


def experiment(ctx: ActionContext) -> list[ActionCard]:
    """Decide which campaigns deserve a day-part test instead of a budget verdict.

    Per live campaign with budget telemetry, all must hold: the ad type is a
    keyword-auction product; exhaustion is believable (cap known and mean
    spend >= half the cap); mean exhaust minute < ``experiment_exhaust_minutes``;
    telemetry direct ROAS within ``[0.8 x be, 2.5 x be]`` (neither clear
    winner nor clear loser); at least ``experiment_min_days`` days of rows.

    Card: "Launch Day-Part Experiment", 7 days, success = worst-slot ROAS
    +15% after removing the lowest slot; it pauses the campaign in the
    removed slot (counts as a down-move in conflict resolution).  Stake:
    ``telemetry spend x 0.1``.

    Note: the ad-type test uses the capability class, so an unknown type
    takes the profile's default class here as everywhere else.
    """
    cfg, be = ctx.cfg, ctx.be
    cards: list[ActionCard] = []
    for seg in segments_in_scope(ctx):
        for c in live_campaigns(ctx, seg):
            b = ctx.telemetry.get(c.entity_id)
            if b is None or capability_class(ctx, c.ad_type) != CapabilityClass.KEYWORD_AUCTION.value:
                continue
            if not b.avg_budget or (b.avg_spend or 0.0) < cfg.experiment_credible_spend_fraction * b.avg_budget:
                continue
            if b.exhaust_minute is None or b.exhaust_minute >= cfg.experiment_exhaust_minutes:
                continue
            if b.roas is None or not (cfg.experiment_roas_min_mult * be <= b.roas
                                      <= cfg.experiment_roas_max_mult * be):
                continue
            if b.n_days < cfg.experiment_min_days:
                continue
            cards.append(new_card(
                ctx, FAMILY, "experiment", "Launch Day-Part Experiment", segment=seg,
                entity_id=c.entity_id, direction="none",
                keywords=top_keywords(ctx, c.entity_id, cfg.experiment_keywords),
                trigger=(f"budget runs out around {b.exhaust_minute / 60:.1f}h local at "
                         f"{fmt_roas(b.roas)} direct ROAS: more budget is not clearly right"),
                targets=(f"hypothesis: concentrating spend in converting local-time windows beats "
                         f"a flat budget; {cfg.experiment_days}-day test; success = worst-slot ROAS "
                         f"+{cfg.experiment_success_pct:g}% after removing the lowest slot; pauses "
                         f"the campaign in that slot"),
                evidence={"roas": b.roas, "spend": b.spend, "exhaust_minute": b.exhaust_minute,
                          "util": b.utilisation, "days": cfg.experiment_days, "pauses": True},
                stake=b.spend * cfg.experiment_stake_share,
                facts=f"droas={b.roas:.2f} exhaust_min={b.exhaust_minute:.0f} nd={b.n_days}"))
    return cards
