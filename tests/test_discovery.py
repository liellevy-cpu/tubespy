"""Snowball discovery: not-Sunday gate, dedup, sequential one-by-one queuing."""
from datetime import datetime

from discovery import (
    is_discovery_day, domain_of, landing_domains, snowball, snowball_sources,
    priority_domains, queue_new_advertisers, run_weekly_discovery, extract_landing_domain,
)


def test_extract_landing_domain_from_adurl_bundle():
    # shape seen live in the content.js preview bundle
    js = r"...google_td_ad_signals:0,adurl\x3dhttps%3A%2F%2Farborahaven.com%2Fproducts%2F16072\x27,vis..."
    assert extract_landing_domain(js) == "arborahaven.com"


def test_extract_landing_domain_filters_noise_and_missing():
    assert extract_landing_domain(r"adurl\x3dhttps%3A%2F%2Fd3.cloudfront.net%2Fx") is None
    assert extract_landing_domain("no adurl in here") is None


def test_discovery_never_runs_on_sunday():
    assert is_discovery_day(datetime(2026, 11, 8)) is False  # Sunday
    assert is_discovery_day(datetime(2026, 11, 9)) is True   # Monday


def test_domain_of_registrable():
    assert domain_of("https://www.derila.com/offer?x=1") == "derila.com"
    assert domain_of("shop.foo.co.uk".replace("co.uk", "com")) == "foo.com"
    assert domain_of("not a url") is None


def test_landing_domains_from_videos():
    dataset = {"videos": [
        {"landing_domain": "https://buyvacuum.com/x"},
        {"landing_domain": "buyvacuum.com"},
        {"landing_domain": "https://figbites.io"},
        {},  # no landing domain -> skipped, never guessed
    ]}
    assert landing_domains(dataset) == ["buyvacuum.com", "figbites.io"]


def test_snowball_dedupes_against_known_and_itself():
    lookups = {
        "a.com": ["AR111", "AR222", "AR111"],
        "b.com": ["AR222", "AR333", "/advertiser/AR444"],
    }
    out = snowball(["a.com", "b.com"], known_advertiser_ids={"AR222"}, lookup_fn=lookups.get)
    assert out == ["AR111", "AR333", "AR444"]  # AR222 known, dupes removed, URL parsed


class _Store:
    def __init__(self):
        self.calls = []

    def submit(self, payload, visitor, trusted=False):
        if payload["name"] == "boom":
            raise ValueError("already exists")
        self.calls.append((payload["name"], payload["url"], visitor, trusted))
        return {}, True


def test_queue_new_advertisers_one_by_one_and_skips_errors():
    store = _Store()
    queued = queue_new_advertisers(store, ["AR111", "AR222"],
                                   name_fn=lambda a: "boom" if a == "AR111" else a)
    assert queued == ["AR222"]                 # AR111 raised -> skipped, stayed sequential
    assert len(store.calls) == 1
    assert store.calls[0][0] == "AR222"
    assert store.calls[0][3] is True           # trusted submit


def test_run_weekly_discovery_end_to_end():
    store = _Store()
    library = {"advertisers": [
        {"advertiser": {"google_advertiser_id": "AR001"},
         "videos": [{"landing_domain": "https://seed.com/x"}]},
    ]}
    lookup = {"seed.com": ["AR001", "AR999"]}  # AR001 already known
    result = run_weekly_discovery(store, library, lookup.get, when=datetime(2026, 11, 9))
    assert result["ran"] is True
    assert result["queued"] == ["AR999"]

    sunday = run_weekly_discovery(store, library, lookup.get, when=datetime(2026, 11, 8))
    assert sunday["ran"] is False


def test_snowball_sources_keeps_first_seen_domain():
    lookups = {"a.com": ["AR111", "AR222"], "b.com": ["AR222", "AR333"]}
    out = snowball_sources(["a.com", "b.com"], known_advertiser_ids=set(), lookup_fn=lookups.get)
    assert out == [("AR111", "a.com"), ("AR222", "a.com"), ("AR333", "b.com")]


def test_priority_domains_from_used_and_suggested_videos():
    library = {"advertisers": [{"videos": [
        {"video_id": "used1", "landing_domain": "https://hotdeal.com/x"},
        {"video_id": "sugg1", "landing_domain": "gadgetz.com"},
        {"video_id": "cold1", "landing_domain": "boring.com"},
        {"video_id": "own1", "landing_domain": "coolinastore.com"},  # our own -> never a seed
    ]}]}
    assert priority_domains(library, {"used1", "sugg1", "own1"}) == {"hotdeal.com", "gadgetz.com"}


def test_run_weekly_discovery_screens_before_queue():
    store = _Store()
    library = {"advertisers": [
        {"advertiser": {"google_advertiser_id": "AR001"},
         "videos": [{"video_id": "u", "landing_domain": "https://seed.com/x"}]},
    ]}
    lookup = {"seed.com": ["AR_good", "AR_bad"]}

    def screen_fn(candidates):
        # the hot domain (from Used video 'u') must be flagged priority
        assert all(c["priority"] for c in candidates)
        passed = [c for c in candidates if c["advertiser_id"] == "AR_good"]
        skipped = [{"advertiser_id": "AR_bad", "reason": "bad-fit"}]
        return {"passed": [{"advertiser_id": "AR_good", "name": "AR_good"}],
                "skipped": skipped, "considered": len(candidates),
                "queued_order": ["AR_good"]}

    result = run_weekly_discovery(store, library, lookup.get, when=datetime(2026, 11, 9),
                                  screen_fn=screen_fn, priority_video_ids={"u"})
    assert result["queued"] == ["AR_good"]       # only the screened survivor is queued
    assert result["passed"] == 1
    assert result["skipped"] == [{"advertiser_id": "AR_bad", "reason": "bad-fit"}]
    assert [c[0] for c in store.calls] == ["AR_good"]
