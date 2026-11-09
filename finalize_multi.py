#!/usr/bin/env python3
"""Validate every completed advertiser, build the multi-brand release, and stage it."""

from __future__ import annotations

import argparse
import json
import re
import shutil
from datetime import datetime
from pathlib import Path

from library import build_dataset, build_stage, combine_datasets, retain_top_viewed, write_dataset
from collect_all import RAW, ROOT, TQR_SOURCE, canonical_creative_url, load_jsonl, read_existing_creatives, slugify


def reported_count(summary: dict, fallback: int) -> int:
    raw = (summary.get("discovery") or {}).get("reported_ads")
    if not raw:
        return fallback
    compact = str(raw).replace(",", "").strip().upper()
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([KMB]?)", compact)
    if not match:
        return fallback
    multiplier = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[match.group(2)]
    return max(1, round(float(match.group(1)) * multiplier))


def normalized_source(slug: str, source: Path) -> Path:
    """Create a tiny canonical evidence view without mutating raw collection evidence."""
    target = ROOT / "build" / "normalized" / slug
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    urls = read_existing_creatives(source / "creative_urls.json")
    (target / "creative_urls.json").write_text(json.dumps(urls, indent=2) + "\n")
    rows = load_jsonl(source / "creative_video_results.jsonl")
    by_url = {canonical_creative_url(row["creative_url"]): row for row in rows}
    ordered = []
    for index, url in enumerate(urls):
        if url not in by_url:
            raise RuntimeError(f"{slug}: missing creative result for {url}")
        row = dict(by_url[url])
        row["index"] = index
        row["creative_url"] = url
        ordered.append(row)
    (target / "creative_video_results.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in ordered))
    shutil.copyfile(source / "youtube_stats.jsonl", target / "youtube_stats.jsonl")
    unavailable = source / "youtube_unavailable.json"
    if unavailable.exists():
        shutil.copyfile(unavailable, target / "youtube_unavailable.json")
    return target


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", default=str(ROOT / "registry.json"))
    parser.add_argument("--snapshot-at", default="2026-09-09T00:00:00+00:00")
    parser.add_argument("--public-dir", type=Path, default=ROOT / "public")
    parser.add_argument("--stage-dir", type=Path, default=ROOT / "stage" / "private-youtube-pilot")
    args = parser.parse_args()
    snapshot = datetime.fromisoformat(args.snapshot_at.replace("Z", "+00:00"))
    registry = json.loads(Path(args.registry).read_text())
    datasets = []
    for item in registry["advertisers"]:
        name = item["name"]
        slug = slugify(name)
        if name == "TQR Marketing Group":
            source = TQR_SOURCE
            advertiser_ids = ["AR15438241099155505153"]
            approx = 400
        else:
            raw = RAW / slug
            summary_path = raw / "summary.json"
            if not summary_path.exists():
                raise RuntimeError(f"{name}: collection summary missing")
            summary = json.loads(summary_path.read_text())
            if summary.get("status") != "complete":
                raise RuntimeError(f"{name}: collection is not complete: {summary}")
            advertiser_ids = summary.get("advertiser_ids") or []
            if len(advertiser_ids) != 1:
                raise RuntimeError(f"{name}: expected one exact advertiser account, got {advertiser_ids}")
            source = normalized_source(slug, raw)
            approx = reported_count(summary, summary["creative_count"])
        full_dataset = build_dataset(source, snapshot, name, advertiser_ids[0], approx)
        datasets.append(retain_top_viewed(full_dataset, limit=5))
    combined = combine_datasets(datasets)
    write_dataset(combined, args.public_dir / "data" / "library.json")
    stage = build_stage(args.public_dir, args.stage_dir)
    print(json.dumps({
        "advertisers": len(combined["advertisers"]),
        "videos": combined["source_summary"]["recovered_unique_youtube_videos"],
        "creative_pages": combined["source_summary"]["unique_creative_pages"],
        "metadata_failures": combined["source_summary"]["youtube_metadata_failures"],
        "stage": str(stage.resolve()),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
