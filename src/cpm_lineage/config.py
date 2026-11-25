"""Shared configuration: economics, availability, budget-bound, liveness, windows.

Every threshold is a named field on a frozen dataclass with a default
and a one-line rationale.  Subsystems
keep their own config dataclasses in their own modules and embed or accept an
``EngineConfig`` for the shared pieces.

Design decisions where more than one definition is common
---------------------------------------------------------
* **Naming of variants.**  The attribution method is ``"ridge_jury"`` (the
  default) or ``"shapley"``; the action rule profile is ``"classic"``,
  ``"marginal"`` or ``"verdict"``.  Numeric version labels are not used.
* **Role-cohort minimum n** is one value, ``role_cohort_min_n = 12``.  (5 is a
  common looser choice; it is an override, not the default.)
* **Default windows** differ per subsystem; ``WindowDefaults`` holds each and
  every function takes an explicit ``WindowPair``.  All are anchored to the
  latest date in the data except the budget reporting window, which is
  calendar-anchored.
* **Availability** thresholds live in one ``AvailabilityPolicy`` on a 0..1
  scale; ``AvailabilityPolicy.normalise`` accepts 0..100 inputs.
* **Scale bar** is ``Economics.scale_bar = scale_bar_mult x be``; defaults
  reproduce the absolute bars 1.0 / 2.0.
* **Budget-bound** has four definitions; ``BudgetBoundPolicy`` names each
  and ``schema.BudgetTelemetry.is_bound`` evaluates them.
* **Liveness** uses one definition (``liveness.py``) driven by
  ``LivenessConfig``.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Mapping, TypeVar

__all__ = [
    "ATTRIBUTION_METHODS", "RULE_PROFILES", "Economics", "AvailabilityPolicy",
    "BudgetBoundPolicy", "LivenessConfig", "PositionConfig", "TierConfig",
    "WindowDefaults", "TradingDay", "EngineConfig", "apply_overrides",
]

ATTRIBUTION_METHODS: tuple[str, ...] = ("ridge_jury", "shapley")
RULE_PROFILES: tuple[str, ...] = ("classic", "marginal", "verdict")


@dataclass(frozen=True)
class Economics:
    """Account economics that re-base every profitability threshold.

    ``be = max(breakeven / max(ltv_mult, 0.1), 0.1)`` is the effective
    breakeven ROAS.  With the defaults ``be = 1.0`` and every "x be" rule
    reads as a plain ROAS number.

    **Reference money scale.**  Every absolute money threshold in the
    library (minimum spend to judge a campaign, placeholder cap, leak
    floors ...) is written for an account whose material campaign spends
    in the *thousands of currency units per day* (a daily cap of 100 is a
    placeholder, 25,000 of window spend is the smallest campaign worth a
    cut card).  ``currency_unit`` rescales all of them at once: a
    campaign that spends 50 a day needs about ``currency_unit=0.01``.
    ``None`` (the default) means "not set here": the engine then takes the
    platform profile's ``currency_unit``; standalone it reads as 1.
    """

    breakeven: float = 1.0        # ROAS at which a first purchase breaks even (about 1 / contribution margin)
    ltv_mult: float = 1.0         # lifetime value per first-purchase currency unit
    currency_unit: float | None = None  # multiplier applied to money thresholds written in reference units; None = take the profile's (else 1)
    scale_bar_mult: float = 2.0   # scale bar = 2 x be: clearly profitable, safe to add spend
    min_knob: float = 0.1         # floor on both knobs so a typo cannot zero the breakeven

    def __post_init__(self) -> None:
        if self.currency_unit is not None and not self.currency_unit > 0:
            raise ValueError("Economics.currency_unit must be positive")

    @property
    def unit(self) -> float:
        """The money scale in force: ``currency_unit``, or 1 when it is not set."""
        return 1.0 if self.currency_unit is None else self.currency_unit

    def resolved(self, profile_unit: float | None) -> "Economics":
        """Fill an unset ``currency_unit`` from the platform profile's (one source of truth, config wins)."""
        if self.currency_unit is not None or profile_unit is None:
            return self
        return dataclasses.replace(self, currency_unit=float(profile_unit))

    @property
    def be(self) -> float:
        """Effective breakeven ROAS."""
        return max(self.breakeven / max(self.ltv_mult, self.min_knob), self.min_knob)

    @property
    def scale_bar(self) -> float:
        """ROAS above which adding spend is considered safe (``scale_bar_mult x be``)."""
        return self.scale_bar_mult * self.be

    def money(self, reference_amount: float) -> float:
        """Scale a money threshold from reference units into account currency.

        Every absolute money default in the library is a plain number chosen
        for a reference currency; multiply by ``currency_unit`` to use it in
        another currency or at another account size.
        """
        return reference_amount * self.unit

    def as_dict(self) -> dict[str, float]:
        """``breakeven``, ``ltv_mult`` and the effective breakeven ``be``."""
        return {"breakeven": self.breakeven, "ltv_mult": self.ltv_mult, "be": self.be}

    @classmethod
    def from_mapping(cls, m: Mapping[str, Any] | None) -> "Economics":
        """Build from a possibly partial mapping; blanks keep defaults, knobs clamp to >= 0.1."""
        m = m or {}
        d = cls()
        def knob(key: str, default: float) -> float:
            v = m.get(key)
            if v is None or v == "":
                return default
            return max(float(v), d.min_knob)
        return cls(
            breakeven=knob("breakeven", d.breakeven),
            ltv_mult=knob("ltv_mult", d.ltv_mult),
            currency_unit=float(m["currency_unit"]) if m.get("currency_unit") else None,
        )


