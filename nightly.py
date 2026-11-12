#!/usr/bin/env python3
"""Pure decision logic for the nightly runner (run_nightly.py).

Kept separate from the live orchestration so the rules that matter -- when we are
allowed to run, when to STOP THE WHOLE NIGHT on a Google block, where we are allowed
to publish, and reading Liel's shared raw READ-ONLY -- are unit-tested without a
browser. run_nightly.py imports these and does the live work."""
from __future__ import annotations

import json
import re
from datetime import datetime, time, timezone
from pathlib import Path

import library
import mini_config


# ---------- schedule window (D) ----------

def is_run_day(when: datetime, skip_weekday: int | None = None) -> bool:
    """We never run on Liel's refresh day (Sunday by default)."""
    skip = mini_config.SKIP_WEEKDAY if skip_weekday is None else skip_weekday
    return when.weekday() != skip


def parse_hhmm(value: str) -> time:
    h, m = value.strip().split(":")
    return time(int(h), int(m))


def in_run_window(when: datetime, start: str | None = None, end: str | None = None) -> bool:
    """True when the local time of `when` is inside [start, end] (HH:MM). Supports a
    window that crosses midnight (e.g. 23:30–01:00)."""
    start_t = parse_hhmm(start if start is not None else mini_config.RUN_WINDOW_START)
    end_t = parse_hhmm(end if end is not None else mini_config.RUN_WINDOW_END)
    now_t = when.timetz().replace(tzinfo=None)
    if start_t <= end_t:
        return start_t <= now_t <= end_t
    return now_t >= start_t or now_t <= end_t          # crosses midnight


def should_run(when: datetime) -> tuple[bool, str]:
    """(ok, reason). ok=False with a reason when today/now is not a run slot."""
    if not is_run_day(when):
        return False, "skip: Liel's refresh day (never run Sunday)"
    if not in_run_window(when):
        return False, f"skip: outside run window {mini_config.RUN_WINDOW_START}-{mini_config.RUN_WINDOW_END} {mini_config.RUN_TZ}"
    return True, "in window"


# ---------- Google block detection (D) ----------

_BLOCK_PATTERNS = (
    "unusual traffic", "too many requests", "http 429", "status 429", "429 ",
    "/sorry/", "recaptcha", "are you a robot", "automated queries",
)


def is_block_signal(blob) -> bool:
    """True if the text/record looks like a Google rate-limit / 'unusual traffic'
    block. We STOP THE WHOLE NIGHT on the first one -- better to lose a night than to
    get the mini's IP blocked for everyone."""
    if blob is None:
        return False
    text = (blob if isinstance(blob, str) else json.dumps(blob, default=str)).lower()
    if re.search(r"(?<!\d)429(?!\d)", text) and ("request" in text or "traffic" in text or "http" in text or "status" in text):
        return True
    return any(p in text for p in _BLOCK_PATTERNS)


class NightBlocked(RuntimeError):
    """Raised to stop the whole night at the first Google block."""


# ---------- publish target guard (G) ----------

def assert_safe_publish_target(slug: str | None, path: Path | None = None) -> None:
    """Refuse to publish to Liel's live library. Our default target is a separate
    preview; this is the backstop that makes 'never Liel's live page' enforceable."""
    live = mini_config.LIVE_PUBLISH_SLUG.lower()
    if slug and live in slug.strip().lower():
        raise ValueError(f"refusing to publish to the live library slug '{slug}'; "
                         "set TUBESPY_PUBLISH_SLUG to a separate preview target")
    if path is not None and live in str(path).lower():
        raise ValueError(f"refusing to publish under a live-library path '{path}'")


# ---------- shared raw, READ-ONLY (E / #4) ----------

_CREATIVE_RE = re.compile(r"/advertiser/(AR\d+)/creative/CR\d+")


def _lang_base(code) -> str | None:
    return str(code).split(".")[0].split("-")[0].lower() if code else None


def _read_jsonl(path: Path) -> list[dict]:
    rows = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return rows


