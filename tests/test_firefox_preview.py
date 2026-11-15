import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from collector.browser_runtime import browser_engine, launch_context
from collector.result_preview import preview_results, same_search


@pytest.mark.parametrize('engine', ['chromium', 'firefox'])
def test_engine_profiles_are_separate_and_headed(monkeypatch, tmp_path, engine):
    monkeypatch.setenv('BROWSER_ENGINE', engine)
    calls = []
    async def launch(path, **kwargs):
        calls.append((path, kwargs))
        return 'context'
    runtime = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch),
                              firefox=SimpleNamespace(launch_persistent_context=launch))
    assert asyncio.run(launch_context(runtime, tmp_path)) == 'context'
    assert calls[0][0] == str(tmp_path / engine)
    assert calls[0][1]['headless'] is False
    assert 'user_agent' not in calls[0][1]


def test_unknown_engine_is_rejected(monkeypatch):
    monkeypatch.setenv('BROWSER_ENGINE', 'other')
    with pytest.raises(ValueError):
        browser_engine()


def test_query_comparison_does_not_ignore_constraints():
    a = 'https://www.cian.ru/cat.php?room1=1&foot_min=10&district%5B0%5D=777'
    assert same_search(a + '&context=not-reported', a)
    assert same_search(a.replace('www.cian.ru', 'cian.ru'), a)
    assert not same_search(a.replace('foot_min=10','foot_min=20'), a)
    assert not same_search(a.replace('777','888'), a)
    assert not same_search(a.replace('www.cian.ru','evil.test'), a)
    assert not same_search(a.replace('www.cian.ru','spb.cian.ru'), a)


def test_preview_reads_current_page_only_and_does_not_collect():
    class Page:
        url = 'https://www.cian.ru/cat.php?room1=1'
        async def evaluate(self, script, selector):
            assert selector == '#primary'
            return {'offers':[{'id':'123'}], 'containers':1}
    class Collector:
        lock = asyncio.Lock()
        page = Page()
        async def inspect(self):
            return 'authorized'
        async def collect(self, *args):
            pytest.fail('Preview must not collect')
    async def run():
        result = await preview_results(Collector(), '#primary', Page.url)
        assert result['query_matches_configured_search']
        assert result['mode'] == 'preview_no_import'
        assert 'url' not in result
    asyncio.run(run())


def test_review_only_blocks_import_even_when_manual_flag_is_off(monkeypatch):
    import collector.service as service
    monkeypatch.setattr(service, 'REVIEW_ONLY', True)
    monkeypatch.setattr(service, 'MANUAL', False)
    from fastapi import HTTPException
    collector = service.Collector(None)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(collector.collect('https://www.cian.ru/cat.php?room1=1'))
    assert exc.value.status_code == 409


def test_all_preview_routes_require_auth_and_open_search_is_pilot_only(monkeypatch):
    import collector.service as service
    from fastapi.testclient import TestClient
    monkeypatch.setenv('BROWSER_API_TOKEN', 'preview-test-token-16')
    client = TestClient(service.app)  # No lifespan, browser or network.
    assert client.get('/preview/results').status_code == 401
    assert client.get('/preview/screenshot').status_code == 401
    assert client.post('/preview/open-search').status_code == 401
    monkeypatch.setattr(service, 'REVIEW_ONLY', False)
    client.headers['Authorization'] = 'Bearer preview-test-token-16'
    assert client.post('/preview/open-search').status_code == 409


def test_pilot_preparation_does_not_deploy_or_copy_production_credentials(tmp_path, monkeypatch):
    import scripts.prepare_firefox_pilot as prepare
    calls = []
    info = [{'Config':{'Env':['CIAN_SEARCH_URL=https://www.cian.ru/cat.php?room1=1',
                            'APP_PASSWORD=must-not-copy','BROWSER_API_TOKEN=must-not-copy']}}]
    monkeypatch.setattr(prepare.subprocess, 'check_output', lambda *a, **kw: json.dumps(info).encode())
    monkeypatch.setattr(prepare.subprocess, 'run', lambda args, **kw: calls.append(args))
    monkeypatch.setattr(prepare.os.sys, 'argv', ['prepare','--output',str(tmp_path/'pilot'),'--build'])
    prepare.main()
    raw = (tmp_path/'pilot/compose.json').read_text()
    doc = json.loads(raw)
    browser = doc['services']['browser']
    assert 'must-not-copy' not in raw and 'APP_PASSWORD' not in raw
    assert browser['environment']['BROWSER_ENGINE'] == 'firefox'
    assert browser['environment']['BROWSER_REVIEW_ONLY'] == '1'
    assert browser['ports'] == ['127.0.0.1:16081:6080']
    assert browser['volumes'] == ['firefox_profile:/profile']
    assert 'xata_default' not in raw and 'xata_xata_data' not in raw
    assert all('up' not in args and 'stop' not in args and 'restart' not in args for args in calls)
