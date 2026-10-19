"""Safety review regressions for Phase 3; synthetic data only."""
import importlib
import io
import json
import sqlite3
import time
import xml.etree.ElementTree as ET
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app.auth import COOKIE, HASHER, digest
from app.property_match import listing_match, best_match, merge_properties
from tests.helpers import sign_in
from tests.test_multiuser import multi_app
from tests.test_phase3 import sample, decide


def saved(main, *items, now='2026-10-18T10:00:00+00:00'):
    with main.database() as conn:
        return main.save_items(conn,items,now)


def property_id(main, listing):
    with main.database() as conn:
        return conn.execute('SELECT property_id FROM listings WHERE id=?',(str(listing),)).fetchone()[0]


def pending(admin, listing):
    return next(row for row in admin.get('/api/state').json()['listings'] if str(listing) in row['listing_ids'])['duplicate_candidate']


def payload(candidate, decision='same'):
    return {'decision':decision,'candidate_id':candidate['id'],'source_property_id':candidate['source_property_id'],
            'candidate_property_id':candidate['candidate_property_id']}


def offer_candidate(main, source, target):
    with main.database() as conn:
        conn.execute('''INSERT OR IGNORE INTO duplicate_candidates(listing_id,candidate_property_id,confidence,reasons,created_at)
                        VALUES (?,?,70,'fixture','2026-10-19')''',(str(source),property_id(main,target)))


@pytest.mark.parametrize('address',[
    'Москва, Ленинградский проспект, д. 11, корп. 1',
    'Москва, Ленинградский проспект, д. 10, корп. 2',
    'Москва, Ленинградский проспект, д. 10/2, корп. 1',
    'Москва, Ленинградский проспект, д. 10, корп. 1, стр. 2',
])
def test_house_corpus_structure_are_not_fuzzy_identity(address):
    old=sample('1',address='Москва, Ленинградский проспект, д. 10/1, корп. 1, стр. 1')
    result=listing_match(sample('2',address=address),old)
    assert not result.auto


@pytest.mark.parametrize('left,right',[
    ('Москва, Очень длинная улица Академика Петрова, 110','Москва, Очень длинная улица Академика Петрова, 111'),
    ('Москва, Ленинградский проспект, д. 10/1','Москва, Ленинградский проспект, д. 10/2'),
    ('Москва, Ленинградский проспект, д. 10, кв. 31','Москва, Ленинградский проспект, д. 10, кв. 32'),
])
def test_explicit_different_house_or_apartment_is_excluded(left,right):
    assert listing_match(sample('1',address=left),sample('2',address=right)).score==0


@pytest.mark.parametrize('description',[
    'Светлая квартира, большая кухня, окна во двор, один собственник.',
    '1-комнатная квартира\n38.7 м2\n7/16 этаж\n12 900 000 руб\nСокол\n8 мин пешком',
    'Звоните! Быстрый выход на сделку, документы готовы, квартира без обременений.',
])
def test_boilerplate_and_card_fields_are_not_identity(description):
    one=sample('1')
    one.update(description=description,card_text=description)
    result=listing_match(dict(one,id='2'),one)
    assert not result.auto
    assert result.score<85


def test_incomplete_address_and_missing_floor_never_auto_merge():
    for change in ({'address':'Москва'},{'address':'ЖК Тестовый'},{'floor':None},{'area':None}):
        one=sample('1',**change)
        assert not listing_match(dict(one,id='2'),one).auto


def test_multiple_equally_good_candidates_are_ambiguous(multi_app):
    main,admin=multi_app
    saved(main,sample('100'),sample('101'))
    result=saved(main,sample('102'),now='2026-10-19T10:00:00+00:00')
    assert result['relisted']==0 and result['possible_duplicates']==1
    assert len({property_id(main,x) for x in ('100','101','102')})==3
    assert pending(admin,'102')['confidence']<85
    assert best_match(sample('3'),[(1,sample('1')),(2,sample('2'))])[2].auto is False


def test_new_and_old_id_in_same_batch_require_confirmation(multi_app):
    main,admin=multi_app
    saved(main,sample('110'))
    result=saved(main,sample('110'),sample('111'),now='2026-10-19T10:00:00+00:00')
    assert result['relisted']==0 and result['possible_duplicates']==1
    assert property_id(main,'110')!=property_id(main,'111')
    assert decide(admin,'111','same').status_code==200
    assert property_id(main,'110')==property_id(main,'111')


