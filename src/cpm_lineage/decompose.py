"""Exact midpoint decompositions of a blended rate, at any grain.

A blended rate ``R = sum(value) / sum(weight)`` (ROAS = sales / spend,
ASP = sales / units, CPM = spend / impressions ...) moves between two
periods for two reasons: the *mix* of weight across items changed, or
the items' own *rates* changed.  With weight shares ``w`` and item rates
``r`` the midpoint (Marshall-Edgeworth) split per item is::

    mix_i  = (w_c - w_p) * (r_c + r_p) / 2
    rate_i = (w_c + w_p) / 2 * (r_c - r_p)
    sum(mix_i) + sum(rate_i) == R_c - R_p            (exact, no interaction term)

:func:`shift_share` is the single primitive; ad-type, campaign, keyword,
product and geo shift-shares are the same call at a different grain, and
the ASP mix/price split is the same call with units as the weight.

Also here: the entry/exit composition split, the Pareto drag ranking and
the segment growth decomposition.  The prior-rate two-factor family is in
:mod:`cpm_lineage.identity`; the two conventions are never mixed inside
one identity.

All functions are pure: plain records in, plain records out.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Hashable, Iterable, Mapping, Sequence

from .adapters.base import PlatformProfile
from .config import Economics
from .schema import Entity, PerfRow, Window, WindowPair
from .windows import filter_rows

__all__ = [
    "AspSplit",
    "BrandShareRow",
    "CompositionItem",
    "CompositionResult",
    "DecomposeConfig",
    "DragRow",
    "GrowthDecomposition",
    "LaunchRow",
    "ParetoResult",
    "ShareMove",
    "ShiftShareInput",
    "ShiftShareItem",
    "ShiftShareResult",
    "TierRow",
    "TierShift",
    "ad_type_shift_share",
    "asp_mix_price",
    "entry_exit",
    "entry_exit_rows",
    "growth_decomposition",
    "pareto_drag",
    "pareto_drag_rows",
    "period_inputs",
    "shift_share",
    "shift_share_rows",
]


@dataclass(frozen=True)
class DecomposeConfig:
    """Thresholds of the decomposition layer (every field has a default)."""

    pareto_top_n: int = 10                   # rows returned by the drag ranking ("worst 10, or all if fewer")
    pareto_cum_pct: float = 80.0             # the "vital few" cut: classic 80/20
    composition_min_spend: float = 1000.0    # reference money: an entity "exists" in a period only above this spend
    composition_top_n: int = 6               # entrants / exits / movers listed
    asp_min_move_pct: float = 10.0           # the ASP split is only worth reading when window ASP moved this much
    asp_min_abs_delta: float = 1.0           # reference money: and when |dASP| exceeds one currency unit
    asp_mover_min_pct: float = 5.0           # products contributing at least this % of dASP are listed as movers
    growth_entrant_prior_share_max: float = 0.05  # share points: effectively absent in the prior period
    growth_entrant_share_min: float = 0.3    # share points: but visible now, so it is an entrant, not a gainer
    growth_mix_shift_min_pp: float = 0.5     # tier mix shift is reported only when the largest shift reaches this
    growth_min_tiers: int = 2                # a mix shift needs at least two tiers
    growth_entrant_names: int = 3            # entrant names listed
    growth_ramping_label: str = "ramping"    # launch trend label counted as still ramping


_DEFAULT = DecomposeConfig()


def _num(x: Any) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    return v if v == v and v not in (float("inf"), float("-inf")) else 0.0


# ---------------------------------------------------------------------------
# shift-share primitive
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ShiftShareInput:
    """One item's two-period sums: a weight (denominator) and a value (numerator).

    For ROAS the weight is spend and the value attributed sales; for ASP the
    weight is units and the value sales.  A missing value is 0.
    """

    key: Hashable
    weight_prior: float = 0.0
    value_prior: float = 0.0
    weight_current: float = 0.0
    value_current: float = 0.0
    extras: Mapping[str, Any] = field(default_factory=dict)


_ALIASES = {
    "weight_prior": ("weight_prior", "spend_prior", "units_prior", "h_sp"),
    "value_prior": ("value_prior", "sales_prior", "h_sa"),
    "weight_current": ("weight_current", "spend_current", "units_current", "w_sp"),
    "value_current": ("value_current", "sales_current", "w_sa"),
}


def _coerce(item: ShiftShareInput | Mapping[str, Any]) -> ShiftShareInput:
    if isinstance(item, ShiftShareInput):
        return item
    vals: dict[str, float] = {}
    used = {"key"}
    for name, names in _ALIASES.items():
        vals[name] = 0.0
        for n in names:
            if n in item:
                vals[name] = _num(item[n])
                used.add(n)
                break
    extras = {k: v for k, v in item.items() if k not in used}
    return ShiftShareInput(key=item.get("key"), extras=extras, **vals)


@dataclass(frozen=True)
class ShiftShareItem:
    """One item's share, rate and exact mix / rate effects (all unrounded)."""

    key: Hashable
    weight_prior: float
    weight_current: float
    value_prior: float
    value_current: float
    share_prior: float             # weight share 0..1
    share_current: float
    rate_prior: float              # value / weight, 0 without weight
    rate_current: float
    mix: float
    rate: float
    total: float
    extras: Mapping[str, Any] = field(default_factory=dict)

    # ROAS-flavoured aliases
    @property
    def spend_prior(self) -> float:
        return self.weight_prior

    @property
    def spend_current(self) -> float:
        return self.weight_current

    @property
    def roas_prior(self) -> float:
        return self.rate_prior

    @property
    def roas_current(self) -> float:
        return self.rate_current

    def as_dict(self) -> dict[str, Any]:
        """Display record: integer-percent shares, 2-dp rates, unrounded effects.

        Recomputing mix / rate from the rounded display values will not
        reproduce the unrounded effects.
        """
        return {
            "key": self.key,
            "weight_cur": self.weight_current, "weight_prev": self.weight_prior,
            "share_cur": round(100 * self.share_current), "share_prev": round(100 * self.share_prior),
            "rate_cur": round(self.rate_current, 2), "rate_prev": round(self.rate_prior, 2),
            "mix": self.mix, "rate": self.rate, "total": self.total, **dict(self.extras),
        }


