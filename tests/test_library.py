import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from library import (
    ValidationError,
    build_dataset,
    build_stage,
    canonical_youtube_id,
    combine_datasets,
    retain_top_viewed,
    dedupe_records,
    derive_metrics,
    rank_records,
    validate_youtube_record,
)

SNAPSHOT = datetime(2026, 9, 9, tzinfo=timezone.utc)
ARTIFACTS = Path(
    "/Users/maxopenclaw/.hermes/cache/browser-use/workspace/"
    "20260909_002841_c8084e0b"
)


def record(video_id="abcdefghijk", **overrides):
    value = {
        "video_id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "title": "A real title",
        "author": "Daily Household Deals",
        "views": 1_000,
        "length_seconds": 60,
        "upload_date": "2026-09-07T00:00:00+00:00",
        "creative_count": 1,
        "creative_urls": [
            "https://adstransparency.google.com/advertiser/"
            "AR15438241099155505153/creative/CR00024466410050682881"
            "?region=US&platform=YOUTUBE&hl=en"
        ],
        "error": None,
    }
    value.update(overrides)
    return value


def write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def write_jsonl(path, values):
    path.write_text("".join(json.dumps(v) + "\n" for v in values), encoding="utf-8")


def evidence_fixture(tmp_path, youtube_records):
    tmp_path.mkdir(parents=True, exist_ok=True)
    creative_urls = sorted({url for row in youtube_records for url in row["creative_urls"]})
    results = []
    for index, url in enumerate(creative_urls):
        ids = [row["video_id"] for row in youtube_records if url in row["creative_urls"]]
        results.append(
            {
                "index": index,
                "creative_url": url,
                "video_ids": ids,
                "variation_count": 1,
                "last_shown": "Sep 8, 2026",
                "error": None,
            }
        )
    write_json(tmp_path / "creative_urls.json", creative_urls)
    write_jsonl(tmp_path / "creative_video_results.jsonl", results)
    write_jsonl(tmp_path / "youtube_stats.jsonl", youtube_records)
    return tmp_path


def test_canonical_youtube_id_accepts_only_supported_safe_urls():
    assert canonical_youtube_id("https://youtu.be/abcdefghijk") == "abcdefghijk"
    assert canonical_youtube_id("https://www.youtube.com/watch?v=abcdefghijk") == "abcdefghijk"
    with pytest.raises(ValidationError):
        canonical_youtube_id("javascript:alert(1)")
    with pytest.raises(ValidationError):
        canonical_youtube_id("https://evil.example/watch?v=abcdefghijk")


def test_strict_schema_rejects_unknown_or_missing_fields():
    missing = record()
    del missing["views"]
    with pytest.raises(ValidationError, match="schema"):
        validate_youtube_record(missing)
    with pytest.raises(ValidationError, match="schema"):
        validate_youtube_record(record(extra="not allowed"))


@pytest.mark.parametrize("field", ["views", "upload_date"])
def test_missing_rank_inputs_are_rejected_not_coerced_to_zero(field):
    bad = record(**{field: None})
    with pytest.raises(ValidationError):
        validate_youtube_record(bad)


def test_dedupe_uses_canonical_video_id_and_merges_creative_evidence():
    first = record()
    second_url = record()["creative_urls"][0].replace("CR00024466410050682881", "CR11111111111111111111")
    second = record(
        url="https://youtu.be/abcdefghijk",
        creative_count=1,
        creative_urls=[second_url],
    )
    deduped = dedupe_records([first, second])
    assert len(deduped) == 1
    assert deduped[0]["video_id"] == "abcdefghijk"
    assert deduped[0]["creative_count"] == 2
    assert deduped[0]["creative_urls"] == sorted([first["creative_urls"][0], second_url])


def test_dedupe_fails_closed_on_conflicting_metadata():
    with pytest.raises(ValidationError, match="conflicting"):
        dedupe_records([record(), record(views=999)])


def test_metrics_use_injected_snapshot_timestamp():
    derived = derive_metrics(record(views=1_000), SNAPSHOT)
    assert derived["age_days"] == 2
    assert derived["lifetime_views_per_day"] == 500.0
    assert derived["thumbnail_url"] == "https://i.ytimg.com/vi/abcdefghijk/hqdefault.jpg"
    with pytest.raises(ValidationError, match="after snapshot"):
        derive_metrics(record(upload_date="2026-09-10T00:00:00+00:00"), SNAPSHOT)


