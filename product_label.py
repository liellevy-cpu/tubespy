#!/usr/bin/env python3
"""AI product labels: title + thumbnail -> Claude -> a short product label.

Used to group variants of the same product in the library. Uses the official
Anthropic SDK; model is claude-haiku-5-5 by default (cheap and ample for this
simple labeling task; override with ANTHROPIC_MODEL, e.g. claude-opus-5-5 for
higher quality). The API key is read from the
environment or from the user's .env; when no key is available label_product returns
None so the build still runs (callers fall back to a title-derived label).
Results cache to build/product_labels.json.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CACHE_PATH = ROOT / "build" / "product_labels.json"
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-5-5")
ENV_CANDIDATES = (
    ROOT / ".env",
    Path(r"C:\Users\user\youtube-stats\.env"),
    Path.home() / "youtube-stats" / ".env",
)

PROMPT = (
    "You label the product advertised in a YouTube video ad, for a competitor "
    "swipe library. Use the thumbnail image and the title. Reply with ONLY a short "
    "product label of 2-5 words naming the physical product or offer — e.g. "
    "'Car Vacuum', 'Blue-Light Reading Glasses', 'Fig Energy Bites', 'In-Car HUD'. "
    "Ignore prices, discounts, promo words, brand names, and the language of the "
    "title (many titles are codes or non-English). If you truly cannot tell, reply "
    "'Unknown'. Title: {title}"
)

_cache: dict | None = None


def _load_cache() -> dict:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            _cache = {}
    return _cache


def _save_cache() -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(_cache, ensure_ascii=False), encoding="utf-8")
    tmp.replace(CACHE_PATH)


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


def _clean(label: str) -> str:
    label = label.strip().strip('"').splitlines()[0].strip()
    return re.sub(r"\s+", " ", label)[:60]


def label_product(video_id: str, title: str, thumbnail_url: str) -> str | None:
    """Short product label for one video, or None if the API key is unavailable.
    Cached by video_id. 'Unknown' is returned (and cached) when Claude can't tell."""
    cache = _load_cache()
    if video_id in cache:
        return cache[video_id]
    key = _api_key()
    if not key:
        return None  # key not set yet; caller falls back to a title-derived label
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        message = client.messages.create(
            model=MODEL,
            max_tokens=32,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "url", "url": thumbnail_url}},
                    {"type": "text", "text": PROMPT.format(title=title)},
                ],
            }],
        )
        text = next((b.text for b in message.content if b.type == "text"), "")
        label = _clean(text) or "Unknown"
    except Exception:
        return None  # transient API/network error: let the caller use the fallback
    cache[video_id] = label
    _save_cache()
    return label
