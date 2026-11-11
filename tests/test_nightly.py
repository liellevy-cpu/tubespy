"""Nightly runner decision logic: run window / never-Sunday, Google block detection
(stop the whole night), publish-target guard (never Liel's live page), and reading
Liel's shared raw READ-ONLY for the All tab."""
import json
from datetime import datetime
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

def test_read_shared_advertisers_is_read_only(tmp_path):
    shared = tmp_path / "liel_raw"
    shared.mkdir()
    (shared / "library.json").write_text(json.dumps({"schema_version": 2, "advertisers": [
        {"advertiser": {"google_advertiser_id": "AR900", "name": "Liel Brand"}, "videos": [{"video_id": "v1"}]},
    ]}), encoding="utf-8")
    before = {p: p.stat().st_mtime_ns for p in shared.rglob("*")}

    advs = N.read_shared_advertisers(shared)
    assert [a["advertiser"]["google_advertiser_id"] for a in advs] == ["AR900"]
    # nothing created or modified under the shared dir
    after = {p: p.stat().st_mtime_ns for p in shared.rglob("*")}
    assert before == after
    assert N.read_shared_advertisers(None) == []
    assert N.read_shared_advertisers(tmp_path / "missing") == []


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
