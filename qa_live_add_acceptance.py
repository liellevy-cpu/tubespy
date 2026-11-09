"""Bounded live acceptance: exactly two non-creating POSTs; no publication."""
import json, hashlib, subprocess, re, datetime
from pathlib import Path
from brand_worker import owner_get, LIVE
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parent
OUT=ROOT/'build/live-add-acceptance';OUT.mkdir(exist_ok=True)
if (OUT/'receipt.json').exists():
    raise SystemExit('Receipt exists: refusing to repeat the two POST probes. Use qa_live_add_browser.py for read-only QA.')
def request(path, method='GET', body=None):
    cmd=['curl','-sS','--max-time','40','-X',method,'-w','\n%{http_code}',LIVE+path]
    if body is not None:cmd+=['-H','Content-Type: application/json','--data',json.dumps(body)]
    result=subprocess.run(cmd,capture_output=True,check=True).stdout
    data,status=result.rsplit(b'\n',1)
    return int(status),data
receipt={'checked_at':datetime.datetime.now(datetime.timezone.utc).isoformat()}
metadata=owner_get('publish/cobalt-violet-hs8j')
receipt['metadata']=metadata
receipt['access']=owner_get('publish/cobalt-violet-hs8j/access')
receipt['assets']={}
secrets=[(ROOT/'build/brand-service.token').read_bytes().strip(),(Path.home()/'.herenow/credentials').read_bytes().strip()]
for name in ['index.html','app.js','styles.css','data/library.json']:
    status,data=request(name)
    expected=(ROOT/'build/add-brand-live-release/stage'/name).read_bytes()
    receipt['assets'][name]={'status':status,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),'stage_equal':data==expected,'known_secret_found':any(s in data for s in secrets)}
    if name=='data/library.json':
        library=json.loads(data)
        receipt['library_advertisers']=len(library['advertisers'])
        receipt['library_keys']=list(library['advertisers'][0])
status,before=request('api/brands');brands=json.loads(before)['brands']
receipt['get']={'status':status,'count':len(brands)}
existing=brands[0]
status,data=request('api/add-brand','POST',{'name':'Acceptance rename must not apply','url':existing['url']})
receipt['duplicate']={'status':status,'response':json.loads(data),'original':existing}
status,data=request('api/add-brand','POST',{'name':'Invalid URL must not save','url':'https://example.com/advertiser/AR1234567890123'})
receipt['invalid']={'status':status,'response':json.loads(data)}
status,after=request('api/brands');receipt['registry_unchanged']=json.loads(before)==json.loads(after)
receipt['denied_routes']={}
for method,path in [('PATCH','api/add-brand'),('DELETE','api/add-brand'),('PUT','api/brands'),('GET','api/add-brand'),('GET','api/brands/extra')]:
    status,data=request(path,method)
    receipt['denied_routes'][method+' '+path]={'status':status,'body':data[:150].decode(errors='replace')}
receipt['private_paths']={}
for path in ['.herenow/proxy.json','build/brand-service.token','brand_worker.py']:
    status,data=request(path);receipt['private_paths'][path]={'status':status,'known_secret_found':any(s in data for s in secrets)}
status,data=request('data/library.json');receipt['library_preserved_after_posts']=hashlib.sha256(data).hexdigest()==receipt['assets']['data/library.json']['sha256']
(OUT/'receipt.json').write_text(json.dumps(receipt,indent=2))
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True,executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    page=browser.new_page(viewport={'width':1440,'height':1000});errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    page.goto(LIVE+'?acceptance=20260929',wait_until='networkidle')
    receipt['browser']={'title':page.title(),'buttons':page.locator('button').all_text_contents(),'card_count':page.locator('video').count()}
    page.locator('.add-brand-panel > summary').click()
    receipt['browser']['form_inputs']=page.locator('input').evaluate_all('(xs)=>xs.map(x=>({id:x.id,placeholder:x.placeholder,type:x.type}))')
    page.screenshot(path=str(OUT/'desktop.png'),full_page=True)
    page.set_viewport_size({'width':390,'height':844});page.wait_for_timeout(300)
    receipt['browser']['mobile']=page.evaluate('({width:innerWidth,scrollWidth:document.documentElement.scrollWidth,forms:[...document.forms].map(x=>({text:x.innerText,rect:x.getBoundingClientRect().toJSON()}))})')
    receipt['browser']['errors']=errors
    page.screenshot(path=str(OUT/'mobile.png'),full_page=True)
    browser.close()
(OUT/'receipt.json').write_text(json.dumps(receipt,indent=2))
print(json.dumps(receipt,indent=2))
