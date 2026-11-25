"""Efficiency family: budget cuts, budget raises and keyword pauses.

Per segment, in order: (A) the classic budget cut, (B) the verdict-profile
band ladder for keyword-led campaigns, else the classic budget scale,
(C) the keyword pause.  Cards carry family ``efficiency-cut`` or
``efficiency-scale`` (the day-part test in the ladder's dead zone carries
``experiment``).
"""

from __future__ import annotations

from collections import Counter

from ...schema import ActionCard, BudgetTelemetry, Change, KeywordRef
from ..primitives import (ActionContext, CampaignStat, bucket_average, bucket_of, budget_bound,
                          campaign_role, decision_roas, deflator, entity_keywords, fmt_money,
                          fmt_roas, is_conquest, keyword_bench, keyword_demand, keyword_threshold,
                          live_campaigns, money, new_card, proportional_step,
                          raise_pct_from_exhaust, raise_pct_from_sufficiency, roas_floor,
                          segments_in_scope, top_keywords, utilisation)

__all__ = ["efficiency", "efficiency_cut", "efficiency_scale", "verdict_scoreboard",
           "classic_cut_card", "classic_scale_card", "ladder_cards", "pause_cards"]

CUT = "efficiency-cut"
SCALE = "efficiency-scale"


def _cap_now(b: BudgetTelemetry | None) -> float | None:
    if b is None:
        return None
    return b.budget_latest or b.avg_budget


def _change(pct: float, frm: float | None) -> Change:
    return Change(pct=pct, frm=frm, to=(frm * (1 + pct / 100.0)) if frm else None)


def classic_cut_card(ctx: ActionContext, c: CampaignStat) -> ActionCard | None:
    """Classic budget cut: a campaign that loses money at scale and is still running.

    Fires when all hold: ``r < 0.9 x be``; window spend >= ``cut_min_spend``;
    last-7-day spend > 0; no cut applied within the cooldown; the recent half
    is not already back above breakeven (``roas_recent x deflator >= be``
    skips); and even the optimistic bound loses money (``roas_hi x deflator
    >= be`` skips; a missing interval does not gate).  Action: budget -20%.
    Stake: ``spend x max(be - r, 0) / be``.  Returns the card or ``None``.
    """
    cfg, be = ctx.cfg, ctx.be
    r = decision_roas(ctx, c)
    if r is None or r >= cfg.cut_roas_mult * be:
        return None
    if c.spend < money(ctx, cfg.cut_min_spend) or c.spend_7d <= 0:
        return None
    if ("cut", c.entity_id) in ctx.recent_applied:
        return None
    b = ctx.telemetry.get(c.entity_id)
    defl = deflator(ctx, c)
    if b is not None:
        if b.roas_recent is not None and b.roas_recent * defl >= be:
            return None
        if b.roas_hi is not None and b.roas_hi * defl >= be:
            return None
    bud = b.avg_budget if b else None
    pct = -cfg.cut_pct
    stake = c.spend * max(be - r, 0.0) / be
    return new_card(
        ctx, CUT, "budget", "Decrease Budget", segment=c.segment, entity_id=c.entity_id,
        direction="down", change=_change(pct, bud),
        trigger=(f"ROAS {fmt_roas(r)} is below {cfg.cut_roas_mult:g} x breakeven "
                 f"({fmt_roas(be)}) on {fmt_money(c.spend)} spend"),
        targets=(f"cut {cfg.cut_pct:g}%, re-check weekly; stop the campaign if ROAS stays "
                 f"below breakeven after two cuts"),
        evidence={"roas": r, "spend": c.spend, "util": utilisation(b), "breakeven": be},
        stake=stake, facts=f"roas={r:.2f} be={be:.2f} spend={c.spend:.0f}")


