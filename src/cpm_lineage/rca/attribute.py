"""Ordered threshold attribution: which causes fired, which were ruled out, which levers follow.

``attribute(facts)`` is a pure function of a :class:`~cpm_lineage.rca.facts.Facts`
bundle.  It makes no model inference: every judgment is a named threshold
in ``RcaConfig`` and the ORDER of the steps is the order of the cause list
(market quadrant first, then price, voice, availability, rivals, spend,
efficiency, product mix, pockets, share, internal mix, geo).

These are threshold attributions on window-over-window deltas, not causal
inference: there is no significance test and no control for co-occurring
causes, and the fixed evaluation order decides which cause leads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import AXES, RcaConfig
from .decompositions import two_factor_units
from .facts import Facts, pct_move
from .guards import lane_match

CAUSE_TITLES: dict[str, str] = {
    "PROMO_PULLBACK": "Promo depth pulled back",
    "STICKER_MIX": "Mixed list prices on shelf",
    "AD_PULLBACK": "Ad voice pulled back",
    "OUT_ESCALATED": "Out-escalated in the ad pool",
    "AVAILABILITY_DRAG": "Availability drag",
    "COMPETITOR_ABSORPTION": "A rival absorbed the share",
    "NEW_ENTRANT": "New entrant on the shelf",
    "AD_SPEND_CUT": "Ad spend was cut",
    "PASSENGERS": "Passenger products",
    "SHARE_CEDED": "Share ceded",
    "MIX_SHIFT": "Possible internal mix shift",
}

RULED_OUT_HYPOTHESES: tuple[str, ...] = (
    "Price / promo change", "Ad-visibility loss", "Availability shock", "New competitor entry",
    "Brand losing share", "Cannibalisation inside the brand",
)

QUADRANT_READS: dict[str, str] = {
    "MARKET_TIDE_OUTPERFORM": "The segment itself fell and the brand held or gained share: a market tide, "
                              "not a brand problem.",
    "MARKET_TIDE_PLUS_BRAND": "The segment fell and the brand lost share on top of it: a market tide plus a "
                              "brand-specific loss.",
    "SHARE_LOSS_IN_GROWTH": "The segment grew while the brand lost ground: the worst quadrant, a pure share "
                            "loss into rising demand.",
    "RIDING_GROWTH": "The segment grew and the brand grew with it.",
    "SHARE_LOSS_FLAT_MARKET": "The segment was flat and the brand ceded share inside it.",
    "STABLE": "Neither the segment nor the brand's share moved materially: second-order moves only.",
}


@dataclass(frozen=True)
class Cause:
    """A confirmed cause: a stable key, the evidence sentence and the numbers behind it."""

    key: str
    evidence: str
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def title(self) -> str:
        return CAUSE_TITLES.get(self.key, self.key)


@dataclass(frozen=True)
class RuledOut:
    hypothesis: str
    why: str


@dataclass(frozen=True)
class Lever:
    """A recommended action with its evidence, a measurable checkpoint and the axis it works on.

    ``id`` is a stable identifier (sizing and de-duplication key on it, not
    on title wording); ``source`` is ``"gate"`` for attribution levers and
    ``"question"`` for levers a question implied.
    """

    id: str
    title: str
    why: str
    checkpoint: str
    axis: str
    source: str = "gate"

    def as_tuple(self) -> tuple[str, str, str, str]:
        return (self.title, self.why, self.checkpoint, self.axis)


@dataclass(frozen=True)
class Mechanism:
    """D1 on the conversion facts: ``impr_effect + conv_effect == total`` exactly."""

    impr_effect: float
    conv_effect: float
    total: float
    read: str      # "conversion-led" | "visibility-led"


@dataclass(frozen=True)
class Pocket:
    keyword: str
    pool_growth_pct: float
    own_sov_pct: float
    pool_size: float


@dataclass(frozen=True)
class Attribution:
    """Everything ``attribute`` decided, in evaluation order."""

    quadrant: str
    quadrant_read: str
    own_pct: float
    segment_pct: float
    share_p: float
    share_c: float
    mech: Mechanism | None = None
    causes: tuple[Cause, ...] = ()
    ruled_out: tuple[RuledOut, ...] = ()
    levers: tuple[Lever, ...] = ()
    held: tuple[str, ...] = ()
    geo_spend: tuple[tuple[str, float, float], ...] = ()    # (geo, own_pct, segment_pct)
    geo_avoid: tuple[tuple[str, float], ...] = ()           # (geo, segment_pct)
    pockets: tuple[Pocket, ...] = ()
    passenger_ids: tuple[str, ...] = ()
    own_voice_pct: float | None = None                      # own battleground ad-impression move
    pool_voice_pct: float | None = None

    @property
    def cause_keys(self) -> tuple[str, ...]:
        return tuple(c.key for c in self.causes)

    def has(self, key: str) -> bool:
        return key in self.cause_keys

    def cause(self, key: str) -> Cause | None:
        return next((c for c in self.causes if c.key == key), None)

    def lever(self, lever_id: str) -> Lever | None:
        return next((l for l in self.levers if l.id == lever_id), None)

    def is_ruled_out(self, hypothesis: str) -> bool:
        return any(r.hypothesis == hypothesis for r in self.ruled_out)

    @property
    def share_pp(self) -> float:
        return self.share_c - self.share_p


def quadrant(own_pct: float, segment_pct: float, share_pp: float, config: RcaConfig | None = None) -> tuple[str, str]:
    """Decide whether the move is the market or the brand.

    ::

        segment_pct <= -segment_move_pct:
            MARKET_TIDE_OUTPERFORM   if share_pp >= -share_move_pp
            MARKET_TIDE_PLUS_BRAND   otherwise
        segment_pct >= +segment_move_pct:
            SHARE_LOSS_IN_GROWTH     if own_pct < 0 or share_pp <= -share_move_pp
            RIDING_GROWTH            otherwise
        share_pp <= -share_move_pp:  SHARE_LOSS_FLAT_MARKET
        else:                        STABLE

    The six classes are mutually exclusive and cover every input.  Returns
    ``(key, one-sentence read)``.
    """
    c = config or RcaConfig()
    if segment_pct <= -c.segment_move_pct:
        key = "MARKET_TIDE_OUTPERFORM" if share_pp >= -c.share_move_pp else "MARKET_TIDE_PLUS_BRAND"
    elif segment_pct >= c.segment_move_pct:
        key = "SHARE_LOSS_IN_GROWTH" if (own_pct < 0 or share_pp <= -c.share_move_pp) else "RIDING_GROWTH"
    elif share_pp <= -c.share_move_pp:
        key = "SHARE_LOSS_FLAT_MARKET"
    else:
        key = "STABLE"
    return key, QUADRANT_READS[key]


def _pc(x: float | None, digits: int = 1) -> str:
    return "n/a" if x is None else f"{x:+.{digits}f}%"


def _av(x: float | None) -> str:
    return "n/a" if x is None else f"{100.0 * x:.0f}%"


def _money(x: float | None) -> str:
    return "n/a" if x is None else f"{x:,.0f}"


def attribute(facts: Facts) -> Attribution:
    """Attribute a sales / share move to causes, in a fixed order, with thresholds only.

    Steps (each is silent, neither fired nor ruled out, when its fact is
    missing):

    0.  Headline: own and segment value moves (%), share before / after.
    1.  Quadrant (:func:`quadrant`).
    2.  Mechanism: D1 on the conversion facts when both windows have
        impressions.
    3.  Promo: depth fell by >= ``promo_depth_pp`` -> ``PROMO_PULLBACK``;
        ``|change| < promo_depth_pp`` -> ruled out.  A deepening beyond the
        gate is neither.
    4.  ``STICKER_MIX`` per product with two or more list-price points.
    5.  Ad voice: SOV fell by >= ``sov_drop_pp`` and own ad impressions fell
        AND trail the pool by >= ``pullback_gap_pp`` -> ``AD_PULLBACK``;
        SOV fell otherwise -> ``OUT_ESCALATED`` (a falling share with rising
        own volume can only be dilution); else ruled out.
    6.  ``AVAILABILITY_DRAG``: availability under the floor, or fell by >=
        ``availability_drop``, or any geo under the floor; else ruled out.
    7.  Rivals: non-own, non-sibling brands that gained >= ``mover_pp`` ->
        ``COMPETITOR_ABSORPTION`` naming the largest; a sibling gainer is a
        portfolio shift noted under "held"; launches -> ``NEW_ENTRANT``,
        else ruled out (only when rival data exists).
    8.  ``AD_SPEND_CUT``: spend fell by >= ``spend_cut_pct``; the lever
        depends on whether own voice held.
    9.  Efficiency guard lever when direct ROAS is under the guard.
    10. Products: a growing product under ``floor - gap`` availability gets
        an unblock lever and is never a passenger; passengers (zero sales
        or availability under ``floor - gap``) are a cause only while the
        hero product is healthy.
    11. Keyword pockets: pool grew >= ``pocket_growth_pct`` with own SOV
        under ``pocket_own_sov``.
    12. Rival vacuum: an advertiser with >= ``vacuum_min_prior_sov_pct`` that
        fell to <= ``vacuum_drop`` of its prior volume.
    13. Share: held -> ruled out; lost -> folded into the absorption cause
        when one fired, else ``SHARE_CEDED`` is inserted first.
    14. ``MIX_SHIFT`` when own value fell and another own line grew by >=
        ``mix_shift_loss_fraction`` of the loss; else ruled out.
    15. Geo: segment collapse is tested before outperformance, so relative
        strength inside a collapsing geo is never a reason to spend.
    16. What held: gates that did not fire.

    Design notes: the own
    brand is excluded from the vacuum scan (``vacuum_exclude_own``); the
    passenger checkpoint uses ``availability_floor`` rather than a fixed
    number; "new competitor entry" is not listed as ruled out when there is
    no rival data at all; and the mechanism is returned on the result rather
    than written back into the facts, so the input is never mutated.

    Returns an :class:`Attribution`.
    """
    scope = facts.scope
    t = scope.config
    be = scope.breakeven
    guard = scope.roas_guard
    off = facts.offtake
    causes: list[Cause] = []
    ruled: list[RuledOut] = []
    levers: list[Lever] = []
    held: list[str] = []

    # 0 headline -------------------------------------------------------------
    own_pct = pct_move(off.cur_value, off.pri_value) or 0.0
    seg_pct = pct_move(off.segment_cur_value, off.segment_pri_value) or 0.0
    share_p = 100.0 * off.pri_value / off.segment_pri_value if off.segment_pri_value else 0.0
    share_c = 100.0 * off.cur_value / off.segment_cur_value if off.segment_cur_value else 0.0
    d_share = round(share_c - share_p, 9)

    # 1 quadrant -------------------------------------------------------------
    quad, quad_read = quadrant(own_pct, seg_pct, d_share, t)

    # 2 mechanism ------------------------------------------------------------
    mech = None
    conv = facts.conversion
    if conv is not None and conv.impr_p and conv.impr_c:
        tf = two_factor_units(conv.impr_p, conv.conv_p, conv.impr_c, conv.conv_c)
        mech = Mechanism(tf.volume_effect, tf.rate_effect, tf.total, tf.mechanism)

    # 3 promo ----------------------------------------------------------------
    pricing = facts.pricing
    dd = None
    if pricing is not None and pricing.depth_c is not None and pricing.depth_p is not None:
        dd = round(pricing.depth_c - pricing.depth_p, 9)
        if dd <= -t.promo_depth_pp:
            seg_note = ""
            if pricing.segment_depth_c is not None and pricing.segment_depth_p is not None:
                seg_note = (f"; the segment moved {pricing.segment_depth_p:.1f}% -> "
                            f"{pricing.segment_depth_c:.1f}%")
            causes.append(Cause("PROMO_PULLBACK",
                                f"Own discount depth fell {pricing.depth_p:.1f}% -> {pricing.depth_c:.1f}% "
                                f"({dd:+.1f}pp){seg_note}.", {"depth_delta_pp": dd}))
            levers.append(Lever("restore_promo", "Restore promo depth toward the prior window's level",
                                f"Depth was pulled back {abs(dd):.1f}pp.",
                                "weekly units back at the prior run-rate within 2 weeks of the depth restore",
                                "pricing"))
        elif abs(dd) < t.promo_depth_pp:
            ruled.append(RuledOut("Price / promo change", f"Discount depth moved only {dd:+.1f}pp."))

    # 4 sticker mix ----------------------------------------------------------
    if pricing is not None:
        for pid, points in pricing.sticker_mix.items():
            desc = ", ".join(f"{_money(price)} on {100 * s:.0f}% of observations" for price, s in points)
            causes.append(Cause("STICKER_MIX", f"{pid} shows mixed list prices: {desc}.",
                                {"product_id": pid, "points": points}))
            levers.append(Lever(f"sticker_migration:{pid}", f"Finish the list-price migration on {pid}",
                                "Two price points on one product split the conversion read.",
                                f"single modal list price >= {100 * t.sticker_checkpoint_share:.0f}% of observations",
                                "pricing"))

    # 5 ad voice -------------------------------------------------------------
    voice = facts.voice
    own_d = pool_d = None
    sov_p = sov_c = None
    if voice is not None and voice.own_p > 0 and voice.pool_p > 0 and voice.pool_c > 0:
        own_d = pct_move(voice.own_c, voice.own_p)
        pool_d = pct_move(voice.pool_c, voice.pool_p)
        sov_p, sov_c = voice.sov_p, voice.sov_c
        if round(sov_p - sov_c, 9) >= t.sov_drop_pp:
            data = {"own_pct": own_d, "pool_pct": pool_d, "sov_p": sov_p, "sov_c": sov_c}
            if own_d < 0 and own_d <= pool_d - t.pullback_gap_pp:
                causes.append(Cause("AD_PULLBACK",
                                    f"Own ad impressions on the battleground moved {_pc(own_d)} while the pool "
                                    f"moved {_pc(pool_d)}; ad SOV {sov_p:.1f}% -> {sov_c:.1f}%. Own volume fell "
                                    "and trailed the pool: a genuine pullback.", data))
                levers.append(Lever("reenter_voice",
                                    "Re-enter generic ad voice: bid to an SOV floor, not a currency cap",
                                    f"Own volume {_pc(own_d)} against a pool at {_pc(pool_d)}.",
                                    f"battleground ad SOV back >= {sov_p:.1f}% with direct ROAS >= {be:.1f}",
                                    "sov"))
            else:
                causes.append(Cause("OUT_ESCALATED",
                                    f"Own ad impressions moved {_pc(own_d)} but the pool moved {_pc(pool_d)}; "
                                    f"ad SOV {sov_p:.1f}% -> {sov_c:.1f}%. The share fell by dilution, not by "
                                    "a pullback.", data))
                levers.append(Lever("buy_voice_target",
                                    "Buy voice to a share target: the pool outgrew the budget",
                                    f"The pool grew {_pc(pool_d)} against own {_pc(own_d)}.",
                                    f"battleground ad SOV >= {sov_p:.1f}% sustained for 2 weeks", "sov"))
        else:
            ruled.append(RuledOut("Ad-visibility loss",
                                  f"Battleground ad SOV {sov_p:.1f}% -> {sov_c:.1f}%, inside the "
                                  f"{t.sov_drop_pp:.1f}pp gate."))

    # 6 availability ---------------------------------------------------------
    av = facts.availability
    d_av = None
    if av is not None and av.own_c is not None:
        d_av = round(av.own_c - (av.own_p if av.own_p is not None else av.own_c), 9)
        if av.own_c < t.availability_floor or d_av <= -t.availability_drop or av.geo_flags:
            parts = [f"Own availability {_av(av.own_p)} -> {_av(av.own_c)}"]
            if av.segment_c is not None:
                parts.append(f"segment benchmark {_av(av.segment_c)}")
            if av.geo_flags:
                parts.append("under the floor in " + ", ".join(f"{g} ({_av(a)})" for g, a in av.geo_flags[:4]))
            if av.loss_c:
                parts.append(f"estimated sales lost {_money(av.loss_c)}")
            causes.append(Cause("AVAILABILITY_DRAG", "; ".join(parts) + ".",
                                {"delta": d_av, "geos": tuple(g for g, _ in av.geo_flags)}))
            levers.append(Lever("supply_triage", "Supply triage in the breach geos",
                                "Fix supply before buying demand there: ad money is hitting empty shelves.",
                                f"all named geos >= {_av(t.availability_floor)} availability in 4 weeks", "availability"))
        else:
            ruled.append(RuledOut("Availability shock",
                                  f"Own availability {_av(av.own_p)} -> {_av(av.own_c)}, above the "
                                  f"{_av(t.availability_floor)} floor with no geo breach."))

    # 7 rivals ---------------------------------------------------------------
    rivals = facts.rivals
    if rivals is not None:
        movers = [m for m in rivals.movers if abs(m.d_pp) >= t.mover_pp]
        gainers = [m for m in movers if m.d_pp > 0 and not m.is_own and not m.is_sibling]
        sibling = next((m for m in movers if m.is_sibling and m.d_pp >= t.mover_pp), None)
        if sibling is not None:
            held.append(f"{sibling.brand} gained {sibling.d_pp:+.1f}pp: a same-house portfolio shift, not a "
                        "competitive loss; coordinate, don't defend")
        if gainers:
            g = gainers[0]
            lane = ""
            if lane_match(g.lane, rivals.own_lane):
                lane = f" It competes in the same lane ({g.lane})."
            causes.append(Cause("COMPETITOR_ABSORPTION",
                                f"{g.brand} gained {g.d_pp:+.1f}pp of segment share "
                                f"({g.share_p:.1f}% -> {g.share_c:.1f}%). How it wins: {g.how}.{lane} "
                                f"{len(gainers)} brand(s) gained at least {t.mover_pp:.1f}pp.",
                                {"brand": g.brand, "d_pp": g.d_pp, "gainers": len(gainers), "lane": g.lane}))
            how_note = ("defend on availability, voice and price together" if "balanced" in g.how.lower()
                        else "match the winning lever, not a different one")
            levers.append(Lever("defend_lane", f"Defend the lane {g.brand} is taking",
                                f"{g.how}: {how_note}.",
                                f"{g.brand} held under +{t.mover_pp:.1f}pp next window", "competition"))
        if rivals.launches:
            names = ", ".join(f"{l.brand} {l.title} ({l.share_pct:.1f}%)" for l in rivals.launches[:3])
            causes.append(Cause("NEW_ENTRANT", f"{len(rivals.launches)} recent launch(es) at material share: "
                                               f"{names}.", {"count": len(rivals.launches)}))
        else:
            ruled.append(RuledOut("New competitor entry",
                                  f"No launch in the last {t.launch_lookback_days} days reached "
                                  f"{t.launch_share_pct:.1f}% of the segment."))

    # 8 spend cut ------------------------------------------------------------
    funnel = facts.funnel if facts.funnel is not None and not facts.funnel.stale else None
    sd = None
    if funnel is not None and funnel.spend_p > 0:
        sd = pct_move(funnel.spend_c, funnel.spend_p)
        if sd <= -t.spend_cut_pct:
            if own_d is not None and own_d >= 0:
                causes.append(Cause("AD_SPEND_CUT",
                                    f"Ad spend {_pc(sd)}; the cut rode falling CPMs and has not cost voice so "
                                    "far: the risk is forward, not booked.", {"spend_pct": sd, "voice_held": True}))
                levers.append(Lever("voice_floor", "Bank the CPM tailwind, but set a voice floor",
                                    f"Spend {_pc(sd)} with own ad impressions {_pc(own_d)}.",
                                    "battleground ad SOV held at or above its current level, checked weekly; "
                                    "re-add budget the week it slips", "sov"))
            else:
                causes.append(Cause("AD_SPEND_CUT", f"Ad spend {_pc(sd)}: a budget decision, not the market.",
                                    {"spend_pct": sd, "voice_held": False}))
                levers.append(Lever("restore_budget",
                                    "Restore the product-level ad budget (or own the cut as deliberate)",
                                    f"Spend {_pc(sd)} window over window.",
                                    "spend back at the prior run-rate, or the cut recorded as an intentional call",
                                    "sov"))

    # 9 efficiency guard -----------------------------------------------------
    if funnel is not None and funnel.roas_c is not None and funnel.roas_c < guard:
        levers.append(Lever("efficiency_guard", "Efficiency guard: direct ROAS is near water level",
                            f"Direct ROAS {funnel.roas_c:.2f} against a guard of {guard:.2f}; cap the worst "
                            "cohort at breakeven while the levers above land.",
                            f"direct ROAS >= {be:.1f} weekly", "conversion"))

    # 10 passengers / scaling unblock ----------------------------------------
    passenger_ids: list[str] = []
    if av is not None and av.product_rows:
        low = t.availability_floor - t.passenger_availability_gap
        hero, rest = av.product_rows[0], av.product_rows[1:]
        growing = [r for r in rest if r.units_c > max(r.units_p, 0.0)
                   and r.availability_c is not None and r.availability_c < low]
        grow_ids = {r.product_id for r in growing}
        passengers = [r for r in rest if r.product_id not in grow_ids and (
            r.units_c + r.units_p == 0 or (r.availability_c is not None and r.availability_c < low))]
        passenger_ids = [r.product_id for r in passengers]
        for r in growing:
            g = pct_move(r.units_c, r.units_p)
            if g is None:
                shown = "new"
            elif g > t.growth_multiple_pct:
                shown = f"x{r.units_c / r.units_p:.1f}"
            else:
                shown = f"{g:+.0f}%"
            levers.append(Lever(f"unblock_scaling:{r.product_id}",
                                f"Unblock the scaling product {r.product_id}: demand outruns its distribution",
                                f"Units {shown} at {_av(r.availability_c)} availability.",
                                f"{r.product_id} >= {_av(t.availability_floor)} availability in 2 weeks", "availability"))
        if passengers and hero.availability_c is not None and hero.availability_c >= t.availability_floor:
            shown = ", ".join(f"{r.product_id} ({_av(r.availability_c)}, {r.units_c:,.0f} units)"
                              for r in passengers[:4])
            more = f" +{len(passengers) - 4} more" if len(passengers) > 4 else ""
            causes.append(Cause("PASSENGERS",
                                f"Hero {hero.product_id} is healthy ({_av(hero.availability_c)}, "
                                f"{hero.units_c:,.0f} units) while these ride along: {shown}{more}.",
                                {"hero": hero.product_id, "passengers": tuple(passenger_ids)}))
            levers.append(Lever("passengers", "Fix or prune the passenger products",
                                "They hold shelf and budget without carrying demand.",
                                f"each passenger either >= {_av(t.availability_floor)} availability or a formal "
                                "keep / kill decision in 4 weeks", "availability"))

    # 11 keyword pockets -----------------------------------------------------
    pockets: list[Pocket] = []
    if voice is not None and voice.keywords:
        listed = sum(k.pool_c for k in voice.keywords)
        for k in voice.keywords:
            if voice.relevant is not None and voice.relevant.get(k.keyword) is False:
                continue
            if k.pool_p <= 0 or k.pool_c < t.pocket_min_pool_share * listed or k.pool_c <= 0:
                continue
            gr = pct_move(k.pool_c, k.pool_p)
            own_sov = 100.0 * k.own_c / k.pool_c
            if gr >= t.pocket_growth_pct and own_sov < t.pocket_own_sov:
                pockets.append(Pocket(k.keyword, gr, own_sov, k.pool_c))
        pockets.sort(key=lambda p: (-p.pool_size, p.keyword))
        if pockets:
            top = ", ".join(f"{p.keyword} (pool {_pc(p.pool_growth_pct, 0)}, own SOV {p.own_sov_pct:.1f}%)"
                            for p in pockets[:3])
            screened = "" if voice.relevant is not None else " (unscreened: some may belong to adjacent own lines)"
            levers.append(Lever("pockets", "Work the unclaimed demand pockets", f"{top}{screened}.",
                                "measurable own impressions on the pocket terms within a week; incremental "
                                "sales >= breakeven by week 2", "sov"))

    # 12 rival vacuum --------------------------------------------------------
    if voice is not None and voice.pool_p > 0:
        for a in voice.advertisers:
            if t.vacuum_exclude_own and a.is_own:
                continue
            pri_sov = 100.0 * a.pri / voice.pool_p
            if pri_sov >= t.vacuum_min_prior_sov_pct and a.pri > 0 and a.cur <= t.vacuum_drop * a.pri:
                levers.append(Lever("contest_vacuum", f"Contest the voice {a.brand} just vacated",
                                    f"{a.brand} held {pri_sov:.1f}% of battleground voice and fell to "
                                    f"{100.0 * a.cur / a.pri:.0f}% of its prior volume.",
                                    f"own battleground SOV up >= {t.vacuum_gain_pp:.0f}pp with direct ROAS >= "
                                    f"{be:.1f}", "sov"))
                break

    # 13 share ---------------------------------------------------------------
    if d_share >= -t.share_move_pp:
        named = [g for g in off.geo_rows if not g.is_aggregate][: t.share_geo_top_n]
        beat = sum(1 for g in named if g.own_pct is not None and g.segment_pct is not None
                   and g.own_pct > g.segment_pct)
        proof = f"; beat the segment in {beat} of the top {len(named)} geos" if named else ""
        ruled.append(RuledOut("Brand losing share", f"Share {share_p:.2f}% -> {share_c:.2f}% "
                                                    f"({d_share:+.2f}pp){proof}."))
    else:
        fold = f"Own share fell {share_p:.2f}% -> {share_c:.2f}% ({d_share:+.2f}pp)."
        idx = next((i for i, c in enumerate(causes) if c.key == "COMPETITOR_ABSORPTION"), None)
        if idx is not None:
            c = causes[idx]
            causes[idx] = Cause(c.key, f"{c.evidence} {fold}", {**c.data, "share_pp": d_share})
        else:
            causes.insert(0, Cause("SHARE_CEDED", fold, {"share_pp": d_share}))

    # 14 mix shift -----------------------------------------------------------
    if own_pct < 0 and rivals is not None and rivals.own_other_lines:
        loss = abs(off.pri_value - off.cur_value)
        grew = [l for l in rivals.own_other_lines if l.cur - l.pri >= t.mix_shift_loss_fraction * loss]
        if grew and loss > 0:
            l = max(grew, key=lambda x: x.cur - x.pri)
            causes.append(Cause("MIX_SHIFT",
                                f"{l.segment} grew by {_money(l.cur - l.pri)} while this line lost "
                                f"{_money(loss)}: a possible internal mix shift; verify with basket data.",
                                {"segment": l.segment}))
        else:
            ruled.append(RuledOut("Cannibalisation inside the brand",
                                  "No other own line grew by at least "
                                  f"{100 * t.mix_shift_loss_fraction:.0f}% of the loss."))

    # 15 geo -----------------------------------------------------------------
    geo_spend: list[tuple[str, float, float]] = []
    geo_avoid: list[tuple[str, float]] = []
    for g in [g for g in off.geo_rows if not g.is_aggregate][: t.geo_top_n]:
        if g.segment_pct is None:
            continue
        if g.segment_pct <= t.geo_collapse_pct:
            geo_avoid.append((g.geo, g.segment_pct))
        elif g.own_pct is not None and g.own_pct > g.segment_pct + t.geo_beat_pp:
            geo_spend.append((g.geo, g.own_pct, g.segment_pct))
    if quad.startswith("MARKET_TIDE") and (geo_spend or geo_avoid):
        why = []
        if geo_spend:
            why.append("outperforming a living segment in " + ", ".join(g for g, _, _ in geo_spend[:4]))
        if geo_avoid:
            why.append("segment collapsed in " + ", ".join(g for g, _ in geo_avoid[:4]))
        levers.append(Lever("geo_rebalance", "Spend where the season still lives, and stop where it doesn't",
                            "; ".join(why) + ".",
                            "media share by geo rebalanced next window; no bid escalation in the collapse geos "
                            "until their segment stabilises", "geo"))

    # 16 what held -----------------------------------------------------------
    if dd is not None and abs(dd) < t.promo_depth_pp:
        held.append("discount depth stable")
    if pricing is not None and not pricing.sticker_mix:
        held.append("one list price per product")
    if conv is not None and conv.conv_p > 0 and abs(conv.conv_c / conv.conv_p - 1.0) < t.conversion_steady_frac:
        held.append("conversion steady")
    if conv is not None and conv.org_p and conv.org_c is not None and conv.org_c >= t.organic_held_frac * conv.org_p:
        held.append("organic impressions held")
    if av is not None and av.own_c is not None and av.own_c >= t.availability_floor \
            and (d_av is None or d_av > -t.availability_drop):
        held.append("availability healthy")

    assert all(l.axis in AXES for l in levers)
    return Attribution(
        quadrant=quad, quadrant_read=quad_read, own_pct=own_pct, segment_pct=seg_pct,
        share_p=share_p, share_c=share_c, mech=mech, causes=tuple(causes), ruled_out=tuple(ruled),
        levers=tuple(levers), held=tuple(held), geo_spend=tuple(geo_spend), geo_avoid=tuple(geo_avoid),
        pockets=tuple(pockets), passenger_ids=tuple(passenger_ids), own_voice_pct=own_d, pool_voice_pct=pool_d,
    )
