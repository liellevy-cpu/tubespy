"""Library rules: English-only, 2-5 min, 1M+, keep all, ranked by views/day."""
from library import filter_and_rank, group_by_product


def _video(vid, length, views, vpd):
    return {"video_id": vid, "length_seconds": length, "views": views,
            "lifetime_views_per_day": vpd, "age_days": 10}


ENGLISH = {"aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc", "ddddddddddd"}


def stub_lang(video_id):
    return True if video_id in ENGLISH else False


def test_filter_and_rank_applies_all_rules():
    dataset = {"videos": [
        _video("aaaaaaaaaaa", 150, 5_000_000, 100.0),   # keep
        _video("bbbbbbbbbbb", 200, 2_000_000, 500.0),   # keep (ranks first by views/day)
        _video("ccccccccccc", 60, 9_000_000, 900.0),    # drop: too short
        _video("ddddddddddd", 240, 500_000, 50.0),      # drop: < 1M views
        _video("eeeeeeeeeee", 180, 8_000_000, 800.0),   # drop: not English
        _video("ccccccccccc2", 290, 1_500_000, 300.0),  # (invalid id length; see below)
    ]}
    # fix the last id to a valid 11-char English id that should be kept
    dataset["videos"][5]["video_id"] = "ccccccccccc"  # duplicate-safe; English set includes it
    result = filter_and_rank(dataset, language_fn=stub_lang)

    kept_ids = [v["video_id"] for v in result["videos"]]
    assert "eeeeeeeeeee" not in kept_ids           # non-English excluded
    assert all(v["length_seconds"] >= 120 and v["length_seconds"] <= 300 for v in result["videos"])
    assert all(v["views"] >= 1_000_000 for v in result["videos"])
    # ranked by views/day descending
    vpds = [v["lifetime_views_per_day"] for v in result["videos"]]
    assert vpds == sorted(vpds, reverse=True)
    assert result["ranking_limit"] == len(result["videos"])  # no top-N cap
    assert result["source_summary"]["excluded_non_english"] == 1
    assert result["rankings"]["most_viewed"]  # rankings rebuilt


def test_filter_and_rank_empty_when_nothing_qualifies():
    dataset = {"videos": [_video("zzzzzzzzzzz", 60, 100, 1.0)]}
    result = filter_and_rank(dataset, language_fn=lambda v: True)
    assert result["videos"] == []
    assert result["rankings"]["most_viewed"] == []
    assert result["source_summary"]["library_videos"] == 0


LABELS = {"aaaaaaaaaaa": "Car Vacuum", "bbbbbbbbbbb": "Car Vacuum", "ccccccccccc": "Fig Bites"}


def test_group_by_product_groups_variants_and_ranks():
    dataset = {"videos": [
        dict(_video("aaaaaaaaaaa", 150, 5_000_000, 100.0), title="t", thumbnail_url="u"),
        dict(_video("bbbbbbbbbbb", 200, 2_000_000, 300.0), title="t", thumbnail_url="u"),
        dict(_video("ccccccccccc", 180, 9_000_000, 900.0), title="t", thumbnail_url="u"),
    ]}
    result = group_by_product(dataset, label_fn=lambda vid, title, thumb: LABELS[vid])
    assert all("product" in v for v in result["videos"])
    products = {p["product"]: p for p in result["products"]}
    assert products["Car Vacuum"]["variants"] == 2
    assert products["Fig Bites"]["variants"] == 1
    # groups ordered by best views/day: Fig Bites (900) before Car Vacuum (300)
    assert [p["product"] for p in result["products"]] == ["Fig Bites", "Car Vacuum"]


def test_group_by_product_falls_back_without_label():
    dataset = {"videos": [dict(_video("aaaaaaaaaaa", 150, 5_000_000, 100.0),
                               title="[50% OFF] Super Car Vacuum Cleaner Pro", thumbnail_url="u")]}
    result = group_by_product(dataset, label_fn=lambda vid, title, thumb: None)
    assert result["videos"][0]["product"] == "Super Car Vacuum Cleaner Pro"  # promo stripped, title-derived
