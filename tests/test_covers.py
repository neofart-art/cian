"""Cover-cache regressions. No network or Cian credentials are used."""
import importlib
import io
import json
from pathlib import Path
import socket
import threading
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image, UnidentifiedImageError

from app import covers as images
from tests.helpers import sign_in
from tests.test_multiuser import multi_app
from tests.test_phase3 import sample, decide

URL='https://images.cdn-cian.ru/images/flat-1.jpg'
SECOND='https://images.cdn-cian.ru/images/flat-2.jpg'


def picture(color='green',size=(1200,800)):
    stream=io.BytesIO()
    with Image.new('RGB',size,color) as image:
        image.save(stream,format='JPEG')
    return stream.getvalue()


def save(main,item):
    with main.database() as conn:
        return main.save_items(conn,[item],'2026-10-22T10:00:00+00:00')


def row(main,key='9000'):
    with main.database() as conn:
        return dict(conn.execute('SELECT * FROM listings WHERE id=?',(key,)).fetchone())


def item(admin,key='9000'):
    return next(entry for entry in admin.get('/api/state').json()['listings'] if key in entry['listing_ids'])


def seed(main,key='9000',url=URL):
    save(main,dict(sample(key),cover_image_url=url))


def test_new_photo_local_variants_authenticated_api_and_no_redownload(multi_app,monkeypatch):
    main,admin=multi_app
    seed(main)
    calls=[]
    monkeypatch.setattr(images,'download_cover',lambda url:calls.append(url) or picture())
    main.covers.sync_one('9000')
    saved=row(main)
    assert saved['cover_image_url']==saved['cover_image_cached_url']==URL
    paths=[main.covers.path(saved[name]) for name in ('cover_image_path','cover_thumb_path')]
    with Image.open(paths[0]) as normal:
        assert normal.format=='JPEG' and normal.width<=1600 and normal.height<=1200
    with Image.open(paths[1]) as thumb:
        assert thumb.size==(220,160)
    before=[p.stat().st_mtime_ns for p in paths]
    for _ in range(3):
        save(main,dict(sample('9000'),cover_image_url=URL,price=12500000))
        main.covers.sync_one('9000')
    assert calls==[URL]
    assert [p.stat().st_mtime_ns for p in paths]==before
    assert len(list(main.covers.root.glob('*.jpg')))==2
    photo=item(admin)
    for name in ('cover_image_src','cover_thumb_src'):
        assert photo[name].startswith('/api/listings/9000/cover/')
        response=admin.get(photo[name])
        assert response.status_code==200 and response.headers['content-type']=='image/jpeg'
        assert response.headers['cache-control']=='no-store'
        assert response.headers['x-content-type-options']=='nosniff'
    # No arbitrary filesystem mount, no unauthenticated access.
    stranger=TestClient(main.app,base_url='https://testserver')
    assert stranger.get(photo['cover_image_src']).status_code==401
    assert stranger.get('/static/../data/xata.sqlite3').status_code==404
    assert admin.get(photo['cover_image_src'].replace('/normal','/../xata.sqlite3')).status_code!=200
    assert admin.get('/api/listings/9000/cover/'+('0'*24)+'/normal').status_code==404


def test_legacy_record_fills_later_and_migration_repeats(multi_app,monkeypatch):
    main,admin=multi_app
    before=admin.get('/api/state').json()
    assert all(x['cover_thumb_src'] is None for x in before['listings'])
    with main.database() as conn:
        columns={x['name']:x for x in conn.execute('PRAGMA table_info(listings)')}
        for field in ('cover_image_url','cover_image_path','cover_thumb_path','cover_image_cached_url'):
            assert columns[field]['notnull']==0
            assert conn.execute('SELECT count(*) FROM listings WHERE '+field+' IS NOT NULL').fetchone()[0]==0
    save(main,{'id':'123','cover_image_url':URL,'description':'Сохраняется для matcher'})
    monkeypatch.setattr(images,'download_cover',lambda _:picture())
    main.covers.sync_one('123')
    assert item(admin,'123')['cover_thumb_src']
    preserved=row(main,'123')
    importlib.reload(main)
    assert row(main,'123')==preserved
    assert row(main,'123')['first_seen']=='2020-01-01T00:00:00+00:00'
    assert row(main,'123')['description']=='Сохраняется для matcher'


