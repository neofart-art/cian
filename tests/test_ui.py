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
                await page.locator('#viewed-rejected-section > summary').click()
                await expect(visit_row).to_be_visible()
                await expect(visit_row).to_have_class('review-rejected viewed-rejected')
                await expect(visit_row.locator('.review-label')).to_have_text('Не подходит')
                await expect(visit_row.locator('td').last).to_contain_text('Не понравилось')
                await page.reload()
                await expect(visit_row).to_be_hidden()
                await page.locator('#viewed-rejected-section > summary').click()
                await expect(visit_row.locator('td').last).to_contain_text('Не понравилось')
                await visit_row.locator('td').last.get_by_role('button',name='Добавить результат',exact=True).click()
                await page.locator('#visit-record-choice').select_option('new')
                await page.locator('#visit-state').select_option('planned')
                await page.locator('#visit-date').fill('2026-11-01T18:30')
                await page.locator('#visit-save').click()
                await expect(page.locator('#visit-dialog')).not_to_be_visible()
                await expect(visit_row).to_have_class('review-rejected viewed-rejected')
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
                await page.evaluate('async()=>{if(refreshPromise)await refreshPromise;}')
                hold=True
                await page.evaluate('void refresh()')
                await asyncio.wait_for(captured.wait(),5)
                await page.locator('tr[data-listing-id="7002"] .duplicate-actions button').first.click()
                await asyncio.wait_for(decided.wait(),5)
                await expect(page.locator('#page-message')).to_have_text('Перепубликации объединены в один объект.')
                assert await page.evaluate('(revision)=>identityRevision>revision',old_revision)
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
                await expect(page.locator('tr[data-listing-id="8100"] img')).not_to_have_js_property('naturalWidth',0)
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
                await expect(page.locator('#cover-large')).not_to_have_js_property('naturalWidth',0)
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
                await expect(page.locator('tr[data-listing-id="8100"] img')).not_to_have_js_property('naturalWidth',0)
                assert await row.locator('.publication-details').evaluate('el=>el.open')
                # Restoring a missing file may retain the same content URL.
                # The placeholder must recover on a bounded retry, without a reload.
                broken_src=await page.locator('tr[data-listing-id="8102"] img').get_attribute('src')
                color['value']='green'
                main.covers.sync_one('8102')
                await page.evaluate("for (const entry of rows.values()) entry.failedCoverAt=0")
                await page.evaluate('refresh(true)')
                await expect(page.locator('tr[data-listing-id="8102"] .cover-button')).to_be_visible()
                await expect(page.locator('tr[data-listing-id="8102"] img')).not_to_have_js_property('naturalWidth',0)
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
                await expect(page.locator('#listings tr')).to_have_count(4)
                await expect(page.locator('#rejected-listings tr')).to_have_count(1)
                assert not await page.locator('#rejected-section').evaluate('el=>el.open')
                await expect(page.locator('#rejected-count')).to_have_text('1')
                await expect(page.locator('[data-review-filter="all"] span')).to_have_text('4')
                await expect(page.locator('[data-review-filter="planned"] span')).to_have_text('3')
                row=page.locator('tr[data-listing-id="9100"]')
                await expect(row.locator('td').nth(12)).to_contain_text('Укажите результат просмотра')
                sort=page.locator('#listing-table [data-sort="visit_date"]')
                await sort.click()
                assert (await page.locator('#listings tr').evaluate_all('rows=>rows.map(r=>r.dataset.listingId)'))[:1]==['9100']
                await sort.click()
                assert (await page.locator('#listings tr').evaluate_all('rows=>rows.map(r=>r.dataset.listingId)'))[:1]==['9100']
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
                await expect(page.locator('#rejected-count')).to_have_text('1')
                assert not await page.locator('#rejected-section').evaluate('el=>el.open')
                assert len(state()['9100']['visits'])==2
                result=next(v for v in state()['9100']['visits'] if v['id']==past['id'])
                assert result['rejection_reasons']==['noise','documents']
                assert result['completed_at']=='2026-10-10T16:45:00+00:00'
                await page.locator('[data-review-filter="all"]').click()
                await expect(page.locator('#listings tr[data-listing-id="9100"]')).to_have_count(0)
                await page.locator('#viewed-rejected-section > summary').click()
                await expect(row).to_be_visible()
                await expect(row.locator('.visit-rejection-reasons').first).to_contain_text('Шум, Документы')
                assert await row.locator('b').count()==0
                await row.locator('.review-select').select_option('liked')
                await expect(page.locator('#viewed-rejected-listings tr[data-listing-id="9100"]')).to_have_count(1)
                # A manual ad opinion cannot replace the actual viewing result.
                assert state()['9100']['visits'][1]['outcome']=='rejected'
                await row.locator('td').last.get_by_role('button',name='Добавить результат',exact=True).click()
                await page.locator('#visit-record-choice').select_option('new')
                await page.locator('[data-visit-outcome="liked"]').click()
                await page.locator('#visit-date').fill('2026-10-11T19:45')
                await page.locator('#visit-save').click()
                await expect(page.locator('#visit-dialog')).to_be_hidden()
                await expect(page.locator('#viewed-liked-listings tr[data-listing-id="9100"]')).to_have_count(1)
                await page.locator('#rejected-section > summary').click()
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
                await expect(page.locator('#compare-table img')).not_to_have_js_property('naturalWidth',0)
                square=page.get_by_role('rowheader',name='Цена за м²',exact=True).locator('..')
                await expect(square.locator('td').first).to_contain_text('300')
                await expect(square.locator('td').nth(2)).to_have_text('—')
                result_row=page.get_by_role('rowheader',name='Результат просмотра',exact=True).locator('..')
                await expect(result_row.locator('td').first).to_have_text('Понравилось')
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
                await expect(page.locator('#rejected-count')).to_have_text('1')
                assert not await page.locator('#rejected-section').evaluate('el=>el.open')
                await row.locator('.visit-history > summary').click()
                await expect(row.locator('.visit-rejection-reasons').first).to_contain_text('Шум, Документы')
                persisted=next(v for v in state()['9100']['visits'] if v['id']==past['id'])
                assert persisted['preparation']=='До поездки: спросить про документы'
                assert persisted['comment']=='<b>После поездки</b>: шумно'
                assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                assert errors==[]
                await context.close();await browser.close()
        asyncio.run(scenario())