def classic_scale_card(ctx: ActionContext, c: CampaignStat) -> ActionCard | None:
    """Classic budget scale: a proven campaign capped by its daily budget.

    Fires when ``r`` is in ``[3 x be, 12 x be)`` (or ``>= 12 x be`` with a
    known own-brand keyword share below 0.5), window spend >=
    ``scale_min_spend``, the campaign is budget-bound, and the lower ROAS
    bound (when known) holds ``2 x be``.  Size: fund to the funding-target
    hour, ``pct = clamp(round(100 x (target - exhaust_hour) / exhaust_hour x
    0.9), 5, 50)``.  Stake: ``spend x max(min(r, 6 x be) - be, 0) x pct / 100``.

    Note: when the campaign is bound by sufficiency and has no exhaust
    minute (verdict profile on a campaign without keyword rows), the raise is
    sized from sufficiency instead of dividing by a missing exhaust hour.
    """
    cfg, be = ctx.cfg, ctx.be
    r = decision_roas(ctx, c)
    b = ctx.telemetry.get(c.entity_id)
    if r is None or b is None or c.spend < money(ctx, cfg.scale_min_spend):
        return None
    binding = budget_bound(ctx, b)
    defl = deflator(ctx, c)
    if b.roas_lo is not None and b.roas_lo * defl < cfg.scale_certainty_mult * be:
        binding = False
    high_ok = (r >= cfg.scale_max_mult * be and c.own_brand_share is not None
               and c.own_brand_share < cfg.scale_high_own_share_max)
    in_band = cfg.scale_min_mult * be <= r < cfg.scale_max_mult * be
    if not ((in_band or high_ok) and binding):
        return None
    evidence = {"roas": r, "spend": c.spend, "util": utilisation(b)}
    if b.exhaust_minute:
        pct = raise_pct_from_exhaust(ctx, b.exhaust_minute)
        evidence["oob_h"] = (1440.0 - b.exhaust_minute) / 60.0
        evidence["missed_impr"] = b.impressions * (1440.0 - b.exhaust_minute) / b.exhaust_minute
        sized = "exhaust"
    else:
        pct = raise_pct_from_sufficiency(ctx, b.sufficiency_3d)
        evidence["suff"] = b.sufficiency_3d
        sized = "sufficiency"
    evidence["sizing"] = sized
    stake = c.spend * max(min(r, cfg.scale_stake_roas_cap_mult * be) - be, 0.0) * pct / 100.0
    return new_card(
        ctx, SCALE, "budget", "Increase Budget", segment=c.segment, entity_id=c.entity_id,
        direction="up", change=_change(pct, b.avg_budget),
        trigger=(f"ROAS {fmt_roas(r)} on {fmt_money(c.spend)} spend and the daily budget "
                 f"runs out early (utilisation {utilisation(b):.0f}%)"),
        targets=(f"raise {pct}%; hold if marginal ROAS decays below "
                 f"{fmt_roas(cfg.scale_certainty_mult * be)}"),
        evidence=evidence, stake=stake,
        facts=f"roas={r:.2f} spend={c.spend:.0f} pct={pct} util={utilisation(b):.0f}")


def _judged(ctx: ActionContext, c: CampaignStat) -> tuple[float, str, float] | None:
    """(decision ROAS, bucket, threshold) when the verdict ladder judges this campaign, else ``None``."""
    if not ctx.verdict:
        return None
    role, _ = campaign_role(ctx, c.entity_id)
    r = decision_roas(ctx, c)
    if role is None or r is None or c.spend < money(ctx, ctx.cfg.scale_min_spend):
        return None
    bucket = bucket_of(role)
    return r, bucket, keyword_threshold(ctx, role, bucket_average(ctx, bucket))


def verdict_scoreboard(ctx: ActionContext) -> dict[str, dict[str, object]]:
    """Scoreboard of every campaign the verdict ladder judged, carded or not.

    ``{entity_id: {r, thr, cap, segment, bucket, spend}}`` where ``cap`` is
    the latest daily cap (else the mean cap).  Input to the budget-neutral
    rebalancer.  Empty under the classic profile.
    """
    board: dict[str, dict[str, object]] = {}
    for seg in segments_in_scope(ctx):
        for c in live_campaigns(ctx, seg):
            j = _judged(ctx, c)
            if j is None:
                continue
            r, bucket, thr = j
            board[c.entity_id] = {"r": r, "thr": thr, "cap": _cap_now(ctx.telemetry.get(c.entity_id)),
                                  "segment": seg, "bucket": bucket, "spend": c.spend}
    return board


