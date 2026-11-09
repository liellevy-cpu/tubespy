"""Nov 2026 Google change: video ads render as a poster (i.ytimg.com/vi/<ID>/) in a SafeFrame.

The detail page also renders a related-ads grid. Only the ad-detail player may supply IDs.
"""
import asyncio
from types import SimpleNamespace

import collect_all


class Element:
    def __init__(self, in_details, href=None):
        self.in_details = in_details
        self.href = href

    async def evaluate(self, script):
        if "creative-preview" in script:
            return self.href
        return self.in_details


class Frame:
    def __init__(self, html, element, children=()):
        self.html = html
        self.element = element
        self.child_frames = list(children)

    async def content(self):
        return self.html

    async def frame_element(self):
        return self.element


def poster(video_id):
    return f'<img src="https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg">'


def test_detail_ids_ignore_related_grid():
    detail = Frame("<div></div>", Element(True), [Frame(poster("AAAAAAAAAAA"), Element(True))])
    related = Frame(poster("BBBBBBBBBBB"), Element(False))
    page = SimpleNamespace(main_frame=SimpleNamespace(child_frames=[detail, related]))
    assert asyncio.run(collect_all.detail_frame_ids(page)) == {"AAAAAAAAAAA"}


def test_grid_harvest_maps_each_card_to_its_own_video():
    base = "https://adstransparency.google.com/advertiser/AR123/creative/"
    cards = [
        Frame("", Element(False, base + "CR1?region=US"), [Frame(poster("AAAAAAAAAAA"), None)]),
        Frame("", Element(False, base + "CR2?region=US"), [Frame(poster("BBBBBBBBBBB"), None)]),
        Frame(poster("CCCCCCCCCCC"), Element(False, None)),  # not a creative card
    ]
    page = SimpleNamespace(main_frame=SimpleNamespace(child_frames=cards))
    result = asyncio.run(collect_all.harvest_grid_videos(page))
    assert sorted(result.values()) == [["AAAAAAAAAAA"], ["BBBBBBBBBBB"]]
    assert all("/creative/CR" in url for url in result)


def test_search_creatives_maps_creative_to_preview_script():
    js = "https://displayads-formats.googleusercontent.com/ads/preview/content.js?client=x&creativeId=1"
    body = ('{"1":[{"1":"AR123","2":"CR9","3":{"1":{"4":"%s"}}},'
            '{"1":"AR123","2":"CR8","3":{"1":{"4":"https://evil.example/content.js"}}}],"2":"tok"}' % js)
    result = collect_all.parse_search_creatives(body)
    assert list(result.values()) == [js]
    (url,) = result
    assert url == collect_all.canonical_creative_url(
        "https://adstransparency.google.com/advertiser/AR123/creative/CR9?region=US&format=VIDEO&platform=YOUTUBE&hl=en")
    assert collect_all.parse_search_creatives("not json") == {}


def test_preview_script_poster_gives_video_id():
    text = 'var a="https:\\/\\/i.ytimg.com\\/vi\\/YWnNaXmZZnc\\/hqdefault.jpg"; b="https://i.ytimg.com/vi/YWnNaXmZZnc/maxresdefault.jpg"'
    assert collect_all.ids_from_preview_js(text) == ["YWnNaXmZZnc"]


def test_webp_poster_path_is_recognised():
    html = "https://i.ytimg.com/vi_webp/_0qYZvOsjvc/maxresdefault.webp"
    assert [m.group(1) for m in collect_all.THUMB_RE.finditer(html)] == ["_0qYZvOsjvc"]


def test_google_withheld_ad_is_recorded_not_failed(monkeypatch, tmp_path):
    import json
    import brand_worker

    async def no_wait(*args):
        pass

    class Page:
        main_frame = SimpleNamespace(child_frames=[])
        frames = []
        def on(self, *args): pass
        async def goto(self, *args, **kwargs): return SimpleNamespace(status=200)
        async def wait_for_timeout(self, *args): pass
        def locator(self, *args): return self
        async def evaluate_all(self, *args): return []
        async def content(self): return "<html></html>"
        async def inner_text(self): return "Last shown: Sep 17, 2026\nSorry, we are unable to show you this variation right now"
        async def close(self): pass

    class Context:
        async def new_page(self): return Page()

    monkeypatch.setattr(collect_all.asyncio, "sleep", no_wait)
    url = "https://adstransparency.google.com/advertiser/AR123/creative/CR1"
    row = asyncio.run(collect_all.extract_one(Context(), url, 0, asyncio.Semaphore(1)))
    assert row["error"] is None and row["video_ids"] == [] and row["withheld"] is True

    ok = {"index": 1, "creative_url": url.replace("CR1", "CR2"), "video_ids": ["AAAAAAAAAAA"],
          "variation_count": 1, "last_shown": None, "error": None}
    hidden = dict(row); hidden.pop("withheld")
    (tmp_path / "creative_video_results.jsonl").write_text(json.dumps(hidden) + "\n" + json.dumps(ok) + "\n")
    # Without the explicit Google-withheld record an empty row is still a failure.
    try:
        brand_worker.require_resolved_creatives(tmp_path, "AR123")
        raise AssertionError("empty unrecorded row must fail")
    except ValueError:
        pass
    (tmp_path / "google_withheld_creatives.json").write_text(json.dumps([url]))
    brand_worker.require_resolved_creatives(tmp_path, "AR123")
