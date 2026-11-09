"""Bounded read-only probe of retained NORTEX detail evidence; never publishes."""
import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright
from brand_worker import RestrictedBrowser
from collect_all import VIDEO_RE, ENCODED_VIDEO_RE

ROOT=Path(__file__).resolve().parent
async def main():
    source=ROOT/'build/brand-runs/6400e7dc845341c3bd892315fc119746/raw/ar16514522125000769537/creative_video_results.jsonl'
    rows=[json.loads(line) for line in source.read_text().splitlines()]
    examples=[next(row for row in rows if not row['video_ids']),next(row for row in rows if row['video_ids'])]
    out=ROOT/'build/nortex-diagnosis';out.mkdir(exist_ok=True)
    results=[]
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True,executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
        context=await RestrictedBrowser(browser).new_context(locale='en-US')
        try:
            for i,row in enumerate(examples):
                page=await context.new_page();requests=[];responses=[]
                page.on('request',lambda request:requests.append(request.url))
                page.on('response',lambda response:responses.append({'url':response.url.split('?')[0],'status':response.status}))
                response=await page.goto(row['creative_url'],wait_until='domcontentloaded',timeout=75000)
                samples=[]
                for delay in [2500,12500]:
                    await page.wait_for_timeout(delay)
                    values=requests+[frame.url for frame in page.frames]+[await page.content()]
                    ids=sorted({m.group(1) for value in values for pattern in [VIDEO_RE,ENCODED_VIDEO_RE] for m in pattern.finditer(value)})
                    body=await page.locator('body').inner_text()
                    samples.append({'wait_ms':delay,'video_ids':ids,'body':body[:12000],'frames':[f.url for f in page.frames]})
                frame_evidence=[]
                for frame_index,frame in enumerate(page.frames):
                    try:
                        html=await frame.content()
                        (out/f'example-{i}-frame-{frame_index}.html').write_text(html)
                        frame_evidence.append({'index':frame_index,'url':frame.url.split('#')[0], 'text':(await frame.locator('body').inner_text())[:500], 'video_ids':sorted({m.group(1) for pattern in [VIDEO_RE,ENCODED_VIDEO_RE] for m in pattern.finditer(html)})})
                    except Exception as exc:frame_evidence.append({'index':frame_index,'error':type(exc).__name__})
                samples[-1]['frame_evidence']=frame_evidence
                await page.screenshot(path=str(out/f'example-{i}.png'),full_page=True)
                (out/f'example-{i}.html').write_text(await page.content())
                results.append({'url':row['creative_url'],'previous_ids':row['video_ids'],'http_status':response.status,'samples':samples,'responses':responses})
                await page.close()
        finally:await browser.close()
    (out/'receipt.json').write_text(json.dumps(results,indent=2))
    for result in results:
        print(json.dumps({k:v for k,v in result.items() if k!='responses'},indent=2))
if __name__=='__main__':asyncio.run(main())
