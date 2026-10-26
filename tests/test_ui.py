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
                    'Фото','№ ЦИАН','ЖК / адрес','Цена','Ипотека / мес.','Площадь','Этаж','Метро','До метро','Добавлено','Статус','Риелтор','Дата просмотра','Результат просмотра']
                assert await page.locator('.table-wrap').evaluate('el => el.scrollWidth <= el.clientWidth')
                assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                await expect(page.locator('#last-sync')).to_contain_text('16.10.2026')
                assert await page.locator('#price,#metro').count() == 0
                await expect(page.locator('#criteria-price')).to_have_text('Бюджет до 14,5 млн ₽')
                await expect(page.locator('#criteria-metro')).to_contain_text('20 минут')
                row = page.locator('tr[data-listing-id="123"]')
                link = row.locator('td').nth(1).locator('a')
                assert await link.text_content() == '123'
                assert await link.get_attribute('target') == '_blank'
                assert await link.get_attribute('rel') == 'noopener noreferrer'
                stale_state = client.get('/api/state').json()
                await row.locator('.publication-details').evaluate('el => {el.open=true}')
                await page.evaluate('window.savedRow = document.querySelector(\'tr[data-listing-id="123"]\')')
                await row.locator('.review-select').select_option('viewed')
                await expect(row.locator('.review-label')).to_have_text('Просмотрено')
                await expect(row.locator('.review-meta')).to_contain_text('Риелтор')
                inject_stale = True
                await page.evaluate('refresh()')
                assert stale_injected == 1
                await expect(row.locator('.review-label')).to_have_text('Просмотрено')
                assert await row.locator('.publication-details').evaluate('el => el.open')
                assert await page.evaluate('window.savedRow === document.querySelector(\'tr[data-listing-id="123"]\')')
                before = state_reads
                await page.locator('[data-review-filter="new"]').click()
                assert not await row.is_visible()
                await page.locator('[data-review-filter="all"]').click()
                assert await row.is_visible()
                assert state_reads == before
                await row.locator('.review-select').select_option('rejected')
                await expect(row.locator('.review-label')).to_have_text('Не подходит')
                await expect(row).to_have_class('review-rejected')
                rejected_background = await row.locator('td').first.evaluate('el => getComputedStyle(el).backgroundColor')
                await expect(row).to_be_hidden()
                await page.locator('#rejected-section > summary').click()
                await row.locator('.review-select').select_option('new')
                await expect(row.locator('.review-label')).to_have_text('НОВОЕ')
                await page.locator('#rejected-section > summary').click()
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
                await expect(row).to_have_class('review-rejected')
                assert await row.locator('td').first.evaluate('el => getComputedStyle(el).backgroundColor') == rejected_background
                await expect(row.locator('.review-label')).to_have_text('НОВОЕ')
                await expect(page.locator('#rejected-count')).to_have_text('1')
                await expect(page.locator('[data-review-filter="new"] span')).to_have_text('2')
                await page.locator('[data-review-filter="new"]').click()
                await expect(row).to_be_hidden()
                await page.locator('#rejected-section > summary').click()
                await expect(row).to_be_visible()
                await page.locator('[data-review-filter="all"]').click()
                await expect(row.locator('.realtor-meta')).to_contain_text('Эксперт')
                await row.locator('.realtor-details > summary').click()
                await expect(row.locator('.comment-text')).to_have_text(comment)
                assert await row.locator('img:not(.listing-cover)').count() == 0
                inject_stale = True
                await page.evaluate('refresh()')
                await expect(row.locator('.realtor-label')).to_have_text('Отбраковано')
                await expect(row.locator('.realtor-comment')).to_have_value(comment)
                await expect(row).to_have_class('review-rejected')
                await page.reload()
                await expect(row).to_have_class('review-rejected')
                await expect(row).to_be_hidden()
                await page.locator('#rejected-section > summary').click()
                await row.locator('.realtor-editor > summary').click()
                await row.locator('.realtor-select').select_option('approved')
                await row.locator('.realtor-editor button').click()
                await expect(row).to_have_class('review-new')
                await expect(page.locator('#rejected-count')).to_have_text('0')
                await expect(page.locator('[data-review-filter="new"] span')).to_have_text('3')
                saved_item = next(item for item in client.get('/api/state').json()['listings'] if str(item['id']) == '123')
                assert saved_item['review_status'] == 'new'
                assert saved_item['realtor_status'] == 'approved'
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
                # Schedule and record a shared viewing from a phone. Drafts
                # survive polls, and the outcome drives the existing family status.
                visit_row = page.locator('tr[data-listing-id="789"]')
                await visit_row.locator('td').last.get_by_role('button',name='Добавить результат',exact=True).click()
                await page.locator('#visit-state').select_option('planned')
                await expect(page.locator('#visit-dialog')).to_be_visible()
                await page.locator('#visit-date').fill('2026-10-01T18:30')
                await page.locator('#visit-preparation').fill('Вход со двора')
                await page.evaluate('refresh()')
                await expect(page.locator('#visit-preparation')).to_have_value('Вход со двора')
                assert await page.locator('#visit-dialog').evaluate('el=>el.scrollWidth<=el.clientWidth')
                await page.locator('#visit-save').click()
                await expect(page.locator('#visit-dialog')).not_to_be_visible()
                await page.locator('[data-review-filter="planned"]').click()
                await expect(page.locator('#visit-agenda .visit-card')).to_have_count(1)
                await expect(page.locator('#visit-agenda')).to_contain_text('Укажите результат')
                await page.locator('#visit-agenda').get_by_role('button',name='Записать результат',exact=True).click()
                await page.locator('[data-visit-outcome="rejected"]').click()
                await page.locator('#visit-comment').fill('<script>Не понравилось</script> · Шумно')
                await page.locator('#visit-save').click()
                await expect(page.locator('#visit-dialog')).not_to_be_visible()
                await page.locator('#rejected-section > summary').click()
                await expect(visit_row).to_be_visible()
                await expect(visit_row).to_have_class('review-rejected')
                await expect(visit_row.locator('.review-label')).to_have_text('Не подходит')
                await expect(visit_row.locator('td').last).to_contain_text('Не понравилось')
                await page.reload()
                await expect(visit_row).to_be_hidden()
                await page.locator('#rejected-section > summary').click()
                await expect(visit_row.locator('td').last).to_contain_text('Не понравилось')
                await visit_row.locator('td').last.get_by_role('button',name='Добавить результат',exact=True).click()
                await page.locator('#visit-record-choice').select_option('new')
                await page.locator('#visit-state').select_option('planned')
                await page.locator('#visit-date').fill('2026-11-01T18:30')
                await page.locator('#visit-save').click()
                await expect(page.locator('#visit-dialog')).not_to_be_visible()
                await expect(visit_row).to_have_class('review-rejected')
                await expect(visit_row.locator('.visit-history')).to_contain_text('Не понравилось')
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



