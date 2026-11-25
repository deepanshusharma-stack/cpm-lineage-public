"""The exact decompositions the RCA relies on, and the change-point scan.

All five are identities with no residual term, which is what lets the
verifier re-add the parts to the headline:

* D1 units = impressions x conversion (prior-rate / current-volume split);
* D2 value = quantity x price (same convention);
* D3 ROAS = revenue per thousand impressions / CPM;
* D4 ROAS = conversion per thousand x price / CPM;
* D5 midpoint shift-share of a blended ROAS across ad types.

D1 / D2 put the interaction term in the rate effect; D5 is a midpoint
split.  Both are exact; they are never mixed inside one identity.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable, Mapping, Sequence

from cpm_lineage.decompose import ShiftShareResult, shift_share
from cpm_lineage.identity import TwoFactor, rounded_parts, two_factor, value_decomposition
from cpm_lineage.metrics import roas_from_identity

from .config import DEFAULT_RCA_CONFIG, RcaConfig

__all__ = [
    "RoasIdentity", "StepChange", "mix_vs_rate", "roas_from_identity", "roas_identity", "rounded_parts",
    "step_scan", "two_factor_units", "value_decomposition",
]


def two_factor_units(impr_p: float, conv_p: float, impr_c: float, conv_c: float) -> TwoFactor:
    """D1: split a change in units into a visibility effect and a conversion effect.

    ``impr_effect = (impr_c - impr_p) x conv_p`` and ``conv_effect =
    impr_c x (conv_c - conv_p)``; they sum to ``impr_c x conv_c - impr_p x
    conv_p`` exactly.  ``volume_effect`` is the impression effect,
    ``rate_effect`` the conversion effect, ``mechanism`` is
    ``"conversion-led"`` when ``|conv_effect| > |impr_effect|`` else
    ``"visibility-led"``.
    """
    return two_factor(impr_p, conv_p, impr_c, conv_c, labels=("conversion-led", "visibility-led"))


@dataclass(frozen=True)
class RoasIdentity:
    """D3 for one window: ``roas == rpi / cpm`` exactly."""

    roas: float
    cpm: float
    rpi: float      # attributed revenue per thousand impressions


def roas_identity(spend: float, impressions: float, roas: float) -> RoasIdentity | None:
    """D3: express ROAS as revenue-per-thousand-impressions over CPM.

    ``cpm = 1000 x spend / impressions`` and ``rpi = roas x cpm``, so
    ``roas == rpi / cpm``.  Says whether a ROAS move is demand-side (``rpi``
    moved) or cost-side (``cpm`` moved).  ``None`` without spend or
    impressions.
    """
    if not spend or not impressions:
        return None
    cpm = 1000.0 * spend / impressions
    return RoasIdentity(roas=roas, cpm=cpm, rpi=roas * cpm)


def mix_vs_rate(ad_types: Iterable[Any]) -> ShiftShareResult:
    """D5: midpoint shift-share of the blended ROAS across ad types.

    For each type with spend share ``w`` and ROAS ``r``: ``mix = (w_c -
    w_p) x (r_c + r_p) / 2`` and ``rate = (w_c + w_p) / 2 x (r_c - r_p)``;
    the sums equal the change in blended ROAS.  ``result.primary`` is
    ``"MIX"`` when ``|sum mix| >= |sum rate|`` else ``"RATE"``.

    Accepts records with ``ad_type, spend_p, sales_p, spend_c, sales_c``
    attributes (for example ``AdTypeRow``) or equivalent mappings.
    """
    items = []
    for a in ad_types:
        get = a.get if isinstance(a, Mapping) else (lambda k, _a=a: getattr(_a, k, 0.0))
        items.append({
            "key": get("ad_type"), "weight_prior": get("spend_p") or 0.0, "value_prior": get("sales_p") or 0.0,
            "weight_current": get("spend_c") or 0.0, "value_current": get("sales_c") or 0.0,
        })
    return shift_share(items)


@dataclass(frozen=True)
class StepChange:
    """A dated level shift in a daily series."""

    date: date
    before: float
    after: float
    pct: float


def step_scan(daily: Sequence[tuple[date, float]], config: RcaConfig | None = None) -> StepChange | None:
    """Decide whether a daily series stepped to a new level, and on which day.

    Needs at least ``step_min_points`` points.  For every split ``k`` with
    both sides at least ``step_min_segment`` days long, compare the mean
    before and after (splits with a non-positive mean before are skipped)
    and keep the split with the largest absolute difference.  It fires only
    when ``|100 x (after / before - 1)| >= step_pct``: a step, not a drift.

    Returns the :class:`StepChange` dated at the first day of the new level,
    or ``None``.
    """
    c = config or DEFAULT_RCA_CONFIG
    n = len(daily)
    if n < c.step_min_points:
        return None
    values = [float(v) for _, v in daily]
    best: tuple[float, int, float, float] | None = None
    for k in range(c.step_min_segment, n - c.step_min_segment + 1):
        m1 = sum(values[:k]) / k
        m2 = sum(values[k:]) / (n - k)
        if m1 <= 0:
            continue
        gap = abs(m2 - m1)
        if best is None or gap > best[0]:
            best = (gap, k, m1, m2)
    if best is None:
        return None
    _, k, m1, m2 = best
    pct = 100.0 * (m2 / m1 - 1.0)
    if abs(pct) < c.step_pct:
        return None
    return StepChange(date=daily[k][0], before=m1, after=m2, pct=pct)