@dataclass(frozen=True)
class ShiftShareResult:
    """Shift-share of a blended rate: items plus the exact headline."""

    items: tuple[ShiftShareItem, ...]   # sorted ascending by ``total`` (biggest drag first)
    mix_total: float
    rate_total: float
    rate_prior: float                   # blended rate, prior period
    rate_current: float
    orphan_value_prior: float = 0.0     # value on items with no weight; excluded from the blended rate
    orphan_value_current: float = 0.0

    @property
    def delta(self) -> float:
        """``rate_current - rate_prior`` (= ``mix_total + rate_total``)."""
        return self.rate_current - self.rate_prior

    @property
    def residual(self) -> float:
        """``mix_total + rate_total - delta``; floating-point noise only."""
        return self.mix_total + self.rate_total - self.delta

    @property
    def primary(self) -> str:
        """``"MIX"`` if ``|mix_total| >= |rate_total|`` else ``"RATE"``."""
        return "MIX" if abs(self.mix_total) >= abs(self.rate_total) else "RATE"

    def item(self, key: Hashable) -> ShiftShareItem | None:
        for it in self.items:
            if it.key == key:
                return it
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "rate_cur": self.rate_current, "rate_prev": self.rate_prior, "delta": self.delta,
            "mix_total": self.mix_total, "rate_total": self.rate_total, "primary": self.primary,
            "items": [it.as_dict() for it in self.items],
        }


def shift_share(
    items: Iterable[ShiftShareInput | Mapping[str, Any]],
    *,
    one_sided: str = "zero",
    sort: bool = True,
) -> ShiftShareResult:
    """Exact midpoint shift-share of a blended rate over any item list.

    Decision: did the blended rate move because weight shifted between
    items (mix) or because items' own rates moved (rate), and which items
    carry it.  Formula, with ``W`` the period weight totals::

        w_p = weight_p / W_p      w_c = weight_c / W_c       (0 when the total is 0)
        r_p = value_p / weight_p  r_c = value_c / weight_c   (0 without weight)
        mix   = (w_c - w_p) * (r_c + r_p) / 2
        rate  = (w_c + w_p) / 2 * (r_c - r_p)
        total = mix + rate

    ``sum(total)`` equals the change in the blended rate to floating-point
    precision, including items present in one period only and a period with
    no weight at all (its blended rate is 0).

    ``one_sided`` sets how an item with weight in only one period is split:
    ``"zero"`` (default, the ROAS convention) treats the missing rate as 0,
    halving the item's level between mix and rate; ``"carry"`` (the price
    convention) uses the single available rate for both periods, so the
    whole level is a mix effect and its rate effect is 0.  Both are exact.

    Items accept :class:`ShiftShareInput` or mappings keyed
    ``key, weight_prior, value_prior, weight_current, value_current``
    (aliases ``spend_* / sales_* / units_*``).  Duplicate keys are summed.
    Value sitting on an item with no weight in that period cannot enter a
    weight-share identity; it is reported in ``orphan_value_*`` and left
    out of the blended rate.

    Returns a :class:`ShiftShareResult`, items sorted ascending by total.
    """
    if one_sided not in ("zero", "carry"):
        raise ValueError(f"one_sided must be 'zero' or 'carry', got {one_sided!r}")
    merged: dict[Hashable, list[Any]] = {}
    for raw in items:
        it = _coerce(raw)
        acc = merged.setdefault(it.key, [0.0, 0.0, 0.0, 0.0, {}])
        acc[0] += _num(it.weight_prior)
        acc[1] += _num(it.value_prior)
        acc[2] += _num(it.weight_current)
        acc[3] += _num(it.value_current)
        acc[4].update(it.extras)
    w_tot_p = sum(a[0] for a in merged.values() if a[0] > 0)
    w_tot_c = sum(a[2] for a in merged.values() if a[2] > 0)
    out: list[ShiftShareItem] = []
    num_p = num_c = orphan_p = orphan_c = 0.0
    for key, (wp_raw, vp, wc_raw, vc, extras) in merged.items():
        has_p, has_c = wp_raw > 0, wc_raw > 0
        if has_p:
            num_p += vp
        else:
            orphan_p += vp
        if has_c:
            num_c += vc
        else:
            orphan_c += vc
        wp = wp_raw / w_tot_p if has_p and w_tot_p else 0.0
        wc = wc_raw / w_tot_c if has_c and w_tot_c else 0.0
        rp = vp / wp_raw if has_p else 0.0
        rc = vc / wc_raw if has_c else 0.0
        mp, mc = rp, rc
        if one_sided == "carry":
            if has_c and not has_p:
                mp = rc
            elif has_p and not has_c:
                mc = rp
        mix = (wc - wp) * (mc + mp) / 2.0
        rate = (wc + wp) / 2.0 * (mc - mp)
        out.append(ShiftShareItem(
            key=key, weight_prior=wp_raw, weight_current=wc_raw, value_prior=vp, value_current=vc,
            share_prior=wp, share_current=wc, rate_prior=rp, rate_current=rc,
            mix=mix, rate=rate, total=mix + rate, extras=extras,
        ))
    if sort:
        out.sort(key=lambda i: i.total)
    return ShiftShareResult(
        items=tuple(out),
        mix_total=sum(i.mix for i in out), rate_total=sum(i.rate for i in out),
        rate_prior=num_p / w_tot_p if w_tot_p else 0.0,
        rate_current=num_c / w_tot_c if w_tot_c else 0.0,
        orphan_value_prior=orphan_p, orphan_value_current=orphan_c,
    )


