import importlib
import io
import json
import time
import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from app import source


def page():
    item = {'@type': 'Apartment', 'name': '=опасная формула', 'url': 'https://www.cian.ru/sale/flat/123/', 'address': {'streetAddress': 'Москва, улица'}, 'offers': {'price': 13000000}}
    return '<script type="application/ld+json">' + json.dumps(item) + '</script>'


def test_source():
    assert source.parse_page(page())[0]['price'] == 13000000
    for html in ('CAPTCHA', '<html>unknown layout</html>'):
        with pytest.raises(source.SourceError):
            source.parse_page(html)
    url = source.search_url({'max_price': 14000000, 'metro_minutes': 20})
    assert 'foot_min=20' in url and 'room1=1' in url


def test_workflow(tmp_path, monkeypatch):
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.setenv('APP_USER', 'owner')
    monkeypatch.setenv('APP_PASSWORD', 'long-test-password')
    from app import main
    importlib.reload(main)
    with TestClient(main.app) as client:
        assert client.get('/healthz').status_code == 200
        assert client.get('/api/state').status_code == 401
        client.auth = ('owner', 'long-test-password')
        assert client.get('/').status_code == 200
        assert client.post('/api/collect', json={}, headers={'Origin': 'https://evil.test'}).status_code == 403
        assert client.post('/api/collect', json={'metro_minutes': 0}).status_code == 422
        monkeypatch.setattr(source, 'collect', lambda filters: source.parse_page(page()))
        assert client.post('/api/collect', json={}).status_code == 202
        for _ in range(100):
            state = client.get('/api/state').json()
            if state['run']['status'] != 'running':
                break
            time.sleep(.01)
        assert state['run']['status'] == 'done'
        assert len(state['listings']) == 1
        assert client.post('/api/collect', json={}).status_code == 429
        workbook = load_workbook(io.BytesIO(client.get('/api/export.xlsx').content))
        assert workbook.active['B2'].value.startswith("'=")
        with main.database() as conn:
            conn.execute("UPDATE runs SET started='2000-01-01T00:00:00+00:00'")
        def fail(filters):
            raise source.SourceError('CAPTCHA: сбор остановлен')
        monkeypatch.setattr(source, 'collect', fail)
        assert client.post('/api/collect', json={}).status_code == 202
        for _ in range(100):
            state = client.get('/api/state').json()
            if state['run']['status'] != 'running':
                break
            time.sleep(.01)
        assert state['run']['status'] == 'error'
        assert 'CAPTCHA' in state['run']['message']
        assert len(state['listings']) == 1
