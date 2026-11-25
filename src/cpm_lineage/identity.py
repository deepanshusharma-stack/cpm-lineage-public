"""The CPM ROAS identity and its exact, model-free decompositions.

On a platform billed per thousand impressions the click funnel may be
unobservable, so return on ad spend is written with three channels that
need only four additive sums (spend, sales, impressions, units)::

    CPM   = 1000 * spend / impressions      cost per thousand impressions
    ICR1k = 1000 * units / impressions      conversions per thousand impressions
    ASP   = sales / units                   average selling price
    ROAS  = sales / spend = ICR1k * ASP / CPM

Taking logs gives an exact three-term split of any ROAS move (layer 1)::

    d ln ROAS = d ln ICR + d ln ASP - d ln CPM          residual = 0

This module holds that split, a rule-based first-pass verdict on a ROAS
drop, and the *prior-rate* two-factor decompositions (volume delta priced
at the prior rate, rate delta at the current volume).  The *midpoint*
shift-share family lives in :mod:`cpm_lineage.decompose`; the two
conventions are both exact and must never be mixed inside one identity.

Everything here is a pure function over plain records: no I/O, no model.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable, Mapping, Sequence

from .config import Economics
from .metrics import cell_from_rows
from .schema import MetricCell, PerfRow, WindowPair
from .windows import split_rows

__all__ = [
    "CHANNELS",
    "CHANNEL_SIGNS",
    "ChannelEffect",
    "IdentityConfig",
    "Layer1Result",
    "Layer1Verdict",
    "RoasMove",
    "TwoFactor",
    "WindowAggregate",
    "decompose_rows",
    "layer1_verdict",
    "log_decomposition",
    "quartile_buckets",
    "roas_move",
    "rounded_parts",
    "two_factor",
    "unit_decomposition",
    "value_decomposition",
    "window_aggregate",
]

#: Channel -> sign with which its log change enters ``d ln ROAS``.
CHANNEL_SIGNS: dict[str, int] = {"icr1k": +1, "asp": +1, "cpm": -1}
#: Channel names in reporting order.
CHANNELS: tuple[str, ...] = tuple(CHANNEL_SIGNS)


@dataclass(frozen=True)
class IdentityConfig:
    """Thresholds of the identity layer (every field has a default)."""

    log_floor: float = 1e-9            # channel values are floored here before ln, so ln(0) never occurs
    min_dln: float = 1e-4              # below this |d ln ROAS| the share denominator falls back (model total, then this)
    share_digits: int | None = None    # optional rounding of share-of-move percentages (None = unrounded)
    verdict_bid_flat_pct: float = 7.0  # |bid change| under this % counts as "bid flat" in the layer-1 verdict
    verdict_min_spend: float = 500.0   # reference money: comparison-window spend needed before a verdict is given
    spend_cut_note_pct: float = -15.0  # spend change at or below this % adds the "separate decisions" note
    quartile_min_days: int = 4         # the good-days/bad-days bucket needs at least one day per quartile


_DEFAULT = IdentityConfig()


# ---------------------------------------------------------------------------
# window aggregates
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WindowAggregate:
    """Window sums and the ratios derived from them (0.0 on a zero denominator).

    The zero convention (rather than ``None``) is deliberate at this layer:
    the log decomposition floors every channel at ``IdentityConfig.log_floor``.
    ``bid`` is the spend-weighted mean bid, or ``None`` when the platform
    does not report bids.
    """

    spend: float = 0.0
    sales: float = 0.0
    impressions: float = 0.0
    units: float = 0.0
    roas: float = 0.0
    icr1k: float = 0.0
    asp: float = 0.0
    cpm: float = 0.0
    bid: float | None = None

    def channel(self, name: str) -> float:
        """Value of channel ``"icr1k"``, ``"asp"`` or ``"cpm"``."""
        if name not in CHANNEL_SIGNS:
            raise KeyError(name)
        return float(getattr(self, name))

    @property
    def rpm(self) -> float:
        """Attributed revenue per thousand impressions (``ICR1k * ASP``)."""
        return 1000.0 * self.sales / self.impressions if self.impressions else 0.0

    @property
    def all_positive(self) -> bool:
        """True when all four sums are positive, i.e. the log identity is exact."""
        return self.spend > 0 and self.sales > 0 and self.impressions > 0 and self.units > 0

    def as_dict(self) -> dict[str, float | None]:
        return {
            "spend": self.spend, "sales": self.sales, "impressions": self.impressions,
            "units": self.units, "roas": self.roas, "icr1k": self.icr1k, "asp": self.asp,
            "cpm": self.cpm, "bid": self.bid,
        }


def _num(x: Any) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    return v if math.isfinite(v) else 0.0


def window_aggregate(source: MetricCell | WindowAggregate | Mapping[str, Any] | Iterable[PerfRow]) -> WindowAggregate:
    """Collapse one window to sums and identity ratios.

    Formula: ``roas = sales/spend``, ``icr1k = 1000*units/impressions``,
    ``asp = sales/units``, ``cpm = 1000*spend/impressions``; any zero
    denominator yields 0.0.  Ratios always come from window *sums*: a mean
    of daily ratios would let a tiny-spend day count like a large one and
    break exactness.

    ``source`` may be a ``MetricCell``, a mapping with ``spend, sales,
    impressions, units`` (optional ``bid``), an iterable of ``PerfRow`` or an
    existing ``WindowAggregate`` (returned unchanged).  Pass every spend
    day of the window, including days without a sale: filtering to
    converting days overstates ROAS and ICR.
    """
    if isinstance(source, WindowAggregate):
        return source
    bid: float | None
    if isinstance(source, MetricCell):
        sp, sa, im, un, bid = source.spend, source.sales, source.impressions, source.units, source.bid
    elif isinstance(source, Mapping):
        sp, sa, im, un = (_num(source.get(k)) for k in ("spend", "sales", "impressions", "units"))
        raw_bid = source.get("bid")
        bid = _num(raw_bid) if raw_bid is not None else None
    else:
        cell = cell_from_rows(source)
        sp, sa, im, un, bid = cell.spend, cell.sales, cell.impressions, cell.units, cell.bid
    sp, sa, im, un = _num(sp), _num(sa), _num(im), _num(un)
    return WindowAggregate(
        spend=sp, sales=sa, impressions=im, units=un,
        roas=sa / sp if sp else 0.0,
        icr1k=1000.0 * un / im if im else 0.0,
        asp=sa / un if un else 0.0,
        cpm=1000.0 * sp / im if im else 0.0,
        bid=bid,
    )


# ---------------------------------------------------------------------------
# layer 1: exact log decomposition
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChannelEffect:
    """One channel's exact contribution to the ROAS move."""

    channel: str
    sign: int
    baseline: float
    comparison: float
    log_points: float              # sign * (ln comparison - ln baseline); the three sum to d ln ROAS
    own_pct: float | None          # the channel's own % change, 100 * (comparison/baseline - 1)
    share_pct: float | None        # % of the net move; None when shares are suppressed

    @property
    def change(self) -> str:
        """``"baseline -> comparison"`` display string."""
        return f"{self.baseline:.4g} -> {self.comparison:.4g}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel, "lp": round(self.log_points, 3),
            "own_pct": None if self.own_pct is None else round(self.own_pct, 1),
            "pct": self.share_pct, "change": self.change,
        }