_KeySpec = str | Sequence[str] | Callable[[PerfRow], Hashable]


def _keyfn(key: _KeySpec) -> Callable[[PerfRow], Hashable]:
    if callable(key):
        return key
    if isinstance(key, str):
        return lambda r: getattr(r, key)
    names = tuple(key)
    return lambda r: tuple(getattr(r, n) for n in names)


def period_inputs(
    rows: Iterable[PerfRow],
    windows: WindowPair,
    key: _KeySpec,
    *,
    value: str = "sales",
    weight: str = "spend",
) -> list[ShiftShareInput]:
    """Aggregate fact rows to two-period ``(weight, value)`` sums per key.

    ``key`` is a ``PerfRow`` field name, a sequence of names (tuple key) or
    a callable.  Rows outside both windows are ignored; a null value or
    weight counts as 0.  Order follows first appearance.
    """
    kf = _keyfn(key)
    acc: dict[Hashable, list[float]] = {}
    for r in rows:
        if windows.current.contains(r.date):
            off = 2
        elif windows.prior.contains(r.date):
            off = 0
        else:
            continue
        a = acc.setdefault(kf(r), [0.0, 0.0, 0.0, 0.0])
        a[off] += _num(getattr(r, weight))
        a[off + 1] += _num(getattr(r, value))
    return [ShiftShareInput(k, a[0], a[1], a[2], a[3]) for k, a in acc.items()]


def shift_share_rows(
    rows: Iterable[PerfRow],
    windows: WindowPair,
    key: _KeySpec,
    *,
    value: str = "sales",
    weight: str = "spend",
    one_sided: str = "zero",
) -> ShiftShareResult:
    """Shift-share of ``sum(value) / sum(weight)`` straight from fact rows.

    Groups ``rows`` by ``key`` over ``windows.prior`` / ``windows.current``
    and calls :func:`shift_share`.  Defaults decompose ROAS; pass
    ``value="spend", weight="impressions"`` for cost per impression, or
    ``value="sales", weight="units", one_sided="carry"`` for price.
    """
    return shift_share(period_inputs(rows, windows, key, value=value, weight=weight), one_sided=one_sided)


def _ad_type_lookup(
    profile: PlatformProfile | None, entities: Iterable[Entity] | None
) -> Callable[[PerfRow], str]:
    by_entity = {e.entity_id: e.ad_type for e in entities or () if e.ad_type}

    def canon(r: PerfRow) -> str:
        raw = r.ad_type or by_entity.get(r.entity_id)
        if profile is not None:
            return profile.canonical_ad_type(raw)
        return (raw or "").strip() or "Unknown"

    return canon


def ad_type_shift_share(
    rows: Iterable[PerfRow],
    windows: WindowPair,
    profile: PlatformProfile | None = None,
    entities: Iterable[Entity] | None = None,
) -> ShiftShareResult:
    """Account ROAS shift-share across canonical ad types.

    Each row's ad-type label is canonicalised through
    ``profile.canonical_ad_type`` *before* aggregation (platforms rename
    labels mid-period; raw labels would split one type into phantom
    buckets and inflate the mix effect).  A row without a label falls back
    to its entity's ad type when ``entities`` is given.  Each item carries
    ``extras["drillable"]`` (the type has targeting-unit grain data) and
    ``extras["no_attrib"]`` (attributed sales are structurally absent, so
    its ROAS of 0 is not a performance reading).

    Returns the :class:`ShiftShareResult`; ``.primary`` routes the account
    read to ``"MIX"`` or ``"RATE"``.
    """
    res = shift_share(period_inputs(rows, windows, _ad_type_lookup(profile, entities)), sort=True)
    drill = profile.keyword_grain_types() if profile is not None else frozenset()
    noattr = profile.no_attribution_types() if profile is not None else frozenset()
    items = tuple(
        ShiftShareItem(**{**it.__dict__, "extras": {"drillable": it.key in drill, "no_attrib": it.key in noattr}})
        for it in res.items
    )
    return ShiftShareResult(
        items=items, mix_total=res.mix_total, rate_total=res.rate_total,
        rate_prior=res.rate_prior, rate_current=res.rate_current,
        orphan_value_prior=res.orphan_value_prior, orphan_value_current=res.orphan_value_current,
    )


