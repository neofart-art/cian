"""Production-schema upgrades and Phase 2 contracts; no Cian requests."""
import importlib
import io
import json
import re
import sqlite3
import time
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app.auth import COOKIE, HASHER, digest
from app.criteria import browser_criteria
from app.finance import mortgage_payment
from app.csv_source import travel_duration
from tests.helpers import sign_in, login_attempt
from tests.test_multiuser import multi_app


def add_user(admin, role, password='12345678', username=None):
    return admin.post('/api/users', json={'username': username or role, 'display_name':'Имя ' + role,
                                          'password':password, 'role':role})


def test_upgrade_old_checks_preserves_all_data_indices_fks_and_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.setenv('APP_USER', 'ignored')
    monkeypatch.setenv('APP_PASSWORD', 'ignored-bootstrap-password')
    monkeypatch.setenv('BROWSER_SCHEDULE_ENABLED', 'false')
    monkeypatch.delenv('PUBLIC_URL', raising=False)
    filename = tmp_path / 'xata.sqlite3'
    with sqlite3.connect(filename) as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            PRAGMA foreign_keys=ON;
            CREATE TABLE users(id INTEGER PRIMARY KEY,username TEXT NOT NULL UNIQUE COLLATE NOCASE,display_name TEXT NOT NULL,
                password_hash TEXT NOT NULL,role TEXT NOT NULL CHECK(role IN ('admin','reviewer')),
                enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
            CREATE INDEX users_custom_name ON users(display_name);
            CREATE TABLE sessions(token_hash TEXT PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id),csrf_token TEXT NOT NULL,expires REAL NOT NULL);
            CREATE TABLE listings(id TEXT PRIMARY KEY,title TEXT,price INTEGER,address TEXT,url TEXT,first_seen TEXT,last_seen TEXT,
                complex_name TEXT,area REAL,floor TEXT,metro TEXT,metro_time TEXT,source TEXT DEFAULT 'ЦИАН online',last_imported TEXT,
                card_text TEXT,description TEXT,metro_walk_minutes INTEGER,
                review_status TEXT NOT NULL DEFAULT 'new' CHECK(review_status IN ('new','viewed','rejected')),
                review_updated_at TEXT,review_updated_by INTEGER REFERENCES users(id),review_version INTEGER NOT NULL DEFAULT 0,
                extra_column BLOB DEFAULT X'ABCD');
            CREATE INDEX listings_custom_price ON listings(price);
            CREATE INDEX listings_review_order ON listings(CASE review_status WHEN 'new' THEN 0 WHEN 'viewed' THEN 1 ELSE 2 END,first_seen DESC,id);
            CREATE TABLE audit(id TEXT);
            CREATE TRIGGER listing_insert AFTER INSERT ON listings BEGIN INSERT INTO audit VALUES (new.id); END;
            CREATE TABLE favorites(listing_id TEXT REFERENCES listings(id),user_id INTEGER REFERENCES users(id));
            CREATE TABLE runs(id INTEGER PRIMARY KEY,started TEXT,finished TEXT,status TEXT,message TEXT,filters TEXT,count INTEGER DEFAULT 0,source TEXT);
            CREATE TABLE browser_state(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL);
            CREATE TABLE import_previews(token TEXT PRIMARY KEY,created REAL NOT NULL,payload TEXT NOT NULL);
        """)
        conn.execute('INSERT INTO users VALUES (7,?,?,?,?,1,?,?)',
                     ('owner','Владелец',HASHER.hash('legacy-password-16'),'admin','2020','2021'))
        conn.execute('INSERT INTO users VALUES (8,?,?,?,?,1,?,?)',
                     ('family','Семья',HASHER.hash('legacy-family-pass'),'reviewer','2020','2021'))
        conn.execute('INSERT INTO sessions VALUES (?,?,?,?)', (digest('retained-session'),7,'retained-csrf',time.time()+3600))
        samples = [
            ('123','4 часа назад сегодня, 13:25','м. Сокол\n7 мин пешком','7 мин. пешком',7),
            ('456','вчера вечером, 19:14','Описание без маршрута',None,None),
            ('789','12 минут пешком','м. Сокол\n15 мин. пешком','12 минут пешком',12),
            ('888','2 часа назад','м. Сокол\n5 мин. транспортом','5 мин. транспортом',None),
            ('999','сегодня, 13:25','м. Сокол\nсегодня 7 минут назад',None,None),
        ]
        for item_id, metro, text, _, _ in samples:
            conn.execute("""INSERT INTO listings(id,title,price,address,url,first_seen,last_seen,area,floor,metro,metro_time,
                         source,last_imported,card_text,description,metro_walk_minutes,review_status,review_updated_at,review_updated_by,review_version)
                         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (item_id,'Квартира',12000000,'Москва','https://cian.ru/sale/flat/'+item_id+'/',
                          '2020-01-01T00:00:00+00:00','2022-02-02T00:00:00+00:00',34.7,'4/9','Сокол',metro,
                          'ЦИАН browser','2021',text,'Сохранено',12 if item_id=='789' else 99,'rejected','2025',8,17))
        conn.execute("INSERT INTO favorites VALUES ('123',8)")
        conn.execute("INSERT INTO runs VALUES (11,'2020','2021','done','Старый поиск','{}',5,'browser')")
        conn.execute('INSERT INTO browser_state VALUES (1,?)', (json.dumps({'status':'authorized','last_success':'2026-10-16T12:00:00+00:00','last_error':'private'}),))
        conn.execute("INSERT INTO import_previews VALUES ('retained-preview',1,'[]')")
        before = {row['id']:dict(row) for row in conn.execute('SELECT * FROM listings')}
        users_before = [dict(row) for row in conn.execute('SELECT * FROM users ORDER BY id')]
        sessions_before = [tuple(row) for row in conn.execute('SELECT * FROM sessions')]
    from app import main
    importlib.reload(main)
    with main.database() as conn:
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
        assert conn.execute('PRAGMA foreign_keys').fetchone()[0] == 1
        for item_id, _, _, expected, walk in samples:
            row = dict(conn.execute('SELECT * FROM listings WHERE id=?',(item_id,)).fetchone())
            for key,value in before[item_id].items():
                if key not in ('metro_time','metro_walk_minutes'):
                    assert row[key] == value, key
            assert row['metro_time'] == expected and row['metro_walk_minutes'] == walk
            assert row['realtor_status']=='pending' and row['realtor_comment']==''
            assert row['realtor_version']==0
        for old,new in zip(users_before,conn.execute('SELECT * FROM users ORDER BY id')):
            assert all(new[key] == value for key,value in old.items())
        assert [tuple(row) for row in conn.execute('SELECT * FROM sessions')] == sessions_before
        assert conn.execute('SELECT count(*) FROM favorites').fetchone()[0] == 1
        assert conn.execute('SELECT message FROM runs WHERE id=11').fetchone()[0]=='Старый поиск'
        assert conn.execute('SELECT token FROM import_previews').fetchone()[0]=='retained-preview'
        for name in ('users_custom_name','listings_custom_price','listings_review_order','listing_insert'):
            assert conn.execute('SELECT 1 FROM sqlite_master WHERE name=?',(name,)).fetchone()
        snapshot = [dict(row) for row in conn.execute('SELECT * FROM listings ORDER BY id')]
    with TestClient(main.app,base_url='https://testserver') as client:
        client.cookies.set(COOKIE,'retained-session')
        client.headers['X-CSRF-Token']='retained-csrf'
        assert client.get('/api/auth/me').status_code == 200
        assert client.post('/api/listings/123/review',json={'status':'liked'}).status_code==200
        assert add_user(client,'realtor').status_code==201
        # A legacy reviewer has unknown password length: no forced reset for
        # login, but promotion requires an explicit compliant admin password.
        assert client.patch('/api/users/8',json={'role':'admin'}).status_code==422
        assert client.patch('/api/users/8',json={'role':'admin','password':'admin-long-12'}).status_code==200
        with main.database() as conn:
            conn.execute("UPDATE listings SET review_status='rejected',review_updated_at='2025',review_updated_by=8,review_version=17 WHERE id='123'")
    importlib.reload(main)
    with main.database() as conn:
        assert [dict(row) for row in conn.execute('SELECT * FROM listings ORDER BY id')] == snapshot
        assert [tuple(row) for row in conn.execute('SELECT * FROM sessions')] == sessions_before
        main.save_items(conn,[{'id':'1000','price':10000000}], '2026')
        assert conn.execute("SELECT count(*) FROM audit WHERE id='1000'").fetchone()[0] == 1


