"""500-property UI on emulated slow network; no real Cian or external requests."""
import asyncio
import importlib
import json
import os
import socket
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from tests.helpers import sign_in
from tests.perf_dataset import seed,realtor_note

pytestmark=pytest.mark.skipif(os.getenv('RUN_BROWSER_TESTS')!='1',reason='real browser opt in')


class SlowServer:
    def __init__(self,app,folder):
        import uvicorn
        self.app=app;self.started=threading.Event();self.release=threading.Event()
        self.initial=True;self.fail_next=False;self.state_reads=[];self.writes=[];self.detail_reads=[]
        self.inflight=0;self.maximum=0;self.wire=[]
        key=folder/'server.key';cert=folder/'server.crt'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),
            '-out',str(cert),'-days','1','-subj','/CN=localhost','-addext','subjectAltName=IP:127.0.0.1'],
            check=True,capture_output=True,timeout=15)
        self.socket=socket.socket();self.socket.bind(('127.0.0.1',0))
        self.url='https://127.0.0.1:'+str(self.socket.getsockname()[1])
        self.server=uvicorn.Server(uvicorn.Config(self,log_level='error',lifespan='off',
            ssl_keyfile=str(key),ssl_certfile=str(cert)))
        self.thread=threading.Thread(target=self.server.run,kwargs={'sockets':[self.socket]},daemon=True)
        self.thread.start();deadline=time.monotonic()+10
        while not self.server.started:
            if time.monotonic()>deadline:raise RuntimeError('isolated HTTPS server did not start')
            time.sleep(.01)

    async def __call__(self,scope,receive,send):
        if scope['type']!='http':return await self.app(scope,receive,send)
        path=scope['path'];state=path=='/api/state'
        if path.endswith('/details'):self.detail_reads.append(path)
        if scope['method'] in ('PATCH','POST','DELETE'):self.writes.append((scope['method'],path))
        if not state:return await self.app(scope,receive,send)
        headers=dict(scope.get('headers',[]))
        self.state_reads.append(headers.get(b'if-none-match'))
        self.inflight+=1;self.maximum=max(self.maximum,self.inflight);finished=False
        def finish():
            nonlocal finished
            if not finished:self.inflight-=1;finished=True
        async def synthetic_error(message):
            body=json.dumps({'detail':message},ensure_ascii=False).encode()
            await send({'type':'http.response.start','status':503,'headers':[
                (b'content-type',b'application/json'),(b'content-length',str(len(body)).encode()),(b'cache-control',b'no-store')]})
            await send({'type':'http.response.body','body':body});finish()
        status=200;encoding=None;wire_bytes=0
        async def shaped(message):
            nonlocal status,encoding,wire_bytes
            if message['type']=='http.response.start':
                status=message['status'];encoding=dict(message['headers']).get(b'content-encoding')
                await asyncio.sleep(.15);await send(message)
            elif message['type']=='http.response.body':
                body=message.get('body',b'');wire_bytes+=len(body)
                for pos in range(0,len(body),4096):
                    chunk=body[pos:pos+4096]
                    await asyncio.sleep(len(chunk)/(256*1024))
                    await send({'type':'http.response.body','body':chunk,'more_body':True})
                if not message.get('more_body'):
                    await send({'type':'http.response.body','body':b'','more_body':False})
                    self.wire.append({'status':status,'bytes':wire_bytes,'encoding':encoding.decode() if encoding else None})
                    finish()
            else:await send(message)
        try:
            if self.initial:
                self.initial=False;self.started.set()
                if not await asyncio.to_thread(self.release.wait,15):raise TimeoutError('initial UI gate')
                return await synthetic_error('Тестовый сбой первой загрузки')
            if self.fail_next:
                self.fail_next=False;return await synthetic_error('Тестовый фоновый сбой')
            await self.app(scope,receive,shaped)
        finally:finish()

    def close(self):
        self.release.set();self.server.should_exit=True;self.thread.join(timeout=10);self.socket.close()


@pytest.fixture
def large_app(tmp_path,monkeypatch):
    monkeypatch.setenv('DATA_DIR',str(tmp_path));monkeypatch.setenv('APP_USER','owner')
    monkeypatch.setenv('APP_PASSWORD','long-test-password');monkeypatch.setenv('BROWSER_SCHEDULE_ENABLED','false')
    monkeypatch.delenv('PUBLIC_URL',raising=False)
    from app import main
    importlib.reload(main)
    monkeypatch.setattr(main.CianBrowserSource,'status',lambda self:{'status':'authorized','message':'Авторизован','manual_access':False})
    seed(main)
    with TestClient(main.app,base_url='https://xata.test') as client:
        sign_in(client)
        server=SlowServer(main.app,tmp_path)
        try:yield main,client,server
        finally:server.close()


