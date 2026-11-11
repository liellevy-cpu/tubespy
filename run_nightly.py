#!/usr/bin/env python3
"""THE single nightly entry point for our isolated Coolina library on the mini.

Runs the whole night in order, and STOPS THE WHOLE NIGHT at the first Google block:

  1. plan      - which brands are due tonight (refresh_policy), capped to the budget
  2. scrape    - one advertiser at a time, under the mini-wide Google lock
  3. backfill  - a tiny landing-domain slice  +  discovery snowball/screen (queues
                 new advertisers for a FUTURE night; it does not scrape them now)
  4. suggest   - rebuild the weekly Suggestions
  5. publish   - build the library (+ Liel's shared raw, read-only, for All) and
                 publish to OUR preview target -- never Liel's live page

Safety rails (all configurable in mini_config / TUBESPY_* env):
  * never Sunday, only inside the run window (default 00:30-02:30 Asia/Jerusalem)
  * one scrape at a time; every Google request waits on the shared lock
  * hard wall-clock budget (default 2h) and max new brands/night (default 5)
  * first "too many requests" / "unusual traffic" -> stop everything, exit non-zero
  * logs to our own folder; ANY failure (including a block) exits non-zero so
    Henry's Slack alert fires.

The pure decisions (window, block detection, publish guard, shared-raw) live in
nightly.py and are unit-tested; this file is the live glue, validated on deploy."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time as _time
from datetime import datetime, timezone
from pathlib import Path

import mini_config
import nightly

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

log = logging.getLogger("nightly")


def _now_local() -> datetime:
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo(mini_config.RUN_TZ))
        except Exception:
            pass
    return datetime.now(timezone.utc)


def _setup_logging() -> Path:
    mini_config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    path = mini_config.LOGS_DIR / f"nightly-{_now_local():%Y%m%d}.log"
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(path, encoding="utf-8"), logging.StreamHandler(sys.stderr)])
    return path


def _plan_brands(store, now_local: datetime):
    """Due brands for tonight, newest-successful-scrape as the incremental cursor,
    first-time scrapes capped. Returns a list of {name, transparency_url, advertiser_id}."""
    import refresh_policy
    now_ts = now_local.timestamp()
    brands = []
    for row in store.list():
        if row.get("state") == "inactive":
            continue
        brands.append({"advertiser_id": row.get("advertiser_id"),
                       "name": row.get("name"), "transparency_url": row.get("url"),
                       "last_scraped_at": row.get("completed_at"), "last_ad_at": None})
    plan = refresh_policy.plan_run(brands, now_ts,
                                   budget_s=mini_config.NIGHTLY_BUDGET_SECONDS,
                                   per_creative_s=mini_config.PER_CREATIVE_SECONDS,
                                   max_new=mini_config.MAX_NEW_BRANDS_PER_RUN)
    return plan


async def _run_async(now_local: datetime, deadline: float, dry_run: bool) -> dict:
    import collect_all
    from add_brand import Store
    collect_all.RAW = mini_config.EXPORTS_DIR            # keep every raw scrape in OUR exports
    collect_all.reset_rate_limit()                      # fresh 429 counter for the night

    store = Store(mini_config.STORE_PATH)
    lib_path = mini_config.OUR_ROOT / "public" / "data" / "library.json"
    report = {"scraped": [], "blocked": False, "backfill": None, "discovery": None,
              "suggestions": None, "published": None, "errors": []}

    # 1) PLAN
    plan = _plan_brands(store, now_local)
    selected = plan.get("selected", [])
    log.info("plan: %d brand(s) due tonight, %d deferred (budget %ds, max_new %d)",
             len(selected), len(plan.get("deferred", [])), mini_config.NIGHTLY_BUDGET_SECONDS,
             mini_config.MAX_NEW_BRANDS_PER_RUN)
    if dry_run:
        report["plan_only"] = [b.get("name") for b in selected]
        return report

    # 2) SCRAPE (one at a time, under the shared Google lock; stop the night on a block)
    from brand_worker import RestrictedBrowser
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, executable_path=os.environ.get("CHROME_PATH"))
        try:
            for brand in selected:
                if _time.monotonic() >= deadline:
                    log.info("budget reached; deferring the rest of the scrape queue")
                    break
                name, url = brand.get("name"), brand.get("transparency_url")
                if not (name and url):
                    continue
                log.info("scrape: %s", name)
                try:
                    with collect_all.google_lock("scrape:" + collect_all.slugify(name)):
                        summary = await collect_all.collect_advertiser(
                            RestrictedBrowser(browser), {"name": name, "transparency_url": url}, mini_config.SCRAPE_CONCURRENCY)
                except collect_all.RateLimitExceeded as exc:
                    report["blocked"] = True
                    raise nightly.NightBlocked(f"429 threshold reached while scraping {name}: {exc}")
                if nightly.is_block_signal(summary):
                    report["blocked"] = True
                    raise nightly.NightBlocked(f"Google block while scraping {name}: {summary.get('reason') or summary.get('status')}")
                # Build this brand's videos into library.json now and mark it scraped,
                # so the planner doesn't re-scrape it every night (#1).
                integrated = _integrate_brand(brand, now_local, lib_path, store)
                report["scraped"].append({"name": name, "status": summary.get("status"),
                                          "videos": integrated.get("videos"), "integrated": integrated.get("ok")})
        finally:
            await browser.close()

    # 3) BACKFILL (tiny slice) + DISCOVERY (once a week on a fixed non-Sunday weekday)
    import landing_backfill
    prio = _priority_ids(store)
    run_discovery = now_local.weekday() == mini_config.DISCOVERY_WEEKDAY
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, executable_path=os.environ.get("CHROME_PATH"))
        context = await browser.new_context(locale="en-US")
        try:
            if _time.monotonic() < deadline and lib_path.exists():
                fetch = landing_backfill.make_live_fetch(context)       # self-locks each fetch
                report["backfill"] = await landing_backfill.backfill_once(lib_path, fetch, when=now_local, priority_video_ids=prio)
                log.info("backfill: %s", report["backfill"])
            if run_discovery and _time.monotonic() < deadline and lib_path.exists():
                report["discovery"] = await _discover_and_screen(context, store, lib_path, prio, now_local)
                log.info("discovery: %s", report["discovery"])
            else:
                report["discovery"] = {"skipped": "not the weekly discovery weekday"} if not run_discovery else {"skipped": "budget/library"}
        except (nightly.NightBlocked, collect_all.RateLimitExceeded) as exc:
            report["blocked"] = True
            raise nightly.NightBlocked(str(exc))
        finally:
            await context.close(); await browser.close()

    # 4) SUGGESTIONS
    import suggestions
    sugg_path = mini_config.OUR_ROOT / "public" / "data" / "suggestions.json"
    if lib_path.exists():
        report["suggestions"] = suggestions.run_refresh(lib_path, store.item_states(), sugg_path, when=now_local)
        log.info("suggestions: %s", report["suggestions"])

    # 5) PUBLISH (full site to OUR preview; never Liel's live page)
    report["published"] = _publish(lib_path, now_local)
    log.info("published: %s", report["published"])
    return report


def _integrate_brand(brand: dict, now_local: datetime, lib_path: Path, store) -> dict:
    """After a successful scrape: build this brand's videos from its fresh raw export
    into library.json (insert/replace by advertiser ID) and record completed_at in our
    store, so the planner treats it as refreshed and does not re-scrape it every night."""
    import collect_all
    import time as _t
    slug = collect_all.slugify(brand.get("name") or "")
    dataset = nightly.build_advertiser_from_raw(mini_config.EXPORTS_DIR / slug, now_local)
    if not dataset:
        log.warning("integrate: no usable videos built for %s", brand.get("name"))
        return {"ok": False, "videos": 0}
    aid = dataset["advertiser"]["google_advertiser_id"]

    if lib_path.exists():
        library_doc = json.loads(lib_path.read_text(encoding="utf-8"))
    else:
        library_doc = {"schema_version": 2, "snapshot_at": now_local.isoformat(),
                       "source_summary": {}, "advertisers": [],
                       "provenance": {"platform": "Google Ads Transparency Center + YouTube",
                                      "media_policy": "Native YouTube embeds only; videos are not downloaded or rehosted.",
                                      "foreplay_dependency": False}}
    merged = nightly.merge_advertiser(library_doc, dataset)
    lib_path.parent.mkdir(parents=True, exist_ok=True)
    lib_path.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    store_id = brand.get("advertiser_id") or aid
    try:
        with store.connect() as db:
            db.execute("UPDATE brands SET state='complete', completed_at=?, message=? WHERE advertiser_id=?",
                       (_t.time(), "Scraped and built into the library.", store_id))
    except Exception as exc:
        log.warning("integrate: could not record completed_at for %s: %s", store_id, exc)
    return {"ok": True, "videos": len(dataset["videos"]), "advertiser_id": aid}


def _priority_ids(store) -> set:
    ids = {v for v, s in store.item_states().items() if s == "used"}
    try:
        sugg = json.loads((mini_config.OUR_ROOT / "public/data/suggestions.json").read_text(encoding="utf-8"))
        ids |= {r["video_id"] for r in sugg.get("suggestions", [])}
    except Exception:
        pass
    return ids


async def _discover_and_screen(context, store, lib_path, prio, now_local) -> dict:
    """Snowball our landing-domain seeds -> screen candidates -> queue the survivors
    for a FUTURE night (we do not scrape them now). All Google requests self-lock."""
    import discovery, screening, own_brands
    library = json.loads(lib_path.read_text(encoding="utf-8"))
    seeds = sorted({d for a in library.get("advertisers", []) for d in discovery.landing_domains(a)
                    if not own_brands.is_own_domain(d)})
    if not seeds:
        return {"seeds": 0, "queued": []}
    page = await context.new_page()
    lookup = {}
    try:
        for d in seeds:
            lookup[d] = await discovery.advertisers_for_domain(page, d)   # self-locked
    finally:
        await page.close()
    known = {(a.get("advertiser") or {}).get("google_advertiser_id") for a in library.get("advertisers", [])}
    known.discard(None)
    discovered = discovery.snowball_sources(seeds, known, lookup.get)
    hot = discovery.priority_domains(library, prio)
    candidates = [{"advertiser_id": i, "name": i, "source_domain": d, "priority": d in hot} for i, d in discovered]
    # cap how many advertisers we actually probe per night (each probe is a live
    # Google request); priority (Used/Suggested) candidates first.
    candidates.sort(key=lambda c: 0 if c["priority"] else 1)
    found_total = len(candidates)
    candidates = candidates[:mini_config.DISCOVERY_MAX_CHECKED]

    probes = {}
    for c in candidates:
        pg = await context.new_page()
        try:
            probes[c["advertiser_id"]] = await screening.probe_advertiser(pg, c["advertiser_id"], now=now_local)
            if probes[c["advertiser_id"]].get("name"):
                c["name"] = probes[c["advertiser_id"]]["name"]
        except Exception as exc:
            if nightly.is_block_signal(f"{type(exc).__name__}: {exc}"):
                raise nightly.NightBlocked(f"Google block during discovery probe: {exc}")
        finally:
            await pg.close()
    screened = screening.screen_candidates(candidates, lambda a: probes.get(a, {}), screening.classify_with_haiku)
    queued = discovery.queue_new_advertisers(store, [p["advertiser_id"] for p in screened["passed"]],
                                             name_fn=lambda a: next((p.get("name") for p in screened["passed"] if p["advertiser_id"] == a), a))
    return {"seeds": len(seeds), "found": found_total, "checked": len(candidates),
            "passed": len(screened["passed"]), "queued": queued, "skipped": len(screened["skipped"])}


def _publish(lib_path: Path, now_local: datetime) -> dict:
    """Build the COMPLETE static site folder (index.html, app.js, styles.css + data)
    into OUR preview target, with the All-tab library (our brands + Liel's shared raw,
    READ-ONLY). Henry uploads this folder to a separate preview link. Refuses, as a hard
    backstop, to publish to Liel's live slug/path. Never writes under the shared raw."""
    import library as lib_mod
    nightly.assert_safe_publish_target(mini_config.PUBLISH_SLUG, mini_config.PUBLISH_DIR)
    our = json.loads(lib_path.read_text(encoding="utf-8")) if lib_path.exists() else {"schema_version": 2, "advertisers": []}
    shared = nightly.read_shared_advertisers(mini_config.SHARED_RAW_DIR, now_local)   # read-only; may be []
    merged = nightly.merge_all_tab(our, shared)

    public_dir = mini_config.OUR_ROOT / "public"
    # Stage the full allowlisted site (html/js/css + data/library.json) into the target,
    # then overwrite data/library.json with the merged (shared-included) All-tab data so
    # our own public/ copy is never polluted with Liel's advertisers.
    stage = lib_mod.build_stage(public_dir, mini_config.PUBLISH_DIR)
    (stage / "data" / "library.json").write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    files = sorted(p.name for p in stage.glob("*") if p.is_file())
    return {"target": str(stage), "slug": mini_config.PUBLISH_SLUG, "files": files,
            "advertisers": len(merged["advertisers"]),
            "shared_included": merged.get("source_summary", {}).get("shared_advertisers_included", 0)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Nightly Coolina library run (isolated).")
    parser.add_argument("--force", action="store_true", help="ignore the day/window gate (manual run)")
    parser.add_argument("--dry-run", action="store_true", help="plan only; no scraping or publishing")
    args = parser.parse_args()
    log_path = _setup_logging()
    now_local = _now_local()
    log.info("=== nightly start %s (%s) log=%s ===", now_local.isoformat(), mini_config.RUN_TZ, log_path)

    ok, reason = nightly.should_run(now_local)
    if not ok and not args.force:
        log.info("not running: %s", reason)
        return 0

    if mini_config.NO_SHARED_KEY and not os.environ.get("YOUTUBE_DATA_API_KEY"):
        log.error("YOUTUBE_DATA_API_KEY is not set and shared-key fallback is disabled; aborting.")
        return 1

    # Make the scraper's 429 counter stop the night after MAX_RATE_LIMIT_HITS hits.
    os.environ["TUBESPY_MAX_429"] = str(mini_config.MAX_RATE_LIMIT_HITS)

    deadline = _time.monotonic() + mini_config.NIGHTLY_BUDGET_SECONDS
    try:
        import asyncio
        report = asyncio.run(_run_async(now_local, deadline, args.dry_run))
    except nightly.NightBlocked as block:
        log.error("BLOCK — stopping the whole night: %s", block)
        return 2
    except Exception:
        log.exception("nightly run failed")
        return 1

    if report.get("errors"):
        log.error("nightly finished with errors: %s", report["errors"])
        return 1
    log.info("=== nightly done: %s ===", json.dumps(report, default=str)[:800])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
