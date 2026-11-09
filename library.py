"""Fail-closed evidence builder for the private YouTube competitor-library pilot."""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urlparse

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
ADVERTISER_ID_RE = re.compile(r"^AR\d+$")
CREATIVE_ID_RE = re.compile(r"^CR\d+$")
YOUTUBE_FIELDS = {
    "video_id",
    "url",
    "title",
    "author",
    "views",
    "length_seconds",
    "upload_date",
    "creative_count",
    "creative_urls",
    "error",
}
CREATIVE_RESULT_FIELDS = {
    "index",
    "creative_url",
    "video_ids",
    "variation_count",
    "last_shown",
    "error",
}
PUBLIC_ALLOWLIST = ("index.html", "styles.css", "app.js", "data/library.json")


class ValidationError(ValueError):
    """Raised when evidence cannot satisfy the release contract."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def _parse_timestamp(value: str, label: str) -> datetime:
    _require(isinstance(value, str) and value.strip() == value and bool(value), f"invalid {label}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"invalid {label}") from exc
    _require(parsed.tzinfo is not None, f"{label} must include timezone")
    return parsed


def canonical_youtube_id(value: str) -> str:
    _require(isinstance(value, str), "YouTube URL must be a string")
    parsed = urlparse(value)
    _require(parsed.scheme == "https" and not parsed.username and not parsed.password, "unsafe YouTube URL")
    host = (parsed.hostname or "").lower()
    if host == "youtu.be":
        video_id = parsed.path.lstrip("/")
        _require("/" not in video_id and not parsed.query, "unsupported YouTube URL")
    elif host in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        _require(parsed.path == "/watch", "unsupported YouTube URL")
        query = parse_qs(parsed.query, keep_blank_values=True)
        _require(set(query) == {"v"} and len(query["v"]) == 1, "unsupported YouTube URL")
        video_id = query["v"][0]
    else:
        raise ValidationError("unsafe YouTube URL host")
    _require(bool(VIDEO_ID_RE.fullmatch(video_id)), "invalid YouTube video ID")
    return video_id


def validate_transparency_url(value: str, advertiser_id: str | None = None) -> str:
    _require(isinstance(value, str), "Transparency URL must be a string")
    parsed = urlparse(value)
    _require(
        parsed.scheme == "https"
        and parsed.hostname == "adstransparency.google.com"
        and not parsed.username
        and not parsed.password
        and not parsed.fragment,
        "unsafe Transparency URL",
    )
    parts = [part for part in parsed.path.split("/") if part]
    _require(
        len(parts) == 4
        and parts[0] == "advertiser"
        and ADVERTISER_ID_RE.fullmatch(parts[1]) is not None
        and parts[2] == "creative"
        and CREATIVE_ID_RE.fullmatch(parts[3]) is not None,
        "invalid Transparency URL path",
    )
    if advertiser_id is not None:
        _require(parts[1] == advertiser_id, "Transparency URL advertiser mismatch")
    query = parse_qs(parsed.query, keep_blank_values=True)
    allowed_query = {"region", "platform", "format", "hl"}
    _require(set(query) <= allowed_query, "unexpected Transparency URL query")
    if "format" in query:
        _require(query["format"] == ["VIDEO"], "unsupported Transparency creative format")
    if "platform" in query:
        _require(query["platform"] == ["YOUTUBE"], "unsupported Transparency platform")
    return value


def validate_youtube_record(value: Any, advertiser_id: str | None = None) -> dict[str, Any]:
    _require(isinstance(value, dict) and set(value) == YOUTUBE_FIELDS, "YouTube record schema mismatch")
    row = deepcopy(value)
    _require(isinstance(row["video_id"], str) and VIDEO_ID_RE.fullmatch(row["video_id"]) is not None, "invalid video_id")
    _require(canonical_youtube_id(row["url"]) == row["video_id"], "YouTube URL does not match video_id")
    _require(isinstance(row["title"], str) and bool(row["title"].strip()), "title is required")
    _require(isinstance(row["author"], str) and bool(row["author"].strip()), "author is required")
    _require(type(row["views"]) is int and row["views"] >= 0, "views must be a non-negative integer")
    _require(type(row["length_seconds"]) is int and row["length_seconds"] > 0, "duration must be positive")
    _parse_timestamp(row["upload_date"], "upload_date")
    _require(type(row["creative_count"]) is int and row["creative_count"] > 0, "creative_count must be positive")
    _require(isinstance(row["creative_urls"], list) and bool(row["creative_urls"]), "creative_urls are required")
    _require(all(isinstance(item, str) for item in row["creative_urls"]), "creative_urls must contain strings")
    urls = sorted(set(validate_transparency_url(item, advertiser_id) for item in row["creative_urls"]))
    _require(len(urls) == row["creative_count"], "creative_count does not match unique creative URLs")
    _require(row["error"] is None, "YouTube metadata error is blocking")
    row["creative_urls"] = urls
    row["url"] = f"https://www.youtube.com/watch?v={row['video_id']}"
    return row


def dedupe_records(values: Iterable[dict[str, Any]], advertiser_id: str | None = None) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    comparable = ("title", "author", "views", "length_seconds", "upload_date", "error")
    for value in values:
        row = validate_youtube_record(value, advertiser_id)
        video_id = canonical_youtube_id(row["url"])
        prior = by_id.get(video_id)
        if prior is None:
            by_id[video_id] = row
            continue
        _require(
            all(prior[field] == row[field] for field in comparable),
            f"conflicting metadata for canonical video ID {video_id}",
        )
        urls = sorted(set(prior["creative_urls"] + row["creative_urls"]))
        prior["creative_urls"] = urls
        prior["creative_count"] = len(urls)
    return [by_id[key] for key in sorted(by_id)]


def derive_metrics(value: dict[str, Any], snapshot_at: datetime) -> dict[str, Any]:
    _require(isinstance(snapshot_at, datetime) and snapshot_at.tzinfo is not None, "snapshot timestamp must be timezone-aware")
    row = deepcopy(value)
    uploaded = _parse_timestamp(row["upload_date"], "upload_date")
    elapsed_seconds = (snapshot_at - uploaded).total_seconds()
    _require(elapsed_seconds >= 0, "upload date is after snapshot timestamp")
    # Count a partial first day as one exposure day; this avoids division by zero
    # without inventing a zero age or relying on ambient wall-clock time.
    age_days = max(1, math.ceil(elapsed_seconds / 86_400))
    row["age_days"] = age_days
    row["lifetime_views_per_day"] = round(row["views"] / age_days, 2)
    row["youtube_url"] = f"https://www.youtube.com/watch?v={row['video_id']}"
    row["embed_url"] = f"https://www.youtube-nocookie.com/embed/{row['video_id']}"
    row["thumbnail_url"] = f"https://i.ytimg.com/vi/{row['video_id']}/hqdefault.jpg"
    row["transparency_url"] = row["creative_urls"][0]
    return row


def rank_records(values: list[dict[str, Any]], limit: int = 24, breakout_days: int = 30) -> dict[str, Any]:
    _require(type(limit) is int and limit > 0, "ranking limit must be positive")
    _require(type(breakout_days) is int and breakout_days > 0, "breakout window must be positive")
    most_viewed = sorted(values, key=lambda row: (-row["views"], row["video_id"]))
    fastest = sorted(values, key=lambda row: (-row["lifetime_views_per_day"], -row["views"], row["video_id"]))
    breakout = sorted(
        (row for row in values if row["age_days"] <= breakout_days),
        key=lambda row: (-row["lifetime_views_per_day"], -row["views"], row["video_id"]),
    )
    ids = lambda rows: [row["video_id"] for row in rows[:limit]]
    return {
        "most_viewed": ids(most_viewed),
        "fastest_since_upload": ids(fastest),
        "newest_breakouts": ids(breakout),
        "fastest_growing_now": {
            "available": False,
            "reason": "Requires a second YouTube statistics snapshot.",
            "items": [],
        },
        "definitions": {
            "most_viewed": "Current lifetime YouTube views, descending.",
            "fastest_since_upload": "Lifetime views divided by age in exposure days, descending.",
            "newest_breakouts": f"Videos no more than {breakout_days} exposure days old, ranked by lifetime views/day.",
        },
    }


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"cannot read valid JSON: {path.name}") from exc


def _read_jsonl(path: Path) -> list[Any]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValidationError(f"cannot read JSONL: {path.name}") from exc
    _require(all(line.strip() for line in lines), f"blank JSONL row: {path.name}")
    try:
        return [json.loads(line) for line in lines]
    except json.JSONDecodeError as exc:
        raise ValidationError(f"invalid JSONL: {path.name}") from exc


def _validate_creative_result(value: Any, advertiser_id: str) -> dict[str, Any]:
    _require(isinstance(value, dict) and set(value) == CREATIVE_RESULT_FIELDS, "creative result schema mismatch")
    _require(type(value["index"]) is int and value["index"] >= 0, "invalid creative result index")
    validate_transparency_url(value["creative_url"], advertiser_id)
    _require(isinstance(value["video_ids"], list), "video_ids must be a list")
    _require(all(isinstance(item, str) and VIDEO_ID_RE.fullmatch(item) for item in value["video_ids"]), "invalid recovered video ID")
    _require(type(value["variation_count"]) is int and value["variation_count"] >= 0, "invalid variation_count")
    _require(value["last_shown"] is None or isinstance(value["last_shown"], str), "invalid last_shown")
    _require(value["error"] is None, "creative extraction error is blocking")
    return value


def build_dataset(
    source_dir: Path,
    snapshot_at: datetime,
    advertiser: str,
    advertiser_id: str,
    reported_ads_approx: int,
) -> dict[str, Any]:
    source_dir = Path(source_dir)
    _require(isinstance(advertiser, str) and bool(advertiser.strip()), "advertiser is required")
    _require(ADVERTISER_ID_RE.fullmatch(advertiser_id) is not None, "invalid advertiser ID")
    _require(type(reported_ads_approx) is int and reported_ads_approx > 0, "reported ad estimate must be positive")
    _require(snapshot_at.tzinfo is not None, "snapshot timestamp must be timezone-aware")

    creative_urls = _read_json(source_dir / "creative_urls.json")
    _require(isinstance(creative_urls, list) and all(isinstance(url, str) for url in creative_urls), "creative URL list schema mismatch")
    _require(len(creative_urls) == len(set(creative_urls)), "creative URL list contains duplicates")
    for url in creative_urls:
        validate_transparency_url(url, advertiser_id)

    raw_results = _read_jsonl(source_dir / "creative_video_results.jsonl")
    _require(len(raw_results) == len(creative_urls), "creative result count does not match creative URL count")
    results = [_validate_creative_result(value, advertiser_id) for value in raw_results]
    _require([row["index"] for row in results] == list(range(len(results))), "creative result indices are not contiguous")
    _require({row["creative_url"] for row in results} == set(creative_urls), "creative result URLs do not reconcile")

    raw_stats = _read_jsonl(source_dir / "youtube_stats.jsonl")
    records = dedupe_records(raw_stats, advertiser_id)
    recovered_ids = {video_id for row in results for video_id in row["video_ids"]}
    stat_ids = {row["video_id"] for row in records}
    unavailable_path = source_dir / "youtube_unavailable.json"
    unavailable = _read_json(unavailable_path) if unavailable_path.exists() else []
    _require(isinstance(unavailable, list), "unavailable YouTube evidence must be a list")
    unavailable_ids: set[str] = set()
    for row in unavailable:
        _require(isinstance(row, dict) and set(row) == {"video_id", "status", "reason", "checked_at"}, "unavailable YouTube evidence schema mismatch")
        _require(isinstance(row["video_id"], str) and VIDEO_ID_RE.fullmatch(row["video_id"]) is not None, "invalid unavailable video ID")
        _require(row["status"] in {"removed", "private"}, "unsupported unavailable video status")
        _require(isinstance(row["reason"], str) and bool(row["reason"].strip()), "unavailable video reason is required")
        _parse_timestamp(row["checked_at"], "checked_at")
        _require(row["video_id"] not in unavailable_ids, "duplicate unavailable video ID")
        unavailable_ids.add(row["video_id"])
    _require(not (stat_ids & unavailable_ids), "available and unavailable YouTube IDs overlap")
    _require(recovered_ids == stat_ids | unavailable_ids, "recovered video IDs do not reconcile with YouTube metadata")

    videos = [derive_metrics(row, snapshot_at) for row in records]
    videos.sort(key=lambda row: (-row["views"], row["video_id"]))
    rankings = rank_records(videos)
    return {
        "schema_version": 1,
        "snapshot_at": snapshot_at.isoformat(),
        "advertiser": {"name": advertiser, "google_advertiser_id": advertiser_id},
        "source_summary": {
            "reported_ads_approx": reported_ads_approx,
            "unique_creative_pages": len(creative_urls),
            "recovered_unique_youtube_videos": len(videos),
            "youtube_metadata_failures": sum(row.get("error") is not None for row in raw_stats),
            "unavailable_youtube_videos": len(unavailable_ids),
        },
        "ranking_limit": 24,
        "rankings": rankings,
        "videos": videos,
        "provenance": {
            "platform": "Google Ads Transparency Center + YouTube",
            "media_policy": "Native YouTube embeds only; videos are not downloaded or rehosted.",
            "foreplay_dependency": False,
        },
    }


def retain_top_viewed(dataset: dict[str, Any], limit: int = 5) -> dict[str, Any]:
    """Return a publication copy containing only highest-viewed winners."""
    _require(type(limit) is int and limit > 0, "top-viewed limit must be positive")
    result = deepcopy(dataset)
    videos = sorted(result.get("videos", []), key=lambda row: (-row["views"], row["video_id"]))[:limit]
    result["videos"] = videos
    result["ranking_limit"] = limit
    result["rankings"] = {
        "most_viewed": [row["video_id"] for row in videos],
        "definitions": {"most_viewed": "Current lifetime YouTube views, descending."},
    }
    return result


def combine_datasets(datasets: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Combine validated advertisers while preserving each brand's rankings."""
    items = [deepcopy(item) for item in datasets]
    _require(bool(items), "at least one advertiser dataset is required")
    names: set[str] = set()
    snapshots: set[str] = set()
    for item in items:
        _require(isinstance(item, dict) and item.get("schema_version") == 1, "advertiser dataset schema mismatch")
        advertiser = item.get("advertiser")
        _require(isinstance(advertiser, dict), "advertiser dataset is missing identity")
        name = advertiser.get("name")
        _require(isinstance(name, str) and bool(name.strip()), "advertiser name is required")
        key = name.casefold().strip()
        _require(key not in names, f"duplicate advertiser: {name}")
        names.add(key)
        snapshot = item.get("snapshot_at")
        _parse_timestamp(snapshot, "snapshot_at")
        snapshots.add(snapshot)
    _require(len(snapshots) == 1, "advertiser snapshots must use the same timestamp")
    return {
        "schema_version": 2,
        "snapshot_at": next(iter(snapshots)),
        "source_summary": {
            "advertisers": len(items),
            "reported_ads_approx": sum(item["source_summary"]["reported_ads_approx"] for item in items),
            "unique_creative_pages": sum(item["source_summary"]["unique_creative_pages"] for item in items),
            "recovered_unique_youtube_videos": sum(item["source_summary"]["recovered_unique_youtube_videos"] for item in items),
            "youtube_metadata_failures": sum(item["source_summary"]["youtube_metadata_failures"] for item in items),
            "unavailable_youtube_videos": sum(item["source_summary"].get("unavailable_youtube_videos", 0) for item in items),
        },
        "advertisers": items,
        "provenance": {
            "platform": "Google Ads Transparency Center + YouTube",
            "media_policy": "Native YouTube embeds only; videos are not downloaded or rehosted.",
            "foreplay_dependency": False,
        },
    }


