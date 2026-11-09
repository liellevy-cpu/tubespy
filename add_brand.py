"""Durable add-only advertiser queue. No visitor can edit or remove a brand."""
from __future__ import annotations
import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlsplit

class Invalid(ValueError): pass
class Limited(ValueError): pass


def validate(payload):
    if not isinstance(payload, dict) or set(payload) != {'name', 'url'}:
        raise Invalid('Provide only a brand name and Google advertiser link.')
    name, url = payload['name'], payload['url']
    if not isinstance(name, str) or not re.fullmatch(r"[\w][\w .&'’()+,/\\\-]{0,79}", name.strip(), re.UNICODE):
        raise Invalid('Use a brand name of 1–80 letters, numbers or ordinary punctuation.')
    if not isinstance(url, str) or len(url) > 2048 or any(ord(c)<33 for c in url):
        raise Invalid('Paste the exact Google advertiser page link.')
    try: parsed = urlsplit(url)
    except ValueError: raise Invalid('Invalid advertiser link.')
    match = re.fullmatch(r'/advertiser/(AR[0-9]{10,25})/?', parsed.path)
    if parsed.scheme != 'https' or parsed.netloc != 'adstransparency.google.com' or parsed.fragment or not match:
        raise Invalid('Use https://adstransparency.google.com/advertiser/AR… — not a search or individual ad link.')
    identity = match.group(1)
    return {'name': name.strip(), 'advertiser_id': identity, 'url': f'https://adstransparency.google.com/advertiser/{identity}?region=US&format=VIDEO&platform=YOUTUBE&hl=en'}


class Store:
    def __init__(self, path, wake=lambda: None, per_hour=5):
        self.path = Path(path); self.path.parent.mkdir(parents=True, exist_ok=True)
        self.wake, self.per_hour = wake, per_hour
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS brands (
              advertiser_id TEXT PRIMARY KEY, name TEXT NOT NULL, url TEXT NOT NULL,
              state TEXT NOT NULL, created_at REAL NOT NULL, attempted_at REAL,
              completed_at REAL, message TEXT NOT NULL DEFAULT '', lease_until REAL);
            CREATE TABLE IF NOT EXISTS submissions (visitor TEXT NOT NULL, at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS item_state (video_id TEXT PRIMARY KEY, state TEXT NOT NULL, at REAL NOT NULL);
            ''')
    def connect(self):
        db=sqlite3.connect(self.path, timeout=30); db.row_factory=sqlite3.Row
        return db
    def list(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT advertiser_id,name,url,state,created_at,attempted_at,completed_at,message FROM brands ORDER BY created_at')]
    def submit(self, payload, visitor, trusted=False):
        item=validate(payload); now=time.time()
        visitor=hashlib.sha256(visitor.encode()).hexdigest()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing=db.execute('SELECT * FROM brands WHERE advertiser_id=?',(item['advertiser_id'],)).fetchone()
            if existing:
                if existing['state']=='inactive': raise Invalid('This advertiser is paused by the owner.')
                return dict(existing), False
            if db.execute('SELECT count(*) FROM brands').fetchone()[0]>=500:
                raise Limited('The library is at capacity. Please try later.')
            if not trusted and (db.execute('SELECT count(*) FROM submissions WHERE visitor=? AND at>?',(visitor,now-3600)).fetchone()[0]>=self.per_hour or db.execute('SELECT count(*) FROM submissions WHERE at>?',(now-86400,)).fetchone()[0]>=30):
                raise Limited('Too many new brands. Please try later.')
            if db.execute('SELECT 1 FROM brands WHERE lower(name)=lower(?)',(item['name'],)).fetchone():
                raise Invalid('That brand name already exists. Use the advertiser’s distinct account name.')
            db.execute('INSERT INTO brands(advertiser_id,name,url,state,created_at) VALUES(?,?,?,\'queued\',?)',(item['advertiser_id'],item['name'],item['url'],now))
            if not trusted: db.execute('INSERT INTO submissions VALUES(?,?)',(visitor,now))
            db.execute('DELETE FROM submissions WHERE at<?',(now-86400,))
        self.wake()
        return next(row for row in self.list() if row['advertiser_id']==item['advertiser_id']), True
    def pause(self, identities):
        with self.connect() as db:
            db.executemany("UPDATE brands SET state='inactive',message='Paused by owner.' WHERE advertiser_id=?",[(identity,) for identity in identities])
    def set_item_state(self, video_id, state):
        # Per-video library state: 'used' (goes to the Used tab) or 'not_relevant' (hidden). 'new' clears it.
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id or ''): raise Invalid('Invalid video id.')
        if state not in ('used','not_relevant','new'): raise Invalid('Unsupported item state.')
        with self.connect() as db:
            if state=='new':
                db.execute('DELETE FROM item_state WHERE video_id=?',(video_id,))
            else:
                db.execute("INSERT INTO item_state(video_id,state,at) VALUES(?,?,?) ON CONFLICT(video_id) DO UPDATE SET state=excluded.state,at=excluded.at",(video_id,state,time.time()))
        return {'video_id':video_id,'state':state}
    def item_states(self):
        with self.connect() as db:
            return {row['video_id']:row['state'] for row in db.execute('SELECT video_id,state FROM item_state')}
    def seed_used(self, video_ids):
        for video_id in video_ids:
            with self.connect() as db:
                if not db.execute('SELECT 1 FROM item_state WHERE video_id=?',(video_id,)).fetchone():
                    try: self.set_item_state(video_id,'used')
                    except Invalid: pass
    def queue_weekly(self):
        with self.connect() as db:
            count=db.execute("UPDATE brands SET state='queued', message='' WHERE state IN ('complete','failed')").rowcount
        self.wake(); return count
    def run_one(self, collect):
        now=time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("SELECT * FROM brands WHERE state='queued' OR (state='collecting' AND lease_until<?) ORDER BY created_at LIMIT 1",(now,)).fetchone()
            if not row: return False
            db.execute("UPDATE brands SET state='collecting',attempted_at=?,lease_until=?,message='' WHERE advertiser_id=?",(now,now+6000,row['advertiser_id']))
        try:
            result=collect(dict(row))
            if result.get('published') is not True: raise RuntimeError('Collection did not produce a verified publication')
            state, message='complete','Collected and published. Included in weekly refreshes.'
        except Exception:
            state, message='failed','Collection could not be verified. Existing videos are unchanged; this brand remains in the weekly queue.'
        with self.connect() as db:
            db.execute('UPDATE brands SET state=?,message=?,lease_until=NULL,completed_at=CASE WHEN ?=\'complete\' THEN ? ELSE completed_at END WHERE advertiser_id=?',(state,message,state,time.time(),row['advertiser_id']))
        return True