@dataclass(frozen=True)
class Layer1Result:
    """Exact three-channel split of a ROAS move between two windows."""

    baseline: WindowAggregate
    comparison: WindowAggregate
    channels: Mapping[str, ChannelEffect]
    raw_dln: float                 # icr + asp + cpm log points (= ln(ROAS_c / ROAS_b) when ``exact``)
    dln: float                     # denominator used for shares after the small-move fallback
    exact: bool                    # all four sums positive in both windows
    shares_suppressed: bool = False

    @property
    def roas_baseline(self) -> float:
        return self.baseline.roas

    @property
    def roas_comparison(self) -> float:
        return self.comparison.roas

    @property
    def roas_pct(self) -> float | None:
        """Plain percentage change in ROAS, or ``None`` without a baseline."""
        return 100.0 * (self.comparison.roas / self.baseline.roas - 1.0) if self.baseline.roas else None

    @property
    def residual(self) -> float | None:
        """``raw_dln - ln(ROAS_c / ROAS_b)``; ~0 when ``exact``, ``None`` otherwise."""
        if not self.exact:
            return None
        return self.raw_dln - math.log(self.comparison.roas / self.baseline.roas)

    @property
    def dominant(self) -> str:
        """Channel with the largest absolute log-point contribution."""
        return max(CHANNELS, key=lambda k: abs(self.channels[k].log_points))

    @property
    def worst(self) -> str:
        """Channel with the most negative contribution (ties: cpm, icr1k, asp)."""
        return min(("cpm", "icr1k", "asp"), key=lambda k: self.channels[k].log_points)

    def log_points(self, channel: str) -> float:
        return self.channels[channel].log_points

    def as_dict(self) -> dict[str, Any]:
        return {
            "baseline": self.baseline.as_dict(), "comparison": self.comparison.as_dict(),
            "channels": {k: v.as_dict() for k, v in self.channels.items()},
            "raw_dln": self.raw_dln, "dln": self.dln, "exact": self.exact,
            "shares_suppressed": self.shares_suppressed,
        }


