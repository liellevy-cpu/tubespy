#!/usr/bin/env python3
"""Resume-safe multi-advertiser Google Transparency → YouTube collector."""

from __future__ import annotations

import argparse
import asyncio
import collections
import concurrent.futures
import contextlib
import json
import os
import re
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent
RAW = ROOT / "raw"
TQR_SOURCE = Path(os.environ.get("TQR_SOURCE", "/nonexistent/tqr-pilot"))  # original pilot data lives only on the Emater Mac mini
VIDEO_RE = re.compile(r"(?:youtube(?:-nocookie)?\.com/(?:embed/|watch\?(?:[^#]*&)?v=)|youtu\.be/)([A-Za-z0-9_-]{11})", re.I)
ENCODED_VIDEO_RE = re.compile(r"%2Fembed%2F([A-Za-z0-9_-]{11})", re.I)
# Since ~Oct 2026 Google renders video creatives in a SafeFrame "discover_video_ads"
# gadget that shows the YouTube poster image instead of an embed URL. The poster
# path carries the same 11-character YouTube video ID.
THUMB_RE = re.compile(r"i\.ytimg\.com/vi(?:_webp)?/([A-Za-z0-9_-]{11})/", re.I)
FRAME_PATTERNS = (VIDEO_RE, ENCODED_VIDEO_RE, THUMB_RE)
MAX_SCROLL_ROUNDS = 400  # ~40 cards per round; 120 capped big advertisers at ~4,800 ads
WITHHELD_TEXT = "unable to show you this variation"
CREATIVE_RE = re.compile(r"/advertiser/(AR\d+)/creative/(CR\d+)")
ADVERTISER_RE = re.compile(r"/advertiser/(AR\d+)")


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")


def canonical_creative_url(url: str) -> str:
    parsed = urlparse(url)
    match = CREATIVE_RE.search(parsed.path)
    if parsed.scheme != "https" or parsed.hostname != "adstransparency.google.com" or not match:
        raise ValueError(f"unsafe creative URL: {url}")
    query = dict(parse_qsl(parsed.query))
    query.update({"region": query.get("region", "US"), "format": "VIDEO", "platform": "YOUTUBE", "hl": "en"})
    query.pop("preset-date", None)
    query.pop("authuser", None)
    query.pop("query", None)
    return urlunparse(("https", parsed.hostname, parsed.path, "", urlencode(query), ""))


def normalize_source_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "adstransparency.google.com":
        raise ValueError(f"unsafe source URL: {url}")
    query = dict(parse_qsl(parsed.query))
    query.pop("preset-date", None)
    query.pop("authuser", None)
    query.update({"region": query.get("region", "US"), "format": "VIDEO", "platform": "YOUTUBE", "hl": "en"})
    return urlunparse(("https", parsed.hostname, parsed.path, "", urlencode(query), ""))


def read_existing_creatives(path: Path) -> list[str]:
    if not path.exists():
        return []
    value = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(value, list):
        raw = value
    elif isinstance(value, dict) and isinstance(value.get("creative_urls"), list):
        raw = value["creative_urls"]
    elif isinstance(value, dict) and isinstance(value.get("creatives"), list):
        raw = [item.get("url") or item.get("creative_url") or item.get("observed_href") for item in value["creatives"]]
    else:
        return []
    return sorted({canonical_creative_url(item) for item in raw if isinstance(item, str) and CREATIVE_RE.search(item)})


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temp.replace(path)


PREVIEW_JS_RE = re.compile(r"^https://displayads-formats\.googleusercontent\.com/ads/preview/content\.js\?")


def parse_search_creatives(body: str) -> dict[str, str]:
    """SearchCreatives RPC JSON -> {canonical creative URL: preview content.js URL}."""
    out: dict[str, str] = {}
    try:
        data = json.loads(body)
    except ValueError:
        return out
    for item in data.get("1") or []:
        if not isinstance(item, dict):
            continue
        advertiser, creative = item.get("1"), item.get("2")
        js = ((item.get("3") or {}).get("1") or {}).get("4")
        if not (isinstance(advertiser, str) and isinstance(creative, str) and isinstance(js, str) and PREVIEW_JS_RE.match(js)):
            continue
        try:
            url = canonical_creative_url(f"https://adstransparency.google.com/advertiser/{advertiser}/creative/{creative}?region=US&format=VIDEO&platform=YOUTUBE&hl=en")
        except ValueError:
            continue
        out[url] = js
    return out


