"""Local real-browser + real-HTTP acceptance; failure is deliberately injected."""
import json
from pathlib import Path
import secrets
import tempfile
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import urllib.request
import urllib.error
from playwright.sync_api import sync_playwright
from add_brand import Store
from brand_service import make_server, Worker

ROOT=Path(__file__).resolve().parent

def main():
    with tempfile.TemporaryDirectory(prefix='youtube-add-qa-') as temp:
        store=Store(Path(temp)/'q');token=secrets.token_urlsafe(32)
        backend=make_server(store,token,0)
        def fail(item):raise RuntimeError('Injected source failure for preservation acceptance test')
        worker=Worker(store,fail)
        class UI(SimpleHTTPRequestHandler):
            def __init__(self,*a,**kw):super().__init__(*a,directory=str(ROOT/'public'),**kw)
            def log_message(self,*a):pass
            def proxy(self):
                payload=self.rfile.read(int(self.headers.get('Content-Length','0'))) if self.command=='POST' else None
                request=urllib.request.Request(f'http://127.0.0.1:{backend.server_port}/brands',data=payload,method=self.command,headers={'Content-Type':'application/json','Authorization':'Bearer '+token})
                try:
                    with urllib.request.urlopen(request) as r: code,data=r.status,r.read()
                except urllib.error.HTTPError as e:code,data=e.code,e.read()
                self.send_response(code);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(data)
            def do_GET(self):
                if self.path=='/api/brands':return self.proxy()
                return super().do_GET()
            def do_POST(self):
                if self.path=='/api/add-brand':return self.proxy()
                self.send_error(404)
        ui=ThreadingHTTPServer(('127.0.0.1',0),UI)
        for server in (backend,ui):threading.Thread(target=server.serve_forever,daemon=True).start()
        worker.start()
        try:
            with sync_playwright() as p:
                browser=p.chromium.launch(headless=True,executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
                page=browser.new_page(viewport={'width':1280,'height':1000});errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(f'http://127.0.0.1:{ui.server_port}/');page.locator('.video-card').first.wait_for()
                page.locator('.add-brand-panel > summary').click()
                assert page.locator('.add-brand-panel').bounding_box()['y'] > page.locator('#advertiser-select').bounding_box()['y'], 'Expanded form must appear below the advertiser filter'
                page.locator('#brand-name').fill('NORTEX')
                page.locator('#brand-url').fill('https://adstransparency.google.com/advertiser/AR16514522125000769537')
                page.locator('#add-brand-form button').click()
                page.wait_for_function("document.querySelector('#brand-status').textContent.includes('Collection failed')")
                assert len(store.list())==1 and store.list()[0]['state']=='failed'
                page.locator('#add-brand-form button').click()
                page.wait_for_function("!document.querySelector('#add-brand-form button').disabled")
                assert len(store.list())==1
                assert page.locator('.video-card').count()>0
                page.screenshot(path=str(ROOT/'build/add-brand-desktop.png'))
                page.set_viewport_size({'width':390,'height':844})
                page.locator('#add-brand-form').scroll_into_view_if_needed()
                page.screenshot(path=str(ROOT/'build/add-brand-mobile.png'))
                overflow=page.evaluate('document.documentElement.scrollWidth>innerWidth')
                assert not overflow, 'Mobile overflow'
                assert not errors,errors
                receipt={'browser':'Chromium','local_http':True,'created_records':len(store.list()),'observed_state':store.list()[0]['state'],'failure_injected_for_test':True,'duplicate_coalesced':True,'existing_cards':page.locator('.video-card').count(),'mobile_overflow':overflow,'page_errors':errors,'live_deployed':False}
                (ROOT/'build/add-brand-browser-receipt.json').write_text(json.dumps(receipt,indent=2));print(json.dumps(receipt))
                browser.close()
        finally:
            worker.close()
            for server in (ui,backend):server.shutdown();server.server_close()

if __name__=='__main__':main()