def test_changed_photo_replaces_pair_and_changes_version(multi_app,monkeypatch):
    main,admin=multi_app
    seed(main)
    monkeypatch.setattr(images,'download_cover',lambda url:picture('red' if url==SECOND else 'green'))
    main.covers.sync_one('9000')
    old=row(main);old_src=item(admin)['cover_thumb_src']
    old_paths=[main.covers.path(old[name]) for name in ('cover_image_path','cover_thumb_path')]
    save(main,{'id':'9000','cover_image_url':SECOND,'price':12300000})
    main.covers.sync_one('9000')
    fresh=row(main)
    assert fresh['cover_image_cached_url']==SECOND
    assert item(admin)['cover_thumb_src']!=old_src
    assert not any(path.exists() for path in old_paths)
    assert len(list(main.covers.root.glob('*.jpg')))==2


@pytest.mark.parametrize('failure',[
    images.ImageDownloadError(403),images.ImageDownloadError(404),TimeoutError('private-url?SECRET'),
    ValueError('unsupported'),OSError('disk full'),
])
def test_download_failure_keeps_data_and_old_working_cover(multi_app,monkeypatch,caplog,failure):
    main,admin=multi_app
    seed(main)
    monkeypatch.setattr(images,'download_cover',lambda _:picture())
    main.covers.sync_one('9000')
    before=row(main);old=item(admin)['cover_thumb_src']
    def fail(_):raise failure
    monkeypatch.setattr(images,'download_cover',fail)
    save(main,{'id':'9000','price':11900000,'cover_image_url':SECOND})
    main.covers.sync_one('9000')
    after=row(main)
    assert after['price']==11900000 and after['cover_image_url']==SECOND
    assert after['cover_image_cached_url']==URL
    assert item(admin)['cover_thumb_src']==old and admin.get(old).status_code==200
    for field in ('cover_image_path','cover_thumb_path'):
        assert after[field]==before[field] and main.covers.path(after[field]).is_file()
    assert 'cover update skipped' in caplog.text
    assert 'SECRET' not in caplog.text and 'cdn-cian.ru' not in caplog.text


def test_bad_image_missing_file_and_write_failure_are_recoverable(multi_app,monkeypatch):
    main,admin=multi_app
    seed(main)
    monkeypatch.setattr(images,'download_cover',lambda _:b'<html>CAPTCHA NOT AN IMAGE</html>')
    main.covers.sync_one('9000')
    assert row(main)['cover_image_path'] is None
    monkeypatch.setattr(images,'download_cover',lambda _:picture())
    main.covers.sync_one('9000')
    normal=main.covers.path(row(main)['cover_image_path']);normal.unlink()
    main.covers.sync_one('9000')
    assert normal.is_file()
    before=row(main)
    save(main,{'id':'9000','cover_image_url':SECOND})
    def disk_full(*args):raise OSError('disk full')
    monkeypatch.setattr(main.covers,'_write',disk_full)
    main.covers.sync_one('9000')
    assert row(main)['cover_image_path']==before['cover_image_path']
    assert normal.exists()


def test_stale_download_never_overwrites_new_requested_url(multi_app,monkeypatch):
    main,admin=multi_app
    seed(main)
    def changed(_):
        save(main,{'id':'9000','cover_image_url':SECOND})
        return picture()
    monkeypatch.setattr(images,'download_cover',changed)
    main.covers.sync_one('9000')
    assert row(main)['cover_image_url']==SECOND and row(main)['cover_image_path'] is None
    assert not list(main.covers.root.glob('*.jpg'))
    monkeypatch.setattr(images,'download_cover',lambda _:picture('red'))
    main.covers.sync_one('9000')
    assert row(main)['cover_image_cached_url']==SECOND