def ids_from_preview_js(text: str) -> list[str]:
    return sorted({m.group(1) for pattern in FRAME_PATTERNS for m in pattern.finditer(text or "")})


async def resolve_preview_js(context, targets: dict[str, str], limit: int = 4,
                             landing_out: dict[str, str] | None = None) -> dict[str, list[str]]:
    """Fetch each creative's own preview script; it names the YouTube poster/video ID.
    When landing_out is given, also record {creative URL: landing domain} from the same
    preview bundle (the ad's click-through adurl) -- no extra fetch."""
    sem = asyncio.Semaphore(limit)
    out: dict[str, list[str]] = {}

    async def one(url: str, js: str) -> None:
        async with sem:
            for attempt in range(3):
                try:
                    response = await context.request.get(js, timeout=45_000)
                    if response.status == 200:
                        text = await response.text()
                        ids = ids_from_preview_js(text)
                        if ids:
                            out[url] = ids
                        if landing_out is not None:
                            domain = landing_domain_from_text(text)
                            if domain:
                                landing_out[url] = domain
                        return
                    if response.status == 429:
                        await asyncio.sleep(10 * (attempt + 1))
                        continue
                    return
                except Exception:
                    await asyncio.sleep(2 + attempt * 2)

    await asyncio.gather(*(one(url, js) for url, js in targets.items()))
    return out


def landing_domain_from_text(text: str) -> str | None:
    """The ad's registrable click-through (landing) domain from a preview/content
    bundle's adurl, reusing discovery's single extractor so the rule can't drift."""
    from discovery import extract_landing_domain
    return extract_landing_domain(text or "")


async def _frame_tree_ids(frame) -> set[str]:
    """Video IDs inside one ad SafeFrame and its nested gadget frames."""
    ids: set[str] = set()
    stack = [frame]
    while stack:
        current = stack.pop()
        stack.extend(current.child_frames)
        try:
            html = await current.content()
        except Exception:
            continue
        ids.update(match.group(1) for pattern in FRAME_PATTERNS for match in pattern.finditer(html))
    return ids


async def harvest_grid_videos(page) -> dict[str, list[str]]:
    """Map each advertiser-grid creative card to the video IDs rendered in its own SafeFrame."""
    found: dict[str, set[str]] = collections.defaultdict(set)
    for frame in getattr(page, "main_frame", None).child_frames if getattr(page, "main_frame", None) else []:
        try:
            element = await frame.frame_element()
            href = await element.evaluate(
                "f => { const card = f.closest('creative-preview'); const a = card && card.querySelector('a[href*=\"/creative/\"]'); return a ? a.href : null; }"
            )
        except Exception:
            continue
        if not href:
            continue
        try:
            url = canonical_creative_url(href)
        except ValueError:
            continue
        found[url].update(await _frame_tree_ids(frame))
    return {url: sorted(ids) for url, ids in found.items() if ids}


async def detail_frame_ids(page) -> set[str]:
    """Video IDs from the ad-detail player only, never from the related-ads grid."""
    main = getattr(page, "main_frame", None)
    if main is None:
        return set()
    ids: set[str] = set()
    for frame in main.child_frames:
        try:
            element = await frame.frame_element()
            inside = await element.evaluate("f => !!f.closest('creative-details') && !f.closest('creative-grid')")
        except Exception:
            continue
        if inside:
            ids.update(await _frame_tree_ids(frame))
    return ids