def test_historical_refresh_does_not_revert_current_or_repeat_prices(multi_app):
    main,admin=multi_app
    saved(main,sample('120',price=13000000),now='2026-10-01T10:00:00+00:00')
    saved(main,sample('121',price=12500000),now='2026-10-02T10:00:00+00:00')
    assert property_id(main,'120')==property_id(main,'121')
    with main.database() as conn:
        before=conn.execute('SELECT count(*) FROM price_history').fetchone()[0]
    for day in ('03','04','05'):
        saved(main,sample('120',price=13000000),sample('121',price=12500000),now=f'2026-10-{day}T10:00:00+00:00')
    with main.database() as conn:
        assert conn.execute('SELECT count(*) FROM price_history').fetchone()[0]==before
    saved(main,sample('120',price=12900000),now='2026-10-06T10:00:00+00:00')
    item=next(x for x in admin.get('/api/state').json()['listings'] if '120' in x['listing_ids'])
    assert item['id']=='121' and item['price']==12500000
    assert item['first_seen']=='2026-10-01T10:00:00+00:00'
    assert [(x['id'],x['status']) for x in item['publication_history']]==[('120','historical'),('121','current')]
    assert [x['price'] for x in item['price_history']]==[13000000,12500000,12900000]
    # Missing price is not a new observation of the previously merged price.
    saved(main,{'id':'120','title':'Без цены'},now='2026-10-07T10:00:00+00:00')
    item2=next(x for x in admin.get('/api/state').json()['listings'] if x['property_id']==item['property_id'])
    assert item2['price_history']==item['price_history']


def test_same_price_relisting_does_not_start_repeated_history(multi_app):
    main,admin=multi_app
    saved(main,sample('130'),now='2026-10-01')
    saved(main,sample('131'),now='2026-10-02')
    with main.database() as conn:
        count=conn.execute('SELECT count(*) FROM price_history').fetchone()[0]
    saved(main,sample('131'),sample('130'),now='2026-10-03')
    with main.database() as conn:
        assert conn.execute('SELECT count(*) FROM price_history').fetchone()[0]==count


def test_manual_reject_survives_alias_and_cannot_be_bridged(multi_app):
    main,admin=multi_app
    saved(main,sample('140'),now='2026-10-01')
    saved(main,sample('141',area=39.4),now='2026-10-02')
    rejected=pending(admin,'141')
    assert decide(admin,'141','different').status_code==200
    first,other=property_id(main,'140'),property_id(main,'141')
    saved(main,sample('142',address='Москва, ул. Другая, д. 71'),now='2026-10-03')
    offer_candidate(main,'140','142')
    assert decide(admin,'140','same').status_code==200
    target=property_id(main,'142')
    assert first!=target
    with main.database() as conn:
        assert conn.execute('SELECT decision FROM duplicate_candidates WHERE id=?',(rejected['id'],)).fetchone()[0]=='rejected'
        assert conn.execute('SELECT merged_into FROM properties WHERE id=?',(first,)).fetchone()[0]==target
        assert conn.execute("SELECT count(*) FROM identity_decisions WHERE decision='different'").fetchone()[0]==1
    offer_candidate(main,'141','142')
    # Even a fabricated/old pending proposal cannot cross a stored rejection.
    with main.database() as conn:
        dc=conn.execute("SELECT id FROM duplicate_candidates WHERE listing_id='141' AND candidate_property_id=?",(target,)).fetchone()[0]
    response=admin.post('/api/listings/141/duplicate-decision',json={
        'decision':'same','candidate_id':dc,'source_property_id':other,'candidate_property_id':target})
    assert response.status_code==409
    saved(main,sample('141',area=39.4,price=12700000),now='2026-10-04')
    assert property_id(main,'141')==other and pending(admin,'141') is None
    importlib.reload(main)
    assert property_id(main,'141')==other


def test_candidate_identity_is_required_and_stale_target_rejected(multi_app):
    main,admin=multi_app
    saved(main,sample('150'),now='2026-10-01')
    saved(main,sample('151',area=39.4),now='2026-10-02')
    candidate=pending(admin,'151')
    assert admin.post('/api/listings/151/duplicate-decision',json={'decision':'same'}).status_code==422
    wrong=payload(candidate);wrong['candidate_property_id']+=999
    assert admin.post('/api/listings/151/duplicate-decision',json=wrong).status_code==409
    assert admin.post('/api/listings/151/duplicate-decision',json=payload(candidate),headers={'X-CSRF-Token':'bad'}).status_code==403
    assert admin.post('/api/listings/151/duplicate-decision',json=payload(candidate),headers={'Origin':'https://evil.test'}).status_code==403
    assert decide(admin,'151','same').status_code==200
    assert admin.post('/api/listings/151/duplicate-decision',json=payload(candidate)).status_code==409