def ladder_cards(ctx: ActionContext, c: CampaignStat) -> list[ActionCard]:
    """Verdict-profile band ladder for one keyword-led campaign (``ratio = r / threshold``).

    * ``ratio >= 1.10`` and budget-bound -> "Increase Budget": ``need =
      max(0, 1 / max(sufficiency, 0.05) - 1)``, ``pct = clamp(round(100 x need
      x 0.9), 5, 50)`` on the latest cap; stake ``spend x (r - thr) x pct / 100``.
      Not bound -> no card.
    * ``0.90 <= ratio < 1.10`` (dead zone) -> no budget move; a bid-cut card on
      drag keywords; a day-part experiment when budget-bound (stake ``spend x 0.1``).
    * ``0.50 <= ratio < 0.90`` -> bid-cut card and budget-cut card linked as an
      exactly-one choice pair (``tension_group``).
    * ``ratio < 0.50`` (deep) -> both, not linked.

    Budget cut: step ``30%`` when ``needed = 1 - ratio > 0.40`` and ``r < be``
    (step 1 of an exit), else ``round(clamp(needed, 0.10, 0.30) x 100)``;
    suppressed when the cap was already lowered within 3 days; stake ``spend x
    (1 - r / thr)``.  Bid cut: over drag keywords (ROAS known, not new-user
    top, ``roas < thr x 0.90`` (0.80 for top-volume terms), spend >=
    ``drag_min_spend``), ``wneed = sum((1 - roas / thr) x spend) / sum(spend)``,
    step ``clamp(wneed, 0.10, 0.30)``; suppressed inside an open bid-probe
    window or on the exit path outside the deep band; stake ``sum(spend) x wneed``.

    Returns the cards (possibly none); an unjudged campaign returns ``[]``.
    """
    j = _judged(ctx, c)
    if j is None:
        return []
    r, bucket, thr = j
    cfg, be = ctx.cfg, ctx.be
    b = ctx.telemetry.get(c.entity_id)
    binding = budget_bound(ctx, b)
    cap = _cap_now(b)
    ratio = r / thr
    base_ev = {"roas": r, "spend": c.spend, "threshold": thr, "bucket": bucket,
               "util": utilisation(b), "suff": b.sufficiency_3d if b else None}
    facts = f"roas={r:.2f} thr={thr:.2f} ratio={ratio:.2f} spend={c.spend:.0f}"

    if r > thr and (ratio - 1.0) >= cfg.raise_margin:
        if not binding:
            return []
        pct = raise_pct_from_sufficiency(ctx, b.sufficiency_3d if b else None)
        if b is not None and b.sufficiency_3d is None and b.exhaust_minute:
            pct = raise_pct_from_exhaust(ctx, b.exhaust_minute)
        return [new_card(
            ctx, SCALE, "budget", "Increase Budget", segment=c.segment, entity_id=c.entity_id,
            direction="up", change=_change(pct, cap),
            trigger=(f"ROAS {fmt_roas(r)} beats the {bucket} bar {fmt_roas(thr)} by "
                     f"{100 * (ratio - 1):.0f}% and the budget under-serves the day"),
            targets=(f"raise {pct}% in steps of at most 25%; re-read marginal ROAS after 7 days"),
            evidence=dict(base_ev, band="raise"), stake=c.spend * (r - thr) * pct / 100.0,
            facts=facts + f" pct={pct}")]

    if not r < (1.0 + cfg.raise_margin) * thr:
        return []
    needed = 1.0 - ratio
    dead = ratio >= cfg.dead_zone_low
    deep = ratio < cfg.deep_band_max
    exit1 = (not dead) and needed > cfg.exit_needed_min and r < be
    probed = ("biddown", c.entity_id) in ctx.recent_applied
    cut3 = (b is not None and b.budget_before_recent is not None and b.budget_latest is not None
            and b.budget_latest < b.budget_before_recent - money(ctx, cfg.console_cut_tolerance))
    band = "dead" if dead else ("deep" if deep else "below")
    demand = keyword_demand(ctx)

    lags = []
    for k in entity_keywords(ctx, c.entity_id):
        key = (k.keyword, k.segment if k.segment is not None else c.segment)
        if k.roas is None or key in demand.new_user_top:
            continue
        mult = cfg.drag_roas_mult_top_volume if key in demand.volume_top else cfg.drag_roas_mult
        if k.roas < thr * mult and k.spend >= money(ctx, cfg.drag_min_spend):
            lags.append(k)
    lags.sort(key=lambda k: (-k.spend, k.keyword))

    cards: list[ActionCard] = []
    bid_card = budget_card = None
    if lags and not probed and (deep or dead or not exit1):
        wsp = sum(k.spend for k in lags)
        wneed = sum((1.0 - k.roas / thr) * k.spend for k in lags) / wsp
        kpct = proportional_step(ctx, wneed)
        bid_card = new_card(
            ctx, CUT, "bid", "Decrease Bid (first response)", segment=c.segment,
            entity_id=c.entity_id, direction="down", change=Change(pct=-kpct),
            keywords=[KeywordRef(kw=k.keyword, spend=k.spend, roas=k.roas, bucket=bucket_of(k.role),
                                 bench=thr) for k in lags[: cfg.drag_max_listed]],
            trigger=(f"{len(lags)} keyword(s) run below the {bucket} bar {fmt_roas(thr)} on "
                     f"{fmt_money(wsp)} spend"),
            targets=f"lower their bids {kpct}%; re-read in {cfg.reread_days} days",
            evidence=dict(base_ev, band=band, n_kws=len(lags), lag_spend=wsp, needed=wneed),
            stake=wsp * wneed, facts=facts + f" lags={len(lags)} step={kpct}")
        cards.append(bid_card)
    if not dead and not cut3:
        step = int(cfg.exit_step_pct) if exit1 else proportional_step(ctx, needed)
        title = "Decrease Budget (step 1 of exit)" if exit1 else "Decrease Budget"
        budget_card = new_card(
            ctx, CUT, "budget", title, segment=c.segment, entity_id=c.entity_id,
            direction="down", change=_change(-step, cap),
            trigger=(f"ROAS {fmt_roas(r)} is {100 * needed:.0f}% below the {bucket} bar "
                     f"{fmt_roas(thr)}"),
            targets=f"cut {step}%; re-read in {cfg.reread_days} days",
            evidence=dict(base_ev, band=band, needed=needed, exit=exit1),
            stake=c.spend * (1.0 - ratio), facts=facts + f" step={step}")
        cards.append(budget_card)
    if dead and binding:
        cards.append(new_card(
            ctx, "experiment", "experiment", "Launch Day-Part Experiment", segment=c.segment,
            entity_id=c.entity_id, direction="none",
            keywords=top_keywords(ctx, c.entity_id, cfg.experiment_keywords),
            trigger=(f"ROAS {fmt_roas(r)} sits within 10% of the bar {fmt_roas(thr)} while the "
                     f"budget under-serves the day"),
            targets=(f"{cfg.experiment_days}-day day-part test against control; success = "
                     f"worst-slot ROAS +{cfg.experiment_success_pct:g}% after removing the lowest "
                     f"slot; pauses the campaign in the removed slot"),
            evidence=dict(base_ev, band=band, pauses=True, days=cfg.experiment_days),
            stake=c.spend * cfg.daypart_stake_share, facts=facts))
    if bid_card is not None and budget_card is not None and not deep:
        for mine, other in ((bid_card, budget_card), (budget_card, bid_card)):
            mine.tension_group = str(c.entity_id)
            mine.tension_with = other.id
    return cards


