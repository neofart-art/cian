"""Read-only targeted Cian IDs. No app.main import, migration, collection or navigation."""
import argparse
from contextlib import closing
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import urllib.error
import urllib.request
from urllib.parse import parse_qs,urlsplit,urlencode

SAFE_PARAMS={'deal_type','engine_version','offer_type','region','room1','object_type[0]','maxprice','foot_min','only_foot','p'}
EXPECTED={'deal_type':'sale','offer_type':'flat','region':'1','room1':'1','object_type[0]':'1','only_foot':'2'}
ENUMS={'sale','rent','flat','house','room','commercial','suburban','newbuilding'}


def validate_ids(values):
    result=[]
    for value in values:
        if not isinstance(value,str) or not re.fullmatch(r'[0-9]{1,32}',value):
            raise ValueError('Use numeric Cian IDs')
        value=str(int(value))
        if value not in result:result.append(value)
    if not 1<=len(result)<=20:raise ValueError('Use 1 to 20 IDs')
    return result


def public_query(url):
    try:
        parsed=urlsplit(url);query=parse_qs(parsed.query,max_num_fields=200)
        safe={k:v for k,v in query.items() if k in SAFE_PARAMS and all(
            value in ENUMS if k in ('deal_type','offer_type') else bool(re.fullmatch(r'[0-9]{1,12}',value)) for value in v)}
        host=(parsed.hostname or '').lower()
        return {'valid_cian_https':len(url)<=4096 and parsed.scheme=='https' and not parsed.username and not parsed.password and
                parsed.port in (None,443) and (host=='cian.ru' or host.endswith('.cian.ru')),
                'public_parameters':safe,'has_unreported_parameters':any(k not in safe for k in query),
                'explicit_conflicts':[k for k,v in EXPECTED.items() if k in safe and safe[k]!=[v]],
                'unconfirmed_constraints':[k for k in EXPECTED if k not in safe]}
    except (ValueError,TypeError):
        return {'valid_cian_https':False,'public_parameters':{},'has_unreported_parameters':True,
                'explicit_conflicts':[],'unconfirmed_constraints':list(EXPECTED)}


def configuration():
    # Reference policy only: never execute production application modules.
    # Even an app/__init__.py with startup side effects must remain untouched.
    override=os.getenv('CIAN_SEARCH_URL','')
    observed=public_query(override) if override else None
    criteria={'max_price':14500000,'metro_minutes':20}
    if observed and observed['valid_cian_https']:
        params=observed['public_parameters']
        for key,field,low,high in [('maxprice','max_price',100000,1000000000),('foot_min','metro_minutes',1,60)]:
            values=params.get(key,[])
            if len(values)==1 and values[0].isascii() and values[0].isdigit() and low<=int(values[0])<=high:
                criteria[field]=int(values[0])
    query={'deal_type':'sale','engine_version':2,'offer_type':'flat','region':1,
           'room1':1,'object_type[0]':1,'maxprice':criteria['max_price'],'foot_min':criteria['metro_minutes'],'only_foot':2}
    return {'application_modules_executed':False,'interface_criteria':criteria,'override_present':bool(override),
            'effective_request':observed if override else public_query('https://www.cian.ru/cat.php?'+urlencode(query)),
            'derivation':'CIAN_SEARCH_URL from runtime env' if override else 'default URL from current repository reference policy',
            'post_extraction_filter':'reference: price is absent OR price <= max_price',
            'fixed_interface_labels':['Moscow','secondary','one_room'],
            'caveat':'Runtime source hashes/local modifications and saved-search visible filters require verification; no application code executed.'}


def runtime_hashes():
    result={}
    # Locate files without import app, importlib.find_spec or executing __init__.
    locations=[Path.cwd()]+[Path(value) for value in sys.path if isinstance(value,str) and value]
    for location in locations:
        folder=location/'app'
        if not (folder/'browser_source.py').is_file():continue
        for name in ('__init__.py','main.py','browser_source.py','source.py','criteria.py','static/review-state.js'):
            path=folder/name
            if path.is_file():
                try:result[name]=hashlib.sha256(path.read_bytes()).hexdigest()
                except OSError:pass
        break
    return result


