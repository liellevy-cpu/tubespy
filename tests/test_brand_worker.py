import json
from pathlib import Path
import pytest


def module():
    assert Path('brand_worker.py').exists(), 'fresh collection worker missing'
    import brand_worker
    return brand_worker

def test_merge_keeps_older_brands_and_timestamps():
    m=module()
    old={'advertiser':{'google_advertiser_id':'AR1','name':'Old'},'snapshot_at':'2026-01-01T00:00:00+00:00','source_summary':{'reported_ads_approx':2,'unique_creative_pages':2,'recovered_unique_youtube_videos':1,'youtube_metadata_failures':0},'videos':[{'video_id':'old'}]}
    new={'advertiser':{'google_advertiser_id':'AR2','name':'New'},'snapshot_at':'2026-09-29T00:00:00+00:00','source_summary':{'reported_ads_approx':3,'unique_creative_pages':3,'recovered_unique_youtube_videos':2,'youtube_metadata_failures':0},'videos':[{'video_id':'new'}]}
    result=m.merge_dataset({'schema_version':2,'snapshot_at':old['snapshot_at'],'advertisers':[old]},new)
    assert result['advertisers'][0]==old
    assert result['snapshot_at']==old['snapshot_at']
    assert result['source_summary']['advertisers']==2
    result2=m.merge_dataset(result,new)
    assert len(result2['advertisers'])==2

def test_collector_egress_is_google_youtube_only():
    m=module()
    for url in ['http://127.0.0.1/a','https://169.254.169.254/','https://evil.test/','https://youtube.com.evil.test/','file:///tmp/private']:
        assert not m.allowed_source_request(url)
    for url in ['https://adstransparency.google.com/advertiser/AR123','https://www.youtube.com/embed/abcdefghijk','https://tpc.googlesyndication.com/x']:
        assert m.allowed_source_request(url)


def test_discovery_is_bounded_and_cannot_cross_advertisers():
    m=module()
    good='https://adstransparency.google.com/advertiser/AR15438241099155505153/creative/CR123'
    m.validate_discovery([good],{'stable':True},'AR15438241099155505153')
    for urls,meta in [([] ,{'stable':True}),([good],{'stable':False}),([good]*12001,{'stable':True}),([good.replace('AR15438241099155505153','AR14056452383756517377')],{'stable':True})]:
        with pytest.raises(ValueError):m.validate_discovery(urls,meta,'AR15438241099155505153')


def test_empty_creative_resolution_fails_closed(tmp_path):
    m=module();p=tmp_path/'creative_video_results.jsonl'
    p.write_text(json.dumps({'creative_url':'https://adstransparency.google.com/advertiser/AR15438241099155505153/creative/CR123','video_ids':[]})+'\n')
    with pytest.raises(ValueError):m.require_resolved_creatives(tmp_path,'AR15438241099155505153')


def test_incomplete_evidence_cannot_publish():
    m=module()
    for summary in ({}, {'status':'blocked'}, {'status':'complete','creative_failures':1,'youtube_metadata_failures':0,'discovery':{'stable':True}}, {'status':'complete','creative_failures':0,'youtube_metadata_failures':0,'discovery':{'stable':False}}):
        with pytest.raises(ValueError):m.require_complete(summary)

def test_fresh_collection_never_reuses_pilot_or_old_raw(tmp_path):
    m=module(); a=m.collection_job({'name':'TQR Marketing Group','advertiser_id':'AR15438241099155505153','url':'https://adstransparency.google.com/advertiser/AR15438241099155505153'},tmp_path)
    b=m.collection_job({'name':'TQR Marketing Group','advertiser_id':'AR15438241099155505153','url':'https://adstransparency.google.com/advertiser/AR15438241099155505153'},tmp_path)
    assert a['raw_root']!=b['raw_root']
    assert a['collector_name']=='AR15438241099155505153'
    assert not (Path(a['raw_root'])/'ar15438241099155505153'/'creative_urls.json').exists()

def test_stage_preserves_proxy_manifest_without_internal_files(tmp_path):
    m=module()
    from library import build_stage
    public=tmp_path/'public';(public/'data').mkdir(parents=True);(public/'.herenow').mkdir()
    for name in ('index.html','styles.css','app.js','data/library.json'):(public/name).write_text('test')
    manifest=public/'.herenow/proxy.json';manifest.write_text('{"proxies":{}}')
    (public/'secret.token').write_text('never public')
    stage=build_stage(public,tmp_path/'stage')
    assert (stage/'.herenow/proxy.json').read_bytes()==manifest.read_bytes()
    assert not (stage/'secret.token').exists()


def test_public_readback_uses_working_anonymous_client():
    m=module()
    result=m.public_bytes(m.LIVE+'styles.css')
    assert b':root' in result and len(result)>1000


def test_owner_sheet_sync_seeds_all_existing_and_pauses_inactive(tmp_path):
    m=module()
    from add_brand import Store
    dataset=json.loads(Path('public/data/library.json').read_text())
    s=Store(tmp_path/'q')
    # Pick an existing advertiser (comma-free name, so the CSV row stays simple) and mark
    # it inactive on the sheet -- derived from the data, not a hard-coded brand that a
    # data change could drop.
    adv=next(a['advertiser'] for a in dataset['advertisers'] if ',' not in (a['advertiser'].get('name') or ''))
    name, aid = adv['name'], adv['google_advertiser_id']
    csv_text=(f'Advertiser,Google Ads Transparency URL,Active\n'
              f'{name},https://adstransparency.google.com/advertiser/{aid},FALSE\n')
    m.sync_owner_registry(s,dataset,csv_text)
    assert len(s.list())==len(dataset['advertisers'])
    assert next(r for r in s.list() if r['advertiser_id']==aid)['state']=='inactive'
    assert s.queue_weekly()==len(dataset['advertisers'])-1


def test_publisher_failure_keeps_canonical_local_bytes(tmp_path):
    m=module();public=tmp_path/'public';(public/'data').mkdir(parents=True);(public/'.herenow').mkdir()
    previous=json.loads(Path('public/data/library.json').read_text())
    snapshot=public/'data/library.json';snapshot.write_text(json.dumps(previous));before=snapshot.read_bytes()
    for name in ('index.html','styles.css','app.js','.herenow/proxy.json'):(public/name).write_text('{}')
    def collected(command,**kwargs):
        job=json.loads(Path(command[-1]).read_text())
        (Path(job['run'])/'dataset.json').write_text(json.dumps(previous['advertisers'][0]))
    def broken_publish(stage):raise RuntimeError('host failure')
    with pytest.raises(RuntimeError,match='host failure'):
        m.collect_and_publish({'name':'Test','url':'https://adstransparency.google.com/advertiser/AR15438241099155505153'},root=tmp_path,run=collected,publish=broken_publish)
    assert snapshot.read_bytes()==before


def test_failed_collection_does_not_call_publisher(tmp_path):
    m=module(); public=tmp_path/'public';(public/'data').mkdir(parents=True)
    snapshot=public/'data/library.json';snapshot.write_text('{"verified":"old"}')
    calls=[]
    def fail(*args,**kwargs):raise RuntimeError('Google unavailable')
    with pytest.raises(RuntimeError):m.collect_and_publish({'name':'X','advertiser_id':'AR15438241099155505153','url':'https://adstransparency.google.com/advertiser/AR15438241099155505153'},root=tmp_path,run=fail,publish=lambda *a:calls.append(a))
    assert snapshot.read_text()=='{"verified":"old"}' and not calls
