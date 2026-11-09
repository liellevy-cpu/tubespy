"""Isolated, opt-in pilot. Not imported by any production collector."""
import re
import json
from datetime import datetime
from urllib.parse import urlsplit, parse_qs, urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_get(url, params, headers):
    # Keys are headers, never URL parameters. Redirects cannot forward them.
    request = Request(url + "?" + urlencode(params), headers=headers)
    with build_opener(NoRedirect()).open(request, timeout=30) as response:
        raw = response.read(8_000_001)
        if len(raw) > 8_000_000:
            raise PilotError("response_too_large")
        return json.loads(raw)


class Client:
    def __init__(self, env, max_requests, enabled=False, transport=None, used=0, reserve=None):
        if not isinstance(max_requests, int) or max_requests < 1 or used < 0:
            raise PilotError("invalid_request_cap")
        self.keys = {k: env.get(k, "") for k in ("SEARCHAPI_API_KEY", "YOUTUBE_DATA_API_KEY")}
        self.max_requests = max_requests
        self.used = used
        self.enabled = enabled
        self.transport = transport or http_get
        self.reserve = reserve

    def redact(self, text):
        for secret in self.keys.values():
            if secret:
                text = text.replace(secret, "[REDACTED]")
        return text

    def request(self, service, params):
        if not self.enabled:
            raise PilotError("network_disabled")
        if self.used >= self.max_requests:
            raise PilotError("request_cap")
        key_name = "SEARCHAPI_API_KEY" if service == "search" else "YOUTUBE_DATA_API_KEY"
        key = self.keys[key_name]
        if not key:
            raise PilotError("missing_credentials")
        self.used += 1
        if self.reserve:
            self.reserve(self.used)  # Durably debit BEFORE dispatch, including failed calls.
        url = "https://www.searchapi.io/api/v1/search" if service == "search" else "https://www.googleapis.com/youtube/v3/videos"
        headers = {"Authorization": "Bearer " + key} if service == "search" else {"X-Goog-Api-Key": key}
        try:
            result = self.transport(url, params, headers)
        except HTTPError as exc:
            raise PilotError("http_" + str(exc.code)) from None
        except Exception:
            raise PilotError("request_failed") from None
        if not isinstance(result, dict) or "error" in result or "errors" in result:
            raise PilotError("invalid_api_response")
        return result

    def search(self, params):
        return self.request("search", params)

    def youtube(self, ids):
        if not 1 <= len(ids) <= 50 or any(not video_id(v) for v in ids):
            raise PilotError("invalid_video_batch")
        return self.request("youtube", {"part": "snippet,statistics,contentDetails", "id": ",".join(ids)})


def fetch_metadata(client, ids):
    ids = list(dict.fromkeys(ids))
    result = {}
    for start in range(0, len(ids), 50):
        batch = ids[start:start + 50]
        try:
            payload = client.youtube(batch)
            if not isinstance(payload.get("items"), list):
                raise PilotError("malformed_metadata")
        except PilotError as exc:
            result.update({v: {"status": str(exc), "video_id": v} for v in batch})
            continue
        result.update({v: {"status": "unavailable", "video_id": v} for v in batch})
        for item in payload["items"]:
            if not isinstance(item, dict) or item.get("id") not in batch:
                continue
            v = item["id"]
            try:
                snippet, stats, content = item["snippet"], item.get("statistics", {}), item["contentDetails"]
                published = snippet["publishedAt"]
                parsed = datetime.fromisoformat(published.replace("Z", "+00:00"))
                if parsed.tzinfo is None or not isinstance(snippet["title"], str):
                    raise ValueError
                views = stats.get("viewCount")
                if views is not None and (not isinstance(views, str) or not re.fullmatch(r"\d+", views)):
                    raise ValueError
                duration = content["duration"]
                if not isinstance(duration, str) or not re.fullmatch(r"P(?=\d|T\d)(?:\d+D)?(?:T(?:\d+H)?(?:\d+M)?(?:\d+(?:\.\d+)?S)?)?", duration):
                    raise ValueError
                result[v] = {"status": "available", "video_id": v,
                    "title": client.redact(snippet["title"]),
                    "channel_id": client.redact(str(snippet.get("channelId", ""))),
                    "channel_title": client.redact(str(snippet.get("channelTitle", ""))),
                    "published_at": published, "view_count": int(views) if views is not None else None,
                    "duration": duration}
            except (KeyError, ValueError, TypeError, AttributeError):
                result[v] = {"status": "malformed", "video_id": v}
    return result