async def collect_creative_urls(page, source_url: str) -> tuple[list[str], dict[str, Any]]:
    normalized = normalize_source_url(source_url)
    await page.goto(normalized, wait_until="domcontentloaded", timeout=90_000)
    await page.wait_for_timeout(3_000)
    initial_text = await page.locator("body").inner_text()
    reported_match = re.search(r"~([\d,.]+[KMB]?)\s+ads", initial_text, re.I)
    reported = reported_match.group(1) if reported_match else None
    source_path = urlparse(normalized).path
    targets = [normalized]
    if not ADVERTISER_RE.search(source_path):
        preview_hrefs = await page.locator('a[href*="/creative/"]').evaluate_all("els => els.map(a => a.href)")
        advertiser_ids = sorted({match.group(1) for href in preview_hrefs if (match := CREATIVE_RE.search(href))})
        if not advertiser_ids:
            raise RuntimeError("search result did not expose a matching advertiser ID")
        targets = [
            f"https://adstransparency.google.com/advertiser/{advertiser_id}?region=US&format=VIDEO&platform=YOUTUBE&hl=en"
            for advertiser_id in advertiser_ids
        ]

    collected: set[str] = set()
    resolved_ids: set[str] = set()
    grid_videos: dict[str, set[str]] = {}
    preview_js: dict[str, str] = {}
    pending_bodies: list = []

    async def capture(response):
        if "SearchService/SearchCreatives" in response.url:
            try:
                pending_bodies.append(await response.text())
            except Exception:
                pass

    page.on("response", lambda response: asyncio.ensure_future(capture(response)))
    rounds = 0
    all_stable = True
    for target in targets:
        await page.goto(target, wait_until="domcontentloaded", timeout=90_000)
        await page.wait_for_timeout(3_000)
        stable = 0
        last = -1
        for _ in range(MAX_SCROLL_ROUNDS):
            rounds += 1
            hrefs = await page.locator('a[href*="/creative/"]').evaluate_all("els => els.map(a => a.href)")
            for href in hrefs:
                try:
                    url = canonical_creative_url(href)
                except ValueError:
                    continue
                collected.add(url)
                match = CREATIVE_RE.search(url)
                if match:
                    resolved_ids.add(match.group(1))
            if len(collected) == last:
                stable += 1
            else:
                stable = 0
                last = len(collected)
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            # Google loads ~40 cards per batch; 800 ms stopped early (320 of 1,251 for Coolina).
            await page.wait_for_timeout(1_500)
            if stable >= 12:
                break
        all_stable = all_stable and stable >= 12
        # Video IDs come from the SearchCreatives preview scripts (captured above); the
        # rendered-grid harvest is only a fallback for small advertisers.
        if len(collected) <= 400:
            await page.wait_for_timeout(3_000)
            for url, ids in (await harvest_grid_videos(page)).items():
                grid_videos.setdefault(url, set()).update(ids)
    await page.wait_for_timeout(1_000)
    for body in pending_bodies:
        preview_js.update(parse_search_creatives(body))
    return sorted(collected), {
        "grid_video_ids": {url: sorted(ids) for url, ids in grid_videos.items()},
        "preview_js": preview_js,
        "reported_ads": reported,
        "advertiser_ids": sorted(resolved_ids),
        "resolved_targets": targets,
        "scroll_rounds": rounds,
        "stable": all_stable,
    }


async def extract_one(context, url: str, index: int, sem: asyncio.Semaphore) -> dict[str, Any]:
    async with sem:
        # Google rate-limits creative detail pages aggressively. Serialized runs
        # deliberately trade speed for complete, auditable evidence.
        await asyncio.sleep(1.25)
        last_error = None
        for attempt in range(3):
            page = None
            observed: list[str] = []
            try:
                page = await context.new_page()  # tab-open can fail under load; retry, don't crash the run
                page.on("request", lambda request: observed.append(request.url))
                response = await page.goto(url, wait_until="domcontentloaded", timeout=75_000)
                if response is None or response.status >= 400:
                    raise RuntimeError(f"creative HTTP status {getattr(response, 'status', None)}")
                await page.wait_for_timeout(2_500 + attempt * 1_500)
                detail_ids = await detail_frame_ids(page)
                values = list(observed)
                values.extend(frame.url for frame in page.frames)
                values.extend(await page.locator("*").evaluate_all("els => els.flatMap(e => ['src','href','data-src'].map(a => e.getAttribute && e.getAttribute(a)).filter(Boolean))"))
                values.append(await page.content())
                ids = sorted(detail_ids or {match.group(1) for value in values for pattern in (VIDEO_RE, ENCODED_VIDEO_RE) for match in pattern.finditer(value or "")})
                body = await page.locator("body").inner_text()
                if not ids and WITHHELD_TEXT in body.lower():
                    # Google's own page says it won't show this ad. Recorded, never guessed.
                    last_shown_match = re.search(r"Last shown:?\s+([^\n]+)", body, re.I)
                    return {"index": index, "creative_url": url, "video_ids": [], "variation_count": 0,
                            "last_shown": last_shown_match.group(1).strip() if last_shown_match else None,
                            "error": None, "withheld": True}
                if not ids:
                    raise RuntimeError("No YouTube video evidence resolved from creative detail page")
                last_shown_match = re.search(r"Last shown\s+([^\n]+)", body, re.I)
                landing = landing_domain_from_text("\n".join(v for v in values if v))
                return {
                    "index": index,
                    "creative_url": url,
                    "video_ids": ids,
                    "variation_count": max(1, len(ids)),
                    "last_shown": last_shown_match.group(1).strip() if last_shown_match else None,
                    "landing_domain": landing,
                    "error": None,
                }
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep((20 * (attempt + 1)) if "429" in last_error else (2 + attempt * 2))
            finally:
                if page is not None:
                    await page.close()
        return {"index": index, "creative_url": url, "video_ids": [], "variation_count": 0, "last_shown": None, "error": last_error}


