"use strict";

const grid = document.querySelector("#video-grid");
const template = document.querySelector("#video-card-template");
const note = document.querySelector("#ranking-note");
const advertiserSelect = document.querySelector("#advertiser-select");
const scopeLabel = document.querySelector("#scope-label");
let library = null;
let activeAdvertiser = null;
let activeRanking = "most_viewed";

function safeUrl(raw, allowedHost) {
  const parsed = new URL(raw);
  if (parsed.protocol !== "https:" || parsed.hostname !== allowedHost || parsed.username || parsed.password) {
    throw new Error("Unsafe source URL");
  }
  return parsed.href;
}

function number(value) {
  return new Intl.NumberFormat("en-US").format(value);
}

function date(value) {
  return new Intl.DateTimeFormat("en-US", { year: "numeric", month: "short", day: "numeric" }).format(new Date(value));
}

function duration(seconds) {
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return `${minutes}:${String(rest).padStart(2, "0")}`;
}

function addSummary(value, label) {
  const item = document.createElement("div");
  item.className = "summary-item";
  const strong = document.createElement("span");
  strong.className = "summary-value";
  strong.textContent = value;
  const text = document.createElement("span");
  text.className = "summary-label";
  text.textContent = label;
  item.append(strong, text);
  document.querySelector("#source-summary").append(item);
}

function renderSummary(data) {
  const container = document.querySelector("#source-summary");
  container.replaceChildren();
  const source = data.source_summary;
  if (data.schema_version === 2) {
    addSummary(number(source.advertisers), "Advertisers");
    addSummary(number(source.unique_creative_pages), "Unique creative pages");
    addSummary(number(source.recovered_unique_youtube_videos), "Recovered videos");
    addSummary(number(source.youtube_metadata_failures), "Metadata failures");
  } else {
    addSummary(`~${number(source.reported_ads_approx)}`, "Reported ads");
    addSummary(number(source.unique_creative_pages), "Unique creative pages");
    addSummary(number(source.recovered_unique_youtube_videos), "Recovered videos");
    addSummary(number(source.youtube_metadata_failures), "YouTube metadata failures");
  }
}

function populateAdvertisers(data) {
  const advertisers = data.schema_version === 2 ? library.advertisers : [data];
  advertiserSelect.replaceChildren();
  if (advertisers.length > 1) {
    const option = document.createElement("option");
    option.value = "all";
    option.textContent = `All brands — ${number(advertisers.reduce((total, item) => total + item.videos.length, 0))} videos`;
    advertiserSelect.append(option);
  }
  advertisers.forEach((item, index) => {
    const option = document.createElement("option");
    option.value = String(index);
    option.textContent = `${item.advertiser.name} — ${number(item.videos.length)} videos`;
    advertiserSelect.append(option);
  });
  activeAdvertiser = advertisers.length > 1 ? null : advertisers[0];
  advertiserSelect.closest(".advertiser-picker").hidden = advertisers.length === 1;
  advertiserSelect.addEventListener("change", () => {
    activeAdvertiser = advertiserSelect.value === "all" ? null : advertisers[Number(advertiserSelect.value)];
    renderActiveAdvertiser();
  });
}

function renderCard(video, rank, advertiserName) {
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
  card.querySelector(".rank").textContent = `Rank ${rank}`;
  card.querySelector(".title").textContent = video.title;
  card.querySelector(".advertiser").textContent = advertiserName;
  card.querySelector(".views").textContent = number(video.views);
  card.querySelector(".upload-date").textContent = date(video.upload_date);
  card.querySelector(".age").textContent = `${number(video.age_days)} days`;
  card.querySelector(".views-day").textContent = `${number(video.lifetime_views_per_day)}/day`;
  card.querySelector(".duration").textContent = duration(video.length_seconds);
  card.querySelector(".youtube-link").href = safeUrl(video.youtube_url, "www.youtube.com");
  card.querySelector(".transparency-link").href = safeUrl(video.transparency_url, "adstransparency.google.com");
  return card;
}

function renderRanking() {
  activeRanking = "most_viewed";
  grid.replaceChildren();
  let entries;
  if (activeAdvertiser === null) {
    note.textContent = "Current lifetime YouTube views across all selected brand winners, descending.";
    entries = library.advertisers
      .flatMap((advertiser) => advertiser.videos.map((video) => ({ video, advertiser })))
      .sort((a, b) => b.video.views - a.video.views || a.video.video_id.localeCompare(b.video.video_id));
  } else {
    note.textContent = activeAdvertiser.rankings.definitions.most_viewed;
    const videos = new Map(activeAdvertiser.videos.map((video) => [video.video_id, video]));
    entries = activeAdvertiser.rankings.most_viewed.map((id) => ({ video: videos.get(id), advertiser: activeAdvertiser }));
  }
  if (!entries.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "No verified YouTube videos are available in the current snapshot.";
    grid.append(empty);
    return;
  }
  entries.forEach((entry, index) => grid.append(renderCard(entry.video, index + 1, entry.advertiser.advertiser.name)));
}

function renderActiveAdvertiser() {
  if (activeAdvertiser === null) {
    document.querySelector("#rank-heading").textContent = "All brands";
    document.querySelector("#snapshot-label").textContent = `Snapshot ${date(library.snapshot_at)} · ${number(library.advertisers.reduce((total, item) => total + item.videos.length, 0))} videos`;
    scopeLabel.textContent = "All selected videos ranked by current YouTube views";
  } else {
    document.querySelector("#rank-heading").textContent = activeAdvertiser.advertiser.name;
    document.querySelector("#snapshot-label").textContent = `Snapshot ${date(activeAdvertiser.snapshot_at)} · top ${number(activeAdvertiser.videos.length)} videos`;
    scopeLabel.textContent = "Top five by current YouTube views";
  }
  renderRanking();
}
fetch("data/library.json", { cache: "no-store" })
  .then((response) => {
    if (!response.ok) throw new Error(`Data request failed: ${response.status}`);
    return response.json();
  })
  .then((data) => {
    library = data;
    renderSummary(data);
    populateAdvertisers(data);
    renderActiveAdvertiser();
  })
  .catch((error) => {
    note.textContent = "Library data is unavailable.";
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = error.message;
    grid.replaceChildren(empty);
  });

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