def pause_cards(ctx: ActionContext, segment: str | None) -> list[ActionCard]:
    """Keyword pause: one "Pause / Negative Keywords" card per campaign with wasteful terms.

    Classic, per keyword row with a known ROAS, ``fl = roas_floor(role)``:
    conquest terms flag at ``roas < min(fl, be)`` with spend >=
    ``pause_conquest_min_spend``; other roles at ``roas <= 0.35 x fl`` with
    spend >= ``pause_other_min_spend``.  Bucket rescue un-flags a term that is
    normal for its kind: bucket spend >= ``rescue_bucket_min_spend``, bucket
    average >= ``0.5 x fl`` and ``roas >= 0.6 x bucket average``.

    Verdict: ``bar = min(mult x threshold, be)`` with ``mult = 0.60`` for
    conquest and ``0.50`` otherwise; flagged when ``roas <= bar`` and spend >=
    ``pause_verdict_min_spend``; no rescue (a negative match is irreversible,
    so only absolute waste earns it).

    Stake (leak): ``sum(spend x max(0, 1 - roas / be))``.
    """
    cfg, be = ctx.cfg, ctx.be
    bench = keyword_bench(ctx)
    cards: list[ActionCard] = []
    for c in live_campaigns(ctx, segment):
        flagged = []
        for k in entity_keywords(ctx, c.entity_id):
            if k.roas is None:
                continue
            bucket = bucket_of(k.role)
            entry = bench.get(bucket)
            if ctx.verdict:
                thr = keyword_threshold(ctx, k.role, entry[0] if entry else None)
                mult = cfg.pause_verdict_conquest_mult if is_conquest(k.role) else cfg.pause_verdict_mult
                hit = (k.roas <= min(mult * thr, be)
                       and k.spend >= money(ctx, cfg.pause_verdict_min_spend))
            else:
                fl = roas_floor(ctx, k.role)
                if is_conquest(k.role):
                    hit = k.roas < min(fl, be) and k.spend >= money(ctx, cfg.pause_conquest_min_spend)
                else:
                    hit = (k.roas <= cfg.pause_other_roas_mult * fl
                           and k.spend >= money(ctx, cfg.pause_other_min_spend))
                if (hit and entry and entry[1] >= money(ctx, cfg.rescue_bucket_min_spend)
                        and entry[0] >= cfg.rescue_bucket_avg_mult * fl
                        and k.roas >= cfg.rescue_keyword_mult * entry[0]):
                    hit = False
            if hit:
                flagged.append(k)
        if not flagged:
            continue
        flagged.sort(key=lambda k: (-k.spend, k.keyword))
        leak = sum(k.spend * max(0.0, 1.0 - k.roas / be) for k in flagged)
        dominant = Counter(bucket_of(k.role) for k in flagged).most_common(1)[0][0]
        top3 = ", ".join(f"{k.keyword} ({fmt_roas(k.roas)})" for k in flagged[:3])
        branded = bench.get("branded")
        cards.append(new_card(
            ctx, CUT, "keyword", "Pause / Negative Keywords", segment=segment,
            entity_id=c.entity_id, direction="down",
            keywords=[KeywordRef(kw=k.keyword, spend=k.spend, roas=k.roas, bucket=bucket_of(k.role),
                                 bench=bench[bucket_of(k.role)][0] if bucket_of(k.role) in bench else None)
                      for k in flagged[: cfg.pause_max_listed]],
            trigger=f"{len(flagged)} keyword(s) lose money: {top3}",
            targets=(f"negative-match them on this campaign; keep any conquest term at or above "
                     f"{fmt_roas(cfg.conquest_keep_roas_mult * be)}"),
            evidence={"n_kws": len(flagged), "leak": leak, "bucket": dominant,
                      "bucket_avg": bench[dominant][0] if dominant in bench else None,
                      "brand_kw_avg": branded[0] if branded else None},
            stake=leak, facts=f"n={len(flagged)} leak={leak:.0f} be={be:.2f}"))
    return cards


