import asyncio, json, re, sys
from playwright.async_api import async_playwright

VIDEO_PATTERNS = [
    re.compile(r"(?:youtube(?:-nocookie)?\.com/(?:embed/|watch\?(?:[^#]*&)?v=)|youtu\.be/)([A-Za-z0-9_-]{11})"),
    re.compile(r"%2Fembed%2F([A-Za-z0-9_-]{11})", re.I),
]

async def main(url):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        observed=[]
        page.on("request", lambda r: observed.append(r.url))
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(5000)
        vals=list(observed)
        vals.extend(f.url for f in page.frames)
        vals.extend(await page.eval_on_selector_all("*", "els => els.flatMap(e => ['src','href','data-src'].map(a => e.getAttribute && e.getAttribute(a)).filter(Boolean))"))
        vals.append(await page.content())
        ids=sorted({m.group(1) for v in vals for pat in VIDEO_PATTERNS for m in pat.finditer(v or '')})
        print(json.dumps({'url':url,'ids':ids,'frames':[f.url for f in page.frames],'observed_youtube':[v for v in observed if 'youtube' in v][:30]},indent=2))
        await browser.close()

asyncio.run(main(sys.argv[1]))
