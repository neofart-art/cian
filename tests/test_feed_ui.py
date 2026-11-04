"""500-property UI on emulated slow network; no real Cian or external requests."""
import asyncio
import importlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from tests.helpers import sign_in
from tests.perf_dataset import seed

pytestmark=pytest.mark.skipif(os.getenv('RUN_BROWSER_TESTS')!='1',reason='real browser opt in')


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
        yield main,client


@pytest.mark.parametrize('engine',['chromium','webkit'])
@pytest.mark.parametrize('width',[390,1440])
@pytest.mark.parametrize('path',['/','/ours'])
def test_slow_large_lists_loading_retry_single_flight_and_realtor_save(large_app,engine,width,path):
    main,client=large_app
    async def scenario():
        from playwright.async_api import async_playwright,expect
        started=asyncio.Event();release=asyncio.Event()
        state_reads=[];detail_reads=[];writes=[];inflight=0;maximum=0;initial=True;fail_next=False
        async def bridge(route):
            nonlocal inflight,maximum,initial,fail_next
            req=route.request;url=urlsplit(req.url)
            assert url.hostname=='xata.test','No external requests allowed'
            if req.method in ('PATCH','POST','DELETE'):writes.append((req.method,url.path,req.post_data_json))
            state=url.path=='/api/state'
            if state:
                inflight+=1;maximum=max(maximum,inflight);state_reads.append(req.headers.get('if-none-match'))
                if initial:
                    initial=False;started.set();await release.wait()
                    inflight-=1
                    await route.fulfill(status=503,content_type='application/json',body='{"detail":"Тестовый сбой первой загрузки"}');return
                if fail_next:
                    fail_next=False;inflight-=1
                    await route.fulfill(status=503,content_type='application/json',body='{"detail":"Тестовый фоновый сбой"}');return
            if url.path.endswith('/details'):detail_reads.append(url.path)
            headers={k:v for k,v in req.headers.items() if k.lower() in
                     ('content-type','origin','sec-fetch-site','x-csrf-token','if-none-match','accept-encoding')}
            result=client.request(req.method,url.path+('?' + url.query if url.query else ''),
                headers=headers,content=req.post_data_buffer,follow_redirects=False)
            if state:
                await asyncio.sleep(.15+int(result.headers.get('content-length','0'))/(256*1024))
                inflight-=1
            forwarded={k:v for k,v in result.headers.items() if k.lower() not in ('content-length','content-encoding','set-cookie','transfer-encoding')}
            await route.fulfill(status=result.status_code,headers=forwarded,body=result.content)
        async with async_playwright() as p:
            browser=await getattr(p,engine).launch(headless=True)
            context=await browser.new_context(viewport={'width':width,'height':900},is_mobile=width<900,
                has_touch=width<900,timezone_id='America/Los_Angeles')
            await context.route('**/*',bridge)
            page=await context.new_page();page.set_default_timeout(15000);failures=[]
            page.on('pageerror',lambda e:failures.append(str(e)))
            async def shot(name):
                folder=os.getenv('UI_SCREENSHOT_DIR')
                if folder:
                    target=Path(folder);target.mkdir(parents=True,exist_ok=True)
                    await page.screenshot(path=str(target/(engine+'-'+str(width)+'-perf-'+('ours' if path=='/ours' else 'all')+'-'+name+'.png')))
            try:
                await page.goto('https://xata.test'+path,wait_until='domcontentloaded')
                await asyncio.wait_for(started.wait(),10)
                await expect(page.locator('#nav-all-count')).to_have_text('Загрузка…')
                await expect(page.locator('#count')).to_have_text('Загрузка…')
                await expect(page.locator('#last-sync')).to_contain_text('Загрузка…')
                assert 'ещё не было' not in await page.locator('#last-sync').inner_text()
                release.set()
                await expect(page.locator('#load-retry')).to_be_visible()
                await expect(page.locator('#load-message')).to_contain_text('Не удалось загрузить')
                await expect(page.locator('#count')).to_have_text('Загрузка…')
                await page.locator('#load-retry').click()
                await expect(page.locator('#count')).to_have_text('20' if path=='/ours' else '500')
                await expect(page.locator('#nav-all-count')).to_have_text('500')
                await expect(page.locator('#nav-ours-count')).to_have_text('20')
                assert await page.locator('tr[data-property-id]').count()==(20 if path=='/ours' else 500)
                assert len(detail_reads)==0,'Histories must not load on initial listing'
                assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                row=page.locator('tr[data-listing-id="800001"]')
                await row.locator('.realtor-edit').click()
                await expect(row.locator('.realtor-comment')).to_have_value(('Риелтор проверил квартиру. '*35).strip())
                assert len(detail_reads)==1
                draft='Сохранение риелтора: '+('полный текст, не усечённый preview. '*30)
                field=row.locator('.realtor-comment')
                await field.fill(draft)
                await field.evaluate('e=>{e.focus();e.setSelectionRange(9,9)}')
                await page.evaluate('refresh()')  # conditional 304
                assert state_reads[-1] is not None
                await expect(field).to_have_value(draft)
                assert await field.evaluate('e=>document.activeElement===e&&e.selectionStart===9')
                await page.evaluate('Promise.all([refresh(true),refresh(true),refresh(true)])')
                assert maximum==1
                await expect(field).to_have_value(draft)
                fail_next=True
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
                await page.reload()
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
                    await expect(control).to_have_value('12.12.2030')
                    await page.reload()
                    await expect(row.locator('.visit-date-input')).to_have_value('12.12.2030')
                await row.scroll_into_view_if_needed();await shot('list')
                # Explicit visibility transition: no polling or force-free fetch
                # while hidden; the scheduled timer is cancelled.
                await page.evaluate("Object.defineProperty(document,'hidden',{configurable:true,get:()=>true});document.dispatchEvent(new Event('visibilitychange'));")
                count=len(state_reads)
                await page.evaluate('poll()');await page.evaluate('refresh()')
                assert len(state_reads)==count
                await page.evaluate("Object.defineProperty(document,'hidden',{configurable:true,get:()=>false});document.dispatchEvent(new Event('visibilitychange'));")
                await page.evaluate('refresh()')
                assert len(state_reads)>count and maximum==1
                assert failures==[]
            except BaseException:
                await shot('failure');raise
            finally:
                release.set();await context.close();await browser.close()
    asyncio.run(scenario())