# ---------------------------------------------------------------------------
# Pareto drag
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DragRow:
    """One entity's drag against the reference ROAS."""

    key: Hashable
    spend: float
    sales: float
    roas: float
    drag: float                    # spend * (reference_roas - roas); positive = below the reference
    cum_pct: float                 # cumulative % of positive drag, in ranked order
    group: str | None = None
    extras: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "group": self.group, "spend": self.spend, "sales": self.sales,
                "roas": round(self.roas, 2), "drag": self.drag, "cum_pct": self.cum_pct, **dict(self.extras)}


@dataclass(frozen=True)
class ParetoResult:
    """Ranked drag list and the "vital few" counts."""

    rows: tuple[DragRow, ...]      # top ``pareto_top_n`` by drag, descending
    n80: int                       # smallest number of entities whose positive drag reaches the cut
    n80_strict: int                # entities with drag > 0 and cum_pct <= cut (the strict count)
    total_positive_drag: float
    reference_roas: float
    n_items: int                   # entities ranked (before the top-N cut)


def pareto_drag(
    items: Iterable[Mapping[str, Any]],
    *,
    reference_roas: float | None = None,
    exclude_groups: Iterable[str] = (),
    cfg: DecomposeConfig | None = None,
) -> ParetoResult:
    """Rank entities by how far they drag the blended ROAS below a reference.

    Decision: which few entities explain most of the shortfall, so the
    drill starts there.  Formula, current period only::

        drag    = spend * (reference_roas - entity_roas)
        cum_pct = 100 * cumsum(max(drag, 0)) / sum(max(drag, 0))     (sorted by drag, descending)

    ``items`` are mappings with ``key, spend, sales`` and optionally
    ``group`` (ad type); other keys are kept as extras.  Duplicate
    ``(key, group)`` pairs are summed.  Entities whose ``group`` is in
    ``exclude_groups`` (types with no attributed sales, which would fake a
    huge drag) and entities without spend are not ranked, but they do
    count in the default ``reference_roas`` = total sales / total spend of
    everything passed.

    Positive drags are a *ranking device*, not an additive decomposition:
    against a reference computed over the same set all drags net to zero.

    ``n80`` is the smallest number of leading entities whose cumulative
    positive drag reaches ``cfg.pareto_cum_pct``.  Counting
    entities with ``cum_pct <= 80`` instead returns 0 when the single
    worst entity alone exceeds the cut; that count is kept as
    ``n80_strict``.

    Returns a :class:`ParetoResult` with the top ``cfg.pareto_top_n`` rows
    (no ``drag > 0`` gate on the list itself).
    """
    c = cfg or _DEFAULT
    excluded = set(exclude_groups)
    acc: dict[tuple[Hashable, Any], list[Any]] = {}
    tot_sp = tot_sa = 0.0
    for it in items:
        sp, sa = _num(it.get("spend")), _num(it.get("sales"))
        tot_sp += sp
        tot_sa += sa
        grp = it.get("group")
        a = acc.setdefault((it.get("key"), grp), [0.0, 0.0, {}])
        a[0] += sp
        a[1] += sa
        a[2].update({k: v for k, v in it.items() if k not in ("key", "spend", "sales", "group")})
    ref = float(reference_roas) if reference_roas is not None else (tot_sa / tot_sp if tot_sp else 0.0)
    ranked = [
        (sp * (ref - sa / sp), key, grp, sp, sa, extras)
        for (key, grp), (sp, sa, extras) in acc.items()
        if sp > 0 and grp not in excluded
    ]
    ranked.sort(key=lambda x: -x[0])
    tot_drag = sum(max(x[0], 0.0) for x in ranked)
    denom = tot_drag or 1e-9
    rows: list[DragRow] = []
    cum = 0.0
    n80 = n80_strict = 0
    reached = False
    for drag, key, grp, sp, sa, extras in ranked:
        cum += max(drag, 0.0)
        cum_pct = 100.0 * cum / denom
        if drag > 0:
            if cum_pct <= c.pareto_cum_pct:
                n80_strict += 1
            if not reached:
                n80 += 1
                reached = cum_pct >= c.pareto_cum_pct - 1e-9
        rows.append(DragRow(key=key, spend=sp, sales=sa, roas=sa / sp, drag=drag,
                            cum_pct=cum_pct, group=grp, extras=extras))
    return ParetoResult(
        rows=tuple(rows[: c.pareto_top_n]), n80=n80, n80_strict=n80_strict,
        total_positive_drag=tot_drag, reference_roas=ref, n_items=len(rows),
    )


