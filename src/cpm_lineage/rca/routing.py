"""Question routing: a deterministic keyword router under an optional classifier.

The classifier (any callable, typically a language model) may *propose* a
segment, a window length and a focus.  ``validate_intent`` decides whether
each proposal is admissible, and the keyword router fills whatever is
missing, so a question always routes, with or without a classifier.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Mapping, MutableMapping, Sequence

from .config import DEFAULT_RCA_CONFIG, FOCUSES, RcaConfig
from .playbooks import FOCUS_INTENT

Classifier = Callable[[str, Sequence[str]], Mapping[str, Any] | None]
"""``classifier(question, segments) -> {"segment"?, "days"?, "focus"?}`` or ``None``."""

DAY_PATTERNS: tuple[tuple[str, int], ...] = (
    (r"\b(two|2)\s+months?\b", 60),
    (r"\bquarter", 90),
    (r"\bfortnight|\b(two|2)\s+weeks?\b", 14),
    (r"\bmonth", 30),
    (r"\bweek", 7),
)

FOCUS_PATTERNS: tuple[tuple[str, str], ...] = (
    ("sov", r"share of voice|\bsov\b|\bvoice\b|visibilit|ad spend|\binvest"),
    ("availability", r"availab|out of stock|\bstock|\bsupply|\boos\b"),
    ("pricing", r"\bpric|discount|promo|coupon"),
    ("competition", r"compet|rival|market share|losing share|share loss"),
    ("conversion", r"\broas\b|conver|efficien|\bcpm\b"),
    ("offtake", r"sales|sell-?out|revenue|units|declin|drop|\bfall|\bfell\b|\bwhy\b"),
)


def keyword_route(question: str, segments: Sequence[str] = (), config: RcaConfig | None = None) -> dict[str, Any]:
    """Route a free-text question with patterns only.

    * segment: the LONGEST own-segment name that appears in the lower-cased
      question;
    * days: the first matching window phrase (two months 60, quarter 90,
      fortnight / two weeks 14, month 30, week 7);
    * focus: the first match in the order sov, availability, pricing,
      competition, conversion, sell-out.

    The multi-word phrases are tested before the single words they contain,
    which is the intended reading of "two months" and "two weeks".  Returns
    a dict holding only the keys that matched.
    """
    q = (question or "").lower()
    out: dict[str, Any] = {}
    hits = [s for s in segments if s and s.lower() in q]
    if hits:
        out["segment"] = max(hits, key=len)
    for pattern, days in DAY_PATTERNS:
        if re.search(pattern, q):
            out["days"] = days
            break
    for focus, pattern in FOCUS_PATTERNS:
        if re.search(pattern, q):
            out["focus"] = focus
            break
    return out


def validate_intent(raw: Any, segments: Sequence[str] = (), config: RcaConfig | None = None) -> dict[str, Any]:
    """Gate a classifier's output: the model classifies, this function decides.

    Kept only when admissible: ``segment`` must case-insensitively equal a
    listed segment (the listed spelling is returned); ``days`` must be in
    ``allowed_days``; ``focus`` must be a known focus.  Anything else,
    including a non-mapping, is dropped.
    """
    c = config or DEFAULT_RCA_CONFIG
    out: dict[str, Any] = {}
    if not isinstance(raw, Mapping):
        return out
    seg = raw.get("segment")
    if isinstance(seg, str):
        match = next((s for s in segments if s.lower() == seg.strip().lower()), None)
        if match is not None:
            out["segment"] = match
    days = raw.get("days")
    if isinstance(days, (int, float)) and not isinstance(days, bool) and int(days) == days and int(days) in c.allowed_days:
        out["days"] = int(days)
    focus = raw.get("focus")
    if isinstance(focus, str) and focus.strip().lower() in FOCUSES:
        out["focus"] = focus.strip().lower()
    return out


def route_intent(
    question: str,
    segments: Sequence[str] = (),
    *,
    classifier: Classifier | None = None,
    cache: MutableMapping[Any, dict[str, Any]] | None = None,
    cache_key: Any = None,
    config: RcaConfig | None = None,
) -> dict[str, Any]:
    """Route a question to ``{segment?, days?, focus?, intent, via}``.

    The classifier's output (if any, and if it does not raise) goes through
    :func:`validate_intent`; the keyword router fills any missing key.
    ``via`` is ``"ai"``, ``"rules"`` or ``"ai+rules"``; ``intent`` is the
    focus mapped through ``FOCUS_INTENT`` (default ``"why"``).  When a
    ``cache`` mapping is passed the result is memoised under ``(cache_key,
    question)`` and the cache is capped at ``route_cache_cap`` entries
    (oldest dropped).
    """
    c = config or DEFAULT_RCA_CONFIG
    key = (cache_key, question)
    if cache is not None and key in cache:
        return dict(cache[key])
    ai: dict[str, Any] = {}
    if classifier is not None:
        try:
            ai = validate_intent(classifier(question, segments), segments, c)
        except Exception:      # the classifier is advisory: any failure means absence
            ai = {}
    rules = keyword_route(question, segments, c)
    out = dict(ai)
    filled = False
    for k, v in rules.items():
        if k not in out:
            out[k] = v
            filled = True
    out["via"] = "ai+rules" if ai and filled else "ai" if ai else "rules"
    out["intent"] = FOCUS_INTENT.get(out.get("focus", "general"), "why")
    if cache is not None:
        while len(cache) >= c.route_cache_cap:
            cache.pop(next(iter(cache)))
        cache[key] = dict(out)
    return out
