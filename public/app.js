"use strict";

const grid = document.querySelector("#video-grid");
const template = document.querySelector("#video-card-template");
const note = document.querySelector("#ranking-note");
const advertiserSelect = document.querySelector("#advertiser-select");
const LS_KEY = "tubespy_item_states";

let library = null;
let allVideos = [];          // [{video, advertiserName}]
let states = {};             // video_id -> "used" | "not_relevant"
let activeTab = "new";
let advertiserName = "all";
let suggestions = [];

function positionIndicator() {
  const seg = document.querySelector(".segmented");
  const active = seg.querySelector(".tab.is-active");
  const ind = seg.querySelector(".seg-indicator");
  if (active && ind) { ind.style.left = `${active.offsetLeft}px`; ind.style.width = `${active.offsetWidth}px`; }
}

function safeUrl(raw, allowedHost) {
  const parsed = new URL(raw);
  if (parsed.protocol !== "https:" || parsed.hostname !== allowedHost || parsed.username || parsed.password) {
    throw new Error("Unsafe source URL");
  }
  return parsed.href;
}
const number = (v) => new Intl.NumberFormat("en-US").format(v);
const date = (v) => new Intl.DateTimeFormat("en-US", { year: "numeric", month: "short", day: "numeric" }).format(new Date(v));
const duration = (s) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;

function addSummary(value, label) {
  const item = document.createElement("div");
  item.className = "summary-item";
  const strong = document.createElement("span"); strong.className = "summary-value"; strong.textContent = value;
  const text = document.createElement("span"); text.className = "summary-label"; text.textContent = label;
  item.append(strong, text);
  document.querySelector("#source-summary").append(item);
}

function renderSummary(data) {
  const container = document.querySelector("#source-summary");
  container.replaceChildren();
  const s = data.source_summary || {};
  if (data.schema_version === 2) {
    addSummary(number(s.advertisers || (data.advertisers || []).length), "Advertisers");
    addSummary(number(allVideos.length), "Videos");          // true count = what "All" shows
    addSummary(number(suggestions.length), "Weekly picks");
  }
}

// Per-tab filter presets. "New" is the filtered view; "All" starts with every
// filter off so it shows the whole library until the user narrows it.
const FILTER_DEFAULTS = {
  new: { views: "1000000", minLen: "2", maxLen: "5", after: "any" },
  all: { views: "0", minLen: "0", maxLen: "999", after: "any" },
};

function applyFilterState(tab) {
  const d = FILTER_DEFAULTS[tab] || FILTER_DEFAULTS.new;
  document.querySelectorAll("#views-chips .chip").forEach((c) => c.classList.toggle("is-active", c.dataset.views === d.views));
  const custom = document.querySelector("#f-min-views");
  custom.hidden = true; custom.value = d.views;
  document.querySelector("#f-min-len").value = d.minLen;
  document.querySelector("#f-max-len").value = d.maxLen;
  document.querySelectorAll("#after-chips .chip").forEach((c) => c.classList.toggle("is-active", c.dataset.after === d.after));
  document.querySelector("#f-after").value = "";
}

function matchesSearch(video, adv) {
  const q = (document.querySelector("#f-search").value || "").trim().toLowerCase();
  if (!q) return true;
  return [video.title, adv, video.product].some((t) => (t || "").toLowerCase().includes(q));
}

// ----- state (server, with localStorage fallback) -----
function loadLocalStates() {
  try { return JSON.parse(localStorage.getItem(LS_KEY) || "{}"); } catch { return {}; }
}
function saveLocalStates() {
  try { localStorage.setItem(LS_KEY, JSON.stringify(states)); } catch { /* ignore */ }
}
async function loadStates() {
  states = loadLocalStates();
  try {
    const r = await fetch("/api/item-states", { cache: "no-store", signal: AbortSignal.timeout(10000) });
    if (r.ok) {
      const data = await r.json();
      if (data && data.states) { states = data.states; saveLocalStates(); }
    }
  } catch { /* offline: keep localStorage copy */ }
}
async function setState(videoId, state) {
  if (state === "new") delete states[videoId]; else states[videoId] = state;
  saveLocalStates();
  render();
  try {
    await fetch("/api/item-state", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ video_id: videoId, state }), signal: AbortSignal.timeout(10000),
    });
  } catch { /* stays in localStorage; will reconcile next load */ }
}

