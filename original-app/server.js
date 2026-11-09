const express = require("express");
const http = require("http");
const { Server } = require("socket.io");
const { chromium } = require("playwright-extra");
const stealth = require("puppeteer-extra-plugin-stealth")();
chromium.use(stealth);
const { createObjectCsvWriter } = require("csv-writer");
const cron = require("node-cron");
const fs = require("fs");
const path = require("path");

const app = express();
const server = http.createServer(app);
const io = new Server(server);

const DATA_DIR = path.join(__dirname, "data");
const ADV_FILE = path.join(DATA_DIR, "advertisers.json");
const RUNS_FILE = path.join(DATA_DIR, "runs.json");
const CSV_DIR = path.join(__dirname, "csv");

if (!fs.existsSync(CSV_DIR)) fs.mkdirSync(CSV_DIR);

app.use(express.json());
app.use(express.static(path.join(__dirname, "public")));
app.use("/csv", express.static(CSV_DIR));

// ─── DATA HELPERS ─────────────────────────────────────────────────
function loadJSON(filePath) {
  try {
    return JSON.parse(fs.readFileSync(filePath, "utf8"));
  } catch {
    return [];
  }
}

function saveJSON(filePath, data) {
  fs.writeFileSync(filePath, JSON.stringify(data, null, 2));
}

function todayISO() {
  return new Date().toISOString().slice(0, 10);
}

// Human-like random delay (ms)
function randomDelay(min = 1000, max = 3000) {
  return Math.floor(Math.random() * (max - min)) + min;
}

// ─── API ROUTES ───────────────────────────────────────────────────
app.get("/api/advertisers", (req, res) => {
  res.json(loadJSON(ADV_FILE));
});

app.post("/api/advertisers", (req, res) => {
  const { name, url } = req.body;
  if (!name || !url) return res.status(400).json({ error: "Name and URL required" });
  const list = loadJSON(ADV_FILE);
  list.push({ name: name.trim(), url: url.trim() });
  saveJSON(ADV_FILE, list);
  res.json({ ok: true, advertisers: list });
});

app.put("/api/advertisers/:index", (req, res) => {
  const list = loadJSON(ADV_FILE);
  const idx = parseInt(req.params.index);
  if (idx < 0 || idx >= list.length) return res.status(404).json({ error: "Not found" });
  const { name, url } = req.body;
  if (name) list[idx].name = name.trim();
  if (url) list[idx].url = url.trim();
  saveJSON(ADV_FILE, list);
  res.json({ ok: true, advertisers: list });
});

app.delete("/api/advertisers/:index", (req, res) => {
  const list = loadJSON(ADV_FILE);
  const idx = parseInt(req.params.index);
  if (idx < 0 || idx >= list.length) return res.status(404).json({ error: "Not found" });
  list.splice(idx, 1);
  saveJSON(ADV_FILE, list);
  res.json({ ok: true, advertisers: list });
});

app.get("/api/runs", (req, res) => {
  const runs = loadJSON(RUNS_FILE);
  res.json(runs.slice(-50)); // last 50 runs
});

app.get("/api/runs/:id", (req, res) => {
  const runs = loadJSON(RUNS_FILE);
  const run = runs.find((r) => r.id === req.params.id);
  if (!run) return res.status(404).json({ error: "Not found" });
  res.json(run);
});

app.get("/api/comparison", (req, res) => {
  const runs = loadJSON(RUNS_FILE);
  if (runs.length < 2) return res.json({ available: false });

  const latest = runs[runs.length - 1];
  const previous = runs[runs.length - 2];

  const comparison = buildComparison(previous, latest);
  res.json({ available: true, latest, previous, comparison });
});

app.get("/api/schedule", (req, res) => {
  res.json({ schedule: currentSchedule });
});

