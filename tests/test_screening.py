"""Advertiser-level pre-scrape screening: giant-brand filter, cheap size/recency
gate, Haiku language/fit gate, priority ordering, and a reason for every skip.
The orchestrator is pure; probe + classify are stubbed."""
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


GOOD = {"english": True, "language": "English", "product_type": "Car gadget",
        "fit": True, "reason": "DR car gadget, on-audience"}
BADFIT = {"english": True, "language": "English", "product_type": "Mobile app",
          "fit": False, "reason": "app, off-audience"}
NONEN = {"english": False, "language": "Thai", "product_type": "?", "fit": True, "reason": "x"}


def test_screen_passes_only_size_recency_english_and_fit():
    candidates = [{"advertiser_id": "AR1", "name": "Good Gear"}]
    probe = _probe(AR1={"us_video_ads": 40, "days_since_active": 5, "samples": [{"video_id": "v"}]})
    out = S.screen_candidates(candidates, probe, _classify({"Good Gear": GOOD}))
    assert out["queued_order"] == ["AR1"]
    assert out["passed"][0]["product_type"] == "Car gadget"
    assert out["skipped"] == []


def test_screen_skips_with_reasons_and_is_cheap_first():
    candidates = [
        {"advertiser_id": "ARown", "name": "Coolina Store"},     # own brand
        {"advertiser_id": "ARgiant", "name": "Nike Inc"},         # giant
        {"advertiser_id": "ARsmall", "name": "Tiny Co"},          # under 20 ads
        {"advertiser_id": "ARstale", "name": "Old Co"},           # inactive
        {"advertiser_id": "ARnonen", "name": "Thai Co"},          # not English
        {"advertiser_id": "ARbad", "name": "App Co"},             # bad fit
        {"advertiser_id": "ARgood", "name": "Good Gear"},         # passes
    ]
    probe = _probe(
        ARsmall={"us_video_ads": 8, "days_since_active": 2, "samples": []},
        ARstale={"us_video_ads": 50, "days_since_active": 120, "samples": []},
        ARnonen={"us_video_ads": 30, "days_since_active": 3, "samples": [{"video_id": "v"}]},
        ARbad={"us_video_ads": 30, "days_since_active": 3, "samples": [{"video_id": "v"}]},
        ARgood={"us_video_ads": 30, "days_since_active": 3, "samples": [{"video_id": "v"}]},
    )
    classify = _classify({"Thai Co": NONEN, "App Co": BADFIT, "Good Gear": GOOD})
    out = S.screen_candidates(candidates, probe, classify)

    assert out["queued_order"] == ["ARgood"]
    reasons = {r["advertiser_id"]: r["reason"] for r in out["skipped"]}
    assert reasons == {"ARown": "own-brand", "ARgiant": "giant-brand",
                       "ARsmall": "under-min-ads", "ARstale": "inactive",
                       "ARnonen": "non-english", "ARbad": "bad-fit"}
    # own + giant were rejected for free -- never probed
    assert "ARown" not in probe.calls and "ARgiant" not in probe.calls
    assert out["considered"] == 7


def test_screen_orders_priority_candidates_first():
    candidates = [
        {"advertiser_id": "ARnorm", "name": "Norm Co", "priority": False},
        {"advertiser_id": "ARhot", "name": "Hot Co", "priority": True},
    ]
    probe = _probe(
        ARnorm={"us_video_ads": 30, "days_since_active": 3, "samples": [{"video_id": "v"}]},
        ARhot={"us_video_ads": 30, "days_since_active": 3, "samples": [{"video_id": "v"}]},
    )
    out = S.screen_candidates(candidates, probe, _classify({"Norm Co": GOOD, "Hot Co": GOOD}))
    assert out["queued_order"] == ["ARhot", "ARnorm"]      # priority screened/queued first
    assert probe.calls[0] == "ARhot"


def test_classify_with_haiku_uses_injected_picker_offline():
    seen = {}
    def picker(name, samples):
        seen["name"] = name
        return GOOD
    assert S.classify_with_haiku("Good Gear", [{"video_id": "v"}], picker=picker) == GOOD
    assert seen["name"] == "Good Gear"
