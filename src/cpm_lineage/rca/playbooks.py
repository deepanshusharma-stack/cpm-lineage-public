"""Playbooks: how a question's intent turns verified facts into an answer, sections and a lever view.

An *intent* is the API (the question wording belongs to the caller's
locale).  Each intent has a label, an answer function and an ordered list
of section keys.  Everything here is templated text over numbers that were
already computed and verified; nothing new is calculated except simple
ratios of those numbers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .attribute import Attribution, Lever
from .decompositions import roas_identity, rounded_parts, step_scan
from .facts import Facts, GeoRow, pct_move

# ---------------------------------------------------------------------------
# intents
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Intent:
    key: str
    label: str
    sections: tuple[str, ...]
    axes: tuple[str, ...] = ()     # on-question lever axes in priority order; empty = show every lever


INTENTS: dict[str, Intent] = {i.key: i for i in (
    Intent("why", "full root-cause read", ("shape", "geos", "reads", "ledger", "funnel", "levers")),
    Intent("sov_invest", "voice versus investment", ("invest", "voice", "funnel", "levers"), ("sov", "conversion")),
    Intent("voice", "ad-voice deep dive", ("invest", "voice", "reads", "levers"), ("sov",)),
    Intent("roas_move", "ROAS decomposition", ("funnel", "invest", "shape", "reads", "levers"),
           ("conversion", "sov")),
    Intent("scale_cut", "scale / cut map", ("geo_actions", "growth", "availability_detail", "levers"),
           ("geo", "sov", "availability")),
    Intent("cut_waste", "waste audit", ("geo_actions", "availability_detail", "funnel", "levers"),
           ("geo", "availability", "conversion")),
    Intent("grow_ms", "ROAS-safe growth paths", ("growth", "voice", "geo_actions", "levers"),
           ("sov", "geo", "competition")),
    Intent("ms_decline", "share movement", ("rivals_detail", "geos", "shape", "ledger", "levers"),
           ("competition", "pricing", "sov")),
    Intent("geo", "geo winners and losers", ("geos", "geo_actions", "availability_detail", "levers"),
           ("geo", "availability")),
    Intent("budget_mix", "ad-type mix", ("adtypes", "funnel", "levers"), ("conversion", "sov")),
    Intent("availability", "availability cost", ("availability_detail", "geos", "levers"), ("availability",)),
    Intent("top_action", "highest-impact action", ("action_stack", "ledger")),
    Intent("pricing", "pricing and promo dive", ("pricing_detail", "shape", "ledger", "levers"), ("pricing",)),
    Intent("competition", "share movement", ("rivals_detail", "geos", "shape", "ledger", "levers"),
           ("competition", "pricing", "sov")),
)}

PRESET_INTENTS: tuple[str, ...] = tuple(k for k in INTENTS if k != "competition")
"""Intents offered as presets; ``competition`` is an alias of ``ms_decline``."""

FOCUS_INTENT: dict[str, str] = {
    "sov": "voice", "conversion": "roas_move", "availability": "availability", "competition": "ms_decline",
    "pricing": "pricing", "offtake": "why", "general": "why",
}

FOCUS_CAUSES: dict[str, tuple[str, ...]] = {
    "sov": ("AD_PULLBACK", "OUT_ESCALATED", "AD_SPEND_CUT"),
    "availability": ("AVAILABILITY_DRAG", "PASSENGERS"),
    "pricing": ("PROMO_PULLBACK", "STICKER_MIX"),
    "competition": ("COMPETITOR_ABSORPTION", "NEW_ENTRANT", "SHARE_CEDED"),
    "conversion": ("PROMO_PULLBACK", "STICKER_MIX", "AVAILABILITY_DRAG"),
    "offtake": (),
}

INTENT_FOCUS: dict[str, str] = {
    "voice": "sov", "sov_invest": "sov", "roas_move": "conversion", "budget_mix": "conversion",
    "availability": "availability", "cut_waste": "availability", "ms_decline": "competition", "competition": "competition",
    "grow_ms": "competition", "pricing": "pricing", "why": "offtake", "geo": "offtake",
    "scale_cut": "offtake", "top_action": "offtake",
}


@dataclass(frozen=True)
class Answer:
    """The direct answer to a question: an optional verdict key, sentences, and the numbers used."""

    intent: str
    verdict: str | None = None
    lines: tuple[str, ...] = ()
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return " ".join(self.lines)


@dataclass(frozen=True)
class Section:
    key: str
    title: str
    lines: tuple[str, ...] = ()


def _pc(x: float | None, d: int = 1) -> str:
    return "n/a" if x is None else f"{x:+.{d}f}%"


def _av(x: float | None) -> str:
    return "n/a" if x is None else f"{100.0 * x:.0f}%"


def _n(x: float | None) -> str:
    return "n/a" if x is None else f"{x:,.0f}"


def _funnel(facts: Facts):
    f = facts.funnel
    return None if f is None or f.stale else f


# ---------------------------------------------------------------------------
# geo buckets
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GeoBuckets:
    """Every named geo sorted into wait / win / fix, plus the resilient shift destinations."""

    wait: tuple[GeoRow, ...] = ()
    win: tuple[GeoRow, ...] = ()
    fix: tuple[GeoRow, ...] = ()
    resilient: tuple[GeoRow, ...] = ()

    @property
    def shift_to(self) -> tuple[GeoRow, ...]:
        """Where budget goes when nothing is winning: resilient geos that are not in ``fix``."""
        fixed = {g.geo for g in self.fix}
        return tuple(g for g in self.resilient if g.geo not in fixed)


def geo_buckets(facts: Facts) -> GeoBuckets:
    """Sort every named geo (aggregate buckets excluded) into action buckets.

    ::

        WAIT       segment_pct <= geo_collapse_pct       (the segment collapsed; bidding cannot fix it)
        living     everything else
          WIN      own_pct > segment_pct + geo_beat_pp
          FIX      own_pct < segment_pct - geo_beat_pp   (a brand-side gap operations can close)
        resilient  top ``resilient_geo_count`` living geos by segment_pct desc

    Sequencing rule: when nothing is WIN, budget goes to resilient geos not
    in FIX; when every resilient geo is also FIX the call is "fix first,
    then shift".  Returns :class:`GeoBuckets`.
    """
    t = facts.config
    wait, win, fix, living = [], [], [], []
    for g in facts.offtake.geo_rows:
        if g.is_aggregate or g.segment_pct is None:
            continue
        if g.segment_pct <= t.geo_collapse_pct:
            wait.append(g)
            continue
        living.append(g)
        if g.own_pct is None:
            continue
        if g.own_pct > g.segment_pct + t.geo_beat_pp:
            win.append(g)
        elif g.own_pct < g.segment_pct - t.geo_beat_pp:
            fix.append(g)
    resilient = sorted(living, key=lambda g: (-(g.segment_pct or 0.0), g.geo))[: t.resilient_geo_count]
    return GeoBuckets(tuple(wait), tuple(win), tuple(fix), tuple(resilient))


def geo_read(g: GeoRow, facts: Facts) -> str:
    """One-word read of a geo row: collapsed (optionally "trailed even that") / beat / with / trailed."""
    t = facts.config
    if g.segment_pct is None or g.own_pct is None:
        return "no prior"
    if g.segment_pct <= t.geo_collapse_pct:
        return "collapsed, trailed even that" if g.own_pct < g.segment_pct - t.geo_trail_far_pp else "collapsed"
    if g.own_pct > g.segment_pct + t.geo_beat_pp:
        return "beat"
    if g.own_pct < g.segment_pct - t.geo_beat_pp:
        return "trailed"
    return "with"


# ---------------------------------------------------------------------------
# verdict helpers
# ---------------------------------------------------------------------------


def invest_verdict(facts: Facts, attr: Attribution) -> Answer:
    """Decide whether the brand is under-, over- or fairly invested in ad voice.

    ::

        gap = ad SOV - market share (pp);  sd = spend % change;  pd = pool % change
        roas_ok = direct ROAS >= guard
        cutting_into_growth = sd <= -spend_cut_pct and pd > 0
        gap < under_voiced_gap_pp or cutting_into_growth  -> UNDER
        gap > over_voiced_gap_pp                          -> OVER_PROFITABLE if roas_ok else OVER
        else                                              -> PARITY

    Doctrine: a brand defending or regaining share needs voice at or above
    its market share; under-investment has two forms (voice below share, or
    cutting spend while the attention pool grows); voice far above share
    only pays while ROAS clears the guard.  Returns an :class:`Answer` with
    verdict ``UNDER | OVER_PROFITABLE | OVER | PARITY`` or ``None`` when
    voice data is unavailable.
    """
    t = facts.config
    v, f = facts.voice, _funnel(facts)
    if v is None or v.sov_c is None:
        return Answer("sov_invest", None, ("Voice data is unavailable, so the investment level cannot be judged.",))
    gap = v.sov_c - attr.share_c
    sd = pct_move(f.spend_c, f.spend_p) if f else None
    pd = pct_move(v.pool_c, v.pool_p)
    roas_ok = bool(f and f.roas_c is not None and f.roas_c >= facts.scope.roas_guard)
    cutting = sd is not None and sd <= -t.spend_cut_pct and pd is not None and pd > 0
    base = f"Ad SOV {v.sov_c:.1f}% against a market share of {attr.share_c:.1f}% (gap {gap:+.1f}pp)."
    lines = [base]
    if gap < t.under_voiced_gap_pp or cutting:
        verdict = "UNDER"
        lines.append("UNDER-investing: " + ("spend was cut while the attention pool grew."
                                            if cutting else "voice sits below market share."))
        lines.append("There is ROAS headroom to buy voice profitably." if roas_ok
                     else "Fix efficiency first: ROAS is under the guard.")
        if cutting and v.own_c >= v.own_p:
            lines.append("Voice has HELD so far: the exposure is forward.")
    elif gap > t.over_voiced_gap_pp:
        verdict = "OVER_PROFITABLE" if roas_ok else "OVER"
        lines.append("OVER-voiced but profitable: voice above share is paying while ROAS clears the guard."
                     if roas_ok else "OVER-investing: voice far above share with ROAS under the guard.")
    else:
        verdict = "PARITY"
        lines.append("Invested at parity: " + ("scaling is a choice, not a need." if roas_ok
                                                else "improve efficiency before adding voice."))
    return Answer("sov_invest", verdict, tuple(lines),
                  {"gap_pp": gap, "spend_pct": sd, "pool_pct": pd, "roas_ok": roas_ok, "cutting_into_growth": cutting})


def mix_verdict(facts: Facts) -> Answer:
    """Decide whether the ad-type budget mix is broadly optimal.

    Among funded ad types with a ROAS (sales tracked; deflator applied) and
    more than ``mix_min_share`` of spend, sorted by ROAS: NOT_OPTIMAL iff
    there are at least two such types, the worst ROAS is above 0,
    ``best / worst >= mix_ratio`` and the worst holds at least
    ``mix_worst_share`` of spend; else OPTIMAL.  Types whose sales are not
    tracked are listed as "n/t", never as a ROAS of 0.  Always a direction
    to re-check next window, not dogma: ad types play different roles.
    """
    t = facts.config
    f = _funnel(facts)
    if f is None or not f.ad_types or f.spend_c <= 0:
        return Answer("budget_mix", None, ("The ads ledger is unavailable, so the mix cannot be judged honestly.",))
    rows = [(a, a.spend_c / f.spend_c, a.roas_c) for a in f.ad_types if a.spend_c > 0]
    shown = ", ".join(f"{a.ad_type} {100 * s:.0f}% of spend at "
                      f"{'n/t' if r is None else f'{r:.2f}'}" for a, s, r in rows)
    ranked = sorted(((a, s, r) for a, s, r in rows if r is not None and s > t.mix_min_share), key=lambda x: x[2])
    data: dict[str, Any] = {"types": len(rows)}
    if len(ranked) >= 2:
        worst, best = ranked[0], ranked[-1]
        data.update(best=best[0].ad_type, worst=worst[0].ad_type, best_roas=best[2], worst_roas=worst[2],
                    worst_share=worst[1])
        if worst[2] > 0 and best[2] / worst[2] >= t.mix_ratio and worst[1] >= t.mix_worst_share:
            return Answer("budget_mix", "NOT_OPTIMAL", (
                f"Mix: {shown}.",
                f"Not optimal: {best[0].ad_type} returns {best[2]:.2f} against {worst[2]:.2f} on "
                f"{worst[0].ad_type}, which holds {100 * worst[1]:.0f}% of spend.",
                "Treat this as a direction to re-check next window, not dogma: ad types play different roles.",
            ), data)
    return Answer("budget_mix", "OPTIMAL", (
        f"Mix: {shown}.", "Broadly optimal: no funded type is materially out-earned at meaningful spend.",
        "Re-check next window; ad types play different roles."), data)


# ---------------------------------------------------------------------------
# answers
# ---------------------------------------------------------------------------


def focus_answer(focus: str | None, facts: Facts, attr: Attribution) -> Answer:
    """One-paragraph answer for a focus (sov, availability, pricing, competition, conversion, offtake).

    ``general`` / ``None`` / unknown returns an empty answer.
    """
    t = facts.config
    intent = FOCUS_INTENT.get(focus or "", "why")
    lines: list[str] = []
    if focus == "sov":
        v = facts.voice
        if v is None or v.sov_c is None or v.sov_p is None:
            lines.append("Voice data is unavailable for this scope.")
        else:
            lines.append(f"Battleground ad SOV moved {v.sov_p:.1f}% -> {v.sov_c:.1f}%; own ad impressions "
                         f"{_pc(attr.own_voice_pct)} against a pool at {_pc(attr.pool_voice_pct)}.")
            if attr.has("AD_PULLBACK"):
                lines.append("Own volume fell and trailed the pool: a genuine pullback.")
            elif attr.has("OUT_ESCALATED"):
                lines.append("The pool outgrew the brand: dilution, not a pullback.")
            else:
                lines.append(f"The move is inside the {t.sov_drop_pp:.1f}pp gate: voice is not the story.")
    elif focus == "availability":
        a = facts.availability
        if a is None or a.own_c is None:
            lines.append("Availability data is unavailable for this scope.")
        else:
            lines.append(f"Own availability {_av(a.own_p)} -> {_av(a.own_c)} against a floor of "
                         f"{_av(t.availability_floor)}" + (f" and a segment benchmark of {_av(a.segment_c)}."
                                                           if a.segment_c is not None else "."))
            lines.append("It is dragging the line." if attr.has("AVAILABILITY_DRAG") else "Availability is not the story.")
    elif focus == "pricing":
        p = facts.pricing
        if p is None or p.depth_c is None or p.depth_p is None:
            lines.append("Pricing data is unavailable for this scope.")
        else:
            lines.append(f"Own discount depth {p.depth_p:.1f}% -> {p.depth_c:.1f}% "
                         f"({p.depth_c - p.depth_p:+.1f}pp against a {t.promo_depth_pp:.1f}pp gate).")
            if attr.has("PROMO_PULLBACK"):
                lines.append("The pullback is a confirmed cause.")
            if attr.has("STICKER_MIX"):
                lines.append(f"{len(p.sticker_mix)} product(s) show mixed list prices.")
            if not (attr.has("PROMO_PULLBACK") or attr.has("STICKER_MIX")):
                lines.append("Price is not the story.")
    elif focus == "competition":
        return share_answer(facts, attr)
    elif focus == "conversion":
        if attr.mech is None:
            lines.append("Conversion impressions are unavailable, so the unit move cannot be split.")
        else:
            total, vis, con = rounded_parts(attr.mech.total, attr.mech.impr_effect)
            lines.append(f"The unit move of {total:+,.0f} is {attr.mech.read}: {vis:+,.0f} from visibility and "
                         f"{con:+,.0f} from conversion.")
    elif focus == "offtake":
        lines.append(f"Own sales moved {_pc(attr.own_pct)} while the segment moved {_pc(attr.segment_pct)}; "
                     f"share {attr.share_p:.2f}% -> {attr.share_c:.2f}%. {attr.quadrant_read}")
        named = [c.title for c in attr.causes if c.key != "SHARE_CEDED"]
        if named:
            lines.append("Confirmed causes: " + "; ".join(dict.fromkeys(named)) + ".")
    return Answer(intent, None, tuple(lines))


def roas_answer(facts: Facts, attr: Attribution) -> Answer:
    """Answer "why did ROAS move" with D3: ``ROAS = revenue per 1000 impressions / CPM``.

    Order: (1) ROAS prior -> current with % change; (2) with prior spend,
    spend % and attributed revenue % (``revenue = ROAS x spend``): revenue
    trailing spend (``revenue % < spend %``) is an efficiency loss (verdict
    ``EFFICIENCY``), otherwise the move is mostly a budget artifact
    (``BUDGET_ARTIFACT``); (3) revenue per 1000 impressions and CPM with %
    changes; (4) the mid-funnel rate when both windows have it; (5) when
    spend fell by ``spend_cut_pct`` or more, a note that the cut and the
    ROAS move are separate decisions (a ratio is unaffected by less spend
    at the same efficiency).
    """
    t = facts.config
    f = _funnel(facts)
    if f is None or f.roas_p is None or f.roas_c is None:
        return Answer("roas_move", None, ("The ads ledger is stale or unfunded for one window, so the ROAS move "
                                          "cannot be computed honestly.",))
    lines = [f"Direct ROAS moved {f.roas_p:.2f} -> {f.roas_c:.2f} ({_pc(pct_move(f.roas_c, f.roas_p))})."]
    verdict = None
    data: dict[str, Any] = {}
    sd = pct_move(f.spend_c, f.spend_p)
    if sd is not None:
        rd = pct_move(f.roas_c * f.spend_c, f.roas_p * f.spend_p)
        data.update(spend_pct=sd, revenue_pct=rd)
        if rd is not None and rd < sd:
            verdict = "EFFICIENCY"
            lines.append(f"Spend {_pc(sd)}, attributed revenue {_pc(rd)}: revenue trailed spend, an "
                         "efficiency loss, not a budget artifact.")
        else:
            verdict = "BUDGET_ARTIFACT"
            lines.append(f"Spend {_pc(sd)}, attributed revenue {_pc(rd)}: revenue kept pace with spend, so "
                         "the ROAS move is mostly a budget artifact, not an efficiency loss.")
    ip, ic = roas_identity(f.spend_p, f.impr_p, f.roas_p), roas_identity(f.spend_c, f.impr_c, f.roas_c)
    if ip and ic:
        data.update(rpi_p=ip.rpi, rpi_c=ic.rpi, cpm_p=ip.cpm, cpm_c=ic.cpm)
        lines.append(f"Revenue per 1000 impressions {ip.rpi:,.1f} -> {ic.rpi:,.1f} ({_pc(pct_move(ic.rpi, ip.rpi))}); "
                     f"CPM {ip.cpm:,.1f} -> {ic.cpm:,.1f} ({_pc(pct_move(ic.cpm, ip.cpm))}).")
    if f.mid_rate_p is not None and f.mid_rate_c is not None:
        lines.append(f"Inside revenue per impression, the {f.mid_label} moved {f.mid_rate_p:.2f}% -> "
                     f"{f.mid_rate_c:.2f}%.")
    if sd is not None and sd <= -t.spend_cut_pct:
        lines.append("The spend cut and the ROAS move are separate decisions: a ratio is unaffected by less "
                     "spend at the same efficiency.")
    return Answer("roas_move", verdict, tuple(lines), data)


def _names(rows, n: int = 4) -> str:
    return ", ".join(g.geo for g in rows[:n])


def geo_answer(facts: Facts, attr: Attribution) -> Answer:
    """Geo winners and losers with the sequencing rule (win, else resilient-not-fix, else fix first)."""
    b = geo_buckets(facts)
    lines = []
    if b.win:
        lines.append(f"Winning (beating a living segment): {_names(b.win)}.")
    if b.fix:
        lines.append(f"Fix (trailing a living segment, a brand-side gap): {_names(b.fix)}.")
    if b.wait:
        lines.append(f"Wait (segment collapsed; bidding cannot fix it): {_names(b.wait)}.")
    if not b.win:
        if b.shift_to:
            lines.append(f"Nothing is winning: shift budget to the resilient geos {_names(b.shift_to)}.")
        elif b.resilient:
            lines.append("Fix first, then shift: the resilient geos are the destination, not yet the spend.")
    if not lines:
        lines.append("No geo stands out in either direction.")
    return Answer("geo", None, tuple(lines), {"win": len(b.win), "fix": len(b.fix), "wait": len(b.wait)})


def scale_cut_answer(facts: Facts, attr: Attribution) -> Answer:
    """Scale / cut map: where to add (geos, hero product, pockets) and where to stop (collapsed geos, passengers)."""
    b = geo_buckets(facts)
    off = facts.offtake
    scale, cut = [], []
    if b.win:
        scale.append(f"geos {_names(b.win)}")
    elif b.shift_to:
        scale.append(f"resilient geos {_names(b.shift_to)}")
    elif b.resilient:
        scale.append(f"resilient geos {_names(b.resilient)} (fix, then scale)")
    if off.product_rows and off.cur_value > 0:
        hero = off.product_rows[0]
        scale.append(f"the hero product {hero.product_id} ({100 * hero.cur_value / off.cur_value:.0f}% of current "
                     "value; protect its budget before anything else)")
    if attr.pockets:
        scale.append("pockets " + ", ".join(p.keyword for p in attr.pockets[:3]))
    if b.wait:
        cut.append(f"bids in {_names(b.wait)}")
    if attr.has("PASSENGERS"):
        cut.append("ads on the passenger products " + ", ".join(attr.passenger_ids[:4]))
    lines = ["Scale: " + ("; ".join(scale) if scale else "nothing clears the bar") + ".",
             "Cut: " + ("; ".join(cut) if cut else "nothing priced") + "."]
    return Answer("scale_cut", None, tuple(lines), {"scale": len(scale), "cut": len(cut)})


def waste_items(facts: Facts, attr: Attribution) -> list[tuple[str, str, float | None]]:
    """Waste audit items in fixed order: ``(id, description, priced value or None)``.

    (1) estimated sales lost to unavailability; (2) spend bid into collapsed
    geos, named but not priced because geo-level spend is not tracked; (3)
    ad spend on passenger products; (4) the whole line under the ROAS guard.
    """
    out: list[tuple[str, str, float | None]] = []
    a, f = facts.availability, _funnel(facts)
    if a is not None and a.loss_c:
        out.append(("availability_loss", f"estimated sales lost to unavailability: {_n(a.loss_c)}", a.loss_c))
    b = geo_buckets(facts)
    if b.wait:
        out.append(("collapsed_geos", f"bids in collapsed geos ({_names(b.wait)}): named, not priced", None))
    if f is not None and attr.passenger_ids:
        spend = sum(f.product_spend.get(p, 0.0) for p in attr.passenger_ids)
        if spend > 0:
            out.append(("passenger_spend", f"ad spend on passenger products: {_n(spend)}", spend))
    if f is not None and f.roas_c is not None and f.roas_c < facts.scope.roas_guard:
        out.append(("under_guard", f"the whole line runs at ROAS {f.roas_c:.2f}, under the guard of "
                                   f"{facts.scope.roas_guard:.2f}", f.spend_c))
    return out


def waste_answer(facts: Facts, attr: Attribution) -> Answer:
    """Waste audit as an answer; "No priced waste found" when nothing qualifies."""
    items = waste_items(facts, attr)
    if not items:
        return Answer("cut_waste", "NONE", ("No priced waste found.",))
    return Answer("cut_waste", "FOUND", tuple(f"{i + 1}. {d}." for i, (_, d, _) in enumerate(items)),
                  {"items": [k for k, _, _ in items]})


def growth_answer(facts: Facts, attr: Attribution) -> Answer:
    """ROAS-safe growth paths, in order: first pocket; geos beating a living segment; else the most exposed rival.

    The exposed rival is the first non-own, non-sibling mover with ``d_pp <
    rival_ceding_pp`` ("take share from the one already losing it, not the
    one winning"), with the caveat that head-on contests pressure ROAS.
    Ends with the ROAS headroom over the guard.
    """
    lines = []
    data: dict[str, Any] = {}
    if attr.pockets:
        p = attr.pockets[0]
        lines.append(f"Path 1: the unclaimed pocket '{p.keyword}' (pool {_pc(p.pool_growth_pct, 0)}, own SOV "
                     f"{p.own_sov_pct:.1f}%).")
    b = geo_buckets(facts)
    if b.win:
        lines.append(f"Path {len(lines) + 1}: geos already beating a living segment: {_names(b.win)}.")
    if not lines:
        r = exposed_rival(facts)
        if r is not None:
            data["rival"] = r.brand
            lines.append(f"Take share from the one already losing it: {r.brand} shed {abs(r.d_pp):.1f}pp. "
                         "A head-on contest pressures ROAS; size it small first.")
        else:
            lines.append("No ROAS-safe growth path stands out in this window.")
    f = _funnel(facts)
    if f is not None and f.roas_c is not None:
        lines.append(f"ROAS headroom over the guard: {f.roas_c - facts.scope.roas_guard:+.2f}.")
    return Answer("grow_ms", None, tuple(lines), data)


def exposed_rival(facts: Facts):
    """The first non-own, non-sibling mover losing more than ``|rival_ceding_pp|`` of share, or ``None``."""
    if facts.rivals is None:
        return None
    return next((m for m in facts.rivals.movers
                 if not m.is_own and not m.is_sibling and m.d_pp < facts.config.rival_ceding_pp), None)


def share_answer(facts: Facts, attr: Attribution) -> Answer:
    """Share movement: own share with rose / fell / held wording, then who gained and who shed."""
    t = facts.config
    d = attr.share_pp
    word = "rose" if d > t.share_word_band_pp else "fell" if d < -t.share_word_band_pp else "held"
    lines = [f"Own share {word}: {attr.share_p:.2f}% -> {attr.share_c:.2f}% ({d:+.2f}pp) while the segment moved "
             f"{_pc(attr.segment_pct)}."]
    if facts.rivals is not None:
        movers = [m for m in facts.rivals.movers if not m.is_own and abs(m.d_pp) >= t.mover_pp]
        up = [m for m in movers if m.d_pp > 0]
        down = [m for m in movers if m.d_pp < 0]
        if up:
            lines.append("Gained: " + ", ".join(
                f"{m.brand} {m.d_pp:+.1f}pp" + (" (same house)" if m.is_sibling else "") for m in up[:3]) + ".")
        if down:
            lines.append("Shed: " + ", ".join(f"{m.brand} {m.d_pp:+.1f}pp" for m in down[:3]) + ".")
        if not movers:
            lines.append(f"No rival moved by {t.mover_pp:.1f}pp or more.")
    return Answer("ms_decline", word.upper(), tuple(lines), {"share_pp": d})


def availability_answer(facts: Facts, attr: Attribution) -> Answer:
    """What unavailability cost: loss this window as % of sales, whether it drove the move, where the holes are.

    When the loss is within ``loss_stable_frac`` of the prior window's and
    own sales fell beyond ``not_driver_own_pct``, availability is stated NOT
    to be what drove the move (verdict ``NOT_DRIVER``).
    """
    t = facts.config
    a = facts.availability
    if a is None or a.own_c is None:
        return Answer("availability", None, ("Availability data is unavailable for this scope.",))
    lines = []
    verdict = "DRAG" if attr.has("AVAILABILITY_DRAG") else "HEALTHY"
    if a.loss_c is not None:
        share = 100.0 * a.loss_c / facts.offtake.cur_value if facts.offtake.cur_value else None
        lines.append(f"Estimated sales lost to unavailability: {_n(a.loss_c)}"
                     + (f" ({share:.1f}% of the window's sales)." if share is not None else "."))
        if a.loss_p and abs(a.loss_c / a.loss_p - 1.0) <= t.loss_stable_frac and attr.own_pct < t.not_driver_own_pct:
            verdict = "NOT_DRIVER"
            lines.append("The loss is about what it was last window, so availability is NOT what drove the move.")
        top = sorted((r for r in a.product_rows if r.loss_c > 0), key=lambda r: -r.loss_c)[:3]
        if top:
            lines.append("Largest losses: " + ", ".join(f"{r.product_id} {_n(r.loss_c)}" for r in top) + ".")
    lines.append(f"Own availability {_av(a.own_p)} -> {_av(a.own_c)}"
                 + (f"; segment {_av(a.segment_c)}." if a.segment_c is not None else "."))
    if a.geo_flags:
        lines.append("Geo holes: " + ", ".join(f"{g} ({_av(v)})" for g, v in a.geo_flags[:4]) + ".")
    under = [r for r in a.product_rows if r.availability_c is not None and r.availability_c < t.availability_floor]
    if under:
        lines.append("Products under the floor: " + ", ".join(
            f"{r.product_id} ({_av(r.availability_c)})" for r in under[:4]) + ".")
    return Answer("availability", verdict, tuple(lines))


@dataclass(frozen=True)
class RankedAction:
    lever: Lever
    size: float | None = None      # recoverable value where the data prices it
    basis: str = ""


def rank_actions(facts: Facts, attr: Attribution) -> list[RankedAction]:
    """Rank the gate levers by recoverable value where the data prices them; unsized levers go last.

    * restore budget = ``(spend_p - spend_c) x ROAS_p``, only when
      ``AD_SPEND_CUT`` fired AND own voice actually fell (never claim
      recoverable revenue when the cut cost no voice);
    * fix availability (supply triage, passengers) = the estimated loss,
      when ``AVAILABILITY_DRAG`` or ``PASSENGERS`` fired;
    * defend the lane = ``(share_p - share_c) / 100 x segment current
      value`` when ``COMPETITOR_ABSORPTION`` fired and share fell: what can
      be recovered is the own share shortfall at today's segment size, not
      the rival's whole gain.

    Sizes are matched to levers by lever id.  The sized lines overlap: total
    recoverable is bounded by the share shortfall, not by their sum.
    """
    f, a = _funnel(facts), facts.availability
    sizes: dict[str, tuple[float, str]] = {}
    cut = attr.cause("AD_SPEND_CUT")
    if cut is not None and f is not None and f.roas_p is not None and not cut.data.get("voice_held"):
        sizes["restore_budget"] = ((f.spend_p - f.spend_c) * f.roas_p, "spend withdrawn x prior ROAS")
    if a is not None and a.loss_c and (attr.has("AVAILABILITY_DRAG") or attr.has("PASSENGERS")):
        key = "supply_triage" if attr.lever("supply_triage") else "passengers"
        sizes[key] = (a.loss_c, "estimated sales lost to unavailability")
    if attr.has("COMPETITOR_ABSORPTION") and attr.share_c < attr.share_p:
        sizes["defend_lane"] = ((attr.share_p - attr.share_c) / 100.0 * facts.offtake.segment_cur_value,
                                "own share shortfall at today's segment size")
    out = [RankedAction(l, *sizes.get(l.id, (None, ""))) for l in attr.levers]
    out.sort(key=lambda r: (r.size is None, -(r.size or 0.0)))
    return out


def top_action_answer(facts: Facts, attr: Attribution) -> Answer:
    """The single highest-impact action: the first of :func:`rank_actions`."""
    ranked = rank_actions(facts, attr)
    if not ranked:
        return Answer("top_action", None, ("No lever is open: nothing fired in this window.",))
    r = ranked[0]
    size = f" Recoverable: about {_n(r.size)} ({r.basis})." if r.size is not None else ""
    return Answer("top_action", r.lever.id, (f"{r.lever.title}. {r.lever.why}{size}",
                                             f"Checkpoint: {r.lever.checkpoint}."), {"size": r.size})


ANSWERS: dict[str, Callable[[Facts, Attribution], Answer]] = {
    "why": lambda f, a: focus_answer("offtake", f, a),
    "sov_invest": invest_verdict,
    "voice": lambda f, a: focus_answer("sov", f, a),
    "roas_move": roas_answer,
    "scale_cut": scale_cut_answer,
    "cut_waste": waste_answer,
    "grow_ms": growth_answer,
    "ms_decline": share_answer,
    "geo": geo_answer,
    "budget_mix": lambda f, a: mix_verdict(f),
    "availability": availability_answer,
    "top_action": top_action_answer,
    "pricing": lambda f, a: focus_answer("pricing", f, a),
    "competition": share_answer,
}


def answer(intent: str, facts: Facts, attr: Attribution) -> Answer:
    """Answer the question an intent stands for.  Unknown intents raise ``KeyError``."""
    return ANSWERS[intent](facts, attr)


# ---------------------------------------------------------------------------
# lever targeting
# ---------------------------------------------------------------------------


def intent_levers(intent: str, facts: Facts, attr: Attribution) -> list[Lever]:
    """Levers the question implies that no gate produced.

    | Intent(s) | Condition | Lever (axis) |
    |---|---|---|
    | sov_invest, voice | spend fell >= ``spend_cut_pct`` and the pool grew; no voice-floor lever yet | re-enter the growing pool (sov) |
    | sov_invest, voice | SOV minus market share < ``under_voiced_gap_pp``; no buy-voice lever yet | buy voice up to market share (sov) |
    | budget_mix | mix verdict NOT_OPTIMAL | shift the next unit of budget worst -> best (conversion) |
    | roas_move | ROAS < ``roas_fell_frac`` x prior and the mid-funnel rate fell | recover the conversion stage (conversion) |
    | geo, cut_waste, scale_cut | FIX bucket non-empty | close the own-vs-segment gap (geo) |
    | grow_ms | no pockets and a rival is ceding share | contest the share it is shedding (competition) |

    De-duplicated against the gate levers by lever id.  Returns levers with
    ``source="question"``.
    """
    t = facts.config
    have = {l.id.split(":")[0] for l in attr.levers}
    v, f = facts.voice, _funnel(facts)
    out: list[Lever] = []

    def add(lever_id: str, title: str, why: str, checkpoint: str, axis: str, blockers: tuple[str, ...]) -> None:
        if lever_id in have or any(b in have for b in blockers):
            return
        have.add(lever_id)
        out.append(Lever(lever_id, title, why, checkpoint, axis, "question"))

    if intent in ("sov_invest", "voice") and v is not None:
        sd = pct_move(f.spend_c, f.spend_p) if f else None
        pd = pct_move(v.pool_c, v.pool_p)
        if sd is not None and sd <= -t.spend_cut_pct and pd is not None and pd > 0:
            add("reenter_pool", "Re-enter the growing pool: budget back, voice floor set",
                f"Spend {_pc(sd)} while the pool grew {_pc(pd)}.",
                f"spend within {t.run_rate_tolerance_pct:.0f}% of the prior run-rate AND SOV not below today's",
                "sov", ("voice_floor", "reenter_voice"))
        if v.sov_c is not None and v.sov_c - attr.share_c < t.under_voiced_gap_pp:
            add("voice_to_share", "Buy voice up to your market share",
                f"Ad SOV {v.sov_c:.1f}% sits under a market share of {attr.share_c:.1f}%.",
                f"SOV >= market share within {t.voice_parity_weeks} weeks", "sov",
                ("buy_voice_target", "reenter_voice"))
    if intent == "budget_mix":
        m = mix_verdict(facts)
        if m.verdict == "NOT_OPTIMAL":
            add("shift_mix", f"Shift the next unit of budget: {m.data['worst']} -> {m.data['best']}",
                f"{m.data['best']} returns {m.data['best_roas']:.2f} against {m.data['worst_roas']:.2f}.",
                f"funded types' ROAS gap <= {100 * (t.mix_ratio - 1):.0f}% next window", "conversion", ())
    if intent == "roas_move" and f is not None and f.roas_c is not None and f.roas_p is not None:
        fell = f.mid_rate_c is not None and f.mid_rate_p is not None and f.mid_rate_c < f.mid_rate_p
        if f.roas_c < t.roas_fell_frac * f.roas_p and fell:
            add("recover_conversion", f"Recover the conversion stage: the {f.mid_label} is the broken step",
                "Fix content, price point and stock, not bids.",
                "rate back at the prior level within 2 weeks", "conversion", ())
    if intent in ("geo", "cut_waste", "scale_cut"):
        b = geo_buckets(facts)
        if b.fix:
            add("close_geo_gap", f"Close the own-vs-segment gap in {_names(b.fix)}",
                "The segment is alive there; the gap is on the brand side.",
                f"own delta within {t.geo_beat_pp:.0f}pp of the segment's in those geos", "geo", ())
    if intent == "grow_ms" and not attr.pockets:
        r = exposed_rival(facts)
        if r is not None:
            add("contest_shedding", f"Contest the share {r.brand} is shedding",
                f"{r.brand} lost {abs(r.d_pp):.1f}pp.",
                f"capture >= half the pp they shed at ROAS >= {facts.scope.roas_guard:.2f}", "competition", ())
    return out


@dataclass(frozen=True)
class LeverView:
    """Primary levers for the question, and titles of the other open levers ("also open")."""

    primary: tuple[Lever, ...] = ()
    also_open: tuple[str, ...] = ()


def target_levers(intent: str, facts: Facts, attr: Attribution) -> LeverView:
    """Build the lever view for an intent.

    For intents with an axis list: primary = question-derived levers + gate
    levers whose axis is on the list, sorted by the list's order (stable);
    every other gate lever collapses to a one-line "also open" title.
    Intents without axes (``why``, ``top_action``) show every gate lever.
    """
    axes = INTENTS[intent].axes
    if not axes:
        return LeverView(tuple(attr.levers), ())
    pool = intent_levers(intent, facts, attr) + [l for l in attr.levers if l.axis in axes]
    pool.sort(key=lambda l: axes.index(l.axis) if l.axis in axes else len(axes))
    return LeverView(tuple(pool), tuple(l.title for l in attr.levers if l.axis not in axes))


# ---------------------------------------------------------------------------
# verdict paragraph and sections
# ---------------------------------------------------------------------------


def verdict_paragraph(facts: Facts, attr: Attribution) -> str:
    """The headline paragraph: move, quadrant sentence, geo proof, mechanism, causes, what held.

    Rounded parts must add to the rounded total, so the displayed conversion
    effect is ``round(total) - round(visibility effect)`` rather than an
    independently rounded number.
    """
    t = facts.config
    d = attr.share_pp
    word = "rose" if d > t.share_word_band_pp else "fell" if d < -t.share_word_band_pp else "held"
    parts = [f"Own sales moved {_pc(attr.own_pct)} over {facts.scope.days} days while the segment moved "
             f"{_pc(attr.segment_pct)}; share {word} ({attr.share_p:.2f}% -> {attr.share_c:.2f}%).",
             attr.quadrant_read]
    if attr.quadrant.startswith("MARKET_TIDE"):
        if attr.geo_avoid:
            parts.append("Segment collapsed in " + ", ".join(f"{g} ({_pc(s, 0)})" for g, s in attr.geo_avoid[:3]) + ".")
        if attr.geo_spend:
            parts.append("The brand outperformed in " + ", ".join(
                f"{g} ({_pc(o, 0)} vs {_pc(s, 0)})" for g, o, s in attr.geo_spend[:3]) + ".")
    if attr.mech is not None:
        total, vis, con = rounded_parts(attr.mech.total, attr.mech.impr_effect)
        parts.append(f"The unit move ({total:+,.0f}) is {attr.mech.read}: {vis:+,.0f} from visibility, "
                     f"{con:+,.0f} from conversion.")
    named = list(dict.fromkeys(c.title for c in attr.causes if c.key != "SHARE_CEDED"))
    if named:
        parts.append(f"{len(named)} brand-specific cause(s): " + "; ".join(named) + ".")
    if len(attr.held) >= 2:
        parts.append("What held: " + "; ".join(attr.held[:3]) + ".")
    if attr.levers:
        parts.append(f"First action: {attr.levers[0].title}.")
    return " ".join(parts)


def _sec_shape(facts: Facts, attr: Attribution) -> Section:
    off = facts.offtake
    lines = [f"Own value {_n(off.pri_value)} -> {_n(off.cur_value)} ({_pc(attr.own_pct)}); units "
             f"{_n(off.pri_units)} -> {_n(off.cur_units)}."]
    step = step_scan(off.daily, facts.config)
    if step is not None:
        lines.append(f"A step, not a drift: the daily run-rate moved {_pc(step.pct)} on {step.date.isoformat()} "
                     f"({_n(step.before)} -> {_n(step.after)} per day).")
    else:
        lines.append("No dated step in the daily series: the move is a drift or too short to date.")
    return Section("shape", "Shape of the move", tuple(lines))


def _sec_geos(facts: Facts, attr: Attribution) -> Section:
    off = facts.offtake
    total = off.cur_value - off.pri_value
    lines = []
    for g in off.geo_rows:
        share = f"{100 * (g.cur_value - g.pri_value) / total:.0f}% of the move" if total else "n/a"
        tag = " (aggregate bucket)" if g.is_aggregate else ""
        lines.append(f"{g.geo}{tag}: own {_pc(g.own_pct)} vs segment {_pc(g.segment_pct)}: "
                     f"{geo_read(g, facts)}; {share}.")
    return Section("geos", "Geo by geo", tuple(lines) or ("No geo split in this feed.",))


def _sec_reads(facts: Facts, attr: Attribution, focus: str | None = None) -> Section:
    first = FOCUS_CAUSES.get(focus or "", ())
    ordered = sorted(attr.causes, key=lambda c: 0 if c.key in first else 1)
    lines = [f"{c.title}: {c.evidence}" for c in ordered] or ["No cause cleared its gate."]
    return Section("reads", "Confirmed causes", tuple(lines))


def _sec_ledger(facts: Facts, attr: Attribution) -> Section:
    lines = [f"Ruled out: {r.hypothesis}. {r.why}" for r in attr.ruled_out]
    lines += [f"Held: {h}." for h in attr.held]
    return Section("ledger", "Ruled out and what held", tuple(lines) or ("Nothing could be ruled out.",))


def _sec_funnel(facts: Facts, attr: Attribution) -> Section:
    f = _funnel(facts)
    if f is None:
        return Section("funnel", "Ads funnel", ("The ads ledger is stale or missing: this axis is unavailable.",))
    lines = [f"Spend {_n(f.spend_p)} -> {_n(f.spend_c)} ({_pc(pct_move(f.spend_c, f.spend_p))}); impressions "
             f"{_n(f.impr_p)} -> {_n(f.impr_c)}; attributed units {_n(f.units_p)} -> {_n(f.units_c)}.",
             "Direct ROAS " + ("n/a" if f.roas_p is None else f"{f.roas_p:.2f}") + " -> "
             + ("n/a" if f.roas_c is None else f"{f.roas_c:.2f}") + "."]
    return Section("funnel", "Ads funnel", tuple(lines))


def _sec_levers(facts: Facts, attr: Attribution, intent: str = "why") -> Section:
    view = target_levers(intent, facts, attr)
    lines = [f"{l.title} [{l.axis}]. {l.why} Checkpoint: {l.checkpoint}." for l in view.primary]
    if view.also_open:
        lines.append("Also open: " + "; ".join(view.also_open) + ".")
    return Section("levers", "Levers", tuple(lines) or ("No lever is open.",))


def _sec_invest(facts: Facts, attr: Attribution) -> Section:
    return Section("invest", "Voice versus investment", invest_verdict(facts, attr).lines + (
        "A brand defending or regaining share needs voice at or above its market share; voice far above share "
        "only pays while ROAS clears the guard.",))


def _sec_voice(facts: Facts, attr: Attribution) -> Section:
    v = facts.voice
    if v is None:
        return Section("voice", "Ad voice", ("Voice data is unavailable.",))
    lines = [f"{a.brand}: {_n(a.pri)} -> {_n(a.cur)} ad impressions ({_pc(pct_move(a.cur, a.pri))})."
             for a in v.advertisers]
    return Section("voice", "Ad voice on the battleground", tuple(lines) or ("No advertiser rows.",))


def _sec_geo_actions(facts: Facts, attr: Attribution) -> Section:
    return Section("geo_actions", "Geo actions", geo_answer(facts, attr).lines)


def _sec_growth(facts: Facts, attr: Attribution) -> Section:
    return Section("growth", "Growth paths", growth_answer(facts, attr).lines)


def _sec_availability(facts: Facts, attr: Attribution) -> Section:
    return Section("availability_detail", "Availability detail", availability_answer(facts, attr).lines)


def _sec_adtypes(facts: Facts, attr: Attribution) -> Section:
    return Section("adtypes", "Ad-type mix", mix_verdict(facts).lines)


def _sec_action_stack(facts: Facts, attr: Attribution) -> Section:
    ranked = rank_actions(facts, attr)
    lines = [f"{i + 1}. {r.lever.title}" + (f": about {_n(r.size)} ({r.basis})." if r.size is not None
                                            else ": not priced by the data.") for i, r in enumerate(ranked)]
    if sum(1 for r in ranked if r.size is not None) > 1:
        lines.append("The sized lines overlap: total recoverable is bounded by the share shortfall, not their sum.")
    return Section("action_stack", "Action stack", tuple(lines) or ("No lever is open.",))


def _sec_rivals(facts: Facts, attr: Attribution) -> Section:
    r = facts.rivals
    if r is None:
        return Section("rivals_detail", "Rivals", ("Rival data is unavailable.",))
    lines = [f"{m.brand}{' (own)' if m.is_own else ' (same house)' if m.is_sibling else ''}: "
             f"{m.share_p:.1f}% -> {m.share_c:.1f}% ({m.d_pp:+.1f}pp). {m.how}." for m in r.movers[:8]]
    return Section("rivals_detail", "Share movers", tuple(lines) or ("No brand rows.",))


def _sec_pricing(facts: Facts, attr: Attribution) -> Section:
    return Section("pricing_detail", "Pricing detail", focus_answer("pricing", facts, attr).lines)


SECTIONS: dict[str, Callable[..., Section]] = {
    "shape": _sec_shape, "geos": _sec_geos, "reads": _sec_reads, "ledger": _sec_ledger, "funnel": _sec_funnel,
    "levers": _sec_levers, "invest": _sec_invest, "voice": _sec_voice, "geo_actions": _sec_geo_actions,
    "growth": _sec_growth, "availability_detail": _sec_availability, "adtypes": _sec_adtypes,
    "action_stack": _sec_action_stack, "rivals_detail": _sec_rivals, "pricing_detail": _sec_pricing,
}


def build_sections(intent: str, facts: Facts, attr: Attribution) -> list[Section]:
    """Build the intent's sections in order.  Every section has a title and at least one line."""
    out = []
    for key in INTENTS[intent].sections:
        if key == "levers":
            out.append(_sec_levers(facts, attr, intent))
        elif key == "reads":
            out.append(_sec_reads(facts, attr, INTENT_FOCUS.get(intent)))
        else:
            out.append(SECTIONS[key](facts, attr))
    return out