def pareto_drag_rows(
    rows: Iterable[PerfRow],
    window: Window,
    profile: PlatformProfile | None = None,
    entities: Iterable[Entity] | None = None,
    *,
    key: _KeySpec = "entity_id",
    cfg: DecomposeConfig | None = None,
) -> ParetoResult:
    """Pareto drag for one window straight from fact rows.

    Aggregates the window's rows to ``(key, canonical ad type)``, excludes
    the profile's no-attribution ad types from the ranking and calls
    :func:`pareto_drag`.  With ``entities`` each row is annotated with the
    entity's ``name`` and ``status`` (pass the *latest* status; stopped
    campaigns are then visibly history).
    """
    ents = list(entities or ())
    canon = _ad_type_lookup(profile, ents)
    kf = _keyfn(key)
    by_id = {e.entity_id: e for e in ents}
    items: list[dict[str, Any]] = []
    for r in filter_rows(rows, window):
        k = kf(r)
        d: dict[str, Any] = {"key": k, "group": canon(r), "spend": r.spend, "sales": r.sales}
        e = by_id.get(r.entity_id)
        if e is not None and k == r.entity_id:
            d["name"] = e.name or r.entity_name or e.entity_id
            d["status"] = e.status
        items.append(d)
    excl = profile.no_attribution_types() if profile is not None else frozenset()
    return pareto_drag(items, exclude_groups=excl, cfg=cfg)


# ---------------------------------------------------------------------------
# composition: entry / exit
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompositionItem:
    """One entity in the composition split (``impact`` in blended-ROAS points)."""

    key: Hashable
    kind: str                      # "entrant" | "exit" | "continuing"
    spend_prior: float
    spend_current: float
    roas_prior: float
    roas_current: float
    impact: float                  # entrant: drag; exit: relief; continuing: mix + rate impact


@dataclass(frozen=True)
class CompositionResult:
    """Blended-ROAS change split into four exact buckets."""

    roas_prior: float
    roas_current: float
    cont_rate: float               # continuing entities' own ROAS moves
    cont_mix: float                # exact residual: budget re-weighting (also absorbs sub-threshold entities)
    entrants: float                # net effect of new entities, measured against the prior blended ROAS
    exits: float                   # net relief (or loss) from entities that stopped
    n_continuing: int
    n_entrants: int
    n_exits: int
    n_below_min: int               # entities under the spend floor in both periods (folded into cont_mix)
    top_entrants: tuple[CompositionItem, ...] = ()
    top_exits: tuple[CompositionItem, ...] = ()
    top_movers: tuple[CompositionItem, ...] = ()

    @property
    def delta(self) -> float:
        return self.roas_current - self.roas_prior

    @property
    def residual(self) -> float:
        """Buckets minus headline; floating-point noise only."""
        return self.cont_rate + self.cont_mix + self.entrants + self.exits - self.delta

    def as_dict(self) -> dict[str, Any]:
        return {
            "roas_prev": self.roas_prior, "roas_cur": self.roas_current, "delta": round(self.delta, 3),
            "cont_rate": round(self.cont_rate, 3), "cont_mix": round(self.cont_mix, 3),
            "entrants": round(self.entrants, 3), "exits": round(self.exits, 3),
            "n_cont": self.n_continuing, "n_ent": self.n_entrants, "n_exit": self.n_exits,
            "n_below_min": self.n_below_min,
        }


