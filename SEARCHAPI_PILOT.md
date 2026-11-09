# SearchAPI → official YouTube pilot (isolated)

This is **not connected to the production worker, scheduler, library builder, or public files**. No publication or purchases. The default command makes **zero network requests and writes no checkpoint**:

```sh
cd /Users/maxopenclaw/.hermes/hermes-agent/youtube-competitor-library
.venv/bin/python searchapi_pilot.py
```

It reports only whether `SEARCHAPI_API_KEY` and `YOUTUBE_DATA_API_KEY` are present. Presence is not verified access. It does not load dotenv files, print keys, fall back to GOOGLE_API_KEY, enable APIs, or change restrictions.

## Current blocker

The parent checked access: neither dedicated credential was present in its shell/default/project environment. Its existing GOOGLE_API_KEY returned HTTP 403 `accessNotConfigured` on videos.list. This implementation did not repeat that authenticated call and has not made any authenticated vendor/YouTube requests.

A human should provide authorized credentials through the local secure environment/secret manager under exactly these names:

- `SEARCHAPI_API_KEY`
- `YOUTUBE_DATA_API_KEY`

Do not paste credential values into chat, command arguments, this README, test fixtures, or source control. Re-run the default command to check presence. The operator must separately verify sufficient **free** SearchAPI credits, no paid overage, and authorized YouTube Data API access. This pilot cannot infer billing status from a key and cannot guarantee that the vendor will not charge an account configured for paid usage. **Do not use its live flag on such an account.** No plan purchase or API enablement is part of this work.

## Capped live command — only after access/free-credit verification

Set `SMALL_ADVERTISER_ID` and `LARGE_ADVERTISER_ID` locally to the two independently verified exact `AR` IDs. These are identifiers, not credentials; advertiser size has not been established by the offline tests. Then run:

```sh
cd /Users/maxopenclaw/.hermes/hermes-agent/youtube-competitor-library
.venv/bin/python searchapi_pilot.py \
  --live --free-credits-confirmed \
  --advertiser "${SMALL_ADVERTISER_ID:?Set the verified small advertiser AR ID}" \
  --advertiser "${LARGE_ADVERTISER_ID:?Set the verified large advertiser AR ID}" \
  --max-requests 12 --max-pages 1 --max-creatives 3 \
  --run-name small-large-pilot
```

The cap is **12 total attempted HTTP requests across BOTH APIs and all resumes of this checkpoint**, not 12 per advertiser or per invocation. At most one listing page and three detail requests per advertiser in this command. One request is reserved for metadata; if there are more than 50 unique recovered IDs, additional metadata batches use only the remaining total budget. Every request, including failed/interrupted requests, is debited before dispatch. No automatic network retries or redirects. Timeout: 30 seconds per request; response-body cap: 8 MB.

The flag `--free-credits-confirmed` is an operator attestation, **not** automatic vendor billing verification. Running a new `--run-name` creates a separate budget; do not use this to bypass the approved total.

## Resume and output

- Re-run the identical command to resume `pilot-results/small-large-pilot.json`. Advertiser IDs/order and page/creative limits are scope-bound; conflicting scope is refused. Increasing the request cap is a new explicit authorization, not an automatic retry.
- Checkpoints are atomic, mode 0600, and protected by a process lock. Completed listing/detail work is not repeated. A crash after dispatch but before saving a result can cause that request to be repeated on resume, but its original attempt still counts toward the cumulative cap.
- Failed details are kept as explicit outcomes and do not block successful siblings. There is no implicit retry of terminal failures. A new, separately approved bounded run is needed to retry them.
- Listing pagination uses `pagination.next_page_token`, never provider-supplied next-page URLs; repeated tokens stop discovery. Raw response bodies, API keys, request URLs, titles and YouTube statistics are not saved in checkpoints.
- Output contains each accepted variation's advertiser ID, creative ID, variation index, canonical creative/watch URL; deduplicated YouTube metadata is separate. Identical video IDs across creatives/advertisers retain every mapping.
- Metadata is fetched fresh and returned only in the current output, **not persisted to checkpoint or a history store**. A resume therefore uses new metadata calls if budget remains. Do not redirect repeated outputs into a historical statistics store until retention/policy permission is verified.
- `unavailable` means videos.list omitted an ID; it does not distinguish private, removed, region-restricted or other causes. `malformed`, `request_failed`, `http_403`, and `request_cap` are separate from unavailable.
- `published_at` is YouTube publication time, not ad launch date. View counts may combine paid and organic views. No age score, views/day, growth metric, historical series or performance inference is calculated.
- Coverage is a **bounded recovered sample**, never an advertiser-completeness claim. `creative_ids` can include more listed entries than the detail cap allows; only entries in `details` were attempted. Check `pages`, `listing_status`, `rejected`, detail outcomes, mappings, and metadata statuses together.