def _youtube_api_key() -> str | None:
    """Our own YouTube Data API key (never logged or published). With
    TUBESPY_NO_SHARED_KEY=1 the ~/.hermes/.env fallback is disabled and a missing
    key is a hard, clear error -- we never silently fall back to a shared / Liel key."""
    import os
    if os.environ.get("YOUTUBE_DATA_API_KEY"):
        return os.environ["YOUTUBE_DATA_API_KEY"]
    if os.environ.get("TUBESPY_NO_SHARED_KEY", "0").strip().lower() in ("1", "true", "yes", "on"):
        raise RuntimeError(
            "YOUTUBE_DATA_API_KEY is not set and the shared-key fallback is disabled "
            "(TUBESPY_NO_SHARED_KEY=1). Set our own YOUTUBE_DATA_API_KEY; never use a shared key.")
    env = Path.home() / ".hermes/.env"
    try:
        for line in env.read_text().splitlines():
            if line.startswith("YOUTUBE_DATA_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'") or None
    except OSError:
        return None
    return None


def _iso_duration_seconds(value: str) -> int | None:
    match = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value or "")
    if not match:
        return None
    days, hours, minutes, seconds = (int(x or 0) for x in match.groups())
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def fetch_youtube_api(pairs: list[tuple[str, list[str]]], key: str) -> dict[str, dict[str, Any]]:
    """Batch metadata via YouTube Data API (1 quota unit per 50 videos). Unlisted videos are included."""
    out: dict[str, dict[str, Any]] = {}
    creatives = dict(pairs)
    ids = list(creatives)
    for start in range(0, len(ids), 50):
        chunk = ids[start:start + 50]
        query = urlencode({"part": "snippet,statistics,contentDetails,status", "id": ",".join(chunk), "key": key, "maxResults": 50})
        payload = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen("https://www.googleapis.com/youtube/v3/videos?" + query, timeout=40) as response:
                    payload = json.load(response)
                break
            except Exception:
                time.sleep(2 + attempt * 3)
        if payload is None:
            continue  # leave for the watch-page fallback
        returned = set()
        for item in payload.get("items", []):
            video_id = item["id"]
            returned.add(video_id)
            stats, snippet, content = item.get("statistics", {}), item.get("snippet", {}), item.get("contentDetails", {})
            urls = sorted(set(creatives[video_id]))
            views = stats.get("viewCount")
            if views is None:
                continue  # hidden view counts fall back to the watch page
            out[video_id] = {
                "video_id": video_id,
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "title": snippet.get("title"),
                "author": snippet.get("channelTitle"),
                "views": int(views),
                "length_seconds": _iso_duration_seconds(content.get("duration", "")),
                "upload_date": snippet.get("publishedAt"),
                "creative_count": len(urls),
                "creative_urls": urls,
                "error": None,
            }
        for video_id in set(chunk) - returned:
            out[video_id] = {"video_id": video_id, "unavailable": {
                "video_id": video_id, "status": "removed",
                "reason": "Not returned by the YouTube API (removed or private)",
                "checked_at": datetime.now(timezone.utc).isoformat()}}
    return out


