"""Offline Chromium tests: no requests reach Cian or any external server."""
import asyncio
import os
import time

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
            await page.evaluate("document.cookie = 'xata_fixture=retained; Max-Age=86400; Secure; SameSite=Lax; Path=/'")
            await context.close()
            context = await playwright.chromium.launch_persistent_context(profile, headless=True)
            await context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html; charset=utf-8', body=HTML))
            page = context.pages[0]
            collector = Collector(context)
            collector.page = page
            await page.goto(URL)
            assert await page.evaluate("localStorage.getItem('xata-fixture')") == 'retained'
            assert await page.evaluate("document.cookie.includes('xata_fixture=retained')")
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
            original_wait = service.DOM_WAIT_SECONDS
            service.DOM_WAIT_SECONDS = 0
            try:
                result = await collector.collect(URL)
            finally:
                service.DOM_WAIT_SECONDS = original_wait
            assert result['status'] == 'error' and result['items'] == []
            assert result['error'] == 'Авторизация подтверждена, но карточки выдачи не распознаны'
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


def test_react_card_replacement_and_bad_card_do_not_stall(tmp_path, caplog):
    """Cards are removed/replaced between RPCs; one bad card never loses good data."""
    async def scenario():
        from playwright.async_api import async_playwright
        from collector.service import Collector
        from collector.page import CARD_SELECTOR
        html = """<!doctype html><meta charset="utf-8"><body>
        <span data-name="UserAvatar">Профиль</span><main id="results"></main>
        <script>
        window.fixtureReplacements = 0;
        function replaceCards() {
          const root = document.querySelector('#results');
          root.replaceChildren();
          const good = document.createElement('article');
          good.dataset.name = 'CardComponent';
          good.innerHTML = '<a href="https://cian.ru/sale/flat/123/"><span data-mark="OfferTitle">Квартира</span></a><span data-mark="MainPrice">12 000 000 ₽</span>';
          root.append(good);
          const bad = document.createElement('article');
          bad.dataset.name = 'CardComponent';
          bad.innerHTML = '<a href="https://example.invalid/flat/456/">Некорректная карточка</a>';
          root.append(bad);
          window.fixtureReplacements += 1;
        }
        replaceCards();
        setInterval(replaceCards, 4);
        </script></body>"""
        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(str(tmp_path / 'profile'), headless=True)
            # Deliberately keep Playwright's old 30 s default: collection must not rely on it.
            context.set_default_timeout(30000)
            await context.route('**/*', lambda route: route.fulfill(
                status=200, content_type='text/html; charset=utf-8', body=html))
            page = context.pages[0]
            collector = Collector(context)
            collector.page = page
            original_inspect = collector.inspect
            replaced_during_collection = False

            async def inspect(http_status=200):
                nonlocal replaced_during_collection
                result = await original_inspect(http_status)
                if not replaced_during_collection:
                    # Capture a real rendered node in the fixture, then prove it disappears.
                    await page.evaluate("window.fixtureOldCard = document.querySelector('#results article')")
                    await page.wait_for_function(
                        'window.fixtureOldCard && !window.fixtureOldCard.isConnected', timeout=1500)
                    replaced_during_collection = True
                return result

            collector.inspect = inspect
            started = time.monotonic()
            result = await asyncio.wait_for(collector.collect(URL), timeout=5)
            assert time.monotonic() - started < 5
            assert replaced_during_collection
            assert await page.evaluate('window.fixtureReplacements') > 1
            assert result['status'] == 'authorized'
            assert [item['id'] for item in result['items']] == ['123']
            assert result['items'][0]['price'] == 12000000
            assert not collector.lock.locked()
            await context.close()
    caplog.set_level('INFO', logger='xata.browser')
    asyncio.run(scenario())
    assert 'navigation completed' in caplog.text
    assert 'DOM classified as authorized' in caplog.text
    assert 'matching_card_nodes=2' in caplog.text
    assert 'normalized_cards=1' in caplog.text
    assert 'skipped_cards=1' in caplog.text
    assert 'example.invalid' not in caplog.text
    assert 'Некорректная карточка' not in caplog.text


def test_all_cards_disappear_before_snapshot_is_explicit_error(tmp_path):
    async def scenario():
        from playwright.async_api import async_playwright
        from collector.service import Collector
        from collector.page import CARD_SELECTOR, UNRECOGNIZED_CARDS
        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(str(tmp_path / 'profile'), headless=True)
            await context.route('**/*', lambda route: route.fulfill(
                status=200, content_type='text/html; charset=utf-8', body=HTML))
            page = context.pages[0]
            collector = Collector(context)
            collector.page = page
            original = collector.inspect
            inspections = 0

            async def inspect(http_status=200):
                nonlocal inspections
                inspections += 1
                status = await original(http_status)
                if inspections == 2:
                    await page.locator(CARD_SELECTOR).evaluate_all('nodes => nodes.forEach(node => node.remove())')
                return status

            collector.inspect = inspect
            result = await asyncio.wait_for(collector.collect(URL), timeout=5)
            assert result['status'] == 'error' and result['items'] == []
            assert result['error'] == UNRECOGNIZED_CARDS
            assert 'TimeoutError' not in result['error']
            await context.close()
    asyncio.run(scenario())


def test_snapshot_keeps_100_card_limit_and_skips_bad_dom_node(tmp_path):
    async def scenario():
        from playwright.async_api import async_playwright
        from collector.service import Collector
        html = '<!doctype html><body><span data-name="UserAvatar">Профиль</span>'
        # An unreadable card is isolated inside the JavaScript snapshot.
        html += '<article id="broken" data-name="CardComponent">Неисправная карточка</article>'
        html += ''.join(
            f'<article data-name="CardComponent"><a href="https://cian.ru/sale/flat/{index}/">Квартира</a></article>'
            for index in range(1, 106))
        html += """<script>Object.defineProperty(document.querySelector('#broken'), 'innerText', {
            get() { throw new Error('unreadable node'); }
        });</script></body>"""
        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(str(tmp_path / 'profile'), headless=True)
            await context.route('**/*', lambda route: route.fulfill(
                status=200, content_type='text/html; charset=utf-8', body=html))
            collector = Collector(context)
            collector.page = context.pages[0]
            result = await asyncio.wait_for(collector.collect(URL), timeout=5)
            assert result['status'] == 'authorized'
            assert len(result['items']) == 99  # one bad node among the first 100 matches
            assert {item['id'] for item in result['items']} == {str(index) for index in range(1, 100)}
            await context.close()
    asyncio.run(scenario())