## Identity contract and limitations

- Listing rows require an exact requested advertiser ID and a valid creative ID. Any supplied details URL must match both exactly.
- Details are fetched with the exact advertiser/creative pair. Conflicting echoed identity is rejected. The current documented variations response may omit identity fields, so such results are explicitly labeled `identity_basis: exact_request`: binding to the requested provider endpoint is **not independent verification of the vendor's internal mapping**.
- All variations are examined. Accept an explicit 11-character `video_id`, a canonical HTTPS YouTube watch URL or embed URL; canonicalize to a watch URL. Reject malformed IDs, conflicting explicit/watch IDs, foreign hosts, duplicate watch `v` parameters, destination/preview URLs, and googlevideo asset IDs. A valid explicit video ID can still be used when a sibling media URL is not a watch URL.
- Metadata uses the official videos.list endpoint with `snippet,statistics,contentDetails`, in deduplicated batches of at most 50. No unofficial YouTube scraping or channel-search substitution.

## Original docs checked during implementation

- https://www.searchapi.io/docs/google-ads-transparency-center-api
  - GET `https://www.searchapi.io/api/v1/search`
  - `engine=google_ads_transparency_center`, exact `advertiser_id`, `platform=youtube`, `ad_format=video`, `num=40`, optional `next_page_token`.
- https://www.searchapi.io/docs/google-ads-transparency-center-ad-details-api
  - Same endpoint, `engine=google_ads_transparency_center_ad_details`, exact `advertiser_id` and `creative_id`.
  - SearchAPI documents Bearer authorization; the key is never placed in the request URL.
- https://developers.google.com/youtube/v3/docs/videos/list
  - GET `https://www.googleapis.com/youtube/v3/videos`, explicit comma-separated `id` and `part`.
- https://cloud.google.com/docs/authentication/api-keys-use
  - Google documents `X-Goog-Api-Key` header authentication, used instead of URL keys. Actual acceptance for the supplied YouTube project remains untested until access is provided.

## Observed test evidence

```sh
.venv/bin/python -m pytest tests/test_searchapi_pilot.py -q
.venv/bin/python -m pytest tests -q
```

Observed development cycles (test-first):

1. Extraction/identity: **18 failed** because the isolated module was absent → **18 passed**; full project suite **83 passed**.
2. Opt-in client, budgets and metadata: **5 failed / 18 passed** for missing functionality → **23 passed**; full suite **88 passed**.
3. Checkpoint/CLI/partial recovery: **7 failed / 23 passed** → **30 passed**; full suite **95 passed**.
4. Nested identity and concurrent-run rejection: **5 failed / 33 passed** → **38 passed**; full suite **103 passed**.
5. Invalid checkpoint rejection: **4 failed / 39 passed** → **43 passed**; full project suite **108 passed**.
6. Pagination-token output suppression: targeted test **1 failed** before the fix → **43 pilot tests passed**, final full project suite **108 passed** (7.00 seconds). Pagination tokens remain private resume state and are not printed.

The tests exercise real extraction, orchestration, temporary-file checkpoint/resume and budget behavior with fixture transports at the external network boundary. A loopback HTTP server additionally exercises actual HTTP serialization, secret headers, redirect refusal and malformed JSON. The real default CLI reported zero requests and both credential-presence booleans false.

**These tests do not establish live vendor recovery, known-unlisted coverage, actual advertiser size, live API authorization, vendor cost, or complete results.** The live small/large comparison is still blocked on credentials and free-credit verification. No commits were made.