def test_rankings_have_deterministic_semantics_and_no_fake_growth():
    rows = [
        derive_metrics(record("aaaaaaaaaaa", views=900, upload_date="2026-09-08T00:00:00+00:00"), SNAPSHOT),
        derive_metrics(record("bbbbbbbbbbb", views=10_000, upload_date="2026-08-20T00:00:00+00:00"), SNAPSHOT),
        derive_metrics(record("ccccccccccc", views=8_000, upload_date="2026-07-01T00:00:00+00:00"), SNAPSHOT),
    ]
    rankings = rank_records(rows, limit=10, breakout_days=30)
    assert rankings["most_viewed"][0] == "bbbbbbbbbbb"
    assert rankings["fastest_since_upload"][0] == "aaaaaaaaaaa"
    assert rankings["newest_breakouts"] == ["aaaaaaaaaaa", "bbbbbbbbbbb"]
    assert rankings["fastest_growing_now"] == {
        "available": False,
        "reason": "Requires a second YouTube statistics snapshot.",
        "items": [],
    }


def test_build_dataset_reconciles_source_counts_and_preserves_all_records(tmp_path):
    source = evidence_fixture(tmp_path, [record()])
    dataset = build_dataset(
        source,
        SNAPSHOT,
        advertiser="TQR Marketing Group",
        advertiser_id="AR15438241099155505153",
        reported_ads_approx=400,
    )
    assert len(dataset["videos"]) == 1
    assert dataset["source_summary"] == {
        "reported_ads_approx": 400,
        "unique_creative_pages": 1,
        "recovered_unique_youtube_videos": 1,
        "youtube_metadata_failures": 0,
        "unavailable_youtube_videos": 0,
    }
    assert len(dataset["rankings"]["most_viewed"]) == 1


def test_build_dataset_fails_when_creative_results_do_not_reconcile(tmp_path):
    source = evidence_fixture(tmp_path, [record()])
    (source / "creative_video_results.jsonl").write_text("", encoding="utf-8")
    with pytest.raises(ValidationError, match="creative result count"):
        build_dataset(source, SNAPSHOT, "TQR Marketing Group", "AR15438241099155505153", 400)


def test_build_dataset_accepts_explicitly_verified_removed_video(tmp_path):
    source = evidence_fixture(tmp_path, [record()])
    rows = [json.loads(line) for line in (source / "creative_video_results.jsonl").read_text().splitlines()]
    rows[0]["video_ids"].append("zzzzzzzzzzz")
    write_jsonl(source / "creative_video_results.jsonl", rows)
    write_json(source / "youtube_unavailable.json", [{
        "video_id": "zzzzzzzzzzz",
        "status": "removed",
        "reason": "YouTube reports that this video was removed.",
        "checked_at": "2026-09-09T18:00:00+00:00",
    }])

    dataset = build_dataset(source, SNAPSHOT, "TQR Marketing Group", "AR15438241099155505153", 400)

    assert [video["video_id"] for video in dataset["videos"]] == ["abcdefghijk"]
    assert dataset["source_summary"]["unavailable_youtube_videos"] == 1


@pytest.mark.skipif(not ARTIFACTS.exists(), reason="original TQR pilot evidence exists only on the Emater Mac mini")
def test_real_evidence_build_has_required_111_video_contract():
    dataset = build_dataset(
        ARTIFACTS,
        SNAPSHOT,
        "TQR Marketing Group",
        "AR15438241099155505153",
        400,
    )
    assert len(dataset["videos"]) == 111
    assert len({v["video_id"] for v in dataset["videos"]}) == 111
    assert dataset["source_summary"]["unique_creative_pages"] == 200
    assert dataset["source_summary"]["youtube_metadata_failures"] == 0
    assert len(dataset["rankings"]["most_viewed"]) < len(dataset["videos"])


def test_stage_allowlist_and_rendered_card_contract(tmp_path):
    source = evidence_fixture(tmp_path / "source", [record()])
    public = tmp_path / "public"
    (public / "data").mkdir(parents=True)
    for name, text in {
        "index.html": '<main id="video-grid"></main><template id="video-card-template"><article class="video-card"><iframe title="YouTube video player"></iframe><a class="youtube-link"></a><a class="transparency-link"></a></article></template>',
        "styles.css": ".embed { aspect-ratio: 16 / 9; }",
        "app.js": "// renderer",
    }.items():
        (public / name).write_text(text, encoding="utf-8")
    dataset = build_dataset(source, SNAPSHOT, "TQR Marketing Group", "AR15438241099155505153", 400)
    write_json(public / "data" / "library.json", dataset)
    stage = build_stage(public, tmp_path / "stage")
    files = sorted(p.relative_to(stage).as_posix() for p in stage.rglob("*") if p.is_file())
    assert files == ["app.js", "data/library.json", "index.html", "styles.css"]
    html = (stage / "index.html").read_text(encoding="utf-8")
    assert 'id="video-card-template"' in html
    assert "video-card" in html
    assert "iframe" in html
    assert "youtube-link" in html
    assert "transparency-link" in html