@pytest.mark.parametrize('browser_zone',['America/Los_Angeles','Asia/Tokyo'])
def test_confirmed_calendar_date_drafts_permissions_mobile_and_timezone(tmp_path,monkeypatch,browser_zone):
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
                if request.method not in ('GET','HEAD'):calls.append((request.method,path,json.loads(request.post_data or '{}')))
                headers={k:v for k,v in request.headers.items() if k.lower() in ('content-type','origin','sec-fetch-site','x-csrf-token')}
                response=client.request(request.method,path,headers=headers,content=request.post_data_buffer,follow_redirects=False)
                forwarded={k:v for k,v in response.headers.items() if k.lower() not in ('set-cookie','content-length','content-encoding','transfer-encoding')}
                await route.fulfill(status=response.status_code,headers=forwarded,body=response.content)
            async with async_playwright() as playwright:
                browser=await playwright.chromium.launch(headless=True)
                context=await browser.new_context(viewport={'width':390,'height':844},locale='en-US',timezone_id=browser_zone)
                await context.route('**/*',bridge)
                page=await context.new_page();errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                await page.clock.set_fixed_time('2026-10-26T09:00:00Z')
                await page.goto('https://xata.test/')
                row=page.locator('tr[data-listing-id="9300"]')
                date_cell=row.locator('td').nth(12);result_cell=row.locator('td').nth(13)
                field=date_cell.locator('.visit-date-input');picker=date_cell.locator('.visit-date-picker')
                clear=date_cell.get_by_role('button',name='Очистить',exact=True)
                ok=date_cell.get_by_role('button',name='ОК',exact=True)
                await expect(field).to_be_visible()
                assert await field.get_attribute('type')=='text'
                assert await field.get_attribute('placeholder')=='ДД.ММ.ГГГГ'
                assert await picker.get_attribute('type')=='button'
                await expect(result_cell.locator('button')).to_have_count(0)
                # Neither typing, calendar selection, blur nor polls may save.
                await field.fill('26.10.2026')
                await field.dispatch_event('change')
                await field.blur()
                await page.evaluate('refresh(true)')
                await expect(field).to_have_value('26.10.2026')
                assert calls==[] and current()['visits']==[]
                await picker.click()
                await expect(page.locator('#visit-calendar-popup')).to_be_visible()
                await page.locator('#visit-calendar-popup [data-calendar-date="2026-10-27"]').click()
                await expect(page.locator('#visit-calendar-popup')).to_be_hidden()
                await expect(field).to_have_value('27.10.2026')
                await page.evaluate('refresh(true)')
                assert calls==[] and current()['visits']==[]
                await clear.click()
                await expect(field).to_have_value('')
                assert calls==[] and current()['visits']==[]
                await field.fill('26.10.2026')
                # Repeated confirmation remains one operation.
                await ok.evaluate('button=>{button.click();button.click();}')
                await expect(date_cell.locator('.visit-date-status')).to_have_text('Дата сохранена.')
                assert len(calls)==1 and calls[0][1].endswith('/visits/date')
                assert calls[0][2]['date']=='2026-10-26'
                assert not await page.locator('#visit-dialog').is_visible()
                state=current();first=state['visits'][0]
                assert len(state['visits'])==1 and first['scheduled_at']=='2026-10-25T21:00:00+00:00'
                assert state['review_status']=='new'
                await expect(field).to_have_value('26.10.2026')
                await expect(date_cell.locator('.visit-overdue')).to_be_hidden()
                await ok.click()
                assert len(calls)==1
                await page.locator('[data-review-filter="planned"]').click()
                await expect(page.locator('#visit-agenda .visit-overdue')).to_have_count(0)
                await page.locator('[data-review-filter="all"]').click()
                await page.reload()
                await expect(field).to_have_value('26.10.2026')
                # Clearing is also just a durable draft until OK.
                await field.fill('27.10.2026')
                await field.blur()
                await page.evaluate('refresh(true)')
                await expect(field).to_have_value('27.10.2026')
                assert len(calls)==1
                await clear.click()
                await field.blur()
                await page.evaluate('refresh(true)')
                await expect(field).to_have_value('')
                assert current()['visits'][0]['status']=='planned' and len(calls)==1
                await ok.click()
                await expect(date_cell.locator('.visit-date-status')).to_have_text('Дата сохранена.')
                assert calls[-1][2]['date'] is None
                assert current()['visits'][0]['status']=='cancelled'
                assert current()['visits'][0]['scheduled_at']==first['scheduled_at']
                assert len(current()['visits'])==1
                # Invalid dates never reach the server.
                count=len(calls)
                await field.fill('31.02.2026')
                await ok.click()
                await expect(date_cell.locator('.visit-date-status')).to_contain_text('корректную дату')
                assert len(calls)==count
                await page.evaluate('refresh(true)')
                await expect(field).to_have_value('31.02.2026')
                await field.fill('26.10.2026')
                await ok.click()
                await expect(date_cell.locator('.visit-date-status')).to_have_text('Дата сохранена.')
                state=current();visit=next(v for v in state['visits'] if v['status']=='planned')
                assert len(state['visits'])==2  # cancellation history plus one repeat
                await field.fill('27.10.2026')
                external=client.patch('/api/listings/9300/visits/date',json={
                    'property_id':state['property_id'],'visits_version':state['visits_version'],
                    'visit_id':visit['id'],'date':'2026-10-28T20:00:00+03:00'})
                assert external.status_code==200
                await page.evaluate('refresh(true)')
                await expect(field).to_have_value('27.10.2026')
                await ok.click()
                await expect(date_cell.locator('.visit-date-status')).to_contain_text('уже изменены')
                assert next(v for v in current()['visits'] if v['id']==visit['id'])['scheduled_at']=='2026-10-28T17:00:00+00:00'
                await expect(field).to_have_value('27.10.2026')
                await page.reload()
                await expect(field).to_have_value('28.10.2026')
                await field.fill('26.10.2026')
                await ok.click()
                await expect(date_cell.locator('.visit-date-status')).to_have_text('Дата сохранена.')
                assert next(v for v in current()['visits'] if v['id']==visit['id'])['scheduled_at']=='2026-10-26T17:00:00+00:00'
                await expect(date_cell.locator('.visit-overdue')).to_be_hidden()
                # Both desktop and phone keep text, calendar and buttons inside
                # the date column. Phone buttons remain proper touch targets.
                for width in (1440,390):
                    await page.set_viewport_size({'width':width,'height':844})
                    await field.scroll_into_view_if_needed()
                    assert await date_cell.evaluate("""cell=>{
                        const box=cell.getBoundingClientRect();
                        return [...cell.querySelectorAll('.visit-date-editor,.visit-date-control,button,input')]
                          .every(n=>{const r=n.getBoundingClientRect();return r.left>=box.left&&r.right<=box.right+1;});
                    }""")
                    fit=await field.evaluate("""input=>{
                        const s=getComputedStyle(input),ctx=document.createElement('canvas').getContext('2d');
                        ctx.font=s.fontWeight+' '+s.fontSize+' '+s.fontFamily;
                        return {text:ctx.measureText(input.value).width,available:input.clientWidth-parseFloat(s.paddingLeft)-parseFloat(s.paddingRight),
                          width:input.clientWidth,font:s.fontSize,paddingLeft:s.paddingLeft,paddingRight:s.paddingRight};
                    }""")
                    assert fit['text']<=fit['available']+1,fit
                    for action in (clear,ok):
                        action_fit=await action.evaluate("""button=>{
                            const s=getComputedStyle(button),ctx=document.createElement('canvas').getContext('2d');
                            ctx.font=s.fontWeight+' '+s.fontSize+' '+s.fontFamily;
                            return {label:button.textContent,text:ctx.measureText(button.textContent).width,
                              available:button.clientWidth-parseFloat(s.paddingLeft)-parseFloat(s.paddingRight),font:s.fontSize,width:button.clientWidth};
                        }""")
                        assert action_fit['text']<=action_fit['available']+1,action_fit
                    assert (await field.bounding_box())['width']<140
                    if width==390:
                        for button in (clear,ok):
                            box=await button.bounding_box()
                            assert box['height']>=44 and box['width']>=44
                    assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                second=page.locator('tr[data-listing-id="9301"]')
                await second.locator('.visit-date-input').fill('25.10.2026')
                await second.get_by_role('button',name='ОК',exact=True).click()
                await expect(second.locator('.visit-date-status')).to_have_text('Дата сохранена.')
                await expect(second.locator('td').nth(12).locator('.visit-overdue')).to_be_visible()
                header=page.locator('#listing-table [data-sort="visit_date"]')
                await header.click()
                assert await page.locator('#listings tr').evaluate_all('rows=>rows.map(r=>r.dataset.listingId)')==['9301','9300']
                await header.click()
                assert await page.locator('#listings tr').evaluate_all('rows=>rows.map(r=>r.dataset.listingId)')==['9300','9301']
                # Buyers have a DD.MM.YYYY value but no inline editor/actions.
                sign_in(client,'buyer','password-123')
                await page.reload()
                await expect(date_cell.locator('input,button')).to_have_count(0)
                await expect(date_cell.locator('time')).to_have_text('26.10.2026')
                assert await date_cell.locator('time').get_attribute('datetime')=='2026-10-26'
                await expect(result_cell.locator('button')).to_have_count(1)
                state=current()
                assert client.patch('/api/listings/9300/visits/date',json={
                    'property_id':state['property_id'],'visits_version':state['visits_version'],
                    'visit_id':visit['id'],'date':'2026-10-27'}).status_code==403
                # The result form and its timestamp semantics are unchanged.
                await result_cell.get_by_role('button',name='Добавить результат',exact=True).click()
                assert await page.locator('#visit-date').get_attribute('type')=='datetime-local'
                await expect(page.locator('#visit-record-choice')).to_have_value(str(visit['id']))
                await page.locator('[data-visit-outcome="rejected"]').click()
                await page.locator('#visit-date').fill('2026-10-26T20:15')
                await page.locator('#visit-preparation').fill('Спросить про документы')
                await page.locator('#visit-comment').fill('Шумно после просмотра')
                await page.locator('#visit-reasons input[value="noise"]').check()
                await page.locator('#visit-save').click()
                await expect(page.locator('#visit-dialog')).to_be_hidden()
                assert len(current()['visits'])==2 and current()['review_status']=='rejected'
                completed=next(v for v in current()['visits'] if v['id']==visit['id'])
                assert completed['completed_at']=='2026-10-26T17:15:00+00:00'
                await page.locator('#viewed-rejected-section > summary').click()
                await result_cell.get_by_role('button',name='Добавить результат',exact=True).click()
                await expect(page.locator('#visit-comment')).to_have_value('Шумно после просмотра')
                await expect(page.locator('#visit-reasons input[value="noise"]')).to_be_checked()
                await page.locator('[data-visit-outcome="liked"]').click()
                await page.locator('#visit-save').click()
                await expect(page.locator('#visit-dialog')).to_be_hidden()
                assert len(current()['visits'])==2 and current()['review_status']=='liked'
                sign_in(client)
                await page.reload()
                await expect(field).to_have_value('26.10.2026')
                before=current();count=len(calls)
                await field.fill('27.10.2026')
                await field.blur()
                await page.evaluate('refresh(true)')
                assert len(calls)==count
                await expect(field).to_have_value('27.10.2026')
                await ok.click()
                await expect(date_cell.locator('.visit-date-status')).to_have_text('Дата сохранена.')
                after=current();updated=next(v for v in after['visits'] if v['id']==visit['id'])
                assert updated['completed_at']=='2026-10-27T17:15:00+00:00'
                assert after['review_status']==before['review_status']=='liked'
                for key in ('outcome','preparation','comment','rejection_reasons'):
                    assert updated[key]==next(v for v in before['visits'] if v['id']==visit['id'])[key]
                # A historical actual date cannot be erased by date-only editing.
                await clear.click()
                await ok.click()
                await expect(date_cell.locator('.visit-date-status')).to_contain_text('Нельзя удалить')
                assert next(v for v in current()['visits'] if v['id']==visit['id'])['completed_at']==updated['completed_at']
                await page.reload()
                await expect(field).to_have_value('27.10.2026')
                await expect(result_cell.locator('button')).to_have_count(1)
                assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                assert errors==[]
                await context.close();await browser.close()
        asyncio.run(scenario())