def fetch_youtube(video_id: str, creatives: list[str]) -> dict[str, Any]:
    last_error = None
    for attempt in range(4):
        try:
            request = urllib.request.Request(
                f"https://www.youtube.com/watch?v={video_id}",
                headers={"User-Agent": "Mozilla/5.0 AppleWebKit/537.36 Chrome/131 Safari/537.36", "Accept-Language": "en-US,en;q=0.9"},
            )
            html = urllib.request.urlopen(request, timeout=40).read().decode("utf-8", "ignore")
            player = None
            for marker in ("ytInitialPlayerResponse = ", "var ytInitialPlayerResponse = "):
                position = html.find(marker)
                if position >= 0:
                    position = html.find("{", position)
                    player = json.JSONDecoder().raw_decode(html[position:])[0]
                    break
            if not player:
                raise ValueError("player response not found")
            playability = player.get("playabilityStatus") or {}
            if not player.get("videoDetails") and playability.get("status") in {"ERROR", "LOGIN_REQUIRED", "UNPLAYABLE"}:
                reason = (playability.get("reason") or playability.get("status") or "").strip()
                status = "private" if ("private" in reason.lower() or playability.get("status") == "LOGIN_REQUIRED") else "removed"
                return {"video_id": video_id, "unavailable": {"video_id": video_id, "status": status, "reason": reason or status,
                        "checked_at": datetime.now(timezone.utc).isoformat()}}
            details = player.get("videoDetails") or {}
            micro = ((player.get("microformat") or {}).get("playerMicroformatRenderer") or {})
            views = details.get("viewCount")
            duration = details.get("lengthSeconds")
            uploaded = micro.get("uploadDate") or micro.get("publishDate")
            title = details.get("title")
            author = details.get("author")
            if not all([views, duration, uploaded, title, author]):
                raise ValueError("required YouTube metadata missing")
            return {
                "video_id": video_id,
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "title": title,
                "author": author,
                "views": int(views),
                "length_seconds": int(duration),
                "upload_date": uploaded,
                "creative_count": len(set(creatives)),
                "creative_urls": sorted(set(creatives)),
                "error": None,
            }
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(1 + attempt)
    return {
        "video_id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "title": None,
        "author": None,
        "views": None,
        "length_seconds": None,
        "upload_date": None,
        "creative_count": len(set(creatives)),
        "creative_urls": sorted(set(creatives)),
        "error": last_error,
    }


@contextlib.contextmanager
def google_lock(label: str = ""):
    """One Google Transparency request at a time on this machine (TubeSpy exports, the
    weekly library worker, discovery, screening and the backfill all share it).
    Serializing scrapes is what avoids Google's "unusual traffic" blocks. The lock path
    is TUBESPY_GOOGLE_LOCK when set, so Henry can point it at the mini-wide shared lock
    and our scrapes wait for Liel's and Sam's too. Cross-platform: fcntl / msvcrt."""
    path = Path(os.environ["TUBESPY_GOOGLE_LOCK"]) if os.environ.get("TUBESPY_GOOGLE_LOCK") else ROOT / "build" / "google-scrape.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        kind = None
        try:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX)
            kind = "fcntl"
        except ModuleNotFoundError:
            import msvcrt
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                kind = "msvcrt"
            except OSError:
                kind = "none"
        try:
            handle.seek(0); handle.truncate()
            handle.write(f"{label} {datetime.now(timezone.utc).isoformat()}\n"); handle.flush()
            yield
        finally:
            if kind == "fcntl":
                import fcntl; fcntl.flock(handle, fcntl.LOCK_UN)
            elif kind == "msvcrt":
                import msvcrt
                try:
                    handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass


def _api_stats_update(out: Path, prior: dict[str, Any], api_key: str | None) -> None:
    """Fetch view counts (YouTube Data API) for already-resolved videos and write
    youtube_stats.jsonl as we go, so a run stopped mid-way still has views on disk.
    API-only and idempotent; the slower watch-page fallback stays in the final pass."""
    if not api_key:
        return
    stats_path = out / "youtube_stats.jsonl"
    unavailable_path = out / "youtube_unavailable.json"
    previous_stats = {row["video_id"]: row for row in load_jsonl(stats_path) if row.get("error") is None}
    unavailable_rows = json.loads(unavailable_path.read_text(encoding="utf-8")) if unavailable_path.exists() else []
    unavailable_ids = {row["video_id"] for row in unavailable_rows}
    by_video: dict[str, list[str]] = collections.defaultdict(list)
    for row in prior.values():
        for video_id in row.get("video_ids", []):
            by_video[video_id].append(row["creative_url"])
    missing = [(video_id, urls) for video_id, urls in sorted(by_video.items())
               if video_id not in previous_stats and video_id not in unavailable_ids]
    if not missing:
        return
    new_unavailable = []
    for video_id, row in fetch_youtube_api(missing, api_key).items():
        if row.get("unavailable"):
            new_unavailable.append(row["unavailable"])
        else:
            previous_stats[video_id] = row
    if new_unavailable:
        unavailable_rows = unavailable_rows + new_unavailable
        write_json(unavailable_path, unavailable_rows)
        unavailable_ids |= {row["video_id"] for row in new_unavailable}
    stats = [previous_stats[v] for v in sorted(by_video) if v in previous_stats and v not in unavailable_ids]
    stats_path.write_text("".join(json.dumps(value, ensure_ascii=False) + "\n" for value in stats), encoding="utf-8")