@dataclass(frozen=True)
class AvailabilityPolicy:
    """One home for every availability threshold, on a 0..1 scale.

    Availability gates are optional: with no availability signal each
    consumer reports the gate as "not armed" instead of passing or failing.
    """

    block: float = 0.60       # below this, fix availability before any ad change: out-of-stock products drop out of search
    caution: float = 0.75     # below this, do not add spend (classic profile supply gate)
    floor: float = 0.85       # healthy floor: geo-expand gate, root-cause floor, watchdog tripwire
    target: float = 0.90      # level a healthy listing should hold
    drill_warn: float = 0.70  # drill rows flag a product x geo below this
    share_band_floors: tuple[tuple[float, bool, float], ...] = (
        (10.0, False, 0.85),  # market share > 10 %: strong brands tolerate a lower floor
        (5.0, True, 0.88),    # market share >= 5 %
    )                         # (share % bound, inclusive?, floor), checked in order
    share_band_default: float = 0.90  # small brands need the highest floor
    enabled: bool = True      # turn all availability gates off (the verdict profile gates nothing on availability)

    @staticmethod
    def normalise(value: float | None) -> float | None:
        """Return availability on 0..1; values above 1.5 are read as percentages."""
        if value is None:
            return None
        return value / 100.0 if value > 1.5 else value

    def band(self, value: float | None) -> str:
        """Classify availability: ``"unknown" | "block" | "caution" | "below_floor" | "ok"``."""
        v = self.normalise(value)
        if v is None or not self.enabled:
            return "unknown"
        if v < self.block:
            return "block"
        if v < self.caution:
            return "caution"
        if v < self.floor:
            return "below_floor"
        return "ok"

    def floor_for_share(self, share_pct: float | None) -> float:
        """Availability floor for an advertiser with market share ``share_pct`` (0..100)."""
        if share_pct is None:
            return self.share_band_default
        for bound, inclusive, flr in self.share_band_floors:
            if share_pct > bound or (inclusive and share_pct >= bound):
                return flr
        return self.share_band_default


@dataclass(frozen=True)
class BudgetBoundPolicy:
    """Thresholds for the four budget-bound definitions (see ``BudgetTelemetry.is_bound``)."""

    method: str = "auto"                  # "auto" | "sufficiency" | "exhaustion" | "capped_days" | "held_days"
    sufficiency_max: float = 0.90         # served under 90 % of expected live time over the last 3 days
    early_exhaust_minutes: float = 720.0  # budget gone before 12 h into the day
    min_spend_fraction: float = 0.5       # exhaust flag only trusted when spend >= half the cap
    min_exhaust_day_share: float = 0.4    # exhaustion must persist on >= 40 % of days
    capped_spend_fraction: float = 0.9    # a day counts as capped at spend >= 90 % of the cap
    capped_day_share: float = 0.6         # capped on >= 60 % of active days
    held_days_min: int = 3                # platform-held on >= 3 days: top up, not a free scale


@dataclass(frozen=True)
class LivenessConfig:
    """Day counts for the liveness rules."""

    soft_dead_days: int = 3   # soft-dead states are dead only with no spend in the last 3 days
    stale_days: int = 7       # hard-dead entities stay in analyses while they spent in the last 7 days


@dataclass(frozen=True)
class PositionConfig:
    """Validity range and semantics of the ``position`` field."""

    valid_min: float = 1.0          # slots are 1-based
    valid_max: float = 100.0        # feeds carry sentinel values far above any real slot
    cap: int = 21                   # positions >= cap are bucketed as "cap+"
    lower_is_better: bool = True    # 1 = top slot
    sanity_max: float = 500.0       # looser bound used when cleaning modelling features