class Reader:
    def __init__(self,conn):
        self.conn=conn
        self.tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.columns={table:{r[1] for r in conn.execute('PRAGMA table_info("'+table.replace('"','""')+'")')}
                      for table in self.tables}

    def find(self,table,fields,where,values):
        if table not in self.tables or not set(where)<=self.columns[table]:return []
        selected=[field for field in fields if field in self.columns[table]]
        if not selected:return []
        return [dict(r) for r in self.conn.execute('SELECT '+','.join(selected)+' FROM '+table+
                ' WHERE '+' OR '.join(field+'=?' for field in where),values)]


def trace(reader,key):
    listing=reader.find('listings',['id','property_id','first_seen','last_seen','last_imported','source'],['id'],[key])
    history=reader.find('price_history',['listing_id','property_id'],['listing_id'],[key])
    candidates=reader.find('duplicate_candidates',['id','listing_id','candidate_property_id','decision'],['listing_id'],[key])
    decisions=reader.find('identity_decisions',['listing_a','listing_b','decision'],['listing_a','listing_b'],[key,key])
    audit=reader.find('property_merge_audit',['listing_id','source_property_id','target_property_id','merged_at'],['listing_id'],[key])
    pointers=reader.find('properties',['id','merged_into','current_listing_id'],['current_listing_id'],[key])
    result={'id':key,'stored_listing':listing[0] if listing else None,'price_history_rows':len(history),
            'duplicate_candidates':candidates,'manual_decisions':decisions,'merge_audit':audit,
            'current_property_pointers':pointers,'properties':[],
            'stages':{'search_result':'not_recorded','extraction':'not_recorded','parameter_check':'not_recorded',
                      'import':'stored_listing_exists' if listing else 'no_listing_row_in_this_snapshot'},
            'caveats':['UI locations predict current repository rules without the client search/status filters.',
                       'Missing rows do not establish the historical collector failure stage.']}
    pids={r['property_id'] for r in listing+history if r.get('property_id') is not None}
    pids.update(r['id'] for r in pointers)
    pids.update(r[field] for r in audit for field in ('source_property_id','target_property_id') if r.get(field) is not None)
    roots={}
    for pid in sorted(pids):
        chain=[];seen=set();prop=None;problem=None
        while pid is not None:
            if pid in seen:problem='merge_cycle';break
            seen.add(pid);chain.append(pid)
            rows=reader.find('properties',['id','merged_into','current_listing_id','in_our_list','viewing_documents_ready'],['id'],[pid])
            if not rows:problem='property_missing_or_legacy_schema';break
            prop=rows[0]
            if prop.get('merged_into') is None:break
            pid=prop['merged_into']
        if problem:
            result['properties'].append({'chain':chain,'integrity_issue':problem});continue
        current=reader.find('listings',['id','property_id','review_status','realtor_status'],['id'],[prop.get('current_listing_id')])
        current=current[0] if current else None
        if current is None or current.get('property_id')!=prop['id']:
            result['properties'].append({'chain':chain,'integrity_issue':'current_publication_missing_or_wrong_owner'});continue
        if prop['id'] in roots:
            roots[prop['id']]['merge_chains'].append(chain);continue
        publications=reader.find('listings',['id','first_seen','last_seen'],['property_id'],[prop['id']])
        visits=reader.find('property_visits',['id','status','outcome','completed_at'],['property_id'],[prop['id']])
        completed=[v for v in visits if v.get('status')=='completed' and v.get('completed_at')]
        invalid_dates=False
        try:latest=max(completed,key=lambda v:(datetime.fromisoformat(v['completed_at']),v['id']),default=None)
        except (ValueError,TypeError):latest=None;invalid_dates=True
        refused=current.get('review_status')=='rejected' or current.get('realtor_status')=='rejected'
        group=('documents' if prop.get('viewing_documents_ready') else 'liked') if latest and latest.get('outcome')=='liked' else (
            'rejected' if latest and latest.get('outcome')=='rejected' else 'planned')
        entry={'merge_chains':[chain],'root_property_id':prop['id'],'current_listing_id':current['id'],
            'target_is_historical':key!=current['id'],
            'publication_ids':[r['id'] for r in sorted(publications,key=lambda r:(r.get('first_seen') or '',r['id']))],
            'family_status':current.get('review_status'),'realtor_status':current.get('realtor_status'),
            'selected':bool(prop.get('in_our_list')),'latest_completed':latest,'viewing_dates_valid':not invalid_dates,
            'expected_all_section':'refused' if refused else 'main','all_initially_collapsed':refused,
            'expected_ours_section':None if not prop.get('in_our_list') else 'unconfirmed' if invalid_dates else group,
            'ours_initially_collapsed':None if invalid_dates else bool(prop.get('in_our_list')) and group=='rejected',
            'target_id_search_matches_publications':any(r['id']==key for r in publications)}
        roots[prop['id']]=entry;result['properties'].append(entry)
    if not listing and (history or audit or pointers):result['stages']['import']='historical_references_only'
    return result


