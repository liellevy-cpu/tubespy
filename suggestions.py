#!/usr/bin/env python3
"""Weekly "Suggestions": 10 AI-picked swipes for Coolina.

Pipeline (cheap first, one paid call at the end):
  1. Free pre-filter: English (the library is already English-only), 2-5 min, 1M+
     views, uploaded in the last 12 months, and NOT Used / Not relevant / already
     suggested.
  2. Signal score (views/day, number of ads using the video, recency); take the
     top 30, at most one video per product.
  3. ONE batched Claude Haiku call: send the 30 with their stats + a fit guide and
     have it pick & rank the best 10, one-line reason each.

History of everything ever suggested is kept so nothing repeats. Refreshed twice a
week (never Sunday) by the caller; generate() is pure given its inputs (the Claude
picker is injectable so tests stay offline).
"""
from __future__ import annotations

import json
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import own_brands

ROOT = Path(__file__).resolve().parent
HISTORY_PATH = ROOT / "build" / "suggested_history.json"
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-5-5")

MIN_SECONDS, MAX_SECONDS, MIN_VIEWS, MAX_AGE_DAYS = 120, 300, 1_000_000, 365
CANDIDATE_POOL, PICK = 30, 10

FIT_GUIDE = (
    "Coolina sells knives and kitchen/outdoor gear to a mostly-male audience. "
    "GOOD fits: tools, gadgets, cars / scratch repair, work gear, knives / cooking, "
    "backyard / outdoor gear — things men like. BAD fits: apps, hospitals, "
    "supplements, giant brands, and ads that open on a pain Coolina doesn't have. "
    "STRONG signals: hardcore direct-response (not branding), an advertiser with a "
    "gadget niche for this audience, an advertiser that also sells knives, and ads "
    "that look like they ran on TV."
)


# ---------- free pre-filter + signal score ----------

def flatten(dataset: dict) -> list[dict]:
    advertisers = dataset.get("advertisers", []) if dataset.get("schema_version") == 2 else [dataset]
    out = []
    for a in advertisers:
        name = a.get("advertiser", {}).get("name")
        for v in a.get("videos", []):
            out.append({**v, "advertiser_name": name})
    return out


def signal_score(video: dict) -> float:
    vpd = video.get("lifetime_views_per_day") or 0
    ads = video.get("creative_count") or 1
    age = video.get("age_days") or MAX_AGE_DAYS
    recency = max(0.3, 1 - age / MAX_AGE_DAYS)        # newer scores higher, floor 0.3
    ad_boost = 1 + 0.15 * min(ads, 20)                # more ads = stronger DR signal
    return vpd * ad_boost * recency


def prefilter(videos: list[dict], excluded_ids: set[str]) -> list[dict]:
    kept = []
    for v in videos:
        if v["video_id"] in excluded_ids:
            continue
        if not (MIN_SECONDS <= (v.get("length_seconds") or 0) <= MAX_SECONDS):
            continue
        if (v.get("views") or 0) < MIN_VIEWS:
            continue
        if (v.get("age_days") or 10**9) > MAX_AGE_DAYS:
            continue
        kept.append(v)
    return kept


MAX_PER_ADVERTISER = 2  # at most 2 picks from any one advertiser


def top_candidates(videos: list[dict], limit: int = CANDIDATE_POOL,
                   max_per_advertiser: int = MAX_PER_ADVERTISER) -> list[dict]:
    """Highest signal score, at most one video per product and at most
    `max_per_advertiser` videos per advertiser."""
    ranked = sorted(videos, key=signal_score, reverse=True)
    seen_products, per_advertiser, out = set(), {}, []
    for v in ranked:
        key = (v.get("product") or v["video_id"]).strip().lower()
        if key in seen_products:
            continue
        adv = (v.get("advertiser_name") or v.get("author") or "").strip().lower()
        if adv and per_advertiser.get(adv, 0) >= max_per_advertiser:
            continue
        seen_products.add(key)
        per_advertiser[adv] = per_advertiser.get(adv, 0) + 1
        out.append(v)
        if len(out) >= limit:
            break
    return out


# ---------- the one batched Claude call ----------

def _api_key() -> str | None:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"]
    for path in (ROOT / ".env", Path(r"C:\Users\user\youtube-stats\.env"), Path.home() / "youtube-stats" / ".env"):
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("ANTHROPIC_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'") or None
        except OSError:
            continue
    return None


def _candidate_line(i: int, v: dict) -> str:
    return (f"{i}. id={v['video_id']} | product={v.get('product') or '?'} | "
            f"channel={v.get('advertiser_name') or v.get('author') or '?'} | "
            f"views={v.get('views')} | views/day={round(v.get('lifetime_views_per_day') or 0)} | "
            f"ads_using={v.get('creative_count')} | age_days={v.get('age_days')} | "
            f"title={(v.get('title') or '')[:70]}")