def log_decomposition(
    baseline: MetricCell | WindowAggregate | Mapping[str, Any] | Iterable[PerfRow],
    comparison: MetricCell | WindowAggregate | Mapping[str, Any] | Iterable[PerfRow],
    *,
    model_total: float | None = None,
    suppress_shares: bool = False,
    cfg: IdentityConfig | None = None,
) -> Layer1Result:
    """Split a ROAS move into conversion, price and cost channels, exactly.

    Decision: through which of ICR, ASP and CPM did ROAS move, and by how
    much.  Formula, per channel ``k`` with sign ``s`` (ICR +1, ASP +1,
    CPM -1) on window aggregates ``B`` (baseline) and ``T`` (comparison)::

        l1[k]   = s * (ln max(T[k], floor) - ln max(B[k], floor))
        raw_dln = l1[icr1k] + l1[asp] + l1[cpm]      (= ln(ROAS_T / ROAS_B))
        dln     = raw_dln            if |raw_dln| > min_dln
                  model_total        elif given and |model_total| > min_dln
                  min_dln            otherwise
        share_pct[k] = 100 * l1[k] / dln
        own_pct[k]   = 100 * (T[k] / B[k] - 1)

    The sum is exact (to floating point) whenever all four sums are
    positive in both windows and no channel value sits below ``log_floor``
    (a floored channel is clipped, so the parts no longer add up); ``Layer1Result.exact`` says so and
    ``residual`` measures it.  ``model_total`` is the layer-2 model's total
    contribution, used only as the share denominator for a near-zero move.
    ``suppress_shares`` is set by the caller when its small-move guard is
    on; shares are then ``None`` because dividing by a tiny net move would
    produce percentages in the hundreds.

    Returns a :class:`Layer1Result`.
    """
    c = cfg or _DEFAULT
    b, t = window_aggregate(baseline), window_aggregate(comparison)
    lp: dict[str, float] = {}
    for k, sg in CHANNEL_SIGNS.items():
        lp[k] = sg * (math.log(max(t.channel(k), c.log_floor)) - math.log(max(b.channel(k), c.log_floor)))
    raw = lp["icr1k"] + lp["asp"] + lp["cpm"]
    if abs(raw) > c.min_dln:
        dln = raw
    elif model_total is not None and abs(model_total) > c.min_dln:
        dln = float(model_total)
    else:
        dln = c.min_dln
    effects: dict[str, ChannelEffect] = {}
    for k, sg in CHANNEL_SIGNS.items():
        bv, tv = b.channel(k), t.channel(k)
        share: float | None = None
        if not suppress_shares:
            share = 100.0 * lp[k] / dln
            if c.share_digits is not None:
                share = round(share, c.share_digits)
        effects[k] = ChannelEffect(
            channel=k, sign=sg, baseline=bv, comparison=tv, log_points=lp[k],
            own_pct=100.0 * (tv / bv - 1.0) if bv else None, share_pct=share,
        )
    return Layer1Result(
        baseline=b, comparison=t, channels=effects, raw_dln=raw, dln=dln,
        exact=(b.all_positive and t.all_positive
               and all(min(b.channel(k), t.channel(k)) >= c.log_floor for k in CHANNEL_SIGNS)),
        shares_suppressed=suppress_shares,
    )