@pytest.mark.parametrize('phone',[False,True])
def test_calendar_icon_opens_real_day_grid_on_desktop_and_touch_phone(tmp_path,monkeypatch,phone):
    monkeypatch.setenv('DATA_DIR',str(tmp_path));monkeypatch.setenv('APP_USER','owner')
    monkeypatch.setenv('APP_PASSWORD','long-test-password');monkeypatch.setenv('BROWSER_SCHEDULE_ENABLED','false')
    monkeypatch.delenv('PUBLIC_URL',raising=False)
    from app import main
    importlib.reload(main)
    with main.database() as conn:
        main.save_items(conn,[{'id':'9500','url':'https://cian.ru/sale/flat/9500/',
                              'title':'Квартира','address':'Москва, Тестовая улица, д. 95'}],'2026-10-26T10:00:00+00:00')
    with TestClient(main.app,base_url='https://xata.test') as client:
        sign_in(client)
        assert client.post('/api/users',json={'username':'expert','display_name':'Риелтор','password':'password-123','role':'realtor'}).status_code==201
        sign_in(client,'expert','password-123')
        async def scenario():
            from playwright.async_api import async_playwright,expect
            writes=[]
            async def bridge(route):
                request=route.request;parsed=urlsplit(request.url)
                assert parsed.hostname=='xata.test'
                if request.method not in ('GET','HEAD'):writes.append((request.method,parsed.path))
                headers={k:v for k,v in request.headers.items() if k.lower() in ('content-type','origin','sec-fetch-site','x-csrf-token')}
                response=client.request(request.method,parsed.path,headers=headers,content=request.post_data_buffer,follow_redirects=False)
                forwarded={k:v for k,v in response.headers.items() if k.lower() not in ('set-cookie','content-length','content-encoding','transfer-encoding')}
                await route.fulfill(status=response.status_code,headers=forwarded,body=response.content)
            async with async_playwright() as playwright:
                browser=await playwright.chromium.launch(headless=True)
                context=await browser.new_context(viewport={'width':390 if phone else 1440,'height':844},
                    locale='en-US',timezone_id='America/Los_Angeles',is_mobile=phone,has_touch=phone)
                await context.route('**/*',bridge)
                page=await context.new_page();errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                await page.clock.set_fixed_time('2026-10-26T09:00:00Z')
                await page.goto('https://xata.test/')
                cell=page.locator('tr[data-listing-id="9500"] td').nth(12)
                field=cell.locator('.visit-date-input')
                icon=cell.get_by_role('button',name='Выбрать дату просмотра квартиры 9500',exact=True)
                popup=page.locator('#visit-calendar-popup')
                async def press(locator):
                    if phone:await locator.tap()
                    else:await locator.click()
                await expect(popup).to_be_hidden()
                await expect(icon.locator('svg')).to_be_visible()
                await field.fill('26.10.2026')
                await press(icon)
                await expect(popup).to_be_visible()
                await expect(icon).to_have_attribute('aria-expanded','true')
                await expect(popup.locator('.visit-calendar-head strong')).to_contain_text('октябрь')
                await page.evaluate('refresh(true)')
                await expect(popup).to_be_visible()
                await expect(field).to_have_value('26.10.2026')
                await press(popup.get_by_role('button',name='Следующий месяц',exact=True))
                await expect(popup.locator('.visit-calendar-head strong')).to_contain_text('ноябрь')
                await expect(popup.locator('[data-calendar-date="2026-11-01"]')).to_be_visible()
                bounds=await popup.bounding_box()
                viewport=page.viewport_size
                assert bounds['x']>=0 and bounds['x']+bounds['width']<=viewport['width']
                assert bounds['y']>=0 and bounds['y']+bounds['height']<=viewport['height']
                target=popup.get_by_role('button',name='01.11.2026',exact=True)
                if phone:
                    button_bounds=await target.bounding_box()
                    assert button_bounds['height']>=44 and button_bounds['width']>=40
                await press(target)
                await expect(popup).to_be_hidden()
                await expect(icon).to_have_attribute('aria-expanded','false')
                await expect(field).to_have_value('01.11.2026')
                await field.blur()
                await page.evaluate('refresh(true)')
                assert writes==[]
                assert client.get('/api/state').json()['listings'][0]['visits']==[]
                await expect(field).to_have_value('01.11.2026')
                # Reopening retains the chosen draft month/day, and Escape
                # closes only the calendar without touching the result form.
                await press(icon)
                await expect(popup).to_be_visible()
                await expect(popup.locator('[data-calendar-date="2026-11-01"]')).to_have_attribute('aria-pressed','true')
                await page.keyboard.press('Escape')
                await expect(popup).to_be_hidden()
                await expect(field).to_have_value('01.11.2026')
                assert not await page.locator('#visit-dialog').is_visible()
                assert writes==[]
                await press(cell.get_by_role('button',name='ОК',exact=True))
                await expect(cell.locator('.visit-date-status')).to_have_text('Дата сохранена.')
                assert len(writes)==1 and writes[0][1].endswith('/visits/date')
                item=client.get('/api/state').json()['listings'][0]
                assert len(item['visits'])==1 and item['review_status']=='new'
                assert item['visits'][0]['scheduled_at']=='2026-10-31T21:00:00+00:00'
                await page.reload()
                await expect(field).to_have_value('01.11.2026')
                await expect(popup).to_be_hidden()
                assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                assert errors==[]
                await context.close();await browser.close()
        asyncio.run(scenario())