def write_dataset(dataset: dict[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(dataset, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def build_stage(public_dir: Path, stage_dir: Path) -> Path:
    public_dir, stage_dir = Path(public_dir), Path(stage_dir)
    _require(public_dir.is_dir() and not public_dir.is_symlink(), "public source must be a real directory")
    if stage_dir.exists():
        _require(stage_dir.is_dir() and not stage_dir.is_symlink(), "stage path must be a real directory")
        shutil.rmtree(stage_dir)
    stage_dir.mkdir(parents=True)
    for relative in PUBLIC_ALLOWLIST:
        source = public_dir / relative
        _require(source.is_file() and not source.is_symlink(), f"missing allowlisted public file: {relative}")
        destination = stage_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    manifest = public_dir / '.herenow/proxy.json'
    if manifest.exists():
        _require(manifest.is_file() and not manifest.is_symlink(), 'proxy manifest must be a real file')
        (stage_dir / '.herenow').mkdir(exist_ok=True)
        shutil.copyfile(manifest, stage_dir / '.herenow/proxy.json')
    return stage_dir


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--snapshot-at", required=True, help="Injected timezone-aware ISO timestamp")
    parser.add_argument("--public-dir", type=Path, default=Path("public"))
    parser.add_argument("--stage-dir", type=Path, default=Path("stage/private-youtube-pilot"))
    args = parser.parse_args()
    dataset = build_dataset(
        args.source,
        _parse_timestamp(args.snapshot_at, "snapshot_at"),
        "TQR Marketing Group",
        "AR15438241099155505153",
        400,
    )
    write_dataset(dataset, args.public_dir / "data/library.json")
    build_stage(args.public_dir, args.stage_dir)
    print(json.dumps({"videos": len(dataset["videos"]), "stage": str(args.stage_dir.resolve())}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
