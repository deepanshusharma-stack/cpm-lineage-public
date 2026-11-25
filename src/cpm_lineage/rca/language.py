"""The optional language layer: an injected callable, quarantined and advisory.

The numeric path never depends on it.  A caller may inject
``complete(prompt) -> str | None`` (any text generator).  Every use here is
guarded and fails to ABSENCE: if the callable is missing, raises, returns
nothing, or returns text that states a number the engine did not compute,
the templated output is kept and nothing is shown.

No provider, endpoint, key or model name lives in this library.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable, Iterable, Mapping, Sequence

from .attribute import Attribution
from .config import DEFAULT_RCA_CONFIG, RcaConfig
from .facts import Facts, VoiceFacts
from .guards import number_guard, same_numbers, trim_to_sentence

Complete = Callable[[str], "str | None"]
"""The only hook: ``complete(prompt) -> text`` (or ``None``).  Off by default."""


def _call(complete: Complete | None, prompt: str) -> str | None:
    if complete is None:
        return None
    try:
        out = complete(prompt)
    except Exception:      # advisory layer: any failure means absence
        return None
    return out.strip() if isinstance(out, str) and out.strip() else None


def fact_digest(facts: Facts, attr: Attribution) -> str:
    """A brand-masked, currency-free digest of a run, safe to hand to an external text generator.

    Contains percentages, the quadrant, cause titles and geo names only: no
    brand name (the own brand is "the brand", rivals are "a rival"), no
    product ids and no money amounts.  It doubles as the source text for
    :func:`~cpm_lineage.rca.guards.number_guard` on anything generated from it.
    """
    s = facts.scope
    lines = [
        f"Segment: {s.segment}. Window: {s.days} days ending {s.feed_max.isoformat()}.",
        f"The brand's sales moved {attr.own_pct:+.1f}% while the segment moved {attr.segment_pct:+.1f}%.",
        f"Share {attr.share_p:.2f}% -> {attr.share_c:.2f}%. Quadrant: {attr.quadrant}.",
    ]
    if attr.geo_avoid:
        lines.append("Segment collapsed in: " + ", ".join(f"{g} ({p:+.0f}%)" for g, p in attr.geo_avoid) + ".")
    if attr.geo_spend:
        lines.append("The brand outperformed in: " + ", ".join(f"{g} ({o:+.0f}%)" for g, o, _ in attr.geo_spend) + ".")
    if attr.causes:
        lines.append("Confirmed causes: " + "; ".join(dict.fromkeys(c.title for c in attr.causes)) + ".")
    if attr.mech is not None:
        lines.append(f"The unit move is {attr.mech.read}.")
    if facts.availability is not None and facts.availability.own_c is not None:
        lines.append(f"Availability {100 * facts.availability.own_c:.0f}%.")
    return "\n".join(lines)


def context_caption(
    complete: Complete | None,
    facts: Facts,
    attr: Attribution,
    *,
    market_description: str = "an online retail marketplace",
) -> str | None:
    """Ask for a short external-context caption (season, weather, holidays) for the window.

    The prompt carries only :func:`fact_digest`.  The reply is trimmed to
    ``context_max_words`` and the last full sentence, and must pass
    ``number_guard`` against the digest; otherwise ``None``.
    """
    cfg = facts.config
    digest = fact_digest(facts, attr)
    prompt = (f"Context: {market_description}.\n{digest}\n\nIn at most {cfg.context_max_words} words, name "
              "external context (season, weather, holidays, events) that could plausibly bear on this window. "
              "Do not state any number that is not in the digest. Do not name brands.")
    out = _call(complete, prompt)
    if out is None:
        return None
    out = trim_to_sentence(out, cfg.context_max_words)
    return out if out and number_guard(out, digest, cfg) else None


def rewrite_blocks(complete: Complete | None, blocks: Sequence[str], config: RcaConfig | None = None) -> list[str]:
    """Optionally rewrite templated prose, block by block, never changing a number.

    A rewritten block is accepted only if :func:`same_numbers` holds against
    the original (digit multisets identical in both directions) and it is
    shorter than ``narrative_max_length_ratio`` x the original; otherwise
    the template is kept for that block.  Returns a list the same length as
    ``blocks``.
    """
    c = config or DEFAULT_RCA_CONFIG
    out = []
    for block in blocks:
        new = _call(complete, "Rewrite this paragraph in plain, direct prose. Keep every number exactly as "
                              f"written and add none.\n\n{block}")
        ok = new is not None and same_numbers(new, block) and len(new) < c.narrative_max_length_ratio * len(block)
        out.append(new if ok else block)
    return out


def ask_report(complete: Complete | None, report_text: str, question: str,
               config: RcaConfig | None = None) -> str | None:
    """Answer a follow-up question over a finished report.

    The reply must pass ``number_guard`` against the report text (it may
    quote the report's numbers, never introduce one); otherwise the answer
    is refused and ``None`` is returned.
    """
    out = _call(complete, "Answer the question using ONLY this report. Do not compute or invent numbers.\n\n"
                          f"REPORT:\n{report_text}\n\nQUESTION: {question}")
    return out if out is not None and number_guard(out, report_text, config) else None


def frame_diff(complete: Complete | None, diff_lines: Sequence[str], config: RcaConfig | None = None) -> str | None:
    """One framing sentence over a run-over-run diff, guarded by ``number_guard`` against the diff body."""
    if not diff_lines:
        return None
    body = "\n".join(diff_lines)
    out = _call(complete, f"In one sentence, say what changed since the last run.\n\n{body}")
    return out if out is not None and number_guard(out, body, config) else None


def fill_cache(
    cache: Mapping[str, Any],
    keys: Iterable[str],
    ask: Callable[[list[str]], Mapping[str, Any] | None] | None,
    validator: Callable[[Any], bool] = lambda v: True,
) -> dict[str, Any]:
    """Enrich-once cache: ask only for the missing keys, in one batch, and keep only valid answers.

    ``ask(missing_keys)`` returns ``{key: value}``.  An answer is stored
    only if its key was asked for and its value passes ``validator``.  A
    failing or absent ``ask`` leaves the cache unchanged.  Returns a NEW
    dict (the caller persists it wherever it likes, ideally somewhere a
    person can review).
    """
    out = dict(cache)
    missing = [k for k in dict.fromkeys(keys) if k not in out]
    if not missing or ask is None:
        return out
    try:
        got = ask(missing) or {}
    except Exception:
        return out
    asked = set(missing)
    for k, v in got.items():
        if k in asked and validator(v):
            out[k] = v
    return out


def is_lane(value: Any) -> bool:
    """Validator for a positioning lane: a 1-4 word string (``"unknown"`` is allowed)."""
    return isinstance(value, str) and 1 <= len(value.split()) <= 4


def verified_terms(terms: Iterable[str], voice: VoiceFacts | None) -> tuple[str, ...]:
    """Keep only proposed keyword ideas that have live demand in the data.

    A proposed term is shown only if it is a listed keyword with a positive
    current pool; its numbers always come from the engine, never from the
    proposer.
    """
    if voice is None:
        return ()
    live = {k.keyword.lower() for k in voice.keywords if k.pool_c > 0}
    return tuple(dict.fromkeys(t.strip().lower() for t in terms if t and t.strip().lower() in live))


def enrich(
    facts: Facts,
    *,
    relevance: Mapping[str, bool] | None = None,
    expansion: Iterable[str] | None = None,
    lanes: Mapping[str, str] | None = None,
    siblings: Iterable[str] | None = None,
) -> Facts:
    """Attach advisory fields to a copy of the facts; the sums feeding the gates are never touched.

    * ``relevance``: ``keyword -> bool`` screen; filters SUGGESTIONS
      (pockets) only.
    * ``expansion``: proposed keyword ideas; only :func:`verified_terms`
      survive.
    * ``lanes``: ``brand -> positioning lane``; upgrades the absorption
      evidence only.
    * ``siblings``: same-house brand names; a sibling can never be the
      absorbing rival.

    Returns a new :class:`Facts`.
    """
    voice, rivals = facts.voice, facts.rivals
    if voice is not None and (relevance is not None or expansion is not None):
        voice = replace(
            voice,
            relevant=dict(relevance) if relevance is not None else voice.relevant,
            expansion=verified_terms(expansion, voice) if expansion is not None else voice.expansion,
        )
    if rivals is not None and (lanes or siblings):
        ln = {k.strip().lower(): v for k, v in (lanes or {}).items()}
        sib = {s.strip().lower() for s in (siblings or ())}
        movers = tuple(replace(
            m, lane=ln.get(m.brand.lower(), m.lane),
            is_sibling=(m.is_sibling or m.brand.lower() in sib) and not m.is_own) for m in rivals.movers)
        rivals = replace(rivals, movers=movers,
                         own_lane=ln.get((rivals.own_brand or "").lower(), rivals.own_lane))
    return replace(facts, voice=voice, rivals=rivals)


__all__ = [
    "Complete", "ask_report", "context_caption", "enrich", "fact_digest", "fill_cache",
    "frame_diff", "is_lane", "rewrite_blocks", "verified_terms",
]
