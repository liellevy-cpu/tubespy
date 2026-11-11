#!/usr/bin/env python3
"""Pure decision logic for the nightly runner (run_nightly.py).

Kept separate from the live orchestration so the rules that matter -- when we are
allowed to run, when to STOP THE WHOLE NIGHT on a Google block, where we are allowed
to publish, and reading Liel's shared raw READ-ONLY -- are unit-tested without a
browser. run_nightly.py imports these and does the live work."""
from __future__ import annotations

import json
import re
from datetime import datetime, time
from pathlib import Path

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


# ---------- shared raw, READ-ONLY (E) ----------

def read_shared_advertisers(shared_raw_dir) -> list[dict]:
    """Read Liel's already-built advertiser datasets from his shared raw dir, READ-ONLY,
    so the All tab can show his advertisers without us re-scraping. We only read: this
    opens files for reading and never creates or writes anything under shared_raw_dir.
    Returns a list of schema_version-1 advertiser dataset dicts (possibly empty)."""
    if not shared_raw_dir:
        return []
    root = Path(shared_raw_dir)
    if not root.exists():
        return []
    out: list[dict] = []
    seen: set[str] = set()
    # Accept either combined library.json files (schema 2) or per-advertiser dataset
    # files (schema 1). Sorted for determinism. Read-only: only .read_text is used.
    for name in ("**/library.json", "**/dataset.json"):
        for f in sorted(root.glob(name)):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            entries = data.get("advertisers") if isinstance(data, dict) and data.get("schema_version") == 2 else [data]
            for adv in entries or []:
                aid = (adv.get("advertiser") or {}).get("google_advertiser_id")
                if aid and aid not in seen and adv.get("videos") is not None:
                    seen.add(aid)
                    out.append(adv)
    return out


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