async def collect_advertiser(browser, item: dict[str, Any], concurrency: int) -> dict[str, Any]:
    name = item["name"]
    slug = slugify(name)
    out = RAW / slug
    out.mkdir(parents=True, exist_ok=True)
    if name == "TQR Marketing Group" and TQR_SOURCE.exists():
        return {"name": name, "slug": slug, "status": "preserved_pilot", "source_dir": str(TQR_SOURCE)}

    context = await browser.new_context(locale="en-US")
    page = await context.new_page()
    existing_urls = read_existing_creatives(out / "creative_urls.json")
    discovery = None
    if existing_urls:
        urls = existing_urls
        discovery = {"reused_existing": True}
        # Normalize heterogeneous interrupted-batch artifacts into the canonical list contract.
        write_json(out / "creative_urls.json", urls)
    else:
        urls, discovery = await collect_creative_urls(page, item["transparency_url"])
        write_json(out / "creative_urls.json", urls)
    await page.close()
    if not urls:
        await context.close()
        result = {"name": name, "slug": slug, "status": "blocked", "reason": "no creative URLs", "discovery": discovery}
        write_json(out / "summary.json", result)
        return result

    results_path = out / "creative_video_results.jsonl"
    prior = {row["creative_url"]: {k: v for k, v in row.items() if k != "source"} for row in load_jsonl(results_path) if row.get("error") is None}
    grid = (discovery or {}).get("grid_video_ids") or {}
    url_set = set(urls)
    for url, ids in grid.items():
        if url in url_set and url not in prior and ids:
            prior[url] = {"index": 0, "creative_url": url, "video_ids": list(ids), "variation_count": len(ids),
                          "last_shown": None, "error": None}
    previews = {url: js for url, js in ((discovery or {}).get("preview_js") or {}).items() if url in url_set and url not in prior}
    if previews:
        landing_by_url: dict[str, str] = {}
        for url, ids in (await resolve_preview_js(context, previews, landing_out=landing_by_url)).items():
            prior[url] = {"index": 0, "creative_url": url, "video_ids": ids, "variation_count": len(ids),
                          "last_shown": None, "landing_domain": landing_by_url.get(url), "error": None}
    if grid or previews:
        print(json.dumps({"advertiser": name, "fast_resolved": f"{sum(1 for u in urls if u in prior)}/{len(urls)}"}), flush=True)
    pending = [(index, url) for index, url in enumerate(urls) if url not in prior]
    sem = asyncio.Semaphore(concurrency)
    tasks = [asyncio.create_task(extract_one(context, url, index, sem)) for index, url in pending]
    api_key = _youtube_api_key()
    for completed, task in enumerate(asyncio.as_completed(tasks), 1):
        row = await task
        prior[row["creative_url"]] = row
        if completed % 20 == 0 or completed == len(tasks):
            ordered = []
            for index, url in enumerate(urls):
                if url in prior:
                    value = dict(prior[url]); value["index"] = index; ordered.append(value)
            results_path.write_text("".join(json.dumps(value, ensure_ascii=False) + "\n" for value in ordered), encoding="utf-8")
            _api_stats_update(out, prior, api_key)  # fetch views in batches as we go
            print(json.dumps({"advertiser": name, "creative_progress": f"{len(prior)}/{len(urls)}"}), flush=True)
    await context.close()
    if not tasks:
        ordered = [dict(prior[url], index=index) for index, url in enumerate(urls) if url in prior]
        results_path.write_text("".join(json.dumps(value, ensure_ascii=False) + "\n" for value in ordered))

    rows = load_jsonl(results_path)
    withheld = sorted(row["creative_url"] for row in rows if row.get("withheld"))
    if withheld:
        rows = [{k: v for k, v in row.items() if k != "withheld"} for row in rows]
        results_path.write_text("".join(json.dumps(value, ensure_ascii=False) + "\n" for value in rows), encoding="utf-8")
        write_json(out / "google_withheld_creatives.json", withheld)
    failures = [row for row in rows if row.get("error")]
    by_video: dict[str, list[str]] = collections.defaultdict(list)
    for row in rows:
        for video_id in row.get("video_ids", []):
            by_video[video_id].append(row["creative_url"])
    unavailable_path = out / "youtube_unavailable.json"
    unavailable_rows = json.loads(unavailable_path.read_text(encoding="utf-8")) if unavailable_path.exists() else []
    unavailable_ids = {row["video_id"] for row in unavailable_rows}
    stats_path = out / "youtube_stats.jsonl"
    previous_stats = {row["video_id"]: row for row in load_jsonl(stats_path) if row.get("error") is None}
    missing = [
        (video_id, urls)
        for video_id, urls in sorted(by_video.items())
        if video_id not in previous_stats and video_id not in unavailable_ids
    ]
    newly_unavailable = []
    api_key = _youtube_api_key()
    if api_key and missing:
        api_rows = fetch_youtube_api(missing, api_key)
        for video_id, row in api_rows.items():
            if row.get("unavailable"):
                newly_unavailable.append(row["unavailable"])
            else:
                previous_stats[video_id] = row
        missing = [pair for pair in missing if pair[0] not in api_rows]
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        for row in executor.map(lambda pair: fetch_youtube(*pair), missing):
            if row.get("unavailable"):
                newly_unavailable.append(row["unavailable"])
                continue
            previous_stats[row["video_id"]] = row
    if newly_unavailable:
        unavailable_rows = unavailable_rows + newly_unavailable
        unavailable_ids |= {row["video_id"] for row in newly_unavailable}
        write_json(unavailable_path, unavailable_rows)
    stats = [previous_stats[video_id] for video_id in sorted(by_video) if video_id in previous_stats and video_id not in unavailable_ids]
    stats_path.write_text("".join(json.dumps(value, ensure_ascii=False) + "\n" for value in stats), encoding="utf-8")
    metadata_failures = [row for row in stats if row.get("error")]
    advertiser_ids = sorted({CREATIVE_RE.search(url).group(1) for url in urls})
    summary = {
        "name": name,
        "slug": slug,
        "source_url": normalize_source_url(item["transparency_url"]),
        "advertiser_ids": advertiser_ids,
        "creative_count": len(urls),
        "creative_failures": len(failures),
        "google_withheld_creatives": len(withheld),
        "unique_video_ids": len(by_video),
        "youtube_metadata_failures": len(metadata_failures),
        "unavailable_youtube_videos": len(unavailable_ids),
        "discovery": discovery,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "status": "complete" if not failures and not metadata_failures and by_video else "blocked",
    }
    write_json(out / "summary.json", summary)
    return summary