def decompose_rows(
    rows: Sequence[PerfRow],
    windows: WindowPair,
    *,
    model_total: float | None = None,
    suppress_shares: bool = False,
    cfg: IdentityConfig | None = None,
) -> Layer1Result:
    """Layer-1 decomposition straight from fact rows and a window pair.

    Sums every row of ``windows.prior`` and ``windows.current`` (all spend
    days, not only converting days) and calls :func:`log_decomposition`.
    Dropping zero-sale days first would overstate ROAS and ICR for sparse
    entities; this function deliberately keeps them.
    """
    prior, current = split_rows(rows, windows)
    return log_decomposition(
        cell_from_rows(prior), cell_from_rows(current),
        model_total=model_total, suppress_shares=suppress_shares, cfg=cfg,
    )


# ---------------------------------------------------------------------------
# layer-1 rule-based verdict
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Layer1Verdict:
    """First-pass classification of a ROAS drop from the three channels.

    ``code`` is one of ``auction_pressure``, ``self_inflicted_bid``,
    ``cpm_rose_bid_cut`` (pressure despite a lower bid),
    ``cpm_rose_bid_unknown``, ``conversion_fell``, ``asp_fell`` when
    ``eligible``; otherwise ``code`` is ``None`` and ``reason`` is one of
    ``not_a_drop``, ``below_min_spend``, ``non_positive_channel``.
    """

    eligible: bool
    code: str | None
    text: str
    reason: str | None = None
    channel: str | None = None
    bid_change_pct: float | None = None


_VERDICT_TEXT = {
    "auction_pressure": "CPM rose with the bid flat: external auction pressure",
    "self_inflicted_bid": "CPM rose and the bid was raised: self-inflicted",
    "cpm_rose_bid_cut": "CPM rose although the bid was cut: external auction pressure",
    "cpm_rose_bid_unknown": "CPM rose; no bid data to separate auction pressure from a bid change",
    "conversion_fell": "Conversion per impression fell: check availability, position and competitor price",
    "asp_fell": "Average selling price fell: own discounting or a product-mix shift",
}


