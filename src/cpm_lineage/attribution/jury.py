"""Lever attribution: backbone + cross-check, the per-lever jury, the verdict,
explained share, the demand-evidence gate and elasticity-sized moves.

``attribute`` is the default attribution path (``attribution="ridge_jury"``).
It answers three questions for one entity and one window pair:

1. which levers moved log ROAS, and through which identity channel;
2. whether an independent first-difference model agrees (the *jury*);
3. whether the whole attribution is trustworthy enough to act on (the
   *verdict*: ``confirmed`` / ``directional`` / ``unexplained`` / ``thin-data``).

Attribution here is associational, not causal.  It explains the *change*
between two windows; structural waste (the *level*) is reported separately
by ``cpm_lineage.recommend.structural_bleed``.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Mapping, Sequence

import numpy as np

from ..config import TierConfig
from ..schema import DataTier, WindowPair
from ..windows import route_tier, tier_allows_move_model
from .config import AttributionConfig
from .levers import DRIVER_CLASS, LEVERS, SHARE_LEVERS, TIER2, LeverPanel, PreparedLevers, prepare_levers
from .ridge import (CHANNEL_SIGN, CHANNELS, Prior, adstock, bounded_ridge, choose_lambda, delta_skill,
                    fit_delta, sign_bounds, time_series_splits, zscore)

_CFG = AttributionConfig()

VERDICTS: tuple[str, ...] = ("confirmed", "directional", "unexplained", "thin-data")
#: verdicts under which no lever story may be told.
NO_STORY_VERDICTS: frozenset[str] = frozenset({"unexplained", "thin-data"})
JURY_LABELS: tuple[str, ...] = ("confirm", "contest", "solo", "account-est")
_JURY_RANK = {"confirm": 0, "solo": 1, "account-est": 1, None: 1, "contest": 2}


# --------------------------------------------------------------------------- small pure pieces

def explained_share(contribs: Sequence[float], raw_dln: float, cfg: AttributionConfig | None = None) -> dict:
    """Explained share of the move and the small-move guard.

    ``tot = sum(contrib)``, ``gross = sum(|contrib|)``.  A move is *small*
    when ``|raw_dln| < small_move_dln`` and ``gross > small_move_gross_mult x
    |raw_dln|``: ROAS barely moved but levers were active and offsetting, so
    percentages of the tiny net move would explode.  Then the denominator
    becomes ``copysign(gross, dln)`` ("activity" basis), which keeps the sign
    meaning ("+" = pushed in the direction of the move).

    Denominator fallback (the same rule as the identity layer): ``dln =
    raw_dln`` when ``|raw_dln| > min_dln``, else the model total when that
    exceeds ``min_dln``, else ``min_dln``.  The small-move guard is always
    judged on ``raw_dln``.

    Returns ``{"net", "gross", "residual", "basis", "small_move", "denom"}``
    with ``net = 100 x tot / dln``, ``gross = 100 x gross / |dln|``,
    ``residual = 100 - net`` and ``basis`` in ``"move" | "activity"``.
    """
    cfg = cfg or _CFG
    tot = float(sum(contribs))
    gross = float(sum(abs(c) for c in contribs))
    if abs(raw_dln) > cfg.min_dln:
        dln = raw_dln
    elif abs(tot) > cfg.min_dln:
        dln = tot                                   # flat move: the model total is the denominator
    else:
        dln = cfg.min_dln
    small = abs(raw_dln) < cfg.small_move_dln and gross > cfg.small_move_gross_mult * abs(raw_dln)
    denom = math.copysign(gross, dln) if small else dln
    net = 100.0 * tot / dln
    return {"net": net, "gross": 100.0 * gross / abs(dln), "residual": 100.0 - net,
            "basis": "activity" if small else "move", "small_move": small, "denom": denom}


def jury_label(backbone_share: float, delta_share: float, *, thin: bool = False, has_prior: bool = False,
               cfg: AttributionConfig | None = None) -> str | None:
    """Per-lever agreement between the backbone and the first-difference model.

    Shares are fractions of the denominator (``contrib / denom``).

    * thin entity with an account prior -> ``"account-est"`` (account elasticities, no entity fit)
    * both ``>= jury_materiality`` in size, same sign -> ``"confirm"``
    * both material, opposite sign -> ``"contest"``
    * only the backbone material -> ``"solo"``
    * otherwise ``None``
    """
    cfg = cfg or _CFG
    if thin:
        return "account-est" if has_prior else None
    m = cfg.jury_materiality
    if abs(backbone_share) >= m and abs(delta_share) >= m:
        return "confirm" if backbone_share * delta_share > 0 else "contest"
    if abs(backbone_share) >= m:
        return "solo"
    return None


def move_skill(channel_skill: Mapping[str, float | None], layer1: Mapping[str, float]) -> float | None:
    """Skill of the cross-check for *this* move: channel skills weighted by ``|layer1[channel]|``.

    Only channels with a defined skill take part.  Returns ``None`` when no channel has one.
    """
    num = den = 0.0
    for ch, sk in channel_skill.items():
        if sk is None:
            continue
        w = abs(layer1.get(ch, 0.0))
        num += w * sk
        den += w
    if den <= 0:
        vals = [s for s in channel_skill.values() if s is not None]
        return float(np.mean(vals)) if vals else None
    return num / den


def verdict(*, thin: bool, small_move: bool, net_explained_pct: float, top_label: str | None,
            skill: float | None, cfg: AttributionConfig | None = None) -> tuple[str, str]:
    """Is the attribution trustworthy enough to tell a lever story?  First match wins.

    1. ``thin`` -> ``thin-data``
    2. small-move guard on, or ``|round(net explained)| < explained_floor_pct`` -> ``unexplained``
    3. the largest lever is ``contest`` -> ``unexplained``
    4. move skill ``< skill_floor`` -> ``unexplained``
    5. the largest lever is ``confirm`` and move skill ``>= confirmed_min_skill`` -> ``confirmed``
    6. otherwise ``directional``

    ``top_label`` is the jury label of the lever with the largest
    ``|contribution|``, judged before any display re-ordering.  An undefined
    skill never triggers rule 4 and never satisfies rule 5.

    Returns ``(verdict, reason)``.
    """
    cfg = cfg or _CFG
    if thin:
        return "thin-data", "too few covered levers for an entity-level fit"
    if small_move:
        return "unexplained", "ROAS barely moved while levers offset each other"
    if abs(round(net_explained_pct)) < cfg.explained_floor_pct:
        return "unexplained", "the levers explain too little of the move"
    if top_label == "contest":
        return "unexplained", "the two models disagree on the direction of the largest lever"
    if skill is not None and skill < cfg.skill_floor:
        return "unexplained", "the day-to-day model predicts worse than persistence"
    if top_label == "confirm" and skill is not None and skill >= cfg.confirmed_min_skill:
        return "confirmed", "both models agree on the largest lever and day-to-day skill is non-negative"
    return "directional", "the level model points to a lever the day-to-day model does not confirm"


def confidence(verdict_value: str, days: int, avg_imputed: float, cfg: AttributionConfig | None = None) -> str:
    """Confidence label for the attribution.

    ``High`` when the verdict is ``confirmed``, ``days >= conf_high_min_days``
    and average imputation ``<= conf_high_max_imputed``; ``Low`` when the
    verdict is ``unexplained`` / ``thin-data``, ``days < conf_low_max_days`` or
    imputation ``> conf_low_min_imputed``; else ``Medium``.
    """
    cfg = cfg or _CFG
    if verdict_value == "confirmed" and days >= cfg.conf_high_min_days and avg_imputed <= cfg.conf_high_max_imputed:
        return "High"
    if verdict_value in NO_STORY_VERDICTS or days < cfg.conf_low_max_days or avg_imputed > cfg.conf_low_min_imputed:
        return "Low"
    return "Medium"


def channel_split(channel_contrib: Mapping[str, float], denom: float,
                  cfg: AttributionConfig | None = None) -> list[dict]:
    """Which identity channels carry a lever's contribution.

    ``channel_contrib[ch]`` is the unsigned-by-identity contribution
    ``w x dz`` on that channel.  Nothing is returned when the lever's total
    share is under ``chip_min_lever_pct``.  Otherwise per channel
    ``p = 100 x sign x contrib / denom``; keep channels with
    ``|p| >= chip_min_channel_pct`` and ``|p| >= chip_min_rel x max|p|``; if none
    qualify keep the largest.  Returns ``[{"ch", "pct"}]`` by ``|pct|`` descending.
    """
    cfg = cfg or _CFG
    if not denom:
        return []
    ps = {ch: 100.0 * CHANNEL_SIGN[ch] * v / denom for ch, v in channel_contrib.items()}
    if abs(sum(ps.values())) < cfg.chip_min_lever_pct:
        return []
    top = max(abs(p) for p in ps.values())
    keep = [(ch, p) for ch, p in ps.items() if abs(p) >= cfg.chip_min_channel_pct and abs(p) >= cfg.chip_min_rel * top]
    if not keep:
        keep = [max(ps.items(), key=lambda kv: abs(kv[1]))]
    keep.sort(key=lambda kv: -abs(kv[1]))
    return [{"ch": ch, "pct": round(p)} for ch, p in keep]


def _corr(a: np.ndarray, b: np.ndarray) -> float:
    if len(a) < 2 or np.std(a) == 0 or np.std(b) == 0:
        return float("nan")
    return float(np.corrcoef(a, b)[0, 1])


def demand_evidence(lever_values: Sequence[float], icr_values: Sequence[float], *, in_logs: bool = False,
                    cfg: AttributionConfig | None = None) -> dict | None:
    """Does a tier-2 (demand) lever actually track conversion day to day?

    Correlates the lever with ``ln(icr)`` on days where both are positive
    (``in_logs=True`` means the lever is already a logarithm and only needs
    to be finite).  Needs ``evidence_min_rows`` such days.

    * ``r`` full-sample, ``r1`` / ``r2`` first and second half, ``rl`` = yesterday's lever vs today's conversion
    * ``stable = r1 x r2 > 0 and min(|r1|, |r2|) >= evidence_half_r``
    * story: ``no-link`` if ``|r| < evidence_min_r``; else ``unstable`` if not stable; else
      ``demand-led`` (``r > 0``) or ``dilution`` (``r < 0``); a ``no-link`` with
      ``|rl| >= evidence_lag_r`` is upgraded to ``leading``.

    Purpose: without it a demand lever gets an unfalsifiable narrative in
    either direction.  Returns ``{"story", "r", "r1", "r2", "rl", "n", "stable"}``
    or ``None`` when there is not enough data or a correlation is undefined.
    """
    cfg = cfg or _CFG
    x = np.asarray(lever_values, dtype=float)
    y = np.asarray(icr_values, dtype=float)
    ok = np.isfinite(x) & np.isfinite(y) & (y > 0)
    if not in_logs:
        ok &= x > 0
    x, y = x[ok], np.log(y[ok])
    n = len(x)
    if n < cfg.evidence_min_rows:
        return None
    h = n // 2
    r, r1, r2, rl = _corr(x, y), _corr(x[:h], y[:h]), _corr(x[h:], y[h:]), _corr(x[:-1], y[1:])
    if not all(math.isfinite(v) for v in (r, r1, r2, rl)):
        return None
    stable = (r1 * r2 > 0) and min(abs(r1), abs(r2)) >= cfg.evidence_half_r
    if abs(r) < cfg.evidence_min_r:
        story = "leading" if abs(rl) >= cfg.evidence_lag_r else "no-link"
    elif not stable:
        story = "unstable"
    else:
        story = "demand-led" if r > 0 else "dilution"
    return {"story": story, "r": round(r, 2), "r1": round(r1, 2), "r2": round(r2, 2), "rl": round(rl, 2),
            "n": n, "stable": bool(stable)}


def size_move(lever: str, beta_by_channel: Mapping[str, float], sd: float, current: float, roas: float,
              spend: float, *, position_top: float = 1.0, cfg: AttributionConfig | None = None) -> dict | None:
    """Size one test step on a lever with the fitted elasticity.

    Steps: ``bid`` x (1 +/- ``size_bid_step``); ``price_gap`` x (1 +/-
    ``size_price_gap_step``); ``position`` one slot, never past
    ``position_top``; share-type levers and ``own_discount`` +/-
    ``size_share_step`` clamped to ``[0, 1]``.

    ``nbz = sum over channels of sign x beta`` is d ln ROAS per +1 sd of the
    lever.  The step goes in the direction that raises ROAS
    (``s = sign(nbz)``); ``draw`` is the absolute raw change after clamping;
    ``dROAS = roas x (exp(nbz x s x draw / sd) - 1)``.

    Suppressed (``None``) when ``|nbz| < size_min_beta``, ``draw <=
    size_min_draw``, the estimate is not finite, ``|dROAS| < size_min_droas``
    or ``|dROAS| > size_max_droas_share x max(roas, size_roas_floor)``.

    Returns ``{"lever", "direction", "from", "to", "step", "unit", "d_roas",
    "value", "estimator"}`` where ``value = |dROAS| x spend`` is a **model
    estimate** of revenue over the comparison window, not a forecast.
    """
    cfg = cfg or _CFG
    if current is None or not math.isfinite(current) or not roas or sd <= 0:
        return None
    nbz = sum(CHANNEL_SIGN[ch] * b for ch, b in beta_by_channel.items() if ch in CHANNEL_SIGN)
    if abs(nbz) < cfg.size_min_beta:
        return None
    s = 1.0 if nbz > 0 else -1.0
    if lever == "bid":
        new, unit, step = current * (1.0 + s * cfg.size_bid_step), "relative", cfg.size_bid_step
    elif lever == "price_gap":
        new, unit, step = current * (1.0 + s * cfg.size_price_gap_step), "relative", cfg.size_price_gap_step
    elif lever == "position":
        new, unit, step = current + s * cfg.size_position_step, "slots", cfg.size_position_step
        new = max(new, position_top)
    elif lever in SHARE_LEVERS or lever == "own_discount":
        new, unit, step = min(max(current + s * cfg.size_share_step, 0.0), 1.0), "points", cfg.size_share_step
    else:
        return None
    draw = abs(new - current)
    if draw <= cfg.size_min_draw:
        return None
    try:
        d_roas = roas * (math.exp(nbz * s * draw / sd) - 1.0)
    except OverflowError:
        return None
    if (not math.isfinite(d_roas) or abs(d_roas) < cfg.size_min_droas
            or abs(d_roas) > cfg.size_max_droas_share * max(roas, cfg.size_roas_floor)):
        return None
    return {"lever": lever, "direction": "up" if s > 0 else "down", "from": current, "to": new, "step": step,
            "unit": unit, "d_roas": d_roas, "value": abs(d_roas) * spend, "estimator": "lever-elasticity"}


# --------------------------------------------------------------------------- result types

@dataclass
class Driver:
    """One lever's line in the attribution.

    ``contrib`` is the signed contribution to the log ROAS move (positive
    raises ROAS); ``pct`` is its share of the denominator (positive = pushed
    in the direction of the move).  ``na`` levers were not in the model
    (uncovered, short-mode, ``suppressed`` or ``folded``); their raw
    ``change`` is still shown.
    """

    lever: str
    contrib: float = 0.0
    pct: float | None = None
    delta_contrib: float = 0.0
    change: dict | None = None
    channels: list[dict] = field(default_factory=list)
    imputed: float = 0.0
    coverage: float = 0.0
    driver_class: str = "int"
    control: bool = False
    jury: str | None = None
    evidence: dict | None = None
    na: bool = False
    folded: bool = False
    suppressed: bool = False
    sizing: dict | None = None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Attribution:
    """Full result of :func:`attribute`.

    ``layer1[channel]`` is the exact log move of each channel between the
    window totals; ``dln`` their signed sum (= ``ln(comparison ROAS /
    baseline ROAS)``).  ``skill`` holds the per-channel one-step skill of the
    *raw* first-difference model; the difference model used for
    ``delta_contrib`` may be a carry-over variant (``adstock``), so the
    reported skill can describe a slightly different model.  That is
    deliberate: reporting the best of three variants scored on the same
    folds would be inflated by selection.
    """

    status: str = "ok"
    method: str = "ridge_jury"
    verdict: str = "thin-data"
    note: str = ""
    baseline_roas: float | None = None
    comparison_roas: float | None = None
    comparison_spend: float = 0.0
    dln: float = 0.0
    layer1: dict[str, float] = field(default_factory=dict)
    drivers: list[Driver] = field(default_factory=list)
    explained: dict = field(default_factory=dict)
    guard: dict = field(default_factory=dict)
    skill: dict[str, float | None] = field(default_factory=dict)
    move_skill: float | None = None
    adstock: dict[str, float] = field(default_factory=dict)
    lambdas: dict[str, float] = field(default_factory=dict)
    betas: dict[str, dict[str, float]] = field(default_factory=dict)
    confidence: dict = field(default_factory=dict)
    suppressed: list[str] = field(default_factory=list)
    sizes: dict[str, dict] = field(default_factory=dict)
    top_driver: str | None = None
    days: int = 0
    n_live: int = 0
    short: bool = False
    thin: bool = True
    tier: str | None = None

    def driver(self, lever: str) -> Driver | None:
        """The driver line for ``lever`` (``None`` if unknown)."""
        return next((d for d in self.drivers if d.lever == lever), None)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["drivers"] = [x.as_dict() for x in self.drivers]
        return d


def _window_totals(panel: LeverPanel, mask: np.ndarray) -> dict[str, float] | None:
    sp, sa = float(panel.spend[mask].sum()), float(panel.sales[mask].sum())
    im, un = float(panel.impressions[mask].sum()), float(panel.units[mask].sum())
    if min(sp, sa, im, un) <= 0:
        return None
    return {"spend": sp, "sales": sa, "icr": un / im, "asp": sa / un, "cpm": 1000.0 * sp / im, "roas": sa / sp}


def _raw_change(prep: PreparedLevers, lever: str, b: np.ndarray, c: np.ndarray) -> dict | None:
    v = prep.raw[lever]
    vb, vc = v[b], v[c]
    vb, vc = vb[np.isfinite(vb)], vc[np.isfinite(vc)]
    if not len(vb) or not len(vc):
        return None
    return {"from": float(vb.mean()), "to": float(vc.mean())}


# --------------------------------------------------------------------------- the attributor

def attribute(panel: LeverPanel, windows: WindowPair, *, prior: Prior | None = None, short: bool | None = None,
              position_lower_is_better: bool = True, position_top: float = 1.0,
              tiers: TierConfig | None = None, cfg: AttributionConfig | None = None,
              clip: bool = True) -> Attribution:
    """Attribute the ROAS move between ``windows.prior`` and ``windows.current`` to levers.

    Decision made: a signed share of the log ROAS move per lever, a jury
    label per lever, and one verdict for the whole attribution.

    Method:

    0. *Clip*: the panel is first restricted to ``[windows.prior.start,
       windows.current.end]``, so coverage, medians, the collinearity guard,
       the day-count tier and the confidence all describe the compared
       periods only.  A short comparison (for example week over week) falls
       below the move-model floor and returns ``thin-data`` rather than a
       verdict fitted on history outside the windows.  ``clip=False`` fits
       on the whole panel (the caller then owns the routing question).
    1. *Layer 1* (exact): window totals over **all** days give
       ``dln = ln ICR_c/ICR_b + ln ASP_c/ASP_b - ln CPM_c/CPM_b``.
    2. *Routing*: the model days (all channels positive) are counted; fewer
       than the MATURE floor -> ``thin-data`` with ``status="insufficient-data"``;
       MATURE -> short mode; FULL -> full mode.  Pass ``short`` explicitly to skip routing.
    3. *Preparation*: :func:`prepare_levers` (coverage, suppression, collinearity fold).
    4. *Backbone*: live levers are z-scored with the account's mean / sd when
       a prior covers them (else the entity's own); per channel a
       sign-constrained ridge is shrunk toward the account betas.  ``thin`` ->
       the account betas are used as they are; short mode -> fixed
       ``short_lambda``; otherwise lambda by blocked CV.
       ``contrib[j] = sum_ch sign_ch x w_ch[j] x (mean z in comparison - mean z in baseline)``.
       Because the model is linear in z this equals the Shapley value of the
       model for baseline -> comparison; no enumeration is needed.
    5. *Cross-check*: a first-difference ridge per channel (optionally on a
       carry-over variant that beats the raw skill by ``adstock_margin``)
       yields ``delta_contrib`` and a one-step skill versus persistence.
    6. *Jury and verdict*: :func:`jury_label` per lever, :func:`verdict` overall.
    7. *Sizing*: for ``confirmed`` / ``directional`` verdicts each live tier-1
       lever gets a :func:`size_move` estimate.

    Returns an :class:`Attribution`.  Drivers cover all eleven levers, sorted
    live before n/a, then confirm < solo < contest, then by ``|contrib|``.
    """
    cfg = cfg or _CFG
    out = Attribution()
    if clip:
        panel = panel.clip(min(windows.prior.start, windows.current.start),
                           max(windows.prior.end, windows.current.end))
    base_all, cmp_all = panel.window_mask(windows.prior), panel.window_mask(windows.current)
    tb, tc = _window_totals(panel, base_all), _window_totals(panel, cmp_all)
    mask = panel.model_mask
    n = int(mask.sum())
    out.days = n
    tier = route_tier(n, tiers)
    out.tier = tier.value if isinstance(tier, DataTier) else str(tier)
    if short is None:
        if not tier_allows_move_model(tier):
            out.status, out.note = "insufficient-data", "too few active days for a move model"
            out.confidence = {"level": "Low", "days": n, "imputed": 0.0, "skill": None}
            if tb and tc:
                out.baseline_roas, out.comparison_roas, out.comparison_spend = tb["roas"], tc["roas"], tc["spend"]
                out.layer1 = {ch: math.log(tc[ch] / tb[ch]) for ch in CHANNELS}
                out.dln = sum(CHANNEL_SIGN[ch] * v for ch, v in out.layer1.items())
            out.drivers = [Driver(lever=f, na=True, driver_class=DRIVER_CLASS[f], control=f in TIER2) for f in LEVERS]
            return out
        short = tier == DataTier.MATURE
    out.short = bool(short)

    prep = prepare_levers({k: v[mask] for k, v in panel.levers.items()}, short=bool(short), cfg=cfg, n_rows=n)
    out.guard, out.suppressed = prep.guard, list(prep.suppressed)
    live = prep.live
    out.n_live, out.thin = len(live), prep.thin
    b, c = base_all[mask], cmp_all[mask]

    def na_drivers() -> list[Driver]:
        return [Driver(lever=f, na=True, imputed=prep.imputed[f], coverage=prep.coverage[f],
                       driver_class=DRIVER_CLASS[f], control=f in TIER2, folded=f in prep.folded,
                       suppressed=f in prep.suppressed,
                       change=_raw_change(prep, f, b, c) if n else None) for f in LEVERS]

    if not tb or not tc or not b.any() or not c.any():
        out.status, out.note = "no-comparison", "one of the windows has no usable days"
        out.confidence = {"level": "Low", "days": n, "imputed": 0.0, "skill": None}
        out.drivers = na_drivers()
        return out

    out.baseline_roas, out.comparison_roas, out.comparison_spend = tb["roas"], tc["roas"], tc["spend"]
    l1 = {ch: math.log(tc[ch] / tb[ch]) for ch in CHANNELS}
    raw_dln = sum(CHANNEL_SIGN[ch] * v for ch, v in l1.items())
    out.layer1, out.dln = l1, raw_dln

    p = len(live)
    mu, sd = {}, {}
    for f in live:
        if prior is not None and f in prior.mu and f in prior.sd:
            mu[f], sd[f] = prior.mu[f], max(prior.sd[f], cfg.sd_floor)
        else:
            mu[f], sd[f] = float(prep.filled[f].mean()), max(float(prep.filled[f].std()), cfg.sd_floor)
    Z = np.column_stack([zscore(prep.filled[f], mu[f], sd[f], cfg) for f in live]) if p else np.zeros((n, 0))
    dz = Z[c].mean(axis=0) - Z[b].mean(axis=0) if p else np.zeros(0)
    ys = {ch: np.log(np.maximum(panel.channel(ch)[mask], cfg.log_floor)) for ch in CHANNELS}
    splits = time_series_splits(n, cfg.cv_splits_short if short else cfg.cv_splits, cfg.cv_min_train)

    contrib, dcontrib = np.zeros(p), np.zeros(p)
    chan: dict[str, np.ndarray] = {}
    run_delta = (not prep.thin) and n >= cfg.delta_min_rows and p > 0
    variants = [(0.0, Z)] + ([(r, adstock(Z, live, r, cfg)) for r in cfg.adstock_lambdas] if run_delta else [])
    for ch in CHANNELS:
        b0 = np.array([prior.beta.get(ch, {}).get(f, 0.0) if prior else 0.0 for f in live])
        lo, hi = sign_bounds(ch, live, position_lower_is_better)
        if prep.thin or p == 0:
            w, lam = b0, None
        elif short or not splits:
            lam = cfg.short_lambda
            w, _ = bounded_ridge(Z, ys[ch], lam, b0, lo, hi, cfg)
        else:
            lam = choose_lambda(Z, ys[ch], b0, lo, hi, splits, cfg)
            w, _ = bounded_ridge(Z, ys[ch], lam, b0, lo, hi, cfg)
        if lam is not None:
            out.lambdas[ch] = lam
        out.betas[ch] = {f: float(w[j]) for j, f in enumerate(live)}
        chan[ch] = w * dz
        contrib += CHANNEL_SIGN[ch] * chan[ch]
        out.skill[ch] = None
        if run_delta:
            sk0 = delta_skill(Z, ys[ch], splits, cfg)
            best, chosen, rate = sk0, Z, 0.0
            for r, zv in variants[1:]:
                sk = delta_skill(zv, ys[ch], splits, cfg)
                if sk is not None and best is not None and sk > best + cfg.adstock_margin:
                    best, chosen, rate = sk, zv, r
            wd = fit_delta(chosen, ys[ch], cfg)
            dcontrib += CHANNEL_SIGN[ch] * wd * (chosen[c].mean(axis=0) - chosen[b].mean(axis=0))
            out.skill[ch] = sk0
            if rate:
                out.adstock[ch] = rate

    ex = explained_share(contrib.tolist(), raw_dln, cfg)
    denom = ex["denom"]
    out.explained = {k: (round(v, 1) if isinstance(v, float) else v) for k, v in ex.items() if k != "denom"}
    labels = {f: jury_label(contrib[j] / denom, dcontrib[j] / denom, thin=prep.thin, has_prior=prior is not None, cfg=cfg)
              for j, f in enumerate(live)}
    out.move_skill = move_skill(out.skill, l1)
    top = live[int(np.argmax(np.abs(contrib)))] if p else None
    out.top_driver = top
    out.verdict, out.note = verdict(thin=prep.thin, small_move=ex["small_move"], net_explained_pct=ex["net"],
                                    top_label=labels.get(top), skill=out.move_skill, cfg=cfg)
    avg_imp = float(np.mean([prep.imputed[f] for f in live])) if live else 0.0
    out.confidence = {"level": confidence(out.verdict, n, avg_imp, cfg), "days": n,
                      "imputed": round(avg_imp, 3), "skill": out.move_skill}

    icr = panel.channel("icr")[mask]
    drivers = na_drivers()
    by_name = {d.lever: d for d in drivers}
    for j, f in enumerate(live):
        d = by_name[f]
        d.na = False
        d.contrib, d.delta_contrib = float(contrib[j]), float(dcontrib[j])
        d.pct = float(round(100.0 * contrib[j] / denom))
        d.channels = channel_split({ch: float(chan[ch][j]) for ch in CHANNELS}, denom, cfg)
        d.jury = labels[f]
        if out.verdict in ("confirmed", "directional") and f not in TIER2 and not prep.thin:
            cur = float(prep.filled[f][c].mean())
            d.sizing = size_move(f, {ch: out.betas[ch][f] for ch in CHANNELS}, sd[f], cur, tc["roas"], tc["spend"],
                                 position_top=position_top, cfg=cfg)
            if d.sizing:
                out.sizes[f] = d.sizing
    for f in ("log_category_impressions", "comp_availability"):
        by_name[f].evidence = demand_evidence(prep.raw[f], icr, in_logs=f.startswith("log_"), cfg=cfg)
    drivers.sort(key=lambda d: (d.na, _JURY_RANK.get(d.jury, 1), -abs(d.contrib)))
    out.drivers = drivers
    return out
