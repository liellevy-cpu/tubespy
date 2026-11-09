#!/usr/bin/env python3
"""TubeSpy export: every YouTube ad for one Google Ads Transparency advertiser -> CSV.

Usage:
  .venv/bin/python tubespy_export.py "https://adstransparency.google.com/advertiser/AR..." [--out DIR]

Prints one JSON line with the CSV path and honest coverage counts. Partial coverage is
reported, never hidden: rows that could not be resolved are listed with their error.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import collect_all  # noqa: E402
from add_brand import Invalid, validate  # noqa: E402
from brand_worker import RestrictedBrowser  # noqa: E402

EXPORTS = ROOT / "build/tubespy-exports"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def parse_target(url: str) -> dict:
    match = re.search(r"/advertiser/(AR\d{10,25})", url or "")
    if not match:
        raise Invalid("Use an advertiser page link like https://adstransparency.google.com/advertiser/AR…")
    return validate({"name": match.group(1), "url": f"https://adstransparency.google.com/advertiser/{match.group(1)}"})


def write_csv(source: Path, path: Path) -> dict:
    creatives = collect_all.load_jsonl(source / "creative_video_results.jsonl")
    stats = {row["video_id"]: row for row in collect_all.load_jsonl(source / "youtube_stats.jsonl")}
    unavailable_file = source / "youtube_unavailable.json"
    unavailable = {row["video_id"]: row for row in (json.loads(unavailable_file.read_text()) if unavailable_file.exists() else [])}
    by_video: dict[str, list[dict]] = {}
    unresolved = []
    for row in creatives:
        if row.get("error") or not row.get("video_ids"):
            unresolved.append(row)
        for video_id in row.get("video_ids", []):
            by_video.setdefault(video_id, []).append(row)
    rows = []
    for video_id, ads in by_video.items():
        meta = stats.get(video_id, {})
        gone = unavailable.get(video_id)
        rows.append({
            "youtube_url": f"https://www.youtube.com/watch?v={video_id}",
            "video_id": video_id,
            "views": meta.get("views") if not gone else None,
            "upload_date": meta.get("upload_date"),
            "length_seconds": meta.get("length_seconds"),
            "title": meta.get("title"),
            "channel": meta.get("author"),
            "status": "unavailable: " + gone["reason"] if gone else ("ok" if meta.get("views") is not None else "metadata failed"),
            "ads_using_this_video": len(ads),
            "last_shown": max((a.get("last_shown") or "" for a in ads), default="") or None,
            "transparency_ad_url": ads[0]["creative_url"],
        })
    rows.sort(key=lambda r: (r["views"] is None, -(r["views"] or 0)))
    fields = list(rows[0].keys()) if rows else ["youtube_url"]
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    withheld_file = source / "google_withheld_creatives.json"
    withheld = set(json.loads(withheld_file.read_text())) if withheld_file.exists() else set()
    unresolved = [row for row in unresolved if row["creative_url"] not in withheld]
    return {"videos": len(rows), "ads_found": len(creatives), "ads_unresolved": len(unresolved),
            "ads_hidden_by_google": len(withheld),
            "videos_with_views": sum(r["views"] is not None for r in rows)}


async def run(target: dict, out: Path) -> dict:
    from playwright.async_api import async_playwright

    run_dir = EXPORTS / f"{datetime.now():%Y%m%d-%H%M%S}-{target['advertiser_id']}-{uuid.uuid4().hex[:6]}"
    collect_all.RAW = run_dir / "raw"
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, executable_path=CHROME)
        try:
            with collect_all.google_lock("tubespy:" + target["advertiser_id"]):
                summary = await collect_all.collect_advertiser(
                RestrictedBrowser(browser), {"name": target["advertiser_id"], "transparency_url": target["url"]}, 1)
        finally:
            await browser.close()
    source = collect_all.RAW / collect_all.slugify(target["advertiser_id"])
    out.mkdir(parents=True, exist_ok=True)
    csv_path = out / f"tubespy-{target['advertiser_id']}-{datetime.now():%Y-%m-%d}.csv"
    counts = write_csv(source, csv_path)
    discovery = summary.get("discovery") or {}
    return {"ok": counts["videos"] > 0, "csv": str(csv_path), "advertiser_id": target["advertiser_id"],
            "google_reported_ads": discovery.get("reported_ads"), "discovery_complete": discovery.get("stable"),
            **counts, "collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("advertiser_url")
    parser.add_argument("--out", type=Path, default=EXPORTS)
    args = parser.parse_args()
    try:
        target = parse_target(args.advertiser_url)
    except Invalid as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 2
    result = asyncio.run(run(target, args.out))
    print(json.dumps(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
