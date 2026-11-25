"""Hand-built context factories for the action-family tests (synthetic names only)."""

from __future__ import annotations

import dataclasses

from cpm_lineage.actions.primitives import (ActionContext, CampaignStat, KeywordMarketStat,
                                            KeywordStat)
from cpm_lineage.adapters.base import AdTypeCapability
from cpm_lineage.adapters.synthetic import synthetic_profile
from cpm_lineage.config import Economics
from cpm_lineage.schema import BudgetTelemetry

SEG = "seg-a"
KW, RECO, REACH, SHELF = "keyword_listing", "recommendation", "featured_listing", "shelf_slot"


def profile(**over):
    p = synthetic_profile()
    types = dict(p.ad_types)
    types[SHELF] = AdTypeCapability(name=SHELF, capability_class="placement", targeting="none",
                                    keyword_drillable=False)
    return dataclasses.replace(p, ad_types=types, **over)


def camp(eid="c1", roas=2.0, spend=100_000.0, *, ad_type=KW, status="ACTIVE", segment=SEG,
         s7=None, s3=None, own=None, direct=None, pure=True):
    return CampaignStat(entity_id=eid, segment=segment, status=status, ad_type=ad_type, spend=spend,
                        spend_7d=spend / 4 if s7 is None else s7,
                        spend_3d=spend / 10 if s3 is None else s3,
                        roas_primary=roas, roas_direct=direct, is_pure=pure, own_brand_share=own)


def tel(eid="c1", *, bud=1000.0, spd=950.0, exh=400.0, nd=20, exh_d=20, roas=4.0, lo=None, hi=None,
        recent=None, suff=None, bud_l=None, bud_p3=None, sp=100_000.0, im=1_000_000.0):
    return BudgetTelemetry(entity_id=eid, n_days=nd, avg_budget=bud, avg_spend=spd, spend=sp,
                           impressions=im, roas=roas, roas_recent=recent, roas_lo=lo, roas_hi=hi,
                           exhaust_minute=exh, exhaust_days=exh_d, budget_latest=bud_l,
                           budget_before_recent=bud_p3, sufficiency_3d=suff)


def kw(keyword, eid="c1", spend=20_000.0, roas=2.0, role="generic", *, nu=None, vol=None, segment=SEG):
    return KeywordStat(keyword=keyword, entity_id=eid, spend=spend, roas=roas, role=role,
                       segment=segment, new_users=nu, search_volume=vol)


def kwm(keyword, spend=30_000.0, dr=2.0, pos=3.0, *, segment=SEG, **kw_):
    return KeywordMarketStat(keyword=keyword, segment=segment, spend=spend, direct_roas=dr,
                             position=pos, **kw_)


def ctx(campaigns=(), *, telemetry=(), keywords=(), profile_=None, be=1.0, rule_profile="classic",
        **kw_):
    econ = kw_.pop("economics", None) or Economics(breakeven=be)
    return ActionContext(campaigns=list(campaigns), keywords=list(keywords),
                         telemetry={t.entity_id: t for t in telemetry},
                         profile=profile_ or profile(), economics=econ, rule_profile=rule_profile,
                         **kw_)


def one(cards, **match):
    hits = [c for c in cards if all(getattr(c, k) == v for k, v in match.items())]
    assert len(hits) == 1, f"expected one card matching {match}, got {[(c.family, c.type, c.title) for c in cards]}"
    return hits[0]


def none(cards, **match):
    hits = [c for c in cards if all(getattr(c, k) == v for k, v in match.items())]
    assert not hits, f"unexpected cards {[(c.family, c.type, c.title, c.entity_id) for c in hits]}"