// ----- data -----
function flatten(data) {
  const advertisers = data.schema_version === 2 ? (data.advertisers || []) : [data];
  const out = [];
  for (const a of advertisers) {
    for (const v of a.videos || []) out.push({ video: v, advertiserName: a.advertiser.name });
  }
  return out;
}

function passesFilters(video) {
  const minLen = Number(document.querySelector("#f-min-len").value) * 60 || 0;
  const maxLen = Number(document.querySelector("#f-max-len").value) * 60 || Infinity;
  const minViews = Number(document.querySelector("#f-min-views").value) || 0;
  const afterVal = document.querySelector("#f-after").value;
  if (video.length_seconds < minLen || video.length_seconds > maxLen) return false;
  if (video.views < minViews) return false;
  if (afterVal && new Date(video.upload_date) < new Date(afterVal)) return false;
  if (advertiserName !== "all") return true; // advertiser filter applied in selection below
  return true;
}

function visibleEntries() {
  if (activeTab === "suggestions") {
    return suggestions
      .filter((s) => !states[s.video_id])  // drop any that became Used / Not relevant
      .map((s) => ({ video: { ...s, lifetime_views_per_day: s.views_per_day }, advertiserName: s.channel, reason: s.reason }));
  }
  let entries = allVideos.filter((e) => advertiserName === "all" || e.advertiserName === advertiserName);
  if (activeTab === "used") {
    entries = entries.filter((e) => states[e.video.video_id] === "used");
  } else if (activeTab === "all") {
    // Every video in the library (any language/length, any state), narrowed only by
    // the filters/search the user sets — all filters start off.
    entries = entries.filter((e) => passesFilters(e.video) && matchesSearch(e.video, e.advertiserName));
  } else {
    // New = English swipe-worthy only, not yet triaged, matching the filters.
    entries = entries.filter((e) => e.video.english === true && !states[e.video.video_id]);
    entries = entries.filter((e) => passesFilters(e.video) && matchesSearch(e.video, e.advertiserName));
  }
  return entries.sort((a, b) =>
    b.video.lifetime_views_per_day - a.video.lifetime_views_per_day ||
    b.video.views - a.video.views ||
    a.video.video_id.localeCompare(b.video.video_id));
}

function renderCard(entry) {
  const { video, advertiserName: adv } = entry;
  const card = template.content.firstElementChild.cloneNode(true);
  const iframe = card.querySelector("iframe");
  iframe.title = `${video.title} — YouTube video player`;
  const launch = card.querySelector(".video-launch");
  const thumbnail = card.querySelector(".thumbnail");
  thumbnail.src = safeUrl(video.thumbnail_url, "i.ytimg.com");
  thumbnail.alt = `Thumbnail for ${video.title}`;
  launch.setAttribute("aria-label", `Play ${video.title}`);
  launch.addEventListener("click", () => {
    iframe.src = `${safeUrl(video.embed_url, "www.youtube-nocookie.com")}?autoplay=1`;
    card.querySelector(".embed").classList.add("playing");
    launch.remove();
  }, { once: true });
  card.querySelector(".product").textContent = video.product || "";
  card.querySelector(".reason").textContent = entry.reason || "";
  card.querySelector(".title").textContent = video.title;
  card.querySelector(".advertiser").textContent = adv;
  card.querySelector(".views-day").textContent = `${number(Math.round(video.lifetime_views_per_day))}/day`;
  card.querySelector(".views").textContent = number(video.views);
  card.querySelector(".upload-date").textContent = date(video.upload_date);
  card.querySelector(".duration").textContent = duration(video.length_seconds);
  card.querySelector(".youtube-link").href = safeUrl(video.youtube_url, "www.youtube.com");
  card.querySelector(".transparency-link").href = safeUrl(video.transparency_url, "adstransparency.google.com");
  const leave = (state) => {
    card.classList.add("leaving");                 // fade/slide the card out, then persist + re-render
    setTimeout(() => setState(video.video_id, state), 330);
  };
  card.querySelector(".act-used").addEventListener("click", () => leave("used"));
  card.querySelector(".act-hide").addEventListener("click", () => leave("not_relevant"));
  card.querySelector(".act-restore").addEventListener("click", () => leave("new"));
  return card;
}