def test_rebuild_failure_rolls_back_schema_and_rows(tmp_path):
    from app.migrations import transaction, expand_check
    @contextmanager
    def database():
        conn = sqlite3.connect(tmp_path/'rollback.sqlite3')
        conn.row_factory=sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        try:
            with conn:
                yield conn
        finally:
            conn.close()
    with database() as conn:
        conn.execute("CREATE TABLE users(id INTEGER PRIMARY KEY,role TEXT CHECK(role IN ('admin','reviewer')))")
        conn.execute("INSERT INTO users VALUES (1,'admin')")
    def fail(conn):
        expand_check(conn,'users','role',['admin','reviewer','realtor'])
        raise RuntimeError('injected failure')
    with pytest.raises(RuntimeError,match='injected'):
        transaction(database,fail)
    with database() as conn:
        assert conn.execute('SELECT * FROM users').fetchone()['role']=='admin'
        assert 'realtor' not in conn.execute("SELECT sql FROM sqlite_master WHERE name='users'").fetchone()[0]
        assert conn.execute('PRAGMA foreign_keys').fetchone()[0] == 1


@pytest.mark.parametrize('value,expected,walk',[
    ('7 мин пешком','7 мин. пешком',7),('12 минут пешком','12 мин. пешком',12),
    ('5 мин. транспортом','5 мин. транспортом',None),('8 минут на транспорте','8 мин. транспортом',None),
    ('4 часа назад сегодня, 13:25',None,None),('вчера вечером',None,None),('7 минут назад',None,None),
    ('сегодня',None,None),('7 мин.',None,None),('0 мин. пешком',None,None),
])
def test_metro_duration(value,expected,walk):
    result = travel_duration(value)
    if expected is None:
        assert result is None
    else:
        assert result == {'metro_time':expected,'metro_walk_minutes':walk}