def test_cover_column_modal_placeholder_refresh_and_hidden_marketing(tmp_path,monkeypatch):
    from PIL import Image
    import io
    from app import covers as image_module
    from tests.test_phase3 import sample
    monkeypatch.setenv('DATA_DIR',str(tmp_path));monkeypatch.setenv('APP_USER','owner')
    monkeypatch.setenv('APP_PASSWORD','long-test-password');monkeypatch.setenv('BROWSER_SCHEDULE_ENABLED','false')
    monkeypatch.delenv('PUBLIC_URL',raising=False)
    from app import main
    importlib.reload(main)
    color={'value':'green'}
    def picture(_):
        data=io.BytesIO()
        with Image.new('RGB',(1200,800),color['value']) as image:image.save(data,format='JPEG')
        return data.getvalue()
    monkeypatch.setattr(image_module,'download_cover',picture)
    with main.database() as conn:
        main.save_items(conn,[
            dict(sample('8100'),cover_image_url='https://images.cdn-cian.ru/images/first.jpg',description='NEVER_SHOW_MARKETING',card_text='NEVER_SHOW_MARKETING'),
            sample('8101',address='Москва, улица с очень длинным названием для переноса, д. 21'),
            dict(sample('8102',address='Москва, ул. Третья, д. 30'),cover_image_url='https://images.cdn-cian.ru/images/missing.jpg'),
        ],'2026-10-22T10:00:00+00:00')
    main.covers.sync_one('8100');main.covers.sync_one('8102')
    with main.database() as conn:
        missing=conn.execute("SELECT cover_thumb_path FROM listings WHERE id='8102'").fetchone()[0]
    main.covers.path(missing).unlink()
    with TestClient(main.app,base_url='https://xata.test') as client:
        sign_in(client)
        assert client.post('/api/users',json={'username':'family','display_name':'Семья','password':'12345678','role':'reviewer'}).status_code==201
        sign_in(client,'family','12345678')
        async def scenario():
            from playwright.async_api import async_playwright,expect
            requests=[]
            async def bridge(route):
                request=route.request;parsed=urlsplit(request.url)
                assert parsed.hostname=='xata.test'
                path=parsed.path+('?' + parsed.query if parsed.query else '')
                requests.append(path)
                headers={key:value for key,value in request.headers.items()
                         if key.lower() in ('content-type','origin','sec-fetch-site','x-csrf-token')}
                response=client.request(request.method,path,headers=headers,content=request.post_data_buffer,follow_redirects=False)
                forwarded={key:value for key,value in response.headers.items()
                           if key.lower() not in ('set-cookie','content-length','content-encoding','transfer-encoding')}
                await route.fulfill(status=response.status_code,headers=forwarded,body=response.content)
            async with async_playwright() as playwright:
                browser=await playwright.chromium.launch(headless=True)
                context=await browser.new_context(viewport={'width':1440,'height':1100})
                await context.route('**/*',bridge)
                page=await context.new_page()
                errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                await page.goto('https://xata.test/')
                await expect(page.locator('#listings tr')).to_have_count(3)
                await expect(page.locator('#listing-table th').first).to_have_text('Фото')
                row=page.locator('tr[data-listing-id="8100"]')
                thumb=row.locator('.listing-cover')
                await expect(row.locator('.cover-button')).to_be_visible()
                await page.wait_for_function("document.querySelector('tr[data-listing-id=\"8100\"] img').naturalWidth>0")
                assert await thumb.get_attribute('loading')=='lazy'
                assert await thumb.evaluate('el=>getComputedStyle(el).objectFit')=='cover'
                size=await thumb.bounding_box()
                assert 100<=size['width']<=111 and size['height']==80
                assert not any(path.endswith('/normal') for path in requests)
                await expect(page.locator('tr[data-listing-id="8101"] .cover-placeholder')).to_be_visible()
                await expect(page.locator('tr[data-listing-id="8102"] .cover-placeholder')).to_be_visible()
                assert await page.locator('.card-details').count()==0
                assert 'Текст карточки / описание' not in await page.locator('body').inner_text()
                assert 'NEVER_SHOW_MARKETING' not in await page.locator('body').inner_text()
                await row.locator('.cover-button').click()
                await expect(page.locator('#cover-dialog')).to_be_visible()
                await page.wait_for_function("document.querySelector('#cover-large').naturalWidth>0")
                assert len([path for path in requests if path.endswith('/normal')])==1
                await expect(page.locator('#cover-cian')).to_have_text('ЦИАН 8100')
                assert 'NEVER_SHOW_MARKETING' not in await page.locator('#cover-dialog').inner_text()
                await page.locator('#cover-close').click()
                await expect(page.locator('#cover-dialog')).to_be_hidden()
                await row.locator('.publication-details > summary').click()
                await expect(row.locator('.publication-details p')).to_contain_text('CIAN 8100')
                await row.locator('.review-select').select_option('liked')
                await expect(row.locator('.review-label')).to_have_text('Понравилось')
                old_src=await thumb.get_attribute('src')
                color['value']='red'
                with main.database() as conn:
                    main.save_items(conn,[{'id':'8100','cover_image_url':'https://images.cdn-cian.ru/images/changed.jpg'}],
                                    '2026-10-22T11:00:00+00:00')
                main.covers.sync_one('8100')
                await page.evaluate('refresh(true)')
                assert await thumb.get_attribute('src')!=old_src
                await page.wait_for_function("document.querySelector('tr[data-listing-id=\"8100\"] img').naturalWidth>0")
                assert await row.locator('.publication-details').evaluate('el=>el.open')
                # Restoring a missing file may retain the same content URL.
                # The placeholder must recover on a bounded retry, without a reload.
                broken_src=await page.locator('tr[data-listing-id="8102"] img').get_attribute('src')
                color['value']='green'
                main.covers.sync_one('8102')
                await page.evaluate("for (const entry of rows.values()) entry.failedCoverAt=0")
                await page.evaluate('refresh(true)')
                await expect(page.locator('tr[data-listing-id="8102"] .cover-button')).to_be_visible()
                await page.wait_for_function("document.querySelector('tr[data-listing-id=\"8102\"] img').naturalWidth>0")
                assert await page.locator('tr[data-listing-id="8102"] img').get_attribute('src')==broken_src
                assert 'NEVER_SHOW_MARKETING' not in await page.locator('body').inner_text()
                assert await page.locator('.table-wrap').evaluate('el=>el.scrollWidth<=el.clientWidth')
                assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                await page.set_viewport_size({'width':390,'height':844})
                assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                await expect(row.locator('.cover-button')).to_be_visible()
                assert errors==[]
                await context.close();await browser.close()
        asyncio.run(scenario())
    with main.database() as conn:
        data=conn.execute("SELECT description,card_text FROM listings WHERE id='8100'").fetchone()
        assert tuple(data)==('NEVER_SHOW_MARKETING','NEVER_SHOW_MARKETING')