def entry_exit(
    items: Iterable[ShiftShareInput | Mapping[str, Any]],
    *,
    cfg: DecomposeConfig | None = None,
    economics: Economics | None = None,
) -> CompositionResult | None:
    """Split a blended-ROAS change into continuing, entrant and exit effects.

    Decision: was the account's move caused by what existing entities did,
    or by *which* entities exist (launches, stops, re-weighting)?  Lever
    models cannot see composition; without this a move caused by new
    campaigns is blamed on levers.

    With ``MIN = economics.money(cfg.composition_min_spend)``, spend shares
    ``w`` and entity ROAS ``r`` (0 without spend), ``Rp`` / ``Rc`` the
    blended ROAS of all items::

        continuing (spend > MIN in both): cont_rate += (wp + wc)/2 * (rc - rp)
        entrant    (current only):        entrants  += wc * (rc - Rp)
        exit       (prior only):          exits     += wp * (Rp - rp)
        cont_mix = (Rc - Rp) - cont_rate - entrants - exits          (exact residual)

    Entrants are measured against the *prior* blended ROAS so a low-ROAS
    entrant reads as a drag (negative), not as a positive level
    contribution.  ``cont_mix`` is labelled budget re-weighting; entities
    under ``MIN`` in both periods are folded into it and counted in
    ``n_below_min`` (never dropped silently).

    Returns ``None`` when either period has no spend, else a
    :class:`CompositionResult` with the top ``cfg.composition_top_n``
    entrants (worst drag first), exits (largest ``|relief|`` first) and
    continuing movers (worst impact first).
    """
    c = cfg or _DEFAULT
    eco = economics or Economics()
    floor = eco.money(c.composition_min_spend)
    merged: dict[Hashable, list[float]] = {}
    for raw in items:
        it = _coerce(raw)
        a = merged.setdefault(it.key, [0.0, 0.0, 0.0, 0.0])
        a[0] += _num(it.weight_prior)
        a[1] += _num(it.value_prior)
        a[2] += _num(it.weight_current)
        a[3] += _num(it.value_current)
    sp_tot = sum(a[0] for a in merged.values())
    sc_tot = sum(a[2] for a in merged.values())
    if sp_tot <= 0 or sc_tot <= 0:
        return None
    r_prior = sum(a[1] for a in merged.values()) / sp_tot
    r_cur = sum(a[3] for a in merged.values()) / sc_tot
    cont_rate = ent_net = exit_net = 0.0
    ents: list[CompositionItem] = []
    exits: list[CompositionItem] = []
    movers: list[CompositionItem] = []
    n_below = 0
    for key, (sp, sa_p, sc, sa_c) in merged.items():
        wp, wc = sp / sp_tot, sc / sc_tot
        rp = sa_p / sp if sp > 0 else 0.0
        rc = sa_c / sc if sc > 0 else 0.0
        in_p, in_c = sp > floor, sc > floor
        if in_p and in_c:
            rate = (wp + wc) / 2.0 * (rc - rp)
            cont_rate += rate
            movers.append(CompositionItem(key, "continuing", sp, sc, rp, rc,
                                          (wc - wp) * (rc + rp) / 2.0 + rate))
        elif in_c:
            drag = wc * (rc - r_prior)
            ent_net += drag
            ents.append(CompositionItem(key, "entrant", sp, sc, rp, rc, drag))
        elif in_p:
            relief = wp * (r_prior - rp)
            exit_net += relief
            exits.append(CompositionItem(key, "exit", sp, sc, rp, rc, relief))
        else:
            n_below += 1
    cont_mix = (r_cur - r_prior) - cont_rate - ent_net - exit_net
    ents.sort(key=lambda x: x.impact)
    exits.sort(key=lambda x: -abs(x.impact))
    movers.sort(key=lambda x: x.impact)
    n = c.composition_top_n
    return CompositionResult(
        roas_prior=r_prior, roas_current=r_cur, cont_rate=cont_rate, cont_mix=cont_mix,
        entrants=ent_net, exits=exit_net,
        n_continuing=len(movers), n_entrants=len(ents), n_exits=len(exits), n_below_min=n_below,
        top_entrants=tuple(ents[:n]), top_exits=tuple(exits[:n]), top_movers=tuple(movers[:n]),
    )


def entry_exit_rows(
    rows: Iterable[PerfRow],
    windows: WindowPair,
    *,
    key: _KeySpec = "entity_id",
    cfg: DecomposeConfig | None = None,
    economics: Economics | None = None,
) -> CompositionResult | None:
    """:func:`entry_exit` straight from fact rows, grouped by ``key`` over a window pair."""
    return entry_exit(period_inputs(rows, windows, key), cfg=cfg, economics=economics)


# ---------------------------------------------------------------------------
# ASP mix versus price
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AspSplit:
    """Average-selling-price move split into product mix and own-price change."""

    asp_prior: float
    asp_current: float
    delta: float                   # asp_current - asp_prior (= mix + price)
    mix: float                     # unit share moved between cheaper and dearer products
    price: float                   # products' own unit prices moved
    mix_pct: float | None          # % of delta; None when delta is 0
    price_pct: float | None
    emitted: bool                  # both materiality gates passed
    reason: str | None             # why not emitted: "small_move" | "below_min_delta" | "no_units"
    movers: tuple[ShiftShareItem, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "asp_prev": self.asp_prior, "asp_cur": self.asp_current, "delta": self.delta,
            "mix": self.mix, "price": self.price, "mix_pct": self.mix_pct, "price_pct": self.price_pct,
            "emitted": self.emitted, "reason": self.reason,
            "movers": [{"key": m.key, "mix": m.mix, "price": m.rate, "total": m.total} for m in self.movers],
        }