app.post("/api/schedule", (req, res) => {
  const { enabled, day, hour } = req.body;
  currentSchedule = { enabled: !!enabled, day: day || 1, hour: hour || 8 };
  setupCron();
  res.json({ ok: true, schedule: currentSchedule });
});

// ─── COMPARISON LOGIC ─────────────────────────────────────────────
function buildComparison(prev, curr) {
  const results = [];

  const prevMap = {};
  for (const ad of prev.results || []) {
    if (ad.youtubeUrl && ad.views) {
      prevMap[ad.youtubeUrl] = {
        views: parseInt(ad.views) || 0,
        advertiser: ad.advertiser,
      };
    }
  }

  const prevAdCount = {};
  const currAdCount = {};
  for (const ad of prev.results || []) {
    prevAdCount[ad.advertiser] = (prevAdCount[ad.advertiser] || 0) + 1;
  }
  for (const ad of curr.results || []) {
    currAdCount[ad.advertiser] = (currAdCount[ad.advertiser] || 0) + 1;
  }

  const allAdvertisers = new Set([
    ...Object.keys(prevAdCount),
    ...Object.keys(currAdCount),
  ]);

  for (const adv of allAdvertisers) {
    const prevCount = prevAdCount[adv] || 0;
    const currCount = currAdCount[adv] || 0;
    const newAds = currCount - prevCount;

    const advVideos = (curr.results || []).filter(
      (a) => a.advertiser === adv && a.youtubeUrl && a.views
    );
    let totalViewGrowth = 0;
    let videosWithGrowth = 0;

    for (const vid of advVideos) {
      const prevData = prevMap[vid.youtubeUrl];
      if (prevData) {
        const growth = (parseInt(vid.views) || 0) - prevData.views;
        if (growth > 0) {
          totalViewGrowth += growth;
          videosWithGrowth++;
        }
      }
    }

    results.push({
      advertiser: adv,
      prevAdCount: prevCount,
      currAdCount: currCount,
      newAds,
      totalViewGrowth,
      videosWithGrowth,
    });
  }

  results.sort((a, b) => b.totalViewGrowth - a.totalViewGrowth);
  return results;
}

// ─── SCRAPING ENGINE ──────────────────────────────────────────────
let isRunning = false;
let cancelRequested = false;
let activeBrowser = null;

function normalizeYouTubeUrl(url) {
  if (!url) return "";
  if (url.startsWith("/")) return "https://www.youtube.com" + url;
  return url;
}

function isYouTubeVideoUrl(url) {
  if (!url) return false;
  return (
    url.includes("youtube.com/watch?v=") ||
    url.includes("youtube.com/shorts/") ||
    url.includes("youtu.be/")
  );
}

function isYouTubeChannelUrl(url) {
  if (!url) return false;
  const u = url.toLowerCase();
  return (
    u.includes("youtube.com/channel/") ||
    u.includes("youtube.com/@") ||
    u.includes("youtube.com/c/") ||
    u.includes("youtube.com/user/")
  );
}

async function dismissSurvey(page) {
  try {
    const frameLocator = page.frameLocator(
      'iframe[id*="google-hats-survey"], iframe[name="google-hats-survey"]'
    );
    const closeBtn = frameLocator.getByRole("button", {
      name: /close|no thanks|dismiss|not now|close user survey/i,
    });
    if (await closeBtn.count()) {
      await closeBtn.first().click({ timeout: 1500 });
      await page.waitForTimeout(250);
    }
  } catch {}
}

