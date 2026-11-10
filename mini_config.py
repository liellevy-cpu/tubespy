#!/usr/bin/env python3
"""Isolation + scheduling config for OUR deployment on the shared Mac mini.

The mini also runs other agents and Liel's own library. This project must never
touch their resources. Everything we read or write lives under OUR_ROOT, in OUR
OWN store, with OUR OWN schedule and logs. We never open Liel's brands store,
change his add-brand limits, or run during his Sunday refresh.

Henry can override any path or window via environment variables (TUBESPY_* below)
without editing code, so our footprint on the mini is explicit and auditable.
"""
from __future__ import annotations

import os
from pathlib import Path

OUR_ROOT = Path(__file__).resolve().parent

# --- Isolated storage (never shared with Liel) ---
STORE_PATH = Path(os.environ.get("TUBESPY_STORE", OUR_ROOT / "build" / "coolina-brands.sqlite"))
EXPORTS_DIR = Path(os.environ.get("TUBESPY_EXPORTS", OUR_ROOT / "build" / "tubespy-exports"))
LOGS_DIR = Path(os.environ.get("TUBESPY_LOGS", OUR_ROOT / "build" / "logs"))

# --- Schedule (never Sunday = Liel's refresh; nightly hard time budget) ---
SKIP_WEEKDAY = 6                       # 0=Mon .. 6=Sun; we never run Sunday
RUN_START_HOUR = int(os.environ.get("TUBESPY_RUN_HOUR", "1"))   # 01:00 local
NIGHTLY_BUDGET_SECONDS = int(os.environ.get("TUBESPY_BUDGET_SECONDS", str(3 * 3600)))

# --- Conservative scraping (a Google block hits the whole mini IP) ---
SCRAPE_CONCURRENCY = 1                 # one advertiser at a time
MAX_NEW_BRANDS_PER_RUN = int(os.environ.get("TUBESPY_MAX_NEW_PER_RUN", "20"))
PER_CREATIVE_SECONDS = float(os.environ.get("TUBESPY_PER_CREATIVE_SECONDS", "1.3"))

# --- Tiers: how often each class of brand is refreshed (days) ---
TIER_CADENCE_DAYS = {"weekly": 7, "monthly": 30}
# A brand counts as "active" (weekly tier) if it ran ads within this window.
ACTIVE_WINDOW_DAYS = 30
