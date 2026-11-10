# Coolina library on the Mac mini — isolation & scale design

The mini also runs other agents and **Liel's** own library. Our project is built to
run next to them without ever touching their resources, and to scale to thousands of
brands without a run ever overlapping Liel's Sunday refresh.

## 1. Full isolation (nothing shared)

Config lives in `mini_config.py`; every path/window is also an env var Henry can set.

| Resource | Ours (isolated) | Liel's (never touched) |
|---|---|---|
| Brand store (DB) | `build/coolina-brands.sqlite` (`TUBESPY_STORE`) | his own brands store |
| Exports / raw scrapes | `build/tubespy-exports/` (`TUBESPY_EXPORTS`) | — |
| Logs | `build/logs/` (`TUBESPY_LOGS`) | — |
| Schedule (cron) | our own unit, **never Sunday**, starts 01:00 (`TUBESPY_RUN_HOUR`) | his Sunday refresh |
| Add-brand limits | our own path (trusted, internal) | his public rate limits unchanged |

Guarantees in code: we open only `mini_config.STORE_PATH`; we never import or write
Liel's store, change his add-brand limits, or run on `SKIP_WEEKDAY` (Sunday).

## 2. Refresh design (`refresh_policy.py`, unit-tested)

- **Tiered** — `tier_for()`: brands that ran ads in the last 30 days refresh **weekly**;
  the long tail refreshes **monthly**. The owner can pin a brand's tier.
- **Incremental** — `scrape_since()`: each run pulls only ads **newer than that brand's
  last successful scrape**. Only a brand's first scrape is large; every run after is a
  small delta.
- **Time-budgeted** — `plan_run()`: packs only as many due brands as fit tonight's
  wall-clock budget (default 3h); the rest roll to the next night. A run can't bleed
  into Liel's window. First-time (full) scrapes are capped per run to protect the IP.
- **Conservative** — concurrency 1 (one advertiser at a time); a Google block hits the
  whole mini IP, so we stay gentle by default.

## 3. Capacity estimate (conservative defaults)

Assumptions: 3h nightly budget, 6 nights/week (skip Sunday), measured scrape rate
~0.8s per creative on this machine — we budget at **1.3s** to stay safe.

| Work type | Per-brand cost | Fits per week |
|---|---|---|
| **Steady-state** incremental refresh (~80 new ads/brand) | ~1.7 min | **~600 brand-refreshes/week** |
| **Onboarding** a normal new brand (~100–400 ads) | ~2–9 min | **~120 new brands/week** (cap 20/night) |
| Onboarding a mega-aggregator (3,000+ ads, e.g. AdTiger) | ~45 min | a few/night — these are the exception |

**Bottom line:** with these defaults the design comfortably refreshes **~600 active
brands weekly** and onboards **~100+ new brands/week**, while never running Sunday and
never exceeding ~3h/night. Raising `TUBESPY_BUDGET_SECONDS` scales both numbers
linearly; the binding constraint is the conservative IP budget, not the code.

## 4. Wired vs. needs Henry

- **Wired here:** isolated config, tiering, incremental cursor, time-budgeted planner,
  conservative concurrency, and our own store (already holds the 6 advertiser seeds).
- **Henry sets on the mini:** the env vars above, a cron/launchd unit that calls our
  worker nightly (never Sunday, after Liel's traffic), and the two secrets
  (`YOUTUBE_API_KEY`, `ANTHROPIC_API_KEY`) as untracked env values. Open questions for
  him are in `HENRY_QUESTIONS.md`.
