#!/usr/bin/env python3
"""Confirm a YouTube video's spoken language is English, from the video itself.

Mandatory English gate for the library. Never uses the title or the uploader's
`defaultAudioLanguage` as a standalone verdict -- that metadata is routinely set to
"en" on non-English dropship ads and must NEVER be enough on its own. We trust the
video's own detected speech instead, and when the signals conflict or are missing we
EXCLUDE the video rather than guess.

Signal priority (most reliable first):
  1. ASR caption language          -- YouTube's auto-generated speech detection.
       * one ASR track              -> that is the spoken language.
       * several ASR tracks (one per auto-dub) -> the ORIGINAL is the ASR whose
         language matches the default audio track; that anchors which speech is real.
  2. default audio track language  -- the source language of an auto-dubbed video,
       used only to disambiguate / when ASR is absent on a multi-audio video.
  3. transcript content check (langdetect) -- fallback when captions are ambiguous,
       and the confirmer whenever a speech signal conflicts with the metadata.
  `defaultAudioLanguage` is used ONLY to flag a conflict (never to pass a video).

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


def _base(code) -> str | None:
    """Primary subtag, lowercased: 'en-US.10' -> 'en', 'es' -> 'es', None -> None."""
    if not code:
        return None
    return str(code).strip().lower().split(".")[0].split("-")[0] or None


def is_en(code) -> bool:
    return _base(code) == "en"


def _result(english, lang, method) -> dict:
    return {"english": english, "lang": lang, "method": method}


MIN_TRANSCRIPT_CHARS = 40


def _langdetect(text: str) -> str | None:
    """Primary language of caption text via langdetect, or None if too little text /
    unavailable. Returns a base code like 'en' / 'es'."""
    if not text or len(text) < MIN_TRANSCRIPT_CHARS:
        return None
    try:
        from langdetect import DetectorFactory, detect
        DetectorFactory.seed = 0
        return _base(detect(text))
    except Exception:
        return None


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


def _en_track(tracks: list) -> dict | None:
    """The English caption track if present (prefer a real one over ASR)."""
    real = [t for t in tracks if is_en(t.get("languageCode")) and t.get("kind") != "asr"]
    asr = [t for t in tracks if is_en(t.get("languageCode"))]
    return (real or asr or [None])[0]


def _confirm_by_transcript(tracks: list, speech_lang, meta) -> dict:
    """A speech signal said English but the metadata names a different real language.
    Settle it with the actual caption text; exclude if the text can't confirm it."""
    track = _en_track(tracks)
    td = _langdetect(_caption_text(track.get("baseUrl", ""))) if track else None
    if td == "en":
        return _result(True, "en", "asr_confirmed_by_transcript")
    if td and td != "en":
        return _result(False, td, "transcript_overrides_metadata")
    return _result(None, None, "conflict_speech_vs_metadata")   # unresolved -> exclude


def _detect(video_id: str) -> dict:
    try:
        p = _player(video_id)
    except Exception as exc:
        return _result(None, None, f"fetch_error:{type(exc).__name__}")
    if not p:
        return _result(None, None, "no_player")

    # Uploader metadata: kept ONLY to flag a conflict, never to pass a video on its own.
    meta = p.get("videoDetails", {}).get("defaultAudioLanguage")
    renderer = p.get("captions", {}).get("playerCaptionsTracklistRenderer", {})
    tracks = renderer.get("captionTracks", [])
    audio = renderer.get("audioTracks", [])
    idx = renderer.get("defaultAudioTrackIndex")
    asr = [t for t in tracks if t.get("kind") == "asr"]

    # Original/default audio track language (auto-dub aware source language).
    audio_lang = None
    if audio and isinstance(idx, int) and 0 <= idx < len(audio):
        audio_lang = (audio[idx].get("audioTrackId") or "").split(".")[0] or None

    # ---- 1. ASR caption language = YouTube's detected speech (trusted first) ----
    speech_lang = speech_method = None
    if len(asr) == 1:
        speech_lang, speech_method = asr[0].get("languageCode"), "asr_caption"
    elif asr and audio_lang:
        # several ASR (one per dub): the ORIGINAL is the ASR matching the default audio.
        match = next((t for t in asr if _base(t.get("languageCode")) == _base(audio_lang)), None)
        speech_lang = (match or {}).get("languageCode") or audio_lang
        speech_method = "asr_matches_default_audio" if match else "default_audio_track"
    elif audio_lang and len(audio) >= 2:
        # auto-dubbed but ASR absent/odd -> the default (original) audio track is the source.
        speech_lang, speech_method = audio_lang, "default_audio_track"

    if speech_lang is not None:
        english = is_en(speech_lang)
        # Conflict guard: speech says English but metadata names a *different real* language.
        if english and meta and not is_en(meta):
            return _confirm_by_transcript(tracks, speech_lang, meta)
        return _result(english, speech_lang, speech_method)

    # ---- 2. A single manually-provided caption + transcript corroboration ----
    if len(tracks) == 1:
        lang = tracks[0].get("languageCode")
        td = _langdetect(_caption_text(tracks[0].get("baseUrl", "")))
        if td and _base(lang) and td != _base(lang):
            return _result(None, None, "conflict_caption_vs_transcript")   # exclude
        if is_en(lang) and td in (None, "en"):
            return _result(True, lang, "single_caption")
        if not is_en(lang):
            return _result(False, lang, "single_caption")
        return _result(None, lang, "single_caption_unverified")

    # ---- 3. Transcript langdetect fallback when captions are ambiguous ----
    if tracks:
        track = _en_track(tracks) or tracks[0]
        td = _langdetect(_caption_text(track.get("baseUrl", "")))
        if td == "en":
            if meta and not is_en(meta):
                return _result(None, None, "conflict_transcript_vs_metadata")   # exclude
            return _result(True, "en", "transcript_langdetect")
        if td:
            return _result(False, td, "transcript_langdetect")
        return _result(None, None, "ambiguous_no_text")

    # ---- No speech evidence at all. Metadata alone is NOT enough -> exclude. ----
    return _result(None, None, "insufficient_signal")


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
