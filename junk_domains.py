#!/usr/bin/env python3
"""Detect JUNK landing domains and keep them out of discovery.

Some advertisers (notably a Hong Kong dropship ad network) send clicks through
throwaway, random-string redirect domains -- e.g. ``qatteeey.com``, ``ehooery.com``,
``mnyyoidy.com``. They are NOT real product landing pages, so they must never be
used as discovery seeds (snowballing from them finds only more shells and wastes
scrape budget), and screening must reject any candidate found through one.

A domain is junk when ANY of these hold (the three signals Henry asked for):

  1. Random-looking name        -- the registrable label reads like noise, not a
                                   word/brand (``looks_random``), e.g. 'qatteeey'.
  2. Shared across advertisers   -- the same registrable domain is the landing
                                   domain of >= ``min_shared`` DISTINCT advertisers.
                                   A real brand owns its domain; a redirect network
                                   reuses the same throwaway domains across unrelated
                                   shells.
  3. Multiple domains per video  -- one video points at >= 2 distinct landing
                                   domains (a legit ad has ONE landing page). Both
                                   domains are flagged. Fed by a per-video
                                   ``landing_domains`` (plural) list when present.

Our own store domains are never junk (they are handled by ``own_brands``), and
Google's own click/CDN noise hosts are ignored here (``discovery`` already drops
them upstream). Pure and unit-tested; callers pass the built library's advertisers.
"""
from __future__ import annotations

from collections import defaultdict

import own_brands
from discovery import domain_of, _LANDING_NOISE

_VOWELS = set("aeiou")
# Letter pairs that are vanishingly rare inside real English words/brands; a
# doubled one is a strong "random string" tell. Common doubles (ee, oo, ss, ll,
# tt, ff, mm, nn, pp, rr, dd, gg, cc, bb) are deliberately NOT here.
_IMPROBABLE_DOUBLES = ("yy", "ii", "uu", "jj", "qq", "vv", "xx", "ww", "kk", "zz", "hh")


def _label(domain: str | None) -> str:
    """The registrable domain's first label, lowercased (``qatteeey.com`` -> ``qatteeey``)."""
    reg = domain_of(domain or "") or ""
    return reg.split(".")[0] if reg else ""


def looks_random(label: str) -> bool:
    """True when a domain label reads like a random string rather than a word or
    brand. Deliberately CONSERVATIVE (high precision): it fires only on strong tells,
    so a legit but oddly spelled brand ('mudwtr') is left alone -- the shared-across-
    advertisers and multi-domain-per-video signals catch anything this misses.

    Strong tells (any one):
      * 'q' not followed by 'u'               (qatteeey)
      * a letter repeated 3x in a row         (qatteeey -> 'eee')
      * an improbable double (yy, ii, jj, ...) (mnyyoidy -> 'yy')
      * a run of 5+ consonants                (real words top out around 4)
      * no vowel at all in a 4+ char label
    """
    s = (label or "").lower()
    if len(s) < 5:
        return False                                   # too short to judge; leave it
    if "q" in s and "qu" not in s:
        return True
    if any(a == b == c for a, b, c in zip(s, s[1:], s[2:])):   # tripled letter
        return True
    if any(d in s for d in _IMPROBABLE_DOUBLES):
        return True
    run = 0
    for ch in s:
        if ch.isalpha() and ch not in _VOWELS:         # 'y' counts as a consonant here
            run += 1
            if run >= 5:
                return True
        else:
            run = 0
    if not (set(s) & _VOWELS):                         # vowelless label (len >= 5)
        return True
    return False


def _video_domains(video: dict) -> set[str]:
    """Every distinct registrable landing domain attached to one video -- the single
    ``landing_domain`` plus any ``landing_domains`` (plural) captured when a video was
    seen pointing at more than one domain. Google/CDN noise hosts are ignored."""
    out: set[str] = set()
    raw = [video.get("landing_domain")]
    raw.extend(video.get("landing_domains") or [])
    for d in raw:
        if not isinstance(d, str):
            continue
        reg = domain_of(d)
        if reg and reg not in _LANDING_NOISE:
            out.add(reg)
    return out


def find_junk_domains(advertisers: list[dict], *, min_shared: int = 2) -> set[str]:
    """Registrable landing domains that are junk, from the built library's advertisers.
    Applies all three signals. Never flags our own store domains."""
    domain_advs: dict[str, set] = defaultdict(set)
    junk: set[str] = set()
    for adv in advertisers or []:
        aid = (adv.get("advertiser") or {}).get("google_advertiser_id") or id(adv)
        for video in adv.get("videos", []):
            doms = _video_domains(video)
            if len(doms) >= 2:                         # (3) multiple domains per video
                junk |= doms
            for reg in doms:
                domain_advs[reg].add(aid)
    for reg, advs in domain_advs.items():
        if len(advs) >= min_shared:                    # (2) shared across advertisers
            junk.add(reg)
        elif looks_random(reg.split(".")[0]):          # (1) random-looking name
            junk.add(reg)
    return {d for d in junk if not own_brands.is_own_domain(d)}


def is_junk(domain: str | None, junk: set[str]) -> bool:
    """True when a landing/source domain's registrable form is in the junk set, or
    its label looks random on its own (so a brand-new junk domain is caught even
    before it has been seen shared)."""
    reg = domain_of(domain or "")
    if not reg:
        return False
    if reg in junk:
        return True
    return looks_random(reg.split(".")[0]) and not own_brands.is_own_domain(reg)