def test_relisting_reuses_same_url_and_keeps_one_cover_per_property(multi_app,monkeypatch):
    main,admin=multi_app
    seed(main)
    calls=[]
    monkeypatch.setattr(images,'download_cover',lambda url:calls.append(url) or picture())
    main.covers.sync_one('9000')
    with main.database() as conn:
        main.save_items(conn,[dict(sample('9001'),cover_image_url=URL)],'2026-10-23T10:00:00+00:00')
    main.covers.sync_one('9001')
    assert calls==[URL]
    assert row(main)['cover_image_path'] is None
    assert item(admin,'9001')['cover_image_src'].startswith('/api/listings/9001/')
    assert len(list(main.covers.root.glob('*.jpg')))==2


def test_current_without_photo_uses_existing_property_cover(multi_app,monkeypatch):
    main,admin=multi_app
    seed(main)
    monkeypatch.setattr(images,'download_cover',lambda _:picture())
    main.covers.sync_one('9000')
    old=item(admin)['cover_thumb_src']
    with main.database() as conn:
        main.save_items(conn,[sample('9001')],'2026-10-23T10:00:00+00:00')
    assert item(admin,'9001')['cover_thumb_src']==old


def test_csv_preview_schedules_no_photo_and_confirm_runs_after_commit(multi_app,monkeypatch):
    main,admin=multi_app
    seed(main)
    queued=[]
    def enqueue(ids):
        # A second connection can see committed imports; no image work under
        # the SQLite writer transaction or preview savepoint.
        assert row(main)['price']==11000000
        queued.extend(ids)
    monkeypatch.setattr(main.covers,'enqueue',enqueue)
    preview=admin.post('/api/import/preview',content=b'url;price\nhttps://cian.ru/sale/flat/9000/;11000000').json()
    assert queued==[] and row(main)['cover_image_path'] is None
    assert admin.post('/api/import/confirm',json={'token':preview['token']}).status_code==200
    assert queued==['9000']
    assert row(main)['cover_image_url']==URL


def test_cover_scheduling_error_never_turns_collection_into_error(multi_app,monkeypatch):
    main,admin=multi_app
    monkeypatch.setattr(main.CianBrowserSource,'collect',lambda self,filters:[dict(sample('9000'),cover_image_url=URL)])
    def fail(_):raise RuntimeError('worker unavailable')
    monkeypatch.setattr(main.covers,'enqueue',fail)
    with main.database() as conn:
        run=conn.execute("INSERT INTO runs(started,status,filters,source) VALUES ('2026','running','{}','browser')").lastrowid
    main.lock.acquire()
    main.run_collection(run,{'max_price':14500000,'metro_minutes':20},'browser')
    with main.database() as conn:
        assert conn.execute('SELECT status FROM runs WHERE id=?',(run,)).fetchone()[0]=='done'
    assert main.browser_state()['status']=='authorized'
    assert row(main)['cover_image_url']==URL


def test_worker_failure_does_not_stop_following_cover(multi_app,monkeypatch):
    main,admin=multi_app
    seed(main)
    save(main,dict(sample('9002',address='Москва, ул. Другая, д. 99'),cover_image_url=SECOND))
    def download(url):
        if url==URL:raise TimeoutError('no photo')
        return picture()
    monkeypatch.setattr(images,'download_cover',download)
    main.covers.enqueue(['9000','9002'])
    deadline=time.monotonic()+5
    while main.covers.jobs.unfinished_tasks and time.monotonic()<deadline:
        time.sleep(.01)
    assert main.covers.jobs.unfinished_tasks==0
    assert row(main)['cover_image_path'] is None
    assert row(main,'9002')['cover_image_path']


