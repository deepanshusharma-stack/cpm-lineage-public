"""Defense family: detect new entrants; fund the moat campaign when a rival takes share."""

from __future__ import annotations

from ...schema import ActionCard, Change
from ..primitives import (ActionContext, entity_keywords, fmt_money, is_branded, live_campaigns,
                          money, new_card, segments_in_scope)

__all__ = ["defense", "moat_campaign"]

FAMILY = "defense"


def moat_campaign(ctx: ActionContext, segment: str | None) -> tuple[str, float] | None:
    """The segment's moat campaign: highest own+sibling share of keyword spend.

    Considers live campaigns with keyword spend >= ``moat_min_keyword_spend``;
    the best must reach ``moat_min_own_share``.  Returns ``(entity_id, share)`` or ``None``.
    """
    best: tuple[str, float] | None = None
    for c in live_campaigns(ctx, segment):
        rows = entity_keywords(ctx, c.entity_id)
        total = sum(k.spend for k in rows)
        if total < money(ctx, ctx.cfg.moat_min_keyword_spend) or total <= 0:
            continue
        share = sum(k.spend for k in rows if is_branded(k.role)) / total
        if best is None or share > best[1]:
            best = (c.entity_id, share)
    if best is None or best[1] < ctx.cfg.moat_min_own_share:
        return None
    return best


def defense(ctx: ActionContext) -> list[ActionCard]:
    """Decide whether a rival needs watching or answering in each segment.

    Per segment with both-window totals and a player list (percent shares):

    * entrant = first rival (not own, not sibling) with prior share < 0.5 and
      current share >= 2.0 -> watch card "New Entrant Detected", no campaign.
      Stake: ``total_current x share_current / 100``.
    * defend = own share change ``dpp <= -0.3`` and a rival gaining
      ``>= 0.3`` points -> "Increase Budget (defend)" +15% on the moat
      campaign (see ``moat_campaign``), unless a defend raise was applied on
      it within the cooldown.  Stake: ``total_current x |dpp| / 100``.
    """
    cfg = ctx.cfg
    cards: list[ActionCard] = []
    for seg in segments_in_scope(ctx):
        m = ctx.segments.get(seg)  # type: ignore[arg-type]
        if m is None or not m.has_both_windows:
            continue
        tot, dpp = m.total_sales_current or 0.0, m.share_delta_pp or 0.0
        rivals = [p for p in m.players if not p.is_own and not p.is_sibling]
        gainer = next((p for p in rivals if p.delta_pp >= cfg.gainer_min_pp), None)
        entrant = next((p for p in rivals if p.share_prior < cfg.entrant_prior_share_max
                        and p.share_current >= cfg.entrant_current_share_min), None)
        if entrant is not None:
            cards.append(new_card(
                ctx, FAMILY, "watch", "New Entrant Detected", segment=seg, direction="none",
                trigger=(f"{entrant.brand} went from {entrant.share_prior:.1f}% to "
                         f"{entrant.share_current:.1f}% of segment sales"),
                targets="watch its keywords and price; no budget move yet",
                evidence={"brand": entrant.brand, "share_p": entrant.share_prior,
                          "share_c": entrant.share_current},
                stake=tot * entrant.share_current / 100.0,
                facts=f"entrant_share={entrant.share_current:.1f} total={fmt_money(tot)}"))
        if dpp > -cfg.defend_share_drop_pp or gainer is None:
            continue
        moat = moat_campaign(ctx, seg)
        if moat is None or ("defend", moat[0]) in ctx.recent_applied:
            continue
        eid, share = moat
        b = ctx.telemetry.get(eid)
        frm = (b.budget_latest or b.avg_budget) if b else None
        pct = cfg.defend_raise_pct
        cards.append(new_card(
            ctx, FAMILY, "budget", "Increase Budget (defend)", segment=seg, entity_id=eid,
            direction="up", change=Change(pct=pct, frm=frm, to=frm * (1 + pct / 100) if frm else None),
            trigger=(f"own share {dpp:+.2f} pp while {gainer.brand} gained "
                     f"{gainer.delta_pp:+.2f} pp"),
            targets=f"raise the moat campaign {pct:g}%; one defend raise per {cfg.defend_cooldown_days} days",
            evidence={"dpp": dpp, "gainer": gainer.brand, "gainer_dpp": gainer.delta_pp,
                      "moat_share": share},
            stake=tot * abs(dpp) / 100.0,
            facts=f"dpp={dpp:.2f} gainer_dpp={gainer.delta_pp:.2f} moat_share={share:.2f}"))
    return cards
