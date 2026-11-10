"""Slack sync: YouTube links in the channel mark Used; Transparency links don't."""
import slack_sync


def test_youtube_ids_extracted_transparency_ignored():
    text = ("new swipe https://youtu.be/dQw4w9WgXcQ and "
            "https://www.youtube.com/watch?v=abcdefghijk&t=5 — source "
            "https://adstransparency.google.com/advertiser/AR123/creative/CR456")
    ids = slack_sync.youtube_ids_in_text(text)
    assert ids == ["dQw4w9WgXcQ", "abcdefghijk"]  # transparency link contributes nothing


def test_shorts_and_embed_forms():
    assert slack_sync.youtube_ids_in_text("https://youtube.com/shorts/ABCDEFGHIJK") == ["ABCDEFGHIJK"]
    assert slack_sync.youtube_ids_in_text("x https://www.youtube.com/embed/KJIHGFEDCBA y") == ["KJIHGFEDCBA"]


class _Store:
    def __init__(self):
        self.state = {}

    def set_item_state(self, video_id, state):
        self.state[video_id] = state


def test_mark_used_from_messages():
    store = _Store()
    messages = [
        "check this https://youtu.be/dQw4w9WgXcQ",
        "and https://www.youtube.com/watch?v=abcdefghijk",
        "just an advertiser link https://adstransparency.google.com/advertiser/AR1 (not used)",
        "no links here",
    ]
    summary = slack_sync.mark_used_from_messages(store, messages)
    assert summary["marked_used"] == ["abcdefghijk", "dQw4w9WgXcQ"]
    assert store.state == {"dQw4w9WgXcQ": "used", "abcdefghijk": "used"}
