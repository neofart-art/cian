import importlib
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app.browser_source import BrowserAttention, CianBrowserSource
from app.source_types import SourceError
from collector.page import ATTENTION, classify, normalize_card, validate_url


@pytest.mark.parametrize('url', [
    'http://cian.ru/cat.php', 'https://cian.ru.evil.test/', 'https://evil.test/',
    'https://user:pass@cian.ru/', 'https://cian.ru:444/', 'file:///etc/passwd',
    'https://cian.ru@127.0.0.1/', 'https://cian.ru:bad/',
])
def test_search_url_restrictions(url):
    with pytest.raises(ValueError):
        validate_url(url)


def test_visible_text_classification_and_card():
    assert validate_url('https://www.cian.ru/cat.php?room1=1')
    assert classify('https://cian.ru/cat.php', 'обычная выдача', authorized=True) == 'authorized'
    assert classify('https://cian.ru/cat.php', 'обычная выдача', login=True, authorized=True) == 'login_required'
    for text in ('Вы не робот?', 'Подтвердите вход', 'Введите код', 'Доступ ограничен'):
        assert classify('https://cian.ru/cat.php', text, authorized=True) == 'confirmation_required'
    assert classify('https://cian.ru/tmgrdfrend/showcaptcha', '', authorized=True) == 'confirmation_required'
    assert classify('https://cian.ru/cat.php', '', authorized=True, http_status=429) == 'confirmation_required'
    assert classify('https://cian.ru/cat.php', '', http_status=401) == 'login_required'
    assert classify('https://cian.ru/logout/', '') == 'login_required'
    item = normalize_card({
        'url': 'https://www.cian.ru/sale/flat/00123/?from=search',
        'title': '1-комн. квартира, 34,7 м²',
        'price': '11\u00a0950\u00a0000 ₽',
        'text': '1-комн. квартира, 34,7 м², 4/9 этаж\nЖК «Тест»\nМосква, улица Тестовая, 1\nм. Сокол\n15 мин. пешком\nОписание:\nСветлая квартира',
    })
    assert item['id'] == '123'
    assert item['price'] == 11950000
    assert item['area'] == 34.7 and item['floor'] == '4/9'
    assert item['complex_name'] == 'Тест' and item['metro_walk_minutes'] == 15
    assert item['description'] == 'Светлая квартира'


def test_adapter_attention_and_service_failure(monkeypatch):
    import app.browser_source as adapter
    original = httpx.Client
    response = {'status': 'confirmation_required', 'message': ATTENTION, 'items': [{'id': 'never-save'}]}
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=response))
    monkeypatch.setattr(adapter.httpx, 'Client', lambda **kwargs: original(transport=transport, **kwargs))
    with pytest.raises(BrowserAttention) as raised:
        CianBrowserSource().collect({'max_price': 14000000, 'metro_minutes': 20})
    assert raised.value.status == 'confirmation_required'
    response.update(status='error', error='Не распознана обычная выдача')
    with pytest.raises(SourceError, match='Не распознана'):
        CianBrowserSource().collect({'max_price': 14000000, 'metro_minutes': 20})


@pytest.fixture
def browser_app(tmp_path, monkeypatch):
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.setenv('APP_USER', 'owner')
    monkeypatch.setenv('APP_PASSWORD', 'long-test-password')
    monkeypatch.setenv('BROWSER_SCHEDULE_ENABLED', 'false')
    from app import main
    importlib.reload(main)
    with TestClient(main.app) as client:
        client.auth = ('owner', 'long-test-password')
        yield client, main


def finish(client, main):
    for _ in range(200):
        state = client.get('/api/state').json()
        if state['run'] and state['run']['status'] != 'running' and not main.lock.locked():
            # Re-read after the worker released its lock (metadata is persisted last).
            return client.get('/api/state').json()
        time.sleep(.01)
    pytest.fail('background collection did not finish')


def age(main):
    with main.database() as conn:
        conn.execute("UPDATE runs SET started='2000-01-01T00:00:00+00:00'")


