"""Root-cause analysis for "why did sales or share move between two windows".

Pipeline: scope -> fact extractors -> ordered threshold attribution ->
identity verification -> intent playbook.  No model inference sits in the
numeric path; an optional injected text callable may add guarded advisory
prose (see :mod:`cpm_lineage.rca.language`).
"""

from .attribute import (CAUSE_TITLES, QUADRANT_READS, RULED_OUT_HYPOTHESES, Attribution, Cause, Lever, Mechanism,
                        Pocket, RuledOut, attribute, quadrant)
from .config import AXES, DEFAULT_RCA_CONFIG, FOCUSES, RcaConfig
from .decompositions import (RoasIdentity, StepChange, mix_vs_rate, roas_from_identity, roas_identity,
                             rounded_parts, step_scan, two_factor_units, value_decomposition)
from .diff import Snapshot, diff_snapshots, previous_snapshot, since_last, snapshot, update_history
from .engine import RcaResult, analyse, render_markdown, run_rca
from .facts import (AdTypeRow, AdvertiserRow, AvailabilityFacts, ConversionFacts, Facts, FunnelFacts, GeoRow,
                    KeywordPoolRow, LaunchRow, LineRow, LossRow, OfftakeFacts, PricingFacts,
                    ProductAvailabilityRow, ProductValueRow, RivalRow, RivalsFacts, Scope, VoiceFacts, build_facts,
                    extract_availability, extract_conversion, extract_funnel, extract_offtake, extract_pricing,
                    extract_rivals, extract_voice, how_they_win, own_segments, pct_move, resolve_scope,
                    sticker_points)
from .guards import digit_tokens, lane_match, number_guard, same_numbers, trim_to_sentence
from .language import (Complete, ask_report, context_caption, enrich, fact_digest, fill_cache, frame_diff, is_lane,
                       rewrite_blocks, verified_terms)
from .playbooks import (ANSWERS, FOCUS_CAUSES, FOCUS_INTENT, INTENTS, PRESET_INTENTS, SECTIONS, Answer, GeoBuckets,
                        Intent, LeverView, RankedAction, Section, answer, availability_answer, build_sections,
                        exposed_rival, focus_answer, geo_answer, geo_buckets, geo_read, growth_answer,
                        intent_levers, invest_verdict, mix_verdict, rank_actions, roas_answer, scale_cut_answer,
                        share_answer, target_levers, top_action_answer, verdict_paragraph, waste_answer,
                        waste_items)
from .routing import Classifier, keyword_route, route_intent, validate_intent
from .verify import Check, hard_failures, is_verified, verify

__all__ = [n for n in dir() if not n.startswith("_")]
