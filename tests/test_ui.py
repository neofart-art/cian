"""Offline browser UI against the actual FastAPI app via an HTTPS TestClient bridge."""
import asyncio
import importlib
import json
import os
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from tests.helpers import sign_in

pytestmark = pytest.mark.skipif(os.getenv('RUN_BROWSER_TESTS') != '1', reason='Chromium UI tests opt in')


def test_shared_review_ui_login_roles_refresh_and_layout(tmp_path, monkeypatch):
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.setenv('APP_USER', 'owner')
    monkeypatch.setenv('APP_PASSWORD', 'long-test-password')
    monkeypatch.setenv('BROWSER_SCHEDULE_ENABLED', 'false')
    monkeypatch.delenv('PUBLIC_URL', raising=False)
    from app import main
    importlib.reload(main)
    monkeypatch.setattr(main.CianBrowserSource, 'status', lambda self: {'status':'authorized','message':'Авторизован','manual_access':False})
    with main.database() as conn:
        main.save_items(conn, [
            {'id':key,'url':'https://www.cian.ru/sale/flat/' + key + '/',
             'title':'Квартира','complex_name':'ЖК с длинным названием для проверки переноса строки',
             'address':'Москва, улица с длинным названием, дом 15, корпус 2',
             'price':11950000,'area':34.7,'floor':'4/9','metro':'Сокол','metro_time':'15 мин. пешком','card_text':'Описание квартиры для совместного просмотра'}
            for key in ('123','456','999')
        ], '2026-10-16T10:00:00+00:00')

    main.update_browser(last_success='2026-10-16T12:24:00+00:00',last_error='PRIVATE_BROWSER_ERROR')
    with TestClient(main.app, base_url='https://xata.test') as client:
        sign_in(client)
        assert client.post('/api/users', json={'username':'agent','display_name':'Риелтор','password':'reviewer-password-16','role':'reviewer'}).status_code == 201
        assert client.post('/api/users', json={'username':'expert','display_name':'Эксперт','password':'12345678','role':'realtor'}).status_code == 201
        assert client.post('/api/auth/logout').status_code == 200

        async def scenario():
            from playwright.async_api import async_playwright
            from playwright.async_api import expect
            stale_state = None
            inject_stale = False
            stale_injected = 0
            state_reads = 0

            async def bridge(route):
                nonlocal stale_injected, inject_stale, state_reads
                request = route.request
                parsed = urlsplit(request.url)
                assert parsed.hostname == 'xata.test', 'UI must not contact external services'
                path = parsed.path + ('?' + parsed.query if parsed.query else '')
                if path == '/api/state':
                    state_reads += 1
                    if inject_stale:
                        inject_stale = False
                        stale_injected += 1
                        await route.fulfill(status=200, content_type='application/json', body=json.dumps(stale_state))
                        return
                # The bridge has one real server-side cookie jar per browser fixture.
                # Secure/HttpOnly cookie attributes and cross-client isolation are tested in test_multiuser.py.
                headers = {key:value for key,value in request.headers.items()
                           if key.lower() in ('content-type','origin','sec-fetch-site','x-csrf-token')}
                response = client.request(request.method, path, content=request.post_data_buffer,
                                          headers=headers, follow_redirects=False)
                forwarded = {key:value for key,value in response.headers.items()
                             if key.lower() not in ('set-cookie','content-length','content-encoding','transfer-encoding')}
                await route.fulfill(status=response.status_code, headers=forwarded, body=response.content)

            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                context = await browser.new_context(viewport={'width':1440,'height':1100}, timezone_id='Europe/Moscow')
                await context.route('**/*', bridge)
                page = await context.new_page()
                failures = []
                page.on('pageerror', lambda error: failures.append(str(error)))
                # Redirect behavior is covered by API tests; routed redirects
                # leave Playwright's offline bridge, so enter the login form directly.
                await page.goto('https://xata.test/login')
                await page.wait_for_url('https://xata.test/login')
                await page.locator('#username').fill('agent')
                await page.locator('#password').fill('reviewer-password-16')
                await page.locator('#login-submit').click()
                await page.wait_for_url('https://xata.test/')
                await expect(page.locator('#listings tr')).to_have_count(3)
                await expect(page.locator('#identity')).to_contain_text('Риелтор')
                assert await page.locator('[data-admin]:visible').count() == 0
                assert await page.locator('#listing-table th').all_text_contents() == [
                    '№ ЦИАН','ЖК / адрес','Цена','Ипотека / мес.','Площадь','Этаж','Метро','До метро','Добавлено','Статус','Риелтор']
                assert await page.locator('.table-wrap').evaluate('el => el.scrollWidth <= el.clientWidth')
                assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                await expect(page.locator('#last-sync')).to_contain_text('16.10.2026')
                assert await page.locator('#price,#metro').count() == 0
                await expect(page.locator('#criteria-price')).to_have_text('Бюджет до 14,5 млн ₽')
                await expect(page.locator('#criteria-metro')).to_contain_text('20 минут')
                row = page.locator('tr[data-listing-id="123"]')
                link = row.locator('td').nth(0).locator('a')
                assert await link.text_content() == '123'
                assert await link.get_attribute('target') == '_blank'
                assert await link.get_attribute('rel') == 'noopener noreferrer'
                stale_state = client.get('/api/state').json()
                await row.locator('.card-details').evaluate('el => {el.open=true}')
                await page.evaluate('window.savedRow = document.querySelector(\'tr[data-listing-id="123"]\')')
                await row.locator('.review-select').select_option('viewed')
                await expect(row.locator('.review-label')).to_have_text('Просмотрено')
                await expect(row.locator('.review-meta')).to_contain_text('Риелтор')
                inject_stale = True
                await page.evaluate('refresh()')
                assert stale_injected == 1
                await expect(row.locator('.review-label')).to_have_text('Просмотрено')
                assert await row.locator('.card-details').evaluate('el => el.open')
                assert await page.evaluate('window.savedRow === document.querySelector(\'tr[data-listing-id="123"]\')')
                before = state_reads
                await page.locator('[data-review-filter="new"]').click()
                assert not await row.is_visible()
                await page.locator('[data-review-filter="all"]').click()
                assert await row.is_visible()
                assert state_reads == before
                await row.locator('.review-select').select_option('rejected')
                await expect(row.locator('.review-label')).to_have_text('Не подходит')
                await row.locator('.review-select').select_option('new')
                await expect(row.locator('.review-label')).to_have_text('НОВОЕ')
                await page.locator('#logout').click()
                await page.wait_for_url('https://xata.test/login')
                await page.locator('#username').fill('expert')
                await page.locator('#password').fill('12345678')
                await page.locator('#login-submit').click()
                await page.wait_for_url('https://xata.test/')
                await expect(page.locator('#identity')).to_contain_text('Эксперт · Риелтор')
                await expect(page.locator('#listings tr')).to_have_count(3)
                row = page.locator('tr[data-listing-id="123"]')
                await expect(row.locator('.review-select')).to_be_hidden()
                assert await page.locator('[data-admin]:visible').count() == 0
                stale_state = client.get('/api/state').json()
                await row.locator('.realtor-editor > summary').click()
                await row.locator('.realtor-select').select_option('rejected')
                comment = '<img src=x onerror=alert(1)> Плохой подъезд\nДокументы?'
                await row.locator('.realtor-comment').fill(comment)
                await page.evaluate('refresh()')
                await expect(row.locator('.realtor-comment')).to_have_value(comment)
                await row.locator('.realtor-editor button').click()
                await expect(row.locator('.realtor-label')).to_have_text('Отбраковано')
                await expect(row.locator('.realtor-meta')).to_contain_text('Эксперт')
                await row.locator('.realtor-details > summary').click()
                await expect(row.locator('.comment-text')).to_have_text(comment)
                assert await row.locator('img').count() == 0
                inject_stale = True
                await page.evaluate('refresh()')
                await expect(row.locator('.realtor-label')).to_have_text('Отбраковано')
                await expect(row.locator('.realtor-comment')).to_have_value(comment)
                await page.locator('#logout').click()
                await page.wait_for_url('https://xata.test/login')
                await page.locator('#username').fill('owner')
                await page.locator('#password').fill('long-test-password')
                await page.locator('#login-submit').click()
                await page.wait_for_url('https://xata.test/')
                await expect(page.locator('.export')).to_be_visible()
                await page.locator('#users-panel > summary').click()
                await expect(page.locator('#users-list .user-editor')).to_have_count(3)
                await page.locator('#new-username').fill('anna')
                await page.locator('#new-name').fill('Анна')
                assert await page.locator('#new-password').get_attribute('minlength') == '8'
                await page.locator('#new-role').select_option('admin')
                assert await page.locator('#new-password').get_attribute('minlength') == '12'
                await page.locator('#new-role').select_option('reviewer')
                await page.locator('#new-password').fill('12345678')
                await page.locator('#user-create button').click()
                await expect(page.locator('#users-list .user-editor')).to_have_count(4)
                await page.locator('.admin-tools > summary').click()
                await page.locator('#csv-file').set_input_files({
                    'name':'favorites.csv','mimeType':'text/csv',
                    'buffer':b'url;title;price\nhttps://cian.ru/sale/flat/789/;CSV;10000000'
                })
                await expect(page.locator('#import-preview')).to_be_visible()
                await page.locator('#import-confirm').click()
                await expect(page.locator('#listings tr')).to_have_count(4)
                await expect(page.locator('tr[data-listing-id="789"] .review-label')).to_have_text('НОВОЕ')
                await page.set_viewport_size({'width':390,'height':844})
                assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                assert await page.locator('.table-wrap').evaluate('el => el.scrollWidth > el.clientWidth')
                assert failures == []
                await context.close()
                await browser.close()
        asyncio.run(scenario())