def test_browser_upsert_attention_and_csv_independence(browser_app, monkeypatch):
    client, main = browser_app
    items = [{'id': '123', 'url': 'https://cian.ru/sale/flat/123/', 'title': 'Квартира', 'price': 12000000, 'source': 'ЦИАН browser'}]
    monkeypatch.setattr(main.CianBrowserSource, 'collect', lambda self, filters: items)
    assert client.post('/api/browser/collect', json={}, auth=('bad', 'bad')).status_code == 401
    assert client.post('/api/browser/collect', json={}, headers={'Origin': 'https://evil.test'}).status_code == 403
    assert client.post('/api/browser/collect', json={'metro_minutes': 0}).status_code == 422
    assert client.post('/api/browser/collect', json={}).status_code == 202
    first = finish(client, main)
    assert first['browser']['new_count'] == 1
    first_seen = first['listings'][0]['first_seen']
    assert client.post('/api/browser/collect', json={}).status_code == 429
    age(main)
    items[0]['price'] = 11900000
    assert client.post('/api/browser/collect', json={}).status_code == 202
    second = finish(client, main)
    assert len(second['listings']) == 1 and second['browser']['new_count'] == 0
    assert second['listings'][0]['first_seen'] == first_seen
    assert second['listings'][0]['price'] == 11900000
    success = second['browser']['last_success']
    age(main)

    def blocked(self, filters):
        raise BrowserAttention('confirmation_required', ATTENTION)

    monkeypatch.setattr(main.CianBrowserSource, 'collect', blocked)
    assert client.post('/api/browser/collect', json={}).status_code == 202
    stopped = finish(client, main)
    assert stopped['run']['status'] == 'blocked'
    assert stopped['browser']['status'] == 'confirmation_required'
    assert stopped['browser']['last_success'] == success
    assert stopped['listings'][0]['first_seen'] == first_seen
    assert client.get('/healthz').status_code == 200
    csv = b'url;price\nhttps://cian.ru/sale/flat/123/;11800000'
    preview = client.post('/api/import/preview', content=csv).json()
    assert client.post('/api/import/confirm', json={'token': preview['token']}).json()['updated'] == 1
    assert client.get('/api/state').json()['listings'][0]['first_seen'] == first_seen
    assert client.get('/api/export.xlsx').status_code == 200
    importlib.reload(main)
    assert main.browser_state()['last_success'] == success
    assert main.browser_state()['status'] == 'confirmation_required'


def test_app_lock_and_schedule_pause(browser_app, monkeypatch):
    client, main = browser_app
    monkeypatch.setenv('BROWSER_SEARCH_INTERVAL_SECONDS', '10')
    assert main.schedule_interval() == 1800
    monkeypatch.setenv('BROWSER_SEARCH_INTERVAL_SECONDS', '3600')
    assert main.schedule_interval() == 3600
    main.lock.acquire()
    try:
        assert client.post('/api/browser/collect', json={}).status_code == 409
        assert client.post('/api/collect', json={}).status_code == 409
    finally:
        main.lock.release()
    called = []
    monkeypatch.setattr(main, 'start_browser', lambda filters: called.append(filters))
    monkeypatch.setattr(main.CianBrowserSource, 'status', lambda self: {'status': 'authorized', 'message': 'Авторизован'})
    for status in ('login_required', 'confirmation_required'):
        main.update_browser(status=status)
        main.scheduler_tick()
    assert called == []
    main.update_browser(status='authorized')
    main.scheduler_tick()
    assert len(called) == 1
    with main.database() as conn:
        conn.execute("INSERT INTO runs(started,status,filters,source) VALUES (?,?,?,?)",
                     (main.datetime.now(main.timezone.utc).isoformat(), 'done', '{}', 'browser'))
    main.scheduler_tick()
    assert len(called) == 1
    # No navigation and no forced 'login_required' just because browser restarted.
    monkeypatch.setattr(main.CianBrowserSource, 'status', lambda self: {'status': 'unknown', 'message': 'Сессия ещё не проверена'})
    assert client.get('/api/browser/status').status_code == 200
    assert main.browser_state()['status'] == 'authorized'


def test_browser_api_auth_and_manual_mode(monkeypatch):
    pytest.importorskip('playwright')
    from collector import service

    monkeypatch.setenv('BROWSER_API_TOKEN', 'internal-test-token-16')
    class Page:
        url = 'about:blank'
        async def evaluate(self, _):
            return 1
    collector = service.Collector(None)
    collector.page = Page()
    service.app.state.collector = collector
    # No lifespan: no Chromium/network needed for access-control contracts.
    client = TestClient(service.app)
    assert client.get('/healthz').status_code == 200
    assert client.get('/status').status_code == 401
    client.headers['Authorization'] = 'Bearer internal-test-token-16'
    assert client.get('/status').json()['status'] == 'unknown'
    assert client.post('/collect', json={'url': 'http://127.0.0.1/'}).status_code == 422
    monkeypatch.setattr(service, 'MANUAL', True)
    assert client.post('/collect', json={'url': 'https://cian.ru/cat.php'}).status_code == 409


