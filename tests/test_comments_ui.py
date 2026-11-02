"""Offline real-browser acceptance: shared selection, editors, cards and screenshots."""
import asyncio
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from tests.helpers import sign_in
from tests.test_ui import two_page_app

pytestmark=pytest.mark.skipif(os.getenv('RUN_BROWSER_TESTS')!='1',reason='real browsers opt in')


@pytest.mark.parametrize('engine',['chromium','webkit'])
@pytest.mark.parametrize('width',[360,390,430,768,1440,1920])
def test_shared_comments_mobile_cards_and_no_duplicate_forms(two_page_app,engine,width):
    main,client,current=two_page_app
    async def scenario():
        from playwright.async_api import async_playwright,expect
        failures=[];writes=[];fail_once=False
        async def bridge(route):
            nonlocal fail_once
            req=route.request;url=urlsplit(req.url)
            assert url.hostname=='xata.test','No external APIs or services permitted'
            if req.method in ('POST','PATCH','DELETE'):writes.append((req.method,url.path))
            if fail_once and url.path.endswith('/family-comment') and req.method=='PATCH':
                fail_once=False
                await route.fulfill(status=503,content_type='application/json',body=json.dumps({'detail':'Тестовая временная ошибка'}));return
            headers={k:v for k,v in req.headers.items() if k.lower() in ('content-type','origin','sec-fetch-site','x-csrf-token')}
            result=client.request(req.method,url.path+('?' + url.query if url.query else ''),
                                  headers=headers,content=req.post_data_buffer,follow_redirects=False)
            await route.fulfill(status=result.status_code,headers={k:v for k,v in result.headers.items()
                if k.lower() not in ('set-cookie','content-length','content-encoding','transfer-encoding')},body=result.content)
        async with async_playwright() as p:
            browser=await getattr(p,engine).launch(headless=True)
            context=await browser.new_context(viewport={'width':width,'height':900},has_touch=width<900,
                is_mobile=width<900,device_scale_factor=1,timezone_id='America/Los_Angeles')
            await context.route('**/*',bridge)
            page=await context.new_page();page.set_default_timeout(8000);page.on('pageerror',lambda e:failures.append(str(e)))
            async def fits():
                assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth'),await page.evaluate('({width:innerWidth,scroll:document.documentElement.scrollWidth})')
                if page.viewport_size['width']<=900:
                    assert await page.locator('#mobile-sort').is_visible()
                    assert await page.locator('table:has(tbody tr[data-property-id])').first.evaluate('e=>getComputedStyle(e).display')=='block'
            async def shot(name,full=True):
                folder=os.getenv('UI_SCREENSHOT_DIR')
                if folder:
                    target=Path(folder);target.mkdir(parents=True,exist_ok=True)
                    await page.screenshot(path=str(target/(engine+'-'+str(width)+'-'+name+'.png')),full_page=full)
            try:
                await page.goto('https://xata.test/')
                await expect(page.locator('#nav-all-count')).to_have_text('9')
                await expect(page.locator('#nav-ours-count')).to_have_text('7')
                await expect(page.locator('#count')).to_have_text('9')
                await expect(page.locator('[data-review-filter="all"] span')).to_have_text('9')
                row=page.locator('tr[data-listing-id="9500"]')
                await expect(row.locator('.membership-label')).to_be_visible()
                assert await row.locator('td').first.evaluate('e=>getComputedStyle(e).backgroundColor')=='rgb(233, 243, 252)'
                assert await page.locator('#listing-table th').all_text_contents()==[
                    'Риелтор','Фото','Номер ЦИАН / ссылка','Объект / адрес','Цена','Ипотека / мес.','Площадь','Этаж','Метро','Добавлено','Семейная оценка']
                await fits();await shot('all')
                await page.locator('#rejected-section>summary').click()
                rejected=page.locator('tr[data-listing-id="9501"]')
                await expect(rejected.locator('.membership-label')).to_be_visible()
                assert await rejected.locator('td').first.evaluate('e=>getComputedStyle(e).backgroundColor')=='rgb(250, 238, 235)'
                await page.locator('#rejected-section>summary').click()
                await page.locator('#listing-search').fill('9500')
                await expect(page.locator('#found-count')).to_have_text('Найдено: 1 из 9')
                await expect(page.locator('#nav-all-count')).to_have_text('9')
                await expect(page.locator('#nav-ours-count')).to_have_text('7')
                await page.locator('#listing-search').fill('')
                if width<=900:await page.locator('#mobile-sort').select_option('price:desc')
                await page.locator('.page-nav a[href="/ours"]').click()
                await expect(page.locator('#count')).to_have_text('7')
                row=page.locator('tr[data-listing-id="9500"]')
                assert await page.locator('tr[data-listing-id="9500"]').count()==1
                assert await page.locator('.viewed-table').first.locator('th').all_text_contents()==[
                    'Риелтор / юрист','Фото','Квартира','Цена','Дата просмотра','Результат просмотра','Наш комментарий','Этап отбора']
                if width>900:
                    assert (await row.locator('.viewing-result-cell button').first.bounding_box())['width']>=140
                    assert (await row.locator('.selection-stage-cell').bounding_box())['width']>=210
                assert await row.locator('.family-comment').count()==1
                await expect(row.locator('.family-comment')).to_be_hidden()
                await row.locator('.family-edit').click()
                field=row.locator('.family-comment')
                draft='<b>Наш выбор</b> — '+('длинный комментарий ' * 18).rstrip()+'\nСомнения по документам.'
                await field.fill(draft)
                draft=await field.input_value()  # Compare refresh with the browser's actual input.
                await field.evaluate('e=>{e.focus();e.setSelectionRange(7,7)}')
                before=len(writes);await page.evaluate('refresh(true)')
                assert await field.evaluate('e=>e===document.activeElement&&e.selectionStart===7')
                await expect(field).to_have_value(draft)
                await page.set_viewport_size({'width':900,'height':width})
                await expect(field).to_have_value(draft)
                await fits()
                await page.set_viewport_size({'width':width,'height':900})
                await fits()
                assert len(writes)==before
                fail_once=True
                await row.locator('.family-editor .comment-save').click()
                await expect(row.locator('.family-editor .comment-error')).to_contain_text('временная ошибка')
                await expect(field).to_have_value(draft)
                item=current('9500')
                assert client.patch('/api/listings/9500/family-comment',json={'property_id':item['property_id'],
                    'family_comment_version':item['family_comment_version'],'text':'Чужое актуальное изменение'}).status_code==200
                await page.evaluate('refresh(true)')
                await row.locator('.family-editor .comment-save').click()
                await expect(row.locator('.family-editor .comment-error')).to_contain_text('уже изменён')
                await expect(row.locator('.family-editor .comment-remote')).to_contain_text('Чужое актуальное изменение')
                await expect(field).to_have_value(draft)
                await row.locator('.family-editor .comment-cancel').click()
                assert current('9500')['family_comment']=='Чужое актуальное изменение'
                await row.locator('.family-edit').click()
                await field.fill(draft)
                await row.locator('.family-editor .comment-save').click()
                await expect(row.locator('.family-editor')).to_be_hidden()
                assert current('9500')['family_comment']==draft
                assert await row.locator('.family-comment-cell b').count()==0
                await expect(row.locator('.family-comment-text')).to_have_text(draft)
                await expect(row.locator('.family-meta')).to_contain_text('buyer')
                assert await row.locator('.family-comment-cell .comment-body:visible').count()==1
                await row.locator('.family-comment-cell .comment-more').click()
                await expect(row.locator('.family-comment-cell .comment-more')).to_have_text('Свернуть')
                await row.locator('.family-edit').click();await field.fill('')
                await row.locator('.family-editor .comment-save').click()
                await expect(row.locator('.family-editor')).to_be_hidden()
                assert current('9500')['family_comment']==''
                assert current('9500')['comment_history'][0]['text']==''
                await row.locator('.family-edit').click();await field.fill(draft)
                await row.locator('.family-editor .comment-save').click()
                await expect(row.locator('.family-editor')).to_be_hidden()
                await row.locator('.selection-toggle').click()
                await expect(row).to_have_count(0)
                await page.goto('https://xata.test/')
                row=page.locator('tr[data-listing-id="9500"]')
                await expect(row).to_be_visible()
                await expect(row.locator('.membership-label')).to_be_hidden()
                await row.locator('.characteristics>summary').click()
                await row.locator('.family-details>summary').click()
                await expect(row.locator('.family-comment-text')).to_have_text(draft)
                await expect(row.locator('.family-edit')).to_be_hidden()
                await expect(row.locator('.characteristics')).to_contain_text('8 мин. пешком')
                await row.locator('.selection-toggle').click()
                await expect(row.locator('.membership-label')).to_be_visible()
                await expect(page.locator('#nav-all-count')).to_have_text('9')
                await page.goto('https://xata.test/ours')
                row=page.locator('tr[data-listing-id="9500"]')
                await expect(row.locator('.family-comment-text')).to_have_text(draft)
                await fits();await shot('ours')
                await row.locator('.family-edit').click()
                await field.fill('Черновик комментария — не сохраняется автоматически')
                await field.scroll_into_view_if_needed()
                await shot('comment-editor',False)
                if width<900:
                    assert (await row.locator('.family-editor .comment-save').bounding_box())['height']>=44
                    assert (await row.locator('.family-editor .comment-cancel').bounding_box())['height']>=44
                await row.locator('.family-editor .comment-cancel').click()
                assert current('9500')['family_comment']==draft
                snapshot=client.get('/api/state').json()['listings']
                await row.locator('.compare-select').check()
                await page.locator('tr[data-listing-id="9502"] .compare-select').check()
                await page.locator('#compare-open').click()
                await expect(page.locator('#compare-dialog')).to_be_visible()
                assert await page.locator('#compare-table thead th').count()==3
                await fits()
                await page.locator('#compare-close').click()
                assert client.get('/api/state').json()['listings']==snapshot
                sign_in(client,'expert','password-123');await page.reload()
                await expect(row.locator('.family-edit')).to_be_hidden()
                await expect(row.locator('.family-comment-text')).to_have_text(draft)
                await row.locator('.realtor-edit').click()
                await row.locator('.realtor-select').select_option('approved')
                await row.locator('.realtor-comment').fill('Проверено риелтором. Текст один раз.')
                await row.locator('.realtor-editor .comment-save').click()
                await expect(row.locator('.realtor-editor')).to_be_hidden()
                assert await row.locator('.realtor-block .comment-body:visible').count()==1
                await expect(row.locator('.comment-preview')).to_have_text('Проверено риелтором. Текст один раз.')
                await row.locator('.visit-date-picker').click()
                await expect(page.locator('.visit-calendar:visible')).to_have_count(1)
                await fits()
                assert failures==[]
            except BaseException:
                await shot('failure',False)
                raise
            finally:
                if failures:await shot('page-errors',False)
                await context.close();await browser.close()
    asyncio.run(scenario())
