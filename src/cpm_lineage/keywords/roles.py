"""Keyword role classifier with a pluggable lexicon, and role cohorts.

Decision: what kind of keyword is this?  ``own_brand`` > ``sibling_brand``
> ``competitor`` > ``generic`` (strict precedence), from a token lexicon
built out of the product catalogue.  Nothing is hard-coded: brand names,
own titles, sibling brands and the stop list are all inputs.

The lexicon is pluggable: anything exposing ``classify(keyword) ->
RoleResult`` (the :class:`RoleLexicon` protocol) can replace the token
lexicon, e.g. a platform's native keyword-type feed or a curated map.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterable, Mapping, Protocol, Sequence, runtime_checkable

from ..adapters.base import PlatformProfile
from ..config import Economics
from ..market._util import in_window, norm, percentile
from ..schema import MarketRow, PerfRow, Product, RoleCohort, TargetingRole, Window
from .aggregate import KeywordStat
from .config import KeywordConfig

__all__ = [
    "OWN", "SIBLING", "COMPETITOR", "GENERIC",
    "BrandSpec", "Lexicon", "RoleLexicon", "MappingLexicon", "RoleResult", "KeywordDim", "RoleCohort",
    "tokens", "brand_tokens", "despace", "brand_spec",
    "build_lexicon", "lexicon_from_products", "classify_role", "classify_keywords",
    "keyword_universe", "search_volumes", "keyword_dimension", "role_map",
    "role_cohorts", "cohort_value", "dimension_cache_key",
]

OWN = TargetingRole.OWN_BRAND.value
SIBLING = TargetingRole.SIBLING_BRAND.value
COMPETITOR = TargetingRole.COMPETITOR.value
GENERIC = TargetingRole.GENERIC.value

_KC = KeywordConfig()
_WORD = re.compile(r"[^\W_]+", re.UNICODE)


# --------------------------------------------------------------------------
# tokenisation
# --------------------------------------------------------------------------

def tokens(text: str | None, cfg: KeywordConfig | None = None) -> list[str]:
    """Lower-case, split on non-alphanumerics, drop empties and stop words."""
    cfg = cfg or _KC
    return [t for t in _WORD.findall((text or "").lower()) if t not in cfg.stop_words]


def brand_tokens(text: str | None, cfg: KeywordConfig | None = None) -> list[str]:
    """:func:`tokens` kept when ``len >= brand_token_min_len`` and not all digits."""
    cfg = cfg or _KC
    return [t for t in tokens(text, cfg) if len(t) >= cfg.brand_token_min_len and not t.isdigit()]


def despace(text: str | None) -> str:
    """Lower-case with every non-alphanumeric removed."""
    return "".join(_WORD.findall((text or "").lower()))


@dataclass(frozen=True)
class BrandSpec:
    """Match signature of one brand name.

    ``tokens`` are its brand tokens; ``despaced`` the name typed without
    spaces; ``variants`` further despaced spellings (an ampersand written
    out as a word).
    """

    name: str
    tokens: tuple[str, ...]
    despaced: str
    variants: tuple[str, ...] = ()


def brand_spec(name: str | None, cfg: KeywordConfig | None = None) -> BrandSpec | None:
    """Build a brand's match signature.

    Returns ``None`` when the name yields no brand tokens *and* its despaced
    form is shorter than ``despaced_min_len`` (nothing safe to match on).

    A name whose ampersand is spelled out as a word is caught by also
    keeping that despaced variant.
    """
    cfg = cfg or _KC
    if not name:
        return None
    bt = tuple(dict.fromkeys(brand_tokens(name, cfg)))
    cc = despace(name)
    if not bt and len(cc) < cfg.despaced_min_len:
        return None
    variants: tuple[str, ...] = ()
    if "&" in name:
        alt = despace(name.replace("&", " and "))
        if alt and alt != cc:
            variants = (alt,)
    return BrandSpec(name=name, tokens=bt, despaced=cc, variants=variants)


# --------------------------------------------------------------------------
# lexicon
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class RoleResult:
    """Outcome of classifying one keyword."""

    role: str
    has_category_token: bool = False
    is_bare_competitor_head: bool = False   # rival name with no category word: pure conquest head term
    word_count: int = 0

    def as_tuple(self) -> tuple[str, bool, bool, int]:
        return (self.role, self.has_category_token, self.is_bare_competitor_head, self.word_count)


@runtime_checkable
class RoleLexicon(Protocol):
    """Anything that can classify a keyword (plug your own in)."""

    def classify(self, keyword: str) -> RoleResult: ...


@dataclass(frozen=True)
class Lexicon:
    """Token lexicon built from a catalogue (see :func:`build_lexicon`)."""

    own_specs: tuple[BrandSpec, ...] = ()
    competitor_specs: tuple[BrandSpec, ...] = ()
    sibling_specs: tuple[BrandSpec, ...] = ()
    category_vocab: frozenset[str] = frozenset()
    cfg: KeywordConfig = field(default_factory=KeywordConfig)

    def classify(self, keyword: str) -> RoleResult:
        """Classify ``keyword`` with this lexicon (see :func:`classify_role`)."""
        return classify_role(keyword, self.own_specs, self.competitor_specs, self.category_vocab,
                             self.sibling_specs, self.cfg)

    @property
    def is_empty(self) -> bool:
        return not (self.own_specs or self.competitor_specs or self.sibling_specs)


@dataclass(frozen=True)
class MappingLexicon:
    """Lexicon backed by an explicit ``{keyword: role}`` map (e.g. a native
    keyword-type feed), with an optional fallback lexicon for unknown terms."""

    roles: Mapping[str, str]
    fallback: RoleLexicon | None = None

    def classify(self, keyword: str) -> RoleResult:
        role = self.roles.get(norm(keyword))
        if role is not None:
            return RoleResult(role=role, word_count=len(set(norm(keyword).split())))
        if self.fallback is not None:
            return self.fallback.classify(keyword)
        return RoleResult(role=GENERIC, word_count=len(set(norm(keyword).split())))


def _match(kt: set[str], kd: str, specs: Sequence[BrandSpec], blocked: frozenset[str],
           cfg: KeywordConfig) -> bool:
    for s in specs:
        bs = set(s.tokens)
        if bs and bs <= kt and not (blocked and bs <= blocked):
            return True
        for cc in (s.despaced, *s.variants):
            if blocked and cc in blocked:
                continue  # the whole name is one category word: not evidence
            if len(cc) >= cfg.despaced_min_len and cc in kd:
                return True
            if cc and cc == kd:
                return True
    return False


def _abbrev(kt: set[str], specs: Sequence[BrandSpec], blocked: frozenset[str], cfg: KeywordConfig) -> bool:
    for s in specs:
        for bt in s.tokens:
            if len(bt) < cfg.abbrev_brand_min_len:
                continue
            for k in kt:
                if len(k) >= cfg.abbrev_keyword_min_len and k not in blocked and bt.startswith(k):
                    return True
    return False


def classify_role(
    keyword: str,
    own_specs: Sequence[BrandSpec],
    competitor_specs: Sequence[BrandSpec],
    category_vocab: frozenset[str] = frozenset(),
    sibling_specs: Sequence[BrandSpec] = (),
    cfg: KeywordConfig | None = None,
) -> RoleResult:
    """Decide a keyword's role from brand signatures.

    A brand matches the keyword when (in order) its token set is a subset
    of the keyword's tokens, or its despaced name (length >=
    ``despaced_min_len``) is a substring of the despaced keyword, or the two
    despaced strings are equal (catches short names); or, as an
    abbreviation / typo, a keyword token of length >=
    ``abbrev_keyword_min_len`` is a prefix of a brand token of length >=
    ``abbrev_brand_min_len``.

    For sibling and competitor brands, evidence made only of category
    vocabulary is ignored (a rival whose whole signature is a category word
    is not evidence of conquest; the guard covers the token test and a
    despaced name that is itself a category word).  Own brands are *not*
    guarded: an own brand named after a category word captures generic terms.

    Precedence ``own_brand > sibling_brand > competitor > generic``.
    Returns :class:`RoleResult` ``(role, has_category_token,
    is_bare_competitor_head, word_count)``.
    """
    cfg = cfg or _KC
    kt = set(tokens(keyword, cfg))
    kd = despace(keyword)
    none: frozenset[str] = frozenset()
    own = _match(kt, kd, own_specs, none, cfg) or _abbrev(kt, own_specs, none, cfg)
    sis = (not own) and bool(sibling_specs) and (
        _match(kt, kd, sibling_specs, category_vocab, cfg) or _abbrev(kt, sibling_specs, category_vocab, cfg))
    comp = (not own) and (not sis) and (
        _match(kt, kd, competitor_specs, category_vocab, cfg) or _abbrev(kt, competitor_specs, category_vocab, cfg))
    has_cat = bool(kt & category_vocab)
    role = OWN if own else SIBLING if sis else COMPETITOR if comp else GENERIC
    return RoleResult(role=role, has_category_token=has_cat,
                      is_bare_competitor_head=bool(comp and not has_cat), word_count=len(kt))


def build_lexicon(
    brands: Iterable[tuple[str, bool]],
    own_titles: Iterable[str] = (),
    sibling_brands: Iterable[str] = (),
    cfg: KeywordConfig | None = None,
) -> Lexicon:
    """Build the token lexicon from a catalogue.

    ``brands`` is ``(brand name, is_own)``.  Own names become own
    signatures, every other name a competitor signature.  ``sibling_brands``
    is a user-supplied list of same-advertiser brand names (empty = no
    context).  Category vocabulary = brand-grade tokens appearing in at
    least ``catvocab_min_titles`` own product titles, minus any token of an
    own brand signature.  Without titles the vocabulary is empty (no guard,
    ``has_category_token`` always false).
    """
    cfg = cfg or _KC
    own: dict[str, BrandSpec] = {}
    comp: dict[str, BrandSpec] = {}
    for name, is_own in brands:
        s = brand_spec(name, cfg)
        if s is None:
            continue
        (own if is_own else comp).setdefault(s.despaced or s.name, s)
    sis: dict[str, BrandSpec] = {}
    for name in sibling_brands:
        s = brand_spec(name, cfg)
        if s is not None:
            sis.setdefault(s.despaced or s.name, s)
    for k in own:
        comp.pop(k, None)
    own_tok = {t for s in own.values() for t in s.tokens}
    counts: dict[str, int] = {}
    for title in own_titles:
        for t in set(brand_tokens(title, cfg)):
            counts[t] = counts.get(t, 0) + 1
    vocab = frozenset(t for t, n in counts.items() if n >= cfg.catvocab_min_titles and t not in own_tok)
    return Lexicon(tuple(own.values()), tuple(comp.values()), tuple(sis.values()), vocab, cfg)


def lexicon_from_products(
    products: Iterable[Product],
    sibling_brands: Iterable[str] = (),
    cfg: KeywordConfig | None = None,
) -> Lexicon:
    """:func:`build_lexicon` from catalogue records.

    Own products give own brand names and titles; products flagged
    ``is_sibling`` add to the sibling list; the rest are competitors.
    """
    brands: list[tuple[str, bool]] = []
    titles: list[str] = []
    sis = list(sibling_brands)
    for p in products:
        if p.is_own and p.title:
            titles.append(p.title)
        if not p.brand:
            continue
        if p.is_sibling and not p.is_own:
            sis.append(p.brand)
        else:
            brands.append((p.brand, bool(p.is_own)))
    return build_lexicon(brands, titles, sis, cfg)


def classify_keywords(keywords: Iterable[str], lexicon: RoleLexicon | None) -> dict[str, RoleResult]:
    """Classify arbitrary keywords with a lexicon.

    Degrades to all ``generic`` when no lexicon is available.  Returns
    ``{normalised keyword: RoleResult}``.
    """
    out: dict[str, RoleResult] = {}
    for kw in keywords:
        k = norm(kw)
        if not k:
            continue
        out[k] = lexicon.classify(k) if lexicon is not None else RoleResult(GENERIC, word_count=len(set(k.split())))
    return out


# --------------------------------------------------------------------------
# keyword dimension
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class KeywordDim:
    """One row of the keyword dimension."""

    keyword: str
    role: str
    has_category_token: bool
    is_bare_competitor_head: bool
    word_count: int
    search_volume: float | None = None


def keyword_universe(
    perf_rows: Iterable[PerfRow],
    anchor: date | None = None,
    profile: PlatformProfile | None = None,
    cfg: KeywordConfig | None = None,
) -> list[str]:
    """Every search keyword bid in the last ``universe_lookback_days`` days.

    ``anchor`` defaults to the latest date in the rows.  Placement
    pseudo-targets are excluded through ``profile.is_search_keyword``.
    """
    cfg = cfg or _KC
    rows = list(perf_rows)
    if not rows:
        return []
    end = anchor or max(r.date for r in rows)
    start = end - timedelta(days=cfg.universe_lookback_days - 1)
    seen: dict[str, None] = {}
    for r in rows:
        if not (start <= r.date <= end):
            continue
        k = norm(r.targeting_unit)
        if k and (profile is None or profile.is_search_keyword(k)):
            seen.setdefault(k)
    return sorted(seen)


def search_volumes(market_rows: Iterable[MarketRow], window: Window | None = None) -> dict[str, float]:
    """Search-volume proxy: total impressions (all brands) per keyword in ``window``."""
    out: dict[str, float] = {}
    for m in market_rows:
        if in_window(m.date, window):
            k = norm(m.targeting_unit)
            out[k] = out.get(k, 0.0) + (m.impressions or 0.0)
    return out


def keyword_dimension(
    keywords: Iterable[str],
    lexicon: RoleLexicon | None,
    volumes: Mapping[str, float] | None = None,
) -> list[KeywordDim]:
    """Classify the keyword universe into dimension rows.

    Each row is ``{keyword, role, has_category_token,
    is_bare_competitor_head, word_count, search_volume}``; ``search_volume``
    is ``None`` when no volume source was supplied or the term is untracked.
    """
    res = classify_keywords(keywords, lexicon)
    return [KeywordDim(k, r.role, r.has_category_token, r.is_bare_competitor_head, r.word_count,
                       None if volumes is None else volumes.get(k)) for k, r in sorted(res.items())]


def role_map(dimension: Iterable[KeywordDim]) -> dict[str, str]:
    """``{keyword: role}`` view of a dimension."""
    return {d.keyword: d.role for d in dimension}


def dimension_cache_key(advertiser_id: str, platform: str, cfg: KeywordConfig | None = None) -> str:
    """Versioned cache key for a stored dimension: bump
    ``KeywordConfig.lexicon_version`` whenever classifier logic or the
    lexicon inputs change so stale classifications are regenerated."""
    cfg = cfg or _KC
    return f"keyword-dim:v{cfg.lexicon_version}:{advertiser_id}:{platform}"


# --------------------------------------------------------------------------
# role cohorts
# --------------------------------------------------------------------------

def role_cohorts(
    stats: Iterable[KeywordStat] | Mapping[str, KeywordStat],
    roles: Mapping[str, str],
    economics: Economics | None = None,
    cfg: KeywordConfig | None = None,
) -> dict[str, RoleCohort]:
    """Role cohorts: per role, percentiles of ROAS and cost per new user.

    Members are keywords with spend above
    ``money(cohort_spend_floor_ref)``; pass stats over the cohort window
    (reference ``cohort_window_days``).  Percentiles are linearly
    interpolated.  Unknown keywords count as ``generic``.  Consumers read
    percentiles through :func:`cohort_value`, which hides cohorts smaller
    than ``cohort_min_n``.  Returns ``{role: RoleCohort}``.
    """
    cfg = cfg or _KC
    floor = (economics or Economics()).money(cfg.cohort_spend_floor_ref)
    by: dict[str, list[KeywordStat]] = {}
    for s in (stats.values() if isinstance(stats, Mapping) else stats):
        if s.spend > floor and s.roas is not None:
            by.setdefault(roles.get(s.keyword, GENERIC), []).append(s)
    out: dict[str, RoleCohort] = {}
    for role, ss in by.items():
        rs = [s.roas for s in ss if s.roas is not None]
        cp = [s.cost_per_new_user for s in ss if s.cost_per_new_user is not None]
        out[role] = RoleCohort(
            n=len(ss),
            roas_p10=percentile(rs, 0.10), roas_p25=percentile(rs, 0.25),
            roas_p50=percentile(rs, 0.50), roas_p75=percentile(rs, 0.75),
            cpnu_p25=percentile(cp, 0.25), cpnu_p50=percentile(cp, 0.50),
        )
    return out


def cohort_value(
    cohorts: Mapping[str, RoleCohort | Mapping[str, float]] | None,
    role: str,
    key: str,
    min_n: int = _KC.cohort_min_n,
) -> float | None:
    """A cohort percentile, or ``None`` when the role has no cohort or the
    cohort has fewer than ``min_n`` keywords (thin cohorts give unstable
    percentiles)."""
    if not cohorts:
        return None
    c = cohorts.get(role)
    if c is None:
        return None
    if isinstance(c, Mapping):
        n, v = c.get("n", 0), c.get(key)
    else:
        n, v = c.n, getattr(c, key, None)
    if (n or 0) < min_n:
        return None
    return v
