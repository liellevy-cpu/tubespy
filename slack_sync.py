#!/usr/bin/env python3
"""Slack -> "Used" sync (PREPARED, NOT CONNECTED).

Rule: every YouTube video link posted in #sam-coolina-youtube-swipes marks that
video Used (it moves to the Used tab and never gets suggested again). Advertiser /
Transparency links (adstransparency.google.com) are NOT Used — they're ignored.

This module is fully built and unit-tested, but it does NOT open any Slack
connection — `fetch_messages` / the HTTP client are injected, so nothing talks to
Slack until Henry wires it on the Mini.

WHAT IT NEEDS (to give Henry):
  - A Slack app in the Coolina workspace with a BOT token `xoxb-…`.
  - Bot scopes: `channels:history` + `channels:read` for a public channel, or
    `groups:history` + `groups:read` if #sam-coolina-youtube-swipes is private
    (invite the bot to the channel either way).
  - The channel ID (e.g. C0123ABCD) — resolve via conversations.list or the
    channel URL.
  - Delivery, pick one:
      * Polling: call conversations.history on a schedule (bot token only).
      * Real-time: Events API subscription `message.channels` (public request URL),
        or Socket Mode with an app-level token `xapp-…` + `connections:write`.
  Store the token as an env var / untracked secret on the Mini — never commit it.
"""
from __future__ import annotations

import json
import re
import urllib.request

# YouTube video links only — NOT adstransparency.google.com (those stay "not Used").
YT_RE = re.compile(
    r"(?:youtube(?:-nocookie)?\.com/(?:watch\?(?:[^ \n]*&)?v=|embed/|shorts/|live/|v/)|youtu\.be/)"
    r"([A-Za-z0-9_-]{11})", re.I)


def youtube_ids_in_text(text: str) -> list[str]:
    """YouTube video IDs in a message. Transparency/advertiser links yield none."""
    out, seen = [], set()
    for m in YT_RE.finditer(text or ""):
        vid = m.group(1)
        if vid not in seen:
            seen.add(vid)
            out.append(vid)
    return out


def mark_used_from_messages(store, messages) -> dict:
    """Mark Used every YouTube video linked in the given message texts.
    `store` is the add_brand.Store (uses set_item_state). Returns a small summary."""
    marked = []
    for text in messages:
        for vid in youtube_ids_in_text(text):
            try:
                store.set_item_state(vid, "used")
                marked.append(vid)
            except Exception:
                continue
    return {"messages": len(messages), "marked_used": sorted(set(marked))}


def fetch_channel_messages(token: str, channel_id: str, limit: int = 200, opener=None) -> list[str]:
    """Poll conversations.history for a channel's message texts. Built but only
    called once Henry supplies the token + channel ID (nothing calls it here)."""
    opener = opener or urllib.request.urlopen
    url = f"https://slack.com/api/conversations.history?channel={channel_id}&limit={int(limit)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    data = json.load(opener(req, timeout=30))
    if not data.get("ok"):
        raise RuntimeError(f"Slack error: {data.get('error')}")
    return [m.get("text", "") for m in data.get("messages", [])]