function render() {
  grid.dataset.tab = activeTab;
  const headings = { new: "New", all: "All", used: "Used", suggestions: "Suggestions" };
  const notes = {
    new: "English-only, 2–5 min, 1M+ views, grouped by product. Ranked by lifetime views/day.",
    all: "Every video in the library. Search and filter are off by default — narrow it however you like.",
    used: "Videos you marked Used. Ranked by lifetime views/day.",
    suggestions: "AI-picked swipes for Coolina, refreshed twice a week — each with a one-line reason.",
  };
  document.querySelector("#rank-heading").textContent = headings[activeTab] || "New";
  note.textContent = notes[activeTab] || notes.new;
  document.querySelector("#filters").style.display = (activeTab === "new" || activeTab === "all") ? "" : "none";
  // counts
  const newCount = allVideos.filter((e) => {
    const v = e.video;                                       // New = English swipe pool
    return v.english === true && v.views >= 1_000_000 && v.length_seconds >= 120 &&
      v.length_seconds <= 300 && !states[v.video_id];
  }).length;
  const usedCount = allVideos.filter((e) => states[e.video.video_id] === "used").length;
  const sugCount = suggestions.filter((s) => !states[s.video_id]).length;
  document.querySelector('[data-count="new"]').textContent = newCount ? `(${newCount})` : "";
  document.querySelector('[data-count="all"]').textContent = allVideos.length ? `(${allVideos.length})` : "";
  document.querySelector('[data-count="used"]').textContent = usedCount ? `(${usedCount})` : "";
  document.querySelector('[data-count="suggestions"]').textContent = sugCount ? `(${sugCount})` : "";
  positionIndicator();
  const entries = visibleEntries();
  grid.replaceChildren();
  if (!entries.length) {
    const empty = document.createElement("p"); empty.className = "empty";
    empty.textContent = activeTab === "used" ? "No videos marked Used yet." : "No videos match the current filters.";
    grid.append(empty);
    return;
  }
  entries.forEach((e) => grid.append(renderCard(e)));
}

function populateAdvertisers() {
  advertiserSelect.replaceChildren();
  const names = [...new Set(allVideos.map((e) => e.advertiserName))].sort();
  const optAll = document.createElement("option"); optAll.value = "all"; optAll.textContent = `All brands — ${number(allVideos.length)} videos`;
  advertiserSelect.append(optAll);
  for (const name of names) {
    const o = document.createElement("option"); o.value = name;
    o.textContent = `${name} — ${number(allVideos.filter((e) => e.advertiserName === name).length)} videos`;
    advertiserSelect.append(o);
  }
  advertiserSelect.closest(".advertiser-picker").hidden = names.length <= 1 && false;
  advertiserSelect.addEventListener("change", () => { advertiserName = advertiserSelect.value; render(); });
}

document.querySelectorAll(".tab").forEach((btn) => btn.addEventListener("click", () => {
  activeTab = btn.dataset.tab;
  document.querySelectorAll(".tab").forEach((b) => { b.classList.toggle("is-active", b === btn); b.setAttribute("aria-selected", b === btn ? "true" : "false"); });
  document.querySelector(".segmented").dataset.active = activeTab;  // slide the pill indicator
  if (activeTab === "new" || activeTab === "all") applyFilterState(activeTab);  // New = filtered, All = everything
  render();
}));

// Search box (title / channel / product) — live filter on New and All
document.querySelector("#f-search").addEventListener("input", render);

// Min-views preset chips (+ Custom) write to the hidden #f-min-views
document.querySelectorAll("#views-chips .chip").forEach((chip) => chip.addEventListener("click", () => {
  document.querySelectorAll("#views-chips .chip").forEach((c) => c.classList.toggle("is-active", c === chip));
  const custom = document.querySelector("#f-min-views");
  if (chip.dataset.views === "custom") { custom.hidden = false; custom.focus(); }
  else { custom.hidden = true; custom.value = chip.dataset.views; }
  render();
}));
document.querySelector("#f-min-views").addEventListener("input", render);

// Length preset dropdowns
document.querySelector("#f-min-len").addEventListener("change", render);
document.querySelector("#f-max-len").addEventListener("change", render);

