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
from urllib.parse import urlencode, urlparse

import own_brands

SUNDAY = 6  # datetime.weekday(): Monday=0 .. Sunday=6
ADVERTISER_RE = re.compile(r"/advertiser/(AR\d+)")

# Each video ad's content.js preview bundle carries the click-through as an
# `adurl=` parameter (percent-encoded). That is the landing domain. TubeSpy's
# CSV does not store it, so discovery extracts it from the preview bundle.
ADURL_RE = re.compile(r"adurl.{0,6}?https%3A%2F%2F([a-z0-9.-]+)", re.I)
# Never a product landing domain: Google's own click-redirect / tracking / infra
# hosts (a click often goes google.com/aclk?...&adurl=<real site>, so the FIRST
# adurl is the Google wrapper -- we must skip it and keep scanning), CDNs, etc.
_LANDING_NOISE = {
    "cloudfront.net", "youtu.be", "youtube.com", "googleusercontent.com",
    "gstatic.com", "ampproject.org", "w3.org", "example.com", "github.com",
    "schema.org", "doubleclick.net", "googlesyndication.com",
    "google.com", "googleadservices.com", "google-analytics.com", "goo.gl",
    "g.co", "gvt1.com", "gvt2.com", "youtube-nocookie.com", "ytimg.com",
}


def extract_landing_domain(content_js_text: str) -> str | None:
    """Registrable landing domain from an ad's content.js preview bundle, or None.
    Scans ALL adurl occurrences and returns the first that is a real product domain,
    so a Google click-redirect wrapper (google.com/aclk...) is skipped, not mistaken
    for the landing site."""
    for match in ADURL_RE.finditer(content_js_text or ""):
        host = match.group(1).lower().strip(".")
        parts = host.split(".")
        registrable = ".".join(parts[-2:]) if len(parts) >= 2 else host
        if registrable and registrable not in _LANDING_NOISE:
            return registrable
    return None


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


def snowball_sources(seed_domains, known_advertiser_ids, lookup_fn) -> list[tuple[str, str]]:
    """Like snowball, but keep each new advertiser's FIRST-seen source domain, so
    downstream screening can prioritise advertisers found via a Used / Suggested
    video. Returns ordered (advertiser_id, source_domain) pairs."""
    known = set(known_advertiser_ids)
    discovered: list[tuple[str, str]] = []
    seen: set[str] = set()
    for domain in seed_domains:
        for advertiser_id in lookup_fn(domain) or []:
            m = ADVERTISER_RE.search(advertiser_id) if "/" in advertiser_id else None
            advertiser_id = m.group(1) if m else advertiser_id
            if advertiser_id.startswith("AR") and advertiser_id not in known and advertiser_id not in seen:
                seen.add(advertiser_id)
                discovered.append((advertiser_id, domain))
    return discovered


def snowball(seed_domains, known_advertiser_ids, lookup_fn) -> list[str]:
    """For each seed domain, lookup_fn(domain) -> iterable of AR advertiser IDs.
    Return new advertiser IDs (preserving first-seen order) not already known."""
    return [a for a, _ in snowball_sources(seed_domains, known_advertiser_ids, lookup_fn)]


def priority_domains(library: dict, priority_video_ids) -> set[str]:
    """Landing domains of library videos the user marked Used or that were Suggested.
    Advertisers discovered from these domains are screened first."""
    wanted = set(priority_video_ids or ())
    domains: set[str] = set()
    for advertiser in library.get("advertisers", []):
        for video in advertiser.get("videos", []):
            if video.get("video_id") in wanted:
                raw = video.get("landing_domain")
                d = domain_of(raw) if isinstance(raw, str) else None
                if d and not own_brands.is_own_domain(d):
                    domains.add(d)
    return domains


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


async def advertisers_for_domain(page, domain: str) -> list[str]:
    """Live ATC lookup: every advertiser whose ads link to this landing domain.
    Verified live 2026-11-10 — payload shape {"3":{"12":{"1":domain,"2":true}},"7":{"1":1}}
    (the "2":true and "7":{"1":1} are required; without them the RPC returns 0).
    Runs under the mini-wide Google lock so it waits for every other scraper."""
    import collect_all  # lazy: keeps discovery's pure logic import-light
    freq = {"2": 40, "3": {"12": {"1": domain, "2": True}}, "7": {"1": 1}}
    body = urlencode({"f.req": json.dumps(freq)})
    with collect_all.google_lock("discovery:" + domain):
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


def run_weekly_discovery(store, library: dict, lookup_fn, when: datetime, name_fn=None,
                         screen_fn=None, priority_video_ids=None) -> dict:
    """Gate on the weekly (non-Sunday) schedule, snowball from the library's landing
    domains, SCREEN the candidates (when screen_fn is given), and queue only the
    survivors one by one. Pure/sync; the caller supplies lookup_fn (live ATC lookup
    or a stub) and, in production, screen_fn (screening.screen_candidates bound to a
    live ATC probe + Haiku classifier). With screen_fn=None the step is a pass-through
    (every discovered advertiser is queued), preserving the original behaviour."""
    if not is_discovery_day(when):
        return {"ran": False, "reason": "Sunday is skipped", "queued": []}
    seeds: set[str] = set()
    for advertiser in library.get("advertisers", []):
        seeds.update(landing_domains(advertiser))
    seeds = {d for d in seeds if not own_brands.is_own_domain(d)}  # don't snowball our own store
    known = {a.get("advertiser", {}).get("google_advertiser_id") for a in library.get("advertisers", [])}
    known.discard(None)
    discovered = snowball_sources(sorted(seeds), known, lookup_fn)
    if name_fn:  # a shared domain can surface our own brand — never even screen it
        discovered = [(i, d) for i, d in discovered if not own_brands.is_own_brand_name(name_fn(i))]

    hot = priority_domains(library, priority_video_ids)
    candidates = [{"advertiser_id": i, "name": (name_fn(i) if name_fn else None) or i,
                   "source_domain": d, "priority": d in hot} for i, d in discovered]

    result = {"ran": True, "seed_domains": len(seeds), "discovered": len(candidates),
              "priority_candidates": sum(1 for c in candidates if c["priority"])}
    if screen_fn is None:
        result["queued"] = queue_new_advertisers(store, [c["advertiser_id"] for c in candidates], name_fn)
        return result

    screened = screen_fn(candidates)  # -> {passed, skipped, considered, queued_order}
    survivors = [p["advertiser_id"] for p in screened["passed"]]
    survivor_names = {p["advertiser_id"]: p.get("name") for p in screened["passed"]}
    queued = queue_new_advertisers(store, survivors,
                                   name_fn=lambda a: survivor_names.get(a) or (name_fn(a) if name_fn else a))
    result.update({"queued": queued, "screened": len(candidates),
                   "passed": len(screened["passed"]), "skipped": screened["skipped"],
                   "passed_detail": screened["passed"]})
    return result
