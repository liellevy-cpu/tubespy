# Questions for Henry — Coolina library on the Mac mini

These all touch shared resources or carry some risk, so we're not deciding them on our
own. Short list:

### Scraping budget (shared IP)
1. The mini's IP is shared with your other agents and Liel's scraping. A Google block
   hits everyone on that IP. **What's a safe scraping budget for us** — requests/minute
   or hours/night — so we stay well under the point where Google starts blocking? Our
   default is conservative (1 advertiser at a time, ~3h/night, never Sunday); tell us if
   that's still too much given everything else on the box.
2. Is there a **proxy or separate egress IP** we should route our scraping through, or do
   we share the mini's IP with everyone?

### Isolation (our own store & schedule)
3. We run **fully isolated**: our own SQLite store (`coolina-brands.sqlite`), our own
   exports/logs, our own cron — and we never open your store, change your add-brand
   limits, or run on Sunday. **Is that separation what you want**, or do you prefer we
   plug into an existing store/scheduler you manage?
4. What **time window** is safe for our nightly run? We default to 01:00 local and skip
   Sunday to stay clear of your refresh — confirm that doesn't collide with your other
   jobs.

### Discovery-added brands
5. Discovery finds new advertisers automatically (it just found 3 with US video ads).
   Should those **go through your public add-brand limits**, or get their **own internal
   path** (trusted, not rate-limited)? We assumed an internal path, since they're our own
   automated additions, not public submissions.
6. As the library grows to thousands of brands, is there a **ceiling** you want on our
   total brand count or nightly new-brand onboarding?

### Shared raw data (read-only)
9. On the Mini, Liel's **full raw scrape data already exists** from his weekly refresh.
   Can our isolated instance **read Liel's full raw scrape data read-only**, so the "All"
   tab can show all of his advertisers' videos **without us re-scraping them**? We'd only
   read (never write) his files, and only to populate "All" for brands he already covers.

### Secrets & ops
7. Where should we store our two secrets on the mini (`YOUTUBE_API_KEY`,
   `ANTHROPIC_API_KEY`) — your secrets manager, or an untracked `.env` only we read?
8. Who should get our **run logs / failure alerts**, and where should they live?
