import importlib
import io
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app.auth import COOKIE, HASHER, digest
from tests.helpers import sign_in, login_attempt


@pytest.fixture
def multi_app(tmp_path, monkeypatch):
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.setenv('APP_USER', 'owner')
    monkeypatch.setenv('APP_PASSWORD', 'long-test-password')
    monkeypatch.setenv('BROWSER_SCHEDULE_ENABLED', 'false')
    monkeypatch.delenv('PUBLIC_URL', raising=False)
    with sqlite3.connect(tmp_path / 'xata.sqlite3') as conn:
        conn.execute('CREATE TABLE listings (id TEXT PRIMARY KEY,title TEXT,price INTEGER,address TEXT,url TEXT,first_seen TEXT,last_seen TEXT)')
        for item_id, first in [('123','2020-01-01T00:00:00+00:00'), ('456','2021-01-01T00:00:00+00:00')]:
            conn.execute('INSERT INTO listings VALUES (?,?,?,?,?,?,?)',
                         (item_id, 'Квартира ' + item_id, 12000000, 'Москва', 'https://cian.ru/sale/flat/' + item_id + '/', first, first))
        conn.execute('CREATE TABLE runs (id INTEGER PRIMARY KEY,started TEXT,finished TEXT,status TEXT,message TEXT,filters TEXT,count INTEGER DEFAULT 0)')
        conn.execute("INSERT INTO runs VALUES (1,'2020','2020','done','История','{}',2)")
        conn.execute('CREATE TABLE browser_state (id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL)')
        conn.execute('INSERT INTO browser_state VALUES (1,?)', (json.dumps({'status':'authorized','last_success':'2022','new_count':2}),))
    from app import main
    importlib.reload(main)
    with TestClient(main.app, base_url='https://testserver') as admin:
        sign_in(admin)
        yield main, admin


def add_reviewer(admin, name='agent'):
    result = admin.post('/api/users', json={'username':name, 'display_name':'Риелтор',
                                           'password':'reviewer-password-16', 'role':'reviewer'})
    assert result.status_code == 201, result.text
    return result.json()


def reviewer_client(main, admin):
    user = add_reviewer(admin)
    client = TestClient(main.app, base_url='https://testserver')
    sign_in(client, 'agent', 'reviewer-password-16')
    return user, client


def test_additive_bootstrap_and_preserved_production_data(multi_app, monkeypatch):
    main, admin = multi_app
    with main.database() as conn:
        owner = dict(conn.execute('SELECT * FROM users').fetchone())
        assert owner['id'] == 1 and owner['role'] == 'admin' and owner['enabled']
        assert owner['password_hash'].startswith('$argon2id$')
        assert owner['password_hash'] != 'long-test-password'
        assert HASHER.verify(owner['password_hash'], 'long-test-password')
        before = [dict(row) for row in conn.execute('SELECT * FROM listings ORDER BY id')]
        assert all(row['review_status'] == 'new' and row['review_version'] == 0 for row in before)
        assert before[0]['first_seen'] == '2020-01-01T00:00:00+00:00'
        assert before[0]['last_seen'] == before[0]['first_seen']
        assert conn.execute('SELECT message FROM runs WHERE id=1').fetchone()[0] == 'История'
        assert main.browser_state()['last_success'] == '2022'
    # Env credentials bootstrap once; they never reset an existing SQLite user.
    monkeypatch.setenv('APP_PASSWORD', 'different-env-password')
    importlib.reload(main)
    with main.database() as conn:
        assert conn.execute('SELECT count(*) FROM users').fetchone()[0] == 1
        assert conn.execute('SELECT password_hash FROM users').fetchone()[0] == owner['password_hash']
        assert [dict(row) for row in conn.execute('SELECT * FROM listings ORDER BY id')] == before
    assert admin.get('/api/auth/me').status_code == 200
    with TestClient(main.app, base_url='https://testserver') as other:
        assert login_attempt(other, password='different-env-password').status_code == 401
        assert login_attempt(other).status_code == 200


