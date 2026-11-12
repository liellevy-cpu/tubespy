#!/usr/bin/env python3
"""Refresh policy for the growing library: tiered, incremental, time-budgeted.

Designed to scale to thousands of brands on a shared machine without ever
overlapping Liel's Sunday refresh:

  * tiered     — top/active brands refresh weekly, the long tail monthly.
  * incremental— each run pulls only ads newer than that brand's last scrape
                 (`scrape_since`), so steady-state runs are small.
  * budgeted   — `plan_run` packs only as many brands as fit tonight's wall-clock
                 budget; the rest roll to the next run. No run runs long.

Pure functions over plain dicts so the scheduler stays unit-testable and never
needs a live browser. A "brand" dict carries at least:
  { 'advertiser_id', 'last_scraped_at' (epoch secs or None),
    'last_ad_at' (epoch secs or None), 'tier' (optional override),
    'expected_new_creatives' (optional int) }
"""
from __future__ import annotations

import mini_config as cfg


def tier_for(brand: dict, now_ts: float) -> str:
    """'weekly' for top/active brands, else 'monthly'. An explicit brand['tier']
    always wins (Henry/owner can pin a brand)."""
    pinned = brand.get("tier")
    if pinned in cfg.TIER_CADENCE_DAYS:
        return pinned
    last_ad = brand.get("last_ad_at")
    if last_ad and (now_ts - last_ad) <= cfg.ACTIVE_WINDOW_DAYS * 86400:
        return "weekly"
    return "monthly"


def is_due(brand: dict, now_ts: float) -> bool:
    """True if this brand's cadence has elapsed since its last scrape (or never
    scraped). First-time brands are always due."""
    last = brand.get("last_scraped_at")
    if not last:
        return True
    cadence = cfg.TIER_CADENCE_DAYS[tier_for(brand, now_ts)]
    return (now_ts - last) >= cadence * 86400


def scrape_since(brand: dict):
    """Incremental cursor: scrape only ads newer than the last successful scrape.
    None => a full first scrape (the only expensive run a brand ever needs)."""
    return brand.get("last_scraped_at")


def _expected_creatives(brand: dict) -> int:
    if brand.get("expected_new_creatives") is not None:
        return max(0, int(brand["expected_new_creatives"]))
    # First scrape: assume a full (large) pull; incremental: a small delta.
    return 1500 if not brand.get("last_scraped_at") else 80


def plan_run(brands, now_ts, *, budget_s=None, per_creative_s=None,
             max_new=None, expected_fn=None):
    """Pick the batch of due brands that fits tonight's time budget.

    Returns {'selected', 'deferred', 'estimated_seconds'}. Priority: weekly tier
    before monthly; within a tier, longest-waiting first. A brand that alone
    exceeds the whole budget is still taken once (so a huge aggregator can never
    be starved forever), but only when nothing else is selected yet."""
    budget_s = cfg.NIGHTLY_BUDGET_SECONDS if budget_s is None else budget_s
    per = cfg.PER_CREATIVE_SECONDS if per_creative_s is None else per_creative_s
    max_new = cfg.MAX_NEW_BRANDS_PER_RUN if max_new is None else max_new
    est = expected_fn or _expected_creatives

    due = [b for b in brands if is_due(b, now_ts)]
    due.sort(key=lambda b: (tier_for(b, now_ts) != "weekly", b.get("last_scraped_at") or 0))

    selected, spent, new_count = [], 0.0, 0
    for b in due:
        first_time = not b.get("last_scraped_at")
        if first_time and new_count >= max_new:
            continue  # protect the IP: cap costly first-time scrapes per run
        cost = est(b) * per
        if spent + cost > budget_s and selected:
            continue  # doesn't fit tonight; try to pack a smaller one, else defer
        selected.append(b)
        spent += cost
        new_count += 1 if first_time else 0
    chosen = {id(b) for b in selected}
    deferred = [b for b in due if id(b) not in chosen]
    return {"selected": selected, "deferred": deferred, "estimated_seconds": round(spent)}


def weekly_capacity(budget_s=None, per_creative_s=None, avg_incremental_creatives=80,
                    run_days_per_week=6):
    """Rough steady-state capacity: how many incremental brand-refreshes fit per
    week given the nightly budget (first-time scrapes are separately capped)."""
    budget_s = cfg.NIGHTLY_BUDGET_SECONDS if budget_s is None else budget_s
    per = cfg.PER_CREATIVE_SECONDS if per_creative_s is None else per_creative_s
    per_night = int(budget_s / (avg_incremental_creatives * per))
    return per_night * run_days_per_week
