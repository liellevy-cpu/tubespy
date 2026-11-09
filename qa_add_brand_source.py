"""Manual real-source failure-path acceptance probe; never uses production registry."""
import json
import hashlib
from pathlib import Path
import tempfile
import threading
import time
import urllib.request
from add_brand import Store
from brand_service import Worker, make_server
from brand_worker import collect_and_publish, ROOT


def main():
    snapshot=ROOT/'public/data/library.json';before=hashlib.sha256(snapshot.read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory(prefix='youtube-source-probe-') as directory:
        store=Store(Path(directory)/'queue.sqlite')
        worker=Worker(store,collect_and_publish)
        server=make_server(store,'local-source-probe-not-production',port=0)
        threading.Thread(target=server.serve_forever,daemon=True).start();worker.start()
        try:
            started=time.monotonic()
            request=urllib.request.Request(f'http://127.0.0.1:{server.server_port}/brands',method='POST',data=json.dumps({'name':'Nonexistent advertiser failure probe','url':'https://adstransparency.google.com/advertiser/AR00000000000000000001'}).encode(),headers={'Authorization':'Bearer local-source-probe-not-production','Content-Type':'application/json'})
            with urllib.request.urlopen(request) as response:reply=json.load(response);code=response.status
            assert code==201 and reply['created'] is True
            deadline=time.monotonic()+180
            while time.monotonic()<deadline and store.list()[0]['state'] in ('queued','collecting'):time.sleep(.2)
            row=store.list()[0]
            after=hashlib.sha256(snapshot.read_bytes()).hexdigest()
            result={'http_status':code,'elapsed_seconds':round(time.monotonic()-started,2),'state':row['state'],'attempted_at':row['attempted_at'],'snapshot_unchanged':before==after,'note':'Intentional nonexistent Google AR ID; real HTTP intake, worker wake, Chrome discovery and failed-source preservation. Not a successful collection.'}
            (ROOT/'build/real-source-http-receipt.json').write_text(json.dumps(result,indent=2))
            print(json.dumps(result))
            assert row['state']=='failed' and row['attempted_at'] and before==after
        finally:worker.close();server.shutdown();server.server_close()

if __name__=='__main__':main()
