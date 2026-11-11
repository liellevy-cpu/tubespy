"""Slow landing-domain backfill: which videos need it (highest signal first), the
conservative per-run plan (never Sunday, budget-bounded, never a burst), and the
write-back. Planner + writer are pure; the live fetch is stubbed."""
import asyncio
import json
from datetime import datetime, timezone

import landing_backfill as B


def _lib():
    return {"advertisers": [
        {"advertiser": {"google_advertiser_id": "AR1", "name": "A"}, "videos": [
            {"video_id": "aaaaaaaaaaa", "views": 10, "landing_domain": "have.com",
             "transparency_url": "https://adstransparency.google.com/advertiser/AR1/creative/CR1"},
            {"video_id": "bbbbbbbbbbb", "views": 500, "landing_domain": None,
             "transparency_url": "https://adstransparency.google.com/advertiser/AR1/creative/CR2"},
        ]},
        {"advertiser": {"google_advertiser_id": "AR2", "name": "B"}, "videos": [
            {"video_id": "ccccccccccc", "views": 50,
             "creative_urls": ["https://adstransparency.google.com/advertiser/AR2/creative/CR3"]},
            {"video_id": "ddddddddddd", "views": 9999, "landing_domain": ""},   # no url -> skipped
        ]},
    ]}


def test_missing_lists_only_unfilled_with_a_url_highest_signal_first():
    missing = B.videos_missing_landing(_lib(), priority_video_ids={"ccccccccccc"})
    ids = [m["video_id"] for m in missing]
    # ddddddddddd has no transparency url -> excluded; priority video first, then by views
    assert ids == ["ccccccccccc", "bbbbbbbbbbb"]
    assert missing[0]["transparency_url"].endswith("CR3")


def test_plan_skips_sunday():
    missing = [{"video_id": "x"}]
    plan = B.plan_backfill(missing, when=datetime(2026, 11, 8, tzinfo=timezone.utc), skip_weekday=6)
    assert plan["run"] is False and plan["due"] == [] and "refresh day" in plan["reason"]


def test_plan_is_bounded_by_budget_and_cap():
    missing = [{"video_id": f"v{i}"} for i in range(100)]
    monday = datetime(2026, 11, 9, tzinfo=timezone.utc)
    # budget 60s / 6s per item = 10 items, under the cap of 40
    plan = B.plan_backfill(missing, when=monday, max_per_run=40, item_seconds=6, budget_seconds=60)
    assert len(plan["due"]) == 10 and plan["remaining"] == 90
    # cap wins when it is the tighter limit
    plan2 = B.plan_backfill(missing, when=monday, max_per_run=5, item_seconds=1, budget_seconds=10_000)
    assert len(plan2["due"]) == 5 and plan2["remaining"] == 95


def test_apply_landing_writes_only_unfilled():
    lib = _lib()
    n = B.apply_landing(lib, {"bbbbbbbbbbb": "newseed.com", "aaaaaaaaaaa": "override.com"})
    assert n == 1   # only the previously-empty one is filled; the existing one is untouched
    vids = {v["video_id"]: v.get("landing_domain") for a in lib["advertisers"] for v in a["videos"]}
    assert vids["bbbbbbbbbbb"] == "newseed.com"
    assert vids["aaaaaaaaaaa"] == "have.com"


def test_backfill_once_paces_and_writes(tmp_path):
    path = tmp_path / "library.json"
    path.write_text(json.dumps(_lib()), encoding="utf-8")
    sleeps = []
    async def fake_sleep(s): sleeps.append(s)
    async def fetch(item):
        return {"ccccccccccc": "seedc.com", "bbbbbbbbbbb": "seedb.com"}.get(item["video_id"])

    out = asyncio.run(B.backfill_once(path, fetch, when=datetime(2026, 11, 9, tzinfo=timezone.utc),
                                      sleep=fake_sleep, max_per_run=40, item_seconds=6, budget_seconds=10_000))
    assert out["ran"] and out["attempted"] == 2 and out["filled"] == 2
    assert sleeps == [6]   # spacing happens BETWEEN the two fetches, never a burst, none after the last
    saved = json.loads(path.read_text(encoding="utf-8"))
    doms = {v["video_id"]: v.get("landing_domain") for a in saved["advertisers"] for v in a["videos"]}
    assert doms["ccccccccccc"] == "seedc.com" and doms["bbbbbbbbbbb"] == "seedb.com"


def test_backfill_once_survives_a_fetch_error(tmp_path):
    path = tmp_path / "library.json"
    path.write_text(json.dumps(_lib()), encoding="utf-8")
    async def fetch(item):
        if item["video_id"] == "ccccccccccc":
            raise RuntimeError("blocked")
        return "seedb.com"
    out = asyncio.run(B.backfill_once(path, fetch, when=datetime(2026, 11, 9, tzinfo=timezone.utc),
                                      sleep=lambda s: asyncio.sleep(0),
                                      max_per_run=40, item_seconds=0, budget_seconds=10_000))
    assert out["ran"] and out["filled"] == 1   # the error one is skipped, the run continues