def test_property_identity_relisting_and_late_poll_after_manual_merge(tmp_path,monkeypatch):
    from tests.test_phase3 import sample
    monkeypatch.setenv('DATA_DIR',str(tmp_path));monkeypatch.setenv('APP_USER','owner')
    monkeypatch.setenv('APP_PASSWORD','long-test-password');monkeypatch.setenv('BROWSER_SCHEDULE_ENABLED','false')
    monkeypatch.delenv('PUBLIC_URL',raising=False)
    from app import main
    importlib.reload(main)
    monkeypatch.setattr(main.CianBrowserSource,'status',lambda self:{'status':'authorized','message':'Авторизован'})
    with main.database() as conn:
        main.save_items(conn,[sample('7000')],'2026-10-01T10:00:00+00:00')
    with TestClient(main.app,base_url='https://xata.test') as client:
        sign_in(client)
        assert client.post('/api/listings/7000/review',json={'status':'liked'}).status_code==200
        async def scenario():
            from playwright.async_api import async_playwright,expect
            hold=False
            captured=asyncio.Event();release=asyncio.Event();decided=asyncio.Event()
            old_revision=0
            async def bridge(route):
                nonlocal hold,old_revision
                request=route.request;parsed=urlsplit(request.url)
                assert parsed.hostname=='xata.test'
                path=parsed.path+('?' + parsed.query if parsed.query else '')
                if path=='/api/state' and hold:
                    hold=False
                    stale=client.get(path).json()
                    old_revision=stale['identity_revision']
                    captured.set()
                    await release.wait()
                    await route.fulfill(status=200,content_type='application/json',body=json.dumps(stale))
                    return
                headers={key:value for key,value in request.headers.items()
                         if key.lower() in ('content-type','origin','sec-fetch-site','x-csrf-token')}
                response=client.request(request.method,path,headers=headers,content=request.post_data_buffer,follow_redirects=False)
                forwarded={key:value for key,value in response.headers.items()
                           if key.lower() not in ('set-cookie','content-length','content-encoding','transfer-encoding')}
                await route.fulfill(status=response.status_code,headers=forwarded,body=response.content)
                if path.endswith('/duplicate-decision'):
                    assert response.status_code==200
                    decided.set()
            async with async_playwright() as playwright:
                browser=await playwright.chromium.launch(headless=True)
                context=await browser.new_context(viewport={'width':1440,'height':1100})
                await context.route('**/*',bridge)
                page=await context.new_page()
                errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                await page.goto('https://xata.test/')
                await expect(page.locator('#listings tr')).to_have_count(1)
                row=page.locator('tr[data-listing-id="7000"]')
                await page.evaluate("window.beforeRelist=document.querySelector('#listings tr')")
                await row.locator('.realtor-editor > summary').click()
                await row.locator('.realtor-comment').fill('Черновик после перепубликации')
                with main.database() as conn:
                    main.save_items(conn,[sample('7001',price=12500000)],'2026-10-18T10:00:00+00:00')
                await page.evaluate('refresh(true)')
                await expect(page.locator('tr[data-listing-id="7000"]')).to_have_count(0)
                row=page.locator('tr[data-listing-id="7001"]')
                await expect(row.locator('.realtor-comment')).to_have_value('Черновик после перепубликации')
                assert await page.evaluate("window.beforeRelist===document.querySelector('#listings tr')")
                assert await row.locator('.realtor-editor').evaluate('el=>el.open')
                await row.locator('.realtor-select').select_option('approved')
                await row.locator('.realtor-editor button').click()
                await expect(row.locator('.realtor-meta')).not_to_have_text('')
                await expect(row.locator('.review-label')).to_have_text('Понравилось')
                with main.database() as conn:
                    main.save_items(conn,[sample('7002',area=39.4,price=12300000)],'2026-10-19T10:00:00+00:00')
                await page.evaluate('refresh(true)')
                await expect(page.locator('#listings tr')).to_have_count(2)
                source=page.locator('tr[data-listing-id="7002"]')
                await source.locator('.realtor-editor > summary').click()
                await source.locator('.realtor-comment').fill('Несохранённый комментарий')
                await source.locator('.duplicate-actions button').first.click()
                await expect(page.locator('#page-message')).to_contain_text('Сначала сохраните')
                await expect(page.locator('#listings tr')).to_have_count(2)
                await source.locator('.realtor-editor button').click()
                await expect(source.locator('.realtor-meta')).not_to_have_text('')
                await page.wait_for_function('refreshPromise===null')
                hold=True
                await page.evaluate('void refresh()')
                await asyncio.wait_for(captured.wait(),5)
                await page.locator('tr[data-listing-id="7002"] .duplicate-actions button').first.click()
                await asyncio.wait_for(decided.wait(),5)
                await page.wait_for_function('(revision)=>identityRevision>revision',arg=old_revision)
                release.set()
                await page.evaluate('refresh(true)')
                await expect(page.locator('#listings tr')).to_have_count(1)
                row=page.locator('tr[data-listing-id="7002"]')
                await expect(row.locator('.duplicate-box')).to_be_hidden()
                await expect(row.locator('.review-label')).to_have_text('Понравилось')
                await expect(row.locator('.comment-preview')).to_contain_text('Несохранённый комментарий')
                await row.locator('.publication-details > summary').click()
                for listing in ('7000','7001','7002'):
                    await expect(row.locator('.publication-details p')).to_contain_text('CIAN '+listing)
                await expect(row.locator('.publication-details p')).to_contain_text('История цен')
                await row.locator('.review-select').select_option('viewed')
                await expect(row.locator('.review-label')).to_have_text('Просмотрено')
                assert await page.locator('.brand-logo').evaluate('el=>el.complete&&el.naturalWidth>0')
                assert await page.locator('.brand-logo').evaluate('el=>getComputedStyle(el).height')=='58px'
                for width in (390,320):
                    await page.set_viewport_size({'width':width,'height':844})
                    assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    brand=await page.locator('header .brand').bounding_box()
                    identity=await page.locator('header .identity').bounding_box()
                    assert brand['x']+brand['width']<=identity['x']+1
                assert errors==[]
                await context.close();await browser.close()
        asyncio.run(scenario())
