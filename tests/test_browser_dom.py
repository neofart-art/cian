"""Offline Chromium tests: no requests reach Cian or any external server."""
import asyncio
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.getenv('RUN_BROWSER_TESTS') != '1', reason='Chromium integration tests opt in')

URL = 'https://www.cian.ru/cat.php?room1=1'
HTML = """<!doctype html><html><body>
<span data-name="UserAvatar">Профиль</span>
<article data-name="CardComponent">
<a href="https://www.cian.ru/sale/flat/123/"><span data-mark="OfferTitle">1-комн. квартира, 34,7 м²</span></a>
<span data-mark="MainPrice">11 950 000 ₽</span>
<div>4/9 этаж</div><div>ЖК «Тест»</div><div>Москва, улица Тестовая, 1</div>
<div>м. Сокол</div><div>15 мин. пешком</div>
<div style="display:none">СКРЫТЫЙ ТЕКСТ</div>
</article>
<article data-name="CardComponent" style="display:none"><a href="https://cian.ru/sale/flat/999/">Hidden offer</a></article>
</body></html>"""


def test_dom_extraction_challenge_and_persistent_profile(tmp_path):
    async def scenario():
        from playwright.async_api import async_playwright
        from collector.service import Collector
        async with async_playwright() as playwright:
            profile = str(tmp_path / 'profile')
            context = await playwright.chromium.launch_persistent_context(profile, headless=True)
            await context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html; charset=utf-8', body=HTML))
            page = context.pages[0]
            collector = Collector(context)
            collector.page = page
            result = await collector.collect(URL)
            assert result['status'] == 'authorized'
            assert [item['id'] for item in result['items']] == ['123']
            assert result['items'][0]['price'] == 11950000
            assert 'СКРЫТЫЙ' not in result['items'][0]['card_text']
            # Synthetic local storage marker tests actual on-disk profile persistence.
            await page.evaluate("localStorage.setItem('xata-fixture', 'retained')")
            await context.close()
            context = await playwright.chromium.launch_persistent_context(profile, headless=True)
            await context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html; charset=utf-8', body=HTML))
            page = context.pages[0]
            collector = Collector(context)
            collector.page = page
            await page.goto(URL)
            assert await page.evaluate("localStorage.getItem('xata-fixture')") == 'retained'
            # A late challenge discards already extracted cards.
            original = collector.inspect
            inspections = 0

            async def late_challenge(http_status=200):
                nonlocal inspections
                inspections += 1
                if inspections == 3:
                    await page.evaluate("document.body.insertAdjacentHTML('beforeend', '<p>Подтвердите, что вы человек</p>')")
                return await original(http_status)

            collector.inspect = late_challenge
            result = await collector.collect(URL)
            assert result['status'] == 'confirmation_required' and result['items'] == []
            collector.inspect = original
            await context.unroute('**/*')
            await context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html; charset=utf-8',
                                                                  body='<body><button>Войти</button></body>'))
            result = await collector.collect(URL)
            assert result['status'] == 'login_required' and result['items'] == []
            await context.unroute('**/*')
            await context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html; charset=utf-8',
                                                                  body='<body><span data-name="UserAvatar">Профиль</span><p>Unknown layout</p></body>'))
            # Fast deterministic timeout; unsupported DOM must never succeed empty.
            import collector.service as service
            original_sleep = service.asyncio.sleep
            async def no_delay(_):
                return None
            service.asyncio.sleep = no_delay
            try:
                result = await collector.collect(URL)
            finally:
                service.asyncio.sleep = original_sleep
            assert result['status'] == 'error' and result['items'] == []
            collector.lock = asyncio.Lock()
            await collector.lock.acquire()
            try:
                from fastapi import HTTPException
                with pytest.raises(HTTPException) as raised:
                    await collector.collect(URL)
                assert raised.value.status_code == 409
            finally:
                collector.lock.release()
            await context.close()
    asyncio.run(scenario())
