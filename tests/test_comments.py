"""Property comments, roles, optimistic conflicts and production-safe migrations."""
import importlib
import io
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from tests.helpers import sign_in, post_realtor
from tests.test_multiuser import multi_app, reviewer_client
from tests.test_our_list import choose
from tests.test_phase3 import sample
from tests.test_viewings import current


def save(client,item,text,**extra):
    return client.patch('/api/listings/'+item['id']+'/family-comment',json={
        'property_id':item['property_id'],'family_comment_version':item['family_comment_version'],'text':text,**extra})


def test_family_comment_is_independent_and_records_author_clear_history_and_export(multi_app):
    main,admin=multi_app
    choose(admin,current(admin))
    _,buyer=reviewer_client(main,admin)
    before=current(admin)
    value='=SUM(1,2)\nПочему выбрали <img src=x onerror=alert(1)>'
    response=save(buyer,before,value)
    assert response.status_code==200,response.text
    item=response.json()
    assert item['family_comment']==value and item['family_comment_version']==1
    assert item['family_comment_updated_by_name']=='Риелтор'  # fixture's buyer display name
    assert item['family_comment_updated_at'].endswith('+00:00')
    for key in ('review_status','realtor_status','visits','visits_version','viewing_documents_ready','selection_version','in_our_list'):
        assert item[key]==before[key]
    # Replay unchanged text does not append another revision.
    assert save(buyer,item,value).json()['family_comment_version']==1
    book=load_workbook(io.BytesIO(admin.get('/api/export.xlsx').content))
    sheet=book['Объявления']
    assert sheet.cell(1,sheet.max_column).value=='Наш комментарий'
    row=next(n for n in range(2,sheet.max_row+1) if sheet.cell(n,1).value=='123')
    assert sheet.cell(row,sheet.max_column).value=="'"+value
    cleared=save(buyer,item,'').json()
    assert cleared['family_comment']=='' and cleared['family_comment_version']==2
    assert [h['text'] for h in cleared['comment_history'] if h['kind']=='family']==['',value]
    assert cleared['comment_history'][0]['changed_by']==item['family_comment_updated_by']
    importlib.reload(main)
    assert current(admin)['family_comment_version']==2
    assert len(current(admin)['comment_history'])==2


def test_family_comment_permissions_csrf_and_unselected_apartment(multi_app):
    main,admin=multi_app
    assert save(admin,current(admin),'not selected').status_code==409
    item=choose(admin,current(admin)).json()
    assert admin.post('/api/users',json={'username':'expert','display_name':'Эксперт','password':'12345678','role':'realtor'}).status_code==201
    with TestClient(main.app,base_url='https://testserver') as agent:
        sign_in(agent,'expert','12345678')
        assert save(agent,item,'forbidden').status_code==403
        assert current(agent)['family_comment']==''
        assert post_realtor(agent,'/api/listings/123/realtor-review',json={'status':'approved','comment':'Его текст'}).status_code==200
    with TestClient(main.app,base_url='https://testserver') as stranger:
        assert save(stranger,item,'forbidden').status_code in (401,403)
        sign_in(stranger);stranger.headers.pop('X-CSRF-Token')
        assert save(stranger,item,'forbidden').status_code==403
    assert save(admin,{**item,'id':'missing'},'missing').status_code==404
    assert save(admin,item,'bad\x00text').status_code==422
    assert save(admin,item,'x'*20001).status_code==422
    assert save(admin,item,'wrong',property_id=current(admin,'456')['property_id']).status_code==409


def test_family_concurrent_writes_one_commit_and_explicit_stale_conflict(multi_app):
    main,admin=multi_app
    item=choose(admin,current(admin)).json()
    barrier=Barrier(2)
    def write(number):
        barrier.wait(timeout=5)
        return save(admin,item,'Вариант '+str(number)).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(write,range(2)))==[200,409]
    saved=current(admin)
    assert saved['family_comment_version']==1
    assert len(saved['comment_history'])==1
    assert save(admin,item,'stale').status_code==409
    assert current(admin)['family_comment']==saved['family_comment']


def test_realtor_server_requires_property_and_version_and_preserves_history(multi_app):
    main,admin=multi_app
    item=current(admin)
    assert admin.post('/api/listings/123/realtor-review',json={'status':'approved','comment':'unsafe legacy write'}).status_code==422
    data={'property_id':item['property_id'],'realtor_version':0,'status':'approved','comment':'Актуальное'}
    response=admin.post('/api/listings/123/realtor-review',json=data)
    assert response.status_code==200,response.text
    saved=response.json()
    assert admin.post('/api/listings/123/realtor-review',json={**data,'comment':'Устаревшее'}).status_code==409
    assert current(admin)['realtor_comment']=='Актуальное'
    assert saved['comment_history'][0]['kind']=='realtor'
    assert saved['comment_history'][0]['text']=='Актуальное'
    assert saved['family_comment']=='' and saved['family_comment_version']==0