def build_advertiser_from_raw(adv_raw_dir: Path, snapshot: datetime, lang_fn=None) -> dict | None:
    """Build ONE advertiser's videos from Liel's raw scrape files (ad list + video
    stats), READ-ONLY -- Henry's option (a). Reuses library's own metric/ranking logic;
    leniently skips videos without clean stats instead of failing. Never writes under
    adv_raw_dir. Returns a schema-1 advertiser dataset, or None if there's nothing usable.

    English-only: every surviving video is run through the language detector and kept
    only if its spoken language is confirmed English; non-English videos never enter the
    library. `lang_fn(video_id) -> {"english": bool|None, "lang": str}`; defaults to
    language.detect_language, tests inject a stub to stay offline."""
    adv_raw_dir = Path(adv_raw_dir)
    results = _read_jsonl(adv_raw_dir / "creative_video_results.jsonl")
    stats_rows = _read_jsonl(adv_raw_dir / "youtube_stats.jsonl")
    if not results and not stats_rows:
        return None

    # advertiser identity: summary.json if present, else inferred from a creative URL
    name = advertiser_id = None
    try:
        summary = json.loads((adv_raw_dir / "summary.json").read_text(encoding="utf-8"))
        name = summary.get("name")
        ids = summary.get("advertiser_ids") or []
        advertiser_id = ids[0] if ids else None
        reported = summary.get("creative_count")
    except (OSError, ValueError):
        reported = None
    if not advertiser_id:
        for row in results:
            m = _CREATIVE_RE.search(row.get("creative_url") or "")
            if m:
                advertiser_id = m.group(1)
                break
    if not advertiser_id:
        return None
    name = name or advertiser_id

    unavailable = set()
    try:
        unavailable = {r.get("video_id") for r in json.loads((adv_raw_dir / "youtube_unavailable.json").read_text(encoding="utf-8"))}
    except (OSError, ValueError):
        pass

    # Collect EVERY distinct landing domain each video was seen with. A legit ad has
    # one landing page; a video pointing at two or more distinct domains is the junk
    # redirect-network signature (junk_domains signal 3), so we keep them all.
    landing_by_video: dict[str, str] = {}
    landing_set_by_video: dict[str, list[str]] = {}
    for row in results:
        domain = row.get("landing_domain")
        if not domain:
            continue
        for vid in row.get("video_ids") or []:
            seen = landing_set_by_video.setdefault(vid, [])
            if domain not in seen:
                seen.append(domain)
            landing_by_video.setdefault(vid, domain)      # first-seen stays the primary

    clean = [r for r in stats_rows if isinstance(r, dict) and r.get("error") is None and r.get("video_id") not in unavailable]
    try:
        records = library.dedupe_records(clean, advertiser_id)
        videos = [library.derive_metrics(r, snapshot) for r in records]
    except library.ValidationError:
        return None
    if not videos:
        return None
    for v in videos:
        v["landing_domain"] = landing_by_video.get(v["video_id"])
        domains = landing_set_by_video.get(v["video_id"]) or []
        if len(domains) > 1:                               # record the multi-domain case
            v["landing_domains"] = sorted(domains)

    # English-only gate: confirm spoken language per video; drop anything not English.
    if lang_fn is None:
        from language import detect_language as lang_fn
    english = []
    for v in videos:
        res = lang_fn(v["video_id"]) or {}
        if res.get("english") is True:
            v["english"] = True
            v["language"] = _lang_base(res.get("lang")) or "en"
            english.append(v)
    if not english:
        return None
    videos = english
    videos.sort(key=lambda r: (-r["views"], r["video_id"]))
    return {
        "schema_version": 1, "snapshot_at": snapshot.isoformat(),
        "advertiser": {"name": name, "google_advertiser_id": advertiser_id},
        "source_summary": {"reported_ads_approx": max(1, reported or len(videos)),
                           "unique_creative_pages": len(results) or len(videos),
                           "recovered_unique_youtube_videos": len(videos),
                           "youtube_metadata_failures": 0, "unavailable_youtube_videos": len(unavailable)},
        "ranking_limit": 24, "rankings": library.rank_records(videos), "videos": videos,
        "provenance": {"platform": "Google Ads Transparency Center + YouTube (shared read-only)",
                       "media_policy": "Native YouTube embeds only; videos are not downloaded or rehosted.",
                       "foreplay_dependency": False},
    }


def read_shared_advertisers(shared_raw_dir, snapshot: datetime | None = None, lang_fn=None) -> list[dict]:
    """Build Liel's advertisers from his raw scrape files, READ-ONLY, so the All tab can
    show them without us re-scraping. Each advertiser is a directory holding
    youtube_stats.jsonl / creative_video_results.jsonl. We only READ under
    shared_raw_dir and never create or modify anything there."""
    if not shared_raw_dir:
        return []
    root = Path(shared_raw_dir)
    if not root.exists():
        return []
    snapshot = snapshot or datetime.now(timezone.utc)
    dirs = {p.parent for p in root.rglob("youtube_stats.jsonl")} | {p.parent for p in root.rglob("creative_video_results.jsonl")}
    out, seen = [], set()
    for d in sorted(dirs):
        adv = build_advertiser_from_raw(d, snapshot, lang_fn=lang_fn)
        if adv:
            aid = adv["advertiser"]["google_advertiser_id"]
            if aid not in seen:
                seen.add(aid)
                out.append(adv)
    return out


# ---------- per-brand incremental merge into the library (#1) ----------

def merge_advertiser(library_doc: dict, advertiser_dataset: dict) -> dict:
    """Insert/replace ONE advertiser's schema-1 dataset in the schema-2 library, by
    advertiser ID, and refresh the summary counts. Pure; returns a new dict. Used after
    each successful scrape so a brand's videos land in library.json immediately."""
    aid = (advertiser_dataset.get("advertiser") or {}).get("google_advertiser_id")
    result = dict(library_doc)
    advertisers = [a for a in library_doc.get("advertisers", [])
                   if (a.get("advertiser") or {}).get("google_advertiser_id") != aid]
    advertisers.append(advertiser_dataset)
    result["advertisers"] = advertisers
    summary = dict(result.get("source_summary") or {})
    summary["advertisers"] = len(advertisers)
    summary["displayed_videos"] = sum(len(a.get("videos") or []) for a in advertisers)
    result["source_summary"] = summary
    return result


def merge_all_tab(our_library: dict, shared_advertisers: list[dict]) -> dict:
    """Union our advertisers with Liel's shared (read-only) advertisers for the All tab.
    Ours win on an advertiser-ID collision; Liel's are never mutated. Returns a new dict;
    does not touch the shared source."""
    result = dict(our_library)
    advertisers = list(our_library.get("advertisers", []))
    have = {(a.get("advertiser") or {}).get("google_advertiser_id") for a in advertisers}
    for adv in shared_advertisers:
        aid = (adv.get("advertiser") or {}).get("google_advertiser_id")
        if aid and aid not in have:
            have.add(aid)
            advertisers.append(adv)
    result["advertisers"] = advertisers
    summary = dict(result.get("source_summary") or {})
    summary["advertisers"] = len(advertisers)
    summary["displayed_videos"] = sum(len(a.get("videos") or []) for a in advertisers)
    summary["shared_advertisers_included"] = sum(
        1 for a in advertisers if (a.get("advertiser") or {}).get("google_advertiser_id") not in
        {(x.get("advertiser") or {}).get("google_advertiser_id") for x in our_library.get("advertisers", [])})
    result["source_summary"] = summary
    return result
