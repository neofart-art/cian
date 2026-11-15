from pathlib import Path

import pytest

from scripts.prepare_admission_update import backport_scope

ROOT = Path(__file__).parents[1]


def test_backport_keeps_old_collector_contract_and_local_unrelated_code():
    old = (ROOT / 'tests/fixtures/collector_page_before_pagination.py').read_text()
    old += '\n# Unrelated local modification retained\n'
    new = (ROOT / 'collector/page.py').read_text()
    patched = backport_scope(old, new)
    assert 'recommendation' in patched
    assert 'Unrelated local modification retained' in patched
    assert 'PAGINATION_SNAPSHOT' not in patched
    assert 'next_search_url' not in patched
    assert 'CARD_PROGRESS' not in patched
    compile(patched, 'page.py', 'exec')
    namespace = {}
    exec(compile(patched, 'page.py', 'exec'), namespace)
    assert 'recommendation' in namespace['CARD_SNAPSHOT']
    assert namespace['normalize_card']({'url': 'https://cian.ru/sale/flat/123/', 'text': ''})['id'] == '123'


def test_unknown_extractor_is_not_overwritten():
    old = (ROOT / 'tests/fixtures/collector_page_before_pagination.py').read_text()
    new = (ROOT / 'collector/page.py').read_text()
    with pytest.raises(ValueError, match='modified card extractor'):
        backport_scope(old.replace('EXTRACT_CARD = r"""el => {', 'EXTRACT_CARD = r"""el => { /* local */'), new)
