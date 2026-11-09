import json,difflib,subprocess
from pathlib import Path
from playwright.sync_api import sync_playwright
root=Path(__file__).resolve().parent
out=root/'build/live-add-acceptance'
receipt=json.loads((out/'receipt.json').read_text())
url='https://youtube-competitor-library.coolina.here.now/'
html=subprocess.run(['curl','-fsSL',url],capture_output=True,check=True).stdout.decode()
source=(root/'public/index.html').read_text()
(out/'html-diff.txt').write_text(''.join(difflib.unified_diff(source.splitlines(True),html.splitlines(True))))
data=json.loads((root/'public/data/library.json').read_text())
receipt['retained_video_count']=sum(len(row['videos']) for row in data['advertisers'])
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True,executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    page=browser.new_page(viewport={'width':1440,'height':1000});errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    page.goto(url+'?acceptance=20260929b',wait_until='networkidle')
    page.locator('.add-brand-panel > summary').click()
    page.wait_for_timeout(800)
    receipt['browser']={'title':page.title(),'cards':page.locator('.video-card').count(),'queue_rows':page.locator('#brand-queue li').count(),'form_visible':page.locator('#add-brand-form').is_visible(),'text':page.locator('.add-brand-panel').inner_text()}
    page.screenshot(path=str(out/'desktop.png'),full_page=True)
    page.set_viewport_size({'width':390,'height':844});page.wait_for_timeout(300)
    receipt['browser']['mobile']=page.evaluate('({width:innerWidth,scrollWidth:document.documentElement.scrollWidth,form:document.querySelector("#add-brand-form").getBoundingClientRect().toJSON()})')
    receipt['browser']['errors']=errors
    page.screenshot(path=str(out/'mobile.png'),full_page=True)
    browser.close()
(out/'receipt.json').write_text(json.dumps(receipt,indent=2))
print(json.dumps({'browser':receipt['browser'],'retained_video_count':receipt['retained_video_count']},indent=2))
