"""Offline parity/scoping tests for both real browser engines."""
import asyncio
import os

import pytest

pytestmark = pytest.mark.skipif(os.getenv('RUN_BROWSER_TESTS') != '1', reason='Browser tests opt in')


@pytest.mark.parametrize('engine', ['chromium','firefox'])
def test_preview_ids_match_explicit_scope_and_screenshot(engine, tmp_path, monkeypatch):
    async def run():
        from playwright.async_api import async_playwright
        from collector.result_preview import PREVIEW_DOM, SCOPED_SNAPSHOT
        def card(i):
            return f'<article data-name="CardComponent"><a href="https://cian.ru/sale/flat/{i}/">1-комн. квартира</a></article>'
        async with async_playwright() as p:
            browser = await getattr(p,engine).launch(headless=True)
            page = await browser.new_page(viewport={'width':1280,'height':900})
            html = '<span data-name="UserAvatar">Профиль</span><main><h1>Поиск</h1><section id="primary">'+card(123)+card(124)+\
                '</section><aside>'+card(333386132)+'</aside></main>'
            await page.set_content(html)
            preview = await page.evaluate(PREVIEW_DOM, '#primary')
            assert [o['id'] for o in preview['offers'] if o['in_selected_container']] == ['123','124']
            assert [o['id'] for o in preview['offers'] if not o['in_selected_container']] == ['333386132']
            snapshot = await page.evaluate(SCOPED_SNAPSHOT, '#primary')
            assert snapshot['scope_confirmed'] and len(snapshot['cards']) == 2
            for selector in ('', '#missing', 'section,aside'):
                assert not (await page.evaluate(SCOPED_SNAPSHOT, selector))['scope_confirmed']
            png = await page.screenshot()
            assert png.startswith(b'\x89PNG')
            # End-to-end collector snapshot must use the exact same primary scope.
            from collector import service
            monkeypatch.setattr(service, 'MANUAL', False)
            monkeypatch.setattr(service, 'REVIEW_ONLY', False)
            monkeypatch.setattr(service, 'RESULTS_SELECTOR', '#primary')
            monkeypatch.setenv('BROWSER_REQUIRE_RESULT_SCOPE', '1')
            monkeypatch.setattr(service, 'MAX_PAGES', 1)
            monkeypatch.setattr(service, 'MAX_SCROLLS', 0)
            await page.context.route('**/*', lambda route: route.fulfill(status=200,content_type='text/html',body=html))
            collector = service.Collector(page.context)
            collector.page = page
            result = await collector.collect('https://www.cian.ru/cat.php?room1=1')
            assert result['status'] == 'authorized'
            assert [item['id'] for item in result['items']] == ['123','124']
            monkeypatch.setattr(service, 'RESULTS_SELECTOR', '#missing')
            result = await collector.collect('https://www.cian.ru/cat.php?room1=1')
            assert result['status'] == 'error' and result['items'] == []
            await browser.close()
    asyncio.run(run())


@pytest.mark.parametrize('engine', ['chromium','firefox'])
def test_observed_cian_six_offers_and_nineteen_suggestions_share_css_class(engine):
    async def run():
        from playwright.async_api import async_playwright
        from collector.result_preview import SCOPED_SNAPSHOT, PREVIEW_DOM
        from collector.page import CARD_SNAPSHOT
        primary = ['336044883','335594112','335586264','335396246','335042696','335016684']
        suggestions = ['336311683'] + [str(900000000+i) for i in range(18)]
        def card(i):
            return f'<article data-name="CardComponent"><a href="https://www.cian.ru/sale/flat/{i}/">Квартира</a></article>'
        async with async_playwright() as p:
            browser = await getattr(p,engine).launch(headless=True)
            page = await browser.new_page()
            await page.set_content('<main data-name="SearchEngineResultsPageContainer">'
                '<div data-name="Offers" class="shared-wrapper">'+''.join(map(card,primary))+'</div>'
                '<h2>Дополнительные предложения по вашему запросу</h2>'
                '<div data-name="SuggestionsLayout" class="shared-wrapper">'+''.join(map(card,suggestions))+'</div></main>')
            selector = '[data-name="Offers"]'
            preview = await page.evaluate(PREVIEW_DOM, selector)
            assert len(preview['offers']) == 25
            assert [o['id'] for o in preview['offers'] if o['in_selected_container']] == primary
            scoped = await page.evaluate(SCOPED_SNAPSHOT, selector)
            assert scoped['scope_confirmed'] and scoped['matching'] == 6
            assert all('336311683' not in c['url'] for c in scoped['cards'])
            # Even the old broad fallback must now recognize the observed suggestion block.
            broad = await page.evaluate(CARD_SNAPSHOT)
            assert len(broad['cards']) == 6 and broad['skipped'] == 19
            await browser.close()
    asyncio.run(run())
