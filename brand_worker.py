"""Fresh bounded collection + fail-closed incremental publication."""
from __future__ import annotations
import argparse
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid
from add_brand import Store, validate

ROOT=Path(__file__).resolve().parent
LIVE='https://youtube-competitor-library.coolina.here.now/'
SLUG='cobalt-violet-hs8j'
ACCOUNT='43e6ae31-c057-4a6d-84ab-4992e5cc3673'
PUBLISH=Path.home()/'.hermes/skills/here-now/scripts/publish.sh'


def collection_job(item, root):
    safe=validate({'name':item['name'],'url':item['url']})
    run=Path(root)/'build/brand-runs'/uuid.uuid4().hex
    run.mkdir(parents=True)
    return {**safe,'collector_name':safe['advertiser_id'],'raw_root':str(run/'raw'),'run':str(run)}


def require_complete(summary):
    if summary.get('status')!='complete' or summary.get('creative_failures')!=0 or summary.get('youtube_metadata_failures')!=0 or summary.get('discovery',{}).get('stable') is not True:
        raise ValueError('Incomplete source coverage; publication prohibited')


def merge_dataset(previous, fresh):
    result=deepcopy(previous)
    identity=fresh['advertiser']['google_advertiser_id']
    rows=[row for row in result['advertisers'] if row['advertiser']['google_advertiser_id']!=identity]
    rows.append(deepcopy(fresh));result['advertisers']=rows
    # Earliest source timestamp is intentionally retained; no old evidence is relabelled fresh.
    result['snapshot_at']=min(row['snapshot_at'] for row in rows)
    result['source_summary']={'advertisers':len(rows)}
    for key in ('reported_ads_approx','unique_creative_pages','recovered_unique_youtube_videos','youtube_metadata_failures','unavailable_youtube_videos'):
        result['source_summary'][key]=sum(row['source_summary'].get(key,0) for row in rows)
    return result


def owner_get(path):
    token=(Path.home()/'.herenow/credentials').read_text().strip()
    req=urllib.request.Request('https://here.now/api/v1/'+path,headers={'Authorization':'Bearer '+token,'X-HereNow-Account':'coolina'})
    with urllib.request.urlopen(req,timeout=45) as response:return json.load(response)


def public_bytes(url):
    return subprocess.run(['curl','--fail','--silent','--show-error','--location','--max-time','45',url],capture_output=True,check=True,timeout=50).stdout


def publish_stage(stage):
    labels=owner_get(f'accounts/{ACCOUNT}/site-labels')['siteLabels']
    if not any(x['url']==LIVE and x['slug']==SLUG and x['status']=='active' for x in labels):
        raise RuntimeError('Live target mapping changed; refusing publication')
    result=subprocess.run(['bash',str(PUBLISH),str(stage),'--slug',SLUG,'--workspace','coolina'],cwd=ROOT,text=True,capture_output=True,timeout=300,check=True)
    receipt=result.stdout+'\n'+result.stderr
    (stage.parent/'publish-receipt.txt').write_text(receipt)
    if 'publish_result.account_url='+LIVE not in receipt or 'warning' in receipt.lower():
        raise RuntimeError('Publish mapping or manifest warning requires inspection')
    for relative in ('app.js','styles.css','data/library.json'):
        expected=(stage/relative).read_bytes()
        for attempt in range(8):
            actual=public_bytes(LIVE+relative+'?release='+uuid.uuid4().hex)
            if actual==expected:break
            time.sleep(3)
        else:raise RuntimeError('Live artifact differs: '+relative)


def collect_and_publish(item, root=ROOT, run=subprocess.run, publish=publish_stage):
    root=Path(root);job=collection_job(item,root);directory=Path(job['run'])
    path=directory/'job.json';path.write_text(json.dumps(job))
    with (directory/'collection.log').open('w') as log:
        run([sys.executable,str(ROOT/'brand_worker.py'),'--collect-job',str(path)],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,timeout=5500,check=True)
    fresh=json.loads((directory/'dataset.json').read_text())
    with (root/'build/publish.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        snapshot=root/'public/data/library.json'
        previous=json.loads(snapshot.read_text());combined=merge_dataset(previous,fresh)
        stage=directory/'stage';stage.mkdir()
        for relative in ('index.html','styles.css','app.js','.herenow/proxy.json'):
            source=root/'public'/relative
            target=stage/relative;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,target)
        target=stage/'data/library.json';target.parent.mkdir();target.write_text(json.dumps(combined,indent=2,ensure_ascii=False)+'\n')
        publish(stage)
        temporary=snapshot.with_suffix('.tmp');shutil.copyfile(target,temporary);os.replace(temporary,snapshot)
    return {'published':True,'run':str(directory)}


def allowed_source_request(url):
    from urllib.parse import urlsplit
    try:
        parsed=urlsplit(url); host=parsed.hostname or ''
        return parsed.scheme=='https' and not parsed.username and parsed.port in (None,443) and any(host==domain or host.endswith('.'+domain) for domain in ('google.com','youtube.com','youtube-nocookie.com','googlevideo.com','gstatic.com','googlesyndication.com','doubleclick.net','googleusercontent.com','ytimg.com','googleadservices.com'))
    except ValueError:return False


class RestrictedBrowser:
    def __init__(self,browser):self.browser=browser
    async def new_context(self,**kwargs):
        context=await self.browser.new_context(**kwargs,service_workers='block')
        async def route_request(route):
            if allowed_source_request(route.request.url):await route.continue_()
            else:await route.abort()
        await context.route('**/*',route_request)
        return context


