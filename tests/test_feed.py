"""Lightweight projections, gzip negotiation and durable invalidation."""
import asyncio
import gzip
import importlib
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from tests.test_multiuser import multi_app
from tests.helpers import sign_in,post_realtor
from tests.perf_dataset import seed


def current(client,key='123'):
    return client.get('/api/listings/'+key+'/details').json()


def test_summaries_ours_details_and_no_filesystem_walk(multi_app,monkeypatch):
    main,client=multi_app
    text='Секретный длинный DOM текст '*500
    with main.database() as conn:
        conn.execute('UPDATE listings SET description=?,card_text=?,realtor_comment=? WHERE id=?',(text,text,'Комментарий '*300,'123'))
    item=current(client)
    client.patch('/api/listings/123/selection',json={'property_id':item['property_id'],'selection_version':0,'selected':True})
    monkeypatch.setattr(main.covers,'path',lambda _:pytest.fail('List must not walk paths'))
    response=client.get('/api/state',headers={'Accept-Encoding':'identity'})
    assert response.status_code==200
    result=response.json()
    assert result['totals']=={'all':2,'ours':1}
    assert len(result['listings'])==2
    entry=next(i for i in result['listings'] if i['id']=='123')
    assert len(entry['realtor_comment'])==240 and entry['realtor_comment_truncated']
    for field in ('description','card_text','price_history','publication_history','comment_history','cover_image_url'):
        assert field not in entry
    ours=client.get('/api/state?view=ours').json()
    assert [i['id'] for i in ours['listings']]==['123']
    assert ours['totals']==result['totals']
    assert 'Секретный' not in response.text
    assert {'sql','assemble','serialize','app'}<={p.split(';')[0].strip() for p in response.headers['server-timing'].split(',')}
    assert current(client)['description']==text


def test_conditional_version_permissions_no_store_and_all_mutation_types(multi_app):
    main,client=multi_app
    first=client.get('/api/state')
    tag=first.headers['etag']
    assert first.headers['cache-control']=='no-store'
    assert 'accept-encoding' in first.headers['vary'].lower()
    same=client.get('/api/state',headers={'If-None-Match':tag})
    assert same.status_code==304 and same.content==b'' and same.headers['etag']==tag
    # Full diagnostic view and ours have distinct validators.
    assert client.get('/api/state?view=ours',headers={'If-None-Match':tag}).status_code==200
    assert client.get('/api/state?full=1',headers={'If-None-Match':tag}).status_code==200
    with TestClient(main.app,base_url='https://testserver') as stranger:
        assert stranger.get('/api/state',headers={'If-None-Match':tag}).status_code==401
        assert stranger.get('/api/listings/123/details').status_code==401
    def changed():
        nonlocal tag
        response=client.get('/api/state',headers={'If-None-Match':tag})
        assert response.status_code==200
        assert response.headers['etag']!=tag
        tag=response.headers['etag']
        assert client.get('/api/state',headers={'If-None-Match':tag}).status_code==304
    assert client.post('/api/listings/123/review',json={'status':'liked'}).status_code==200;changed()
    assert post_realtor(client,'/api/listings/123/realtor-review',json={'status':'approved','comment':'Текст'}).status_code==200;changed()
    item=current(client)
    assert client.patch('/api/listings/123/selection',json={'property_id':item['property_id'],'selection_version':0,'selected':True}).status_code==200;changed()
    item=current(client)
    assert client.patch('/api/listings/123/family-comment',json={'property_id':item['property_id'],'family_comment_version':0,'text':'Наш выбор'}).status_code==200;changed()
    item=current(client)
    assert client.post('/api/listings/123/visits',json={'property_id':item['property_id'],'status':'completed','outcome':'liked','completed_at':'2026-11-01T12:00:00Z'}).status_code==200;changed()
    item=current(client)
    assert client.patch('/api/listings/123/viewing-classification',json={'property_id':item['property_id'],'visits_version':item['visits_version'],'documents_ready':True}).status_code==200;changed()
    with main.database() as conn:
        main.save_items(conn,[{'id':'123','price':11900000}],'2026-11-04T10:00:00Z',imported=True)
    changed()
    from app.property_match import merge_properties
    with main.database() as conn:
        a,b=[r[0] for r in conn.execute("SELECT property_id FROM listings ORDER BY id")]
        merge_properties(conn,a,b,'2026-11-04T11:00:00Z')
    changed()
    # Polling an identical browser status and touching sessions cause no list churn.
    main.update_browser(**main.browser_state())
    client.get('/api/auth/me')
    assert client.get('/api/state',headers={'If-None-Match':tag}).status_code==304


@pytest.mark.parametrize('accept,encoded,status',[
    ('gzip',True,200),('identity',False,200),('gzip;q=0',False,200),
    ('gzip;q=0,*;q=1',False,200),('*;q=.5',True,200),
    ('gzip;q=.4,identity;q=.8',False,200),('gzip;q=.9,identity;q=.1',True,200),
    ('identity;q=0,gzip;q=0',False,406),('*;q=0',False,406)])