async def run(args) -> int:
    registry = json.loads(Path(args.registry).read_text())
    requested = set(args.slug or [])
    items = [item for item in registry["advertisers"] if not requested or slugify(item["name"]) in requested]
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            executable_path=os.environ.get("CHROME_PATH", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        )
        results = []
        for item in items:
            print(json.dumps({"starting": item["name"]}), flush=True)
            try:
                results.append(await collect_advertiser(browser, item, args.concurrency))
            except Exception as exc:
                result = {"name": item["name"], "slug": slugify(item["name"]), "status": "blocked", "reason": f"{type(exc).__name__}: {exc}"}
                write_json(RAW / result["slug"] / "summary.json", result)
                results.append(result)
            print(json.dumps(results[-1], ensure_ascii=False), flush=True)
        await browser.close()
    print(json.dumps({"advertisers": len(results), "complete": sum(r.get("status") in {"complete", "preserved_pilot"} for r in results), "blocked": [r for r in results if r.get("status") == "blocked"]}, ensure_ascii=False), flush=True)
    return 0 if all(r.get("status") in {"complete", "preserved_pilot"} for r in results) else 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", default=str(ROOT / "registry.json"))
    parser.add_argument("--slug", action="append", help="Collect only this slug; repeatable")
    parser.add_argument("--concurrency", type=int, default=5)
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