def validate_discovery(urls, metadata, identity):
    from collect_all import CREATIVE_RE
    if not 0<len(urls)<=12000 or metadata.get('stable') is not True:
        raise ValueError('Discovery empty, incomplete or beyond the collection limit')
    for url in urls:
        match=CREATIVE_RE.search(url)
        if not allowed_source_request(url) or not match or match.group(1)!=identity:
            raise ValueError('Discovery crossed advertiser identity')


def require_resolved_creatives(source, identity):
    from collect_all import load_jsonl, CREATIVE_RE
    rows=load_jsonl(Path(source)/'creative_video_results.jsonl')
    if not rows:raise ValueError('No creative evidence')
    withheld_path=Path(source)/'google_withheld_creatives.json'
    withheld=set(json.loads(withheld_path.read_text())) if withheld_path.exists() else set()
    # Google-withheld ads ("unable to show you this variation") are recorded, not guessed; cap them.
    if len(withheld)>max(3,len(rows)//20):raise ValueError('Too many creatives withheld by Google')
    if not any(row.get('video_ids') for row in rows):raise ValueError('No resolved videos')
    for row in rows:
        match=CREATIVE_RE.search(row.get('creative_url',''))
        if not match or match.group(1)!=identity or row.get('error'):
            raise ValueError('Incomplete creative resolution or advertiser mismatch')
        if not row.get('video_ids') and row.get('creative_url') not in withheld:
            raise ValueError('Incomplete creative resolution or advertiser mismatch')


async def collect_job(job):
    import collect_all
    from library import build_dataset, retain_top_viewed
    from playwright.async_api import async_playwright
    collect_all.RAW=Path(job['raw_root'])
    original_discover=collect_all.collect_creative_urls
    async def checked_discover(page,url):
        urls,metadata=await original_discover(page,url)
        validate_discovery(urls,metadata,job['advertiser_id'])
        return urls,metadata
    collect_all.collect_creative_urls=checked_discover
    # Fresh UUID root and ID-based name forbid old caches and the historic TQR bypass.
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True,executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
        try:
          with collect_all.google_lock('library:'+job['advertiser_id']):
            summary=await asyncio.wait_for(collect_all.collect_advertiser(RestrictedBrowser(browser),{'name':job['collector_name'],'transparency_url':job['url']},1),timeout=5400)
        finally:await browser.close()
    require_complete(summary)
    if summary.get('advertiser_ids') != [job['advertiser_id']]:raise ValueError('Advertiser identity mismatch')
    snapshot=datetime.now(timezone.utc)
    source=Path(job['raw_root'])/collect_all.slugify(job['collector_name'])
    require_resolved_creatives(source,job['advertiser_id'])
    dataset=retain_top_viewed(build_dataset(source,snapshot,job['name'],job['advertiser_id'],summary['creative_count']),5)
    (Path(job['run'])/'dataset.json').write_text(json.dumps(dataset,indent=2))


def sync_owner_registry(store, dataset, csv_text):
    import csv
    import io
    from registry_sync import REQUIRED_HEADERS, _active
    reader=csv.DictReader(io.StringIO(csv_text.lstrip('\ufeff')))
    if not REQUIRED_HEADERS.issubset(set(reader.fieldnames or [])):
        raise ValueError('Owner registry headers missing; no changes applied')
    rows=list(reader)
    known={row['advertiser']['name'].casefold():row['advertiser']['google_advertiser_id'] for row in dataset['advertisers']}
    existing={row['advertiser_id'] for row in store.list()}
    planned=[]
    for row in rows:
        name=(row.get('Advertiser') or '').strip()
        if not name: continue
        active=_active(row.get('Active') or '')
        url=(row.get('Google Ads Transparency URL') or '').strip()
        # Older owner sheet search links are mapped only through verified published identity.
        identity=known.get(name.casefold())
        if identity: url='https://adstransparency.google.com/advertiser/'+identity
        safe=validate({'name':name,'url':url})
        planned.append((safe,active))
    for row in dataset['advertisers']:
        identity=row['advertiser']
        if identity['google_advertiser_id'] not in existing:
            brand,_=store.submit({'name':identity['name'],'url':'https://adstransparency.google.com/advertiser/'+identity['google_advertiser_id']},'owner-seed',trusted=True)
            with store.connect() as db: db.execute("UPDATE brands SET state='complete',message='Existing snapshot; included in weekly refreshes.' WHERE advertiser_id=?",(brand['advertiser_id'],))
            existing.add(brand['advertiser_id'])
    for safe,active in planned:
        if safe['advertiser_id'] not in existing:
            store.submit({'name':safe['name'],'url':safe['url']},'owner-sheet',trusted=True)
            existing.add(safe['advertiser_id'])
        if not active:store.pause([safe['advertiser_id']])


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--collect-job',type=Path);parser.add_argument('--weekly',action='store_true');parser.add_argument('--seed',action='store_true');parser.add_argument('--drain',action='store_true')
    args=parser.parse_args()
    if args.collect_job:
        asyncio.run(collect_job(json.loads(args.collect_job.read_text())));return
    store=Store(ROOT/'build/brands.sqlite')
    if args.seed or args.weekly:
        from registry_sync import download_registry_csv, SPREADSHEET_ID, SHEET_GID
        dataset=json.loads((ROOT/'public/data/library.json').read_text())
        sync_owner_registry(store,dataset,download_registry_csv(SPREADSHEET_ID,SHEET_GID))
    if args.weekly: print(json.dumps({'queued':store.queue_weekly()}))
    if args.drain:
        while store.run_one(collect_and_publish):pass
    print(json.dumps({'brands':store.list()}))

if __name__=='__main__':main()