@pytest.mark.parametrize('role,password,expected',[
    ('reviewer','12345678',201),('realtor','12345678',201),('reviewer','1234567',422),
    ('realtor','1234567',422),('admin','12345678901',422),('admin','123456789012',201),
])
def test_role_password_minimums(multi_app,role,password,expected):
    main,admin=multi_app
    result = add_user(admin,role,password)
    assert result.status_code==expected
    if expected==201:
        with TestClient(main.app,base_url='https://testserver') as client:
            assert login_attempt(client,role,password).status_code==200
        path='/api/users/'+str(result.json()['id'])
        assert admin.patch(path,json={'password':'1234567'}).status_code==422
        assert admin.patch(path,json={'password':password}).status_code==200


@pytest.mark.parametrize('role',['reviewer','realtor'])
def test_password_promotion_and_effective_role(multi_app,role):
    main,admin=multi_app
    user=add_user(admin,role).json()
    path='/api/users/'+str(user['id'])
    assert admin.patch(path,json={'role':'admin'}).status_code==422
    assert admin.patch(path,json={'role':'admin','password':'12345678901'}).status_code==422
    assert admin.patch(path,json={'role':'admin','password':'123456789012'}).status_code==200
    assert admin.patch(path,json={'password':'12345678'}).status_code==422
    assert admin.patch(path,json={'role':role,'password':'12345678'}).status_code==200


def test_bootstrap_minimum_unchanged(tmp_path,monkeypatch):
    monkeypatch.setenv('DATA_DIR',str(tmp_path))
    monkeypatch.setenv('APP_USER','owner')
    monkeypatch.setenv('APP_PASSWORD','123456789012345')
    monkeypatch.setenv('BROWSER_SCHEDULE_ENABLED','false')
    from app import main
    importlib.reload(main)
    with main.database() as conn:
        assert conn.execute('SELECT count(*) FROM users').fetchone()[0]==0
    monkeypatch.setenv('APP_PASSWORD','1234567890123456')
    importlib.reload(main)
    with main.database() as conn:
        assert conn.execute('SELECT role FROM users').fetchone()[0]=='admin'


