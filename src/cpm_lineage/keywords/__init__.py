"""Keyword subsystem: role classifier and lexicon, role cohorts, CUT / ADD
recommenders, cross-campaign leaks, re-slot, owned niches, cannibalisation."""

from .aggregate import KeywordStat, keyword_stats, scope_filter
from .config import DEFAULT_STOP_WORDS, KeywordConfig
from .leaks import (LeakResult, LeakRow, NicheResult, NicheRow, cannibalisation_rate, exhausted_days_by_keyword,
                    incremental_roas, keyword_leaks, niche_class, owned_niches)
from .recommend import (AddRec, AddResult, CutRec, CutResult, KeywordRecs, RelevanceRow, ShelfPresence,
                        acquisition_spare, add_recs, cut_recs, is_fragment, keyword_recs, relevance_pool,
                        role_from_native, score_add, shelf_presence)
from .roles import (COMPETITOR, GENERIC, OWN, SIBLING, BrandSpec, KeywordDim, Lexicon, MappingLexicon, RoleCohort,
                    RoleLexicon, RoleResult, brand_spec, brand_tokens, build_lexicon, classify_keywords,
                    classify_role, cohort_value, despace, dimension_cache_key, keyword_dimension, keyword_universe,
                    lexicon_from_products, role_cohorts, role_map, search_volumes, tokens)

__all__ = sorted(_n for _n, _v in list(globals().items())
                 if not _n.startswith("_") and not isinstance(_v, type(__import__("sys"))))