async function scrollAndCollectAdLinks(page, maxScrolls = 80) {
  const adSelector = 'a[href^="/advertiser/"][aria-label^="Advertisement ("]';
  const collectedHrefs = new Set();

  async function collectVisible() {
    const hrefs = await page.locator(adSelector)
      .evaluateAll((els) => els.map((a) => a.getAttribute("href")).filter(Boolean))
      .catch(() => []);
    for (const h of hrefs) collectedHrefs.add(h);
  }

  let lastHeight = 0;
  let stableRounds = 0;

  for (let i = 0; i < maxScrolls; i++) {
    await dismissSurvey(page);
    await collectVisible();

    await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
    await page.waitForTimeout(4000);

    const newHeight = await page.evaluate(() => document.body.scrollHeight);
    if (newHeight === lastHeight) {
      stableRounds++;
      if (stableRounds >= 5) break;
    } else {
      stableRounds = 0;
      lastHeight = newHeight;
    }
  }

  await collectVisible();

  const links = [
    ...new Set(
      [...collectedHrefs].map((h) =>
        h.startsWith("http") ? h : `https://adstransparency.google.com${h}`
      )
    ),
  ];

  return links;
}

async function findYouTubeVideoInFrames(page) {
  const frames = page.frames();
  let sawChannel = false;

  for (const frame of frames) {
    try {
      const a = frame.locator(
        'a[href*="youtube.com/watch"], a[href*="youtube.com/shorts/"], a[href*="youtu.be/"]'
      );
      if (await a.count()) {
        const href = await a.first().getAttribute("href");
        const u = normalizeYouTubeUrl(href);
        if (isYouTubeVideoUrl(u)) return { url: u, sawChannel };
      }
      const ch = frame.locator(
        'a[href*="youtube.com/channel/"], a[href*="youtube.com/@"], a[href*="youtube.com/c/"], a[href*="youtube.com/user/"]'
      );
      if (await ch.count()) sawChannel = true;
    } catch {}
  }

  for (const frame of frames) {
    try {
      const watch = frame.getByRole("link", { name: /Watch on/i });
      if (await watch.count()) {
        const href = await watch.first().getAttribute("href");
        const u = normalizeYouTubeUrl(href);
        if (isYouTubeVideoUrl(u)) return { url: u, sawChannel };
        if (isYouTubeChannelUrl(u)) sawChannel = true;
      }
    } catch {}
  }

  return { url: null, sawChannel };
}

async function findYouTubeViaShare(page) {
  let sawChannel = false;

  for (const frame of page.frames()) {
    try {
      const shareBtn = frame.getByRole("button", { name: /Share/i });
      if (!(await shareBtn.count())) continue;
      await shareBtn.first().click({ timeout: 2000 });
      await page.waitForTimeout(1000);

      const ytLink = frame.locator(
        'a[href*="youtube.com/shorts/"], a[href*="youtube.com/watch"], a[href*="youtu.be/"], a[href*="youtube.com/channel/"], a[href*="youtube.com/@"], a[href*="youtube.com/c/"], a[href*="youtube.com/user/"]'
      );
      if (await ytLink.count()) {
        const href = await ytLink.first().getAttribute("href");
        const u = normalizeYouTubeUrl(href);
        if (isYouTubeVideoUrl(u)) return { url: u, sawChannel };
        if (isYouTubeChannelUrl(u)) sawChannel = true;
      }

      const input = frame.locator('input[type="text"], input[readonly], textarea');
      if (await input.count()) {
        const val = await input.first().inputValue().catch(() => "");
        const u = normalizeYouTubeUrl(val);
        if (isYouTubeVideoUrl(u)) return { url: u, sawChannel };
        if (isYouTubeChannelUrl(u)) sawChannel = true;
      }
    } catch {}
  }

  return { url: null, sawChannel };
}

async function getYouTubeStats(page, url) {
  const m = url.match(/youtube\.com\/shorts\/([\w-]+)/i);
  const finalUrl = m ? `https://www.youtube.com/watch?v=${m[1]}` : url;

  await page.goto(finalUrl, { waitUntil: "domcontentloaded", timeout: 60000 });

  const data = await page.evaluate(() => {
    const pr = window.ytInitialPlayerResponse;
    const vd = pr?.videoDetails;
    const micro = pr?.microformat?.playerMicroformatRenderer;
    return {
      viewCount: vd?.viewCount || "",
      uploadDate: micro?.uploadDate || "",
    };
  });

  return {
    views: data.viewCount ? String(data.viewCount) : "",
    uploadDate: data.uploadDate || "",
  };
}