def layer1_verdict(
    result: Layer1Result,
    bid_change_pct: float | None = None,
    *,
    cfg: IdentityConfig | None = None,
    economics: Economics | None = None,
) -> Layer1Verdict:
    """Classify a ROAS drop from layer 1 alone (cheap, no model).

    Decision table, on the signed channel contributions ``k``:

    =====================================================  ======================
    condition                                              code
    =====================================================  ======================
    ``k_cpm`` most negative and ``|bid change| < 7%``      ``auction_pressure``
    ``k_cpm`` most negative and the bid rose >= 7%         ``self_inflicted_bid``
    ``k_cpm`` most negative and the bid fell >= 7%         ``cpm_rose_bid_cut``
    ``k_cpm`` most negative and no bid data                ``cpm_rose_bid_unknown``
    ``k_icr`` most negative                                ``conversion_fell``
    otherwise                                              ``asp_fell``
    =====================================================  ======================

    Gates: ROAS fell; comparison-window spend above
    ``economics.money(cfg.verdict_min_spend)``; ROAS, CPM, ICR and ASP all
    positive in both windows.  ``bid_change_pct`` is the percentage change
    in the spend-weighted bid; when omitted it is derived from the two
    aggregates if both carry a bid.  A platform without bid data degrades
    to ``cpm_rose_bid_unknown`` rather than guessing.

    Returns a :class:`Layer1Verdict`; ineligible cases carry a ``reason``.
    """
    c = cfg or _DEFAULT
    eco = economics or Economics()
    b, t = result.baseline, result.comparison
    if not result.exact:
        return Layer1Verdict(False, None, "", reason="non_positive_channel")
    if t.spend <= eco.money(c.verdict_min_spend):
        return Layer1Verdict(False, None, "", reason="below_min_spend")
    if not t.roas < b.roas:
        return Layer1Verdict(False, None, "", reason="not_a_drop")
    if bid_change_pct is None and b.bid and t.bid is not None:
        bid_change_pct = 100.0 * (t.bid / b.bid - 1.0)
    worst = result.worst
    if worst == "cpm":
        if bid_change_pct is None:
            code = "cpm_rose_bid_unknown"
        elif bid_change_pct >= c.verdict_bid_flat_pct:
            code = "self_inflicted_bid"
        elif bid_change_pct <= -c.verdict_bid_flat_pct:
            code = "cpm_rose_bid_cut"
        else:
            code = "auction_pressure"
    elif worst == "icr1k":
        code = "conversion_fell"
    else:
        code = "asp_fell"
    return Layer1Verdict(True, code, _VERDICT_TEXT[code], channel=worst, bid_change_pct=bid_change_pct)


# ---------------------------------------------------------------------------
# prior-rate two-factor decompositions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TwoFactor:
    """Exact split of ``volume * rate`` between two periods.

    ``volume_effect + rate_effect == total`` with no residual; the
    interaction term sits in ``rate_effect``.
    """

    volume_effect: float
    rate_effect: float
    total: float
    mechanism: str                 # "rate-led" if |rate_effect| > |volume_effect| else "volume-led"

    def as_dict(self) -> dict[str, Any]:
        return {
            "volume_effect": self.volume_effect, "rate_effect": self.rate_effect,
            "total": self.total, "mechanism": self.mechanism,
        }


def two_factor(
    volume_prior: float,
    rate_prior: float,
    volume_current: float,
    rate_current: float,
    *,
    labels: tuple[str, str] = ("rate-led", "volume-led"),
) -> TwoFactor:
    """Split the change in ``volume * rate`` into a volume and a rate effect.

    Formula (prior-rate / current-volume convention)::

        volume_effect = (volume_c - volume_p) * rate_p
        rate_effect   = volume_c * (rate_c - rate_p)
        volume_effect + rate_effect == volume_c*rate_c - volume_p*rate_p   (exact)

    The volume delta is priced at the *prior* rate and the rate delta at the
    *current* volume, so the interaction term is assigned to the rate
    effect.  This is intentionally not the midpoint split used by
    shift-share; a verifier that re-adds the parts depends on it.

    ``mechanism`` is ``labels[0]`` when ``|rate_effect| > |volume_effect|``,
    else ``labels[1]``.  Returns a :class:`TwoFactor`.
    """
    vp, rp, vc, rc = _num(volume_prior), _num(rate_prior), _num(volume_current), _num(rate_current)
    vol = (vc - vp) * rp
    rate = vc * (rc - rp)
    return TwoFactor(vol, rate, vol + rate, labels[0] if abs(rate) > abs(vol) else labels[1])