def asp_mix_price(
    products: Iterable[ShiftShareInput | Mapping[str, Any]],
    *,
    window_asp_prior: float | None = None,
    window_asp_current: float | None = None,
    cfg: DecomposeConfig | None = None,
    economics: Economics | None = None,
) -> AspSplit:
    """Split an ASP move into product mix and price, exactly.

    Decision: did the average selling price move because shoppers bought a
    different mix of products, or because the products' own prices changed
    (discounting)?  Midpoint shift-share over products with ``u`` = unit
    share and ``p`` = unit price (sales / units)::

        mix   = sum((u_c - u_p) * pbar)
        price = sum((u_p + u_c)/2 * (p_c - p_p))
        mix + price == ASP_c - ASP_p                      (exact)

    ``pbar`` is the midpoint price, or the single available price for a
    product sold in only one window (its price term is then 0): this is
    :func:`shift_share` with ``one_sided="carry"``.

    ``products`` carry per-product ``units_prior, sales_prior,
    units_current, sales_current`` (or the generic ``weight_* / value_*``).
    Materiality gates (``emitted`` is False and ``reason`` set otherwise):
    the window ASP moved at least ``cfg.asp_min_move_pct`` percent, judged
    on ``window_asp_*`` when supplied (the ad-attributed window ASP) else
    on the product data, and ``|delta|`` exceeds
    ``economics.money(cfg.asp_min_abs_delta)``.  The numbers are returned
    either way.  ``movers`` are products contributing at least
    ``cfg.asp_mover_min_pct`` percent of ``|delta|``, largest first.

    Returns an :class:`AspSplit`.
    """
    c = cfg or _DEFAULT
    eco = economics or Economics()
    res = shift_share(products, one_sided="carry")
    units_p = sum(i.weight_prior for i in res.items if i.weight_prior > 0)
    units_c = sum(i.weight_current for i in res.items if i.weight_current > 0)
    delta = res.delta
    mix_pct = 100.0 * res.mix_total / delta if delta else None
    price_pct = 100.0 * res.rate_total / delta if delta else None
    reason: str | None = None
    if units_p <= 0 or units_c <= 0:
        reason = "no_units"
    else:
        ref_p = window_asp_prior if window_asp_prior is not None else res.rate_prior
        ref_c = window_asp_current if window_asp_current is not None else res.rate_current
        move_pct = abs(100.0 * (ref_c / ref_p - 1.0)) if ref_p else 0.0
        if move_pct < c.asp_min_move_pct:
            reason = "small_move"
        elif not abs(delta) > eco.money(c.asp_min_abs_delta):
            reason = "below_min_delta"
    movers: tuple[ShiftShareItem, ...] = ()
    if delta:
        cut = c.asp_mover_min_pct / 100.0 * abs(delta)
        movers = tuple(sorted((i for i in res.items if abs(i.total) >= cut), key=lambda i: -abs(i.total)))
    return AspSplit(
        asp_prior=res.rate_prior, asp_current=res.rate_current, delta=delta,
        mix=res.mix_total, price=res.rate_total, mix_pct=mix_pct, price_pct=price_pct,
        emitted=reason is None, reason=reason, movers=movers,
    )


# ---------------------------------------------------------------------------
# segment growth decomposition
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BrandShareRow:
    """One row of a segment competition table (shares in percentage points)."""

    brand: str
    share_pct: float               # current share of segment sales, 0..100
    share_delta_pp: float = 0.0    # change versus the prior equal window, in share points
    is_own: bool = False


@dataclass(frozen=True)
class TierRow:
    """One price tier (or sub-segment) of the segment."""

    name: str
    sales_current: float
    growth_pct: float              # the tier's own period-over-period growth, percent


@dataclass(frozen=True)
class LaunchRow:
    """One recent product launch in the segment."""

    name: str
    sales_current: float
    trend: str = "steady"          # "ramping" | "steady" | "fading"


@dataclass(frozen=True)
class ShareMove:
    """A brand's share move valued at current segment size."""

    brand: str
    share_delta_pp: float
    value: float                   # share_delta_pp / 100 * segment sales (current)


@dataclass(frozen=True)
class TierShift:
    """A tier's weight in the segment now versus before, in share points."""

    name: str
    weight_now: float
    weight_before: float
    shift_pp: float


@dataclass(frozen=True)
class GrowthDecomposition:
    """Where a segment's growth came from: market, mix, share exchange, entrants."""

    segment_current: float
    segment_prior: float
    delta: float                   # (a) market growth in money
    growth_pct: float
    own: ShareMove | None
    gainer: ShareMove | None       # (c) biggest incumbent gainer
    loser: ShareMove | None        # (c) biggest incumbent loser
    entrants_count: int            # (d)
    entrants_value: float
    entrant_names: tuple[str, ...]
    mix_shift: tuple[TierShift, ...] | None   # (b) None when immaterial or fewer than two tiers
    ramping_count: int
    ramping_sales: float

    def as_dict(self) -> dict[str, Any]:
        def mv(m: ShareMove | None) -> dict[str, Any] | None:
            return None if m is None else dict(m.__dict__)

        return {
            "segment_current": self.segment_current, "segment_prior": self.segment_prior,
            "delta": self.delta, "growth_pct": self.growth_pct, "own": mv(self.own),
            "gainer": mv(self.gainer), "loser": mv(self.loser),
            "entrants": {"n": self.entrants_count, "value": self.entrants_value,
                         "names": list(self.entrant_names)},
            "mix_shift": None if self.mix_shift is None else [dict(t.__dict__) for t in self.mix_shift],
            "ramping": {"n": self.ramping_count, "sales": self.ramping_sales},
        }