def test_navigation_timeout_with_visible_challenge(monkeypatch, caplog):
    pytest.importorskip('playwright')
    import asyncio
    from collector import service
    from playwright.async_api import Error

    class Page:
        async def goto(self, *args, **kwargs):
            raise Error('navigation timed out https://cian.ru/?secret=DO_NOT_LOG <html>PRIVATE</html>')

    async def scenario():
        collector = service.Collector(None)
        collector.page = Page()
        async def inspect(http_status=200):
            return 'confirmation_required'
        collector.inspect = inspect
        result = await collector.collect('https://cian.ru/cat.php')
        assert result['status'] == 'confirmation_required'
        assert result['message'] == ATTENTION
        assert result['items'] == []
        assert not collector.lock.locked()
    monkeypatch.setattr(service, 'MANUAL', False)
    asyncio.run(scenario())
    assert 'stage=navigation error_type=Error' in caplog.text
    assert 'DO_NOT_LOG' not in caplog.text and 'PRIVATE' not in caplog.text


def test_snapshot_timeout_is_bounded_and_reports_stage(monkeypatch, caplog):
    pytest.importorskip('playwright')
    import asyncio
    from collector import service

    class Cards:
        async def count(self):
            return 1
        async def evaluate_all(self, expression):
            await asyncio.sleep(30)

    class Page:
        async def goto(self, *args, **kwargs):
            return None
        def locator(self, selector):
            return Cards()

    async def scenario():
        collector = service.Collector(None)
        collector.page = Page()
        async def inspect(http_status=200):
            return 'authorized'
        collector.inspect = inspect
        result = await asyncio.wait_for(collector.collect('https://cian.ru/cat.php'), timeout=1)
        assert result['status'] == 'error' and result['items'] == []
        assert not collector.lock.locked()
    monkeypatch.setattr(service, 'MANUAL', False)
    monkeypatch.setattr(service, 'SNAPSHOT_TIMEOUT_SECONDS', .02)
    asyncio.run(scenario())
    assert 'stage=card_snapshot error_type=TimeoutError' in caplog.text


@pytest.mark.parametrize('slow_attempt', [False, True])
def test_readiness_errors_share_one_deadline(monkeypatch, caplog, slow_attempt):
    pytest.importorskip('playwright')
    import asyncio
    from collector import service

    class Page:
        async def goto(self, *args, **kwargs):
            return None
        def locator(self, _):
            raise AssertionError('No card reads without successful classification')

    async def scenario():
        collector = service.Collector(None)
        collector.page = Page()
        attempts = 0
        async def inspect(http_status=200):
            nonlocal attempts
            attempts += 1
            if slow_attempt:
                await asyncio.sleep(30)
            raise asyncio.TimeoutError('DO_NOT_LOG https://cian.ru/?secret=PRIVATE <html>PRIVATE</html>')
        collector.inspect = inspect
        started = time.monotonic()
        result = await asyncio.wait_for(collector.collect('https://cian.ru/cat.php'), timeout=2)
        elapsed = time.monotonic() - started
        assert .5 <= elapsed < 1.5
        if slow_attempt:
            assert attempts == 1
        else:
            assert attempts >= 2
        assert result['status'] == 'error' and result['items'] == []
        assert result['error'] == service.READINESS_TIMEOUT
        assert 'TimeoutError' not in result['error']
        assert not collector.lock.locked()
    monkeypatch.setattr(service, 'DOM_WAIT_SECONDS', .55)
    monkeypatch.setattr(service, 'MANUAL', False)
    caplog.set_level('INFO', logger='xata.browser')
    asyncio.run(scenario())
    assert 'readiness deadline exceeded stage=classification_loading' in caplog.text
    assert 'browser collection error stage=classification_loading' not in caplog.text
    assert 'PRIVATE' not in caplog.text and 'DO_NOT_LOG' not in caplog.text


@pytest.mark.parametrize('attention', ['confirmation_required', 'login_required'])
def test_readiness_retry_stops_on_confirmation(monkeypatch, attention):
    pytest.importorskip('playwright')
    import asyncio
    from collector import service
    from playwright.async_api import Error

    class Page:
        async def goto(self, *args, **kwargs):
            return None
        def locator(self, _):
            raise AssertionError('No card reads after confirmation/login is detected')

    async def scenario():
        collector = service.Collector(None)
        collector.page = Page()
        attempts = 0
        async def inspect(http_status=200):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise Error('execution context changed')
            return attention
        collector.inspect = inspect
        result = await asyncio.wait_for(collector.collect('https://cian.ru/cat.php'), timeout=1)
        assert result['status'] == attention and result['items'] == []
        assert attempts == 2
        assert not collector.lock.locked()
    monkeypatch.setattr(service, 'MANUAL', False)
    asyncio.run(scenario())