async function runScraper(source = "manual", selectedIndices = null) {
  if (isRunning) {
    io.emit("error", "A scan is already running.");
    return null;
  }

  isRunning = true;
  cancelRequested = false;
  const allAdvertisers = loadJSON(ADV_FILE);
  
  // Filter to selected advertisers if specified
  const advertisers = selectedIndices
    ? selectedIndices.map((i) => ({ ...allAdvertisers[i], originalIndex: i })).filter(Boolean)
    : allAdvertisers.map((a, i) => ({ ...a, originalIndex: i }));
  
  const allResults = [];
  const runId = `run_${Date.now()}`;
  const startTime = Date.now();
  const advertiserTimes = [];

  io.emit("run:start", {
    id: runId,
    total: advertisers.length,
    source,
  });

  let browser;
  try {
    browser = await chromium.launch({
      headless: false,
      args: [
        "--window-size=1280,900",
        "--window-position=50,50",
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--no-focus-on-navigate",
        "--disable-blink-features=AutomationControlled",
      ],
    });
    activeBrowser = browser;
    const context = await browser.newContext({
      locale: "en-US",
      timezoneId: "America/New_York",
      viewport: { width: 1280, height: 900 },
      userAgent:
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
      extraHTTPHeaders: { "Accept-Language": "en-US,en;q=0.9" },
    });

    // Extra stealth: patch any remaining webdriver leaks
    await context.addInitScript(() => {
      // Hide webdriver flag
      Object.defineProperty(navigator, "webdriver", { get: () => undefined });
      // Fake plugins array (real Chrome has at least a few)
      Object.defineProperty(navigator, "plugins", {
        get: () => [1, 2, 3, 4, 5],
      });
      // Fake languages
      Object.defineProperty(navigator, "languages", {
        get: () => ["en-US", "en"],
      });
      // Remove Playwright-injected variables
      delete window.__playwright;
      delete window.__pw_manual;
    });

    // Reuse a single page for YouTube stats
    const ytPage = await context.newPage();

    for (let advIdx = 0; advIdx < advertisers.length; advIdx++) {
      if (cancelRequested) {
        io.emit("run:cancelled", { processedSoFar: advIdx, total: advertisers.length });
        break;
      }

      const advStartTime = Date.now();
      const { name, url: rawUrl } = advertisers[advIdx];
      const url = rawUrl.includes("hl=") ? rawUrl : rawUrl + "&hl=en";

      let estimatedRemaining = null;
      if (advertiserTimes.length > 0) {
        const avgMs = advertiserTimes.reduce((a, b) => a + b, 0) / advertiserTimes.length;
        estimatedRemaining = Math.round((avgMs * (advertisers.length - advIdx)) / 1000);
      }

      io.emit("run:advertiser", {
        index: advIdx,
        name,
        total: advertisers.length,
        status: "loading",
        estimatedRemaining,
        elapsed: Math.round((Date.now() - startTime) / 1000),
      });

      const page = await context.newPage();

      try {
        await page.goto("about:blank");
        await page.waitForTimeout(500);
        await page.goto(url, { waitUntil: "domcontentloaded", timeout: 60000 });
        await page.waitForTimeout(6000);
        await dismissSurvey(page);

        const content = await page.content();
        if (content.includes("not found") || content.includes("404")) {
          await page.goto("about:blank");
          await page.waitForTimeout(1000);
          await page.goto(url, { waitUntil: "domcontentloaded", timeout: 60000 });
          await page.waitForTimeout(6000);
          await dismissSurvey(page);
        }

        try {
          const seeAll = page.getByRole("button", { name: "See all ads" });
          await seeAll.waitFor({ state: "visible", timeout: 30000 });
          await seeAll.click();
          await page.waitForTimeout(4000);
        } catch {}

        await dismissSurvey(page);

        const adSelector = 'a[href^="/advertiser/"][aria-label^="Advertisement ("]';
        try {
          await page.locator(adSelector).first().waitFor({ state: "visible", timeout: 30000 });
        } catch {}

        const links = await scrollAndCollectAdLinks(page);

        console.log(`  ${name}: found ${links.length} ads`);

        io.emit("run:advertiser", {
          index: advIdx,
          name,
          total: advertisers.length,
          status: "scraping",
          adCount: links.length,
        });

        if (links.length === 0) {
          allResults.push({
            advertiser: name,
            adIndex: 0,
            adDetailUrl: "",
            youtubeUrl: "",
            views: "",
            uploadDate: "",
            status: "NO_ADS_FOUND",
          });
        }

        for (let i = 0; i < links.length; i++) {
          if (cancelRequested) break;
          
          const adUrl = links[i];

          // Random delay between ad visits to appear human
          if (i > 0) await page.waitForTimeout(randomDelay(1500, 3500));

          await page.goto(adUrl, { waitUntil: "domcontentloaded", timeout: 60000 });
          await dismissSurvey(page);
          await page.waitForTimeout(3000);

          let youtubeUrl = "";
          let status = "NO_YOUTUBE_VIDEO_LINK_FOUND";
          let sawChannel = false;

          for (let step = 0; step < 15; step++) {
            const a = await findYouTubeVideoInFrames(page);
            if (a.sawChannel) sawChannel = true;
            youtubeUrl = a.url || "";

            if (!youtubeUrl) {
              const b = await findYouTubeViaShare(page);
              if (b.sawChannel) sawChannel = true;
              youtubeUrl = b.url || "";
            }

            if (youtubeUrl) {
              status = "YOUTUBE_VIDEO_LINK_FOUND";
              break;
            }

            const nextBtn = page.getByRole("button", { name: "Next variation" });
            if (!(await nextBtn.count()) || (await nextBtn.isDisabled())) break;
            await nextBtn.click({ timeout: 3000 });
            await page.waitForTimeout(2000);
          }

          if (!youtubeUrl && sawChannel) status = "YOUTUBE_CHANNEL_LINK_ONLY";

          let views = "";
          let uploadDate = "";

          if (youtubeUrl) {
            try {
              await page.waitForTimeout(randomDelay(800, 2000));
              const stats = await getYouTubeStats(ytPage, youtubeUrl);
              views = stats.views;
              uploadDate = stats.uploadDate;
              status = views || uploadDate ? "OK" : "YOUTUBE_VIDEO_BUT_NO_STATS";
            } catch {
              status = "YOUTUBE_BLOCKED_OR_LOGIN_REQUIRED";
            }
          }

          allResults.push({
            advertiser: name,
            adIndex: i + 1,
            adDetailUrl: adUrl,
            youtubeUrl,
            views,
            uploadDate,
            status,
          });

          io.emit("run:ad", {
            advertiserIndex: advIdx,
            advertiserName: name,
            adIndex: i + 1,
            adTotal: links.length,
            status,
            views,
            youtubeUrl,
          });
        }

        io.emit("run:advertiser", {
          index: advIdx,
          name,
          total: advertisers.length,
          status: "done",
          adCount: links.length,
          elapsed: Math.round((Date.now() - startTime) / 1000),
        });
      } catch (err) {
        allResults.push({
          advertiser: name,
          adIndex: 0,
          adDetailUrl: "",
          youtubeUrl: "",
          views: "",
          uploadDate: "",
          status: `ERROR: ${err.message}`,
        });
        io.emit("run:advertiser", {
          index: advIdx,
          name,
          total: advertisers.length,
          status: "error",
          error: err.message,
        });
      }

      advertiserTimes.push(Date.now() - advStartTime);

      await page.close();
      await context.clearCookies();
      await new Promise((r) => setTimeout(r, 500));
    }

    await ytPage.close().catch(() => {});
    await browser.close();
    activeBrowser = null;
  } catch (err) {
    io.emit("error", `Browser error: ${err.message}`);
    if (browser) await browser.close().catch(() => {});
    activeBrowser = null;
  }

  const wasCancelled = cancelRequested;
  cancelRequested = false;

  // Save CSV
  const csvName = `scan_${todayISO()}_${Date.now()}.csv`;
  const csvPath = path.join(CSV_DIR, csvName);
  const csvWriter = createObjectCsvWriter({
    path: csvPath,
    header: [
      { id: "advertiser", title: "Advertiser" },
      { id: "adIndex", title: "Ad #" },
      { id: "adDetailUrl", title: "Ad Detail URL" },
      { id: "youtubeUrl", title: "YouTube URL" },
      { id: "views", title: "Views" },
      { id: "uploadDate", title: "Upload Date" },
      { id: "status", title: "Status" },
    ],
  });
  await csvWriter.writeRecords(allResults);

  // Save run to history
  const runData = {
    id: runId,
    date: new Date().toISOString(),
    source,
    csvFile: csvName,
    totalAds: allResults.length,
    results: allResults,
    summary: {},
  };

  for (const r of allResults) {
    if (!runData.summary[r.advertiser]) {
      runData.summary[r.advertiser] = { ads: 0, withVideo: 0, totalViews: 0 };
    }
    runData.summary[r.advertiser].ads++;
    if (r.youtubeUrl) runData.summary[r.advertiser].withVideo++;
    if (r.views) runData.summary[r.advertiser].totalViews += parseInt(r.views) || 0;
  }

  const runs = loadJSON(RUNS_FILE);
  runs.push(runData);
  if (runs.length > 100) runs.splice(0, runs.length - 100);
  saveJSON(RUNS_FILE, runs);

  isRunning = false;

  io.emit("run:complete", {
    id: runId,
    csvFile: csvName,
    totalAds: allResults.length,
    summary: runData.summary,
    elapsed: Math.round((Date.now() - startTime) / 1000),
    cancelled: wasCancelled,
  });

  return runData;
}

