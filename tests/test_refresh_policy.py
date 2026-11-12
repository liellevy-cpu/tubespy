"""Refresh policy: tiering, incremental cursor, time-budgeted batch selection."""
import mini_config as cfg
import refresh_policy as rp

DAY = 86400
NOW = 1_000 * DAY  # arbitrary epoch


def brand(aid, last_scraped=None, last_ad=None, tier=None, expected=None):
    b = {"advertiser_id": aid, "last_scraped_at": last_scraped, "last_ad_at": last_ad}
    if tier: b["tier"] = tier
    if expected is not None: b["expected_new_creatives"] = expected
    return b


def test_tier_active_is_weekly_stale_is_monthly_pin_wins():
    assert rp.tier_for(brand("A", last_ad=NOW - 5 * DAY), NOW) == "weekly"
    assert rp.tier_for(brand("B", last_ad=NOW - 200 * DAY), NOW) == "monthly"
    assert rp.tier_for(brand("C", last_ad=NOW - 200 * DAY, tier="weekly"), NOW) == "weekly"


def test_is_due_respects_cadence_and_first_time():
    assert rp.is_due(brand("new"), NOW) is True                         # never scraped
    assert rp.is_due(brand("w", last_scraped=NOW - 8 * DAY, last_ad=NOW), NOW) is True
    assert rp.is_due(brand("w2", last_scraped=NOW - 3 * DAY, last_ad=NOW), NOW) is False
    # monthly tier: 10 days is not yet due
    assert rp.is_due(brand("m", last_scraped=NOW - 10 * DAY, last_ad=NOW - 100 * DAY), NOW) is False
    assert rp.is_due(brand("m2", last_scraped=NOW - 31 * DAY, last_ad=NOW - 100 * DAY), NOW) is True


def test_scrape_since_is_incremental_cursor():
    assert rp.scrape_since(brand("x")) is None                          # full first scrape
    assert rp.scrape_since(brand("x", last_scraped=NOW - DAY)) == NOW - DAY


def test_plan_run_budget_prioritizes_weekly_and_defers_overflow():
    brands = [
        brand("weekly_old", last_scraped=NOW - 8 * DAY, last_ad=NOW, expected=100),
        brand("monthly", last_scraped=NOW - 40 * DAY, last_ad=NOW - 100 * DAY, expected=100),
        brand("weekly_new", last_scraped=NOW - 8 * DAY, last_ad=NOW, expected=100),
    ]
    # budget fits ~2 brands of 100 creatives at 1s each
    plan = rp.plan_run(brands, NOW, budget_s=250, per_creative_s=1.0, max_new=99)
    picked = [b["advertiser_id"] for b in plan["selected"]]
    assert picked[0].startswith("weekly") and picked[1].startswith("weekly")  # weekly first
    assert [b["advertiser_id"] for b in plan["deferred"]] == ["monthly"]      # overflow deferred
    assert plan["estimated_seconds"] == 200


def test_plan_run_caps_first_time_scrapes():
    firsts = [brand(f"first{i}", expected=100) for i in range(5)]
    plan = rp.plan_run(firsts, NOW, budget_s=10 ** 9, per_creative_s=1.0, max_new=2)
    assert len(plan["selected"]) == 2            # only 2 first-time scrapes per run
    assert len(plan["deferred"]) == 3


def test_huge_brand_is_not_starved():
    huge = brand("huge", last_scraped=NOW - 8 * DAY, last_ad=NOW, expected=10 ** 6)
    plan = rp.plan_run([huge], NOW, budget_s=100, per_creative_s=1.0, max_new=99)
    assert [b["advertiser_id"] for b in plan["selected"]] == ["huge"]  # taken once despite overflow


def test_weekly_capacity_is_positive_and_scales_with_budget():
    small = rp.weekly_capacity(budget_s=3600, per_creative_s=1.0, avg_incremental_creatives=100)
    big = rp.weekly_capacity(budget_s=7200, per_creative_s=1.0, avg_incremental_creatives=100)
    assert big == 2 * small and small > 0
