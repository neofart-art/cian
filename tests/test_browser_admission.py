import json

import pytest

from app.browser_admission import Admission
from app.browser_source import CianBrowserSource
from app.source_types import SourceError
from tests.test_browser import browser_app, finish, age


# ID/name here is an offline fixture, not a Cian geographic dictionary.
URL = ('https://www.cian.ru/cat.php?district%5B0%5D=777&room1=1'
       '&maxprice=15000000&mintarea=35&only_foot=2&foot_min=10'
       '&is_first_floor=0&minfloorn=5&electronic_trading=2')
FILTERS = {'max_price': 15000000, 'metro_minutes': 10}
NAMES = json.dumps({'ids': ['777'], 'names': ['Тестовый']}, ensure_ascii=False)


def card(**changes):
    return dict(dict(id='123', title='1-комн. квартира, 40,6 м²',
        price=13000000, area=40.6, floor='10/14',
        address='Москва, ЮВАО, р-н Тестовый, улица Примерная, 1',
        metro='Люблино', metro_time='10 мин. пешком', metro_walk_minutes=10,
        card_text='1-комн. квартира, 40,6 м²'), **changes)


def policy():
    return Admission(URL, FILTERS, NAMES)


def test_reported_kapotnya_transport_card_never_reaches_upsert():
    item = card(id='333386132', address='Москва, ЮВАО, р-н Капотня, м. Люблино, 2-й кв-л, вл21',
                metro_time='10 мин. транспортом', metro_walk_minutes=10)
    admitted, stats = policy().apply([item])
    assert admitted == []
    assert stats['reasons'] == {'transport': 1, 'district': 1}


@pytest.mark.parametrize(('changes', 'reason'), [
    ({'card_text': 'Аукцион. Объект реализуется на городских торгах'}, 'auction'),
    ({'search_section': 'recommendation'}, 'recommendation'),
    ({'metro_time': '11 мин. пешком'}, 'walk_time'),
    ({'metro_time': '10 мин.'}, 'walk_unknown'),
    ({'metro_time': None}, 'walk_unknown'),
    ({'price': None}, 'price_unknown'),
    ({'price': 15000001}, 'price'),
    ({'price': float('nan')}, 'price_unknown'),
    ({'area': 34.9}, 'area'),
    ({'area': None}, 'area_unknown'),
    ({'title': '2-комн. квартира, 40 м²'}, 'rooms'),
    ({'floor': '1/14'}, 'first_floor'),
    ({'floor': '2/4'}, 'storeys'),
    ({'address': 'Москва, улица Примерная, м. Тестовый'}, 'district_unknown'),
    ({'address': 'Москва, р-н Тестовый Южный, улица Примерная'}, 'district'),
])
def test_reject_invalid_and_unknown(changes, reason):
    assert reason in policy().reasons(card(**changes))


def test_valid_card_and_building_storeys_not_apartment_floor():
    assert policy().reasons(card(floor='2/14')) == []
    items, stats = policy().apply([card(), card(id='bad', metro_time='10 мин. транспортом')])
    assert [i['id'] for i in items] == ['123']
    assert stats['received'] == 2 and stats['rejected'] == 1


def test_districts_fail_closed_without_verified_names():
    p = Admission(URL, FILTERS)
    assert p.missing_district_names == 1
    assert 'district_unverified' in p.reasons(card())
    with pytest.raises(SourceError, match='текущего набора ID'):
        Admission(URL + '&district%5B1%5D=888', FILTERS, NAMES)


def test_selected_names_only_and_exact_normalization():
    p = Admission(URL, FILTERS, NAMES)
    assert p.reasons(card()) == []
    assert 'district' in p.reasons(card(address='Москва, р-н Капотня, улица Примерная'))


@pytest.mark.parametrize('mapping', ['[]', '{', '{"777": 3}', '{"777": ""}'])
def test_bad_mapping_is_explicit_error(mapping):
    with pytest.raises(SourceError):
        Admission(URL, FILTERS, mapping)


def test_legacy_collector_no_coverage_is_still_guarded(monkeypatch):
    monkeypatch.setenv('CIAN_SEARCH_URL', URL)
    monkeypatch.setenv('CIAN_DISTRICT_POLICY', NAMES)
    source = CianBrowserSource()
    payload = {'status': 'authorized', 'items': [card(), card(id='333386132', metro_time='10 мин. транспортом')]}
    monkeypatch.setattr(source, 'request', lambda *args, **kwargs: payload)
    items = source.collect(FILTERS)
    assert [i['id'] for i in items] == ['123']
    assert items[0]['source'] == source.name
    assert source.last_coverage['admission']['reasons'] == {'transport': 1}
    payload['items'] = payload['items'][1:]
    with pytest.raises(SourceError, match='Все полученные карточки'):
        source.collect(FILTERS)


def test_partial_reject_does_not_modify_input(monkeypatch):
    item = card()
    original = dict(item)
    policy().apply([item])
    assert item == original


def test_rejected_batch_keeps_database_and_human_review(browser_app, monkeypatch):
    client, main = browser_app
    monkeypatch.setenv('CIAN_SEARCH_URL', URL)
    monkeypatch.setenv('CIAN_DISTRICT_POLICY', NAMES)
    payload = {'status': 'authorized', 'items': [card()]}
    monkeypatch.setattr(CianBrowserSource, 'request', lambda *a, **kw: payload)
    assert client.post('/api/browser/collect', json={}).status_code == 202
    first = finish(client, main)
    assert first['run']['status'] == 'done'
    with main.database() as conn:
        conn.execute("UPDATE listings SET realtor_status='approved', realtor_comment='Проверено риелтором' WHERE id='123'")
        before = conn.execute('SELECT * FROM listings WHERE id=?', ('123',)).fetchone()
        properties = conn.execute('SELECT * FROM properties').fetchall()
    age(main)
    payload['items'] = [card(id='333386132', address='Москва, р-н Капотня, 2-й квартал',
                             metro_time='10 мин. транспортом'), card(price=20000000)]
    assert client.post('/api/browser/collect', json={}).status_code == 202
    second = finish(client, main)
    assert second['run']['status'] == 'error'
    assert second['browser']['last_success'] == first['browser']['last_success']
    assert second['browser']['coverage']['admission']['rejected'] == 2
    with main.database() as conn:
        assert conn.execute('SELECT COUNT(*) FROM listings').fetchone()[0] == 1
        assert tuple(conn.execute('SELECT * FROM listings WHERE id=?', ('123',)).fetchone()) == tuple(before)
        assert [tuple(r) for r in conn.execute('SELECT * FROM properties')] == [tuple(r) for r in properties]
