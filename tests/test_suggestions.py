"""Suggestions: pre-filter, signal score, product dedup, Haiku pick (stubbed), schedule."""
import json
from datetime import datetime

import pytest

import suggestions as S


def vid(i, **kw):
    v = {"video_id": f"vid{i:06d}ab", "length_seconds": 180, "views": 5_000_000, "age_days": 30,
         "lifetime_views_per_day": 10_000, "creative_count": 5, "product": f"Product {i}",
         "title": f"title {i}", "upload_date": "2026-09-07T00:00:00+00:00", "english": True,
         "embed_url": "e", "thumbnail_url": "th", "youtube_url": "y", "transparency_url": "tr"}
    v.update(kw)
    return v


def ds(videos):
    return {"schema_version": 2, "advertisers": [{"advertiser": {"name": "Brand"}, "videos": videos}]}


def test_prefilter_applies_free_rules():
    videos = [
        vid(1),                                   # keep
        vid(2, length_seconds=60),                # drop: too short
        vid(3, views=500_000),                    # drop: < 1M
        vid(4, age_days=500),                     # drop: older than 12 months
        vid(5),                                   # excluded below
    ]
    kept = S.prefilter(videos, excluded_ids={"vid000005ab"})
    ids = {v["video_id"] for v in kept}
    assert ids == {"vid000001ab"}


def test_signal_score_rewards_vpd_ads_and_recency():
    hi = vid(1, lifetime_views_per_day=20_000, creative_count=15, age_days=10)
    lo = vid(2, lifetime_views_per_day=5_000, creative_count=1, age_days=300)
    assert S.signal_score(hi) > S.signal_score(lo)


def test_top_candidates_one_video_per_product():
    videos = [
        vid(1, product="Knife", lifetime_views_per_day=9000),
        vid(2, product="Knife", lifetime_views_per_day=1000),   # same product, lower -> dropped
        vid(3, product="Vacuum", lifetime_views_per_day=8000),
    ]
    out = S.top_candidates(videos, limit=10)
    products = [v["product"] for v in out]
    assert products == ["Knife", "Vacuum"]                      # one per product, by signal


def test_generate_uses_picker_and_excludes_used_and_history():
    videos = [vid(i) for i in range(1, 6)]
    picker = lambda cands: [{"video_id": c["video_id"], "reason": f"pick {c['video_id']}"} for c in cands]
    rows = S.generate(ds(videos),
                      item_states={"vid000002ab": "used", "vid000003ab": "not_relevant"},
                      history={"vid000004ab"},
                      picker=picker)
    ids = [r["video_id"] for r in rows]
    assert "vid000002ab" not in ids and "vid000003ab" not in ids and "vid000004ab" not in ids
    assert rows[0]["rank"] == 1 and rows[0]["reason"].startswith("pick ")
    assert all({"video_id", "reason", "rank", "views_per_day", "embed_url"} <= set(r) for r in rows)


def test_generate_excludes_our_own_brands():
    dataset = {"schema_version": 2, "advertisers": [
        {"advertiser": {"name": "Coolina"}, "videos": [vid(1)]},       # our own brand
        {"advertiser": {"name": "Super Brand Tools"}, "videos": [vid(2)]},
    ]}
    picker = lambda cands: [{"video_id": c["video_id"], "reason": "r"} for c in cands]
    rows = S.generate(dataset, item_states={}, history=set(), picker=picker)
    channels = {r["channel"] for r in rows}
    assert "Coolina" not in channels
    assert "Super Brand Tools" in channels


def test_generate_excludes_non_english_videos():
    dataset = ds([vid(1, english=True), vid(2, english=False), vid(3, english=None)])
    picker = lambda cands: [{"video_id": c["video_id"], "reason": "r"} for c in cands]
    ids = [r["video_id"] for r in S.generate(dataset, {}, set(), picker=picker)]
    assert ids == ["vid000001ab"]  # only the confirmed-English video is eligible


def test_refresh_is_twice_a_week_never_sunday():
    assert S.is_refresh_day(datetime(2026, 11, 9)) is True    # Monday
    assert S.is_refresh_day(datetime(2026, 11, 12)) is True   # Thursday
    assert S.is_refresh_day(datetime(2026, 11, 10)) is False  # Tuesday
    assert S.is_refresh_day(datetime(2026, 11, 8)) is False   # Sunday


# ---------- robust parsing (the crash Henry hit) ----------

def test_first_json_array_reads_only_the_first_list_amid_two_lists_and_prose():
    # The reply Henry saw: prose, then the real picks array, then a SECOND list and
    # more text. The old greedy "[.*]" regex spanned from the first "[" to the last
    # "]" across BOTH lists -> invalid JSON -> json.loads crashed the whole night.
    reply = (
        'Here are my picks for Coolina:\n'
        '[{"video_id": "vid000001ab", "reason": "great knife ad"}, '
        '{"video_id": "vid000002ab", "reason": "strong DR gadget"}]\n\n'
        'For reference the raw scores were [9.1, 8.4, 7.7] and that is all.'
    )
    picks = S._first_json_array(reply)
    assert picks == [
        {"video_id": "vid000001ab", "reason": "great knife ad"},
        {"video_id": "vid000002ab", "reason": "strong DR gadget"},
    ]


def test_first_json_array_returns_none_when_no_list():
    assert S._first_json_array("no array at all, sorry") is None
    assert S._first_json_array('broken [not, valid json') is None


def test_picks_from_response_prefers_the_structured_tool_call():
    class Block:
        def __init__(self, **kw): self.__dict__.update(kw)
    # a tool_use block carries the picks as structured input -> used directly
    resp = type("R", (), {"content": [
        Block(type="text", text="ignored prose [1,2,3]"),
        Block(type="tool_use", input={"picks": [{"video_id": "x", "reason": "r"}]}),
    ]})
    assert S._picks_from_response(resp) == [{"video_id": "x", "reason": "r"}]


def test_run_refresh_keeps_previous_file_when_parsing_fails(tmp_path):
    # Simulate an unparseable reply: the picker raises the parse error that the live
    # path raises. The night must NOT crash and must keep the previous suggestions.json.
    lib = tmp_path / "library.json"
    lib.write_text(json.dumps(ds([vid(1)])), encoding="utf-8")
    out = tmp_path / "suggestions.json"
    previous = '{"generated_at": "yesterday", "suggestions": [{"video_id": "old"}]}\n'
    out.write_text(previous, encoding="utf-8")

    def boom(_cands):
        raise S.SuggestionsParseError("two lists, no clean parse")

    result = S.run_refresh(lib, {}, out, when=datetime(2026, 11, 9), picker=boom)
    assert result == {"refreshed": False, "reason": "parse-failed", "kept_previous": True}
    assert out.read_text(encoding="utf-8") == previous   # previous file untouched