def test_json_gzip_raw_negotiation(accept,encoded,status):
    from app.compression import JsonGzipMiddleware
    body=json.dumps({'text':'Описание'*500},ensure_ascii=False).encode()
    async def app(scope,receive,send):
        await send({'type':'http.response.start','status':200,'headers':[(b'content-type',b'application/json'),(b'cache-control',b'no-store'),(b'vary',b'Origin')]})
        await send({'type':'http.response.body','body':body})
    messages=[]
    async def send(message):messages.append(message)
    async def receive():return {'type':'http.request','body':b''}
    asyncio.run(JsonGzipMiddleware(app)({'type':'http','path':'/api/state','headers':[(b'accept-encoding',accept.encode())]},receive,send))
    start,payload=messages;headers=dict(start['headers'])
    assert start['status']==status
    assert (headers.get(b'content-encoding')==b'gzip')==encoded
    assert headers[b'cache-control']==b'no-store'
    assert b'Accept-Encoding' in headers[b'vary'] and b'Origin' in headers[b'vary']
    if status==200:assert (gzip.decompress(payload['body']) if encoded else payload['body'])==body
    assert int(headers[b'content-length'])==len(payload['body'])


def test_500_long_listings_scoped_payload_and_histories(multi_app):
    main,client=multi_app
    seed(main)
    response=client.get('/api/state')
    full=client.get('/api/state?full=1')
    ours=client.get('/api/state?view=ours')
    assert len(response.json()['listings'])==502
    assert len(ours.json()['listings'])==20
    assert ours.json()['totals']=={'all':502,'ours':20}
    assert len(response.content)<len(full.content)/4
    assert int(response.headers['content-length'])<len(response.content)/3
    assert len(ours.content)<len(response.content)/10
    detail=current(client,'800001')
    assert len(detail['visits'])==4 and len(detail['price_history'])==13 and len(detail['comment_history'])==6
    assert detail['description'] and detail['visits'][0]['preparation'] is not None
    assert detail['cover_image_src'] and detail['cover_thumb_src']
    for item in response.json()['listings']:
        assert len(item.get('visits',[]))<=2 and item['details_loaded'] is False


def test_revision_migration_idempotent_and_unknown_schema_preserved(multi_app):
    main,client=multi_app
    from app import feed
    with main.database() as conn:
        conn.execute('ALTER TABLE listings ADD COLUMN vendor_payload BLOB')
        conn.execute('UPDATE listings SET vendor_payload=?',(b'\\xff\\x00',))
        conn.execute('CREATE INDEX vendor_payload_idx ON listings(vendor_payload)')
        conn.execute('CREATE TRIGGER vendor_payload_trigger AFTER UPDATE OF vendor_payload ON listings BEGIN SELECT 1; END')
        before=[tuple(r) for r in conn.execute('SELECT * FROM listings')]
        schema=[tuple(r) for r in conn.execute("SELECT name,sql FROM sqlite_master WHERE name LIKE 'vendor_%'")]
        version=feed.version(conn)
    feed.migrate(main.database);feed.migrate(main.database)
    with main.database() as conn:
        assert [tuple(r) for r in conn.execute('SELECT * FROM listings')]==before
        assert [tuple(r) for r in conn.execute("SELECT name,sql FROM sqlite_master WHERE name LIKE 'vendor_%'")]==schema
        assert feed.version(conn)==version
        assert conn.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert conn.execute('PRAGMA foreign_key_check').fetchone() is None
    assert client.get('/api/auth/me').status_code==200


def test_summary_last_completed_uses_actual_instant_for_legacy_offsets_and_microseconds(multi_app):
    main,client=multi_app
    with main.database() as conn:
        pid=conn.execute("SELECT property_id FROM listings WHERE id='123'").fetchone()[0]
        for instant,outcome in [('2026-11-01T11:00:00+03:00','rejected'),('2026-11-01T10:00:00Z','liked'),
                                ('2026-11-01T10:00:00.000001+00:00','rejected')]:
            conn.execute("""INSERT INTO property_visits(property_id,status,outcome,completed_at,created_at,updated_at,updated_by)
                VALUES (?,'completed',?,?,'2026','2026',1)""",(pid,outcome,instant))
    result=client.get('/api/state').json()
    item=next(i for i in result['listings'] if i['id']=='123')
    assert len(item['visits'])==1
    assert item['visits'][0]['completed_at']=='2026-11-01T10:00:00.000001+00:00'
    assert item['viewed_group']==current(client)['viewed_group']=='rejected'


def test_old_browser_tab_is_not_given_a_truncated_editable_comment(multi_app):
    main,client=multi_app
    value='Полный комментарий '*90
    post_realtor(client,'/api/listings/123/realtor-review',json={'status':'approved','comment':value})
    old=client.get('/api/state',headers={'Sec-Fetch-Site':'same-origin'})
    assert old.status_code==409 and 'Обновите вкладку' in old.json()['detail']
    assert current(client)['realtor_comment']==value
    assert client.get('/api/state?view=all',headers={'Sec-Fetch-Site':'same-origin'}).status_code==200
    legacy=client.get('/api/state?full=1',headers={'Sec-Fetch-Site':'same-origin'})
    assert next(i for i in legacy.json()['listings'] if i['id']=='123')['realtor_comment']==value
