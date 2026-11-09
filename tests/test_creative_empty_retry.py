"""An HTTP 200 detail page without video evidence must not be success."""
import asyncio
from types import SimpleNamespace
import collect_all

class Page:
    frames=[]
    def __init__(self,html):self.html=html
    def on(self,*args):pass
    async def goto(self,*args,**kwargs):return SimpleNamespace(status=200)
    async def wait_for_timeout(self,*args):pass
    def locator(self,*args):return self
    async def evaluate_all(self,*args):return []
    async def content(self):return self.html
    async def inner_text(self):return 'Ad details\nFormat: Video'
    async def close(self):pass

class Context:
    def __init__(self,pages):self.pages=pages;self.attempts=0
    async def new_page(self):
        p=Page(self.pages[min(self.attempts,len(self.pages)-1)]);self.attempts+=1;return p

async def no_wait(*args):pass

def test_empty_http200_retries_then_resolves(monkeypatch):
    monkeypatch.setattr(collect_all.asyncio,'sleep',no_wait)
    context=Context(['<html>blank ad</html>','https://www.youtube.com/embed/abcdefghijk'])
    result=asyncio.run(collect_all.extract_one(context,'https://adstransparency.google.com/advertiser/AR123/creative/CR1',0,asyncio.Semaphore(1)))
    assert context.attempts==2
    assert result['video_ids']==['abcdefghijk']
    assert result['error'] is None

def test_repeated_empty_http200_is_blocked_not_success(monkeypatch):
    monkeypatch.setattr(collect_all.asyncio,'sleep',no_wait)
    context=Context(['<html>blank ad</html>'])
    result=asyncio.run(collect_all.extract_one(context,'https://adstransparency.google.com/advertiser/AR123/creative/CR1',0,asyncio.Semaphore(1)))
    assert context.attempts==3
    assert result['video_ids']==[]
    assert result['error'] and 'No YouTube video evidence' in result['error']
