"""Offline contract tests: these do not prove vendor coverage."""
import importlib.util
import json
from pathlib import Path
import pytest

MODULE = Path(__file__).resolve().parents[1] / "searchapi_pilot.py"

def pilot():
    assert MODULE.exists(), "isolated pilot has not been implemented"
    spec = importlib.util.spec_from_file_location("searchapi_pilot", MODULE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

A = "AR17828074650563772417"
B = "AR15438241099155505153"
C = "CR14400445986759507969"
D = "CR00024466410050682881"
V = "Y7TWpkZnebY"
W = "EOrRr3ylwUI"

def details(**overrides):
    return {"search_parameters": {"advertiser_id": A, "creative_id": C},
            "variations": [{"video_id": V}, {"video_link": f"https://www.youtube.com/watch?v={V}"},
                           {"video_link": f"https://www.youtube.com/embed/{W}"}], **overrides}

def test_variations_preserve_mapping_and_dedupe_video_ids():
    p = pilot()
    result = p.resolve_details(details(), A, C)
    assert [r["video_id"] for r in result["mappings"]] == [V, V, W]
    assert [r["variation_index"] for r in result["mappings"]] == [0, 1, 2]
    assert all(r["advertiser_id"] == A and r["creative_id"] == C for r in result["mappings"])
    assert result["video_ids"] == [V, W]

@pytest.mark.parametrize("variation", [None, {}, {"video_id": "bad"},
    {"video_link": f"https://rr.googlevideo.com/videoplayback?id={V}"},
    {"video_link": f"https://youtube.com.evil.example/watch?v={V}"},
    {"link": f"https://www.youtube.com/watch?v={V}"},
    {"video_id": V, "video_link": f"https://www.youtube.com/watch?v={W}"},
    {"video_link": f"https://www.youtube.com/watch?v={V}&v={W}"}])
def test_bad_variations_do_not_discard_good_siblings(variation):
    result = pilot().resolve_details(details(variations=[variation, {"video_id": V}]), A, C)
    assert result["video_ids"] == [V]
    assert len(result["mappings"]) == 1
    assert result["unresolved_variations"] == 1

@pytest.mark.parametrize("payload", [None, [], {"variations": {}},
    details(search_parameters={"advertiser_id": B, "creative_id": C}),
    details(search_parameters={"advertiser_id": A, "creative_id": D}),
    details(advertiser_id=B), details(creative_id=D)])
def test_malformed_or_cross_identity_details_rejected(payload):
    p = pilot()
    with pytest.raises(p.PilotError):
        p.resolve_details(payload, A, C)

def test_documented_details_without_echo_bound_to_exact_request():
    result = pilot().resolve_details({"variations": [{"video_id": V}]}, A, C)
    assert result["mappings"][0]["identity_basis"] == "exact_request"

def test_listing_requires_exact_advertiser_and_creative_identity():
    p = pilot()
    rows = [{"id": C, "advertiser": {"id": A}}, {"id": D, "advertiser": {"id": B}},
            {"id": "bad", "advertiser": {"id": A}}, None,
            {"id": D, "advertiser": {"id": A}, "details_link": f"https://adstransparency.google.com/advertiser/{B}/creative/{D}"}]
    result = p.parse_listing({"ad_creatives": rows, "pagination": {"next_page_token": "opaque"}}, A)
    assert result == {"creative_ids": [C], "rejected": 4, "next_page_token": "opaque"}
    with pytest.raises(p.PilotError):
        p.parse_listing({"ad_creatives": None}, A)


class Transport:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def __call__(self, url, params, headers):
        self.calls.append((url, params, headers))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def test_client_explicit_network_gate_and_cumulative_budget():
    p = pilot()
    assert hasattr(p, "Client"), "budgeted opt-in client missing"
    transport = Transport([{}, {}])
    client = p.Client({"SEARCHAPI_API_KEY": "secret-s", "YOUTUBE_DATA_API_KEY": "secret-y"},
                      max_requests=2, transport=transport)
    with pytest.raises(p.PilotError, match="network_disabled"):
        client.search({"engine": "google_ads_transparency_center", "advertiser_id": A})
    assert not transport.calls
    client.enabled = True
    client.search({"engine": "google_ads_transparency_center", "advertiser_id": A})
    client.youtube([V])
    with pytest.raises(p.PilotError, match="request_cap"):
        client.youtube([W])
    assert len(transport.calls) == 2
    assert client.used == 2
    assert transport.calls[0][0] == "https://www.searchapi.io/api/v1/search"
    assert transport.calls[0][2] == {"Authorization": "Bearer secret-s"}
    assert transport.calls[1][0] == "https://www.googleapis.com/youtube/v3/videos"
    assert transport.calls[1][1] == {"part": "snippet,statistics,contentDetails", "id": V}
    assert transport.calls[1][2] == {"X-Goog-Api-Key": "secret-y"}
    assert "secret" not in json.dumps([c[:2] for c in transport.calls])


def test_failed_request_consumes_budget_and_errors_do_not_leak_secrets():
    p = pilot()
    assert hasattr(p, "Client"), "safe client missing"
    transport = Transport([RuntimeError("https://example/?key=secret-y secret-s")])
    client = p.Client({"SEARCHAPI_API_KEY": "secret-s", "YOUTUBE_DATA_API_KEY": "secret-y"},
                      max_requests=1, enabled=True, transport=transport)
    with pytest.raises(p.PilotError, match="request_failed") as error:
        client.youtube([V])
    assert "secret" not in str(error.value)
    assert client.used == 1
    with pytest.raises(p.PilotError, match="request_cap"):
        client.youtube([V])


def test_metadata_batching_partial_unavailable_and_fields():
    p = pilot()
    assert hasattr(p, "fetch_metadata"), "official metadata batching missing"
    ids = [f"{n:011d}" for n in range(51)]
    item = {"id": ids[0], "snippet": {"title": "Title", "channelId": "UCchannel",
             "channelTitle": "Channel", "publishedAt": "2026-09-01T12:00:00Z"},
            "statistics": {"viewCount": "123"}, "contentDetails": {"duration": "PT1M2S"}}
    transport = Transport([{"items": [item, {"id": V}]}, {"items": []}])
    client = p.Client({"YOUTUBE_DATA_API_KEY": "s"}, max_requests=2, enabled=True, transport=transport)
    result = p.fetch_metadata(client, ids + [ids[0]])
    assert [len(c[1]["id"].split(",")) for c in transport.calls] == [50, 1]
    assert result[ids[0]] == {"status": "available", "video_id": ids[0], "title": "Title",
        "channel_id": "UCchannel", "channel_title": "Channel", "published_at": "2026-09-01T12:00:00Z",
        "view_count": 123, "duration": "PT1M2S"}
    assert result[ids[1]]["status"] == "unavailable"
    assert V not in result


def test_metadata_bad_item_does_not_hide_other_items():
    p = pilot()
    assert hasattr(p, "fetch_metadata"), "metadata validation missing"
    transport = Transport([{"items": [{"id": V, "statistics": {"viewCount": "n/a"}},
        {"id": W, "snippet": {"title": "secret-y", "publishedAt": "2026-09-01T00:00:00Z"},
         "contentDetails": {"duration": "PT0S"}, "statistics": {}}]}])
    client = p.Client({"YOUTUBE_DATA_API_KEY": "secret-y"}, max_requests=1, enabled=True, transport=transport)
    result = p.fetch_metadata(client, [V, W])
    assert result[V]["status"] == "malformed"
    assert result[W]["status"] == "available"
    assert result[W]["view_count"] is None
    assert result[W]["title"] == "[REDACTED]"


def test_metadata_failure_is_partial_not_all_or_nothing():
    p = pilot()
    assert hasattr(p, "fetch_metadata"), "metadata partial recovery missing"
    ids = [f"{n:011d}" for n in range(51)]
    transport = Transport([RuntimeError("bad secret"), {"items": []}])
    client = p.Client({"YOUTUBE_DATA_API_KEY": "secret"}, max_requests=2, enabled=True, transport=transport)
    result = p.fetch_metadata(client, ids)
    assert result[ids[0]]["status"] == "request_failed"
    assert result[ids[50]]["status"] == "unavailable"


def listing(creative=C, token=None):
    return {"ad_creatives": [{"id": creative, "advertiser": {"id": A}}],
            "pagination": {"next_page_token": token}}


def test_checkpoint_resume_preserves_mapping_and_never_stores_metadata(tmp_path):
    p = pilot()
    assert hasattr(p, "run_pilot"), "checkpointed pilot missing"
    checkpoint = tmp_path / "state.json"
    env = {"SEARCHAPI_API_KEY": "secret-s", "YOUTUBE_DATA_API_KEY": "secret-y"}
    transport = Transport([listing(token="opaque"), details(), listing(D),
                           {"variations": [{"video_id": V}]}, {"items": []}])
    result = p.run_pilot([A], checkpoint, env, 8, 2, 3, transport=transport)
    assert len(transport.calls) == 5
    assert transport.calls[2][1]["next_page_token"] == "opaque"
    assert transport.calls[0][1] == {"engine": "google_ads_transparency_center", "advertiser_id": A,
        "platform": "youtube", "ad_format": "video", "num": 40}
    assert transport.calls[1][1] == {"engine": "google_ads_transparency_center_ad_details",
        "advertiser_id": A, "creative_id": C}
    assert len(result["mappings"]) == 4
    assert set(result["metadata"]) == {V, W}
    saved = json.loads(checkpoint.read_text())
    assert saved["requests_used"] == 5
    assert "metadata" not in saved
    assert "secret" not in checkpoint.read_text()
    resumed = Transport([{"items": []}])
    second = p.run_pilot([A], checkpoint, env, 8, 2, 3, transport=resumed)
    assert len(resumed.calls) == 1  # Refresh ephemeral metadata, never repeat discovery.
    assert second["requests_used"] == 6
    assert second["mappings"] == result["mappings"]


def test_checkpoint_debits_before_dispatch_and_resumes_after_interruption(tmp_path):
    p = pilot()
    assert hasattr(p, "run_pilot"), "checkpoint reservation missing"
    checkpoint = tmp_path / "state.json"
    env = {"SEARCHAPI_API_KEY": "s", "YOUTUBE_DATA_API_KEY": "y"}
    def interrupted(url, params, headers):
        assert json.loads(checkpoint.read_text())["requests_used"] == 1
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        p.run_pilot([A], checkpoint, env, 3, 1, 1, transport=interrupted)
    transport = Transport([listing(), {"items": []}])
    result = p.run_pilot([A], checkpoint, env, 3, 1, 1, transport=transport)
    assert result["requests_used"] <= 3
    assert len(transport.calls) <= 2


def test_failed_creative_does_not_block_next_creative(tmp_path):
    p = pilot()
    assert hasattr(p, "run_pilot"), "partial pilot missing"
    page = listing()
    page["ad_creatives"].append({"id": D, "advertiser": {"id": A}})
    transport = Transport([page, RuntimeError("secret-y"), {"variations": [{"video_id": W}]}, {"items": []}])
    result = p.run_pilot([A], tmp_path / "state.json", {"SEARCHAPI_API_KEY": "s", "YOUTUBE_DATA_API_KEY": "secret-y"},
                         5, 1, 2, transport=transport)
    assert [m["video_id"] for m in result["mappings"]] == [W]
    assert result["advertisers"][A]["details"][C]["status"] == "request_failed"
    assert "secret-y" not in json.dumps(result)


def test_request_cap_applies_across_resume_and_scope_mismatch_fails(tmp_path):
    p = pilot()
    assert hasattr(p, "run_pilot"), "durable cap missing"
    checkpoint = tmp_path / "state.json"
    env = {"SEARCHAPI_API_KEY": "s", "YOUTUBE_DATA_API_KEY": "y"}
    first = Transport([listing(), details(), {"items": []}])
    p.run_pilot([A], checkpoint, env, 3, 1, 1, transport=first)
    second = Transport([])
    result = p.run_pilot([A], checkpoint, env, 3, 1, 1, transport=second)
    assert not second.calls
    assert result["requests_used"] == 3
    assert all(x["status"] == "request_cap" for x in result["metadata"].values())
    with pytest.raises(p.PilotError, match="checkpoint_scope"):
        p.run_pilot([B], checkpoint, env, 3, 1, 1, transport=second)


def test_repeated_page_token_stops_and_checkpoint_never_saves_echoed_key(tmp_path):
    p = pilot()
    assert hasattr(p, "run_pilot"), "safe pagination missing"
    checkpoint = tmp_path / "state.json"
    transport = Transport([listing(token="secret-long-key")])
    result = p.run_pilot([A], checkpoint, {"SEARCHAPI_API_KEY": "secret-long-key", "YOUTUBE_DATA_API_KEY": "y"},
                         4, 2, 1, transport=transport)
    assert "secret-long-key" not in checkpoint.read_text()
    assert len(transport.calls) == 1
    assert result["advertisers"][A]["listing_status"] == "unsafe_page_token"


def test_cli_defaults_to_no_network_no_checkpoint(monkeypatch, tmp_path, capsys):
    p = pilot()
    assert hasattr(p, "main"), "safe CLI missing"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SEARCHAPI_API_KEY", "secret-s")
    monkeypatch.setenv("YOUTUBE_DATA_API_KEY", "secret-y")
    monkeypatch.setattr(p, "http_get", lambda *args: pytest.fail("dry run made network call"))
    assert p.main([]) == 0
    output = capsys.readouterr().out
    assert "dry_run" in output and "not_verified" in output
    assert "secret-s" not in output and "secret-y" not in output
    assert not list(tmp_path.iterdir())


def test_live_cli_requires_cap_and_free_credit_confirmation(monkeypatch, capsys):
    p = pilot()
    assert hasattr(p, "main"), "live CLI safety gate missing"
    monkeypatch.setattr(p, "http_get", lambda *args: pytest.fail("unapproved call"))
    assert p.main(["--live", "--advertiser", A]) == 2
    assert p.main(["--live", "--advertiser", A, "--max-requests", "4"]) == 2
    monkeypatch.delenv("SEARCHAPI_API_KEY", raising=False)
    monkeypatch.delenv("YOUTUBE_DATA_API_KEY", raising=False)
    assert p.main(["--live", "--advertiser", A, "--max-requests", "4", "--free-credits-confirmed"]) == 2
    assert "missing_credentials" in capsys.readouterr().out


@pytest.mark.parametrize("extra", [
    {"advertiser": {"id": B}}, {"ad_information": {"advertiser_id": B}},
    {"ad_information": {"creative_id": D}},
    {"search_metadata": {"request_url": f"https://adstransparency.google.com/advertiser/{B}/creative/{C}"}},
])
def test_conflicting_nested_identity_rejected(extra):
    p = pilot()
    with pytest.raises(p.PilotError, match="identity_mismatch"):
        p.resolve_details(details(**extra), A, C)


def test_concurrent_checkpoint_run_refused(tmp_path):
    p = pilot()
    checkpoint = tmp_path / "state.json"
    with checkpoint.with_suffix(".lock").open("w") as lock:
        try:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ModuleNotFoundError:  # Windows
            import msvcrt
            lock.write(" "); lock.flush(); lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        with pytest.raises(p.PilotError, match="checkpoint_busy"):
            p.run_pilot([A], checkpoint, {"SEARCHAPI_API_KEY": "s", "YOUTUBE_DATA_API_KEY": "y"},
                        3, 1, 1, transport=Transport([]))


def test_two_advertisers_share_video_without_losing_either_mapping(tmp_path):
    p = pilot()
    page_b = {"ad_creatives": [{"id": D, "advertiser": {"id": B}}]}
    transport = Transport([listing(), {"variations": [{"video_id": V}]},
                           page_b, {"variations": [{"video_id": V}]}, {"items": []}])
    result = p.run_pilot([A, B], tmp_path / "state.json", {"SEARCHAPI_API_KEY": "s", "YOUTUBE_DATA_API_KEY": "y"},
                         8, 1, 1, transport=transport)
    assert {m["advertiser_id"] for m in result["mappings"]} == {A, B}
    assert transport.calls[-1][1]["id"] == V


def test_repeated_token_pagination_terminates(tmp_path):
    p = pilot()
    transport = Transport([listing(token="opaque"), {"variations": []}, listing(D, token="opaque"), {"variations": []}])
    result = p.run_pilot([A], tmp_path / "state.json", {"SEARCHAPI_API_KEY": "long-secret", "YOUTUBE_DATA_API_KEY": "other-secret"},
                         10, 10, 10, transport=transport)
    assert len(transport.calls) == 4
    assert result["advertisers"][A]["listing_status"] == "repeated_page_token"
    assert "opaque" not in json.dumps(result), "pagination tokens belong only in private resume state"


def test_real_http_transport_headers_redirects_and_malformed_json():
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    p = pilot()
    observed = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            observed.append((self.path, self.headers.get("Authorization")))
            if self.path.startswith("/redirect"):
                self.send_response(302)
                self.send_header("Location", "/leak")
                self.end_headers()
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"ok": true}' if self.path.startswith("/ok") else b'not json secret')
        def log_message(self, format, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        assert p.http_get(base + "/ok", {"q": "x"}, {"Authorization": "Bearer secret"}) == {"ok": True}
        with pytest.raises(Exception):
            p.http_get(base + "/redirect", {}, {"Authorization": "Bearer secret"})
        with pytest.raises(ValueError):
            p.http_get(base + "/bad", {}, {})
        assert [path for path, _ in observed] == ["/ok?q=x", "/redirect?", "/bad?"]
        assert observed[0][1] == "Bearer secret"
        assert all("secret" not in path for path, _ in observed)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("mutation", ["mapping_identity", "creative_identity", "count", "secret_url"])
def test_invalid_checkpoint_refused_before_network(tmp_path, mutation):
    p = pilot()
    checkpoint = tmp_path / "state.json"
    env = {"SEARCHAPI_API_KEY": "long-secret", "YOUTUBE_DATA_API_KEY": "other-secret"}
    p.run_pilot([A], checkpoint, env, 5, 1, 1,
                transport=Transport([listing(), details(), {"items": []}]))
    state = json.loads(checkpoint.read_text())
    if mutation == "mapping_identity":
        state["advertisers"][A]["details"][C]["mappings"][0]["advertiser_id"] = B
    elif mutation == "creative_identity":
        state["advertisers"][A]["creative_ids"] = ["not-an-id"]
    elif mutation == "count":
        state["requests_used"] = True
    else:
        state["advertisers"][A]["next_page_token"] = "https://vendor/?api_key=long-secret"
    checkpoint.write_text(json.dumps(state))
    transport = Transport([])
    with pytest.raises(p.PilotError, match="invalid_checkpoint"):
        p.run_pilot([A], checkpoint, env, 5, 1, 1, transport=transport)
    assert not transport.calls


def test_live_cli_contract_with_fixture_transport(monkeypatch, tmp_path, capsys):
    p = pilot()
    monkeypatch.setattr(p, "__file__", str(tmp_path / "searchapi_pilot.py"))
    monkeypatch.setenv("SEARCHAPI_API_KEY", "long-secret")
    monkeypatch.setenv("YOUTUBE_DATA_API_KEY", "other-secret")
    transport = Transport([listing(), details(), {"items": []}])
    monkeypatch.setattr(p, "http_get", transport)
    assert p.main(["--live", "--free-credits-confirmed", "--advertiser", A,
                   "--max-requests", "4", "--max-creatives", "1"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["requests_used"] == 3
    assert len(output["mappings"]) == 3
    assert (tmp_path / "pilot-results" / "small-large-pilot.json").exists()