// ─── SOCKET.IO ────────────────────────────────────────────────────
io.on("connection", (socket) => {
  console.log("Client connected");

  socket.on("run:start", (data) => {
    const selectedIndices = data?.selectedIndices || null;
    runScraper("manual", selectedIndices);
  });

  socket.on("run:cancel", async () => {
    if (!isRunning) return;
    console.log("Cancel requested by user");
    cancelRequested = true;
    if (activeBrowser) {
      try {
        await activeBrowser.close();
        activeBrowser = null;
      } catch {}
    }
  });

  socket.on("disconnect", () => {
    console.log("Client disconnected");
  });
});

// ─── CRON SCHEDULING ──────────────────────────────────────────────
let currentSchedule = { enabled: true, day: 1, hour: 8 };
let cronJob = null;

function setupCron() {
  if (cronJob) {
    cronJob.stop();
    cronJob = null;
  }

  if (!currentSchedule.enabled) return;

  const expr = `0 ${currentSchedule.hour} * * ${currentSchedule.day}`;
  cronJob = cron.schedule(expr, () => {
    console.log("Running scheduled scan...");
    runScraper("scheduled");
  });

  const days = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
  console.log(
    `Scheduled: Every ${days[currentSchedule.day]} at ${String(currentSchedule.hour).padStart(2, "0")}:00`
  );
}

// ─── START ────────────────────────────────────────────────────────
const PORT = 3847;

server.listen(PORT, () => {
  console.log(`\n  ╔══════════════════════════════════════╗`);
  console.log(`  ║    TubeSpy is running!               ║`);
  console.log(`  ║    Open: http://localhost:${PORT}       ║`);
  console.log(`  ╚══════════════════════════════════════╝\n`);
  setupCron();
});
