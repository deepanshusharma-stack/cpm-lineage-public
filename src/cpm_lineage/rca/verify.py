"""The verifier: re-add every component to its headline before anything is reported.

A report carries its own proof block.  If an accounting identity fails the
run is refused: no verdict, no causes, no levers.
"""

from __future__ import annotations

from dataclasses import dataclass

from .attribute import Attribution
from .decompositions import two_factor_units
from .facts import Facts


@dataclass(frozen=True)
class Check:
    """One verification line.  ``hard`` checks refuse the report when they fail; soft ones degrade an axis."""

    name: str
    ok: bool
    detail: str
    hard: bool = True

    def as_tuple(self) -> tuple[str, bool, str]:
        return (self.name, self.ok, self.detail)


def _tol(absolute: float, relative: float, scale: float) -> float:
    return max(absolute, relative * abs(scale))


def verify(facts: Facts, attribution: Attribution | None = None) -> list[Check]:
    """Check that the components sum back to the headline, and state what the run relied on.

    | Check | Rule | Kind |
    |---|---|---|
    | geo sums == total | ``|sum(geo current value) - own current value| < tol`` | hard |
    | product sums == total | ``|sum(product current value) - own current value| < tol`` | hard |
    | decomposition exact | ``|(impr_c x conv_c - impr_p x conv_p) - (impr_effect + conv_effect)| < tol`` | hard |
    | brand-join coverage | ``join_coverage >= join_coverage_min_pct`` (when present) | hard |
    | market feed freshness | always true; states the feed's latest date | info |
    | threshold override: k | always true; ``default -> value`` | info |
    | override key ignored | always true; names the unknown key | info |
    | ads feed freshness | false when the ads ledger is stale | soft |

    Value tolerance is ``max(Economics.money(tolerance_value),
    tolerance_rel x |total|)``; the unit tolerance is ``max(tolerance_units,
    tolerance_rel x |units|)``.  (Absolute tolerances alone would be scale- and
    currency-dependent.)

    The decomposition check uses ``attribution.mech`` when an attribution is
    passed (so a tampered or stale mechanism is caught) and otherwise
    recomputes D1 from the conversion facts.  Returns a list of
    :class:`Check`.
    """
    scope = facts.scope
    cfg = scope.config
    off = facts.offtake
    out: list[Check] = []
    tol_v = _tol(scope.economics.money(cfg.tolerance_value), cfg.tolerance_rel, off.cur_value)

    if off.geo_rows:
        s = sum(g.cur_value for g in off.geo_rows)
        out.append(Check("geo sums == total", abs(s - off.cur_value) < tol_v,
                         f"sum of geos {s:,.2f} vs own total {off.cur_value:,.2f}"))
    if off.product_rows:
        s = sum(p.cur_value for p in off.product_rows)
        out.append(Check("product sums == total", abs(s - off.cur_value) < tol_v,
                         f"sum of products {s:,.2f} vs own total {off.cur_value:,.2f}"))

    conv = facts.conversion
    mech = attribution.mech if attribution is not None else None
    if mech is None and attribution is None and conv is not None and conv.impr_p and conv.impr_c:
        tf = two_factor_units(conv.impr_p, conv.conv_p, conv.impr_c, conv.conv_c)
        parts = tf.volume_effect + tf.rate_effect
    elif mech is not None:
        parts = mech.impr_effect + mech.conv_effect
    else:
        parts = None
    if parts is not None and conv is not None:
        head = conv.impr_c * conv.conv_c - conv.impr_p * conv.conv_p
        tol_u = _tol(cfg.tolerance_units, cfg.tolerance_rel, max(abs(conv.impr_c * conv.conv_c),
                                                                 abs(conv.impr_p * conv.conv_p)))
        out.append(Check("decomposition exact", abs(head - parts) < tol_u,
                         f"unit delta {head:,.2f} vs visibility + conversion effects {parts:,.2f}"))

    if facts.rivals is not None and facts.rivals.join_coverage is not None:
        jc = facts.rivals.join_coverage
        out.append(Check(f"brand-join coverage >= {cfg.join_coverage_min_pct:.0f}%",
                         jc >= cfg.join_coverage_min_pct, f"{jc:.2f}% of segment value maps to a brand"))

    out.append(Check("market feed freshness", True, f"latest date {scope.feed_max.isoformat()}", hard=False))
    for key, (default, value) in scope.overrides.items():
        out.append(Check(f"threshold override: {key}", True, f"{default} -> {value}", hard=False))
    for key in scope.unknown_overrides:
        out.append(Check("override key ignored", True, f"unknown threshold {key!r}", hard=False))
    if facts.funnel is not None and facts.funnel.stale:
        latest = facts.funnel.feed_max.isoformat() if facts.funnel.feed_max else "none"
        out.append(Check("ads feed freshness", False,
                         f"latest ads date {latest} is before the current window: the funnel axis is marked "
                         "unavailable, not silently reported", hard=False))
    return out


def hard_failures(checks: list[Check]) -> list[Check]:
    """The failed checks that refuse a report (soft freshness failures only degrade an axis)."""
    return [c for c in checks if c.hard and not c.ok]


def is_verified(checks: list[Check]) -> bool:
    """Decide whether a report may ship: true when no hard check failed."""
    return not hard_failures(checks)