def test_family_and_realtor_inheritance_versions_and_audit(multi_app):
    main,admin=multi_app
    saved(main,sample('160'),now='2026-10-01')
    saved(main,sample('161',area=39.4),now='2026-10-02')
    with main.database() as conn:
        conn.execute("""UPDATE listings SET review_status='liked',review_updated_at='2026-10-19T09:00:00+03:00',review_updated_by=1,
            review_version=100,realtor_status='approved',realtor_comment='Комментарий А',realtor_updated_at='2026-10-19T08:00:00Z',
            realtor_updated_by=1,realtor_version=4 WHERE id='160'""")
        conn.execute("""UPDATE listings SET review_status='rejected',review_updated_at='2026-10-19T07:00:00Z',review_updated_by=1,
            review_version=2,realtor_status='pending',realtor_comment='Комментарий Б',realtor_updated_at='2026-10-19T07:00:00Z',
            realtor_updated_by=1,realtor_version=200 WHERE id='161'""")
    response=decide(admin,'161','same')
    assert response.status_code==200
    item=response.json()
    assert item['review_status']=='rejected' and item['review_version']==101
    assert item['realtor_status']=='approved' and item['realtor_comment']=='Комментарий А' and item['realtor_version']==201
    with main.database() as conn:
        snapshots=[json.loads(row[0]) for row in conn.execute('SELECT review_snapshot FROM property_merge_audit')]
        assert {row['realtor_comment'] for row in snapshots}=={'Комментарий А','Комментарий Б'}
    assert admin.post('/api/listings/160/review',json={'status':'liked'}).json()['review_version']==102
    assert admin.post('/api/listings/160/realtor-review',json={'status':'rejected','comment':'Новый'}).json()['realtor_version']==202
    with main.database() as conn:
        assert {row[0] for row in conn.execute("SELECT review_version FROM listings WHERE id IN ('160','161')")}=={102}
        assert {row[0] for row in conn.execute("SELECT realtor_version FROM listings WHERE id IN ('160','161')")}=={202}


def test_csv_preview_is_readonly_and_browser_counts_properties(multi_app,monkeypatch):
    import csv
    main,admin=multi_app
    old=sample('170')
    saved(main,old,now='2026-10-01T00:00:00+00:00')
    assert admin.post('/api/listings/170/review',json={'status':'liked'}).status_code==200
    assert admin.post('/api/listings/170/realtor-review',json={'status':'approved','comment':'Сохранить'}).status_code==200
    text=io.StringIO();writer=csv.DictWriter(text,fieldnames=list(old),delimiter=';');writer.writeheader()
    writer.writerow(dict(old,id='171',url='https://cian.ru/sale/flat/171/',price=12500000))
    with main.database() as conn:
        before={table:[tuple(row) for row in conn.execute('SELECT * FROM '+table)]
                for table in ('properties','listings','price_history','listing_price_state','property_state')}
    preview=admin.post('/api/import/preview',content=text.getvalue().encode()).json()
    assert preview['added']==0 and preview['relisted']==1
    with main.database() as conn:
        for table,rows in before.items():
            assert [tuple(row) for row in conn.execute('SELECT * FROM '+table)]==rows
    result=admin.post('/api/import/confirm',json={'token':preview['token']}).json()
    assert result['added']==0 and result['relisted']==1
    assert admin.post('/api/import/confirm',json={'token':preview['token']}).status_code==409
    monkeypatch.setattr(main.CianBrowserSource,'collect',lambda self,filters:[sample('172',price=12000000)])
    with main.database() as conn:
        run_id=conn.execute("INSERT INTO runs(started,status,filters,source) VALUES ('2026','running','{}','browser')").lastrowid
    main.lock.acquire()
    main.run_collection(run_id,{'max_price':14500000,'metro_minutes':20},'browser')
    assert main.browser_state()['new_count']==0 and main.browser_state()['relisted_count']==1
    item=next(row for row in admin.get('/api/state').json()['listings'] if row['id']=='172')
    assert item['listing_ids']==['170','171','172']
    assert item['review_status']=='liked' and item['realtor_comment']=='Сохранить'
    book=load_workbook(io.BytesIO(admin.get('/api/export.xlsx').content))
    rows=list(book.active.values)
    exported=dict(zip(rows[0],next(row for row in rows[1:] if row[0]=='172')))
    assert exported['Все CIAN ID']=='170 → 171 → 172'
    assert 'CIAN 170' in exported['История цен (UTC)']
    assert exported['Перепубликаций']==2