def test_realtor_roles_independent_statuses_and_refresh_xlsx(multi_app,caplog):
    main,admin=multi_app
    realtor=add_user(admin,'realtor').json()
    reviewer=add_user(admin,'reviewer').json()
    secret_comment='<script>alert("x")</script> = плохой подъезд\nUTF-8: ёж'
    with TestClient(main.app,base_url='https://testserver') as agent, TestClient(main.app,base_url='https://testserver') as family:
        sign_in(agent,'realtor','12345678')
        sign_in(family,'reviewer','12345678')
        main.update_browser(last_success='2026-10-16T12:00:00+00:00',last_error='PRIVATE_BROWSER_ERROR')
        for client in (agent,family):
            state=client.get('/api/state').json()
            assert state['last_success']=='2026-10-16T12:00:00+00:00'
            assert 'browser' not in state and 'run' not in state
            assert 'PRIVATE_BROWSER_ERROR' not in json.dumps(state)
        assert family.post('/api/listings/123/review',json={'status':'liked'}).status_code==200
        assert agent.post('/api/listings/123/review',json={'status':'viewed'}).status_code==403
        assert family.post('/api/listings/123/realtor-review',json={'status':'approved'}).status_code==403
        saved=agent.post('/api/listings/123/realtor-review',json={'status':'rejected','comment':secret_comment}).json()
        assert saved['review_status']=='liked' and saved['realtor_status']=='rejected'
        assert saved['realtor_updated_by']==realtor['id']
        assert saved['realtor_updated_by_name']=='Имя realtor'
        assert saved['realtor_updated_at'].endswith('+00:00') and saved['realtor_version']==1
        for path in ('/api/users','/api/browser/status','/api/export.xlsx'):
            assert agent.get(path).status_code==403
        for path,payload in [('/api/browser/collect',{}),('/api/collect',{}),('/api/import/preview',{}),
                             ('/api/import/confirm',{'token':'x'*32}),('/api/import/cancel',{'token':'x'*32}),
                             ('/api/users',{})]:
            assert agent.post(path,json=payload).status_code==403
        assert agent.patch('/api/users/1',json={'enabled':False}).status_code==403
        assert agent.post('/api/listings/missing/realtor-review',json={'status':'pending'}).status_code==404
        assert agent.post('/api/listings/123/realtor-review',json={'status':'bad'}).status_code==422
        assert agent.post('/api/listings/123/realtor-review',json={'status':'approved','comment':'bad\x00text'}).status_code==422
        assert agent.post('/api/listings/123/realtor-review',json={'status':'approved','comment':'x'*2001}).status_code==422
        for headers in ({'X-CSRF-Token':'bad'},{'Origin':'https://evil.test'},{'Sec-Fetch-Site':'cross-site'}):
            assert agent.post('/api/listings/123/realtor-review',json={'status':'approved'},headers=headers).status_code==403
        with main.database() as conn:
            main.save_items(conn,[{'id':'123','price':13000000,'source':'ЦИАН browser'}],'2026')
        preview=admin.post('/api/import/preview',content=b'url;price\nhttps://cian.ru/sale/flat/123/;12500000').json()
        assert admin.post('/api/import/confirm',json={'token':preview['token']}).status_code==200
        row=next(x for x in family.get('/api/state').json()['listings'] if x['id']=='123')
        for key in ('review_status','review_version','first_seen','realtor_status','realtor_comment','realtor_updated_at','realtor_updated_by','realtor_version'):
            assert row[key]==saved[key]
        assert row['mortgage_payment']==pytest.approx(mortgage_payment(12500000))
        assert admin.post('/api/listings/123/review',json={'status':'viewed'}).status_code==200
        assert admin.post('/api/listings/456/realtor-review',json={'status':'approved','comment':'=SUM(1,2)'}).status_code==200
        book=load_workbook(io.BytesIO(admin.get('/api/export.xlsx').content))
        data=list(book.active.values)
        exported=dict(zip(data[0],next(row for row in data[1:] if row[0]=='456')))
        assert exported['Оценка риелтора']=='approved'
        assert exported['Комментарий риелтора']=="'=SUM(1,2)"
        assert exported['Ипотека / мес. (6 млн / 18% / 15 лет)']==pytest.approx(mortgage_payment(12000000))
        assert admin.post('/api/listings/123/realtor-review',json={'status':'pending','comment':''}).json()['realtor_comment']==''
        # Session revocation applies equally to the third role.
        assert admin.patch('/api/users/'+str(realtor['id']),json={'enabled':False}).status_code==200
        assert agent.get('/api/state').status_code==401
    assert secret_comment not in caplog.text


@pytest.mark.parametrize('price,expected',[(None,None),(0,0),(6000000,0),(5000000,0),(12000000,96625.26236476943)])
def test_mortgage(price,expected):
    result=mortgage_payment(price)
    assert result is None if expected is None else result==pytest.approx(expected,rel=1e-6)


