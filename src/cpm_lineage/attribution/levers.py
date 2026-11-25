"""The lever set, the daily lever panel and feature preparation.

A *lever* is one entity x day scalar.  Eight are things the advertiser
controls (tier 1) and three are outside conditions (tier 2) that stay in
the model as controls but can never be the subject of an action.

Design rules behind the set:

* **Two tiers.**  Tier-2 levers avoid omitted-variable bias; they only ever
  produce a context note.
* **No mirrors.**  Competitor paid share of voice is one minus the own
  share, so only the own side is modelled.
* **n - 1 of a partition.**  The targeting mix (own / competitor / generic)
  sums to one; only the adversarial share is modelled.
* **Matched definitions.**  Own and competitor availability and price must
  come from the same feed at the same grain.

Position semantics are platform specific: the adapter states whether a
lower number is better (``PositionSemantics.lower_is_better``); the sign
bound in the ridge flips accordingly.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Mapping, Sequence

import numpy as np

from ..config import PositionConfig
from ..metrics import position_is_valid
from ..schema import MarketRow, PerfRow, ShelfRow, TargetingUnit, Window
from .collinearity import fold_price_cluster
from .config import AttributionConfig

# --------------------------------------------------------------------------- lever set

LEVERS: tuple[str, ...] = (
    "bid",                        # spend-weighted mean bid
    "position",                   # impression-weighted mean ad position
    "own_paid_sov",               # own ad impressions / all ad impressions on the entity's targeting units
    "own_organic_sov",            # own organic impressions / all organic impressions
    "own_availability",           # own products: available / checks (0..1), optionally velocity weighted
    "competitor_share",           # spend share on targeting units of the adversarial role
    "own_discount",               # 1 - avg price / avg list price, own products
    "price_gap",                  # avg own price / avg competitor price
    "log_category_impressions",   # ln of total impressions on the entity's targeting units (demand control)
    "comp_availability",          # competitor products: available / checks
    "comp_discount",              # 1 - avg price / avg list price, competitor products
)

TIER2: frozenset[str] = frozenset({"log_category_impressions", "comp_discount", "comp_availability"})
TIER1: tuple[str, ...] = tuple(f for f in LEVERS if f not in TIER2)

#: display class: "int" = you control it, "ext" = outside condition, "pricing" = your pricing.
DRIVER_CLASS: dict[str, str] = {
    "bid": "int", "own_paid_sov": "int", "own_organic_sov": "int", "own_availability": "int",
    "position": "int", "competitor_share": "int",
    "log_category_impressions": "ext", "comp_availability": "ext", "comp_discount": "ext",
    "own_discount": "pricing", "price_gap": "pricing",
}

#: fast-moving levers kept in short mode (slow levers barely move inside 30 days).
SHORT_KEEP: frozenset[str] = frozenset({
    "bid", "own_paid_sov", "position", "own_availability", "price_gap",
    "log_category_impressions", "competitor_share",
})

SHARE_LEVERS: frozenset[str] = frozenset({
    "own_availability", "comp_availability", "own_paid_sov", "own_organic_sov", "competitor_share",
})
DISCOUNT_LEVERS: frozenset[str] = frozenset({"own_discount", "comp_discount"})

#: levers given a geometric carry-over variant in the first-difference cross-check.
ADSTOCK_LEVERS: frozenset[str] = frozenset({"own_paid_sov", "own_organic_sov", "log_category_impressions", "bid"})

#: the channel a lever acts on most directly, used by the raw facts.
ALIGNED_CHANNEL: dict[str, str] = {"bid": "cpm", "own_availability": "icr", "own_discount": "asp", "position": "icr"}

_CFG = AttributionConfig()
_NAN = float("nan")


def is_tier2(lever: str) -> bool:
    """True when ``lever`` is an outside condition that may never carry an action."""
    return lever in TIER2


# --------------------------------------------------------------------------- panel

@dataclass
class LeverPanel:
    """Entity x day panel: additive measures plus one array per lever.

    ``levers[name][i]`` is ``nan`` when the lever is unknown on ``dates[i]``.
    ``extras`` carries additive daily series the raw facts need
    (``category_impressions``, ``own_ad_impressions``, ``ad_pool_impressions``).
    All arrays are aligned with ``dates`` (sorted ascending).
    """

    dates: list[date]
    spend: np.ndarray
    sales: np.ndarray
    impressions: np.ndarray
    units: np.ndarray
    levers: dict[str, np.ndarray] = field(default_factory=dict)
    extras: dict[str, np.ndarray] = field(default_factory=dict)

    def __post_init__(self) -> None:
        n = len(self.dates)
        for name in ("spend", "sales", "impressions", "units"):
            arr = np.asarray(getattr(self, name), dtype=float)
            if arr.shape != (n,):
                raise ValueError(f"{name} must have one value per date")
            setattr(self, name, arr)
        self.levers = {k: _as_array(v, n, k) for k, v in self.levers.items()}
        self.extras = {k: _as_array(v, n, k) for k, v in self.extras.items()}

    def __len__(self) -> int:
        return len(self.dates)

    @property
    def model_mask(self) -> np.ndarray:
        """Days usable by the log-level models: spend, impressions, units and sales all positive.

        Layer-1 window totals use **every** day; only the regressions need
        positive channels (a zero-sale day has no finite ``ln``).
        """
        return (self.spend > 0) & (self.impressions > 0) & (self.units > 0) & (self.sales > 0)

    def window_mask(self, window: Window) -> np.ndarray:
        """Boolean mask of the days inside ``window`` (inclusive)."""
        return np.array([window.start <= d <= window.end for d in self.dates], dtype=bool)

    def clip(self, start: date, end: date) -> "LeverPanel":
        """The panel restricted to the days in ``[start, end]`` (inclusive)."""
        keep = np.array([start <= d <= end for d in self.dates], dtype=bool)
        if keep.all():
            return self
        return LeverPanel(dates=[d for d, k in zip(self.dates, keep) if k], spend=self.spend[keep],
                          sales=self.sales[keep], impressions=self.impressions[keep], units=self.units[keep],
                          levers={k: v[keep] for k, v in self.levers.items()},
                          extras={k: v[keep] for k, v in self.extras.items()})

    def channel(self, name: str) -> np.ndarray:
        """Daily channel values: ``icr`` = units / impressions, ``asp`` = sales / units, ``cpm`` = 1000 x spend / impressions."""
        with np.errstate(divide="ignore", invalid="ignore"):
            if name == "icr":
                return self.units / self.impressions
            if name == "asp":
                return self.sales / self.units
            if name == "cpm":
                return 1000.0 * self.spend / self.impressions
        raise KeyError(name)


def _as_array(values: Sequence[float], n: int, name: str) -> np.ndarray:
    arr = np.array([_NAN if v is None else v for v in values], dtype=float) if not isinstance(values, np.ndarray) \
        else values.astype(float)
    if arr.shape != (n,):
        raise ValueError(f"lever {name!r} must have one value per date")
    return arr


def _ratio(num: float, den: float) -> float:
    return num / den if den > 0 else _NAN


def build_lever_panel(
    perf_rows: Iterable[PerfRow],
    *,
    market_rows: Iterable[MarketRow] = (),
    shelf_rows: Iterable[ShelfRow] = (),
    targeting_units: Iterable[TargetingUnit] = (),
    entity_id: str | None = None,
    position: PositionConfig | None = None,
    cfg: AttributionConfig | None = None,
) -> LeverPanel:
    """Build the entity x day lever panel from the raw feeds.

    ``entity_id=None`` pools every entity (the account panel used for the
    prior).  Days are kept when ``spend > 0 and impressions > 0``.  Every
    lever degrades to ``nan`` when its feed is missing; nothing fails.

    Formulas (per day):

    * ``bid`` = sum(spend x bid) / sum(spend) over rows with a positive bid
    * ``position`` = sum(impressions x position) / sum(impressions) over valid positions
    * ``own_paid_sov`` / ``own_organic_sov`` = own / all ad (organic) impressions on the entity's targeting units
    * ``log_category_impressions`` = ln(max(total impressions on those units, 1))
    * ``own_availability`` / ``comp_availability`` = sum(w x available) / sum(w x checks), ``w`` = the row weight
      (1 for inactive products) so fast movers out of stock count for what they sell
    * ``own_discount`` / ``comp_discount`` = 1 - mean(price) / mean(list price)
    * ``price_gap`` = mean own price / mean competitor price
    * ``competitor_share`` = spend on units of the adversarial role / spend on all non-placement units
      (0 when there is no such spend; units without a role default to ``generic``)

    Own shelf rows are restricted to the entity's advertised products when the
    performance rows name them; competitor rows are every ``is_own=False`` row
    the adapter supplies.

    Returns a :class:`LeverPanel`.
    """
    cfg = cfg or _CFG
    rows = [r for r in perf_rows if entity_id is None or r.entity_id == entity_id]
    role_exact: dict[tuple[str | None, str], TargetingUnit] = {}
    role_any: dict[str, TargetingUnit] = {}
    for u in targeting_units:
        role_exact[(u.entity_id, u.value)] = u
        role_any.setdefault(u.value, u)

    day: dict[date, dict[str, float]] = {}
    unit_set: set[str] = set()
    product_set: set[str] = set()
    for r in rows:
        d = day.setdefault(r.date, dict.fromkeys(
            ("spend", "sales", "impr", "units", "bw", "bs", "pw", "pi", "adv", "kw"), 0.0))
        d["spend"] += r.spend
        d["sales"] += r.sales
        d["impr"] += r.impressions
        d["units"] += r.units or 0.0
        if r.bid is not None and r.bid > 0 and r.spend > 0:
            d["bw"] += r.spend * r.bid
            d["bs"] += r.spend
        if r.position is not None and r.impressions > 0 and position_is_valid(r.position, position):
            d["pw"] += r.impressions * r.position
            d["pi"] += r.impressions
        if r.product_id:
            product_set.add(r.product_id)
        if r.targeting_unit:
            tu = role_exact.get((r.entity_id, r.targeting_unit)) or role_any.get(r.targeting_unit)
            role = tu.role if tu else "generic"
            if (tu is not None and tu.is_placement) or role == "placement":
                continue
            unit_set.add(r.targeting_unit)
            d["kw"] += r.spend
            if str(role).startswith(cfg.adversarial_role):
                d["adv"] += r.spend

    dates = sorted(k for k, v in day.items() if v["spend"] > 0 and v["impr"] > 0)

    mk: dict[date, list[float]] = {}
    for m in market_rows:
        if m.targeting_unit not in unit_set:
            continue
        a = mk.setdefault(m.date, [0.0, 0.0, 0.0, 0.0, 0.0])
        a[0] += m.ad_impressions
        a[1] += m.organic_impressions
        a[2] += m.impressions
        if m.is_own:
            a[3] += m.ad_impressions
            a[4] += m.organic_impressions

    # shelf: [w*avail, w*checks, price_sum, price_n, list_sum, list_n] per side
    sh: dict[date, dict[bool, list[float]]] = {}
    for s in shelf_rows:
        if s.is_own and product_set and s.product_id not in product_set:
            continue
        a = sh.setdefault(s.date, {True: [0.0] * 6, False: [0.0] * 6})[bool(s.is_own)]
        w = (s.weight if s.weight and s.weight > 0 else 1.0) if s.is_active else 1.0
        if s.available_count is not None and s.check_count is not None and s.check_count > 0:
            a[0] += w * s.available_count
            a[1] += w * s.check_count
        if s.price is not None and s.price > 0:
            a[2] += s.price
            a[3] += 1
        if s.list_price is not None and s.list_price > 0 and s.price is not None and s.price > 0:
            a[4] += s.list_price
            a[5] += 1

    def col(fn) -> np.ndarray:
        return np.array([fn(d) for d in dates], dtype=float)

    def shelf_val(d: date, own: bool, what: str) -> float:
        a = sh.get(d, {}).get(own)
        if a is None:
            return _NAN
        if what == "avail":
            return _ratio(a[0], a[1])
        if what == "price":
            return _ratio(a[2], a[3])
        if a[3] <= 0 or a[5] <= 0:
            return _NAN
        return 1.0 - (a[2] / a[3]) / (a[4] / a[5])

    def price_gap(d: date) -> float:
        own, comp = shelf_val(d, True, "price"), shelf_val(d, False, "price")
        return own / comp if own == own and comp == comp and comp > 0 else _NAN

    levers = {
        "bid": col(lambda d: _ratio(day[d]["bw"], day[d]["bs"])),
        "position": col(lambda d: _ratio(day[d]["pw"], day[d]["pi"])),
        "own_paid_sov": col(lambda d: _ratio(mk[d][3], mk[d][0]) if d in mk else _NAN),
        "own_organic_sov": col(lambda d: _ratio(mk[d][4], mk[d][1]) if d in mk else _NAN),
        "own_availability": col(lambda d: shelf_val(d, True, "avail")),
        "competitor_share": col(lambda d: day[d]["adv"] / day[d]["kw"] if day[d]["kw"] > 0 else 0.0),
        "own_discount": col(lambda d: shelf_val(d, True, "disc")),
        "price_gap": col(price_gap),
        "log_category_impressions": col(lambda d: math.log(max(mk[d][2], 1.0)) if d in mk else _NAN),
        "comp_availability": col(lambda d: shelf_val(d, False, "avail")),
        "comp_discount": col(lambda d: shelf_val(d, False, "disc")),
    }
    extras = {
        "category_impressions": col(lambda d: mk[d][2] if d in mk else _NAN),
        "own_ad_impressions": col(lambda d: mk[d][3] if d in mk else _NAN),
        "ad_pool_impressions": col(lambda d: mk[d][0] if d in mk else _NAN),
    }
    return LeverPanel(
        dates=dates,
        spend=col(lambda d: day[d]["spend"]), sales=col(lambda d: day[d]["sales"]),
        impressions=col(lambda d: day[d]["impr"]), units=col(lambda d: day[d]["units"]),
        levers=levers, extras=extras,
    )


# --------------------------------------------------------------------------- preparation

def derive_levers(columns: Mapping[str, Sequence[float]]) -> dict[str, np.ndarray]:
    """Add the two derived levers when their ingredients are supplied.

    ``log_category_impressions = ln(max(category_impressions, 1))`` and
    ``price_gap = own_price / comp_price``; existing lever columns win.
    Returns a new dict of float arrays restricted to known lever names.
    """
    cols = {k: np.asarray([_NAN if v is None else v for v in vals], dtype=float) for k, vals in columns.items()}
    out = {k: v for k, v in cols.items() if k in LEVERS}
    if "log_category_impressions" not in out and "category_impressions" in cols:
        out["log_category_impressions"] = np.log(np.maximum(cols["category_impressions"], 1.0))
    if "price_gap" not in out and "own_price" in cols and "comp_price" in cols:
        with np.errstate(divide="ignore", invalid="ignore"):
            out["price_gap"] = cols["own_price"] / cols["comp_price"]
    return out


def sanity(lever: str, values: Sequence[float], cfg: AttributionConfig | None = None) -> np.ndarray:
    """Range check: out-of-range and non-finite values become ``nan`` instead of entering the model.

    * ``position`` outside ``[position_sanity_min, position_sanity_max]``
    * share-type levers outside ``[0, 1]``
    * discounts outside ``[-0.5, 0.95]``
    * ``price_gap`` outside ``[0.1, 10]``
    * ``bid <= 0``

    Rationale: one mis-joined field is enough to put an absurd number into a
    recommendation.  Returns a new float array.
    """
    cfg = cfg or _CFG
    v = np.array(values, dtype=float)
    v[~np.isfinite(v)] = _NAN
    with np.errstate(invalid="ignore"):
        if lever == "position":
            bad = (v < cfg.position_sanity_min) | (v > cfg.position_sanity_max)
        elif lever in SHARE_LEVERS:
            bad = (v < cfg.share_min) | (v > cfg.share_max)
        elif lever in DISCOUNT_LEVERS:
            bad = (v < cfg.discount_min) | (v > cfg.discount_max)
        elif lever == "price_gap":
            bad = (v < cfg.price_gap_min) | (v > cfg.price_gap_max)
        elif lever == "bid":
            bad = v <= 0
        else:
            bad = np.zeros(v.shape, dtype=bool)
    v[bad] = _NAN
    return v


@dataclass
class PreparedLevers:
    """Result of :func:`prepare_levers`.

    ``raw`` is the range-checked, pre-imputation frame (the collinearity
    guard and the raw facts read it); ``filled`` is the imputed frame the
    models read.  ``live`` are the levers that enter the model, in
    :data:`LEVERS` order.
    """

    raw: dict[str, np.ndarray]
    filled: dict[str, np.ndarray]
    coverage: dict[str, float]
    imputed: dict[str, float]
    live: list[str]
    suppressed: list[str]
    folded: list[str]
    guard: dict
    thin: bool
    n_rows: int

    def matrix(self, levers: Sequence[str] | None = None) -> np.ndarray:
        """Imputed design matrix, one column per lever in ``levers`` (default: the live ones)."""
        names = list(self.live if levers is None else levers)
        if not names:
            return np.zeros((self.n_rows, 0))
        return np.column_stack([self.filled[f] for f in names])


def prepare_levers(levers: Mapping[str, Sequence[float]], *, short: bool = False,
                   cfg: AttributionConfig | None = None, n_rows: int | None = None) -> PreparedLevers:
    """Decide which levers enter the model and build the imputed design frame.

    Steps, in order:

    1. derive ``log_category_impressions`` / ``price_gap`` if only their ingredients were given;
    2. non-finite and out-of-range values become missing (:func:`sanity`);
    3. ``coverage[f]`` = share of rows with a real, non-zero value; ``imputed[f]`` = share of missing rows;
    4. impute missing with the column median, then 0;
    5. ``live`` = levers with ``coverage > coverage_min``;
    6. short mode keeps only :data:`SHORT_KEEP`;
    7. levers with ``imputed > impute_max`` are *suppressed* (removed from the model, kept as raw facts);
    8. the price-cluster collinearity guard may *fold* the two discounts into ``price_gap``;
    9. ``thin = len(live) < thin_min_levers``.

    A zero normally counts as "not covered", which is right for a mix share
    that is structurally 0 but would drop a genuine 0% discount.  Levers named in
    ``cfg.zero_is_real`` count zeros as real observations.

    Caveat: the median uses every row, so imputed values see both windows.

    ``n_rows`` gives the row count when ``levers`` may be empty or partial:
    a lever that was not supplied becomes an all-missing column of that
    length (never live), so the result always has one value per row.

    Returns a :class:`PreparedLevers`.
    """
    cfg = cfg or _CFG
    cols = derive_levers(levers)
    n = len(next(iter(cols.values()))) if cols else 0
    if n_rows is not None:
        if cols and n != n_rows:
            raise ValueError("lever columns must have n_rows values")
        n = int(n_rows)
    raw: dict[str, np.ndarray] = {}
    filled: dict[str, np.ndarray] = {}
    cov: dict[str, float] = {}
    imp: dict[str, float] = {}
    for f in LEVERS:
        v = sanity(f, cols[f], cfg) if f in cols else np.full(n, _NAN)
        raw[f] = v
        null = np.isnan(v)
        real = ~null if f in cfg.zero_is_real else (~null & (v != 0))
        cov[f] = float(real.mean()) if n else 0.0
        imp[f] = float(null.mean()) if n else 1.0
        med = float(np.nanmedian(v)) if n and not null.all() else 0.0
        filled[f] = np.where(null, med, v)
    live = [f for f in LEVERS if cov[f] > cfg.coverage_min]
    if short:
        live = [f for f in live if f in SHORT_KEEP]
    suppressed = [f for f in live if imp[f] > cfg.impute_max]
    live = [f for f in live if f not in suppressed]
    live, guard = fold_price_cluster(raw, live, cfg)
    return PreparedLevers(raw=raw, filled=filled, coverage=cov, imputed=imp, live=live,
                          suppressed=suppressed, folded=list(guard["folded"]), guard=guard,
                          thin=len(live) < cfg.thin_min_levers, n_rows=n)


# --------------------------------------------------------------------------- raw facts

def _wmean(v: np.ndarray, mask: np.ndarray, min_days: int) -> float | None:
    x = v[mask]
    x = x[np.isfinite(x)]
    return float(x.mean()) if len(x) >= min_days else None


def raw_facts(
    raw: Mapping[str, Sequence[float]],
    base_mask: Sequence[bool],
    cmp_mask: Sequence[bool],
    *,
    channel_moves: Mapping[str, float] | None = None,
    suppressed: Sequence[str] = (),
    extras: Mapping[str, Sequence[float]] | None = None,
    impressions: Sequence[float] | None = None,
    cfg: AttributionConfig | None = None,
) -> list[dict]:
    """Model-independent statements about what moved between the two windows.

    Computed from the **pre-imputation** frame so they stand even when the
    model ignored or suppressed a lever.  Each window needs
    ``fact_min_days`` real days of the lever.

    * share-type levers: a fact when the window means differ by ``fact_share_points`` (7 points);
    * ``bid``: a relative change of ``fact_bid_rel`` (20%); other levers ``fact_other_rel`` (25%);
    * if the lever's aligned channel (``channel_moves`` = relative change of ``icr`` / ``asp`` / ``cpm``)
      moved at least ``fact_align_rel``, it is attached; a bid move in the same direction as CPM is
      tagged ``self_inflicted`` (the advertiser's own bids, not auction inflation);
    * paid share-of-voice split (needs ``extras['own_ad_impressions']`` and ``['ad_pool_impressions']``):
      when share (window sums) fell ``sov_drop_points``, compare per-day own volume with the per-day ad
      pool: pool up more than 10% and more than own -> ``rivals_scaled_in``; own down more than 10% ->
      ``own_pulled_back``; else ``mixed``;
    * displacement: impressions per day up ``displacement_impr_up`` while paid share fell -> winning more
      but deeper slots.

    Returns at most ``fact_max`` dicts
    ``{"kind", "lever", "from", "to", "change", "unit", "suppressed", ...}`` (no prose: callers render).
    """
    cfg = cfg or _CFG
    b = np.asarray(base_mask, dtype=bool)
    c = np.asarray(cmp_mask, dtype=bool)
    moves = channel_moves or {}
    facts: list[dict] = []
    for f in LEVERS:
        if f not in raw:
            continue
        v = np.asarray(raw[f], dtype=float)
        mb, mc = _wmean(v, b, cfg.fact_min_days), _wmean(v, c, cfg.fact_min_days)
        if mb is None or mc is None:
            continue
        if f in SHARE_LEVERS:
            change, unit = mc - mb, "points"
            hit = abs(change) >= cfg.fact_share_points
        else:
            if abs(mb) < 1e-12:
                continue
            change, unit = mc / mb - 1.0, "relative"
            hit = abs(change) >= (cfg.fact_bid_rel if f == "bid" else cfg.fact_other_rel)
        if not hit:
            continue
        fact: dict = {"kind": "lever_move", "lever": f, "from": mb, "to": mc, "change": change,
                      "unit": unit, "suppressed": f in suppressed}
        ch = ALIGNED_CHANNEL.get(f)
        mv = moves.get(ch) if ch else None
        if mv is not None and abs(mv) >= cfg.fact_align_rel:
            fact["aligned_channel"] = ch
            fact["channel_change"] = mv
            if f == "bid" and mv * change > 0:
                fact["self_inflicted"] = True
        facts.append(fact)

    ex = extras or {}
    if "own_ad_impressions" in ex and "ad_pool_impressions" in ex:
        own = np.asarray(ex["own_ad_impressions"], dtype=float)
        pool = np.asarray(ex["ad_pool_impressions"], dtype=float)
        ok = np.isfinite(own) & np.isfinite(pool)
        nb, nc = int((b & ok).sum()), int((c & ok).sum())
        if nb >= cfg.fact_min_days and nc >= cfg.fact_min_days and pool[b & ok].sum() > 0 and pool[c & ok].sum() > 0:
            sov_b = own[b & ok].sum() / pool[b & ok].sum()
            sov_c = own[c & ok].sum() / pool[c & ok].sum()
            if sov_b - sov_c >= cfg.sov_drop_points:
                own_b, own_c = own[b & ok].sum() / nb, own[c & ok].sum() / nc
                pool_b, pool_c = pool[b & ok].sum() / nb, pool[c & ok].sum() / nc
                d_own = own_c / own_b - 1.0 if own_b > 0 else 0.0
                d_pool = pool_c / pool_b - 1.0
                if d_pool > cfg.sov_pool_up and d_pool > d_own:
                    cause = "rivals_scaled_in"
                elif d_own < -cfg.sov_own_down:
                    cause = "own_pulled_back"
                else:
                    cause = "mixed"
                facts.append({"kind": "sov_split", "lever": "own_paid_sov", "from": float(sov_b), "to": float(sov_c),
                              "change": float(sov_c - sov_b), "unit": "points", "cause": cause,
                              "own_change": float(d_own), "pool_change": float(d_pool), "suppressed": False})
                if impressions is not None:
                    im = np.asarray(impressions, dtype=float)
                    if b.sum() and c.sum() and im[b].mean() > 0:
                        d_im = im[c].mean() / im[b].mean() - 1.0
                        if d_im >= cfg.displacement_impr_up:
                            facts.append({"kind": "displacement", "lever": "position", "from": float(im[b].mean()),
                                          "to": float(im[c].mean()), "change": float(d_im), "unit": "relative",
                                          "suppressed": False})
    return facts[: cfg.fact_max]
