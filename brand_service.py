"""Loopback-only service behind exact here.now proxy routes."""
import argparse
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from add_brand import Store, Invalid, Limited

ROOT=Path(__file__).resolve().parent

class Worker:
    def __init__(self, store, collect):
        self.store, self.collect = store, collect
        self.event=threading.Event(); self.stop=threading.Event()
        store.wake=self.event.set
        self.thread=threading.Thread(target=self.run,daemon=True)
    def start(self): self.thread.start(); self.event.set()
    def run(self):
        while not self.stop.is_set():
            self.event.wait(60); self.event.clear()
            while not self.stop.is_set() and self.store.run_one(self.collect): pass
    def close(self): self.stop.set(); self.event.set(); self.thread.join(timeout=5)


def make_server(store, token, port=8793):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def reply(self, code, value):
            data=json.dumps(value).encode(); self.send_response(code)
            self.send_header('Content-Type','application/json');self.send_header('Cache-Control','no-store')
            self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
        def dispatch(self):
            if not hmac.compare_digest(self.headers.get('Authorization',''), 'Bearer '+token):
                return self.reply(401,{'error':'Unauthorized'})
            if self.path != '/brands': return self.reply(404,{'error':'Not found'})
            if self.command=='GET': return self.reply(200,{'brands':store.list()})
            if self.command!='POST': return self.reply(405,{'error':'Only add and list are supported'})
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<=4096: return self.reply(413,{'error':'Invalid request size'})
                if self.headers.get('Content-Type','').split(';')[0]!='application/json':
                    return self.reply(415,{'error':'JSON required'})
                self.connection.settimeout(10)
                body=json.loads(self.rfile.read(size))
                # Proxy edge rate-limits by visitor IP; backend also has a durable global limit.
                visitor=self.client_address[0]
                row,created=store.submit(body,visitor)
                return self.reply(201 if created else 200,{'brand':row,'created':created})
            except Limited as exc: return self.reply(429,{'error':str(exc)})
            except (Invalid,ValueError,UnicodeError): return self.reply(400,{'error':'Use a brand name and exact https://adstransparency.google.com/advertiser/AR… link.'})
            except Exception: return self.reply(503,{'error':'Could not save this brand. Please retry.'})
        do_GET=do_POST=do_PATCH=do_DELETE=do_PUT=do_OPTIONS=dispatch
    return ThreadingHTTPServer(('127.0.0.1',port),Handler)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=8793)
    parser.add_argument('--db',type=Path,default=ROOT/'build/brands.sqlite')
    parser.add_argument('--token-file',type=Path,default=ROOT/'build/brand-service.token')
    args=parser.parse_args()
    token=args.token_file.read_text().strip()
    if len(token)<32: raise ValueError('Service token must be at least 32 characters')
    from brand_worker import collect_and_publish
    store=Store(args.db); worker=Worker(store,collect_and_publish)
    http=make_server(store,token,args.port);worker.start()
    try: http.serve_forever()
    finally: worker.close();http.server_close()

if __name__=='__main__': main()