def growth_decomposition(
    segment_current: float,
    growth_pct: float,
    brands: Iterable[BrandShareRow | Mapping[str, Any]] = (),
    tiers: Iterable[TierRow | Mapping[str, Any]] = (),
    launches: Iterable[LaunchRow | Mapping[str, Any]] = (),
    *,
    cfg: DecomposeConfig | None = None,
) -> GrowthDecomposition | None:
    """Split a segment's growth into market, mix, share exchange and entrants.

    Inputs are the segment's current sales, its period-over-period growth
    in percent, the visible rows of the competition table, the tier rows
    and the recent launches.  Reads:

    * (a) market: ``prior = current / (1 + growth/100)``, ``delta = current - prior``.
    * entrant = a non-own brand with prior share ``share - delta`` below
      ``cfg.growth_entrant_prior_share_max`` and current share at least
      ``cfg.growth_entrant_share_min`` (share points).
    * (c) share exchange over incumbents only (non-own, non-entrant): the
      biggest gainer and biggest loser by share delta, each valued
      ``delta_pp / 100 * current``; the own row is valued the same way.  An
      entrant is never also reported as the gainer.  A gainer must have
      gained and a loser must have lost, else that side is ``None``.
    * (d) entrants: count, value ``sum(share / 100 * current)``, first
      ``cfg.growth_entrant_names`` names in table order.
    * (b) mix shift: each tier's weight now minus its weight before, the
      prior size re-derived from the tier's own growth; reported (largest
      absolute shift first) only with at least ``cfg.growth_min_tiers``
      tiers and a largest shift of ``cfg.growth_mix_shift_min_pp`` or more.
    * launches still ramping: count and current sales.

    These are reads of one move from several angles, not additive parts.
    Limit: only the rows passed are read; the unlisted tail of small brands
    sits inside market growth.  Several own rows are aggregated into one.

    Returns ``None`` when ``growth_pct <= -100`` (no prior size can be
    derived), else a :class:`GrowthDecomposition`.
    """
    c = cfg or _DEFAULT
    cur = _num(segment_current)
    g = _num(growth_pct)
    if g <= -100.0:
        return None
    prior = cur / (1.0 + g / 100.0)

    def _brand(b: BrandShareRow | Mapping[str, Any]) -> BrandShareRow:
        if isinstance(b, BrandShareRow):
            return b
        return BrandShareRow(str(b.get("brand", "")), _num(b.get("share_pct")),
                             _num(b.get("share_delta_pp")), bool(b.get("is_own", False)))

    def _tier(t: TierRow | Mapping[str, Any]) -> TierRow:
        if isinstance(t, TierRow):
            return t
        return TierRow(str(t.get("name", "")), _num(t.get("sales_current")), _num(t.get("growth_pct")))

    def _launch(x: LaunchRow | Mapping[str, Any]) -> LaunchRow:
        if isinstance(x, LaunchRow):
            return x
        return LaunchRow(str(x.get("name", "")), _num(x.get("sales_current")), str(x.get("trend", "steady")))

    def _move(b: BrandShareRow) -> ShareMove:
        return ShareMove(b.brand, b.share_delta_pp, b.share_delta_pp / 100.0 * cur)

    rows = [_brand(b) for b in brands]
    own_rows = [b for b in rows if b.is_own]
    own: ShareMove | None = None
    if own_rows:
        d = sum(b.share_delta_pp for b in own_rows)
        own = ShareMove(own_rows[0].brand if len(own_rows) == 1 else "own", d, d / 100.0 * cur)
    entrants = [
        b for b in rows
        if not b.is_own
        and (b.share_pct - b.share_delta_pp) < c.growth_entrant_prior_share_max
        and b.share_pct >= c.growth_entrant_share_min
    ]
    ent_ids = {id(b) for b in entrants}
    incumbents = [b for b in rows if not b.is_own and id(b) not in ent_ids]
    gainer = loser = None
    if incumbents:
        top = max(incumbents, key=lambda b: b.share_delta_pp)
        bottom = min(incumbents, key=lambda b: b.share_delta_pp)
        gainer = _move(top) if top.share_delta_pp > 0 else None
        loser = _move(bottom) if bottom.share_delta_pp < 0 else None

    mix: tuple[TierShift, ...] | None = None
    tier_rows = [_tier(t) for t in tiers]
    if len(tier_rows) >= c.growth_min_tiers and all(t.growth_pct > -100.0 for t in tier_rows):
        now_tot = sum(t.sales_current for t in tier_rows)
        before = [t.sales_current / (1.0 + t.growth_pct / 100.0) for t in tier_rows]
        before_tot = sum(before)
        if now_tot > 0 and before_tot > 0:
            shifts = [
                TierShift(t.name, 100.0 * t.sales_current / now_tot, 100.0 * b / before_tot,
                          100.0 * t.sales_current / now_tot - 100.0 * b / before_tot)
                for t, b in zip(tier_rows, before)
            ]
            if max(abs(s.shift_pp) for s in shifts) >= c.growth_mix_shift_min_pp:
                mix = tuple(sorted(shifts, key=lambda s: -abs(s.shift_pp)))

    ramping = [x for x in (_launch(x) for x in launches) if x.trend == c.growth_ramping_label]
    return GrowthDecomposition(
        segment_current=cur, segment_prior=prior, delta=cur - prior, growth_pct=g,
        own=own, gainer=gainer, loser=loser,
        entrants_count=len(entrants),
        entrants_value=sum(b.share_pct / 100.0 * cur for b in entrants),
        entrant_names=tuple(b.brand for b in entrants[: c.growth_entrant_names]),
        mix_shift=mix,
        ramping_count=len(ramping), ramping_sales=sum(x.sales_current for x in ramping),
    )