def choose_with_claude(candidates: list[dict], picker=None, count: int = PICK) -> list[dict]:
    """Return [{video_id, reason}] (best first). picker(candidates)->list lets tests
    stay offline; default calls Claude Haiku once with all candidates."""
    if picker is not None:
        return picker(candidates)[:count]
    key = _api_key()
    if not key:
        # no key: fall back to the signal-score order with a neutral reason
        return [{"video_id": v["video_id"], "reason": "Top signal score (AI pick unavailable)."}
                for v in candidates[:count]]
    prompt = (
        "You curate a competitor swipe library for Coolina. " + FIT_GUIDE +
        f"\n\nFrom the {len(candidates)} competitor YouTube video ads below, pick and rank the "
        f"best {count} swipes for Coolina (best first). Prefer the GOOD/STRONG fits; drop BAD fits "
        "even if their numbers are high. Reply with ONLY a JSON array of exactly "
        f"{count} objects {{\"video_id\": \"...\", \"reason\": \"one short line\"}} — no prose.\n\n"
        + "\n".join(_candidate_line(i + 1, v) for i, v in enumerate(candidates))
    )
    import anthropic
    client = anthropic.Anthropic(api_key=key)
    params = {"model": MODEL, "max_tokens": 1024,
              "messages": [{"role": "user", "content": prompt}]}
    if MODEL.startswith("claude-haiku"):
        params["thinking"] = {"type": "disabled"}
    text = next((b.text for b in client.messages.create(**params).content if b.type == "text"), "")
    m = re.search(r"\[.*\]", text, re.S)
    picks = json.loads(m.group(0)) if m else []
    valid = {v["video_id"] for v in candidates}
    return [{"video_id": p["video_id"], "reason": str(p.get("reason", "")).strip()}
            for p in picks if isinstance(p, dict) and p.get("video_id") in valid][:count]


# ---------- history ----------

def load_history() -> set[str]:
    try:
        return set(json.loads(HISTORY_PATH.read_text(encoding="utf-8")))
    except Exception:
        return set()


def save_history(ids: set[str]) -> None:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_PATH.write_text(json.dumps(sorted(ids)), encoding="utf-8")


# ---------- orchestration ----------

REFRESH_WEEKDAYS = {0, 3}  # Monday & Thursday — twice a week, never Sunday (weekday 6)


def is_refresh_day(when: datetime) -> bool:
    return when.weekday() in REFRESH_WEEKDAYS


def run_refresh(library_path, item_states, out_path, when=None, picker=None) -> dict:
    """Twice-weekly refresh: generate 10 suggestions, write suggestions.json, and
    append them to the no-repeat history. No-op on non-refresh days (and never Sunday)."""
    when = when or datetime.now(timezone.utc)
    if not is_refresh_day(when):
        return {"refreshed": False, "reason": "not a refresh day"}
    dataset = json.loads(Path(library_path).read_text(encoding="utf-8"))
    history = load_history()
    rows = generate(dataset, item_states, history, picker)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(
        json.dumps({"generated_at": when.isoformat(), "suggestions": rows}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    save_history(history | {r["video_id"] for r in rows})
    return {"refreshed": True, "count": len(rows)}


def generate(dataset: dict, item_states: dict, history: set[str], picker=None) -> list[dict]:
    """Return up to 10 suggestion rows (self-contained for the UI). Does not mutate
    history — the caller persists it after writing the file."""
    excluded = set(history) | {vid for vid, st in (item_states or {}).items() if st in ("used", "not_relevant")}
    # English-only, and never our own brands (Coolina et al.). Videos carry an
    # `english` flag now that the library also holds non-English ads (the All tab).
    flat = [v for v in flatten(dataset)
            if v.get("english") is True
            and not own_brands.is_own(v.get("advertiser_name"), v.get("landing_domain"))]
    videos = {v["video_id"]: v for v in flat}
    pool = top_candidates(prefilter(list(videos.values()), excluded))
    picks = choose_with_claude(pool, picker)
    rows = []
    for rank, pick in enumerate(picks, 1):
        v = videos.get(pick["video_id"])
        if not v:
            continue
        rows.append({
            "rank": rank, "reason": pick["reason"], "video_id": v["video_id"],
            "product": v.get("product"), "title": v.get("title"),
            "channel": v.get("advertiser_name") or v.get("author"),
            "views": v.get("views"), "views_per_day": round(v.get("lifetime_views_per_day") or 0),
            "length_seconds": v.get("length_seconds"), "upload_date": v.get("upload_date"),
            "embed_url": v.get("embed_url"), "thumbnail_url": v.get("thumbnail_url"),
            "youtube_url": v.get("youtube_url"), "transparency_url": v.get("transparency_url"),
        })
    return rows