def efficiency(ctx: ActionContext) -> list[ActionCard]:
    """Decide which campaigns and keywords to cut and which capped winners to fund.

    Per segment in scope: classic cut on every live campaign (all profiles);
    then, per campaign, the verdict ladder when it applies (verdict profile,
    campaign has keyword rows, ROAS known, spend >= ``scale_min_spend``) else
    the classic scale; then keyword pauses.  When the ladder itself cuts a
    campaign's budget the classic cut is not emitted for it: one campaign
    gets one budget cut, sized by the rule that judged it, and every card
    id in the output is unique.  Returns the cards in emission order.
    """
    cards: list[ActionCard] = []
    for seg in segments_in_scope(ctx):
        live = live_campaigns(ctx, seg)
        ladder = {c.entity_id: ladder_cards(ctx, c) for c in live if _judged(ctx, c) is not None}
        for c in live:
            if any(x.type == "budget" and x.direction == "down" for x in ladder.get(c.entity_id, ())):
                continue
            card = classic_cut_card(ctx, c)
            if card is not None:
                cards.append(card)
        for c in live:
            if c.entity_id in ladder:
                cards.extend(ladder[c.entity_id])
            else:
                card = classic_scale_card(ctx, c)
                if card is not None:
                    cards.append(card)
        cards.extend(pause_cards(ctx, seg))
    return cards


def efficiency_cut(ctx: ActionContext) -> list[ActionCard]:
    """The ``efficiency-cut`` cards of ``efficiency`` (budget cuts, campaign bid cuts, keyword pauses)."""
    return [c for c in efficiency(ctx) if c.family == CUT]


def efficiency_scale(ctx: ActionContext) -> list[ActionCard]:
    """The ``efficiency-scale`` cards of ``efficiency`` (budget raises)."""
    return [c for c in efficiency(ctx) if c.family == SCALE]
