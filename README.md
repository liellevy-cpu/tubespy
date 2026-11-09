# TubeSpy + Coolina YouTube Competitor Library

Source code for TubeSpy (every US YouTube ad for one Google Ads Transparency advertiser, exported to CSV) and the YouTube competitor library site (https://youtube-competitor-library.coolina.here.now/).

Live copy runs on the Emater Mac mini at `~/.hermes/hermes-agent/youtube-competitor-library`. Sam runs TubeSpy through `scripts/tubespy_export.sh`.

- `tubespy_export.py` - one advertiser link -> CSV.
- `collect_all.py` - the Google Ads Transparency scraper + YouTube view lookup.
- `library.py`, `public/` - builds and serves the library site.
- `add_brand.py`, `brand_service.py`, `brand_worker.py` - the "Add a brand" form backend and weekly refresh queue.
- `original-app/` - the original TubeSpy Node app (Sept 2026), kept for reference. Not used by the live system.

Not in this repo: raw scrape data, run output, logs, access tokens and hosting config. Those stay on the Mini.

Setup: `python3 -m venv .venv && .venv/bin/pip install playwright==1.63.0 pytest && .venv/bin/python -m pytest -q tests`. Optional `YOUTUBE_DATA_API_KEY` env var for fast view counts.

---

# Coolina YouTube Competitor Library

Standalone public-source dashboard built from Google Ads Transparency Center and YouTube evidence. It does not modify or depend on the existing Meta competitor library or Foreplay.

## Add brands (local implementation; not yet deployed)

Public submissions use the page's **Add a brand** form. The server accepts only a name and an exact `https://adstransparency.google.com/advertiser/AR…` link. It reconstructs US/VIDEO/YOUTUBE filters, commits the unique advertiser identity and queued state to SQLite, then wakes the collection worker. Duplicates cannot rename, reactivate, or force-refresh a brand. No public editing or deletion exists.

The owner-managed Sheet remains a secondary source for seed/weekly synchronization. Its ID is private: set `TUBESPY_SHEET_ID` / `TUBESPY_SHEET_GID` or put `{"spreadsheet_id": ..., "sheet_gid": ...}` in an untracked `local_config.json`. The tab needs the columns Advertiser, Google Ads Transparency URL, Active.

- `brand_worker.py --seed` imports the published advertiser identities and current owner Sheet. Historical search links resolve only through an already-verified published ID. Run before enabling the service.
- `brand_service.py --port 8793` runs the loopback-only API plus immediate worker. Requires private `build/brand-service.token` with at least 32 random characters. The parent owns service supervision and approved ingress configuration.
- `brand_worker.py --weekly` synchronizes the owner Sheet and queues every active durable brand, including public additions. Owner `Active=FALSE` pauses an existing identity; removing a Sheet row does not erase a public submission or old snapshot.
- Durable registry/queue: `build/brands.sqlite`. Back it up. Public capacity is 500 brands; admission is limited to 30 new brands per day globally and five per hour per upstream connection address, plus here.now per-IP proxy limits. The upstream address may be shared by the proxy, so the local hourly limit is intentionally conservative.
- New collection runs use unique private directories, fresh Google discovery and YouTube metadata, a single active worker, and bounded runtime. Failed/incomplete collection leaves prior published videos unchanged. A successful new brand can publish without waiting for other failed brands.
- Exact planned proxy routes: GET `/api/brands` and POST `/api/add-brand`, upstream `/brands`; scoped bearer token is injected by here.now, never shipped to browsers. Configure read limit to tolerate pending-job polling, e.g. 600/hour/ip, write 5/hour/ip. No wildcard proxy.
- **Deployment remains blocked until a durable approved HTTPS ingress is verified.** Do not publish the form with a dead endpoint. See `build/add-brand-handoff.md` for exact mapping and receipts.
- Use project `.venv/bin/python` for worker/service/tests. `qa_add_brand.py` is real browser/HTTP acceptance with an explicitly injected failure, not proof of successful source collection or live deployment.

Legacy `registry_sync.py`/`finalize_multi.py` remain for old manual workflows; do not schedule them alongside the new worker or use the old staging publisher to remove proxy routes.

## Build

```bash
python3 library.py \
  --source /Users/maxopenclaw/.hermes/cache/browser-use/workspace/20260909_002841_c8084e0b \
  --snapshot-at 2026-09-09T00:00:00+00:00 \
  --public-dir public \
  --stage-dir stage/private-youtube-pilot
```

The snapshot timestamp is injected. The builder fails closed on unknown/missing fields, missing views or upload dates, unsafe links, extraction errors, duplicate/conflicting metadata, and source-count or ID reconciliation failures.

## Test

```bash
/Users/maxopenclaw/.hermes/hermes-agent/.venv/bin/python -m pytest -q tests
```

## Local preview

Serve `stage/private-youtube-pilot` with a local static server. The staged manifest is intentionally restricted to `index.html`, `styles.css`, `app.js`, and `data/library.json`.