@dataclass(frozen=True)
class TierConfig:
    """Active-day bounds of the data tiers."""

    warm_min_days: int = 7      # below this: COLD, nothing but priors
    mature_min_days: int = 21   # move model and saturation curve need three weeks
    full_min_days: int = 35     # full-length model


@dataclass(frozen=True)
class WindowDefaults:
    """Default window lengths per subsystem (all data-anchored unless noted)."""

    identity_days: int = 30     # campaign diagnosis: 30 vs prior 30
    rca_days: int = 14          # root-cause analysis: 14 vs prior 14
    shiftshare_days: int = 30   # account triage: 30 vs prior 30
    drill_days: int = 30        # drilldown buckets
    actions_days: int = 30      # action lookback (14 / 30 / 60 offered)
    label_days: int = 15        # placement-label evidence
    changelog_days: int = 7     # input-change scan
    min_rows: int = 5           # fewer rows than this in a window triggers the fallback split
    week_end_isoweekday: int = 7  # budget reporting window ends on a completed Sunday (calendar-anchored)
    short_month_span_days: int = 20  # budget window: trailing 3 weeks when the month has < 1 full week
    wow_shift_days: int = 7     # custom range, week-over-week shift
    dod_shift_days: int = 1     # custom range, day-over-day shift


@dataclass(frozen=True)
class TradingDay:
    """Local trading-day hours used by pacing and revenue-at-stake maths."""

    start_hour: float = 6.0            # serving starts
    end_hour: float = 18.0             # last hour that still converts to same-day orders
    funding_target_hour: float = 22.0  # a raise funds serving up to this hour
    tz_offset_minutes: int = 0         # offset of local time from the feed's timestamps

    @property
    def expected_live_minutes(self) -> float:
        """Minutes from serving start to the funding target hour."""
        return (self.funding_target_hour - self.start_hour) * 60.0


@dataclass(frozen=True)
class EngineConfig:
    """Shared base configuration passed to (or embedded by) every subsystem."""

    economics: Economics = field(default_factory=Economics)
    availability: AvailabilityPolicy = field(default_factory=AvailabilityPolicy)
    budget_bound: BudgetBoundPolicy = field(default_factory=BudgetBoundPolicy)
    liveness: LivenessConfig = field(default_factory=LivenessConfig)
    position: PositionConfig = field(default_factory=PositionConfig)
    tiers: TierConfig = field(default_factory=TierConfig)
    windows: WindowDefaults = field(default_factory=WindowDefaults)
    trading_day: TradingDay | None = None   # None = built from the platform profile's trading-day hours
    indirect_sales_weight: float = 1.0      # share of PerfRow.indirect_sales counted in entity ROAS by diagnose() and plan(): 1 = total, 0 = direct only
    attribution: str = "ridge_jury"   # attribution method; "shapley" is the optional exact alternative
    rule_profile: str = "classic"     # action rule profile: "classic" | "marginal" | "verdict"
    role_cohort_min_n: int = 12       # a role cohort needs 12 units before its percentiles are used
    lever_min_coverage: float = 0.20  # a lever with coverage at or below this is reported as not available

    def __post_init__(self) -> None:
        if self.attribution not in ATTRIBUTION_METHODS:
            raise ValueError(f"attribution must be one of {ATTRIBUTION_METHODS}")
        if self.rule_profile not in RULE_PROFILES:
            raise ValueError(f"rule_profile must be one of {RULE_PROFILES}")
        if not 0.0 <= self.indirect_sales_weight <= 1.0:
            raise ValueError("indirect_sales_weight must be between 0 and 1")


_T = TypeVar("_T")


def apply_overrides(cfg: _T, overrides: Mapping[str, Any] | None) -> tuple[_T, dict[str, Any], list[str]]:
    """Apply per-account threshold overrides to a frozen config dataclass.

    Decision: known field names override the default (numeric fields are
    cast to the field's current numeric type); unknown names are ignored
    and reported.  The input object is never mutated, so concurrent runs
    stay isolated.

    Returns ``(new_config, applied, unknown)`` where ``applied`` maps each
    overridden field to its new value and ``unknown`` lists ignored keys.
    """
    if not dataclasses.is_dataclass(cfg) or isinstance(cfg, type):
        raise TypeError("cfg must be a dataclass instance")
    names = {f.name for f in dataclasses.fields(cfg)}
    applied: dict[str, Any] = {}
    unknown: list[str] = []
    for key, value in (overrides or {}).items():
        if key not in names:
            unknown.append(key)
            continue
        current = getattr(cfg, key)
        if isinstance(current, bool):
            value = bool(value)
        elif isinstance(current, int) and not isinstance(value, bool):
            value = int(value)
        elif isinstance(current, float):
            value = float(value)
        applied[key] = value
    return dataclasses.replace(cfg, **applied), applied, unknown
