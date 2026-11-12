"""Advertiser-level pre-scrape screening: giant-brand filter, cheap size/recency
gate, Haiku English-count gate (>=2 of the sample), fit used only for queue priority
(never a skip), and a reason for every skip. Orchestrator is pure; probe + classify
are stubbed."""
from datetime import datetime, timezone

import screening as S


# ---------- cheap helpers ----------

def test_is_giant_brand_matches_whole_words_only():
    assert S.is_giant_brand("Nike, Inc.")
    assert S.is_giant_brand("Samsung Electronics America")
    assert S.is_giant_brand("L'Oreal Paris")
    assert not S.is_giant_brand("Bradford Exchange")      # 'ford' is not a word here
    assert not S.is_giant_brand("Pineapple Gadgets")      # 'apple' is not a word here
    assert not S.is_giant_brand("Coolina")
    assert not S.is_giant_brand("")


def test_parse_reported_ads_handles_suffixes():
    assert S.parse_reported_ads("Showing ~1,120 ads for this advertiser") == 1120
    assert S.parse_reported_ads("~2.5K ads") == 2500
    assert S.parse_reported_ads("~3M ads") == 3_000_000
    assert S.parse_reported_ads("no count here") is None


def test_days_since_parses_last_shown():
    now = datetime(2026, 11, 11, tzinfo=timezone.utc)
    assert S.days_since("Last shown Nov 1, 2026", now) == 10
    assert S.days_since("Oct 12, 2026", now) == 30
    assert S.days_since(None, now) is None
    assert S.days_since("whenever", now) is None


def test_pick_sample_mixes_newest_and_most_viewed():
    newest = ["n1", "n2", "n3", "n4", "n5", "n6", "n7"]
    meta = {v: {"title": v, "views": i} for i, v in enumerate(newest)}  # n7 most viewed
    out = S.pick_sample(newest, meta, size=4, newest_n=2)
    ids = [s["video_id"] for s in out]
    assert ids[:2] == ["n1", "n2"]          # the two newest, in order
    assert "n7" in ids and "n6" in ids      # then the most-viewed fill the rest
    assert len(ids) == 4


# ---------- orchestrator ----------

def _probe(**by_id):
    calls = []
    def probe(advertiser_id):
        calls.append(advertiser_id)
        return by_id[advertiser_id]
    probe.calls = calls
    return probe


def _classify(verdict_by_name):
    def classify(name, samples):
        return verdict_by_name[name]
    return classify


def _v(english_count, fit=True, **kw):
    return {"english_count": english_count, "languages": ["English"],
            "product_type": "Gadget", "fit": fit, "reason": "r", **kw}


OK = {"us_video_ads": 30, "days_since_active": 3, "samples": [{"video_id": "v"}]}


def test_screen_passes_multilingual_with_min_two_english():
    candidates = [{"advertiser_id": "AR1", "name": "Mixed Co"}]
    probe = _probe(AR1=OK)
    out = S.screen_candidates(candidates, probe,
                              _classify({"Mixed Co": _v(2, fit=True, languages=["Spanish", "English"])}))
    assert out["queued_order"] == ["AR1"]        # 2 English of the sample -> passes
    assert out["passed"][0]["english_count"] == 2
    assert out["skipped"] == []


def test_fit_is_priority_not_a_skip():
    candidates = [{"advertiser_id": "ARbad", "name": "Off Niche"}]
    probe = _probe(ARbad=OK)
    out = S.screen_candidates(candidates, probe, _classify({"Off Niche": _v(6, fit=False)}))
    assert out["queued_order"] == ["ARbad"]      # bad fit but English -> still queued
    assert out["passed"][0]["fit"] is False


def test_screen_skips_only_english_and_cheap_gates():
    candidates = [
        {"advertiser_id": "ARown", "name": "Coolina Store"},     # own brand
        {"advertiser_id": "ARgiant", "name": "Nike Inc"},         # giant
        {"advertiser_id": "ARsmall", "name": "Tiny Co"},          # under 20 ads
        {"advertiser_id": "ARstale", "name": "Old Co"},           # inactive
        {"advertiser_id": "ARfew", "name": "Mostly Thai Co"},     # only 1 English of sample
        {"advertiser_id": "ARbadfit", "name": "App Co"},          # English, bad fit -> PASSES
        {"advertiser_id": "ARgood", "name": "Good Gear"},         # English, good fit -> passes
    ]
    probe = _probe(
        ARsmall={"us_video_ads": 8, "days_since_active": 2, "samples": []},
        ARstale={"us_video_ads": 50, "days_since_active": 120, "samples": []},
        ARfew=OK, ARbadfit=OK, ARgood=OK,
    )
    classify = _classify({"Mostly Thai Co": _v(1, languages=["Thai", "English"]),
                          "App Co": _v(5, fit=False), "Good Gear": _v(9, fit=True)})
    out = S.screen_candidates(candidates, probe, classify)

    assert out["queued_order"] == ["ARgood", "ARbadfit"]     # good fit queued ahead of bad fit
    reasons = {r["advertiser_id"]: r["reason"] for r in out["skipped"]}
    assert reasons == {"ARown": "own-brand", "ARgiant": "giant-brand",
                       "ARsmall": "under-min-ads", "ARstale": "inactive",
                       "ARfew": "too-few-english"}
    assert "ARown" not in probe.calls and "ARgiant" not in probe.calls   # free rejects, no probe
    assert out["considered"] == 7


def test_junk_source_domain_is_a_free_reject():
    candidates = [
        {"advertiser_id": "ARjunk", "name": "Shell Co", "source_domain": "shared-junk.com"},
        {"advertiser_id": "ARrandom", "name": "Shell Two", "source_domain": "qatteeey.com"},  # random label
        {"advertiser_id": "ARgood", "name": "Good Gear", "source_domain": "realbrand.com"},
    ]
    probe = _probe(ARgood=OK)
    out = S.screen_candidates(candidates, probe, _classify({"Good Gear": _v(9, fit=True)}),
                              junk={"shared-junk.com"})
    assert out["queued_order"] == ["ARgood"]
    reasons = {r["advertiser_id"]: r["reason"] for r in out["skipped"]}
    assert reasons == {"ARjunk": "junk-domain", "ARrandom": "junk-domain"}
    assert "ARjunk" not in probe.calls and "ARrandom" not in probe.calls   # free reject, no probe


def test_queue_order_is_source_priority_then_fit():
    candidates = [
        {"advertiser_id": "ARa", "name": "A", "priority": False},   # good fit, not sourced
        {"advertiser_id": "ARb", "name": "B", "priority": True},    # bad fit, sourced
        {"advertiser_id": "ARc", "name": "C", "priority": True},    # good fit, sourced
    ]
    probe = _probe(ARa=OK, ARb=OK, ARc=OK)
    out = S.screen_candidates(candidates, probe,
                              _classify({"A": _v(5, fit=True), "B": _v(5, fit=False), "C": _v(5, fit=True)}))
    assert out["queued_order"] == ["ARc", "ARb", "ARa"]   # sourced first (fit breaks ties), then rest
    assert probe.calls[0] in ("ARb", "ARc")               # sourced candidates screened first


def test_classify_with_haiku_uses_injected_picker_offline():
    seen = {}
    def picker(name, samples):
        seen["name"] = name
        return _v(7)
    assert S.classify_with_haiku("Good Gear", [{"video_id": "v"}], picker=picker)["english_count"] == 7
    assert seen["name"] == "Good Gear"
