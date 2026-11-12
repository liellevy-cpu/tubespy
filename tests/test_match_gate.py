"""Publish gate: brands >= 90% matched publish; unmatched ads are surfaced."""
import json
from datetime import datetime, timezone

import pytest

from library import (
    ValidationError, apply_match_gate, build_dataset, match_report,
    meets_publish_threshold,
)

SNAPSHOT = datetime(2026, 9, 9, tzinfo=timezone.utc)
ADV = "AR15438241099155505153"


def cr_url(n):
    return (f"https://adstransparency.google.com/advertiser/{ADV}"
            f"/creative/CR{n:020d}?region=US&platform=YOUTUBE&hl=en")


def vid(i):
    return f"vid{i:06d}ab"  # 11 chars


def matched_result(index, vid):
    return {"index": index, "creative_url": cr_url(index), "video_ids": [vid],
            "variation_count": 1, "last_shown": "Sep 8, 2026", "error": None}


def unmatched_result(index):
    return {"index": index, "creative_url": cr_url(index), "video_ids": [],
            "variation_count": 0, "last_shown": None, "error": "RuntimeError: no evidence"}


def yt_record(index, vid):
    return {"video_id": vid, "url": f"https://www.youtube.com/watch?v={vid}",
            "title": "A real title", "author": "Daily Household Deals", "views": 2_000_000,
            "length_seconds": 150, "upload_date": "2026-09-07T00:00:00+00:00",
            "creative_count": 1, "creative_urls": [cr_url(index)], "error": None}


def test_match_report_counts_rate_and_unmatched():
    results = [matched_result(i, vid(i)) for i in range(9)] + [unmatched_result(9)]
    report = match_report(results, withheld=set())
    assert report["matched"] == 9 and report["unmatched"] == 1
    assert report["match_rate"] == 0.9
    assert report["unmatched_ads"] == [cr_url(9)]


def test_match_report_excludes_withheld_from_denominator():
    results = [matched_result(0, vid(0)), unmatched_result(1), unmatched_result(2)]
    report = match_report(results, withheld={cr_url(2)})
    assert report["match_rate"] == 0.5  # 1 matched / (3 - 1 withheld)
    assert report["unmatched_ads"] == [cr_url(1)]


def test_meets_threshold_boundary():
    assert meets_publish_threshold(0.90) is True
    assert meets_publish_threshold(0.8999) is False


def _write_source(tmp_path, results, records):
    urls = sorted(r["creative_url"] for r in results)
    (tmp_path / "creative_urls.json").write_text(json.dumps(urls), encoding="utf-8")
    (tmp_path / "creative_video_results.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in results), encoding="utf-8")
    (tmp_path / "youtube_stats.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return tmp_path


def test_apply_match_gate_blocks_below_threshold(tmp_path):
    results = [matched_result(0, vid(0)), unmatched_result(1), unmatched_result(2)]
    _write_source(tmp_path, results, [yt_record(0, vid(0))])
    with pytest.raises(ValidationError):
        apply_match_gate(tmp_path)  # 1/3 matched


def test_apply_match_gate_passes_trims_and_builds(tmp_path):
    results = [matched_result(0, vid(0)), matched_result(1, vid(1))]
    records = [yt_record(0, vid(0)), yt_record(1, vid(1))]
    _write_source(tmp_path, results, records)
    report = apply_match_gate(tmp_path)
    assert report["match_rate"] == 1.0
    assert json.loads((tmp_path / "unmatched_ads.json").read_text()) == []
    # the strict builder accepts the trimmed source
    dataset = build_dataset(tmp_path, SNAPSHOT, "Daily Household Deals", ADV, 2)
    assert len(dataset["videos"]) == 2
