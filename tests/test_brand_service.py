import json
import threading
import urllib.request
import urllib.error
from pathlib import Path
import pytest
from test_add_brand import module, URL


def service_module():
    assert Path('brand_service.py').exists(), 'HTTP service missing'
    import brand_service
    return brand_service

@pytest.fixture
def server(tmp_path):
    m=service_module(); store=module().Store(tmp_path/'q')
    http=m.make_server(store, 'test-only-secret', port=0)
    thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
    yield 'http://127.0.0.1:'+str(http.server_port),store
    http.shutdown();http.server_close();thread.join()

def request(server,method='GET',body=None,token='test-only-secret',path='/brands'):
    base,_=server
    req=urllib.request.Request(base+path, data=json.dumps(body).encode() if body is not None else None,method=method,headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req) as r:return r.status,json.load(r)
    except urllib.error.HTTPError as e:return e.code,json.load(e)

def test_http_create_duplicate_status(server):
    code,row=request(server,'POST',{'name':'Ridge','url':URL});assert code==201 and row['brand']['state']=='queued'
    assert request(server,'POST',{'name':'Ridge','url':URL})[0]==200
    assert request(server)[1]['brands'][0]['name']=='Ridge'

@pytest.mark.parametrize('method',['PATCH','DELETE','PUT'])
def test_no_public_edit_delete_routes(server,method):
    assert request(server,method,{},path='/brands')[0]==405

def test_http_fails_closed(server):
    assert request(server,token='wrong')[0]==401
    assert request(server,path='/arbitrary')[0]==404
    assert request(server,'POST',{'name':'Bad','url':'https://evil.test'})[0]==400

def test_worker_wakes_without_polling(tmp_path):
    m=service_module(); done=threading.Event(); seen=[]
    store=module().Store(tmp_path/'q')
    worker=m.Worker(store,lambda item: (seen.append(item),done.set(),{'published':True})[-1])
    worker.start()
    try:
        store.submit({'name':'Ridge','url':URL},'ip')
        assert done.wait(2), 'submission must immediately signal collection'
        assert seen[0]['advertiser_id']=='AR14056452383756517377'
    finally: worker.close()