def test_dashboard_uses_click_to_load_players_instead_of_eager_iframes():
    project = Path(__file__).resolve().parents[1]
    html = (project / "public" / "index.html").read_text(encoding="utf-8")
    script = (project / "public" / "app.js").read_text(encoding="utf-8")
    assert 'class="video-launch"' in html
    assert 'class="thumbnail"' in html
    assert "video.thumbnail_url" in script
    assert 'iframe.src=safeUrl(video.embed_url,"www.youtube-nocookie.com")' not in script


def test_combine_datasets_preserves_per_advertiser_rankings_and_totals():
    first_source = evidence_fixture(Path("/tmp") / "youtube-library-first", [record("aaaaaaaaaaa")])
    second_record = record(
        "bbbbbbbbbbb",
        creative_urls=[record()["creative_urls"][0].replace("CR00024466410050682881", "CR22222222222222222222")],
    )
    second_source = evidence_fixture(Path("/tmp") / "youtube-library-second", [second_record])
    first = build_dataset(first_source, SNAPSHOT, "First", "AR15438241099155505153", 10)
    second = build_dataset(second_source, SNAPSHOT, "Second", "AR15438241099155505153", 20)
    combined = combine_datasets([first, second])
    assert combined["schema_version"] == 2
    assert [item["advertiser"]["name"] for item in combined["advertisers"]] == ["First", "Second"]
    assert combined["source_summary"]["advertisers"] == 2
    assert combined["source_summary"]["recovered_unique_youtube_videos"] == 2
    assert combined["advertisers"][0]["rankings"]["most_viewed"] == ["aaaaaaaaaaa"]


def test_combine_datasets_rejects_duplicate_advertiser_identity():
    source = evidence_fixture(Path("/tmp") / "youtube-library-duplicate", [record()])
    dataset = build_dataset(source, SNAPSHOT, "Duplicate", "AR15438241099155505153", 10)
    with pytest.raises(ValidationError, match="duplicate advertiser"):
        combine_datasets([dataset, dataset])


def test_dashboard_exposes_advertiser_selector_for_multi_brand_library():
    project = Path(__file__).resolve().parents[1]
    html = (project / "public" / "index.html").read_text(encoding="utf-8")
    script = (project / "public" / "app.js").read_text(encoding="utf-8")
    assert 'id="advertiser-select"' in html
    assert "allVideos" in script
    assert "advertiserName" in script


def test_dashboard_defaults_to_one_all_brands_view_ranked_by_views_per_day():
    project = Path(__file__).resolve().parents[1]
    script = (project / "public" / "app.js").read_text(encoding="utf-8")
    assert 'optAll.value = "all"' in script
    assert "All brands" in script
    assert "b.video.lifetime_views_per_day - a.video.lifetime_views_per_day" in script
    assert 'let advertiserName = "all"' in script


def test_retain_top_viewed_publishes_only_five_winners():
    videos = [
        {"video_id": f"video-{index}", "views": index}
        for index in range(1, 9)
    ]
    dataset = {
        "videos": videos,
        "ranking_limit": 24,
        "rankings": {
            "most_viewed": [row["video_id"] for row in reversed(videos)],
            "fastest_since_upload": [row["video_id"] for row in videos],
            "newest_breakouts": [],
            "fastest_growing_now": {"available": False, "reason": "later", "items": []},
            "definitions": {"most_viewed": "Current lifetime YouTube views, descending."},
        },
    }

    result = retain_top_viewed(dataset, limit=5)

    assert [row["video_id"] for row in result["videos"]] == [
        "video-8", "video-7", "video-6", "video-5", "video-4"
    ]
    assert result["ranking_limit"] == 5
    assert result["rankings"]["most_viewed"] == [
        "video-8", "video-7", "video-6", "video-5", "video-4"
    ]
    assert set(result["rankings"]) == {"most_viewed", "definitions"}
    assert len(dataset["videos"]) == 8


def test_dashboard_has_new_used_tabs_and_views_per_day_ranking():
    project = Path(__file__).resolve().parents[1]
    html = (project / "public" / "index.html").read_text(encoding="utf-8")
    assert 'data-ranking="fastest_since_upload"' not in html
    assert 'data-ranking="newest_breakouts"' not in html
    assert 'data-tab="new"' in html and 'data-tab="used"' in html
    assert "views/day" in html.lower()