def test_logo_favicon_share_mark_and_cache_hash(multi_app):
    import hashlib
    import re
    from pathlib import Path
    main,admin=multi_app
    svg={'s':'http://www.w3.org/2000/svg'}
    logo=ET.fromstring((Path(main.__file__).parent/'static/xata-logo.svg').read_text())
    mark=ET.fromstring((Path(main.__file__).parent/'static/xata-mark.svg').read_text())
    assert [x.attrib for x in logo.findall('.//s:path',svg)]==[x.attrib for x in mark.findall('.//s:path',svg)]
    assert len(logo.findall('.//s:rect',svg))==len(mark.findall('.//s:rect',svg))==4
    assert logo.find('s:text',svg).attrib.get('text-anchor')=='middle'
    assert mark.find('s:text',svg) is None
    for asset in ('xata-logo.svg','xata-mark.svg'):
        html=admin.get('/').text
        match=re.search('/static/'+asset+r'\?v=([a-f0-9]+)',html)
        body=admin.get(match[0])
        assert body.status_code==200
        assert match[1]==hashlib.sha256(body.content).hexdigest()[:16]
        assert 'no-cache' in body.headers['cache-control']
        assert "default-src 'self'" in body.headers['content-security-policy']
        assert b'<image' not in body.content and b'http://' not in body.content.replace(b'http://www.w3.org/2000/svg',b'')
        assert len(body.content)<2000


@pytest.fixture
def phase2_database(tmp_path,monkeypatch):
    monkeypatch.setenv('DATA_DIR',str(tmp_path));monkeypatch.setenv('APP_USER','owner')
    monkeypatch.setenv('APP_PASSWORD','long-test-password');monkeypatch.setenv('BROWSER_SCHEDULE_ENABLED','false')
    filename=tmp_path/'xata.sqlite3'
    @contextmanager
    def database():
        conn=sqlite3.connect(filename);conn.row_factory=sqlite3.Row;conn.execute('PRAGMA foreign_keys=ON')
        try:
            with conn: yield conn
        finally: conn.close()
    with database() as conn:
        conn.executescript("""
        CREATE TABLE users(id INTEGER PRIMARY KEY,username TEXT NOT NULL UNIQUE COLLATE NOCASE,display_name TEXT NOT NULL,
          password_hash TEXT NOT NULL,role TEXT NOT NULL CHECK(role IN ('admin','reviewer','realtor')),enabled INTEGER NOT NULL,
          created_at TEXT NOT NULL,updated_at TEXT NOT NULL,password_length INTEGER);
        CREATE TABLE sessions(token_hash TEXT PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id),csrf_token TEXT NOT NULL,expires REAL NOT NULL);
        CREATE TABLE login_challenges(token_hash TEXT PRIMARY KEY,csrf_token TEXT NOT NULL,expires REAL NOT NULL);
        CREATE TABLE login_attempts(bucket TEXT PRIMARY KEY,failed INTEGER NOT NULL,started REAL NOT NULL);
        CREATE TABLE listings(id TEXT PRIMARY KEY,title TEXT,price INTEGER,address TEXT,url TEXT,first_seen TEXT,last_seen TEXT,
          complex_name TEXT,area REAL,floor TEXT,metro TEXT,metro_time TEXT,source TEXT,last_imported TEXT,card_text TEXT,description TEXT,
          metro_walk_minutes INTEGER,review_status TEXT NOT NULL DEFAULT 'new' CHECK(review_status IN ('new','liked','viewed','rejected')),
          review_updated_at TEXT,review_updated_by INTEGER REFERENCES users(id),review_version INTEGER NOT NULL DEFAULT 0,
          realtor_status TEXT NOT NULL DEFAULT 'pending' CHECK(realtor_status IN ('pending','approved','rejected')),realtor_comment TEXT NOT NULL DEFAULT '',
          realtor_updated_at TEXT,realtor_updated_by INTEGER REFERENCES users(id),realtor_version INTEGER NOT NULL DEFAULT 0,extra BLOB DEFAULT X'ABCD');
        CREATE INDEX original_price ON listings(price);
        CREATE TABLE audit(id TEXT);
        CREATE TRIGGER original_trigger AFTER INSERT ON listings BEGIN INSERT INTO audit VALUES(new.id); END;
        CREATE TABLE runs(id INTEGER PRIMARY KEY,started TEXT,finished TEXT,status TEXT,message TEXT,filters TEXT,count INTEGER DEFAULT 0,source TEXT);
        CREATE TABLE browser_state(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL);
        CREATE TABLE import_previews(token TEXT PRIMARY KEY,created REAL NOT NULL,payload TEXT NOT NULL);
        CREATE TABLE schema_migrations(name TEXT PRIMARY KEY,applied_at TEXT NOT NULL);
        INSERT INTO schema_migrations VALUES ('phase2_metro_cleanup','2026');
        """)
        conn.execute('INSERT INTO users VALUES (17,?,?,?,?,1,?,?,12)',('owner','Владелец',HASHER.hash('long-test-password'),'admin','2020','2020'))
        conn.execute('INSERT INTO sessions VALUES (?,?,?,?)',(digest('legacy-session'),17,'legacy-csrf',time.time()+3600))
        conn.execute("INSERT INTO login_challenges VALUES ('challenge','csrf',9999999999)")
        conn.execute("INSERT INTO login_attempts VALUES ('bucket',4,123)")
        for item_id,status,comment,first,last in [('501','liked','Оставить','2026-10-01','2026-10-02'),
                                                  ('502','rejected','Документы?','2026-10-03','2026-10-04')]:
            item=sample(item_id)
            columns=list(item)+['first_seen','last_seen','last_imported','review_status','review_updated_at','review_updated_by','review_version',
                                'realtor_status','realtor_comment','realtor_updated_at','realtor_updated_by','realtor_version']
            values=list(item.values())+[first,last,'2026-09-30',status,'2026-10-05',17,11,'approved',comment,'2026-10-06',17,21]
            conn.execute('INSERT INTO listings('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')',values)
        conn.execute("INSERT INTO runs VALUES (3,'2026','2026','done','Сохранено','{}',2,'browser')")
        conn.execute("INSERT INTO browser_state VALUES (1,?)",(json.dumps({'status':'authorized','last_success':'2026','last_error':'kept'}),))
        conn.execute("INSERT INTO import_previews VALUES ('saved-preview',123,'[]')")
    return database