@pytest.mark.parametrize('phone',[False,True])
def test_viewed_groups_controls_colors_legacy_refresh_and_mobile(tmp_path,monkeypatch,phone):
    monkeypatch.setenv('DATA_DIR',str(tmp_path));monkeypatch.setenv('APP_USER','owner')
    monkeypatch.setenv('APP_PASSWORD','long-test-password');monkeypatch.setenv('BROWSER_SCHEDULE_ENABLED','false')
    monkeypatch.delenv('PUBLIC_URL',raising=False)
    from app import main
    importlib.reload(main)
    monkeypatch.setattr(main.CianBrowserSource,'status',lambda self:{'status':'authorized','message':'Авторизован','manual_access':False})
    with main.database() as conn:
        main.save_items(conn,[{'id':str(key),'url':'https://cian.ru/sale/flat/'+str(key)+'/',
            'title':'Квартира','address':'Москва, очень длинный адрес квартиры '+str(key),'price':12000000,'area':40}
            for key in range(9500,9507)],'2026-10-26T10:00:00+00:00')
    with TestClient(main.app,base_url='https://xata.test') as client:
        sign_in(client)
        def current(key):
            return next(i for i in client.get('/api/state').json()['listings'] if i['id']==key)
        def complete(key,outcome,day):
            item=current(key)
            response=client.post('/api/listings/'+key+'/visits',json={'property_id':item['property_id'],
                'status':'completed','outcome':outcome,'completed_at':day+'T10:00:00Z','comment':'Общий итог'})
            assert response.status_code==200,response.text
            return response.json()
        assert client.post('/api/listings/9500/review',json={'status':'liked'}).status_code==200
        complete('9501','liked','2026-10-21')
        item=complete('9502','liked','2026-10-20')
        assert client.patch('/api/listings/9502/viewing-classification',json={'property_id':item['property_id'],
            'visits_version':item['visits_version'],'documents_ready':True}).status_code==200
        complete('9503','liked','2026-10-19')
        item=complete('9503','rejected','2026-10-24')
        negative=item['visits'][0]
        assert client.post('/api/listings/9503/visits',json={'property_id':item['property_id'],
            'status':'planned','scheduled_at':'2030-01-01T10:00:00Z','preparation':'Спросить ещё раз'}).status_code==200
        complete('9505','thinking','2026-10-24')
        complete('9506','liked','2026-10-23')
        for key in ('9501','9504'):
            assert client.post('/api/listings/'+key+'/realtor-review',json={'status':'rejected','comment':'Проверить документы'}).status_code==200
        for username,role in [('buyer','reviewer'),('expert','realtor')]:
            assert client.post('/api/users',json={'username':username,'display_name':username,'password':'password-123','role':role}).status_code==201
        sign_in(client,'buyer','password-123')
        async def scenario():
            from playwright.async_api import async_playwright,expect
            stale=None;inject=False
            async def bridge(route):
                nonlocal inject
                request=route.request;parsed=urlsplit(request.url)
                assert parsed.hostname=='xata.test'
                path=parsed.path+('?' + parsed.query if parsed.query else '')
                if path=='/api/state' and inject:
                    inject=False
                    await route.fulfill(status=200,content_type='application/json',body=json.dumps(stale))
                    return
                headers={k:v for k,v in request.headers.items() if k.lower() in ('content-type','origin','sec-fetch-site','x-csrf-token')}
                response=client.request(request.method,path,headers=headers,content=request.post_data_buffer,follow_redirects=False)
                forwarded={k:v for k,v in response.headers.items() if k.lower() not in ('set-cookie','content-length','content-encoding','transfer-encoding')}
                await route.fulfill(status=response.status_code,headers=forwarded,body=response.content)
            async with async_playwright() as p:
                browser=await p.chromium.launch(headless=True)
                context=await browser.new_context(viewport={'width':390 if phone else 1440,'height':900},has_touch=phone,is_mobile=phone)
                await context.route('**/*',bridge)
                page=await context.new_page();errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                await page.goto('https://xata.test/')
                await expect(page.locator('#listings tr')).to_have_count(2)
                await expect(page.locator('#rejected-listings tr')).to_have_count(1)
                await expect(page.locator('#viewed-count')).to_have_text('4')
                for group,count,opened in [('liked','2',True),('documents','1',True),('rejected','1',False)]:
                    await expect(page.locator('#viewed-'+group+'-count')).to_have_text(count)
                    assert await page.locator('#viewed-'+group+'-section').evaluate('el=>el.open')==opened
                await expect(page.locator('#viewed-legacy-warning')).to_contain_text('Думаем')
                await expect(page.locator('#listings tr[data-listing-id="9505"] .viewed-group-label')).to_have_text('Думаем — группа не выбрана')
                assert current('9505')['visits'][0]['outcome']=='thinking'
                assert await page.locator('tr[data-property-id]').count()==7
                assert len(set(await page.locator('tr[data-property-id]').evaluate_all('rows=>rows.map(r=>r.dataset.propertyId)')))==7
                row=page.locator('tr[data-listing-id="9501"]')
                await expect(row.locator('.realtor-label')).to_have_text('Отбраковано риелтором')
                await expect(row.locator('.realtor-editor')).to_be_hidden()
                await expect(row.locator('.viewed-group-label')).to_have_text('Понравилось — пока без проверки документов')
                assert await row.locator('td').first.evaluate('el=>getComputedStyle(el).backgroundColor')=='rgb(233, 243, 252)'
                assert '73, 134, 181' in await row.locator('td').first.evaluate('el=>getComputedStyle(el).boxShadow')
                stale=client.get('/api/state').json()
                before=current('9501')
                await row.locator('.viewed-stage-toggle').click()
                await expect(page.locator('#viewed-documents-listings tr[data-listing-id="9501"]')).to_have_count(1)
                after=current('9501')
                assert after['visits']==before['visits']
                assert after['review_status']==before['review_status']
                assert after['realtor_status']==before['realtor_status']=='rejected'
                assert await row.locator('td').first.evaluate('el=>getComputedStyle(el).backgroundColor')=='rgb(233, 245, 231)'
                await expect(row.locator('.realtor-label')).to_have_text('Отбраковано риелтором')
                inject=True
                await page.evaluate('refresh(true)')
                await expect(page.locator('#viewed-documents-listings tr[data-listing-id="9501"]')).to_have_count(1)
                await row.locator('.viewed-stage-toggle').click()
                await expect(page.locator('#viewed-liked-listings tr[data-listing-id="9501"]')).to_have_count(1)
                await row.locator('.viewed-stage-toggle').click()
                await expect(page.locator('#viewed-documents-listings tr[data-listing-id="9501"]')).to_have_count(1)
                await row.locator('td').last.get_by_role('button',name='Добавить результат',exact=True).click()
                await page.locator('#visit-comment').fill('<b>Уточнение</b> после поездки')
                await page.evaluate('refresh(true)')
                await expect(page.locator('#visit-comment')).to_have_value('<b>Уточнение</b> после поездки')
                await page.locator('#visit-save').click()
                await expect(page.locator('#visit-dialog')).to_be_hidden()
                assert current('9501')['viewed_group']=='documents'
                await page.locator('[data-review-filter="planned"]').click()
                await expect(page.locator('#visit-agenda .visit-card')).to_have_count(1)
                await expect(page.locator('#visit-agenda')).to_contain_text('Спросить ещё раз')
                await page.locator('[data-review-filter="all"]').click()
                # Groups stay independent from main filters; their dates still sort.
                await page.locator('[data-review-filter="new"]').click()
                await expect(page.locator('tr[data-listing-id="9506"]')).to_be_visible()
                await page.locator('#viewed-liked-section [data-sort="visit_date"]').click()
                assert await page.locator('#viewed-liked-listings tr').evaluate_all('rows=>rows.map(r=>r.dataset.listingId)')==['9506']
                await page.locator('#viewed-rejected-section > summary').click()
                negative_row=page.locator('tr[data-listing-id="9503"]')
                await expect(negative_row.locator('.viewed-group-label')).to_have_text('Не понравилось')
                assert await negative_row.locator('td').first.evaluate('el=>getComputedStyle(el).backgroundColor')=='rgb(251, 236, 235)'
                await negative_row.locator('td').last.get_by_role('button',name='Добавить результат',exact=True).click()
                await page.locator('#visit-record-choice').select_option(str(negative['id']))
                await page.locator('[data-visit-outcome="liked"]').click()
                await page.locator('#visit-save').click()
                await expect(page.locator('#visit-dialog')).to_be_hidden()
                await expect(page.locator('#viewed-liked-listings tr[data-listing-id="9503"]')).to_have_count(1)
                assert current('9503')['viewed_group']=='liked' and len(current('9503')['visits'])==3
                await page.reload()
                await expect(page.locator('#viewed-documents-listings tr[data-listing-id="9501"]')).to_have_count(1)
                assert not await page.locator('#viewed-rejected-section').evaluate('el=>el.open')
                assert not await page.locator('#rejected-section').evaluate('el=>el.open')
                for group in ('liked','documents'):
                    assert await page.locator('#viewed-'+group+'-section').evaluate('el=>el.open')
                assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                assert await page.locator('#viewed-documents-section .viewed-wrap').evaluate('el=>el.scrollWidth>el.clientWidth')
                if phone:
                    box=await row.locator('.viewed-stage-toggle').bounding_box()
                    assert box['height']>=44
                sign_in(client,'expert','password-123')
                await page.reload()
                await expect(row.locator('.viewed-stage-toggle')).to_be_hidden()
                await expect(row.locator('.visit-date-input')).to_be_visible()
                assert errors==[]
                await context.close();await browser.close()
        asyncio.run(scenario())
