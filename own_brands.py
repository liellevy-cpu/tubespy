#!/usr/bin/env python3
"""Our own brands — never surface them as competitor swipes or discovery targets.

Coolina is the audience we build swipes FOR, so Coolina's own ads (and our other
owned brands) must never appear in Suggestions, in the New tab, or get queued by
discovery. This is the single config list; edit the two lists below to add or
remove an owned brand and every surface picks it up:

  * Suggestions  -> suggestions.generate() drops owned-brand videos
  * New tab      -> library.combine_datasets() drops owned-brand advertisers
                    from the built library.json the site reads
  * Discovery    -> discovery.run_weekly_discovery() drops owned landing domains
                    from the seeds and never queues an owned advertiser
"""
from __future__ import annotations

# Advertiser display names we own — matched as case-insensitive substrings, so
# "Coolina", "Coolina Store" and "COOLINA LLC" all match the token "coolina".
OWN_BRAND_NAMES = ["coolina", "emater", "bodywell"]

# Store / landing domains we own — matched on the registrable domain (and on any
# domain whose first label carries an owned token, e.g. coolina.com).
OWN_DOMAINS = ["coolinastore.com"]


def _norm(text: str | None) -> str:
    return (text or "").strip().lower()


def is_own_brand_name(name: str | None) -> bool:
    """True if an advertiser display name belongs to one of our own brands."""
    n = _norm(name)
    return any(token in n for token in OWN_BRAND_NAMES)


def is_own_domain(domain: str | None) -> bool:
    """True if a landing/store domain is one of ours."""
    d = _norm(domain).removeprefix("www.")
    if not d:
        return False
    if d in {x.lower() for x in OWN_DOMAINS}:
        return True
    label = d.split(".")[0]  # catches coolina.com / coolinastore.net / …
    return any(token in label for token in OWN_BRAND_NAMES)


def is_own(name: str | None = None, domain: str | None = None) -> bool:
    """True if either the advertiser name or the landing domain is ours."""
    return is_own_brand_name(name) or is_own_domain(domain)


def drop_own_advertisers(advertisers: list[dict]) -> list[dict]:
    """Drop owned-brand entries from a list of advertiser datasets (schema v1/v2
    shape, each with an ``advertiser`` identity dict)."""
    return [a for a in advertisers
            if not is_own_brand_name((a.get("advertiser") or {}).get("name"))]