def test_comments_survive_selection_rejection_import_and_republication(multi_app):
    main,admin=multi_app
    with main.database() as conn:
        main.save_items(conn,[sample('700')],'2026-10-01T10:00:00Z')
    item=current(admin,'700')
    item=choose(admin,item,url='/api/listings/700/selection').json()
    item=save(admin,item,'Не переносить старые заметки. Наши сомнения.').json()
    admin.post('/api/listings/700/review',json={'status':'rejected'})
    post_realtor(admin,'/api/listings/700/realtor-review',json={'status':'rejected','comment':'Документы'})
    item=current(admin,'700')
    choose(admin,item,False,url='/api/listings/700/selection')
    old=current(admin,'700')
    assert save(admin,old,'Нельзя редактировать удалённое').status_code==409
    assert old['family_comment']=='Не переносить старые заметки. Наши сомнения.'
    choose(admin,old,url='/api/listings/700/selection')
    with main.database() as conn:
        main.save_items(conn,[sample('700',price=12500000)],'2026-10-02T10:00:00Z',imported=True)
        main.save_items(conn,[sample('701',price=12400000)],'2026-10-03T10:00:00Z')
    saved=current(admin,'700')
    assert saved['family_comment']==old['family_comment']
    assert saved['family_comment_version']==old['family_comment_version']
    assert '701' in saved['listing_ids'] and saved['in_our_list']
    assert saved['comment_history']==old['comment_history']


@pytest.mark.parametrize('both',[False,True])
def test_merge_preserves_both_comments_provenance_history_and_conflicts(multi_app,both):
    main,admin=multi_app
    a=choose(admin,current(admin)).json()
    b=choose(admin,current(admin,'456'),url='/api/listings/456/selection').json()
    a=save(admin,a,'Текст квартиры А').json()
    if both:b=save(admin,b,'Текст квартиры Б').json()
    post_realtor(admin,'/api/listings/123/realtor-review',json={'status':'approved','comment':'Риелтор А'})
    post_realtor(admin,'/api/listings/456/realtor-review',json={'status':'rejected','comment':'Риелтор Б'})
    from app.property_match import merge_properties
    with main.database() as conn:
        conn.execute('BEGIN IMMEDIATE')
        merge_properties(conn,a['property_id'],b['property_id'],'2026-11-02T10:00:00Z')
    item=current(admin)
    assert item['in_our_list']
    assert 'Текст квартиры А' in item['family_comment']
    if both:
        assert 'Текст квартиры Б' in item['family_comment']
        assert 'ЦИАН 123' in item['family_comment'] and 'ЦИАН 456' in item['family_comment']
    texts=[h['text'] for h in item['comment_history']]
    assert 'Текст квартиры А' in texts and 'Риелтор А' in texts and 'Риелтор Б' in texts
    assert {h['origin_property_id'] for h in item['comment_history']}=={a['property_id'],b['property_id']}
    assert item['family_comment_version']>a['family_comment_version']
    assert save(admin,a,'Устаревшая связь').status_code==409
    assert len([i for i in admin.get('/api/state?full=1').json()['listings'] if '123' in i['listing_ids']])==1


def test_additive_migration_preserves_vendor_schema_sessions_and_is_idempotent(multi_app):
    main,admin=multi_app
    choose(admin,current(admin))
    save(admin,current(admin),'Семейный текст')
    with main.database() as conn:
        conn.execute('ALTER TABLE properties ADD COLUMN vendor_blob BLOB')
        conn.execute('UPDATE properties SET vendor_blob=?',(bytes([255,0]),))
        conn.execute('CREATE INDEX vendor_comment_index ON properties(vendor_blob)')
        conn.execute('CREATE TRIGGER vendor_comment_trigger AFTER UPDATE OF vendor_blob ON properties BEGIN SELECT 1; END')
        tables=('properties','listings','property_visits','property_comment_history','sessions')
        before={table:[dict(r) for r in conn.execute('SELECT * FROM '+table)] for table in tables}
        objects=[tuple(r) for r in conn.execute("SELECT name,sql FROM sqlite_master WHERE name LIKE 'vendor_comment%' ORDER BY name")]
    from app.migrations import migrate_listings
    migrate_listings(main.database);migrate_listings(main.database)
    with main.database() as conn:
        for table in tables:
            assert [dict(r) for r in conn.execute('SELECT * FROM '+table)]==before[table]
        assert [tuple(r) for r in conn.execute("SELECT name,sql FROM sqlite_master WHERE name LIKE 'vendor_comment%' ORDER BY name")]==objects
        assert conn.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert conn.execute('PRAGMA foreign_key_check').fetchone() is None
    assert admin.get('/api/auth/me').status_code==200


def test_previous_app_reopens_copy_without_losing_new_comments_or_sessions(multi_app,tmp_path):
    import os
    import sqlite3
    import subprocess
    import sys
    from pathlib import Path
    root=os.getenv('XATA_OLD_ROOT')
    if not root:
        pytest.skip('CI prepares immutable previous app checkout')
    main,admin=multi_app
    choose(admin,current(admin))
    save(admin,current(admin),'Новое поле — сохранить при откате')
    destination=tmp_path/'rollback-copy';destination.mkdir()
    with main.database() as source,sqlite3.connect(destination/'xata.sqlite3') as copy:
        source.backup(copy)
    code="""
import sys,sqlite3
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from app import main
assert str(Path(main.__file__).resolve()).startswith(sys.argv[1])
with main.database() as db:
    row=db.execute("SELECT family_comment,family_comment_version FROM properties WHERE id=(SELECT property_id FROM listings WHERE id='123')").fetchone()
    assert row['family_comment']=='Новое поле — сохранить при откате' and row['family_comment_version']==1
    assert db.execute('SELECT count(*) FROM property_comment_history').fetchone()[0]==1
    assert db.execute('SELECT count(*) FROM sessions').fetchone()[0]>0
    assert db.execute('PRAGMA foreign_key_check').fetchone() is None
    assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
main.covers.close()
"""
    result=subprocess.run([sys.executable,'-c',code,str(Path(root).resolve())],
        env={**os.environ,'DATA_DIR':str(destination)},capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stderr
    assert current(admin)['family_comment']=='Новое поле — сохранить при откате'
