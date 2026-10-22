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
            service.DOM_WAIT_SECONDS = .5
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


@pytest.mark.parametrize('failure_type,failures', [
    ('asyncio', 1), ('asyncio', 3), ('playwright_timeout', 2), ('playwright_error', 2),
])
def test_loading_classification_retries_then_collects_cards(tmp_path, monkeypatch, caplog, failure_type, failures):
    """Actual DOM extraction still succeeds after transient body/classification errors."""
    async def scenario():
        from playwright.async_api import async_playwright, Locator, TimeoutError as PlaywrightTimeoutError, Error
        from collector.service import Collector
        from collector import service
        original_inner_text = Locator.inner_text
        attempts = 0
        navigations = 0

        async def transient_body_read(locator, *args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts <= failures:
                message = 'PRIVATE https://cian.ru/?secret=DO_NOT_LOG <html>DO_NOT_LOG</html>'
                error = {'asyncio': asyncio.TimeoutError,
                         'playwright_timeout': PlaywrightTimeoutError,
                         'playwright_error': Error}[failure_type]
                raise error(message)
            return await original_inner_text(locator, *args, **kwargs)

        monkeypatch.setattr(Locator, 'inner_text', transient_body_read)
        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(str(tmp_path / 'profile'), headless=True)
            async def fixture(route):
                nonlocal navigations
                if route.request.is_navigation_request():
                    navigations += 1
                await route.fulfill(status=200, content_type='text/html; charset=utf-8', body=HTML)
            await context.route('**/*', fixture)
            collector = Collector(context)
            collector.page = context.pages[0]
            started = time.monotonic()
            result = await asyncio.wait_for(collector.collect(URL), timeout=5)
            assert time.monotonic() - started < 5
            assert attempts >= failures + 3  # recovered readiness + both final checks
            assert navigations == 1  # retry DOM reads, never the navigation
            assert result['status'] == 'authorized'
            assert [item['id'] for item in result['items']] == ['123']
            assert result['items'][0]['price'] == 11950000
            assert not collector.lock.locked()
            await context.close()
    caplog.set_level('INFO', logger='xata.browser')
    asyncio.run(scenario())
    assert 'DOM still loading stage=classification_loading' in caplog.text
    assert 'browser collection error stage=classification_loading' not in caplog.text
    assert 'matching_card_nodes=2' in caplog.text
    assert 'PRIVATE' not in caplog.text and 'DO_NOT_LOG' not in caplog.text



def test_timelabel_is_only_used_for_explicit_travel_duration(tmp_path):
    """TimeLabel publication timestamps cannot replace a rendered metro route."""
    async def scenario():
        from playwright.async_api import async_playwright
        from collector.service import Collector
        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(str(tmp_path/'profile'),headless=True)
            page = context.pages[0]
            collector = Collector(context)
            collector.page = page
            for label,route,expected,walk in [
                ('4 часа назад сегодня, 13:25','м. Сокол<br>7 мин пешком','7 мин. пешком',7),
                ('вчера вечером','м. Сокол<br>12 минут пешком','12 мин. пешком',12),
                ('7 минут назад','м. Сокол',None,None),
                ('сегодня, 13:25','м. Сокол',None,None),
                ('7 мин пешком','м. Сокол','7 мин. пешком',7),
                ('5 мин. транспортом','м. Сокол<br>7 мин пешком','5 мин. транспортом',None),
            ]:
                html = ('<!doctype html><meta charset="utf-8"><span data-name="UserAvatar">Профиль</span>'
                        '<article data-name="CardComponent"><a href="https://cian.ru/sale/flat/123/">Квартира</a>'
                        '<div>'+route+'</div><span data-name="TimeLabel">'+label+'</span></article>')
                await context.route('**/*',lambda route:route.fulfill(status=200,content_type='text/html; charset=utf-8',body=html))
                result = await collector.collect(URL)
                assert result['status']=='authorized', result
                assert result['items'][0].get('metro_time')==expected
                assert result['items'][0].get('metro_walk_minutes')==walk
                await context.unroute('**/*')
            await context.close()
    asyncio.run(scenario())



def test_only_first_visible_cover_is_extracted_without_gallery(tmp_path):
    async def scenario():
        import io
        from PIL import Image
        from playwright.async_api import async_playwright
        from collector.service import Collector
        photo='https://images.cdn-cian.ru/images/visible-first.jpg'
        html=('''<!doctype html><meta charset="utf-8"><span data-name="UserAvatar">Профиль</span>
          <article data-name="CardComponent"><a href="https://cian.ru/sale/flat/123/">
          <img src="https://images.cdn-cian.ru/images/hidden.jpg" width="110" height="80" style="display:none">
          <img src="'''+photo+'''" width="110" height="80">
          <img src="https://images.cdn-cian.ru/images/second.jpg" width="110" height="80" style="display:none">
          <span data-mark="OfferTitle">1-комн. квартира, 35 м²</span></a>
          <p>Авторский текст сохраняется для matcher</p></article>''')
        data=io.BytesIO()
        with Image.new('RGB',(110,80),'green') as image:image.save(data,format='PNG')
        async with async_playwright() as playwright:
            context=await playwright.chromium.launch_persistent_context(str(tmp_path/'profile'),headless=True)
            async def route(request):
                if request.request.resource_type=='image':
                    await request.fulfill(status=200,content_type='image/png',body=data.getvalue())
                else:
                    await request.fulfill(status=200,content_type='text/html; charset=utf-8',body=html)
            await context.route('**/*',route)
            collector=Collector(context);collector.page=context.pages[0]
            result=await collector.collect(URL)
            assert result['status']=='authorized'
            item=result['items'][0]
            assert item['cover_image_url']==photo
            assert 'Авторский текст сохраняется' in item['card_text']
            assert not any('gallery' in name or 'photos' in name for name in item)
            await context.close()
    asyncio.run(scenario())
