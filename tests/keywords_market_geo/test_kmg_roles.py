"""Keyword role classifier, lexicon and role cohorts."""

from __future__ import annotations

import pytest

from cpm_lineage.keywords import (COMPETITOR, GENERIC, OWN, SIBLING, KeywordConfig, KeywordStat, MappingLexicon,
                                  RoleLexicon, brand_spec, build_lexicon, classify_keywords, cohort_value,
                                  dimension_cache_key, keyword_dimension, keyword_universe, lexicon_from_products,
                                  role_cohorts, role_map, search_volumes, tokens)

BRANDS = [("Acme Peak", True), ("Nook & Vale", True), ("Zest's", True), ("Luminara", True), ("Rivalo", False), ("K1 Chips", False),
          ("Crispster", False), ("Sisterly", False)]
TITLES = ["Acme Peak puppy kibble 2 kg", "Acme Peak night kibble", "Zest's chips salted 50 g",
          "Zest's chips masala pack", "Nook & Vale crisps"]


@pytest.fixture(scope="module")
def lex():
    return build_lexicon(BRANDS, TITLES, sibling_brands=["Sisterly"])


def test_tokens_drop_stop_words_and_punctuation():
    assert tokens("The Best  Kibble, 30 ml (pack of 2)") == ["kibble", "30", "2"]


def test_category_vocab_needs_two_titles_and_excludes_own_brand_tokens(lex):
    assert lex.category_vocab == frozenset({"kibble", "chips"})
    assert "peak" not in lex.category_vocab and "crisps" not in lex.category_vocab


@pytest.mark.parametrize("kw, role", [
    ("acmepeak", OWN),                 # despaced own name
    ("acmepeak kibble", OWN),           # despaced name inside a multi-word keyword, plus a category word
    ("nook vale biscuit", OWN),             # spaced ampersand name via token subset
    ("nookvale", OWN),                    # despaced ampersand name
    ("nookandvale", OWN),                 # ampersand spelled out 
    ("lumin kibble", OWN),              # abbreviation prefix
    ("zests", OWN),                    # short possessive name: despaced exact equality
    ("rivalo", COMPETITOR),
    ("rivalo kibble", COMPETITOR),      # rival + category word
    ("puppy kibble", GENERIC),           # pure category words
    ("chips", GENERIC),                # rival whose signature is only category vocabulary must not capture it
    ("k1 chips", COMPETITOR),          # the same rival's full name still matches
    ("crispster chips", COMPETITOR),   # real rival token plus the category word
    ("sisterly biscuit", SIBLING),
    ("catnip", GENERIC),
])
def test_classify_pinned_cases(lex, kw, role):
    assert lex.classify(kw).role == role


def test_precedence_own_over_competitor(lex):
    assert lex.classify("acme peak vs rivalo").role == OWN


def test_flags_and_word_count(lex):
    r = lex.classify("rivalo")
    assert r.is_bare_competitor_head and not r.has_category_token and r.word_count == 1
    r = lex.classify("rivalo kibble")
    assert not r.is_bare_competitor_head and r.has_category_token and r.word_count == 2
    assert r.as_tuple() == (COMPETITOR, True, False, 2)


def test_abbreviation_lengths():
    lx = build_lexicon([("Crispster", False), ("Peaky", True)])
    assert lx.classify("cris").role == COMPETITOR      # 4-char prefix of a >= 5-char brand token
    assert lx.classify("cri").role == GENERIC          # too short
    cfg = KeywordConfig(abbrev_keyword_min_len=5)
    assert build_lexicon([("Crispster", False)], cfg=cfg).classify("cris").role == GENERIC


def test_category_prefix_is_not_conquest():
    # rival token "chipsy" is prefixed by the category word "chips": blocked for rivals
    lx = build_lexicon([("Chipsy", False), ("Zest", True)], ["Zest chips salted", "Zest chips masala"])
    assert lx.classify("chips").role == GENERIC
    assert lx.classify("chipsy").role == COMPETITOR


def test_own_brand_has_no_category_guard():
    # an own brand named after a category word captures the generic term
    lx = build_lexicon([("Kibble", True)], ["Kibble daily kibble", "Kibble night kibble"])
    assert lx.classify("kibble").role == OWN


def test_brand_spec_none_for_unusable_names():
    assert brand_spec("x1") is None
    assert brand_spec("") is None
    assert brand_spec("Zest") is not None


def test_no_lexicon_degrades_to_generic():
    out = classify_keywords(["Rivalo Kibble", "  acme  peak "], None)
    assert set(out) == {"rivalo kibble", "acme peak"}
    assert all(r.role == GENERIC for r in out.values())


def test_lexicon_is_pluggable(lex):
    mp = MappingLexicon({"house term": OWN}, fallback=lex)
    assert isinstance(mp, RoleLexicon) and isinstance(lex, RoleLexicon)
    assert mp.classify("House  Term").role == OWN
    assert mp.classify("rivalo").role == COMPETITOR
    assert MappingLexicon({}).classify("anything").role == GENERIC


def test_lexicon_from_products_and_dimension(synthetic_dataset):
    d = synthetic_dataset
    lx = lexicon_from_products(d.products)
    assert {s.name for s in lx.own_specs} == {"own-brand"}
    universe = keyword_universe(d.perf, profile=d.profile)
    assert "protein bar" in universe and "rivalb bar" in universe
    dim = keyword_dimension(universe, lx, search_volumes(d.market))
    roles = role_map(dim)
    assert roles["ownbrand bar"] == OWN
    assert roles["rivalb bar"] == COMPETITOR
    assert roles["protein bar"] == GENERIC
    assert all(x.search_volume is None or x.search_volume > 0 for x in dim)


def test_universe_excludes_placements_and_old_rows(make_perf_row, synthetic_profile):
    rows = [make_perf_row(0, targeting_unit="old term"), make_perf_row(450, targeting_unit="feed"),
            make_perf_row(450, targeting_unit="Fresh Term")]
    assert keyword_universe(rows, profile=synthetic_profile) == ["fresh term"]


def test_cache_key_is_versioned():
    assert dimension_cache_key("adv-001", "generic") != dimension_cache_key(
        "adv-001", "generic", KeywordConfig(lexicon_version="2"))


def test_role_cohorts_percentiles_and_floor():
    stats = [KeywordStat(f"k{i}", spend=2000.0, sales=2000.0 * (i + 1), new_users=10.0 * (i + 1)) for i in range(5)]
    stats.append(KeywordStat("tiny", spend=1000.0, sales=99999.0))        # at the floor: excluded (needs > floor)
    co = role_cohorts(stats, {"k0": COMPETITOR})
    g = co[GENERIC]
    assert g.n == 4
    assert g.roas_p50 == pytest.approx(3.5)        # ROAS 2, 3, 4, 5
    assert g.roas_p25 == pytest.approx(2.75) and g.roas_p75 == pytest.approx(4.25)
    assert co[COMPETITOR].n == 1 and co[COMPETITOR].cpnu_p50 == pytest.approx(200.0)


def test_cohort_value_min_n_guard():
    co = role_cohorts([KeywordStat(f"k{i}", spend=5000.0, sales=10000.0) for i in range(11)], {})
    assert cohort_value(co, GENERIC, "roas_p50") is None           # 11 < 12
    assert cohort_value(co, GENERIC, "roas_p50", min_n=11) == pytest.approx(2.0)
    assert cohort_value(co, OWN, "roas_p50", min_n=1) is None
    assert cohort_value({"generic": {"n": 20, "roas_p50": 1.7}}, GENERIC, "roas_p50") == 1.7
