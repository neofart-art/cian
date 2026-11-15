"""Synthetic public DOM only; never visit Cian during tests."""
import asyncio
import os

import pytest

pytestmark = pytest.mark.skipif(os.getenv('RUN_BROWSER_TESTS') != '1', reason='Chromium opt in')


def test_results_recommendations_heading_boundary_and_visible_text():
    async def run():
        from playwright.async_api import async_playwright
        from collector.page import CARD_SNAPSHOT, normalize_card

        def card(offer_id, title='1-комн. квартира, 40 м²'):
            return f'''<article data-name="CardComponent">
              <a href="https://www.cian.ru/sale/flat/{offer_id}/">
                <span data-mark="OfferTitle">{title}</span></a>
              <span data-mark="MainPrice">13 000 000 ₽</span>
              <span style="display:none">Аукцион СКРЫТЫЙ</span>
              <div data-name="GeoLabel">Москва, р-н Тестовый</div>
              <div data-name="TimeLabel">10 мин. пешком</div></article>'''

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.set_content('<main><h2>Результаты поиска</h2>' + card('123') +
                '<section data-name="Recommendations">' + card('333386132') + '</section>' +
                card('124', '1-комн. квартира, рекомендуем посмотреть') +
                '<h2 style="display:none">Похожие предложения</h2>' + card('125') +
                '<h2>Похожие предложения</h2>' + card('126') + '</main>')
            result = await page.evaluate(CARD_SNAPSHOT)
            assert result['matching'] == 5
            assert [normalize_card(c)['id'] for c in result['cards']] == ['123', '124', '125']
            assert result['skipped'] == 2
            assert all('СКРЫТЫЙ' not in c['text'] for c in result['cards'])
            await browser.close()
    asyncio.run(run())