def current_dom(keys,service_url,token):
    if len(token)<16:return {'available':False,'reason':'internal_token_unavailable'}
    try:
        parts=urlsplit(service_url)
        if parts.scheme not in ('http','https') or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError()
        payload={'ids':keys}
        budget=configuration().get('interface_criteria',{}).get('max_price')
        if budget is not None:payload['max_price']=budget
        request=urllib.request.Request(service_url.rstrip('/')+'/diagnostics/dom',data=json.dumps(payload).encode(),method='POST',
            headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs):return None
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
        with opener.open(request,timeout=15) as response:
            raw=response.read(128*1024+1)
            if len(raw)>128*1024:raise ValueError()
            result=json.loads(raw)
            if not isinstance(result,dict) or result.get('mode')!='current_dom_read_only':raise ValueError()
        return {'available':True,'evidence':result,'scope':'current page, not historical collection'}
    except urllib.error.HTTPError as exc:
        return {'available':False,'http_status':exc.code,'reason':'endpoint_not_installed' if exc.code==404 else 'busy_or_rejected'}
    except Exception as exc:
        return {'available':False,'reason':type(exc).__name__}  # Never echo URLs/errors containing credentials.


def diagnose(path,keys,with_dom=False):
    keys=validate_ids(keys)
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=3)) as conn:
        conn.row_factory=sqlite3.Row
        conn.execute('PRAGMA query_only=ON');conn.execute('BEGIN')
        reader=Reader(conn)
        report={'mode':'read_only_no_collection','database':Path(path).name,'configuration':configuration(),
                'runtime_hashes':runtime_hashes(),'schema_tables_checked':sorted(reader.tables&{'listings','properties',
                    'price_history','property_visits','duplicate_candidates','identity_decisions','property_merge_audit'}),
                'listings':[trace(reader,key) for key in keys],'database_snapshot_checked':True,
                'collector_history_verified':False,'database_scope':'supplied SQLite; production identity is not inferred'}
        conn.rollback()
    report['current_dom']=current_dom(keys,os.getenv('BROWSER_SERVICE_URL','http://browser:8010'),
        os.getenv('BROWSER_API_TOKEN') or os.getenv('APP_PASSWORD','')) if with_dom else {'available':False,'reason':'not_requested'}
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('ids',nargs='+')
    parser.add_argument('--db',default=str(Path(os.getenv('DATA_DIR','/data'))/'xata.sqlite3'))
    parser.add_argument('--current-dom',action='store_true',help='Inspect already open page only; never collect')
    args=parser.parse_args()
    try:print(json.dumps(diagnose(args.db,args.ids,args.current_dom),ensure_ascii=False,indent=2))
    except Exception as exc:
        print('Read-only diagnostics failed: '+type(exc).__name__,file=sys.stderr);raise SystemExit(1)

if __name__=='__main__':main()