@pytest.mark.parametrize('query,expected',[
    ('',{'max_price':14500000,'metro_minutes':20}),
    ('maxprice=14500000&foot_min=20',{'max_price':14500000,'metro_minutes':20}),
    ('maxprice=15000000&foot_min=15',{'max_price':15000000,'metro_minutes':15}),
    ('maxprice=bad&foot_min=0',{'max_price':14500000,'metro_minutes':20}),
    ('maxprice=10000000&maxprice=14000000&foot_min=999',{'max_price':14500000,'metro_minutes':20}),
])
def test_configured_criteria(monkeypatch,query,expected):
    monkeypatch.setenv('CIAN_SEARCH_URL','https://www.cian.ru/cat.php?'+query)
    assert browser_criteria()==expected


def test_manual_and_scheduler_use_server_criteria_not_legacy_filters(multi_app,monkeypatch):
    main,admin=multi_app
    monkeypatch.setenv('CIAN_SEARCH_URL','https://www.cian.ru/cat.php?maxprice=14500000&foot_min=20')
    calls=[]
    class NoThread:
        def __init__(self,target,args,**kwargs): self.args=args
        def start(self): calls.append(self.args)
    monkeypatch.setattr(main.threading,'Thread',NoThread)
    assert admin.post('/api/browser/collect',json={'max_price':14000000,'metro_minutes':10}).status_code==422
    assert admin.post('/api/browser/collect',json={}).status_code==202
    assert calls[-1][1]=={'max_price':14500000,'metro_minutes':20}
    main.lock.release()  # Fake worker does not run its finally.
    with main.database() as conn:
        conn.execute("UPDATE runs SET started='2000-01-01T00:00:00+00:00',filters=?,status='done',source='browser'",
                     (json.dumps({'max_price':14000000,'metro_minutes':10}),))
    monkeypatch.setattr(main.CianBrowserSource,'status',lambda self:{'status':'authorized','message':'Авторизован'})
    main.scheduler_tick()
    assert calls[-1][1]=={'max_price':14500000,'metro_minutes':20}
    main.lock.release()
    state=admin.get('/api/state').json()
    assert state['criteria']=={'max_price':14500000,'metro_minutes':20}
    assert state['run']['filters']==json.dumps(state['criteria'],separators=(',',':'))


def test_versioned_assets_and_readonly_criteria(multi_app):
    main,admin=multi_app
    import hashlib
    from pathlib import Path
    html=admin.get('/')
    assert html.headers['cache-control']=='no-store'
    for asset in ('app.js','review-state.js','style.css'):
        match=re.search('/static/'+re.escape(asset)+r'\?v=([a-f0-9]{16})',html.text)
        assert match
        assert match[1]==hashlib.sha256((Path(main.__file__).parent/'static'/asset).read_bytes()).hexdigest()[:16]
        response=admin.get(match[0])
        assert response.status_code==200 and 'no-cache' in response.headers['cache-control']
        assert 'must-revalidate' in response.headers['cache-control']
        assert 'etag' in response.headers
    assert 'id="price"' not in html.text and 'id="metro"' not in html.text
    assert 'Критерии поиска' in html.text and 'Сравниваем варианты. Выбираем лучшее.' in html.text
    with TestClient(main.app,base_url='https://testserver') as client:
        assert re.search(r'/static/login.js\?v=[a-f0-9]{16}',client.get('/login').text)



@pytest.mark.parametrize('explicit,expected,walk',[
    ('4 часа назад сегодня, 13:25','7 мин. пешком',7),
    ('5 мин. транспортом','5 мин. транспортом',None),
    ('12 минут пешком','12 мин. пешком',12),
])
def test_csv_explicit_duration_does_not_retain_wrong_walking_minutes(explicit,expected,walk):
    import csv
    from app.csv_source import CianCsvSource
    output=io.StringIO()
    writer=csv.writer(output,delimiter=';')
    writer.writerow(['url','card_text','metro_time'])
    writer.writerow(['https://cian.ru/sale/flat/123/','м. Сокол\n7 мин пешком',explicit])
    item=CianCsvSource().parse(output.getvalue().encode('utf-8'))['items'][0]
    assert item['metro_time']==expected
    assert item.get('metro_walk_minutes')==walk
