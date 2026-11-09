#!/usr/bin/env python3
"""Confirm a YouTube video's spoken language is English, from the video itself.

Mandatory English gate for the library. Never uses the title. Signal priority:
  1. videoDetails.defaultAudioLanguage        (explicit original audio language)
  2. default audio track language              (auto-dubbed multi-audio videos)
  3. the single ASR caption track              (YouTube's detected speech language)
  4. transcript content check via langdetect   (fallback for ambiguous multi-track)

confirm_english(video_id) -> True / False / None (unconfirmed). Callers EXCLUDE
anything that is not True. Results cache to build/lang_cache.json.
"""
from __future__ import annotations

import json
import re
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CACHE_PATH = ROOT / "build" / "lang_cache.json"
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

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


def is_en(code) -> bool:
    return bool(code) and str(code).strip().lower().split(".")[0].split("-")[0] == "en"


def _player(video_id: str) -> dict | None:
    req = urllib.request.Request("https://www.youtube.com/watch?v=" + video_id, headers=UA)
    html = urllib.request.urlopen(req, timeout=40).read().decode("utf-8", "ignore")
    for marker in ("ytInitialPlayerResponse = ", "var ytInitialPlayerResponse = "):
        i = html.find(marker)
        if i >= 0:
            i = html.find("{", i)
            return json.JSONDecoder().raw_decode(html[i:])[0]
    return None


def _caption_text(base_url: str, limit: int = 1200) -> str:
    url = base_url + ("&" if "?" in base_url else "?") + "fmt=json3"
    try:
        data = json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=40))
        parts = [seg.get("utf8", "") for ev in data.get("events", []) for seg in (ev.get("segs") or [])]
        return re.sub(r"\s+", " ", "".join(parts)).strip()[:limit]
    except Exception:
        return ""


def _detect(video_id: str) -> dict:
    try:
        p = _player(video_id)
    except Exception as exc:
        return {"english": None, "lang": None, "method": f"fetch_error:{type(exc).__name__}"}
    if not p:
        return {"english": None, "lang": None, "method": "no_player"}
    dal = p.get("videoDetails", {}).get("defaultAudioLanguage")
    if dal:
        return {"english": is_en(dal), "lang": dal, "method": "defaultAudioLanguage"}
    renderer = p.get("captions", {}).get("playerCaptionsTracklistRenderer", {})
    tracks = renderer.get("captionTracks", [])
    audio = renderer.get("audioTracks", [])
    default_index = renderer.get("defaultAudioTrackIndex")
    if audio and isinstance(default_index, int) and 0 <= default_index < len(audio):
        lang = (audio[default_index].get("audioTrackId") or "").split(".")[0]
        if lang:
            return {"english": is_en(lang), "lang": lang, "method": "defaultAudioTrack"}
    asr = [t for t in tracks if t.get("kind") == "asr"]
    if len(tracks) == 1:
        return {"english": is_en(tracks[0].get("languageCode")), "lang": tracks[0].get("languageCode"), "method": "single_caption"}
    if len(asr) == 1:
        return {"english": is_en(asr[0].get("languageCode")), "lang": asr[0].get("languageCode"), "method": "single_asr"}
    if tracks:
        pick = next((t for t in tracks if is_en(t.get("languageCode"))), tracks[0])
        text = _caption_text(pick.get("baseUrl", ""))
        if len(text) >= 40:
            try:
                from langdetect import DetectorFactory, detect
                DetectorFactory.seed = 0
                lang = detect(text)
                return {"english": lang == "en", "lang": lang, "method": "transcript_langdetect"}
            except Exception:
                pass
        return {"english": None, "lang": None, "method": "ambiguous_no_text"}
    return {"english": None, "lang": None, "method": "no_captions"}


def detect_language(video_id: str, throttle: float = 0.6) -> dict:
    cache = _load_cache()
    if video_id in cache:
        return cache[video_id]
    result = _detect(video_id)
    result["video_id"] = video_id
    cache[video_id] = result
    _save_cache()
    if throttle:
        time.sleep(throttle)
    return result


def confirm_english(video_id: str) -> bool | None:
    """True if the video's spoken language is confirmed English, else False/None."""
    return detect_language(video_id).get("english")


if __name__ == "__main__":
    import sys
    from collections import Counter

    ids: list[str] = []
    for arg in sys.argv[1:]:
        p = Path(arg)
        ids += [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()] if p.exists() else [arg]
    eng, methods = Counter(), Counter()
    for vid in ids:
        r = detect_language(vid)
        eng[r["english"]] += 1
        methods[r["method"]] += 1
        print(f"{vid}  english={r['english']}  lang={r['lang']}  ({r['method']})")
    print("\nenglish:", dict(eng), "\nmethods:", dict(methods))