@pytest.mark.parametrize('engine',['chromium','webkit'])
@pytest.mark.parametrize('width',[390,1440])
@pytest.mark.parametrize('path',['/','/ours'])
def test_slow_large_lists_loading_retry_single_flight_and_realtor_save(large_app,engine,width,path):
    main,client,server=large_app
    async def scenario():
        from playwright.async_api import async_playwright,expect
        state_reads=server.state_reads;detail_reads=server.detail_reads;writes=server.writes
        async def bridge(route):
            url=urlsplit(route.request.url)
            assert url.hostname=='127.0.0.1' and url.port==int(server.url.rsplit(':',1)[1]),'No external requests allowed'
            await route.continue_()  # Real HTTP 304, including WebKit; never fulfill a mocked 304.
        async with async_playwright() as p:
            browser=await getattr(p,engine).launch(headless=True)
            context=await browser.new_context(viewport={'width':width,'height':900},is_mobile=width<900,
                has_touch=width<900,timezone_id='America/Los_Angeles',ignore_https_errors=True)
            from app.auth import COOKIE
            await context.add_cookies([{'name':COOKIE,'value':client.cookies.get(COOKIE),'url':server.url,
                'httpOnly':True,'secure':True,'sameSite':'Lax'}])
            await context.route('**/*',bridge)
            page=await context.new_page();page.set_default_timeout(15000);failures=[]
            page.on('pageerror',lambda e:failures.append(str(e)))
            async def before_reload():
                # Reload cancels the old document's fetch, but its already-sent
                # ASGI body can still be finishing. Drain it before asserting
                # per-document single-flight again in the replacement page.
                await page.evaluate('async()=>{clearTimeout(pollTimer);if(refreshPromise)await refreshPromise;}')
                deadline=time.monotonic()+5
                while server.inflight:
                    assert time.monotonic()<deadline,'state response did not drain'
                    await asyncio.sleep(.02)
            async def shot(name):
                folder=os.getenv('UI_SCREENSHOT_DIR')
                if folder:
                    target=Path(folder);target.mkdir(parents=True,exist_ok=True)
                    await page.screenshot(path=str(target/(engine+'-'+str(width)+'-perf-'+('ours' if path=='/ours' else 'all')+'-'+name+'.png')))
            try:
                await page.goto(server.url+path,wait_until='domcontentloaded')
                assert await asyncio.to_thread(server.started.wait,10)
                await expect(page.locator('#nav-all-count')).to_have_text('Загрузка…')
                await expect(page.locator('#count')).to_have_text('Загрузка…')
                await expect(page.locator('#last-sync')).to_contain_text('Загрузка…')
                assert 'ещё не было' not in await page.locator('#last-sync').inner_text()
                server.release.set()
                await expect(page.locator('#load-retry')).to_be_visible()
                await expect(page.locator('#load-message')).to_contain_text('Не удалось загрузить')
                await expect(page.locator('#count')).to_have_text('Загрузка…')
                await page.locator('#load-retry').click()
                await expect(page.locator('#count')).to_have_text('20' if path=='/ours' else '500')
                await expect(page.locator('#nav-all-count')).to_have_text('500')
                await expect(page.locator('#nav-ours-count')).to_have_text('20')
                assert await page.locator('tr[data-property-id]').count()==(20 if path=='/ours' else 500)
                assert len(detail_reads)==0,'Histories must not load on initial listing'
                render_ms=await page.evaluate("performance.getEntriesByName('xata-list-render').at(-1).duration")
                metrics={'engine':engine,'width':width,'page':path,'rows':20 if path=='/ours' else 500,'render_ms':round(render_ms,3),'network':'real loopback HTTPS, 150 ms latency, 256 KiB/s shaped body; not physical device'}
                print('XATA_UI_METRICS:'+json.dumps(metrics,ensure_ascii=False))
                if os.getenv('UI_SCREENSHOT_DIR'):
                    destination=Path(os.environ['UI_SCREENSHOT_DIR']);destination.mkdir(parents=True,exist_ok=True)
                    (destination/(engine+'-'+str(width)+'-'+('ours' if path=='/ours' else 'all')+'-metrics.json')).write_text(json.dumps(metrics,ensure_ascii=False,indent=2))
                assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                row=page.locator('tr[data-listing-id="800001"]')
                if path=='/ours':
                    await page.clock.set_fixed_time('2030-01-01T12:00:00Z')
                    await page.evaluate('refresh()')
                    await expect(row.locator('.viewing-date-cell .visit-overdue')).to_be_hidden()
                    await page.clock.set_fixed_time('2030-01-02T12:00:00Z')
                    await page.evaluate('refresh()')
                    await expect(row.locator('.viewing-date-cell .visit-overdue')).to_be_visible()
                await row.locator('.realtor-edit').click()
                await expect(row.locator('.realtor-comment')).to_have_value(realtor_note(1))
                assert len(detail_reads)==1
                draft='Сохранение риелтора: '+('полный текст, не усечённый preview. '*30).rstrip()
                field=row.locator('.realtor-comment')
                await field.fill(draft)
                draft=await field.input_value()  # Preserve the actual browser edit, including WebKit whitespace.
                await field.evaluate('e=>{e.focus();e.setSelectionRange(9,9)}')
                await page.evaluate('refresh()')  # conditional 304
                assert state_reads[-1] is not None
                await expect(field).to_have_value(draft)
                assert await field.evaluate('e=>document.activeElement===e&&e.selectionStart===9')
                await page.evaluate('Promise.all([refresh(true),refresh(true),refresh(true)])')
                assert server.maximum==1
                await expect(field).to_have_value(draft)
                server.fail_next=True
                await page.evaluate('refresh(true)')
                await expect(page.locator('#load-message')).to_contain_text('Показаны предыдущие данные')
                await expect(field).to_have_value(draft)
                assert await page.locator('tr[data-property-id]').count()==(20 if path=='/ours' else 500)
                await row.locator('.realtor-editor .comment-save').click()
                await expect(row.locator('.realtor-editor')).to_be_hidden()
                saved=client.get('/api/listings/800001/details').json()
                assert saved['realtor_comment']==draft and saved['realtor_version']==2
                assert saved['comment_history'][0]['text']==draft and saved['comment_history'][0]['kind']=='realtor'
                assert len([w for w in writes if w[1].endswith('/realtor-review')])==1
                await page.locator('#load-retry').click()
                await expect(page.locator('#load-state')).to_be_hidden()
                await before_reload();await page.reload()
                await expect(page.locator('#nav-all-count')).to_have_text('500')
                await row.locator('.realtor-edit').click()
                await expect(field).to_have_value(draft)
                await shot('editor')
                await row.locator('.realtor-editor .comment-cancel').click()
                assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                await row.locator('.characteristics>summary').click()
                await row.locator('.publication-details>summary').click()
                await expect(row.locator('.publication-details')).to_contain_text('История цен')
                assert 'Синтетическое длинное описание' not in await page.locator('main').inner_text()
                if path=='/ours':
                    control=row.locator('.visit-date-input')
                    await control.fill('12.12.2030')
                    before=len(writes)
                    await page.evaluate('refresh(true)')
                    await expect(control).to_have_value('12.12.2030')
                    assert len(writes)==before
                    await row.locator('.visit-date-ok').click()
                    await expect(row.locator('.visit-date-status')).to_have_text('Дата сохранена.')
                    await expect(control).to_have_value('12.12.2030')
                    await page.reload()
                    await expect(row.locator('.visit-date-input')).to_have_value('12.12.2030')
                await row.scroll_into_view_if_needed();await shot('list')
                # Explicit visibility transition: no polling or force-free fetch
                # while hidden; the scheduled timer is cancelled.
                await page.evaluate("Object.defineProperty(document,'hidden',{configurable:true,get:()=>true});document.dispatchEvent(new Event('visibilitychange'));")
                count=len(state_reads)
                deadline=time.monotonic()+5
                while server.inflight:
                    assert time.monotonic()<deadline,'cancelled request did not finish'
                    await asyncio.sleep(.05)
                await page.evaluate('poll()');await page.evaluate('refresh()')
                assert len(state_reads)==count
                await page.evaluate("Object.defineProperty(document,'hidden',{configurable:true,get:()=>false});document.dispatchEvent(new Event('visibilitychange'));")
                await page.evaluate('refresh()')
                assert len(state_reads)>count and server.maximum==1
                assert any(v['status']==304 and v['bytes']==0 for v in server.wire)
                assert any(v['status']==200 and v['encoding']=='gzip' for v in server.wire)
                assert failures==[]
            except BaseException:
                try:await shot('failure')
                except Exception:pass
                raise
            finally:
                server.release.set();await context.close();await browser.close()
    asyncio.run(scenario())