@pytest.mark.parametrize('url',[
    'http://images.cdn-cian.ru/a.jpg','https://images.cdn-cian.ru.evil.test/a.jpg',
    'https://evil.test/a.jpg','https://127.0.0.1/a.jpg','http://169.254.169.254/latest/',
    'file:///etc/passwd','https://user:password@images.cdn-cian.ru/a.jpg',
    'https://images.cdn-cian.ru:444/a.jpg','https://images.cdn-cian.ru/a.jpg#fragment',
    'https://images.cdn-cian.ru/a.jpg\r\nCookie: secret',
])
def test_cover_url_rejects_arbitrary_sources(url):
    assert images.cover_url(url) is None


@pytest.mark.parametrize('address',['127.0.0.1','10.0.0.1','169.254.169.254','::1','fd00::1','192.0.2.1'])
def test_dns_private_or_reserved_addresses_rejected(monkeypatch,address):
    monkeypatch.setattr(images.socket,'getaddrinfo',lambda *args,**kwargs:[(socket.AF_INET,socket.SOCK_STREAM,6,'',(address,443))])
    with pytest.raises(ValueError):
        images.public_address('images.cdn-cian.ru')


def test_pinned_connection_uses_checked_ip_and_original_tls_host(monkeypatch):
    calls=[]
    class Sock:
        def settimeout(self,value):calls.append(('timeout',value))
        def close(self):pass
    class Context:
        def wrap_socket(self,sock,server_hostname):
            calls.append(('tls',server_hostname));return sock
    monkeypatch.setattr(images.socket,'create_connection',lambda addr,timeout:calls.append(('connect',addr)) or Sock())
    connection=images.PinnedHTTPSConnection('images.cdn-cian.ru','93.184.216.34')
    connection._context=Context();connection.connect();connection.close()
    assert ('connect',('93.184.216.34',443)) in calls
    assert ('tls','images.cdn-cian.ru') in calls


@pytest.mark.parametrize('status',[301,302,403,404])
def test_downloader_rejects_redirects_and_errors_without_following(monkeypatch,status):
    requests=[]
    class Response:
        def __init__(self):self.status=status
    class Connection:
        def __init__(self,*args):pass
        def request(self,method,path,headers):requests.append((method,path,headers))
        def getresponse(self):return Response()
        def close(self):pass
    monkeypatch.setattr(images,'public_address',lambda _: '93.184.216.34')
    monkeypatch.setattr(images,'PinnedHTTPSConnection',Connection)
    with pytest.raises(images.ImageDownloadError):
        images.download_cover(URL)
    assert len(requests)==1
    assert not any(key.lower() in ('cookie','authorization') for key in requests[0][2])


def test_downloader_limits_body_bytes_and_does_not_trust_mime(monkeypatch):
    class Response:
        status=200
        def getheader(self,name,default=None):
            return {'Content-Type':'image/jpeg','Content-Length':str(images.MAX_BYTES+1)}.get(name,default)
    class Connection:
        def __init__(self,*args):pass
        def request(self,*args,**kwargs):pass
        def getresponse(self):return Response()
        def close(self):pass
    monkeypatch.setattr(images,'public_address',lambda _:'93.184.216.34')
    monkeypatch.setattr(images,'PinnedHTTPSConnection',Connection)
    with pytest.raises(ValueError):
        images.download_cover(URL)
    with pytest.raises(UnidentifiedImageError):
        images.resize_cover(b'<svg onload="alert(1)"></svg>')


def test_oversized_image_pixels_are_rejected(monkeypatch):
    monkeypatch.setattr(images,'MAX_PIXELS',10)
    with pytest.raises(ValueError):
        images.resize_cover(picture(size=(20,20)))


def test_local_image_paths_cannot_escape_data_directory(multi_app):
    main,admin=multi_app
    assert main.covers.path('../../xata.sqlite3') is None
    assert main.covers.path('/etc/passwd') is None
    main.covers.root.mkdir(parents=True)
    outside=main.DATA/'xata.sqlite3'
    relative='images/listings/9000-'+('a'*24)+'-normal.jpg'
    (main.DATA/relative).symlink_to(outside)
    assert main.covers.path(relative) is None
