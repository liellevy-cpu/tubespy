#!/usr/bin/env python3
"""Weekly snowball discovery of new US YouTube advertisers.

Idea: an advertiser's top ads point at product landing domains; searching the
Ads Transparency Center for a landing domain surfaces OTHER advertisers running
the same product. We snowball from the domains of current library ads to new
advertisers, dedupe against what we already have, and queue them ONE BY ONE for
the normal sequential collector. Discovery runs weekly but never on Sunday.

The pure logic here (scheduling gate, domain extraction, snowball dedup, queuing)
is unit-tested with stubs. The live ATC domain->advertiser lookup reuses the same
RestrictedBrowser + SearchCreatives RPC as collect_all and is validated on deploy.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from urllib.parse import urlparse

SUNDAY = 6  # datetime.weekday(): Monday=0 .. Sunday=6
ADVERTISER_RE = re.compile(r"/advertiser/(AR\d+)")


def is_discovery_day(when: datetime) -> bool:
    """Weekly discovery runs any day except Sunday."""
    return when.weekday() != SUNDAY


def domain_of(url: str) -> str | None:
    try:
        host = (urlparse(url if "//" in url else "https://" + url).hostname or "").lower()
    except ValueError:
        return None
    if not host or "." not in host:
        return None
    host = host.removeprefix("www.")
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def landing_domains(dataset: dict) -> list[str]:
    """Registrable landing domains from an advertiser dataset's videos.
    Uses a per-video `landing_domain` (captured during collection) when present;
    otherwise returns nothing for that video (never guessed from the title)."""
    domains: set[str] = set()
    for video in dataset.get("videos", []):
        raw = video.get("landing_domain")
        if isinstance(raw, str):
            d = domain_of(raw)
            if d:
                domains.add(d)
    return sorted(domains)


def snowball(seed_domains, known_advertiser_ids, lookup_fn) -> list[str]:
    """For each seed domain, lookup_fn(domain) -> iterable of AR advertiser IDs.
    Return new advertiser IDs (preserving first-seen order) not already known."""
    known = set(known_advertiser_ids)
    discovered: list[str] = []
    seen: set[str] = set()
    for domain in seed_domains:
        for advertiser_id in lookup_fn(domain) or []:
            m = ADVERTISER_RE.search(advertiser_id) if "/" in advertiser_id else None
            advertiser_id = m.group(1) if m else advertiser_id
            if advertiser_id.startswith("AR") and advertiser_id not in known and advertiser_id not in seen:
                seen.add(advertiser_id)
                discovered.append(advertiser_id)
    return discovered


def queue_new_advertisers(store, advertiser_ids, name_fn=None) -> list[str]:
    """Queue discovered advertisers ONE BY ONE (sequential collection downstream).
    name_fn(advertiser_id) -> display name; defaults to the advertiser ID."""
    queued: list[str] = []
    for advertiser_id in advertiser_ids:
        name = (name_fn(advertiser_id) if name_fn else None) or advertiser_id
        url = f"https://adstransparency.google.com/advertiser/{advertiser_id}"
        try:
            store.submit({"name": name, "url": url}, "discovery-snowball", trusted=True)
            queued.append(advertiser_id)
        except Exception:
            continue  # already present, paused, or capacity: skip, stay sequential
    return queued


async def advertisers_for_domain(page, domain: str, region_num: int = 2840) -> list[str]:
    """Live ATC lookup: advertisers running video ads on a landing domain (US).
    Reuses the SearchCreatives RPC; validated on deploy against live ATC."""
    body = "f.req=" + json.dumps({"2": 40, "3": {"12": {"1": domain}, "8": [region_num]}})
    response = await page.request.post(
        "https://adstransparency.google.com/anji/_/rpc/SearchService/SearchCreatives?authuser=",
        data=body, headers={"content-type": "application/x-www-form-urlencoded"}, timeout=45_000)
    text = (await response.text()).lstrip(")]}'\n")
    try:
        data = json.loads(text)
    except ValueError:
        return []
    ids: list[str] = []
    for item in data.get("1") or []:
        advertiser = item.get("1") if isinstance(item, dict) else None
        if isinstance(advertiser, str) and advertiser.startswith("AR") and advertiser not in ids:
            ids.append(advertiser)
    return ids


def run_weekly_discovery(store, library: dict, lookup_fn, when: datetime, name_fn=None) -> dict:
    """Gate on the weekly (non-Sunday) schedule, snowball from the library's
    landing domains, and queue new advertisers one by one. Pure/sync; the caller
    supplies lookup_fn (live ATC lookup or a stub)."""
    if not is_discovery_day(when):
        return {"ran": False, "reason": "Sunday is skipped", "queued": []}
    seeds: set[str] = set()
    for advertiser in library.get("advertisers", []):
        seeds.update(landing_domains(advertiser))
    known = {a.get("advertiser", {}).get("google_advertiser_id") for a in library.get("advertisers", [])}
    known.discard(None)
    new_ids = snowball(sorted(seeds), known, lookup_fn)
    queued = queue_new_advertisers(store, new_ids, name_fn)
    return {"ran": True, "seed_domains": len(seeds), "discovered": len(new_ids), "queued": queued}
