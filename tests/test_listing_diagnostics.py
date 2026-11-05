"""Targeted investigation on synthetic SQLite only; no collection or production writes."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
from tests.test_multiuser import multi_app
from tests.helpers import post_realtor
from app.property_match import merge_properties

PATH=Path(__file__).parents[1]/'scripts/diagnose_listings.py'
spec=importlib.util.spec_from_file_location('listing_diagnostics',PATH)
diag=importlib.util.module_from_spec(spec);spec.loader.exec_module(diag)


def test_historical_merged_refused_and_missing_ids_without_writes(multi_app,monkeypatch):
    main,client=multi_app
    assert post_realtor(client,'/api/listings/123/realtor-review',json={'status':'rejected','comment':'PRIVATE_COMMENT'}).status_code==200
    with main.database() as conn:
        a,b=[r[0] for r in conn.execute('SELECT property_id FROM listings ORDER BY id')]
        conn.execute('UPDATE properties SET in_our_list=1 WHERE id=?',(a,))
        merge_properties(conn,a,b,'2026-11-04T20:00:00Z')
        before={table:[tuple(r) for r in conn.execute('SELECT * FROM '+table)] for table in
            ('listings','properties','property_merge_audit','identity_decisions','sessions','list_revision')}
    monkeypatch.setattr(diag,'current_dom',lambda *a:pytest.fail('No default network request'))
    report=diag.diagnose(main.DB,['123','456','999'])
    first,second,missing=report['listings']
    assert first['stages']['import']=='stored_listing_exists'
    assert len(first['properties'])==1
    prop=first['properties'][0]
    assert prop['root_property_id']==b and prop['publication_ids']==['123','456']
    assert prop['target_is_historical'] and prop['current_listing_id']=='456'
    assert prop['expected_all_section']=='refused' and prop['all_initially_collapsed']
    assert prop['selected'] and prop['expected_ours_section']=='planned'
    assert [a,b] in prop['merge_chains']
    assert first['merge_audit'] and first['stages']['extraction']=='not_recorded'
    assert second['properties'][0]['root_property_id']==b
    assert missing['stored_listing'] is None and missing['properties']==[]
    assert missing['stages']['search_result']=='not_recorded'
    assert report['current_dom']['reason']=='not_requested'
    assert 'PRIVATE_COMMENT' not in json.dumps(report)
    with main.database() as conn:
        for table,rows in before.items():assert [tuple(r) for r in conn.execute('SELECT * FROM '+table)]==rows


def test_viewing_group_uses_latest_actual_instant_not_family_opinion(multi_app):
    main,_=multi_app
    with main.database() as conn:
        pid=conn.execute("SELECT property_id FROM listings WHERE id='123'").fetchone()[0]
        conn.execute('UPDATE properties SET in_our_list=1,viewing_documents_ready=1 WHERE id=?',(pid,))
        for at,outcome in [('2026-11-01T13:00:00+03:00','rejected'),('2026-11-01T11:00:00Z','liked')]:
            conn.execute("""INSERT INTO property_visits(property_id,status,outcome,completed_at,created_at,updated_at,updated_by)
                VALUES (?,'completed',?,?,'2026','2026',1)""",(pid,outcome,at))
    prop=diag.diagnose(main.DB,['123'])['listings'][0]['properties'][0]
    assert prop['expected_ours_section']=='documents' and not prop['ours_initially_collapsed']
    assert prop['family_status']=='new'


def test_legacy_database_and_nonexistent_file_never_migrate_or_create(tmp_path):
    path=tmp_path/'legacy.sqlite3'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE listings(id TEXT PRIMARY KEY,first_seen TEXT,description TEXT)')
        conn.execute("INSERT INTO listings VALUES ('123','2020','PRIVATE_BODY')")
        conn.execute('CREATE INDEX vendor_keep ON listings(first_seen)')
    before=hashlib.sha256(path.read_bytes()).digest()
    report=diag.diagnose(path,['123','456'])
    assert report['listings'][0]['stored_listing']['id']=='123'
    assert report['listings'][0]['properties']==[]
    assert 'PRIVATE_BODY' not in json.dumps(report)
    assert hashlib.sha256(path.read_bytes()).digest()==before
    absent=tmp_path/'absent.sqlite3'
    with pytest.raises(sqlite3.OperationalError):diag.diagnose(absent,['123'])
    assert not absent.exists()
    result=subprocess.run([sys.executable,str(PATH),'--db',str(path),'123'],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
    assert hashlib.sha256(path.read_bytes()).digest()==before


def test_public_query_never_echoes_secrets_or_claims_saved_filters(monkeypatch):
    secret='DO_NOT_OUTPUT_QUERY_SECRET'
    monkeypatch.setenv('CIAN_SEARCH_URL','https://www.cian.ru/cat.php?maxprice=13000000&foot_min=15&room1=2&token='+secret)
    config=diag.configuration()
    assert config['interface_criteria']=={'max_price':13000000,'metro_minutes':15}
    assert config['effective_request']['explicit_conflicts']==['room1']
    assert config['effective_request']['has_unreported_parameters']
    assert secret not in json.dumps(config)
    saved=diag.public_query('https://cian.ru/saved-search/?token='+secret)
    assert not saved['explicit_conflicts'] and 'room1' in saved['unconfirmed_constraints']
    assert secret not in json.dumps(saved)
    assert secret not in json.dumps(diag.public_query('https://cian.ru/cat.php?maxprice='+secret))


def test_dom_client_only_calls_diagnostic_endpoint_and_does_not_follow_redirects(monkeypatch):
    seen=[]
    class Response:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self,size):return b'{"mode":"current_dom_read_only","status":"authorized","targets":[]}'
    class Opener:
        def open(self,request,timeout):
            seen.append((request.full_url,request.get_method(),json.loads(request.data)))
            return Response()
    monkeypatch.setattr(diag.urllib.request,'build_opener',lambda *args:Opener())
    result=diag.current_dom(['123'],'http://browser:8010','a'*20)
    assert result['available'] and seen[0][0]=='http://browser:8010/diagnostics/dom'
    assert seen[0][1]=='POST' and seen[0][2]['ids']==['123']
    assert diag.current_dom(['123'],'http://browser:8010','')['reason']=='internal_token_unavailable'
    assert diag.validate_ids(['000123','123','456'])==['123','456']
    for value in ['123/','https://cian.ru/sale/flat/123/','secret=1','']:
        with pytest.raises(ValueError):diag.validate_ids([value])


def test_dom_endpoint_auth_validation_and_no_collector_start(monkeypatch):
    from collector.service import app
    from fastapi.testclient import TestClient
    import collector.service as service
    monkeypatch.setenv('BROWSER_API_TOKEN','test-diagnostic-token')
    class Collector:
        async def collect(self,*args):pytest.fail('Diagnostic must never collect')
    monkeypatch.setattr(app.state,'collector',Collector(),raising=False)
    async def probe(collector,ids,max_price):
        assert ids==['123'] and max_price==14500000
        return {'mode':'current_dom_read_only','status':'authorized','targets':[]}
    monkeypatch.setattr(service,'diagnose_current_page',probe)
    client=TestClient(app)  # No context manager/lifespan: no Chromium launch.
    assert client.post('/diagnostics/dom',json={'ids':['123']}).status_code==401
    headers={'Authorization':'Bearer test-diagnostic-token'}
    assert client.post('/diagnostics/dom',headers=headers,json={'ids':['000123','123'],'max_price':14500000}).status_code==200
    assert client.post('/diagnostics/dom',headers=headers,json={'ids':['https://cian.ru/']}).status_code==422
    assert client.post('/diagnostics/dom',headers=headers,json={'ids':['123'],'url':'https://cian.ru/'}).status_code==422
    assert client.post('/diagnostics/dom',headers=headers,json={'ids':['123']*21}).status_code==422



def test_navigation_during_read_only_probe_is_not_misattributed_to_old_page():
    import asyncio
    from collector.diagnostics import diagnose_current_page
    async def scenario():
        class Page:
            url='https://cian.ru/cat.php?room1=1'
            async def evaluate(self,*args):return {'snapshot':{'cards':[]},'targets':[]}
        class Collector:
            def __init__(self):self.lock=asyncio.Lock();self.page=Page();self.calls=0
            async def inspect(self):
                self.calls+=1
                if self.calls==2:self.page.url='https://cian.ru/cat.php?token=PRIVATE_NEW_PAGE'
                return 'authorized'
        result=await diagnose_current_page(Collector(),['123'])
        assert result['reason']=='page_changed_during_diagnostic' and result['targets']==[]
        assert 'PRIVATE_NEW_PAGE' not in json.dumps(result)
    asyncio.run(scenario())



def test_cli_never_executes_application_package_initializers(tmp_path):
    import os
    runtime=tmp_path/'runtime';app=runtime/'app';app.mkdir(parents=True)
    marker=tmp_path/'forbidden-startup'
    (app/'__init__.py').write_text("from pathlib import Path\nPath("+repr(str(marker))+").write_text('started')\nraise RuntimeError('must not run')\n")
    (app/'browser_source.py').write_text("raise RuntimeError('must not run')\n")
    path=tmp_path/'readonly.sqlite3'
    with sqlite3.connect(path) as conn:conn.execute('CREATE TABLE listings(id TEXT PRIMARY KEY)')
    before=hashlib.sha256(path.read_bytes()).digest()
    result=subprocess.run([sys.executable,str(PATH),'--db',str(path),'123'],
        env={**os.environ,'PYTHONPATH':str(runtime)},cwd=runtime,capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
    assert not marker.exists()
    assert hashlib.sha256(path.read_bytes()).digest()==before
    value=json.loads(result.stdout)
    assert value['configuration']['application_modules_executed'] is False
    assert 'browser_source.py' in value['runtime_hashes']