def unit_decomposition(
    impressions_prior: float,
    units_prior: float,
    impressions_current: float,
    units_current: float,
) -> TwoFactor:
    """Split a change in units into a visibility and a conversion effect.

    ``units = impressions * conversion`` with ``conversion =
    units / impressions`` per window (0 without impressions), then
    :func:`two_factor`: ``volume_effect`` is the impression (visibility)
    effect, ``rate_effect`` the conversion effect, and they sum to
    ``units_current - units_prior`` exactly.  ``mechanism`` is
    ``"conversion-led"`` or ``"visibility-led"``.

    A period with units but no impressions (attributed orders on a
    no-impression day) has no defined conversion rate.  The split is then
    not identifiable, so the whole change is reported as ``rate_effect``
    with ``volume_effect = 0``: the parts still add up.
    """
    ip, ic = _num(impressions_prior), _num(impressions_current)
    up, uc = _num(units_prior), _num(units_current)
    if (not ip and up) or (not ic and uc):
        return TwoFactor(0.0, uc - up, uc - up, "conversion-led")
    cp = _num(units_prior) / ip if ip else 0.0
    cc = _num(units_current) / ic if ic else 0.0
    return two_factor(ip, cp, ic, cc, labels=("conversion-led", "visibility-led"))


def value_decomposition(
    quantity_prior: float,
    price_prior: float,
    quantity_current: float,
    price_current: float,
) -> TwoFactor:
    """Split a change in value into a quantity and a price effect.

    ``delta_value = (qty_c - qty_p) * asp_p + qty_c * (asp_c - asp_p)``,
    exact, same convention as :func:`two_factor`.  ``mechanism`` is
    ``"price-led"`` or ``"quantity-led"``.
    """
    return two_factor(quantity_prior, price_prior, quantity_current, price_current,
                      labels=("price-led", "quantity-led"))


def rounded_parts(total: float, first_effect: float, ndigits: int = 0) -> tuple[float, float, float]:
    """Round a two-part decomposition so the displayed parts still add up.

    Presentation rule: round the total and the first effect, then show the
    second effect as their difference instead of rounding it on its own.
    Returns ``(rounded_total, rounded_first, displayed_second)`` with
    ``rounded_first + displayed_second == rounded_total``.
    """
    rt, rf = round(total, ndigits), round(first_effect, ndigits)
    return rt, rf, round(rt - rf, ndigits)


# ---------------------------------------------------------------------------
# ROAS = revenue per thousand impressions / CPM
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoasMove:
    """Demand-side versus cost-side reading of a ROAS move."""

    roas_prior: float
    roas_current: float
    roas_pct: float | None
    spend_pct: float | None
    revenue_pct: float | None
    driver: str | None             # "efficiency" (revenue fell faster than spend) or "budget_artifact"
    rpm_prior: float | None
    rpm_current: float | None
    rpm_pct: float | None
    cpm_prior: float | None
    cpm_current: float | None
    cpm_pct: float | None
    side: str | None               # "demand" (revenue per impression moved more) or "cost" (CPM moved more)
    spend_cut_note: bool           # spend fell enough that the cut and the ROAS move are separate decisions

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _pct(cur: float, pri: float) -> float | None:
    return 100.0 * (cur / pri - 1.0) if pri else None