def test_login_cookies_logout_expiry_and_basic_removed(multi_app):
    main, admin = multi_app
    with TestClient(main.app, base_url='https://testserver') as client:
        assert client.get('/', follow_redirects=False).headers['location'] == '/login'
        assert client.get('/login').status_code == 200
        assert client.get('/api/state', auth=('owner', 'long-test-password')).status_code == 401
        assert 'www-authenticate' not in client.get('/api/state').headers
        assert login_attempt(client, password='incorrect').status_code == 401
        response = login_attempt(client)
        assert response.status_code == 200
        cookie = next(value for value in response.headers.get_list('set-cookie') if value.startswith(COOKIE + '='))
        assert 'httponly' in cookie.lower() and 'secure' in cookie.lower() and 'samesite=lax' in cookie.lower()
        raw = client.cookies.get(COOKIE)
        with main.database() as conn:
            row = conn.execute('SELECT * FROM sessions WHERE token_hash=?', (digest(raw),)).fetchone()
            assert row and row['token_hash'] != raw
        assert client.get('/api/auth/me').json()['user']['role'] == 'admin'
        assert 'password_hash' not in response.text
        assert client.post('/api/auth/logout').status_code == 200
        assert client.get('/api/state', headers={'Cookie':COOKIE + '=' + raw}).status_code == 401
        sign_in(client)
        with main.database() as conn:
            conn.execute('UPDATE sessions SET expires=0 WHERE token_hash=?', (digest(client.cookies.get(COOKIE)),))
        assert client.get('/api/auth/me').status_code == 401


def test_roles_and_administrative_api_enforcement(multi_app, monkeypatch):
    main, admin = multi_app
    user, reviewer = reviewer_client(main, admin)
    def never(*args, **kwargs):
        raise AssertionError('Denied reviewer request must not reach browser')
    monkeypatch.setattr(main.CianBrowserSource, 'collect', never)
    monkeypatch.setattr(main.CianBrowserSource, 'status', never)
    state = reviewer.get('/api/state').json()
    assert len(state['listings']) == 2
    assert 'browser' not in state and 'run' not in state
    for path in ('/api/users','/api/browser/status','/api/export.xlsx'):
        assert reviewer.get(path).status_code == 403
    for path, payload in [
        ('/api/browser/collect', {}), ('/api/collect', {}),
        ('/api/import/preview', {}), ('/api/import/confirm', {'token':'x'*32}),
        ('/api/import/cancel', {'token':'x'*32}),
        ('/api/users', {'username':'evil','display_name':'Evil','password':'password-123456','role':'admin'}),
    ]:
        assert reviewer.post(path, json=payload).status_code == 403
    assert reviewer.patch('/api/users/1', json={'enabled':False}).status_code == 403
    assert admin.get('/api/users').status_code == 200
    assert admin.get('/api/export.xlsx').status_code == 200
    assert 'password_hash' not in admin.get('/api/users').text
    # Role checks use current SQLite data, rather than a stale role saved in the cookie.
    assert admin.patch('/api/users/' + str(user['id']), json={'role':'admin'}).status_code == 200
    assert reviewer.get('/api/users').status_code == 200
    assert admin.patch('/api/users/' + str(user['id']), json={'role':'reviewer'}).status_code == 200
    assert reviewer.get('/api/users').status_code == 403


@pytest.mark.parametrize('status', ['new','viewed','rejected'])
def test_reviewer_status_author_and_time(multi_app, status):
    main, admin = multi_app
    user, reviewer = reviewer_client(main, admin)
    response = reviewer.post('/api/listings/123/review', json={'status':status})
    assert response.status_code == 200
    item = response.json()
    assert item['review_status'] == status
    assert item['review_updated_by'] == user['id']
    assert item['review_updated_by_name'] == 'Риелтор'
    assert item['review_updated_at'].endswith('+00:00')
    assert item['review_version'] == 1
    assert item['first_seen'] == item['last_seen'] == '2020-01-01T00:00:00+00:00'
    same = next(item for item in reviewer.get('/api/state').json()['listings'] if item['id'] == '123')
    assert same['review_updated_at'] == item['review_updated_at']
    assert reviewer.post('/api/listings/123/review', json={'status':'sold'}).status_code == 422
    assert reviewer.post('/api/listings/missing/review', json={'status':'viewed'}).status_code == 404


