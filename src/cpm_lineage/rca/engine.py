"""The RCA pipeline: scope -> facts -> attribution -> verification -> structured result.

``analyse`` is the pure core over a :class:`Facts` bundle.  ``run_rca``
wraps it for callers holding raw feed records.  Markdown is one renderer
among several; the result itself is structured.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, MutableMapping, Sequence

from cpm_lineage.config import Economics

from .attribute import Attribution, attribute
from .config import RcaConfig
from .decompositions import StepChange, step_scan
from .diff import Snapshot, since_last, snapshot
from .facts import Facts, LossRow, build_facts, own_segments, resolve_scope
from .language import Complete, context_caption, enrich, frame_diff
from .playbooks import (FOCUS_INTENT, INTENTS, Answer, LeverView, Section, answer, build_sections, target_levers,
                        verdict_paragraph)
from .routing import Classifier, route_intent
from .verify import Check, hard_failures, verify

STATUS_OK = "ok"
STATUS_REFUSED = "refused"
STATUS_WARNING = "warning"
STATUS_NEEDS_SEGMENT = "needs_segment"
MAX_SEGMENTS_LISTED = 15


@dataclass(frozen=True)
class RcaResult:
    """Structured outcome of one run.

    ``status``: ``"ok"``; ``"refused"`` (a hard identity failed: only the
    checks are populated, there is no verdict); ``"warning"`` (the scope
    could not be resolved); ``"needs_segment"`` (the question did not name
    one of several own segments).
    """

    status: str
    intent: str = "why"
    message: str = ""
    route: Mapping[str, Any] = field(default_factory=dict)
    facts: Facts | None = None
    attribution: Attribution | None = None
    checks: tuple[Check, ...] = ()
    verdict: str = ""
    answer: Answer | None = None
    levers: LeverView | None = None
    sections: tuple[Section, ...] = ()
    step: StepChange | None = None
    since_last: tuple[str, ...] = ()
    since_last_framing: str | None = None
    context: str | None = None
    snapshot: Snapshot | None = None

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK


def analyse(
    facts: Facts,
    intent: str = "why",
    *,
    history: Sequence[Snapshot] = (),
    complete: Complete | None = None,
    market_description: str = "an online retail marketplace",
    route: Mapping[str, Any] | None = None,
) -> RcaResult:
    """Attribute, verify and assemble the answer for one intent.

    Order: attribution -> verification.  Any failed HARD check refuses the
    report: the result carries the checks and a message, and no verdict,
    causes, levers or answer.  A stale ads feed is a soft failure: the
    funnel axis is unavailable and the run continues.  Otherwise the result
    carries the verdict paragraph, the intent's answer, the lever view, the
    intent's sections, the change-point scan, the since-last-run diff and a
    snapshot for the caller to store.  ``complete`` is optional and only
    ever adds guarded advisory text.
    """
    if intent not in INTENTS:
        raise KeyError(f"unknown intent {intent!r}")
    attr = attribute(facts)
    checks = tuple(verify(facts, attr))
    failed = hard_failures(list(checks))
    if failed:
        return RcaResult(
            status=STATUS_REFUSED, intent=intent, route=dict(route or {}), checks=checks,
            message="REFUSED: " + "; ".join(f"{c.name} ({c.detail})" for c in failed),
        )
    snap = snapshot(facts, attr)
    diff = tuple(since_last(history, snap, facts.config))
    return RcaResult(
        status=STATUS_OK, intent=intent, route=dict(route or {}), facts=facts, attribution=attr, checks=checks,
        verdict=verdict_paragraph(facts, attr), answer=answer(intent, facts, attr),
        levers=target_levers(intent, facts, attr), sections=tuple(build_sections(intent, facts, attr)),
        step=step_scan(facts.offtake.daily, facts.config), since_last=diff,
        since_last_framing=frame_diff(complete, diff, facts.config),
        context=context_caption(complete, facts, attr, market_description=market_description),
        snapshot=snap,
    )


def run_rca(
    dataset: Any,
    *,
    own_brand: str,
    segment: str | None = None,
    product_ids: Sequence[str] | None = None,
    days: int | None = None,
    date_from: Any = None,
    date_to: Any = None,
    cmp: str = "wow",
    intent: str | None = None,
    question: str | None = None,
    overrides: Mapping[str, Any] | None = None,
    config: RcaConfig | None = None,
    economics: Economics | None = None,
    sibling_brands: Sequence[str] = (),
    lanes: Mapping[str, str] | None = None,
    relevance: Mapping[str, bool] | None = None,
    loss_rows: Iterable[LossRow] | None = None,
    history: Sequence[Snapshot] = (),
    classifier: Classifier | None = None,
    complete: Complete | None = None,
    route_cache: MutableMapping[Any, dict[str, Any]] | None = None,
    market_description: str = "an online retail marketplace",
) -> RcaResult:
    """Run the whole pipeline on feed records.

    ``dataset`` is anything exposing ``offtake``, ``market``, ``shelf``,
    ``perf``, ``products`` and (optionally) ``profile`` (for example an
    adapter's ``Dataset``).

    1. Intent: an explicit ``intent`` wins; else the question is routed
       (classifier through the validator, keyword router as the floor) and
       its focus mapped to an intent; else ``"why"``.  Explicit ``segment``
       and ``days`` always override the router.
    2. Scope fallback: with no segment and no products, a single own
       segment is used; several, with a question, return ``needs_segment``
       listing up to 15.
    3. Windows and scope; a ``ValueError`` becomes a ``warning`` result.
    4. Facts in fixed order, optional advisory enrichment, then
       :func:`analyse`.
    """
    offtake = list(getattr(dataset, "offtake", ()) or ())
    products = list(getattr(dataset, "products", ()) or ())
    segments = own_segments(own_brand, offtake, products)
    route: dict[str, Any] = {}
    if question:
        route = route_intent(question, segments, classifier=classifier, cache=route_cache,
                             cache_key=own_brand, config=config)
    chosen = intent or (FOCUS_INTENT.get(route.get("focus", "general"), "why") if route else "why")
    segment = segment or route.get("segment")
    days = days or route.get("days")
    if not segment and not product_ids:
        if len(segments) == 1:
            segment = segments[0]
        elif len(segments) > 1 and question:
            shown = ", ".join(segments[:MAX_SEGMENTS_LISTED])
            return RcaResult(status=STATUS_NEEDS_SEGMENT, intent=chosen, route=route,
                             message=f"Which segment? This advertiser sells in: {shown}.")
    try:
        scope = resolve_scope(
            own_brand=own_brand, segment=segment, product_ids=product_ids, products=products, offtake=offtake,
            days=days, date_from=date_from, date_to=date_to, cmp=cmp, config=config, overrides=overrides,
            economics=economics, sibling_brands=sibling_brands,
        )
    except ValueError as exc:
        return RcaResult(status=STATUS_WARNING, intent=chosen, route=route, message=str(exc))
    facts = build_facts(
        scope, offtake=offtake, market=getattr(dataset, "market", ()) or (),
        shelf=getattr(dataset, "shelf", ()) or (), perf=getattr(dataset, "perf", ()) or (),
        products=products, profile=getattr(dataset, "profile", None), loss_rows=loss_rows, lanes=lanes,
    )
    if relevance is not None:
        facts = enrich(facts, relevance=relevance)
    return analyse(facts, chosen, history=history, complete=complete,
                   market_description=market_description, route=route)


def render_markdown(result: RcaResult) -> str:
    """Render a result as one markdown document.

    ``ok``: header, verdict, answer, since-last diff, the intent's sections,
    the optional advisory context, the verification table and a method
    footer naming the window anchor.  ``refused``: the failed identities
    and the verification table, nothing else.  Other statuses: the message.
    """
    if result.status in (STATUS_WARNING, STATUS_NEEDS_SEGMENT):
        return f"> {result.message}\n"
    table = ["| Check | Result | Detail |", "|---|---|---|"]
    table += [f"| {c.name} | {'pass' if c.ok else 'FAIL'} | {c.detail} |" for c in result.checks]
    if result.status == STATUS_REFUSED:
        return "\n".join(["# Root-cause analysis: REFUSED", "", result.message, "", "## Verification", *table]) + "\n"
    facts = result.facts
    assert facts is not None and result.attribution is not None
    s = facts.scope
    out = [f"# Root-cause analysis: {s.segment} ({INTENTS[result.intent].label})", "",
           f"Current {s.current.start.isoformat()} to {s.current.end.isoformat()} vs prior "
           f"{s.prior.start.isoformat()} to {s.prior.end.isoformat()} ({s.days} days).", "",
           "## Verdict", "", result.verdict, ""]
    if result.answer is not None and result.answer.lines:
        out += ["## Answer", "", *result.answer.lines, ""]
    if result.since_last:
        out += ["## Since the last run", ""]
        if result.since_last_framing:
            out += [result.since_last_framing, ""]
        out += [f"- {line}" for line in result.since_last] + [""]
    for sec in result.sections:
        out += [f"## {sec.title}", "", *[f"- {line}" for line in sec.lines], ""]
    if result.context:
        out += ["## Context (advisory, not verified)", "", result.context, ""]
    method = ("Method: threshold attribution on window-over-window deltas, anchored to the market feed's latest "
              f"date ({s.feed_max.isoformat()}). {s.note}").strip()
    out += ["## Verification", "", *table, "", f"_{method}_"]
    return "\n".join(out) + "\n"
