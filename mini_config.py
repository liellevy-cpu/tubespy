#!/usr/bin/env python3
"""Isolation + scheduling config for OUR deployment on the shared Mac mini.

The mini also runs other agents (Sam) and Liel's own library. This project must
never touch their resources. Everything we read or write lives under OUR_ROOT, in
OUR OWN store, with OUR OWN schedule and logs. We never open Liel's brands store,
change his add-brand limits, run during his Sunday refresh, or publish to his live
page.

Henry can override any path, window, budget, or safety switch via environment
variables (TUBESPY_* below) without editing code, so our footprint on the mini is
explicit and auditable.
"""
from __future__ import annotations

import os
from pathlib import Path

OUR_ROOT = Path(__file__).resolve().parent


def _flag(name: str, default: bool = False) -> bool:
    return os.environ.get(name, "1" if default else "0").strip().lower() in ("1", "true", "yes", "on")


# --- Isolated storage (never shared with Liel) ---
STORE_PATH = Path(os.environ.get("TUBESPY_STORE", OUR_ROOT / "build" / "coolina-brands.sqlite"))
EXPORTS_DIR = Path(os.environ.get("TUBESPY_EXPORTS", OUR_ROOT / "build" / "tubespy-exports"))
LOGS_DIR = Path(os.environ.get("TUBESPY_LOGS", OUR_ROOT / "build" / "logs"))

# --- Mini-wide Google scrape lock (A) ---
# A single lock serializes EVERY Google Transparency request on the mini, so our
# scrapes wait for Liel's and Sam's (one scraper at a time = no "unusual traffic").
# Henry points this at the existing shared lock; default is our own local file.
GOOGLE_LOCK_PATH = Path(os.environ.get("TUBESPY_GOOGLE_LOCK", OUR_ROOT / "build" / "google-scrape.lock"))

# --- Key handling (C): never fall back to a shared / Liel key ---
# When TUBESPY_NO_SHARED_KEY=1, the ~/.hermes/.env fallback is disabled and a
# missing YOUTUBE_DATA_API_KEY is a hard, clear error instead of a silent fallback.
NO_SHARED_KEY = _flag("TUBESPY_NO_SHARED_KEY", default=False)

# --- Shared raw data (E): read-only path to Liel's raw scrapes for the All tab ---
# Our code may READ this to show Liel's advertisers in All without re-scraping; it
# must NEVER write under it. None unless Henry sets it.
SHARED_RAW_DIR = Path(os.environ["TUBESPY_SHARED_RAW"]) if os.environ.get("TUBESPY_SHARED_RAW") else None

# --- Used-tab seed (F): the video(s) that seed the Used tab, no longer hard-coded ---
USED_SEED_VIDEO_IDS = [v.strip() for v in os.environ.get("TUBESPY_USED_SEED", "GmrgKvRelpk").split(",") if v.strip()]

# --- Publish target (G): default a SEPARATE preview site, never Liel's live page ---
# Liel's live library publishes to the slug below; we refuse to publish there.
LIVE_PUBLISH_SLUG = "youtube-competitor-library"
PUBLISH_DIR = Path(os.environ.get("TUBESPY_PUBLISH_DIR", OUR_ROOT / "build" / "preview"))
PUBLISH_SLUG = os.environ.get("TUBESPY_PUBLISH_SLUG", "coolina-library-preview")

# --- Schedule (never Sunday = Liel's refresh) ---
SKIP_WEEKDAY = 6                       # 0=Mon .. 6=Sun; we never run Sunday
RUN_TZ = os.environ.get("TUBESPY_RUN_TZ", "Asia/Jerusalem")
# Nightly run window (D): 00:30–02:30 Israel time by default.
RUN_WINDOW_START = os.environ.get("TUBESPY_WINDOW_START", "00:30")
RUN_WINDOW_END = os.environ.get("TUBESPY_WINDOW_END", "02:30")
# Hard wall-clock budget per night (D). 2h now; raise to 3h later via the env var.
NIGHTLY_BUDGET_SECONDS = int(os.environ.get("TUBESPY_BUDGET_SECONDS", str(2 * 3600)))

# --- Conservative scraping (a Google block hits the whole mini IP) ---
SCRAPE_CONCURRENCY = 1                 # one advertiser at a time
# Max first-time (new) brand scrapes per night (D/I): 5 now; raise to 20 later.
MAX_NEW_BRANDS_PER_RUN = int(os.environ.get("TUBESPY_MAX_NEW_PER_RUN", "5"))
PER_CREATIVE_SECONDS = float(os.environ.get("TUBESPY_PER_CREATIVE_SECONDS", "1.3"))
# Stop the whole night after this many Google "too many requests" (429) hits,
# counting even the ones the scraper retries per ad.
MAX_RATE_LIMIT_HITS = int(os.environ.get("TUBESPY_MAX_429", "2"))

# --- Discovery cadence: once a week on a fixed non-Sunday weekday, and only a
# handful of advertisers checked per night (each probe is a live Google request). ---
DISCOVERY_WEEKDAY = int(os.environ.get("TUBESPY_DISCOVERY_WEEKDAY", "2"))   # 0=Mon..6=Sun; 2=Wed
DISCOVERY_MAX_CHECKED = int(os.environ.get("TUBESPY_DISCOVERY_MAX", "10"))  # advertisers probed/night

# --- Tiers: how often each class of brand is refreshed (days) ---
TIER_CADENCE_DAYS = {"weekly": 7, "monthly": 30}
# A brand counts as "active" (weekly tier) if it ran ads within this window.
ACTIVE_WINDOW_DAYS = 30

# --- Landing-domain backfill (slow, gradual; fills the click-through domain of
# existing videos that pre-date scrape-time capture, so discovery gains seeds).
# Deliberately tiny and well-paced: a secondary job that must never burst Google
# nor eat the scrape budget. A few per night, spaced seconds apart, is the point.
BACKFILL_MAX_PER_RUN = int(os.environ.get("TUBESPY_BACKFILL_MAX_PER_RUN", "40"))
BACKFILL_ITEM_SECONDS = float(os.environ.get("TUBESPY_BACKFILL_ITEM_SECONDS", "6.0"))  # spacing per fetch
BACKFILL_BUDGET_SECONDS = int(os.environ.get("TUBESPY_BACKFILL_BUDGET_SECONDS", str(20 * 60)))  # <= nightly slice