def roas_move(
    prior: MetricCell | WindowAggregate | Mapping[str, Any] | Iterable[PerfRow],
    current: MetricCell | WindowAggregate | Mapping[str, Any] | Iterable[PerfRow],
    *,
    cfg: IdentityConfig | None = None,
) -> RoasMove:
    """Say whether a ROAS move is demand-side or cost-side.

    Identity: ``ROAS = RPM / CPM`` with ``RPM = ROAS * CPM`` = attributed
    revenue per thousand impressions.  Reads, in order:

    1. ROAS prior -> current with % change.
    2. With prior spend: spend % and attributed-revenue % change.
       ``revenue_pct < spend_pct`` -> ``driver = "efficiency"`` (revenue
       fell faster, or grew slower, than spend: an efficiency loss), else
       ``"budget_artifact"`` (spend moved at least as fast as revenue).
    3. With impressions and spend in both windows: RPM and CPM with %
       changes, and ``side`` = whichever moved more in log terms
       (``"demand"`` for RPM, ``"cost"`` for CPM), so the comparison is
       named rather than left to the reader.
    4. ``spend_cut_note`` when spend fell by ``|cfg.spend_cut_note_pct|`` or
       more: a ratio is unaffected by less spend at the same efficiency, so
       the cut and the ROAS move are separate decisions.

    Returns a :class:`RoasMove`.
    """
    c = cfg or _DEFAULT
    p, t = window_aggregate(prior), window_aggregate(current)
    spend_pct = revenue_pct = None
    driver: str | None = None
    if p.spend:
        spend_pct = _pct(t.spend, p.spend)
        revenue_pct = _pct(t.sales, p.sales)
        if spend_pct is not None and revenue_pct is not None:
            driver = "efficiency" if revenue_pct < spend_pct else "budget_artifact"
    rpm_p = rpm_c = cpm_p = cpm_c = None
    side: str | None = None
    if p.impressions and t.impressions and p.spend and t.spend:
        rpm_p, rpm_c, cpm_p, cpm_c = p.rpm, t.rpm, p.cpm, t.cpm
        if rpm_p > 0 and rpm_c > 0:
            side = "demand" if abs(math.log(rpm_c / rpm_p)) >= abs(math.log(cpm_c / cpm_p)) else "cost"
    return RoasMove(
        roas_prior=p.roas, roas_current=t.roas, roas_pct=_pct(t.roas, p.roas),
        spend_pct=spend_pct, revenue_pct=revenue_pct, driver=driver,
        rpm_prior=rpm_p, rpm_current=rpm_c,
        rpm_pct=_pct(rpm_c, rpm_p) if rpm_p and rpm_c is not None else None,
        cpm_prior=cpm_p, cpm_current=cpm_c,
        cpm_pct=_pct(cpm_c, cpm_p) if cpm_p and cpm_c is not None else None,
        side=side,
        spend_cut_note=spend_pct is not None and spend_pct <= c.spend_cut_note_pct,
    )


# ---------------------------------------------------------------------------
# quartile bucket strategy
# ---------------------------------------------------------------------------


def quartile_buckets(
    daily: Mapping[date, MetricCell],
    *,
    cfg: IdentityConfig | None = None,
) -> tuple[MetricCell, MetricCell] | None:
    """Alternative "good days versus bad days" buckets (optional strategy).

    Ranks the scope's spend days by daily ROAS and returns
    ``(healthy, worst)`` = the summed top and bottom quartiles, ignoring
    dates (quartile sizes follow the usual n-tile rule: the remainder goes
    to the leading tiles).  It contrasts good days with bad days rather
    than two periods, so it is *not* a time trend; the default everywhere
    else is period-over-period.  Feed the pair to
    :func:`log_decomposition` as ``(baseline, comparison)``.

    Returns ``None`` with fewer than ``cfg.quartile_min_days`` spend days.
    """
    from .metrics import sum_cells

    c = cfg or _DEFAULT
    days = [(cell.sales / cell.spend, d, cell) for d, cell in daily.items() if cell.spend > 0]
    n = len(days)
    if n < max(c.quartile_min_days, 4):
        return None
    days.sort(key=lambda x: (-x[0], x[1]))
    top_n = n // 4 + (1 if n % 4 else 0)
    bottom_n = n // 4
    healthy = sum_cells(x[2] for x in days[:top_n])
    worst = sum_cells(x[2] for x in days[n - bottom_n:])
    return healthy, worst
