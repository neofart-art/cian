import csv
import importlib
import io
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from app import source
from app.csv_source import CianCsvSource, parse_card_text, MAX_BYTES

CARD = '1-комн. квартира, 34,7 м², 4/9 этаж\nЖК «Тест»\nМосква, улица Тестовая, 1\nм. Сокол\n15 мин. пешком\nОписание:\nСветлая квартира; с кухней.'
URL = 'https://www.cian.ru/sale/flat/123/?from=favorites'


def csv_file(rows=None, delimiter=';', encoding='utf-8-sig'):
    output = io.StringIO(newline='')
    writer = csv.writer(output, delimiter=delimiter)
    writer.writerow(['url', 'title', 'price', 'area', 'card_text'])
    writer.writerows(rows or [[URL, '', '11 950 000 ₽', '34,70 м²', CARD]])
    text = output.getvalue()
    if encoding == 'cp1251':
        text = text.replace('₽', 'руб.').replace('²', '2')
    return text.encode(encoding)


@pytest.mark.parametrize('encoding,delimiter', [('utf-8-sig', ';'), ('cp1251', ';'), ('utf-16', '\t'), ('utf-8', ',')])
def test_exact_csv(encoding, delimiter):
    result = CianCsvSource().parse(csv_file(encoding=encoding, delimiter=delimiter))
    item = result['items'][0]
    assert result['found'] == 1
    assert item['id'] == '123'
    assert item['url'] == URL
    assert item['price'] == 11950000
    assert item['area'] == 34.7
    assert item['floor'] == '4/9'
    assert item['metro'] == 'Сокол'
    assert item['metro_walk_minutes'] == 15
    assert item['address'] == 'Москва, улица Тестовая, 1'
    assert item['complex_name'] == 'Тест'
    assert item['card_text'] == (CARD.replace('²', '2') if encoding == 'cp1251' else CARD)
    assert item['description'] == 'Светлая квартира; с кухней.'


def test_duplicate_and_non_walking():
    result = CianCsvSource().parse(csv_file([[URL, 'Первый', '12000000', '34,7', CARD],
                                          ['https://cian.ru/sale/flat/123/', '', '11950000', '', '']]))
    assert result['found'] == 2 and result['duplicates'] == 1
    assert result['items'][0]['title'] == 'Первый'
    assert result['items'][0]['area'] == 34.7
    assert result['items'][0]['price'] == 11950000
    assert 'metro_walk_minutes' not in parse_card_text('м. Сокол\n7 мин. транспортом')
    assert 'metro_walk_minutes' not in parse_card_text('м. Сокол\n7 мин.')
    assert 'metro_walk_minutes' not in parse_card_text('м. Сокол\nОписание:\nДо школы 7 мин. пешком')


@pytest.mark.parametrize('content,reason', [
    (b'title;price\nflat;12', 'Нет столбца'),
    (b'url;price\n', 'нет объявлений'),
    (b'url;price\nhttps://evil.test/123;12000', 'Строка 2'),
    (b'url;price\nhttps://cian.ru/sale/flat/1/;abc', 'Цена'),
    (b'url;price\nhttps://cian.ru/sale/flat/1/;10;extra', 'количество полей'),
    (b'url;URL\nhttps://cian.ru/sale/flat/1/;x', 'Дублируется'),
    (b'x' * (MAX_BYTES + 1), '5 МБ'),
    (b'url;price\n"unclosed;1', 'Ошибка структуры'),
])
def test_invalid_csv(content, reason):
    with pytest.raises(source.SourceError, match=reason):
        CianCsvSource().parse(content)


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.setenv('APP_USER', 'owner')
    monkeypatch.setenv('APP_PASSWORD', 'long-test-password')
    # Original production schema with one record: validate additive migration.
    with sqlite3.connect(tmp_path / 'xata.sqlite3') as conn:
        conn.execute('CREATE TABLE listings (id TEXT PRIMARY KEY,title TEXT,price INTEGER,address TEXT,url TEXT,first_seen TEXT,last_seen TEXT)')
        conn.execute('INSERT INTO listings VALUES (?,?,?,?,?,?,?)', ('123', 'Старый заголовок', 14000000, '', URL, '2020-01-01T00:00:00+00:00', '2020-01-01T00:00:00+00:00'))
        conn.execute('INSERT INTO listings VALUES (?,?,?,?,?,?,?)', ('999', 'Не удалять', 12000000, '', 'https://cian.ru/sale/flat/999/', '2020', '2020'))
    from app import main
    importlib.reload(main)
    with TestClient(main.app) as client:
        client.auth = ('owner', 'long-test-password')
        yield client, main


