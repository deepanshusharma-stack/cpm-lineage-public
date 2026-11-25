"""Pure text guards used around the optional language layer.

Nothing here calls a model.  These functions decide whether a piece of
generated text may be shown next to verified numbers.
"""

from __future__ import annotations

import re
from collections import Counter

from .config import DEFAULT_RCA_CONFIG, RcaConfig

_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
_WORD = re.compile(r"[a-z0-9]+")

LANE_STOP_WORDS: frozenset[str] = frozenset(
    {"the", "and", "for", "with", "from", "into", "unknown", "brand", "product", "products", "range", "focused"}
)


def digit_tokens(text: str) -> list[str]:
    """Every number in ``text`` as a canonical string: thousands separators stripped, trailing dot removed."""
    out = []
    for m in _NUM.findall(text or ""):
        tok = m.replace(",", "").rstrip(".")
        if tok:
            out.append(tok)
    return out


def number_guard(text: str, source: str, config: RcaConfig | None = None) -> bool:
    """Decide whether ``text`` states only numbers that already exist in ``source``.

    Rule: every digit sequence in ``text`` (separators stripped) must appear
    in ``source``.  Single digits and calendar years in
    ``[free_year_min, free_year_max]`` are always allowed.  Returns ``True``
    when the text is safe to show.
    """
    c = config or DEFAULT_RCA_CONFIG
    allowed = set(digit_tokens(source))
    for tok in digit_tokens(text):
        if tok in allowed:
            continue
        if tok.isdigit() and (len(tok) == 1 or c.free_year_min <= int(tok) <= c.free_year_max):
            continue
        return False
    return True


def same_numbers(a: str, b: str) -> bool:
    """Decide whether a rewrite kept the numbers: the multisets of digit sequences must be identical.

    Checked in both directions, so nothing is invented and nothing dropped.
    """
    return Counter(digit_tokens(a)) == Counter(digit_tokens(b))


def lane_match(a: str | None, b: str | None) -> bool:
    """Decide whether two positioning lanes overlap.

    Lanes match when they share a word longer than two characters that is
    not a stop word.  Comma-separated lanes are compared word by word.
    ``None`` / empty / "unknown" never match.
    """
    wa = {w for w in _WORD.findall((a or "").lower()) if len(w) > 2 and w not in LANE_STOP_WORDS}
    wb = {w for w in _WORD.findall((b or "").lower()) if len(w) > 2 and w not in LANE_STOP_WORDS}
    return bool(wa & wb)


def trim_to_sentence(text: str, max_words: int) -> str:
    """Cap ``text`` at ``max_words`` words, then cut back to the last full sentence if one exists."""
    words = (text or "").split()
    if len(words) <= max_words:
        return " ".join(words)
    cut = " ".join(words[:max_words])
    end = max(cut.rfind("."), cut.rfind("!"), cut.rfind("?"))
    return cut[: end + 1] if end > 0 else cut
