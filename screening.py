#!/usr/bin/env python3
"""Advertiser-level screening BEFORE any scrape (scrapes are expensive).

Discovery snowballs landing domains to candidate advertisers. Each candidate is
screened on cheap signals FIRST, and only survivors are queued for the full
sequential collector:

  1. Size + recency gate (one light ATC probe, NOT a full scrape): at least
     MIN_US_VIDEO_ADS US YouTube video ads, and shown within MAX_IDLE_DAYS days.
  2. One cheap Haiku call: up to SAMPLE_SIZE sample titles + thumbnails -> the
     advertiser's dominant language, product type, and Coolina fit (Liel's guide).
     Drop if not English or a clear bad fit.
  3. Priority: candidates discovered from a video the user marked Used, or that
     made it into Suggestions, are screened first.
  4. Never our own brands, never giant mega-brands.

Every skip is logged with a reason. The orchestrator (screen_candidates) is pure
and unit-tested with stubs; the live ATC probe (probe_advertiser) and the live
Haiku call (classify_with_haiku) are injected -- the same pure/live split as
discovery.run_weekly_discovery and discovery.advertisers_for_domain.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import own_brands

ROOT = Path(__file__).resolve().parent
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-5-5")
ENV_CANDIDATES = (
    ROOT / ".env",
    Path(r"C:\Users\user\youtube-stats\.env"),
    Path.home() / "youtube-stats" / ".env",
)

MIN_US_VIDEO_ADS = 20     # requirement 1: a real, running US video-ad advertiser
MAX_IDLE_DAYS = 30        # requirement 1: active in the last 30 days
SAMPLE_SIZE = 5           # requirement 2: 5 sample titles + thumbnails to Haiku

# Mega-brands we never want in a Coolina competitor swipe library: household
# names with their own brand pull -- the opposite of the hungry direct-response
# advertisers discovery looks for. Matched as whole tokens in the lowercased name.
GIANT_BRANDS = {
    "nike", "adidas", "puma", "reebok", "under armour", "new balance",
    "apple", "samsung", "google", "microsoft", "amazon", "meta", "facebook",
    "instagram", "tiktok", "netflix", "disney", "sony", "lg", "panasonic",
    "huawei", "xiaomi", "oppo", "dell", "hp", "lenovo", "asus", "intel", "nvidia",
    "coca-cola", "coca cola", "pepsi", "pepsico", "nestle", "unilever",
    "procter & gamble", "procter and gamble", "p&g", "loreal", "l'oreal",
    "colgate", "gillette", "dove", "nivea", "toyota", "honda", "nissan", "ford",
    "chevrolet", "chevy", "bmw", "mercedes", "mercedes-benz", "audi", "volkswagen",
    "hyundai", "kia", "tesla", "walmart", "target", "costco", "ikea", "home depot",
    "starbucks", "mcdonald", "mcdonald's", "burger king", "kfc", "pfizer",
    "johnson & johnson", "visa", "mastercard", "paypal", "oracle", "ibm", "sap",
    "adobe", "salesforce", "shopify", "nestlé", "red bull", "heineken", "budweiser",
}

FIT_GUIDE = (
    "Coolina sells knives and kitchen/outdoor gear to a mostly-male audience. "
    "GOOD fits: tools, gadgets, cars / scratch repair, work gear, knives / cooking, "
    "backyard / outdoor gear -- things men like. BAD fits: apps, hospitals, "
    "supplements, giant brands, and ads that open on a pain Coolina doesn't have. "
    "STRONG signals: hardcore direct-response (not branding), an advertiser with a "
    "gadget niche for this audience, an advertiser that also sells knives, and ads "
    "that look like they ran on TV."
)


# ---------- shared helpers ----------

def _api_key() -> str | None:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"]
    for path in ENV_CANDIDATES:
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("ANTHROPIC_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'") or None
        except OSError:
            continue
    return None


_WORD = r"(?<![a-z0-9])%s(?![a-z0-9])"


def is_giant_brand(name: str | None) -> bool:
    """True when the advertiser name contains a mega-brand token as a whole word.
    Word-bounded so 'ford' doesn't match 'Bradford' nor 'apple' 'Pineapple'."""
    n = (name or "").lower()
    if not n:
        return False
    return any(re.search(_WORD % re.escape(token), n) for token in GIANT_BRANDS)


# ---------- the cheap Haiku classifier (requirement 2) ----------

