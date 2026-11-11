#!/usr/bin/env python3
"""Slow, gradual landing-domain backfill for EXISTING videos.

Scrapes now record each ad's landing (click-through) domain, but videos collected
before that have none, so discovery has no seeds from them. This backfill fills
them in a little at a time: a small, well-paced nightly batch that fetches each
ad's preview bundle and extracts the adurl -- never a burst (a Google block hits
the whole mini IP), never during Liel's Sunday refresh, and always inside a tiny
slice of the scrape budget.

The planner and the writer are pure and unit-tested; the live per-ad fetch is
injected (fetch_fn), the same pure/live split as the collector and discovery."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import mini_config


def videos_missing_landing(library: dict, priority_video_ids=None) -> list[dict]:
    """Videos with no landing_domain yet, as {advertiser_id, advertiser_name,
    video_id, transparency_url, views}. Highest-signal first (Used/Suggested, then
    most-viewed) so the most useful discovery seeds are recovered earliest."""
    hot = set(priority_video_ids or ())
    out: list[dict] = []
    for adv in library.get("advertisers", []):
        meta = adv.get("advertiser", {})
        for v in adv.get("videos", []):
            if v.get("landing_domain"):
                continue
            tu = v.get("transparency_url") or (v.get("creative_urls") or [None])[0]
            if not tu:
                continue
            out.append({"advertiser_id": meta.get("google_advertiser_id"),
                        "advertiser_name": meta.get("name"), "video_id": v.get("video_id"),
                        "transparency_url": tu, "views": v.get("views") or 0,
                        "priority": v.get("video_id") in hot})
    out.sort(key=lambda r: (0 if r["priority"] else 1, -r["views"]))
    return out


def plan_backfill(missing: list[dict], when: datetime | None = None,
                  max_per_run: int | None = None, item_seconds: float | None = None,
                  budget_seconds: int | None = None, skip_weekday: int | None = None) -> dict:
    """The slice to do THIS run: never on Liel's refresh day, and never more than the
    budget or the per-run cap allow. Conservative by construction -- a few per night."""
    when = when or datetime.now(timezone.utc)
    skip_weekday = mini_config.SKIP_WEEKDAY if skip_weekday is None else skip_weekday
    max_per_run = mini_config.BACKFILL_MAX_PER_RUN if max_per_run is None else max_per_run
    item_seconds = mini_config.BACKFILL_ITEM_SECONDS if item_seconds is None else item_seconds
    budget_seconds = mini_config.BACKFILL_BUDGET_SECONDS if budget_seconds is None else budget_seconds
    if when.weekday() == skip_weekday:
        return {"run": False, "reason": "Liel's refresh day", "due": [], "remaining": len(missing)}
    by_budget = int(budget_seconds // item_seconds) if item_seconds > 0 else len(missing)
    take = max(0, min(len(missing), max_per_run, by_budget))
    return {"run": take > 0, "reason": None if take else "nothing due", "due": missing[:take],
            "remaining": max(0, len(missing) - take), "item_seconds": item_seconds}


def apply_landing(library: dict, resolved: dict) -> int:
    """Write resolved {video_id: landing_domain} back onto the library's videos.
    Only truthy domains are written. Returns how many videos were filled."""
    filled = 0
    for adv in library.get("advertisers", []):
        for v in adv.get("videos", []):
            domain = resolved.get(v.get("video_id"))
            if domain and not v.get("landing_domain"):
                v["landing_domain"] = domain
                filled += 1
    return filled


async def backfill_once(library_path, fetch_fn, when: datetime | None = None,
                        sleep=asyncio.sleep, priority_video_ids=None, **plan_overrides) -> dict:
    """One gradual pass: plan a tiny batch, fetch each ad's landing domain ONE AT A
    TIME (spaced item_seconds apart -- never a burst), write the survivors back to
    library.json, and report. fetch_fn(item) -> domain|None is injected (live or stub).
    A single fetch failure is logged and skipped; it never aborts the run."""
    path = Path(library_path)
    library = json.loads(path.read_text(encoding="utf-8"))
    missing = videos_missing_landing(library, priority_video_ids)
    plan = plan_backfill(missing, when, **plan_overrides)
    if not plan["run"]:
        return {"ran": False, "reason": plan["reason"], "filled": 0,
                "attempted": 0, "remaining": plan["remaining"]}

    resolved: dict[str, str] = {}
    log: list[dict] = []
    for i, item in enumerate(plan["due"]):
        try:
            domain = await fetch_fn(item)
        except Exception as exc:
            domain = None
            log.append({"video_id": item["video_id"], "error": f"{type(exc).__name__}: {exc}"})
        if domain:
            resolved[item["video_id"]] = domain
        log.append({"video_id": item["video_id"], "landing_domain": domain})
        if i + 1 < len(plan["due"]):
            await sleep(plan["item_seconds"])     # deliberate spacing: never a burst

    filled = apply_landing(library, resolved)
    if filled:
        path.write_text(json.dumps(library, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"ran": True, "attempted": len(plan["due"]), "filled": filled,
            "remaining": plan["remaining"] + (len(plan["due"]) - len(resolved)), "log": log}


def make_live_fetch(context):
    """Factory for the live per-ad fetch: open the creative's transparency page, read
    its content, and extract the landing domain (reusing the collector's extractor).
    Injected into backfill_once on the mini; never exercised by the unit tests."""
    import collect_all

    async def fetch(item) -> str | None:
        page = await context.new_page()
        try:
            await page.goto(item["transparency_url"], wait_until="domcontentloaded", timeout=75_000)
            await page.wait_for_timeout(2_500)
            text = await page.content()
            frames = [text]
            frames.extend(f.url for f in page.frames)
            return collect_all.landing_domain_from_text("\n".join(frames))
        finally:
            await page.close()

    return fetch