def test_viewing_dates_rejections_agenda_conflicts_and_comparison_on_phone(tmp_path,monkeypatch):
    from PIL import Image
    from app import covers as images
    import io
    monkeypatch.setenv('DATA_DIR',str(tmp_path));monkeypatch.setenv('APP_USER','owner')
    monkeypatch.setenv('APP_PASSWORD','long-test-password');monkeypatch.setenv('BROWSER_SCHEDULE_ENABLED','false')
    monkeypatch.delenv('PUBLIC_URL',raising=False)
    from app import main
    importlib.reload(main)
    def picture(_):
        stream=io.BytesIO()
        with Image.new('RGB',(600,400),'green') as image:image.save(stream,format='JPEG')
        return stream.getvalue()
    monkeypatch.setattr(images,'download_cover',picture)
    with main.database() as conn:
        main.save_items(conn,[{'id':str(key),'url':'https://cian.ru/sale/flat/'+str(key)+'/',
            'title':'1-комнатная квартира','complex_name':'ЖК '+str(key),'address':'Москва, ул. Тестовая, д. '+str(key),
            'price':None if key==9103 else 12000000,'area':0 if key==9103 else 40,'floor':'7/16',
            'metro':'Сокол','metro_time':'8 мин. пешком',
            **({'cover_image_url':'https://images.cdn-cian.ru/images/one.jpg'} if key==9100 else {})}
            for key in range(9100,9106)],'2026-10-26T10:00:00+00:00')
    main.covers.sync_one('9100')
    with TestClient(main.app,base_url='https://xata.test') as client:
        sign_in(client)
        assert client.post('/api/listings/9102/realtor-review',json={'status':'rejected','comment':'Документы требуют проверки'}).status_code==200
        assert client.post('/api/users',json={'username':'family','display_name':'Семья','password':'12345678','role':'reviewer'}).status_code==201
        sign_in(client,'family','12345678')
        def state():
            return {item['id']:item for item in client.get('/api/state').json()['listings']}
        def visit(key,**changes):
            item=state()[key]
            response=client.post('/api/listings/'+key+'/visits',json={
                'property_id':item['property_id'],'status':'planned','scheduled_at':'2026-10-01T18:30:00+03:00',
                'preparation':'До поездки: <img src=x onerror=alert(1)> проверить окна',**changes})
            assert response.status_code==200,response.text
            return response.json()['visits'][0]
        past=visit('9100')
        visit('9100',scheduled_at='2050-11-01T18:30:00+03:00')
        visit('9101',status='completed',outcome='liked',completed_at='2026-09-01T18:30:43.123456+03:00',comment='Понравилась тишина')
        cancelled=visit('9102',scheduled_at='2030-11-01T18:30:00+03:00')
        async def scenario():
            from playwright.async_api import async_playwright,expect
            writes=[]
            async def bridge(route):
                request=route.request;parsed=urlsplit(request.url)
                assert parsed.hostname=='xata.test'
                path=parsed.path+('?' + parsed.query if parsed.query else '')
                if request.method not in ('GET','HEAD'):writes.append((request.method,path))
                headers={k:v for k,v in request.headers.items() if k.lower() in ('content-type','origin','sec-fetch-site','x-csrf-token')}
                response=client.request(request.method,path,headers=headers,content=request.post_data_buffer,follow_redirects=False)
                forwarded={k:v for k,v in response.headers.items() if k.lower() not in ('set-cookie','content-length','content-encoding','transfer-encoding')}
                await route.fulfill(status=response.status_code,headers=forwarded,body=response.content)
            async with async_playwright() as playwright:
                browser=await playwright.chromium.launch(headless=True)
                context=await browser.new_context(viewport={'width':1440,'height':1100})
                await context.route('**/*',bridge)
                page=await context.new_page()
                errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                await page.goto('https://xata.test/')
                await expect(page.locator('#listings tr')).to_have_count(5)
                await expect(page.locator('#rejected-listings tr')).to_have_count(1)
                assert not await page.locator('#rejected-section').evaluate('el=>el.open')
                await expect(page.locator('#rejected-count')).to_have_text('1')
                await expect(page.locator('[data-review-filter="all"] span')).to_have_text('5')
                await expect(page.locator('[data-review-filter="planned"] span')).to_have_text('3')
                row=page.locator('tr[data-listing-id="9100"]')
                await expect(row.locator('td').nth(12)).to_contain_text('Укажите результат просмотра')
                sort=page.locator('#listing-table [data-sort="visit_date"]')
                await sort.click()
                assert (await page.locator('#listings tr').evaluate_all('rows=>rows.map(r=>r.dataset.listingId)'))[:2]==['9101','9100']
                await sort.click()
                assert (await page.locator('#listings tr').evaluate_all('rows=>rows.map(r=>r.dataset.listingId)'))[:2]==['9100','9101']
                await sort.click()
                assert await sort.locator('..').get_attribute('aria-sort')=='none'
                assert not await page.locator('#rejected-section').evaluate('el=>el.open')
                await page.set_viewport_size({'width':390,'height':844})
                await page.locator('[data-review-filter="planned"]').click()
                cards=page.locator('#visit-agenda .visit-card')
                await expect(cards).to_have_count(3)
                assert await cards.evaluate_all('cards=>cards.map(c=>c.dataset.visitId)')==[str(past['id']),str(cancelled['id']),str(past['id']+1)]
                await expect(cards.first.locator('.visit-preparation')).to_contain_text('проверить окна')
                await expect(cards.first.locator('.visit-overdue')).to_have_text('Укажите результат просмотра')
                refused=page.locator('#visit-agenda .visit-card[data-listing-id="9102"]')
                await expect(refused.locator('.visit-rejected')).to_contain_text('Не подходит')
                await refused.get_by_role('button',name='Отменить',exact=True).click()
                await page.locator('#visit-save').click()
                await expect(page.locator('#visit-dialog')).to_be_hidden()
                await expect(cards).to_have_count(2)
                await expect(page.locator('[data-review-filter="planned"] span')).to_have_text('2')
                assert state()['9102']['realtor_status']=='rejected'
                assert state()['9100']['review_status']=='new'
                card=page.locator('#visit-agenda .visit-card[data-visit-id="'+str(past['id'])+'"]')
                await card.get_by_role('button',name='Записать результат',exact=True).click()
                await page.locator('[data-visit-outcome="rejected"]').click()
                await page.locator('#visit-date').fill('2026-10-10T19:45')
                await page.locator('#visit-comment').fill('<b>После поездки</b>: шумно')
                await page.locator('#visit-preparation').fill('До поездки: спросить про документы')
                await page.locator('#visit-reasons input[value="noise"]').check()
                await page.locator('#visit-reasons input[value="documents"]').check()
                item=state()['9100']
                response=client.patch('/api/listings/9100/visits/'+str(past['id']),json={
                    'property_id':item['property_id'],'version':past['version'],'status':'planned',
                    'scheduled_at':past['scheduled_at'],'preparation':'Заметка другого пользователя'})
                assert response.status_code==200
                await page.evaluate('refresh(true)')
                await expect(page.locator('#visit-comment')).to_have_value('<b>После поездки</b>: шумно')
                await expect(page.locator('#visit-preparation')).to_have_value('До поездки: спросить про документы')
                await page.locator('#visit-save').click()
                await expect(page.locator('#visit-error')).to_contain_text('Просмотр уже изменён')
                await expect(page.locator('#visit-comment')).to_have_value('<b>После поездки</b>: шумно')
                assert next(v for v in state()['9100']['visits'] if v['id']==past['id'])['status']=='planned'
                await page.locator('#visit-close').click()
                await card.get_by_role('button',name='Записать результат',exact=True).click()
                await page.locator('[data-visit-outcome="rejected"]').click()
                await page.locator('#visit-date').fill('2026-10-10T19:45')
                await page.locator('#visit-comment').fill('<b>После поездки</b>: шумно')
                await page.locator('#visit-preparation').fill('До поездки: спросить про документы')
                await page.locator('#visit-reasons input[value="noise"]').check()
                await page.locator('#visit-reasons input[value="documents"]').check()
                assert await page.locator('#visit-dialog').evaluate('el=>el.scrollWidth<=el.clientWidth')
                writes_before=len(writes)
                await page.evaluate("const form=document.querySelector('#visit-form');form.requestSubmit();form.requestSubmit();")
                await expect(page.locator('#visit-dialog')).to_be_hidden()
                assert len(writes)==writes_before+1
                await expect(page.locator('#rejected-count')).to_have_text('2')
                assert not await page.locator('#rejected-section').evaluate('el=>el.open')
                assert len(state()['9100']['visits'])==2
                result=next(v for v in state()['9100']['visits'] if v['id']==past['id'])
                assert result['rejection_reasons']==['noise','documents']
                assert result['completed_at']=='2026-10-10T16:45:00+00:00'
                await page.locator('[data-review-filter="all"]').click()
                await expect(page.locator('#listings tr[data-listing-id="9100"]')).to_have_count(0)
                await page.locator('#rejected-section > summary').click()
                await expect(row).to_be_visible()
                await expect(row.locator('.visit-rejection-reasons').first).to_contain_text('Шум, Документы')
                assert await row.locator('b').count()==0
                await row.locator('.review-select').select_option('liked')
                await expect(page.locator('#listings tr[data-listing-id="9100"]')).to_have_count(1)
                # Old negative history alone does not prevent returning.
                assert state()['9100']['visits'][1]['outcome']=='rejected'
                refused_row=page.locator('tr[data-listing-id="9102"]')
                await refused_row.locator('.review-select').select_option('liked')
                await expect(refused_row.locator('.return-hint')).to_contain_text('отказ риелтора')
                await expect(refused_row.locator('.realtor-editor')).to_be_hidden()
                await expect(page.locator('#listings tr[data-listing-id="9102"]')).to_have_count(0)
                await page.locator('#rejected-section > summary').click()
                await page.locator('[data-review-filter="new"]').click()
                assert not await page.locator('#rejected-section').evaluate('el=>el.open')
                await page.locator('[data-review-filter="all"]').click()
                original_stamp=state()['9101']['visits'][0]['completed_at']
                positive_row=page.locator('tr[data-listing-id="9101"]')
                await positive_row.locator('td').last.get_by_role('button',name='Добавить результат',exact=True).click()
                await page.locator('#visit-comment').fill('Понравилась тишина. Уточнение после поездки.')
                await page.locator('#visit-save').click()
                await expect(page.locator('#visit-dialog')).to_be_hidden()
                assert state()['9101']['visits'][0]['completed_at']==original_stamp
                assert state()['9101']['review_status']=='liked'
                before=state()
                write_count=len(writes)
                for key in ('9100','9101','9103','9104'):
                    await page.locator('tr[data-listing-id="'+key+'"] .compare-select').check()
                await page.locator('tr[data-listing-id="9105"] .compare-select').click()
                await expect(page.locator('tr[data-listing-id="9105"] .compare-select')).not_to_be_checked()
                await expect(page.locator('#compare-count')).to_have_text('4')
                await expect(page.locator('#compare-message')).to_contain_text('не более четырёх')
                await page.locator('#compare-open').click()
                await expect(page.locator('#compare-dialog')).to_be_visible()
                await expect(page.locator('#compare-table thead th')).to_have_count(5)
                await expect(page.locator('#compare-table .compare-cover')).to_have_count(1)
                await page.wait_for_function("document.querySelector('#compare-table img').naturalWidth>0")
                square=page.get_by_role('rowheader',name='Цена за м²',exact=True).locator('..')
                await expect(square.locator('td').first).to_contain_text('300')
                await expect(square.locator('td').nth(2)).to_have_text('—')
                result_row=page.get_by_role('rowheader',name='Результат просмотра',exact=True).locator('..')
                await expect(result_row.locator('td').first).to_have_text('Не понравилось')
                await expect(result_row.locator('td').nth(1)).to_have_text('Понравилось')
                await expect(result_row.locator('td').nth(2)).to_have_text('—')
                assert await page.locator('#compare-table b').count()==0
                assert await page.locator('#compare-scroll').evaluate('el=>el.scrollWidth>el.clientWidth')
                assert await page.locator('#compare-dialog').evaluate('el=>el.scrollWidth<=el.clientWidth')
                assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                assert len(writes)==write_count and state()==before
                assert client.post('/api/listings/9100/review',json={'status':'rejected'}).status_code==200
                await page.evaluate('refresh(true)')
                await expect(page.locator('#compare-count')).to_have_text('3')
                await expect(page.locator('#compare-table thead th')).to_have_count(4)
                await expect(page.locator('#compare-dialog-message')).to_contain_text('убрана из сравнения')
                await page.locator('#compare-close').click()
                await page.reload()
                await expect(page.locator('#rejected-count')).to_have_text('2')
                assert not await page.locator('#rejected-section').evaluate('el=>el.open')
                await page.locator('#rejected-section > summary').click()
                await expect(row.locator('.visit-rejection-reasons').first).to_contain_text('Шум, Документы')
                persisted=next(v for v in state()['9100']['visits'] if v['id']==past['id'])
                assert persisted['preparation']=='До поездки: спросить про документы'
                assert persisted['comment']=='<b>После поездки</b>: шумно'
                assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                assert errors==[]
                await context.close();await browser.close()
        asyncio.run(scenario())