def test_stable_phase2_upgrade_preserves_every_original_value(phase2_database):
    database=phase2_database
    tables=('listings','users','sessions','runs','browser_state','import_previews','login_challenges','login_attempts')
    with database() as conn:
        before={table:[dict(row) for row in conn.execute('SELECT * FROM '+table)] for table in tables}
    from app import main
    importlib.reload(main)
    with database() as conn:
        for table,rows in before.items():
            after=[dict(row) for row in conn.execute('SELECT * FROM '+table)]
            assert len(after)==len(rows)
            for original,current in zip(rows,after):
                assert all(current[key]==value for key,value in original.items()),table
        assert len({row[0] for row in conn.execute('SELECT property_id FROM listings')})==2
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='original_trigger'").fetchone()
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='original_price'").fetchone()
        prices=[tuple(row) for row in conn.execute('SELECT listing_id,observed_at,price FROM price_history ORDER BY listing_id')]
        assert [row[1] for row in prices]==['2026-10-02','2026-10-04']
        snapshot=[dict(row) for row in conn.execute('SELECT * FROM listings')]
    with TestClient(main.app,base_url='https://testserver') as client:
        client.cookies.set(COOKIE,'legacy-session');client.headers['X-CSRF-Token']='legacy-csrf'
        assert client.get('/api/auth/me').status_code==200
        assert len(client.get('/api/state').json()['listings'])==2
    importlib.reload(main)
    with database() as conn:
        assert [dict(row) for row in conn.execute('SELECT * FROM listings')]==snapshot
        assert [tuple(row) for row in conn.execute('SELECT listing_id,observed_at,price FROM price_history ORDER BY listing_id')]==prices


def test_phase3_schema_failure_rolls_back_all_new_tables(phase2_database,monkeypatch):
    from app import migrations
    def fail(*args): raise RuntimeError('injected price seed failure')
    monkeypatch.setattr(migrations,'record_price',fail)
    with phase2_database() as conn:
        before=[dict(row) for row in conn.execute('SELECT * FROM listings')]
    with pytest.raises(RuntimeError,match='injected'):
        migrations.migrate_listings(phase2_database)
    with phase2_database() as conn:
        assert [dict(row) for row in conn.execute('SELECT * FROM listings')]==before
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='properties'").fetchone()
        assert conn.execute('PRAGMA foreign_keys').fetchone()[0]==1