def test_browser_csv_refresh_keeps_review_and_xlsx_history(multi_app):
    main, admin = multi_app
    user, reviewer = reviewer_client(main, admin)
    review = reviewer.post('/api/listings/123/review', json={'status':'rejected'}).json()
    with main.database() as conn:
        conn.execute('BEGIN IMMEDIATE')
        main.save_items(conn, [{'id':'123','price':11000000,'source':'ЦИАН browser'},
                               {'id':'789','title':'Новое','url':'https://cian.ru/sale/flat/789/'}], '2026-10-16T10:00:00+00:00')
    preview = admin.post('/api/import/preview', content=b'url;price\nhttps://cian.ru/sale/flat/123/;10500000').json()
    assert admin.post('/api/import/confirm', json={'token':preview['token']}).status_code == 200
    assert admin.patch('/api/users/' + str(user['id']), json={'username':'agent2','display_name':'=Имя после переименования'}).status_code == 200
    state = admin.get('/api/state').json()['listings']
    assert [item['id'] for item in state] == ['789','456','123']
    item = state[-1]
    for key in ('review_status','review_updated_at','review_updated_by','review_version','first_seen'):
        assert item[key] == review[key]
    assert item['price'] == 10500000
    assert item['source'] == 'ЦИАН CSV' and item['last_imported']
    assert item['review_updated_by_name'] == '=Имя после переименования'
    assert state[0]['review_status'] == 'new' and state[0]['review_updated_by'] is None
    book = load_workbook(io.BytesIO(admin.get('/api/export.xlsx').content))
    rows = list(book.active.values)
    assert rows[0][-3:] == ('Статус','Статус изменил','Дата изменения статуса (UTC)')
    saved = next(row for row in rows[1:] if row[0] == '123')
    assert saved[-3:] == ('rejected', "'=Имя после переименования", review['review_updated_at'])


def test_disabling_password_reset_and_last_active_admin(multi_app):
    main, admin = multi_app
    user, reviewer = reviewer_client(main, admin)
    path = '/api/users/' + str(user['id'])
    assert admin.patch('/api/users/1', json={'enabled':False}).status_code == 409
    assert admin.patch('/api/users/1', json={'role':'reviewer'}).status_code == 409
    assert admin.patch(path, json={'enabled':False}).status_code == 200
    assert reviewer.get('/api/state').status_code == 401
    assert login_attempt(reviewer, 'agent', 'reviewer-password-16').status_code == 401
    assert admin.patch(path, json={'enabled':True}).status_code == 200
    sign_in(reviewer, 'agent', 'reviewer-password-16')
    assert admin.patch(path, json={'password':'new-reviewer-password'}).status_code == 200
    assert reviewer.get('/api/state').status_code == 401
    assert login_attempt(reviewer, 'agent', 'reviewer-password-16').status_code == 401
    sign_in(reviewer, 'agent', 'new-reviewer-password')
    assert reviewer.get('/api/state').status_code == 200
    assert admin.post('/api/users', json={'username':'OWNER','display_name':'Duplicate','password':'valid-password-16'}).status_code == 409
    response = admin.post('/api/users', json={'username':'weak','display_name':'Weak','password':'DO_NOT_ECHO'})
    assert response.status_code == 422 and 'DO_NOT_ECHO' not in response.text
    assert admin.patch(path, json={'password_hash':'evil'}).status_code == 422


def test_csrf_login_replay_origin_and_security_headers(multi_app, monkeypatch):
    main, admin = multi_app
    for headers in ({'X-CSRF-Token':''}, {'X-CSRF-Token':'incorrect'}, {'Origin':'https://evil.test'}, {'Sec-Fetch-Site':'cross-site'}):
        response = admin.post('/api/listings/123/review', json={'status':'viewed'}, headers=headers)
        assert response.status_code == 403
        assert response.headers['x-content-type-options'] == 'nosniff'
        assert "frame-ancestors 'none'" in response.headers['content-security-policy']
    assert admin.patch('/api/users/1', json={'display_name':'Wrong'}, headers={'Origin':'https://evil.test'}).status_code == 403
    with TestClient(main.app, base_url='https://testserver') as client:
        assert client.post('/api/auth/login', json={'username':'owner','password':'long-test-password'}).status_code == 403
        challenge = client.get('/api/auth/csrf').json()['csrf_token']
        args = {'json':{'username':'owner','password':'long-test-password'}, 'headers':{'X-CSRF-Token':challenge}}
        assert client.post('/api/auth/login', **args).status_code == 200
        assert client.post('/api/auth/login', **args).status_code == 403
    assert admin.post('/api/listings/123/review', json={'status':'viewed'}).status_code == 200


def test_login_rate_limit_and_logs(multi_app, caplog):
    main, admin = multi_app
    from app.auth import digest
    with main.database() as conn:
        import time
        conn.execute('INSERT OR REPLACE INTO login_attempts VALUES (?,20,?)', (digest('testclient'), time.time()))
    with TestClient(main.app, base_url='https://testserver') as client:
        assert login_attempt(client).status_code == 429
    assert 'long-test-password' not in caplog.text
    assert '$argon2' not in caplog.text
