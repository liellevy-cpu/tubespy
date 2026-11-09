import importlib.util
from pathlib import Path
import concurrent.futures
import pytest

ROOT = Path(__file__).resolve().parents[1]
URL = 'https://adstransparency.google.com/advertiser/AR14056452383756517377'

def module():
    assert (ROOT / 'add_brand.py').exists(), 'add-only backend is missing'
    import add_brand
    return add_brand

def test_submission_is_durable_unique_and_wakes_worker(tmp_path):
    m = module(); wakes = []
    store = m.Store(tmp_path / 'queue.sqlite', wake=lambda: wakes.append(True))
    first, created = store.submit({'name': 'Ridge', 'url': URL}, 'visitor')
    second, duplicate = store.submit({'name': 'Other label', 'url': URL + '?region=GB'}, 'visitor')
    assert created and not duplicate
    assert first['advertiser_id'] == second['advertiser_id']
    assert wakes == [True]
    assert m.Store(tmp_path / 'queue.sqlite').list()[0]['name'] == 'Ridge'
    assert first['state'] == 'queued'

@pytest.mark.parametrize('url', ['http://adstransparency.google.com/advertiser/AR123', 'https://evil.test/advertiser/AR123', 'https://adstransparency.google.com@evil.test/advertiser/AR123', 'https://adstransparency.google.com:443/advertiser/AR123', 'https://adstransparency.google.com/?query=ridge', URL+'/creative/CR123', URL+'%2f..', URL+'#x'])
def test_server_rejects_unsafe_or_non_advertiser_links(tmp_path, url):
    m = module()
    with pytest.raises(m.Invalid): m.Store(tmp_path/'q').submit({'name':'Brand','url':url}, 'ip')

def test_server_rejects_extra_fields_and_bad_names(tmp_path):
    m=module(); s=m.Store(tmp_path/'q')
    for payload in ({'name':'x','url':URL,'command':'rm'}, {'name':'<script>','url':URL}, {'name':'','url':URL}, {'name':'x'*81,'url':URL}):
        with pytest.raises(m.Invalid): s.submit(payload,'ip')

def test_concurrent_duplicate_submissions_only_create_one(tmp_path):
    m=module(); s=m.Store(tmp_path/'q')
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results=list(pool.map(lambda i:s.submit({'name':'Ridge','url':URL},str(i)),range(16)))
    assert sum(created for _,created in results)==1
    assert len(s.list())==1

def test_abuse_limits_are_durable(tmp_path):
    m=module(); s=m.Store(tmp_path/'q', per_hour=2)
    for i in range(2): s.submit({'name':f'Brand {i}','url':f'https://adstransparency.google.com/advertiser/AR123456789012345678{i}'},'ip')
    with pytest.raises(m.Limited): m.Store(tmp_path/'q', per_hour=2).submit({'name':'Third','url':URL},'ip')

def test_failure_preserves_snapshot_and_is_not_complete(tmp_path):
    m=module(); s=m.Store(tmp_path/'q'); s.submit({'name':'Ridge','url':URL},'ip')
    previous=tmp_path/'snapshot'; previous.write_text('verified old data')
    def fail(item): raise RuntimeError('source unavailable')
    assert s.run_one(fail)
    row=s.list()[0]
    assert row['state']=='failed' and row['attempted_at']
    assert previous.read_text()=='verified old data'
    assert not s.run_one(fail)

def test_existing_brand_punctuation_is_accepted(tmp_path):
    m=module()
    assert m.Store(tmp_path/'q').submit({'name':r'MUD\WTR','url':URL},'ip')[1]


def test_owner_inactive_brand_cannot_be_reenabled(tmp_path):
    m=module(); s=m.Store(tmp_path/'q')
    s.submit({'name':'Ridge','url':URL},'owner',trusted=True)
    s.pause(['AR14056452383756517377'])
    with pytest.raises(m.Invalid):s.submit({'name':'New name','url':URL},'visitor')
    assert s.queue_weekly()==0
    assert s.list()[0]['state']=='inactive'


def test_running_job_cannot_be_claimed_by_second_worker(tmp_path):
    import threading
    m=module();s=m.Store(tmp_path/'q');s.submit({'name':'Ridge','url':URL},'ip')
    started=threading.Event();release=threading.Event()
    def collect(item):started.set();release.wait(5);return {'published':True}
    thread=threading.Thread(target=lambda:s.run_one(collect));thread.start()
    assert started.wait(2)
    assert not m.Store(tmp_path/'q').run_one(lambda item:pytest.fail('double claim'))
    release.set();thread.join()


def test_success_and_weekly_requeue(tmp_path):
    m=module(); s=m.Store(tmp_path/'q'); s.submit({'name':'Ridge','url':URL},'ip')
    assert s.run_one(lambda item: {'published':True})
    assert s.list()[0]['state']=='complete'
    assert s.queue_weekly()==1
    assert s.list()[0]['state']=='queued'

def test_unpublished_result_is_failure(tmp_path):
    m=module(); s=m.Store(tmp_path/'q'); s.submit({'name':'Ridge','url':URL},'ip')
    s.run_one(lambda item:{'published':False})
    assert s.list()[0]['state']=='failed'
