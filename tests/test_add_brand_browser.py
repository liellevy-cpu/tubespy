"""Real browser + local HTTP integration; collector is deliberately failed in this regression."""
import functools
import os
from http.server import SimpleHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import threading
import urllib.request
from playwright.sync_api import sync_playwright
from add_brand import Store
from brand_service import make_server, Worker


def test_browser_add_form_real_http_duplicate_and_failure(tmp_path):
    root=Path(__file__).resolve().parents[1]
    store=Store(tmp_path/'queue.sqlite')
    called=threading.Event()
    def blocked(item):called.set();raise RuntimeError('Deliberate test source failure')
    worker=Worker(store,blocked);api=make_server(store,'test-only-token',port=0)
    api_thread=threading.Thread(target=api.serve_forever,daemon=True);api_thread.start();worker.start()
    class Frontend(SimpleHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            if self.path=='/api/brands':return self.proxy('GET')
            return super().do_GET()
        def do_POST(self):return self.proxy('POST')
        def proxy(self,method):
            data=self.rfile.read(int(self.headers.get('Content-Length','0'))) if method=='POST' else None
            req=urllib.request.Request(f'http://127.0.0.1:{api.server_port}/brands',data=data,method=method,headers={'Authorization':'Bearer test-only-token','Content-Type':'application/json'})
            with urllib.request.urlopen(req) as response:
                body=response.read();self.send_response(response.status);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(body)
    web=ThreadingHTTPServer(('127.0.0.1',0),functools.partial(Frontend,directory=str(root/'public')))
    thread=threading.Thread(target=web.serve_forever,daemon=True);thread.start()
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path=os.environ.get('CHROME_PATH','/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'),headless=True)
            page=browser.new_page(viewport={'width':390,'height':844})
            errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(f'http://127.0.0.1:{web.server_port}/');page.locator('.video-card').first.wait_for()
            page.locator('.add-brand-panel > summary').click()
            page.locator('#brand-name').fill('Ridge')
            page.locator('#brand-url').fill('https://adstransparency.google.com/advertiser/AR14056452383756517377')
            page.locator('#add-brand-form button').click()
            page.wait_for_function("document.querySelector('#brand-status').textContent.includes('failed')")
            assert called.is_set() and len(store.list())==1
            page.locator('#add-brand-form button').click()
            page.wait_for_timeout(300);assert len(store.list())==1
            assert not errors
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            (root/'build').mkdir(exist_ok=True)
            page.screenshot(path=str(root/'build/add-brand-mobile-qa.png'),full_page=True)
            browser.close()
    finally:
        worker.close();web.shutdown();api.shutdown();web.server_close();api.server_close()