def classify_with_haiku(name: str, samples: list[dict], picker=None) -> dict:
    """One cheap call: up to SAMPLE_SIZE thumbnails + titles -> the advertiser's
    dominant language, product type, and Coolina fit. picker(name, samples)->dict
    lets tests stay offline. Returns
    {language, english: bool, product_type, fit: bool, reason}."""
    if picker is not None:
        return picker(name, samples)
    key = _api_key()
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set; cannot screen advertisers")
    content: list[dict] = []
    titles: list[str] = []
    for i, s in enumerate(samples[:SAMPLE_SIZE], 1):
        vid = s.get("video_id")
        thumb = s.get("thumbnail_url") or (f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg" if vid else None)
        if thumb:
            content.append({"type": "image", "source": {"type": "url", "url": thumb}})
        titles.append(f"{i}. {s.get('title') or '(no title)'}")
    instruction = (
        "You screen competitor advertisers for Coolina's YouTube competitor swipe "
        "library. " + FIT_GUIDE + f"\n\nAdvertiser name: {name}\n"
        f"Below are up to {SAMPLE_SIZE} of this advertiser's video ads (each thumbnail "
        "followed by its title):\n" + "\n".join(titles) +
        "\n\nJudge the advertiser AS A WHOLE. Reply with ONLY a JSON object:\n"
        '{"language": "<dominant ad language>", "english": <true|false>, '
        '"product_type": "<short product category>", "fit": <true|false>, '
        '"reason": "<one short line>"}\n'
        "english=true only if the ads are predominantly English. fit=true only if this "
        "advertiser is a GOOD or STRONG fit per the guide; fit=false for BAD fits, "
        "unrelated products, and giant brands."
    )
    content.append({"type": "text", "text": instruction})
    params = {"model": MODEL, "max_tokens": 512, "messages": [{"role": "user", "content": content}]}
    if MODEL.startswith("claude-haiku"):
        params["thinking"] = {"type": "disabled"}   # trivial task; keep the small budget for the answer
    import anthropic
    client = anthropic.Anthropic(api_key=key)
    text = next((b.text for b in client.messages.create(**params).content if b.type == "text"), "")
    m = re.search(r"\{.*\}", text, re.S)
    data = json.loads(m.group(0)) if m else {}
    return {
        "language": data.get("language"),
        "english": bool(data.get("english")),
        "product_type": data.get("product_type"),
        "fit": bool(data.get("fit")),
        "reason": str(data.get("reason") or "").strip(),
    }


# ---------- the pure orchestrator ----------

def _ordered(candidates: list[dict]) -> list[dict]:
    """Priority candidates first; stable within each group (keeps discovery order)."""
    return sorted(candidates, key=lambda c: 0 if c.get("priority") else 1)


def screen_candidates(candidates: list[dict], probe_fn, classify_fn,
                      min_ads: int = MIN_US_VIDEO_ADS,
                      max_idle_days: int = MAX_IDLE_DAYS) -> dict:
    """Screen candidate advertisers BEFORE any scrape. Pure: probe_fn(advertiser_id)
    -> {us_video_ads, days_since_active, samples:[{video_id,title,thumbnail_url}]}
    and classify_fn(name, samples) -> {english,language,product_type,fit,reason} are
    injected (live or stubbed). Returns {passed, skipped, considered, queued_order}.

    candidate dict: {advertiser_id, name, priority: bool, source_domain}.
    Every skip is logged with a reason, cheapest checks first so we never probe or
    call Haiku for an advertiser a free rule already rejects."""
    passed: list[dict] = []
    skipped: list[dict] = []

    def skip(cand: dict, reason: str, **extra) -> None:
        row = {"advertiser_id": cand.get("advertiser_id"), "name": cand.get("name"),
               "priority": bool(cand.get("priority")), "source_domain": cand.get("source_domain"),
               "reason": reason}
        row.update(extra)
        skipped.append(row)

    for cand in _ordered(candidates):
        name = cand.get("name") or cand.get("advertiser_id") or ""
        # (4) free rejects first -- never spend a probe or a Haiku call on these
        if own_brands.is_own_brand_name(name):
            skip(cand, "own-brand"); continue
        if is_giant_brand(name):
            skip(cand, "giant-brand"); continue
        # (1) cheap ATC probe: size + recency
        try:
            probe = probe_fn(cand["advertiser_id"]) or {}
        except Exception as exc:
            skip(cand, "probe-error", detail=f"{type(exc).__name__}: {exc}"); continue
        ads = probe.get("us_video_ads")
        if not isinstance(ads, int) or ads < min_ads:
            skip(cand, "under-min-ads", us_video_ads=ads); continue
        idle = probe.get("days_since_active")
        if not isinstance(idle, int) or idle > max_idle_days:
            skip(cand, "inactive", days_since_active=idle); continue
        # (2) one cheap Haiku call on the survivors only
        samples = probe.get("samples") or []
        try:
            verdict = classify_fn(name, samples) or {}
        except Exception as exc:
            skip(cand, "screen-error", detail=f"{type(exc).__name__}: {exc}"); continue
        if not verdict.get("english"):
            skip(cand, "non-english", language=verdict.get("language")); continue
        if not verdict.get("fit"):
            skip(cand, "bad-fit", product_type=verdict.get("product_type"),
                 detail=verdict.get("reason")); continue
        passed.append({
            "advertiser_id": cand["advertiser_id"], "name": cand.get("name"),
            "priority": bool(cand.get("priority")), "source_domain": cand.get("source_domain"),
            "us_video_ads": ads, "days_since_active": idle,
            "language": verdict.get("language"), "product_type": verdict.get("product_type"),
            "fit_reason": verdict.get("reason"), "samples": samples,
        })
    return {"passed": passed, "skipped": skipped, "considered": len(candidates),
            "queued_order": [p["advertiser_id"] for p in passed]}


# ---------- the live ATC probe (injected; validated on deploy) ----------

def parse_reported_ads(body_text: str) -> int | None:
    """'~1,120 ads' / '~2.5K ads' on the region=US&format=VIDEO advertiser page -> int."""
    m = re.search(r"~\s*([\d,.]+)\s*([KMB]?)\s+ads", body_text or "", re.I)
    if not m:
        return None
    try:
        value = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return int(value * {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[m.group(2).upper()])


_DATE_FORMATS = ("%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%Y-%m-%d")


def days_since(last_shown_text: str | None, now: datetime | None = None) -> int | None:
    """'Last shown' date string -> whole days since, or None if unparseable."""
    if not last_shown_text:
        return None
    now = now or datetime.now(timezone.utc)
    cleaned = re.sub(r"(?i)^last shown:?\s*", "", last_shown_text).strip()
    for fmt in _DATE_FORMATS:
        try:
            when = datetime.strptime(cleaned, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        return max(0, (now - when).days)
    return None


async def probe_advertiser(page, advertiser_id: str, titles_fn=None,
                           now: datetime | None = None) -> dict:
    """ONE light ATC visit (no full recovery): the US video-ad count, recency from
    the newest creative, and up to SAMPLE_SIZE sample video IDs. titles_fn(ids)->
    {id: title} fills sample titles (e.g. a YouTube videos.list helper); thumbnails
    are derived from the IDs. Reuses collect_all's proven primitives."""
    import collect_all
    url = (f"https://adstransparency.google.com/advertiser/{advertiser_id}"
           "?region=US&format=VIDEO&platform=YOUTUBE&hl=en")
    bodies: list[str] = []

    async def capture(response):
        if "SearchService/SearchCreatives" in response.url:
            try:
                bodies.append(await response.text())
            except Exception:
                pass

    page.on("response", lambda r: asyncio.ensure_future(capture(r)))
    await page.goto(url, wait_until="domcontentloaded", timeout=90_000)
    await page.wait_for_timeout(3_000)
    body = await page.locator("body").inner_text()
    us_video_ads = parse_reported_ads(body)

    # {creative URL: preview content.js URL}, in ATC order (newest first). No scroll:
    # the first SearchCreatives batch (~40 creatives) is ample for a 5-ad sample.
    await page.wait_for_timeout(500)
    targets: dict[str, str] = {}
    for b in bodies:
        targets.update(collect_all.parse_search_creatives(b))
    sampled = dict(list(targets.items())[:SAMPLE_SIZE])
    sample_ids: list[str] = []
    if sampled:
        resolved = await collect_all.resolve_preview_js(page.context, sampled, limit=4)
        for creative_url in sampled:
            for vid in resolved.get(creative_url, []):
                if vid not in sample_ids:
                    sample_ids.append(vid)

    # recency: the newest creative's "Last shown" (ONE detail page, not the whole set)
    days = None
    newest = next(iter(targets), None)
    if newest:
        try:
            detail = await collect_all.extract_one(page.context, newest, 0, asyncio.Semaphore(1))
            days = days_since(detail.get("last_shown"), now)
            for vid in detail.get("video_ids") or []:
                if vid not in sample_ids:
                    sample_ids.append(vid)
        except Exception:
            days = None

    sample_ids = sample_ids[:SAMPLE_SIZE]
    titles = titles_fn(sample_ids) if (titles_fn and sample_ids) else {}
    samples = [{"video_id": v, "title": (titles or {}).get(v),
                "thumbnail_url": f"https://i.ytimg.com/vi/{v}/hqdefault.jpg"} for v in sample_ids]
    return {"advertiser_id": advertiser_id, "us_video_ads": us_video_ads,
            "days_since_active": days, "samples": samples}
