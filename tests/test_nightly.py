"""Nightly runner decision logic: run window / never-Sunday, Google block detection
(stop the whole night), publish-target guard (never Liel's live page), and reading
Liel's shared raw READ-ONLY for the All tab."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import nightly as N


# ---------- schedule ----------

def test_is_run_day_never_sunday():
    assert N.is_run_day(datetime(2026, 11, 8)) is False   # Sunday
    assert N.is_run_day(datetime(2026, 11, 9)) is True     # Monday


def test_in_run_window_default_and_midnight_crossing():
    assert N.in_run_window(datetime(2026, 11, 9, 1, 0), "00:30", "02:30") is True
    assert N.in_run_window(datetime(2026, 11, 9, 3, 0), "00:30", "02:30") is False
    # window that crosses midnight
    assert N.in_run_window(datetime(2026, 11, 9, 0, 15), "23:30", "01:00") is True
    assert N.in_run_window(datetime(2026, 11, 9, 2, 0), "23:30", "01:00") is False


def test_should_run_combines_day_and_window():
    ok, _ = N.should_run(datetime(2026, 11, 9, 1, 0))     # Monday 01:00, default window
    assert ok is True
    ok2, reason = N.should_run(datetime(2026, 11, 8, 1, 0))  # Sunday
    assert ok2 is False and "Sunday" in reason


# ---------- block detection ----------

def test_is_block_signal_catches_google_blocks():
    assert N.is_block_signal("Our systems have detected unusual traffic") is True
    assert N.is_block_signal("HTTP 429 Too Many Requests") is True
    assert N.is_block_signal({"status": "blocked", "reason": "RuntimeError: creative HTTP status 429"}) is True
    assert N.is_block_signal("https://www.google.com/sorry/index") is True
    assert N.is_block_signal("completed fine, 120 creatives") is False
    assert N.is_block_signal("video 4294967296 views") is False   # a bare number isn't a 429
    assert N.is_block_signal(None) is False


# ---------- publish guard ----------

def test_assert_safe_publish_target_refuses_live():
    with pytest.raises(ValueError, match="live"):
        N.assert_safe_publish_target("youtube-competitor-library")
    with pytest.raises(ValueError):
        N.assert_safe_publish_target(None, Path("/srv/youtube-competitor-library/public"))
    N.assert_safe_publish_target("coolina-library-preview")      # preview is fine
    N.assert_safe_publish_target("coolina-library-preview", Path("/tmp/preview"))


# ---------- shared raw, read-only ----------

AID = "AR10000000000000000001"
CREATIVE = (f"https://adstransparency.google.com/advertiser/{AID}/creative/"
            "CR10000000000000000001?region=US&format=VIDEO&platform=YOUTUBE&hl=en")
SNAP = datetime(2026, 11, 11, tzinfo=timezone.utc)


def _write_liel_raw(adv_dir):
    """A minimal but valid raw advertiser folder (ad list + video stats), like Liel's."""
    adv_dir.mkdir(parents=True)
    (adv_dir / "summary.json").write_text(json.dumps(
        {"name": "Liel Brand", "advertiser_ids": [AID], "creative_count": 1}), encoding="utf-8")
    (adv_dir / "creative_video_results.jsonl").write_text(json.dumps(
        {"index": 0, "creative_url": CREATIVE, "video_ids": ["abcdefghijk"],
         "variation_count": 1, "last_shown": None, "landing_domain": "realsite.com", "error": None}) + "\n",
        encoding="utf-8")
    (adv_dir / "youtube_stats.jsonl").write_text(json.dumps(
        {"video_id": "abcdefghijk", "url": "https://www.youtube.com/watch?v=abcdefghijk",
         "title": "Demo", "author": "Chan", "views": 1000, "length_seconds": 180,
         "upload_date": "2026-09-01T00:00:00+00:00", "creative_count": 1,
         "creative_urls": [CREATIVE], "error": None}) + "\n", encoding="utf-8")


def test_read_shared_advertisers_builds_from_raw_read_only(tmp_path):
    shared = tmp_path / "liel_raw"
    _write_liel_raw(shared / "liel-brand")
    before = {p: p.stat().st_mtime_ns for p in shared.rglob("*")}

    advs = N.read_shared_advertisers(shared, SNAP)
    assert len(advs) == 1
    adv = advs[0]
    assert adv["advertiser"]["google_advertiser_id"] == AID
    assert adv["schema_version"] == 1 and len(adv["videos"]) == 1
    assert adv["videos"][0]["landing_domain"] == "realsite.com"      # built from the raw ad list

    # nothing created or modified under the shared dir (READ-ONLY)
    after = {p: p.stat().st_mtime_ns for p in shared.rglob("*")}
    assert before == after
    assert N.read_shared_advertisers(None) == []
    assert N.read_shared_advertisers(tmp_path / "missing") == []


def test_merge_advertiser_inserts_then_replaces_by_id():
    lib = {"schema_version": 2, "source_summary": {}, "advertisers": [
        {"advertiser": {"google_advertiser_id": "AR1"}, "videos": [{"video_id": "a"}]},
    ]}
    fresh = {"schema_version": 1, "advertiser": {"google_advertiser_id": "AR2"}, "videos": [{"video_id": "b"}, {"video_id": "c"}]}
    one = N.merge_advertiser(lib, fresh)
    assert [a["advertiser"]["google_advertiser_id"] for a in one["advertisers"]] == ["AR1", "AR2"]
    assert one["source_summary"]["displayed_videos"] == 3
    # re-scraping AR2 replaces it (no duplicate), with the newer videos
    newer = {"schema_version": 1, "advertiser": {"google_advertiser_id": "AR2"}, "videos": [{"video_id": "d"}]}
    two = N.merge_advertiser(one, newer)
    ar2 = [a for a in two["advertisers"] if a["advertiser"]["google_advertiser_id"] == "AR2"][0]
    assert [v["video_id"] for v in ar2["videos"]] == ["d"]
    assert len([a for a in two["advertisers"] if a["advertiser"]["google_advertiser_id"] == "AR2"]) == 1


def test_merge_all_tab_unions_without_mutating_shared():
    ours = {"schema_version": 2, "source_summary": {}, "advertisers": [
        {"advertiser": {"google_advertiser_id": "AR1", "name": "Ours"}, "videos": [{"video_id": "a"}, {"video_id": "b"}]},
    ]}
    shared = [
        {"advertiser": {"google_advertiser_id": "AR1", "name": "Dup"}, "videos": [{"video_id": "x"}]},   # collision -> ours win
        {"advertiser": {"google_advertiser_id": "AR9", "name": "Liel"}, "videos": [{"video_id": "y"}]},
    ]
    merged = N.merge_all_tab(ours, shared)
    ids = [a["advertiser"]["google_advertiser_id"] for a in merged["advertisers"]]
    assert ids == ["AR1", "AR9"]                                  # AR1 kept once (ours), AR9 added
    assert merged["advertisers"][0]["advertiser"]["name"] == "Ours"
    assert merged["source_summary"]["displayed_videos"] == 3
    assert merged["source_summary"]["shared_advertisers_included"] == 1
    assert len(ours["advertisers"]) == 1                          # our input not mutated