def test_import_preview_confirmation_updates_and_export(app_client, monkeypatch):
    client, main = app_client
    def no_network(*args, **kwargs):
        raise AssertionError('CSV must never contact Cian')
    monkeypatch.setattr(source.httpx.HTTPTransport, 'handle_request', no_network)
    assert client.get('/api/state').json()['listings'][0]['source'] == 'ЦИАН online'
    rows = [[URL, '', '11 950 000 ₽', '34,70 м²', CARD],
            ['http://cian.ru/sale/flat/456/?from=csv', '=CSV title', '12500000', '35', 'м. Сокол\n7 мин. транспортом']]
    preview = client.post('/api/import/preview', content=csv_file(rows)).json()
    assert (preview['found'], preview['added'], preview['updated']) == (2, 1, 1)
    assert len(client.get('/api/state').json()['listings']) == 2
    result = client.post('/api/import/confirm', json={'token': preview['token']})
    assert result.status_code == 200
    assert result.json()['added'] == 1 and result.json()['updated'] == 1
    listings = {row['id']: row for row in client.get('/api/state').json()['listings']}
    assert len(listings) == 3 and '999' in listings
    item = listings['123']
    assert item['first_seen'] == '2020-01-01T00:00:00+00:00'
    assert item['title'] == 'Старый заголовок'
    assert item['price'] == 11950000
    assert item['last_imported'] == item['last_seen']
    assert item['source'] == 'ЦИАН CSV' and item['url'] == URL
    assert listings['456']['metro_time'] == '7 мин. транспортом'
    assert listings['456']['metro_walk_minutes'] is None
    assert client.post('/api/import/confirm', json={'token': preview['token']}).status_code == 409
    preview2 = client.post('/api/import/preview', content=csv_file(rows)).json()
    assert preview2['added'] == 0 and preview2['updated'] == 2
    assert client.post('/api/import/confirm', json={'token': preview2['token']}).status_code == 200
    assert len(client.get('/api/state').json()['listings']) == 3
    book = load_workbook(io.BytesIO(client.get('/api/export.xlsx').content))
    data = list(book.active.values)
    assert 'Текст карточки' in data[0]
    assert any(row[1] == "'=CSV title" for row in data[1:])
    assert any('ЦИАН CSV' in row for row in data[1:])
    # Migration/startup is repeatable and keeps imported data.
    importlib.reload(main)
    assert len(client.get('/api/state').json()['listings']) == 3


def test_invalid_expired_unauthorized_and_atomic_import(app_client):
    client, main = app_client
    assert client.post('/api/import/preview', content=b'bad').status_code == 422
    assert client.post('/api/import/preview', content=csv_file(), headers={'Origin': 'https://evil.test'}).status_code == 403
    assert client.post('/api/import/preview', content=csv_file(), auth=('bad', 'bad')).status_code == 401
    malformed = csv_file([[URL, '', '100', '', ''], ['https://evil.test/', '', '1', '', '']])
    assert client.post('/api/import/preview', content=malformed).status_code == 422
    assert client.get('/api/state').json()['listings'][0]['price'] == 14000000
    preview = client.post('/api/import/preview', content=csv_file()).json()
    with main.database() as conn:
        conn.execute('UPDATE import_previews SET created=0')
    assert client.post('/api/import/confirm', json={'token': preview['token']}).status_code == 409
    assert len(client.get('/api/state').json()['listings']) == 2


def test_captcha_is_expected_state(app_client, monkeypatch):
    client, main = app_client
    def blocked(filters):
        raise source.SourceBlocked('Циан запросил CAPTCHA. Можно импортировать CSV.')
    monkeypatch.setattr(source, 'collect', blocked)
    assert client.post('/api/collect', json={}).status_code == 202
    for _ in range(100):
        state = client.get('/api/state').json()
        if state['run']['status'] != 'running':
            break
        time.sleep(.01)
    assert state['run']['status'] == 'blocked'
    assert client.get('/healthz').json() == {'status': 'ok'}
    assert client.post('/api/import/preview', content=csv_file()).status_code == 200


def test_inline_card_and_explicit_transport_update(app_client):
    client, main = app_client
    inline = parse_card_text('1-комн. квартира, 34,7 м², 5/12 этаж Москва, улица Тестовая, 1 м. Сокол 15 мин. пешком')
    assert inline['floor'] == '5/12'
    assert inline['metro'] == 'Сокол'
    assert inline['address'] == 'Москва, улица Тестовая, 1'
    assert inline['metro_walk_minutes'] == 15
    first = client.post('/api/import/preview', content=csv_file()).json()
    client.post('/api/import/confirm', json={'token': first['token']})
    transport = csv_file([[URL, '', '', '', 'м. Сокол\n7 мин. транспортом']])
    second = client.post('/api/import/preview', content=transport).json()
    client.post('/api/import/confirm', json={'token': second['token']})
    row = next(row for row in client.get('/api/state').json()['listings'] if row['id'] == '123')
    assert row['metro_time'] == '7 мин. транспортом' and row['metro_walk_minutes'] is None
    assert row['price'] == 11950000 and row['area'] == 34.7
    third = client.post('/api/import/preview', content=csv_file()).json()
    assert client.post('/api/import/cancel', json={'token': third['token']}).status_code == 200
    assert client.post('/api/import/confirm', json={'token': third['token']}).status_code == 409
