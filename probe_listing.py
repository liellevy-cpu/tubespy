import asyncio, json, re, sys
from playwright.async_api import async_playwright
VIDEO_RE=re.compile(r'(?:youtube(?:-nocookie)?\.com/(?:embed/|watch\?(?:[^#]*&)?v=)|youtu\.be/)([A-Za-z0-9_-]{11})',re.I)
async def main(url):
 async with async_playwright() as p:
  b=await p.chromium.launch(headless=True,executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
  page=await b.new_page(); seen=[]; page.on('request',lambda r:seen.append(r.url))
  await page.goto(url,wait_until='domcontentloaded',timeout=90000); await page.wait_for_timeout(3000)
  out=[]
  for i in range(12):
   hrefs=await page.locator('a[href*="/creative/"]').evaluate_all('els=>els.map(a=>a.href)')
   vals=list(seen)+[f.url for f in page.frames]+[await page.content()]
   ids=sorted({m.group(1) for v in vals for m in VIDEO_RE.finditer(v or '')})
   out.append({'round':i,'creative_links':len(set(hrefs)),'video_ids':len(ids),'requests':len(seen)})
   await page.evaluate('window.scrollTo(0,document.body.scrollHeight)'); await page.wait_for_timeout(1000)
  print(json.dumps(out,indent=2)); await b.close()
asyncio.run(main(sys.argv[1]))
