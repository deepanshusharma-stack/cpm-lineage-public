"""Hand-built fact bundles for the RCA tests.  All numbers are synthetic."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from typing import Any

import pytest

from cpm_lineage.config import Economics
from cpm_lineage.rca import (AdTypeRow, AdvertiserRow, AvailabilityFacts, ConversionFacts, Facts, FunnelFacts,
                             GeoRow, OfftakeFacts, PricingFacts, ProductAvailabilityRow,
                             ProductValueRow, RcaConfig, RivalRow, RivalsFacts, Scope, VoiceFacts, pct_move)
from cpm_lineage.windows import trailing_windows

FEED_MAX = date(2026, 3, 31)


def scope(days: int = 14, config: RcaConfig | None = None, **kw: Any) -> Scope:
    return Scope(own_brand="own-brand", segment="snack-bars", own_products=("sku-1", "sku-2", "sku-3"),
                 windows=trailing_windows(FEED_MAX, days), feed_max=FEED_MAX,
                 config=config or RcaConfig(), economics=Economics(), **kw)


def geo(name: str, pri: float, cur: float, seg_pct: float | None, aggregate: bool = False) -> GeoRow:
    return GeoRow(name, cur, pri, pct_move(cur, pri), seg_pct, aggregate)


def offtake(pri: float, cur: float, seg_pri: float, seg_cur: float, geos: list[GeoRow] | None = None,
            products: list[ProductValueRow] | None = None, daily: tuple = ()) -> OfftakeFacts:
    geos = geos if geos is not None else [geo("geo-north", pri, cur, pct_move(seg_cur, seg_pri))]
    products = products if products is not None else [ProductValueRow("sku-1", cur, pri)]
    return OfftakeFacts(cur_value=cur, pri_value=pri, cur_units=cur / 100.0, pri_units=pri / 100.0,
                        segment_cur_value=seg_cur, segment_pri_value=seg_pri, product_rows=tuple(products),
                        geo_rows=tuple(geos), daily=daily)


def voice(own_p: float, own_c: float, pool_p: float, pool_c: float, **kw: Any) -> VoiceFacts:
    return VoiceFacts(own_c=own_c, own_p=own_p, pool_c=pool_c, pool_p=pool_p, **kw)


def funnel(spend_p: float, spend_c: float, roas_p: float | None = 3.0, roas_c: float | None = 3.0,
           **kw: Any) -> FunnelFacts:
    return FunnelFacts(spend_p=spend_p, spend_c=spend_c, impr_p=kw.pop("impr_p", 1_000_000.0),
                       impr_c=kw.pop("impr_c", 1_000_000.0), roas_p=roas_p, roas_c=roas_c, **kw)


def rival(brand: str, share_p: float, share_c: float, **kw: Any) -> RivalRow:
    return RivalRow(brand=brand, share_c=share_c, share_p=share_p, d_pp=share_c - share_p,
                    value_delta=kw.pop("value_delta", 0.0), how=kw.pop("how", "Balanced: no single lever dominates"),
                    **kw)


def rivals(*rows: RivalRow, **kw: Any) -> RivalsFacts:
    ordered = tuple(sorted(rows, key=lambda r: -abs(r.d_pp)))
    return RivalsFacts(movers=ordered, join_coverage=kw.pop("join_coverage", 100.0), own_brand="own-brand", **kw)


def facts(off: OfftakeFacts, sc: Scope | None = None, **kw: Any) -> Facts:
    return Facts(scope=sc or scope(), offtake=off, **kw)


class Build:
    """Namespace of the builders above, handed to tests as the ``b`` fixture."""

    scope = staticmethod(scope)
    geo = staticmethod(geo)
    offtake = staticmethod(offtake)
    voice = staticmethod(voice)
    funnel = staticmethod(funnel)
    rival = staticmethod(rival)
    rivals = staticmethod(rivals)
    facts = staticmethod(facts)
    FEED_MAX = FEED_MAX

    @staticmethod
    def growth_market() -> Facts:
        """Segment up 12%, own down 6%: promo pullback, two list prices, dilution, availability, a rival gaining."""
        off = offtake(1_000_000.0, 940_000.0, 5_000_000.0, 5_600_000.0, geos=[
            geo("geo-north", 600_000.0, 560_000.0, 11.0), geo("geo-south", 400_000.0, 380_000.0, 13.0)])
        return facts(
            off,
            conversion=ConversionFacts(impr_p=2_000_000.0, impr_c=2_100_000.0, conv_p=0.005, conv_c=0.0045,
                                       org_p=900_000.0, org_c=880_000.0),
            pricing=PricingFacts(depth_c=14.0, depth_p=19.0, segment_depth_c=18.0, segment_depth_p=18.5,
                                 sticker_mix={"sku-1": ((300.0, 0.6), (340.0, 0.4))}),
            voice=voice(100_000.0, 154_000.0, 500_000.0, 970_000.0),
            availability=AvailabilityFacts(own_c=0.78, own_p=0.90, segment_c=0.91, segment_p=0.92, loss_c=40_000.0,
                                           loss_p=15_000.0, geo_flags=(("geo-south", 0.70),)),
            rivals=rivals(rival("own-brand", 20.0, 16.8, is_own=True), rival("rival-b", 10.0, 12.4),
                          rival("rival-c", 15.0, 15.2)),
            funnel=funnel(100_000.0, 137_000.0),
        )

    @staticmethod
    def tide_market() -> Facts:
        """Segment down 18%, own down 20%: a genuine ad pullback inside a market move; promo unchanged."""
        off = offtake(1_000_000.0, 800_000.0, 5_000_000.0, 4_100_000.0, geos=[
            geo("geo-north", 500_000.0, 430_000.0, -8.0), geo("geo-south", 300_000.0, 200_000.0, -35.0),
            geo("geo-east", 200_000.0, 170_000.0, -22.0)])
        return facts(
            off,
            conversion=ConversionFacts(impr_p=2_000_000.0, impr_c=1_500_000.0, conv_p=0.005, conv_c=0.0052,
                                       org_p=900_000.0, org_c=850_000.0),
            pricing=PricingFacts(depth_c=18.2, depth_p=18.0),
            voice=voice(100_000.0, 75_000.0, 500_000.0, 450_000.0),
            availability=AvailabilityFacts(own_c=0.93, own_p=0.94, segment_c=0.92, segment_p=0.92),
            rivals=rivals(rival("own-brand", 20.0, 19.51, is_own=True), rival("rival-b", 10.0, 10.2)),
            funnel=funnel(100_000.0, 80_600.0),
        )

    @staticmethod
    def rich() -> Facts:
        """A wide fixture for the playbook answers: three ad types, a loss estimate, a passenger, three geos."""
        off = offtake(1_000_000.0, 850_000.0, 5_000_000.0, 4_900_000.0, geos=[
            geo("geo-north", 500_000.0, 470_000.0, 3.0), geo("geo-south", 300_000.0, 210_000.0, -30.0),
            geo("geo-east", 200_000.0, 170_000.0, -4.0)],
            products=[ProductValueRow("sku-1", 600_000.0, 650_000.0), ProductValueRow("sku-2", 250_000.0, 350_000.0)])
        ad_types = (AdTypeRow("keyword_listing", 73_000.0, 70_000.0, 219_000.0, 210_000.0),
                    AdTypeRow("recommendation", 27_000.0, 30_000.0, 50_760.0, 60_000.0),
                    AdTypeRow("featured_listing", 0.0, 0.0, 0.0, 0.0, sales_tracked=False))
        return facts(
            off,
            conversion=ConversionFacts(impr_p=2_000_000.0, impr_c=1_900_000.0, conv_p=0.005, conv_c=0.0045),
            voice=voice(100_000.0, 96_000.0, 500_000.0, 520_000.0, advertisers=(
                AdvertiserRow("own-brand", 96_000.0, 100_000.0, is_own=True),
                AdvertiserRow("rival-b", 200_000.0, 190_000.0))),
            availability=AvailabilityFacts(
                own_c=0.88, own_p=0.90, segment_c=0.91, segment_p=0.91, loss_c=42_500.0, loss_p=40_000.0,
                product_rows=(ProductAvailabilityRow("sku-1", 0.95, 6000.0, 6500.0, 12_500.0),
                              ProductAvailabilityRow("sku-2", 0.60, 2500.0, 3500.0, 30_000.0),
                              ProductAvailabilityRow("sku-3", None, 0.0, 0.0, 0.0))),
            pricing=PricingFacts(depth_c=18.0, depth_p=18.5),
            rivals=rivals(rival("own-brand", 20.0, 17.35, is_own=True), rival("rival-b", 10.0, 11.5),
                          rival("rival-c", 15.0, 14.2)),
            funnel=funnel(100_000.0, 100_000.0, roas_p=2.7, roas_c=2.4, ad_types=ad_types, mid_rate_p=4.0,
                          mid_rate_c=3.5, product_spend={"sku-1": 70_000.0, "sku-2": 25_000.0, "sku-3": 5_000.0}),
        )


@pytest.fixture
def b() -> type[Build]:
    return Build


@pytest.fixture
def with_(b):
    """``with_(facts, voice=...)``: copy a bundle replacing whole fact records."""
    return lambda f, **kw: replace(f, **kw)


def daily_series(values: list[float], end: date = FEED_MAX) -> tuple:
    start = end - timedelta(days=len(values) - 1)
    return tuple((start + timedelta(days=i), v) for i, v in enumerate(values))


@pytest.fixture
def series():
    return daily_series
