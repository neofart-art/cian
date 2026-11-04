"""Reproducible offline benchmark against immutable previous source, never production."""
import argparse
import asyncio
import contextlib
import cProfile
import http.client
import importlib.util
import json
import os
from pathlib import Path
import pstats
import shutil
import socket
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time

ROOT=Path(__file__).resolve().parents[1]


def worker(source,data,output,label):
    sys.path.insert(0,str(source))
    os.environ.update(DATA_DIR=str(data),APP_USER='owner',APP_PASSWORD='long-test-password',BROWSER_SCHEDULE_ENABLED='false')
    from app import main
    from fastapi.testclient import TestClient
    from tests.helpers import sign_in
    assert Path(main.__file__).resolve().is_relative_to(source)
    spec=importlib.util.spec_from_file_location('benchmark_timers',ROOT/'app/feed.py')
    timers=importlib.util.module_from_spec(spec);spec.loader.exec_module(timers)
    connections=[];connect=sqlite3.connect
    def measured_connect(*a,**kw):
        kw['factory']=timers.TimedConnection
        conn=connect(*a,**kw);connections.append(conn);return conn
    sqlite3.connect=measured_connect
    results={}
    client=TestClient(main.app,base_url='https://testserver')
    with client:
        sign_in(client)
        paths={'all':'/api/state'}
        if label=='after':paths['ours']='/api/state?view=ours'
        for mode,path in paths.items():
            samples=[];sql=[];queries=[]
            for _ in range(5):
                connections.clear();begin=time.perf_counter()
                response=client.get(path,headers={'Accept-Encoding':'identity'})
                samples.append((time.perf_counter()-begin)*1000)
                assert response.status_code==200,response.text[:200]
                sql.append(sum(c.sql_seconds for c in connections)*1000)
                queries.append(sum(c.query_count for c in connections))
            profile=cProfile.Profile()
            profile.enable()
            response=client.get(path,headers={'Accept-Encoding':'identity'})
            profile.disable()
            stats=pstats.Stats(profile).stats
            # Endpoint runs in an AnyIO worker thread: directly profile its
            # Python construction/encoder stages there as a separate sample.
            from fastapi.encoders import jsonable_encoder
            with main.database() as conn:
                begin=time.perf_counter()
                if label=='before':payload={'listings':main.grouped_listing_data(conn)}
                else:payload={'listings':main.feed.summaries(conn,mode,lambda r:main.covers.sources(r,check_path=False))}
                assembled=(time.perf_counter()-begin)*1000
                sql_stage=conn.sql_seconds*1000
            begin=time.perf_counter()
            encoded=jsonable_encoder(payload) if label=='before' else payload
            conversion=(time.perf_counter()-begin)*1000
            begin=time.perf_counter()
            json.dumps(encoded,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()
            serialization=(time.perf_counter()-begin)*1000
            # Profile filesystem resolves separately from the network request.
            fs=cProfile.Profile()
            with main.database() as conn:
                fs.enable()
                if label=='before':main.grouped_listing_data(conn)
                else:main.feed.summaries(conn,mode,lambda r:main.covers.sources(r,check_path=False))
                fs.disable()
            resolve=[v for k,v in pstats.Stats(fs).stats.items() if k[2]=='resolve' and 'pathlib' in k[0]]
            compressed=client.get(path,headers={'Accept-Encoding':'gzip'})
            value={'rows':len(response.json()['listings']),'json_bytes':len(response.content),
                   'wire_gzip_bytes':int(compressed.headers.get('content-length',len(compressed.content))),
                   'content_encoding':compressed.headers.get('content-encoding'),
                   'request_ms_median':round(statistics.median(samples),3),'request_ms_samples':[round(v,3) for v in samples],
                   'sql_ms_median':round(statistics.median(sql),3),'queries':queries[-1],
                   'projection_sql_ms':round(sql_stage,3),'projection_python_ms':round(max(0,assembled-sql_stage),3),
                   'fastapi_conversion_ms':round(conversion,3),'serialization_ms':round(serialization,3),
                   'path_resolve_calls':sum(v[1] for v in resolve),'path_resolve_ms':round(sum(v[3] for v in resolve)*1000,3),
                   'server_timing':compressed.headers.get('server-timing')}
            if label=='after':
                unchanged=client.get(path,headers={'If-None-Match':response.headers['etag']})
                assert unchanged.status_code==304
                value['unchanged_body_bytes']=len(unchanged.content)
            results[mode]=value
        # Actual loopback HTTP body shaping, not a calculated network estimate.
        # Secure session is copied only into the isolated loopback request.
        cookie='; '.join(k+'='+v for k,v in client.cookies.items())
        rate=256*1024
        async def shaped(scope,receive,send):
            async def throttled(message):
                if message['type']=='http.response.start':
                    await asyncio.sleep(.15);await send(message)
                elif message['type']=='http.response.body':
                    body=message.get('body',b'')
                    for start in range(0,len(body),4096):
                        chunk=body[start:start+4096]
                        await asyncio.sleep(len(chunk)/rate)
                        await send({'type':'http.response.body','body':chunk,'more_body':True})
                    if not message.get('more_body'):
                        await send({'type':'http.response.body','body':b'','more_body':False})
                else:await send(message)
            await main.app(scope,receive,throttled)
        import uvicorn
        sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        server=uvicorn.Server(uvicorn.Config(shaped,log_level='error',lifespan='off'))
        thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
        deadline=time.monotonic()+10
        while not server.started:
            if time.monotonic()>deadline:raise RuntimeError('isolated server did not start')
            time.sleep(.01)
        try:
            for mode,path in paths.items():
                connection=http.client.HTTPConnection('127.0.0.1',port,timeout=180)
                begin=time.perf_counter()
                connection.request('GET',path,headers={'Cookie':cookie,'Accept-Encoding':'gzip' if label=='after' else 'identity'})
                wire=connection.getresponse();assert wire.status==200,wire.status
                first=wire.read(1);ttfb=time.perf_counter()-begin
                body=first+wire.read();elapsed=time.perf_counter()-begin;connection.close()
                results[mode]['slow_http']={'rate_bytes_per_second':rate,'latency_ms':150,'ttfb_ms':round(ttfb*1000,3),
                    'total_ms':round(elapsed*1000,3),'wire_bytes':len(body)}
        finally:
            server.should_exit=True;thread.join(timeout=10);sock.close()
    main.covers.close()
    Path(output).write_text(json.dumps(results,ensure_ascii=False,indent=2))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--worker',nargs=4)
    parser.add_argument('--output',default='artifacts/performance.json')
    args=parser.parse_args()
    if args.worker:
        source,data,output,label=args.worker
        worker(Path(source).resolve(),Path(data),output,label);return
    base=os.getenv('XATA_PERF_BASE_ROOT')
    if not base:raise SystemExit('Set XATA_PERF_BASE_ROOT to immutable 21c0d740 checkout')
    sys.path.insert(0,str(ROOT))
    from tests.perf_dataset import seed
    with tempfile.TemporaryDirectory(prefix='xata-benchmark-') as temp:
        folder=Path(temp);data=folder/'seed';data.mkdir()
        os.environ.update(DATA_DIR=str(data),APP_USER='owner',APP_PASSWORD='long-test-password',BROWSER_SCHEDULE_ENABLED='false')
        sys.path.insert(0,str(ROOT))
        from app import main as app
        seed(app,500);app.covers.close()
        result={'baseline':'21c0d7405fd2b849ca7db69057cd4f9bf46e362b','dataset':{'properties':500,'selected':20,
                'price_points_per_property':13,'completed_visits_per_property':3,'comment_revisions_per_property':6},
                'environment':'GitHub Actions ubuntu-latest, synthetic SQLite; not OMV or real mobile hardware'}
        for label,source in [('before',Path(base)),('after',ROOT)]:
            target=folder/label;shutil.copytree(data,target)
            output=folder/(label+'.json')
            subprocess.run([sys.executable,str(Path(__file__).resolve()),'--worker',str(source),str(target),str(output),label],
                           check=True,cwd=source,timeout=240)
            result[label]=json.loads(output.read_text())
        destination=ROOT/args.output;destination.parent.mkdir(parents=True,exist_ok=True)
        destination.write_text(json.dumps(result,ensure_ascii=False,indent=2))
        print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