class PilotError(Exception):
    """Only fixed, safe messages belong in this exception."""


def valid_id(value, prefix):
    return isinstance(value, str) and re.fullmatch(prefix + r"\d{20}", value) is not None


def video_id(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{11}", value) else None


def watch_id(value):
    if not isinstance(value, str):
        return None
    try:
        u = urlsplit(value)
        if u.scheme != "https" or u.hostname not in {"www.youtube.com", "youtube.com", "m.youtube.com"} or u.username or u.port:
            return None
        if u.path == "/watch":
            values = parse_qs(u.query).get("v", [])
            return video_id(values[0]) if len(values) == 1 else None
        if u.path.startswith("/embed/"):
            return video_id(u.path.removeprefix("/embed/"))
    except ValueError:
        pass
    return None


def check_identity(payload, advertiser, creative=None):
    if not isinstance(payload, dict) or not valid_id(advertiser, "AR") or (creative is not None and not valid_id(creative, "CR")):
        raise PilotError("malformed_identity")
    for source in (payload, payload.get("search_parameters", {}), payload.get("ad_information", {})):
        if not isinstance(source, dict):
            raise PilotError("malformed_identity")
        if "advertiser" in source and (not isinstance(source["advertiser"], dict) or source["advertiser"].get("id") != advertiser):
            raise PilotError("identity_mismatch")
        for key, expected in (("advertiser_id", advertiser), ("creative_id", creative)):
            if expected is not None and key in source and source[key] != expected:
                raise PilotError("identity_mismatch")
    metadata = payload.get("search_metadata", {})
    if not isinstance(metadata, dict):
        raise PilotError("malformed_identity")
    request_url = metadata.get("request_url")
    if isinstance(request_url, str):
        echoed_advertisers = re.findall(r"/advertiser/([^/?#]+)", request_url)
        echoed_creatives = re.findall(r"/creative/([^/?#]+)", request_url)
        if any(a != advertiser for a in echoed_advertisers) or (creative and any(c != creative for c in echoed_creatives)):
            raise PilotError("identity_mismatch")


def resolve_details(payload, advertiser, creative):
    check_identity(payload, advertiser, creative)
    variations = payload.get("variations")
    if not isinstance(variations, list):
        raise PilotError("malformed_variations")
    mappings = []
    unresolved = 0
    for index, variation in enumerate(variations):
        if not isinstance(variation, dict):
            unresolved += 1
            continue
        explicit = video_id(variation.get("video_id"))
        linked = watch_id(variation.get("video_link"))
        if ("video_id" in variation and not explicit) or (explicit and linked and explicit != linked):
            unresolved += 1
            continue
        resolved = explicit or linked
        if not resolved:
            unresolved += 1
            continue
        mappings.append({"advertiser_id": advertiser, "creative_id": creative,
                         "variation_index": index, "video_id": resolved,
                         "watch_url": f"https://www.youtube.com/watch?v={resolved}",
                         "creative_url": f"https://adstransparency.google.com/advertiser/{advertiser}/creative/{creative}",
                         "identity_basis": "exact_request"})
    return {"mappings": mappings, "video_ids": list(dict.fromkeys(m["video_id"] for m in mappings)),
            "unresolved_variations": unresolved}


def parse_listing(payload, advertiser):
    check_identity(payload, advertiser)
    rows = payload.get("ad_creatives")
    if not isinstance(rows, list):
        raise PilotError("malformed_listing")
    ids, rejected = [], 0
    for row in rows:
        if not isinstance(row, dict) or not valid_id(row.get("id"), "CR") or not isinstance(row.get("advertiser"), dict) or row["advertiser"].get("id") != advertiser:
            rejected += 1
            continue
        creative = row["id"]
        if "details_link" in row:
            try:
                u = urlsplit(row["details_link"])
                if u.scheme != "https" or u.netloc != "adstransparency.google.com" or u.path != f"/advertiser/{advertiser}/creative/{creative}":
                    raise ValueError
            except (ValueError, TypeError, AttributeError):
                rejected += 1
                continue
        if creative not in ids:
            ids.append(creative)
    pagination = payload.get("pagination", {})
    if not isinstance(pagination, dict):
        raise PilotError("malformed_pagination")
    token = pagination.get("next_page_token")
    if token is not None and (not isinstance(token, str) or len(token) > 16384):
        raise PilotError("malformed_pagination")
    return {"creative_ids": ids, "rejected": rejected, "next_page_token": token or None}


def save_checkpoint(path, state):
    import os
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".pilot-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(state, output, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def run_pilot(advertisers, checkpoint, env, max_requests, max_pages, max_creatives, transport=None):
    import fcntl
    import os
    from pathlib import Path
    if any(not env.get(k) for k in ("SEARCHAPI_API_KEY", "YOUTUBE_DATA_API_KEY")):
        raise PilotError("missing_credentials")
    checkpoint = Path(checkpoint)
    checkpoint.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(checkpoint.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PilotError("checkpoint_busy") from None
        return _run_pilot(advertisers, checkpoint, env, max_requests, max_pages, max_creatives, transport)


def validate_checkpoint(state, advertisers, env):
    try:
        if type(state["requests_used"]) is not int or state["requests_used"] < 0:
            raise ValueError
        if set(state["advertisers"]) != set(advertisers):
            raise ValueError
        for advertiser, progress in state["advertisers"].items():
            if type(progress["pages"]) is not int or progress["pages"] < 0:
                raise ValueError
            if not isinstance(progress["creative_ids"], list) or any(not valid_id(c, "CR") for c in progress["creative_ids"]):
                raise ValueError
            if not isinstance(progress["seen_tokens"], list):
                raise ValueError
            for token in progress["seen_tokens"] + [progress["next_page_token"]]:
                if token is not None and (not isinstance(token, str) or len(token) > 16384 or "://" in token
                    or any(env.get(k) and env[k] in token for k in ("SEARCHAPI_API_KEY", "YOUTUBE_DATA_API_KEY"))):
                    raise ValueError
            for creative, detail in progress["details"].items():
                if creative not in progress["creative_ids"]:
                    raise ValueError
                for mapping in detail["mappings"]:
                    v = mapping["video_id"]
                    if (mapping["advertiser_id"] != advertiser or mapping["creative_id"] != creative
                        or not video_id(v) or type(mapping["variation_index"]) is not int or mapping["variation_index"] < 0
                        or mapping["watch_url"] != f"https://www.youtube.com/watch?v={v}"
                        or mapping["creative_url"] != f"https://adstransparency.google.com/advertiser/{advertiser}/creative/{creative}"):
                        raise ValueError
    except (ValueError, TypeError, KeyError, AttributeError):
        raise PilotError("invalid_checkpoint") from None


def _run_pilot(advertisers, checkpoint, env, max_requests, max_pages, max_creatives, transport=None):
    from pathlib import Path
    advertisers = list(dict.fromkeys(advertisers))
    if not advertisers or any(not valid_id(a, "AR") for a in advertisers) or not 1 <= len(advertisers) <= 2:
        raise PilotError("invalid_advertisers")
    if max_pages < 1 or max_creatives < 1:
        raise PilotError("invalid_limits")
    if any(not env.get(k) for k in ("SEARCHAPI_API_KEY", "YOUTUBE_DATA_API_KEY")):
        raise PilotError("missing_credentials")
    checkpoint = Path(checkpoint)
    scope = {"advertisers": advertisers, "max_pages": max_pages, "max_creatives": max_creatives}
    state = {"version": 1, "scope": scope, "requests_used": 0, "advertisers": {
        a: {"pages": 0, "next_page_token": None, "seen_tokens": [], "listing_status": "pending",
            "creative_ids": [], "rejected": 0, "details": {}} for a in advertisers}}
    if checkpoint.exists():
        try:
            state = json.loads(checkpoint.read_text())
            if state["version"] != 1 or state["scope"] != scope:
                raise PilotError("checkpoint_scope")
            validate_checkpoint(state, advertisers, env)
        except (ValueError, KeyError, TypeError):
            raise PilotError("invalid_checkpoint") from None
    def reserve(used):
        state["requests_used"] = used
        save_checkpoint(checkpoint, state)
    client = Client(env, max_requests, enabled=True, transport=transport,
                    used=state["requests_used"], reserve=reserve)
    for advertiser in advertisers:
        progress = state["advertisers"][advertiser]
        while True:
            # Finish each saved page before requesting the next one.
            for creative in progress["creative_ids"][:max_creatives]:
                if creative in progress["details"]:
                    continue
                if client.used >= max_requests - 1:
                    break  # Keep one request for metadata on recovered IDs.
                try:
                    payload = client.search({"engine": "google_ads_transparency_center_ad_details",
                                             "advertiser_id": advertiser, "creative_id": creative})
                    resolved = resolve_details(payload, advertiser, creative)
                    progress["details"][creative] = {"status": "resolved" if resolved["video_ids"] else "unresolved", **resolved}
                except PilotError as exc:
                    progress["details"][creative] = {"status": str(exc), "mappings": [], "video_ids": []}
                save_checkpoint(checkpoint, state)
            if (client.used >= max_requests - 1 or len(progress["details"]) >= max_creatives
                    or progress["pages"] >= max_pages or progress["listing_status"] not in {"pending", "more"}):
                break
            params = {"engine": "google_ads_transparency_center", "advertiser_id": advertiser,
                      "platform": "youtube", "ad_format": "video", "num": 40}
            if progress["next_page_token"]:
                params["next_page_token"] = progress["next_page_token"]
            try:
                parsed = parse_listing(client.search(params), advertiser)
                token = parsed["next_page_token"]
                if token and any(key and key in token for key in client.keys.values()):
                    raise PilotError("unsafe_page_token")
                progress["pages"] += 1
                progress["rejected"] += parsed["rejected"]
                progress["creative_ids"] = list(dict.fromkeys(progress["creative_ids"] + parsed["creative_ids"]))
                if token and token in progress["seen_tokens"]:
                    progress["listing_status"] = "repeated_page_token"
                    progress["next_page_token"] = None
                else:
                    progress["next_page_token"] = token
                    progress["listing_status"] = "more" if token else "complete"
                    if token:
                        progress["seen_tokens"].append(token)
            except PilotError as exc:
                progress["listing_status"] = str(exc)
            save_checkpoint(checkpoint, state)
    mappings = [mapping for a in advertisers for c in state["advertisers"][a]["creative_ids"]
                if c in state["advertisers"][a]["details"]
                for mapping in state["advertisers"][a]["details"][c]["mappings"]]
    metadata = fetch_metadata(client, [m["video_id"] for m in mappings])
    save_checkpoint(checkpoint, state)
    # API metadata is ephemeral: never stored in the discovery checkpoint.
    progress_summary = {a: {k: v for k, v in state["advertisers"][a].items()
                            if k not in {"next_page_token", "seen_tokens"}} for a in advertisers}
    return {"mode": "live_pilot", "requests_used": client.used, "request_cap": max_requests,
            "advertisers": progress_summary, "mappings": mappings, "metadata": metadata,
            "coverage": "bounded_sample_not_complete_advertiser_coverage"}


def main(argv=None):
    import argparse
    import os
    from pathlib import Path
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--advertiser", action="append", default=[])
    parser.add_argument("--live", action="store_true", help="Explicitly enable authenticated requests")
    parser.add_argument("--free-credits-confirmed", action="store_true", help="Operator has verified sufficient free SearchAPI credits and no paid overage")
    parser.add_argument("--max-requests", type=int, help="Cumulative cap across all APIs and resumes")
    parser.add_argument("--max-pages", type=int, default=1)
    parser.add_argument("--max-creatives", type=int, default=3)
    parser.add_argument("--run-name", default="small-large-pilot")
    args = parser.parse_args(argv)
    keys = ("SEARCHAPI_API_KEY", "YOUTUBE_DATA_API_KEY")
    if not args.live:
        print(json.dumps({"mode": "dry_run", "network_requests": 0,
                          "credentials_present": {k: bool(os.environ.get(k)) for k in keys},
                          "access": "not_verified", "note": "Presence is not authorization or free-credit verification"}))
        return 0
    try:
        if args.max_requests is None or args.max_requests < 1:
            raise PilotError("explicit_positive_request_cap_required")
        if not args.free_credits_confirmed:
            raise PilotError("free_credits_confirmation_required")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", args.run_name):
            raise PilotError("invalid_run_name")
        checkpoint = Path(__file__).resolve().parent / "pilot-results" / (args.run_name + ".json")
        result = run_pilot(args.advertiser, checkpoint, os.environ, args.max_requests,
                           args.max_pages, args.max_creatives)
        print(json.dumps(result, sort_keys=True))
        return 0
    except PilotError as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}))
        return 2
    except (OSError, ValueError, KeyError, TypeError):
        print(json.dumps({"status": "blocked", "reason": "local_state_error"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
