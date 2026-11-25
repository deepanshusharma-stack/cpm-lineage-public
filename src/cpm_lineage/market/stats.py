"""Market context: market share, segment growth, ad cost as a share of
total sales, the share / voice ratio, the saturation screen and the
per-product organic "flywheel" state.

Everything here needs optional feeds (market sell-out for all brands, the
impression-share feed).  Without them the functions return a result with
``reason`` set rather than raising.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Iterable, Mapping, Sequence

from ..schema import MarketRow, OfftakeRow, PerfRow, Window, WindowPair
from ._util import diff, in_window, pct, pct_growth, ratio
from .config import MarketConfig
from .sov import ad_sov as _ad_sov
from .sov import organic_share

__all__ = [
    "SegmentShare", "SaturationVerdict", "MarketStats", "FlywheelProduct", "FlywheelResult",
    "MS_SOV_CHAIN", "ms_sov_label", "saturation_verdict", "market_stats", "flywheel_state", "flywheel_states",
]

_MC = MarketConfig()

# Ordered conversion-barrier chain to walk when share lags voice.
MS_SOV_CHAIN: tuple[str, ...] = (
    "availability",
    "organic flywheel state",
    "keyword contamination",
    "pricing / off-platform demand",
)


@dataclass(frozen=True)
class SegmentShare:
    """Own share of one segment (or of the total), percent."""

    segment: str | None
    own_sales: float
    segment_sales: float
    share_pct: float | None
    share_prev_pct: float | None
    delta_pp: float | None
    own_growth_pct: float | None
    segment_growth_pct: float | None
    attributable_pct: float | None       # own growth minus segment growth (seasonality stripped)


def ms_sov_label(ratio_value: float | None, cfg: MarketConfig | None = None) -> str | None:
    """Label the market-share / ad-share-of-voice ratio.

    ``< 0.5`` over-investing (block scale, clean the ROAS gate first);
    ``< 0.7`` over-investing in ads; ``< 1.0`` mild under-conversion;
    ``<= 1.5`` healthy; above, highly efficient (may be under-investing).
    ``None`` for an unknown ratio.
    """
    cfg = cfg or _MC
    if ratio_value is None:
        return None
    if ratio_value < cfg.ms_sov_block:
        return "over-investing: block scale, clean the ROAS gate first"
    if ratio_value < cfg.ms_sov_over:
        return "over-investing in ads"
    if ratio_value < cfg.ms_sov_mild:
        return "mild under-conversion"
    if ratio_value <= cfg.ms_sov_healthy_max:
        return "healthy"
    return "highly efficient: may be under-investing"


@dataclass(frozen=True)
class SaturationVerdict:
    """Outcome of the three-period saturation screen."""

    code: str                 # see :func:`saturation_verdict`
    text: str
    declining: bool = False
    cost_rising: bool = False
    ratios: tuple[float | None, ...] = ()   # share / voice per period, oldest first
    organic_share: float | None = None


def saturation_verdict(
    share: Sequence[float | None],
    voice: Sequence[float | None],
    cost_share: Sequence[float | None] = (None, None, None),
    organic: float | None = None,
    cfg: MarketConfig | None = None,
) -> SaturationVerdict:
    """Screen three equal consecutive periods (oldest first) for ad saturation.

    ``r[k] = share[k] / voice[k]``; all three must exist.
    ``declining = r3 < r2 < r1 and r3 < saturation_net_decline x r1`` (both
    legs fall and the net move exceeds 5 %).  ``cost_rising = cost3 >
    saturation_cost_rise x cost2`` (ad cost as a share of total sales; both
    must be known).  ``organic`` is the own organic impression share in the
    newest period (0..1).

    Codes:

    * ``insufficient_data``: a ratio is missing.
    * ``not_saturating``: not declining.
    * declining and cost rising: ``measure_organic_first`` (organic
      unknown: do not cut blind), ``flywheel_cold`` (organic below
      ``saturation_cold_organic``: do not cut, fix conversion first),
      ``saturation_confirmed`` (organic above
      ``saturation_confirmed_organic``: cap own-brand bids, redirect
      spend), else ``test_reduction``.
    * declining only: ``efficiency_drift`` when the cost share is known
      (check competitor displacement first), else ``verify_spend_source``.
    """
    cfg = cfg or _MC
    r = tuple(ratio(share[k], voice[k]) if k < len(share) and k < len(voice) else None for k in range(3))
    if any(v is None for v in r):
        return SaturationVerdict("insufficient_data", "share or voice missing in one of the three periods",
                                 ratios=r, organic_share=organic)
    r1, r2, r3 = r  # type: ignore[misc]
    declining = r3 < r2 and r2 < r1 and r3 < cfg.saturation_net_decline * r1
    c2 = cost_share[1] if len(cost_share) > 1 else None
    c3 = cost_share[2] if len(cost_share) > 2 else None
    known = c2 is not None and c3 is not None
    rising = bool(known and c3 > cfg.saturation_cost_rise * c2)  # type: ignore[operator]
    if not declining:
        return SaturationVerdict("not_saturating", "share is keeping pace with voice", False, rising, r, organic)
    if rising:
        if organic is None:
            code, text = "measure_organic_first", "do not cut blind: measure the organic share first"
        elif organic < cfg.saturation_cold_organic:
            code, text = "flywheel_cold", "organic flywheel is cold: do not cut spend, fix conversion first"
        elif organic > cfg.saturation_confirmed_organic:
            code, text = "saturation_confirmed", "saturation confirmed: cap own-brand bids and redirect the spend"
        else:
            code, text = "test_reduction", (f"test a {cfg.saturation_test_cut} reduction on the lowest-ROAS keywords "
                                            f"and re-measure in {cfg.saturation_test_weeks} weeks")
    elif known:
        code, text = "efficiency_drift", ("efficiency drift, not over-spend saturation: "
                                          "check competitor displacement first")
    else:
        code, text = "verify_spend_source", "verify the spend source before calling saturation"
    return SaturationVerdict(code, text, True, rising, r, organic)


@dataclass(frozen=True)
class MarketStats:
    """Market context for the account over ``windows``."""

    days: int = 0
    segments: tuple[SegmentShare, ...] = ()
    total: SegmentShare | None = None
    ad_cost_share_pct: float | None = None   # 100 x own ad spend / own total sales
    ad_sov_pct: float | None = None
    price_index: float | None = None         # own revenue per unit / segment revenue per unit
    ms_sov: float | None = None
    ms_sov_label: str | None = None
    ms_sov_chain: tuple[str, ...] = ()
    saturation: SaturationVerdict | None = None
    reason: str | None = None

    @property
    def over_invested(self) -> bool:
        """True when share / voice is below the block line (dampens scale)."""
        return self.ms_sov is not None and self.ms_sov < _MC.ms_sov_block


def _share(rows: Sequence[OfftakeRow], windows: WindowPair, segment: str | None, all_segments: bool) -> SegmentShare:
    oc = op = cc = cp = 0.0
    for o in rows:
        if not all_segments and o.segment != segment:
            continue
        if in_window(o.date, windows.current):
            cc += o.sales or 0.0
            if o.is_own:
                oc += o.sales or 0.0
        elif in_window(o.date, windows.prior):
            cp += o.sales or 0.0
            if o.is_own:
                op += o.sales or 0.0
    ms, msp = pct(oc, cc), pct(op, cp)
    og, cg = pct_growth(oc, op), pct_growth(cc, cp)
    return SegmentShare(None if all_segments else segment, oc, cc, ms, msp, diff(ms, msp), og, cg, diff(og, cg))


def _own_spend(perf_rows: Iterable[PerfRow] | None, window: Window) -> float | None:
    if perf_rows is None:
        return None
    return sum(r.spend or 0.0 for r in perf_rows if in_window(r.date, window))


def market_stats(
    offtake_rows: Iterable[OfftakeRow] | None,
    windows: WindowPair,
    *,
    perf_rows: Iterable[PerfRow] | None = None,
    market_rows: Iterable[MarketRow] | None = None,
    cfg: MarketConfig | None = None,
) -> MarketStats:
    """Market share, growth, ad cost share, share / voice and saturation.

    Per segment and in total (the total uses the full denominator, not the
    listed segments): ``share = 100 x own / segment`` now and before,
    ``delta_pp``, own and segment growth, and ``attributable = own growth -
    segment growth``.  Top ``top_segments`` by own sales.

    * ``ad_cost_share = 100 x own ad spend / own total sales`` (needs ``perf_rows``).
    * ``ad_sov`` = own ad impressions / all ad impressions (needs ``market_rows``).
    * ``price_index`` = own revenue per unit / segment revenue per unit;
      below 1 the rivals sell larger or dearer packs, a product gap ads
      cannot close.
    * ``ms_sov = share_total / ad_sov`` with :func:`ms_sov_label`; below
      ``ms_sov_over`` the ordered conversion-barrier chain is attached.
    * ``saturation``: :func:`saturation_verdict` over three consecutive
      periods of ``windows.current.days`` ending at ``windows.current.end``.

    Returns :class:`MarketStats`; ``reason`` is set when the sell-out feed
    is missing or empty in the current window.
    """
    cfg = cfg or _MC
    rows = list(offtake_rows or ())
    cur = windows.current
    if not any(in_window(o.date, cur) and (o.sales or 0) > 0 for o in rows):
        return MarketStats(days=cur.days, reason="no market sell-out feed in the current window")
    perf = None if perf_rows is None else list(perf_rows)
    mkt = None if market_rows is None else list(market_rows)

    segs = sorted({o.segment for o in rows if o.segment is not None})
    shares = [_share(rows, windows, s, False) for s in segs]
    shares = [s for s in shares if s.segment_sales > 0]
    shares.sort(key=lambda s: (-s.own_sales, str(s.segment)))
    total = _share(rows, windows, None, True)

    spend = _own_spend(perf, cur)
    cost_share = pct(spend, total.own_sales) if spend is not None else None
    sov = _ad_sov(mkt, cur) if mkt is not None else None

    ou = cu = 0.0
    for o in rows:
        if in_window(o.date, cur):
            cu += o.units or 0.0
            if o.is_own:
                ou += o.units or 0.0
    own_rpu, cat_rpu = ratio(total.own_sales, ou), ratio(total.segment_sales, cu)
    price_index = ratio(own_rpu, cat_rpu)

    ms_sov = ratio(total.share_pct, sov)
    label = ms_sov_label(ms_sov, cfg)
    chain = MS_SOV_CHAIN if ms_sov is not None and ms_sov < cfg.ms_sov_over else ()

    # three equal consecutive periods, oldest first
    d = cur.days
    periods = [Window(cur.end - timedelta(days=d * (3 - k) + d - 1), cur.end - timedelta(days=d * (3 - k)))
               for k in (1, 2, 3)]
    ms3, sov3, cost3 = [], [], []
    for w in periods:
        own = sum(o.sales or 0.0 for o in rows if o.is_own and in_window(o.date, w))
        cat = sum(o.sales or 0.0 for o in rows if in_window(o.date, w))
        ms3.append(pct(own, cat))
        sov3.append(_ad_sov(mkt, w) if mkt is not None else None)
        sp = _own_spend(perf, w)
        cost3.append(pct(sp, own) if sp is not None and sp > 0 else None)
    og = organic_share(mkt, periods[2]) if mkt is not None else None
    sat = saturation_verdict(ms3, sov3, cost3, og, cfg)

    return MarketStats(days=d, segments=tuple(shares[: cfg.top_segments]), total=total,
                       ad_cost_share_pct=cost_share, ad_sov_pct=sov, price_index=price_index,
                       ms_sov=ms_sov, ms_sov_label=label, ms_sov_chain=chain, saturation=sat)


# --------------------------------------------------------------------------
# flywheel
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class FlywheelProduct:
    """Organic flywheel state of one own product."""

    product_id: str
    organic_impressions: float
    ad_impressions: float
    organic_share: float
    state: str
    spend: float | None = None       # ``None`` when no spend feed: use neutral wording
    roas: float | None = None

    @property
    def impressions(self) -> float:
        return self.organic_impressions + self.ad_impressions


@dataclass(frozen=True)
class FlywheelResult:
    """Per-product flywheel states (never average across products)."""

    n_products: int = 0
    distribution: Mapping[str, int] = field(default_factory=dict)
    mixed_state: bool = False
    note: str = ""
    products: tuple[FlywheelProduct, ...] = ()
    reason: str | None = None


def flywheel_state(organic_share_value: float, cfg: MarketConfig | None = None) -> str:
    """State from the organic impression share: ``< 0.20`` Cold, ``< 0.50``
    Warming, ``< 0.70`` Active, else Self-Sustaining."""
    cfg = cfg or _MC
    if organic_share_value < cfg.flywheel_cold:
        return "Cold"
    if organic_share_value < cfg.flywheel_warming:
        return "Warming"
    if organic_share_value < cfg.flywheel_active:
        return "Active"
    return "Self-Sustaining"


def flywheel_states(
    market_rows: Iterable[MarketRow] | None,
    window: Window | None = None,
    *,
    perf_rows: Iterable[PerfRow] | None = None,
    cfg: MarketConfig | None = None,
) -> FlywheelResult:
    """Per own product: how much of its search exposure is organic.

    ``organic_share = organic / (organic + ad)`` impressions over
    ``window`` for each own product (products with no impressions are
    skipped), mapped to a state by :func:`flywheel_state`.  Optional
    per-product ad spend and ROAS from ``perf_rows``.  Products are sorted
    by impressions; the "material" set is the smallest prefix covering
    ``flywheel_material_share`` of impressions, and ``mixed_state`` is true
    when that set holds more than one state.  State is per product, never
    an account average.  Needs a product-grain impression feed.
    """
    cfg = cfg or _MC
    acc: dict[str, list[float]] = {}
    for m in market_rows or ():
        if m.is_own and m.product_id and in_window(m.date, window):
            a = acc.setdefault(m.product_id, [0.0, 0.0])
            a[0] += m.organic_impressions or 0.0
            a[1] += m.ad_impressions or 0.0
    acc = {k: a for k, a in acc.items() if a[0] + a[1] > 0}
    if not acc:
        return FlywheelResult(reason="no product-grain own impression rows in the window")
    money: dict[str, list[float]] | None = None
    if perf_rows is not None:
        money = {}
        for r in perf_rows:
            if r.product_id and in_window(r.date, window):
                a = money.setdefault(r.product_id, [0.0, 0.0])
                a[0] += r.spend or 0.0
                a[1] += r.sales or 0.0
    prods: list[FlywheelProduct] = []
    for pid, (og, ad) in acc.items():
        sh = og / (og + ad)
        sp = roas = None
        if money is not None:
            mny = money.get(pid, [0.0, 0.0])
            sp, roas = mny[0], ratio(mny[1], mny[0])
        prods.append(FlywheelProduct(pid, og, ad, sh, flywheel_state(sh, cfg), sp, roas))
    prods.sort(key=lambda p: (-p.impressions, p.product_id))
    dist: dict[str, int] = {}
    for p in prods:
        dist[p.state] = dist.get(p.state, 0) + 1
    tot = sum(p.impressions for p in prods)
    cum, material = 0.0, []
    for p in prods:
        material.append(p)
        cum += p.impressions
        if cum >= cfg.flywheel_material_share * tot:
            break
    mixed = len({p.state for p in material}) > 1
    note = ("products sit in different flywheel states: decide per product, never on the account average"
            if mixed else "material products share one flywheel state")
    return FlywheelResult(len(prods), dist, mixed, note, tuple(prods[: cfg.flywheel_top_n]))