// Uploaded-after preset chips (+ explicit date picker) write to #f-after
document.querySelectorAll("#after-chips .chip").forEach((chip) => chip.addEventListener("click", () => {
  document.querySelectorAll("#after-chips .chip").forEach((c) => c.classList.toggle("is-active", c === chip));
  const dateInput = document.querySelector("#f-after");
  if (chip.dataset.after === "any") { dateInput.value = ""; }
  else { const d = new Date(); d.setDate(d.getDate() - Number(chip.dataset.after)); dateInput.value = d.toISOString().slice(0, 10); }
  render();
}));
document.querySelector("#f-after").addEventListener("change", () => {
  document.querySelectorAll("#after-chips .chip").forEach((c) => c.classList.remove("is-active"));
  render();
});

async function init() {
  try {
    const response = await fetch("data/library.json", { cache: "no-store" });
    if (!response.ok) throw new Error(`Data request failed: ${response.status}`);
    library = await response.json();
    allVideos = flatten(library);
    await loadStates();
    try { const sr = await fetch("data/suggestions.json", { cache: "no-store" }); if (sr.ok) suggestions = (await sr.json()).suggestions || []; } catch { /* none generated yet */ }
    renderSummary(library);
    populateAdvertisers();
    document.querySelector("#snapshot-label").textContent = library.snapshot_at ? `Snapshot ${date(library.snapshot_at)}` : "";
    render();
  } catch (error) {
    note.textContent = "Library data is unavailable.";
    const empty = document.createElement("p"); empty.className = "empty"; empty.textContent = error.message;
    grid.replaceChildren(empty);
  }
}
init();
window.addEventListener("resize", positionIndicator);

// ----- add-brand panel (unchanged endpoints: /api/brands, /api/add-brand) -----
const brandForm = document.querySelector('#add-brand-form');
const brandStatus = document.querySelector('#brand-status');
const brandQueue = document.querySelector('#brand-queue');
let submittedBrand = null;
let refreshTimer = null;
async function refreshBrands() {
  try {
    const response = await fetch('/api/brands', { cache: 'no-store', signal: AbortSignal.timeout(15000) });
    if (!response.ok) throw new Error('Collection status is temporarily unavailable.');
    const result = await response.json();
    const labels = { queued: 'Queued for automatic collection', collecting: 'Collecting now', complete: 'Published · weekly refresh enabled', failed: 'Collection failed · existing videos unchanged', inactive: 'Paused by owner' };
    brandQueue.replaceChildren();
    for (const brand of result.brands) {
      if (brand.state === 'complete' && brand.advertiser_id !== submittedBrand) continue;
      const item = document.createElement('li');
      item.textContent = `${brand.name}: ${labels[brand.state] || 'Status unavailable'}`;
      brandQueue.append(item);
      if (brand.advertiser_id === submittedBrand) {
        brandStatus.textContent = `${brand.name}: ${labels[brand.state] || 'Status unavailable'}. ${brand.message || ''}`;
        if (brand.state === 'complete') {
          const link = document.createElement('a'); link.href = './?updated=' + Date.now(); link.textContent = ' Reload the library to see its videos.';
          brandStatus.append(link);
        }
      }
    }
    if (refreshTimer) clearTimeout(refreshTimer);
    if (result.brands.some(brand => ['queued', 'collecting'].includes(brand.state))) refreshTimer = setTimeout(refreshBrands, 10000);
  } catch (error) {
    brandStatus.textContent = error.message + ' A saved submission stays queued; you do not need to resubmit.';
  }
}
brandForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const submit = brandForm.querySelector('button');
  if (submit.disabled) return;
  submit.disabled = true;
  brandStatus.textContent = 'Saving brand…';
  try {
    const response = await fetch('/api/add-brand', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: document.querySelector('#brand-name').value.trim(), url: document.querySelector('#brand-url').value.trim() }),
      signal: AbortSignal.timeout(20000)
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Could not save this brand. Please try again.');
    submittedBrand = result.brand.advertiser_id;
    brandStatus.textContent = result.created ? 'Saved. Automatic collection is queued; videos are not ready yet.' : 'This advertiser is already in the library. No duplicate was added.';
    await refreshBrands();
  } catch (error) {
    brandStatus.textContent = `${error.message} If the connection was interrupted, retrying the same advertiser will not add a duplicate.`;
  } finally { submit.disabled = false; }
});
document.querySelector('.add-brand-panel').addEventListener('toggle', (event) => { if (event.target.open) refreshBrands(); });