def test_inline_date_permissions_mobile_reload_conflicts_and_single_result_button(tmp_path,monkeypatch):
    from uuid import uuid4
    monkeypatch.setenv('DATA_DIR',str(tmp_path));monkeypatch.setenv('APP_USER','owner')
    monkeypatch.setenv('APP_PASSWORD','long-test-password');monkeypatch.setenv('BROWSER_SCHEDULE_ENABLED','false')
    monkeypatch.delenv('PUBLIC_URL',raising=False)
    from app import main
    importlib.reload(main)
    monkeypatch.setattr(main.CianBrowserSource,'status',lambda self:{'status':'authorized','message':'Авторизован','manual_access':False})
    with main.database() as conn:
        main.save_items(conn,[{'id':str(key),'url':'https://cian.ru/sale/flat/'+str(key)+'/',
            'title':'Квартира','address':'Москва, Тестовая улица, д. '+str(key),'price':12000000}
            for key in (9300,9301)],'2026-10-26T10:00:00+00:00')
    with TestClient(main.app,base_url='https://xata.test') as client:
        sign_in(client)
        for username,role in [('expert','realtor'),('buyer','reviewer')]:
            assert client.post('/api/users',json={'username':username,'display_name':username,'password':'password-123','role':role}).status_code==201
        sign_in(client,'expert','password-123')
        def current(key='9300'):
            return next(i for i in client.get('/api/state').json()['listings'] if i['id']==key)
        async def scenario():
            from playwright.async_api import async_playwright,expect
            calls=[]
            async def bridge(route):
                request=route.request;parsed=urlsplit(request.url)
                assert parsed.hostname=='xata.test'
                path=parsed.path+('?' + parsed.query if parsed.query else '')
                if request.method not in ('GET','HEAD'):calls.append((request.method,path))
                headers={k:v for k,v in request.headers.items() if k.lower() in ('content-type','origin','sec-fetch-site','x-csrf-token')}
                response=client.request(request.method,path,headers=headers,content=request.post_data_buffer,follow_redirects=False)
                forwarded={k:v for k,v in response.headers.items() if k.lower() not in ('set-cookie','content-length','content-encoding','transfer-encoding')}
                await route.fulfill(status=response.status_code,headers=forwarded,body=response.content)
            async with async_playwright() as playwright:
                browser=await playwright.chromium.launch(headless=True)
                context=await browser.new_context(viewport={'width':390,'height':844})
                await context.route('**/*',bridge)
                page=await context.new_page();errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                await page.goto('https://xata.test/')
                row=page.locator('tr[data-listing-id="9300"]')
                date_cell=row.locator('td').nth(12);result_cell=row.locator('td').nth(13)
                field=date_cell.locator('.visit-date-input')
                await expect(field).to_be_visible()
                await expect(result_cell.locator('button')).to_have_count(0)
                await expect(date_cell.locator('button')).to_have_count(0)
                before=current()
                # Two change events from a phone picker must still be one job.
                await field.focus()
                await field.evaluate("input=>{input.value='2026-11-01T18:30';input.dispatchEvent(new Event('input',{bubbles:true}));input.dispatchEvent(new Event('change',{bubbles:true}));input.dispatchEvent(new Event('change',{bubbles:true}));}")
                await expect(date_cell.locator('.visit-date-status')).to_have_text('Дата сохранена.')
                assert len(calls)==1 and calls[0][1].endswith('/visits/date')
                assert not await page.locator('#visit-dialog').is_visible()
                saved=current();visit=saved['visits'][0]
                assert len(saved['visits'])==1 and visit['scheduled_at']=='2026-11-01T15:30:00+00:00'
                assert saved['review_status']==before['review_status']=='new'
                await page.reload()
                await expect(field).to_have_value('2026-11-01T18:30')
                # Polling preserves the draft and its original revision.
                await field.focus()
                await field.evaluate("input=>{input.value='2026-11-03T19:00';input.dispatchEvent(new Event('input',{bubbles:true}));}")
                state=current()
                external=client.patch('/api/listings/9300/visits/date',json={
                    'property_id':state['property_id'],'visits_version':state['visits_version'],
                    'visit_id':visit['id'],'date':'2026-11-02T20:00:00+03:00'})
                assert external.status_code==200
                await page.evaluate('refresh(true)')
                await expect(field).to_have_value('2026-11-03T19:00')
                await field.dispatch_event('change')
                await expect(date_cell.locator('.visit-date-status')).to_contain_text('уже изменены')
                assert current()['visits'][0]['scheduled_at']=='2026-11-02T17:00:00+00:00'
                assert len(current()['visits'])==1
                await page.reload()
                await expect(field).to_have_value('2026-11-02T20:00')
                await field.fill('2026-11-03T18:45')
                await field.dispatch_event('change')
                await expect(date_cell.locator('.visit-date-status')).to_have_text('Дата сохранена.')
                assert current()['visits'][0]['id']==visit['id']
                assert current()['review_status']=='new'
                assert (await field.bounding_box())['height']>=44
                assert (await field.bounding_box())['width']>=210
                assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                # Buyers see a value, never the inline editing control.
                sign_in(client,'buyer','password-123')
                await page.reload()
                await expect(date_cell.locator('input')).to_have_count(0)
                await expect(date_cell).to_contain_text('03.11.2026')
                await expect(result_cell.locator('button')).to_have_count(1)
                await expect(result_cell.locator('button')).to_have_text('Добавить результат')
                state=current()
                assert client.patch('/api/listings/9300/visits/date',json={
                    'property_id':state['property_id'],'visits_version':state['visits_version'],
                    'visit_id':visit['id'],'date':'2026-11-04T18:00:00+03:00'}).status_code==403
                await result_cell.get_by_role('button',name='Добавить результат',exact=True).click()
                await expect(page.locator('#visit-record-choice')).to_have_value(str(visit['id']))
                await page.locator('[data-visit-outcome="rejected"]').click()
                await page.locator('#visit-date').fill('2026-11-03T20:15')
                await page.locator('#visit-preparation').fill('Спросить про документы')
                await page.locator('#visit-comment').fill('Шумно после просмотра')
                await page.locator('#visit-reasons input[value="noise"]').check()
                await page.locator('#visit-save').click()
                await expect(page.locator('#visit-dialog')).to_be_hidden()
                assert len(current()['visits'])==1
                assert current()['visits'][0]['id']==visit['id']
                await page.locator('#rejected-section > summary').click()
                await expect(result_cell.locator('button')).to_have_count(1)
                await result_cell.get_by_role('button',name='Добавить результат',exact=True).click()
                await expect(page.locator('#visit-comment')).to_have_value('Шумно после просмотра')
                await expect(page.locator('#visit-reasons input[value="noise"]')).to_be_checked()
                await page.locator('[data-visit-outcome="liked"]').click()
                await page.locator('#visit-save').click()
                await expect(page.locator('#visit-dialog')).to_be_hidden()
                assert len(current()['visits'])==1
                assert current()['review_status']=='liked'
                assert current()['visits'][0]['comment']=='Шумно после просмотра'
                # Admin may also edit the displayed actual date, without
                # changing the outcome or either note.
                sign_in(client)
                await page.reload()
                await expect(field).to_have_value('2026-11-03T20:15')
                before=current()
                await field.fill('2026-11-04T19:30')
                await field.dispatch_event('change')
                await expect(date_cell.locator('.visit-date-status')).to_have_text('Дата сохранена.')
                after=current()
                assert len(after['visits'])==1 and after['visits'][0]['id']==visit['id']
                assert after['review_status']==before['review_status']=='liked'
                for key in ('outcome','preparation','comment','rejection_reasons'):
                    assert after['visits'][0][key]==before['visits'][0][key]
                second=page.locator('tr[data-listing-id="9301"]')
                await second.locator('.visit-date-input').fill('2026-11-02T18:00')
                await second.locator('.visit-date-input').dispatch_event('change')
                await expect(second.locator('.visit-date-status')).to_have_text('Дата сохранена.')
                header=page.locator('#listing-table [data-sort="visit_date"]')
                await header.click()
                assert await page.locator('#listings tr').evaluate_all('rows=>rows.map(r=>r.dataset.listingId)')==['9301','9300']
                await header.click()
                assert await page.locator('#listings tr').evaluate_all('rows=>rows.map(r=>r.dataset.listingId)')==['9300','9301']
                await page.reload()
                await expect(field).to_have_value('2026-11-04T19:30')
                await expect(result_cell.locator('button')).to_have_count(1)
                assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                assert await page.locator('.table-wrap').evaluate('el=>el.scrollWidth>el.clientWidth')
                assert errors==[]
                await context.close();await browser.close()
        asyncio.run(scenario())
