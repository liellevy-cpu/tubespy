# YouTube public add-brand feature

## Deployment status

Live at https://youtube-competitor-library.coolina.here.now/. Hosting and tunnel settings are private and kept off this repo.

## Runtime and commands

Use project `.venv/bin/python` (Python 3.11 with Playwright + pytest). Existing Google Chrome executable is used, not a personal browser profile.

- `brand_worker.py --seed`: fetch current owner Sheet, import verified existing advertiser IDs, respect explicit inactive rows. Run before opening public intake.
- `brand_service.py --port 8793`: loopback HTTP service + single worker. Requires protected `build/brand-service.token` (32+ chars). Parent owns LaunchAgent supervision.
- `brand_worker.py --weekly`: refresh owner registry then queue all non-inactive durable brands, including public additions. Parent owns weekly schedule. Service notices external queued jobs within 60 sec. **Immediate public submits instead signal the worker directly after transaction commit**, not via that interval.
- `brand_worker.py --drain`: bounded manual batch processing; do not use concurrently with production service.
- `.venv/bin/python -m pytest tests -q`: unit, real HTTP, local Chrome/browser tests. One anonymous live static asset readback test requires internet.

## Ingress contract

Add only these exact site proxy routes in `public/.herenow/proxy.json` once the approved durable upstream is known:

- `/api/add-brand`: upstream exact `/brands` route, `method: POST`, `rateLimit: 5/hour/ip`.
- `/api/brands`: same upstream, `method: GET`, `rateLimit: 600/hour/ip`.
- Inject `Authorization: Bearer ${YOUTUBE_BRAND_SERVICE_TOKEN}` from a here.now Coolina workspace variable pinned to the upstream hostname. Never ship the actual token.

No wildcard proxy, arbitrary upstream URL, admin/scheduler endpoint, edit/delete handler, or request-controlled command. Both routes require the service token upstream. Backend rejects PATCH/PUT/DELETE, invalid URLs/names/extra fields and bodies over4096 bytes. Google query parameters are discarded and reconstructed as US/video/YouTube without date restrictions.

## Persistence and limits

`build/brands.sqlite` is the authoritative append-only public registry and durable work queue. Preserve and back up this file. Existing sheet synchronization never overwrites public additions. Explicit inactive owner IDs remain refused on public resubmission. Public duplicates do not rename or requeue an existing advertiser.

SQLite transactions protect canonical AR-ID uniqueness and claims. Limit500 total registry rows,30 anonymous new brands/day globally,5/hour per backend visitor bucket (conservative shared-proxy bucket), and edge5/hour/IP. Do not trust arbitrary X-Forwarded-For. One concurrent worker; collecting leases last3700seconds; fresh collection subprocess limit1800seconds. Source discovery allows at most3000 creative URLs,120 scroll rounds, serial detail extraction, bounded retries. Browser egress is limited to declared Google/YouTube service domains; unknown sources are blocked.

Each run uses a new UUID directory and AR-ID raw folder, bypassing historic caches/TQR exception. Collection must be stable, exact-advertiser, nonempty, resolved, and metadata-complete; otherwise retain old videos and show failure. No guessed zeros or fabricated success. Some real advertisers may be unable to pass complete-coverage gates because Google/YouTube blocks or removes content.

## Publication

Fresh brand data is merged under a publication lock with the newest local verified dataset. Other advertisers retain their original timestamps. The aggregate timestamp is the oldest retained source snapshot, not a false new freshness date.

Stage only index.html/styles.css/app.js/data/library.json and `.herenow/proxy.json`. The regular `library.build_stage` now preserves the optional proxy manifest, so old build paths do not silently drop routes. `publish_stage` verifies live workspace mapping, invokes existing authenticated here.now publisher, checks exact account URL/warnings and anonymously compares JS/CSS/data bytes before committing local canonical data. No live publish has been claimed while ingress is blocked.

## Evidence

See `build/add-brand-handoff.md`, tests/, `build/add-brand-red.txt`, and `build/add-brand-mobile-qa.png`. Browser test uses a deliberately failing collector to assert honest failure rendering; it is not evidence of successful upstream collection. Actual source-run receipts are separately recorded in build/brand-runs/ and build/real-collection.log.
