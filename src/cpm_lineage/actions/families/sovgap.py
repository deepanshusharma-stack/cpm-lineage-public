"""Share-of-voice gap family: market share versus ad share of voice per segment."""

from __future__ import annotations

from ...schema import ActionCard, CapabilityClass, Change
from ..primitives import (ActionContext, budget_bound, capability_class, decision_roas, fmt_money,
                          fmt_roas, live_campaigns, money, new_card, segments_in_scope)

__all__ = ["sov_gap"]

FAMILY = "sov-gap"


def sov_gap(ctx: ActionContext) -> list[ActionCard]:
    """Decide whether a segment is under- or over-invested in ad visibility.

    ``ms = 100 x own_sales / total_sales`` and ``sov`` = own share of ad
    impressions (percent), both for the current window.

    Under-invested (``ms - sov >= 8`` and ``total x (ms - sov) / 100 >=
    sov_under_min_worth`` and live campaigns exist): ``good`` = live
    keyword-auction campaigns with ``1.5 x be <= r < 12 x be``; candidates =
    those that are budget-bound.  With ``good`` but no candidate -> watch
    card "Close the SOV Gap on Bids, Not Budget".  Otherwise prefer
    generic-heavy candidates (own-brand share < 0.4, unknown read as 0) and
    raise the max-ROAS one +15%.  Stake (both): ``total x (ms - sov) / 100 x 0.1``.

    Over-invested (``sov - ms >= 10``): blended ROAS over campaigns with
    attributable ROAS, ``sum(spend x r) / sum(spend)``; below ``2 x be`` ->
    watch card "Over-invested vs Share".  Stake: ``attributable spend x 0.15``.
    """
    cfg, be = ctx.cfg, ctx.be
    cards: list[ActionCard] = []
    for seg in segments_in_scope(ctx):
        m = ctx.segments.get(seg)  # type: ignore[arg-type]
        if m is None or m.market_share_pct is None or m.sov_current is None:
            continue
        ms, sov, tot = m.market_share_pct, m.sov_current, m.total_sales_current or 0.0
        live = live_campaigns(ctx, seg)
        gap = ms - sov
        ev = {"ms": ms, "sov": sov, "gap": gap, "segment_sales": tot}
        facts = f"ms={ms:.1f} sov={sov:.1f} total={fmt_money(tot)}"
        if gap >= cfg.sov_under_gap_pp and tot * gap / 100.0 >= money(ctx, cfg.sov_under_min_worth) and live:
            good = []
            for c in live:
                r = decision_roas(ctx, c)
                if (r is not None and capability_class(ctx, c.ad_type) == CapabilityClass.KEYWORD_AUCTION.value
                        and cfg.sov_roas_min_mult * be <= r < cfg.scale_max_mult * be):
                    good.append((r, c))
            cands = [(r, c) for r, c in good if budget_bound(ctx, ctx.telemetry.get(c.entity_id))]
            stake = tot * gap / 100.0 * cfg.sov_stake_share
            if good and not cands:
                cards.append(new_card(
                    ctx, FAMILY, "watch", "Close the SOV Gap on Bids, Not Budget", segment=seg,
                    direction="none",
                    trigger=(f"market share {ms:.1f}% vs ad share of voice {sov:.1f}%, but no "
                             f"efficient campaign is held back by its budget"),
                    targets="the gap is a bid / coverage gap; raising a non-binding cap changes nothing",
                    evidence=dict(ev, n_good=len(good)), stake=stake, facts=facts))
            elif cands:
                generic = [(r, c) for r, c in cands
                           if (c.own_brand_share or 0.0) < cfg.sov_generic_own_share_max]
                r, c = sorted(generic or cands, key=lambda rc: (-rc[0], rc[1].entity_id))[0]
                b = ctx.telemetry.get(c.entity_id)
                frm = (b.budget_latest or b.avg_budget) if b else None
                pct = cfg.sov_raise_pct
                cards.append(new_card(
                    ctx, FAMILY, "budget", "Increase Budget (close the SOV gap)", segment=seg,
                    entity_id=c.entity_id, direction="up",
                    change=Change(pct=pct, frm=frm, to=frm * (1 + pct / 100) if frm else None),
                    trigger=(f"market share {ms:.1f}% vs ad share of voice {sov:.1f}%; this "
                             f"budget-bound campaign runs at {fmt_roas(r)}"),
                    targets=f"raise {pct:g}%; re-read share of voice after 7 days",
                    evidence=dict(ev, roas=r, spend=c.spend), stake=stake, facts=facts + f" roas={r:.2f}"))
        if sov - ms >= cfg.sov_over_gap_pp:
            num = den = 0.0
            for c in live:
                r = decision_roas(ctx, c)
                if r is not None and c.spend > 0:
                    num += c.spend * r
                    den += c.spend
            if den > 0 and num / den < cfg.sov_over_blended_mult * be:
                cards.append(new_card(
                    ctx, FAMILY, "watch", "Over-invested vs Share", segment=seg, direction="none",
                    trigger=(f"ad share of voice {sov:.1f}% vs market share {ms:.1f}% at blended "
                             f"ROAS {fmt_roas(num / den)}"),
                    targets="act the efficiency cuts first; do not add share of voice here",
                    evidence=dict(ev, blended=num / den, spend=den),
                    stake=den * cfg.sov_over_stake_share, facts=facts + f" blended={num / den:.2f}"))
    return cards
