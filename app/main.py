import io
import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
from contextlib import contextmanager, asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response, RedirectResponse, JSONResponse, HTMLResponse
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from openpyxl import Workbook
from pydantic import BaseModel, Field, ConfigDict, field_validator
from typing import Literal
from app.auth import AuthStore, UNSAFE
from app.migrations import migrate_listings
from app.property_match import (best_match, candidate_rows, create_property, record_price,
                                sync_property_reviews, touch_property, listing_match, identity_revision, bump_identity,
                                root_property, distinct_properties, save_decision, merge_properties, AUTO_MATCH_MARGIN)
from app.criteria import browser_criteria
from app.finance import mortgage_payment
from app.covers import CoverCache, cover_url
from app import viewings
from app import source
from app.csv_source import CianCsvSource, MAX_BYTES
from app.browser_source import CianBrowserSource, BrowserAttention

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('xata')
DATA = Path(os.getenv('DATA_DIR', 'data'))
DATA.mkdir(parents=True, exist_ok=True)
DB = DATA / 'xata.sqlite3'
lock = threading.Lock()


@contextmanager
def database():
    conn = sqlite3.connect(DB, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    try:
        with conn:
            yield conn
    finally:
        conn.close()


with database() as conn:
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS listings (id TEXT PRIMARY KEY, title TEXT, price INTEGER, address TEXT, url TEXT, first_seen TEXT, last_seen TEXT);
    CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, started TEXT, finished TEXT, status TEXT, message TEXT, filters TEXT, count INTEGER DEFAULT 0);
    UPDATE runs SET status='error', message='Процесс был перезапущен во время сбора' WHERE status='running';
    ''')

# Additive migration: existing volumes and first_seen values remain intact.
with database() as conn:
    columns = {row['name'] for row in conn.execute('PRAGMA table_info(listings)')}
    for name, kind in {'complex_name': 'TEXT', 'area': 'REAL', 'floor': 'TEXT',
                       'metro': 'TEXT', 'metro_time': 'TEXT',
                       'source': "TEXT DEFAULT 'ЦИАН online'", 'last_imported': 'TEXT',
                       'card_text': 'TEXT', 'description': 'TEXT', 'metro_walk_minutes': 'INTEGER'}.items():
        if name not in columns:
            conn.execute(f'ALTER TABLE listings ADD COLUMN {name} {kind}')
    conn.execute('''CREATE TABLE IF NOT EXISTS import_previews
        (token TEXT PRIMARY KEY, created REAL NOT NULL, payload TEXT NOT NULL)''')


with database() as conn:
    run_columns = {row['name'] for row in conn.execute('PRAGMA table_info(runs)')}
    if 'source' not in run_columns:
        conn.execute("ALTER TABLE runs ADD COLUMN source TEXT DEFAULT 'online'")
    conn.execute('CREATE TABLE IF NOT EXISTS browser_state (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)')
    conn.execute('INSERT OR IGNORE INTO browser_state VALUES (1, ?)', (json.dumps({
        'status': 'login_required', 'message': 'Требуется вход', 'last_success': None,
        'new_count': 0, 'last_error': None, 'manual_access': False,
    }),))


security = AuthStore(database)
security.migrate()
auth = security.current_user
admin = security.admin_dependency()

# User-owned review fields never participate in LISTING_FIELDS/save_items.
with database() as conn:
    columns = {row['name'] for row in conn.execute('PRAGMA table_info(listings)')}
    for name, kind in {
        'review_status': "TEXT NOT NULL DEFAULT 'new' CHECK(review_status IN ('new','liked','viewed','rejected'))",
        'review_updated_at': 'TEXT',
        'review_updated_by': 'INTEGER REFERENCES users(id)',
        'review_version': 'INTEGER NOT NULL DEFAULT 0',
    }.items():
        if name not in columns:
            conn.execute(f'ALTER TABLE listings ADD COLUMN {name} {kind}')
    conn.execute("""CREATE INDEX IF NOT EXISTS listings_review_order ON listings(
        CASE review_status WHEN 'new' THEN 0 WHEN 'viewed' THEN 1 ELSE 2 END,first_seen DESC,id)""")

migrate_listings(database)
covers = CoverCache(DATA,database)

LISTING_QUERY = '''SELECT l.*,u.display_name AS review_updated_by_name,u.username AS review_updated_by_username,
                          r.display_name AS realtor_updated_by_name,r.username AS realtor_updated_by_username
                   FROM listings l LEFT JOIN users u ON u.id=l.review_updated_by
                   LEFT JOIN users r ON r.id=l.realtor_updated_by'''
LISTING_ORDER = " ORDER BY CASE l.review_status WHEN 'new' THEN 0 WHEN 'liked' THEN 1 WHEN 'viewed' THEN 2 ELSE 3 END,l.first_seen DESC,l.id"


def listing_data(row):
    # Preserve unknown database columns during migrations without exposing them
    # through the public listing API (including possible non-UTF8 BLOB values).
    fields = ('id', *LISTING_FIELDS, 'first_seen', 'last_seen', 'last_imported', 'property_id',
              'cover_image_path','cover_thumb_path','cover_image_cached_url',
              'review_status', 'review_updated_at', 'review_updated_by', 'review_version',
              'review_updated_by_name', 'review_updated_by_username',
              'realtor_status', 'realtor_comment', 'realtor_updated_at', 'realtor_updated_by', 'realtor_version',
              'realtor_updated_by_name', 'realtor_updated_by_username')
    item = {field: row[field] for field in fields}
    item['mortgage_payment'] = mortgage_payment(item['price'])
    return item


def property_context(conn):
    """Batch reads: /api/state does not execute queries per property row."""
    rows = conn.execute(LISTING_QUERY).fetchall()
    props = {row['id']:dict(row) for row in conn.execute('SELECT * FROM properties')}
    by_id = {row['id']:row for row in rows}
    groups, prices = {}, {}
    for row in rows:
        groups.setdefault(row['property_id'], []).append(row)
    for entries in groups.values():
        entries.sort(key=lambda row: (row['first_seen'] or '', row['id']))
    for row in conn.execute('SELECT listing_id,property_id,observed_at,price FROM price_history ORDER BY observed_at,id'):
        prices.setdefault(row['property_id'], []).append(dict(row))
    def root(pid):
        seen = set()
        while pid in props and props[pid]['merged_into'] is not None and pid not in seen:
            seen.add(pid)
            pid = props[pid]['merged_into']
        return pid
    negative = {tuple(sorted((row[0],row[1]))) for row in conn.execute('''SELECT a.property_id,b.property_id
        FROM identity_decisions d JOIN listings a ON a.id=d.listing_a JOIN listings b ON b.id=d.listing_b
        WHERE d.decision='different' ''')}
    pending = {}
    for dc in conn.execute("SELECT * FROM duplicate_candidates WHERE decision='pending' ORDER BY confidence DESC,id"):
        source_row = by_id.get(dc['listing_id'])
        if source_row is None:
            continue
        source, target = source_row['property_id'], root(dc['candidate_property_id'])
        target_row = by_id.get(props.get(target,{}).get('current_listing_id'))
        if source == target or target_row is None or tuple(sorted((source,target))) in negative:
            continue
        choice = {'id':dc['id'],'listing_id':dc['listing_id'],'confidence':dc['confidence'],
                  'source_property_id':source,'candidate_property_id':target,
                  'candidate_listing_id':target_row['id'],'candidate_url':target_row['url']}
        if not any(item['candidate_property_id']==target for item in pending.get(source,[])):
            pending.setdefault(source,[]).append(choice)
    return {'rows':rows,'props':props,'groups':groups,'prices':prices,'pending':pending,
            'revision':identity_revision(conn),'by_id':by_id,'visits':viewings.context(conn)}


def property_listing_data(conn, row, context=None):
    context = context or property_context(conn)
    item = listing_data(row)
    pid = item['property_id']
    history = context['groups'].get(pid, [row])
    current = context['props'][pid]['current_listing_id']
    # The current publication owns the cover. While it is being downloaded,
    # retain a usable older cover from this same property.
    cached = row if row['cover_image_path'] and row['cover_thumb_path'] else next(
        (entry for entry in reversed(history) if entry['cover_image_path'] and entry['cover_thumb_path']),row)
    item.update(covers.sources(cached))
    item['listing_ids'] = [entry['id'] for entry in history]
    item['relist_count'] = max(0,len(history)-1)
    item['publication_history'] = [
        {'id':entry['id'],'first_seen':entry['first_seen'],'last_seen':entry['last_seen'],
         'status':'current' if entry['id']==current else 'historical'} for entry in history]
    dates = [entry['first_seen'] for entry in history if entry['first_seen']]
    item['first_seen'] = min(dates) if dates else item['first_seen']
    prices = context['prices'].get(pid,[])
    item['price_history'] = prices
    item['price_min'] = min(entry['price'] for entry in prices) if prices else item['price']
    item['price_max'] = max(entry['price'] for entry in prices) if prices else item['price']
    item['duplicate_candidate'] = next(iter(context['pending'].get(pid,[])),None)
    item['identity_revision'] = context['revision']
    item['visits'] = context['visits'].get(pid, [])
    item['visits_version'] = context['props'][pid]['visits_version']
    return item


def current_property_row(conn, property_id):
    property_id = root_property(conn,property_id)
    return conn.execute(LISTING_QUERY + ''' JOIN properties p ON p.current_listing_id=l.id
                        WHERE p.id=? AND l.property_id=p.id''',(property_id,)).fetchone()


def grouped_listing_data(conn):
    context = property_context(conn)
    items = [property_listing_data(conn,context['by_id'][prop['current_listing_id']],context)
             for prop in context['props'].values()
             if prop['merged_into'] is None and prop['current_listing_id'] in context['by_id']]
    items.sort(key=lambda item:item.get('first_seen') or '',reverse=True)
    rank = {'new':0,'liked':1,'viewed':2,'rejected':3}
    items.sort(key=lambda item:rank.get(item.get('review_status'),9))
    return items


def browser_state():
    with database() as conn:
        return json.loads(conn.execute('SELECT payload FROM browser_state WHERE id=1').fetchone()[0])


def update_browser(**changes):
    with database() as conn:
        conn.execute('BEGIN IMMEDIATE')
        state = json.loads(conn.execute('SELECT payload FROM browser_state WHERE id=1').fetchone()[0])
        state.update(changes)
        conn.execute('UPDATE browser_state SET payload=? WHERE id=1', (json.dumps(state, ensure_ascii=False),))


LISTING_FIELDS = ('title', 'price', 'address', 'url', 'complex_name', 'area', 'floor', 'metro', 'metro_time', 'source', 'card_text', 'description', 'metro_walk_minutes', 'cover_image_url')


def save_items(conn, items, now, imported=False):
    added = updated = relisted = possible_duplicates = 0
    items = list(items)
    incoming_ids = {str(item['id']) for item in items}
    # Freeze the old candidate set once. Two unknown ads in the same batch must
    # not silently collapse, and large CSVs must not rescan the DB per row.
    candidates = candidate_rows(conn,incoming_ids)
    coobserved = {row['property_id'] for row in conn.execute('SELECT id,property_id FROM listings') if str(row['id']) in incoming_ids}
    claimed_targets = set()
    for item in items:
        if 'cover_image_url' in item:
            item = {**item,'cover_image_url':cover_url(item['cover_image_url'])}
        previous = conn.execute('SELECT * FROM listings WHERE id=?',(item['id'],)).fetchone()
        merged = dict(previous) if previous else {'title':'Квартира'}
        merged.update({key:item[key] for key in LISTING_FIELDS if item.get(key) not in (None,'')})
        if item.get('metro_time'):
            merged['metro_walk_minutes'] = item.get('metro_walk_minutes')
        values = [merged.get(field) for field in LISTING_FIELDS]
        if previous:
            pid = previous['property_id']
            conn.execute('UPDATE listings SET ' + ','.join(f'{field}=?' for field in LISTING_FIELDS) +
                         ',last_seen=?,last_imported=? WHERE id=?',
                         (*values,now,now if imported else previous['last_imported'],item['id']))
            current = conn.execute('SELECT current_listing_id FROM properties WHERE id=?',(pid,)).fetchone()[0]
            if current == item['id']:
                touch_property(conn,pid,merged,now)
            if item.get('price') is not None:
                record_price(conn,pid,item['id'],now,item['price'])
            updated += 1
            continue

        match = best_match(merged,candidates)
        automatic = bool(match and match[2].auto and match[0] not in claimed_targets and match[0] not in coobserved)
        if automatic:
            pid = match[0]
            claimed_targets.add(pid)
            relisted += 1
        else:
            pid = create_property(conn,merged,now)
            added += 1
        fields = ('id',*LISTING_FIELDS,'first_seen','last_seen','last_imported','property_id')
        conn.execute('INSERT INTO listings ('+','.join(fields)+') VALUES ('+','.join('?' for _ in fields)+')',
                     (item['id'],*values,now,now,now if imported else None,pid))
        conn.execute('UPDATE properties SET current_listing_id=? WHERE id=?',(item['id'],pid))
        touch_property(conn,pid,merged,now)
        record_price(conn,pid,item['id'],now,merged.get('price'))
        if automatic:
            sync_property_reviews(conn,pid)
        elif match and match[2].possible:
            # Retain competing candidates. The browser acts on the exact candidate
            # shown, never on whichever row happens to be first at POST time.
            for candidate_pid,candidate_item in candidates:
                result = listing_match(merged,candidate_item)
                if result.possible and result.score >= match[2].score - AUTO_MATCH_MARGIN:
                    conn.execute('''INSERT OR IGNORE INTO duplicate_candidates(
                        listing_id,candidate_property_id,confidence,reasons,created_at) VALUES (?,?,?,?,?)''',
                        (item['id'],candidate_pid,min(result.score,84),','.join(result.reasons),now))
            possible_duplicates += 1
        bump_identity(conn)
    return {'added':added,'updated':updated,'relisted':relisted,'possible_duplicates':possible_duplicates}


def schedule_covers(listing_ids):
    try:
        ids = {str(value) for value in listing_ids}
        if not ids:
            return
        with database() as conn:
            property_ids = {row['property_id'] for row in conn.execute('SELECT id,property_id FROM listings') if str(row['id']) in ids}
            preferred = []
            for row in conn.execute('''SELECT l.id,l.property_id,p.current_listing_id FROM listings l JOIN properties p ON p.id=l.property_id
                                       WHERE l.cover_image_url IS NOT NULL ORDER BY l.first_seen DESC,l.rowid DESC'''):
                if row['property_id'] in property_ids:
                    preferred.append((row['property_id'],row['id'],row['id']==row['current_listing_id']))
        selected = {}
        for pid,listing_id,current in preferred:
            if pid not in selected or current:
                selected[pid] = listing_id
        covers.enqueue(selected.values())

    except Exception as exc:
        log.warning('cover scheduling skipped error_type=%s',type(exc).__name__)


@asynccontextmanager
async def lifespan(app):
    stop = threading.Event()
    worker = threading.Thread(target=browser_scheduler, args=(stop,), daemon=True)
    worker.start()
    try:
        yield
    finally:
        stop.set()
        await run_in_threadpool(worker.join, 2)
        await run_in_threadpool(covers.close)


app = FastAPI(title='Хата — поиск квартир', docs_url=None, redoc_url=None, lifespan=lifespan)
app.include_router(security.router())


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    # FastAPI otherwise echoes invalid input, which may contain a new password.
    return JSONResponse({'detail': [{key: error[key] for key in ('loc', 'msg', 'type')} for error in exc.errors()]}, status_code=422)


class Filters(BaseModel):
    max_price: int = Field(default=14000000, ge=100000, le=1000000000)
    metro_minutes: int = Field(default=20, ge=1, le=60)


@app.get('/healthz')
def health():
    with database() as conn:
        conn.execute('SELECT 1').fetchone()
    return {'status': 'ok'}


@app.get('/api/listings/{listing_id}/cover/{version}/{size}')
def listing_cover(listing_id: str, version: str, size: Literal['normal','thumb'], user=Depends(auth)):
    if not re.fullmatch(r'[a-f0-9]{24}',version):
        raise HTTPException(404,'Фото отсутствует')
    with database() as conn:
        row = conn.execute('SELECT * FROM listings WHERE id=?',(listing_id,)).fetchone()
    if row is None:
        raise HTTPException(404,'Фото отсутствует')
    expected = covers.sources(row)['cover_image_src' if size=='normal' else 'cover_thumb_src']
    if expected != f'/api/listings/{listing_id}/cover/{version}/{size}':
        raise HTTPException(404,'Фото отсутствует')
    path = covers.path(row['cover_image_path' if size=='normal' else 'cover_thumb_path'])
    if path is None or not path.is_file():
        raise HTTPException(404,'Фото отсутствует')
    return FileResponse(path,media_type='image/jpeg')


@app.middleware('http')
async def protect_origin(request, call_next):
    denied = None
    if request.method in UNSAFE:
        origin = request.headers.get('origin')
        expected = os.getenv('PUBLIC_URL', '').rstrip('/')
        if origin and origin != (expected or str(request.base_url).rstrip('/')):
            denied = Response('Недопустимый Origin', status_code=403)
        if request.headers.get('sec-fetch-site') == 'cross-site':
            denied = Response('Недопустимый Sec-Fetch-Site', status_code=403)
    response = denied if denied is not None else await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'same-origin'
    if request.url.path in ('/', '/login') or request.url.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
    elif request.url.path.startswith('/static/'):
        response.headers['Cache-Control'] = 'no-cache, must-revalidate'
    response.headers['Content-Security-Policy'] = "default-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'self'"
    return response


STATIC = Path(__file__).parent / 'static'
ASSET_VERSIONS = {name: hashlib.sha256((STATIC / name).read_bytes()).hexdigest()[:16]
                  for name in ('app.js', 'visits.js', 'compare.js', 'review-state.js', 'login.js', 'style.css', 'xata-logo.svg', 'xata-mark.svg')}


def html_document(name):
    document = (STATIC / name).read_text(encoding='utf-8')
    for asset, version in ASSET_VERSIONS.items():
        document = document.replace('/static/' + asset + '"', '/static/' + asset + '?v=' + version + '"')
    return HTMLResponse(document)


@app.get('/')
def index(request: Request):
    if not security.principal(request):
        return RedirectResponse('/login', status_code=303)
    return html_document('index.html')


@app.get('/login')
def login_page(request: Request):
    if security.principal(request):
        return RedirectResponse('/', status_code=303)
    return html_document('login.html')


@app.get('/api/state')
def state(user=Depends(auth)):
    with database() as conn:
        # Read listings and their run from one SQLite snapshot.
        conn.execute('BEGIN')
        listings = grouped_listing_data(conn)
        run = conn.execute('SELECT * FROM runs ORDER BY id DESC LIMIT 1').fetchone()
        revision = identity_revision(conn)
    saved_browser = browser_state()
    result = {'listings': listings, 'identity_revision': revision, 'user': user, 'last_success': saved_browser.get('last_success'), 'criteria': browser_criteria()}
    if user['role'] == 'admin':
        result.update(run=dict(run) if run else None, browser=saved_browser)
    return result


class Review(BaseModel):
    status: Literal['new', 'liked', 'viewed', 'rejected']


@app.post('/api/listings/{listing_id}/review')
def review_listing(listing_id: str, data: Review, user=Depends(auth)):
    if user['role'] not in ('admin', 'reviewer'):
        raise HTTPException(403, 'Общий статус доступен только семье и администратору')
    timestamp = datetime.now(timezone.utc).isoformat()
    with database() as conn:
        conn.execute('BEGIN IMMEDIATE')
        target = conn.execute('SELECT property_id FROM listings WHERE id=?', (listing_id,)).fetchone()
        if not target:
            raise HTTPException(404, 'Объявление не найдено')
        property_id = target['property_id']
        version = conn.execute('SELECT MAX(review_version)+1 FROM listings WHERE property_id=?',(property_id,)).fetchone()[0]
        cursor = conn.execute('''UPDATE listings SET review_status=?,review_updated_at=?,review_updated_by=?,
                                 review_version=? WHERE property_id=?''',
                              (data.status, timestamp, user['id'], version, property_id))
        if not cursor.rowcount:
            raise HTTPException(404, 'Объявление не найдено')
        item = property_listing_data(conn, current_property_row(conn, property_id))
    log.info('Review changed user_id=%s listing_id=%s', user['id'], listing_id)
    return item


class RealtorReview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    status: Literal['pending', 'approved', 'rejected']
    comment: str = Field(default='', max_length=2000)

    @field_validator('comment')
    @classmethod
    def plain_text(cls, value):
        if re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', value):
            raise ValueError('Комментарий содержит недопустимые управляющие символы')
        return value


@app.post('/api/listings/{listing_id}/realtor-review')
def realtor_review(listing_id: str, data: RealtorReview, user=Depends(auth)):
    if user['role'] not in ('admin', 'realtor'):
        raise HTTPException(403, 'Оценка доступна только риелтору и администратору')
    timestamp = datetime.now(timezone.utc).isoformat()
    with database() as conn:
        conn.execute('BEGIN IMMEDIATE')
        target = conn.execute('SELECT property_id FROM listings WHERE id=?', (listing_id,)).fetchone()
        if not target:
            raise HTTPException(404, 'Объявление не найдено')
        property_id = target['property_id']
        version = conn.execute('SELECT MAX(realtor_version)+1 FROM listings WHERE property_id=?',(property_id,)).fetchone()[0]
        cursor = conn.execute('''UPDATE listings SET realtor_status=?,realtor_comment=?,realtor_updated_at=?,
                                 realtor_updated_by=?,realtor_version=? WHERE property_id=?''',
                              (data.status, data.comment, timestamp, user['id'], version, property_id))
        if not cursor.rowcount:
            raise HTTPException(404, 'Объявление не найдено')
        item = property_listing_data(conn, current_property_row(conn, property_id))
    log.info('Realtor review changed user_id=%s listing_id=%s', user['id'], listing_id)
    return item


def save_viewing(listing_id, data, user, visit_id=None):
    if user['role'] not in ('admin', 'reviewer'):
        raise HTTPException(403, 'Просмотры изменяют только семья и администратор')
    with database() as conn:
        conn.execute('BEGIN IMMEDIATE')
        pid = viewings.save(conn, listing_id, data, user['id'], visit_id)
        return property_listing_data(conn, current_property_row(conn, pid))


@app.post('/api/listings/{listing_id}/visits')
def create_viewing(listing_id: str, data: viewings.VisitInput, user=Depends(auth)):
    return save_viewing(listing_id, data, user)


@app.patch('/api/listings/{listing_id}/visits/date')
def update_viewing_date(listing_id: str, data: viewings.VisitDateInput, user=Depends(auth)):
    if user['role'] not in ('admin','realtor'):
        raise HTTPException(403,'Дату просмотра меняют только риелтор и администратор')
    with database() as conn:
        conn.execute('BEGIN IMMEDIATE')
        pid = viewings.save_date(conn,listing_id,data,user['id'])
        return property_listing_data(conn,current_property_row(conn,pid))


@app.patch('/api/listings/{listing_id}/visits/{visit_id}')
def update_viewing(listing_id: str, visit_id: int, data: viewings.VisitInput, user=Depends(auth)):
    return save_viewing(listing_id, data, user, visit_id)


class DuplicateDecision(BaseModel):
    model_config = ConfigDict(extra='forbid')
    decision: Literal['same','different']
    candidate_id: int = Field(ge=1)
    source_property_id: int = Field(ge=1)
    candidate_property_id: int = Field(ge=1)


@app.post('/api/listings/{listing_id}/duplicate-decision')
def duplicate_decision(listing_id: str, data: DuplicateDecision, user=Depends(auth)):
    if user['role'] not in ('admin','reviewer'):
        raise HTTPException(403,'Подтверждение дублей доступно семье и администратору')
    now = datetime.now(timezone.utc).isoformat()
    with database() as conn:
        conn.execute('BEGIN IMMEDIATE')
        listing = conn.execute('SELECT property_id FROM listings WHERE id=?',(listing_id,)).fetchone()
        if listing is None:
            raise HTTPException(404,'Объявление не найдено')
        source_pid = listing['property_id']
        candidate = conn.execute('''SELECT dc.* FROM duplicate_candidates dc JOIN listings l ON l.id=dc.listing_id
                    WHERE dc.id=? AND dc.decision='pending' AND l.property_id=?''',
                                 (data.candidate_id,source_pid)).fetchone()
        if candidate is None:
            raise HTTPException(409,'Предложение уже изменилось. Обновите список.')
        target_pid = root_property(conn,candidate['candidate_property_id'])
        if (source_pid != data.source_property_id or target_pid != data.candidate_property_id or source_pid==target_pid):
            raise HTTPException(409,'Связь объектов изменилась. Обновите список.')
        if data.decision=='same' and distinct_properties(conn,source_pid,target_pid):
            raise HTTPException(409,'Эти объекты ранее отмечены как разные квартиры')
        anchor = current_property_row(conn,target_pid)
        save_decision(conn,candidate['listing_id'],anchor['id'],data.decision,now,user['id'])
        if data.decision=='same':
            merge_properties(conn,source_pid,target_pid,now)
        else:
            bump_identity(conn)
        conn.execute('UPDATE duplicate_candidates SET decision=?,decided_at=?,decided_by=? WHERE id=?',
                     ('confirmed' if data.decision=='same' else 'rejected',now,user['id'],candidate['id']))
        item = property_listing_data(conn,current_property_row(conn,target_pid if data.decision=='same' else source_pid))
    schedule_covers([item['id']])
    log.info('Duplicate decision user_id=%s listing_id=%s decision=%s',user['id'],listing_id,data.decision)
    return item


def run_collection(run_id, filters, kind='online'):
    try:
        items = (CianBrowserSource() if kind == 'browser' else source.CianOnlineSource()).collect(filters)
        now = datetime.now(timezone.utc).isoformat()
        with database() as conn:
            conn.execute('BEGIN IMMEDIATE')
            result = save_items(conn, items, now)
            conn.execute("UPDATE runs SET status='done', finished=?, message=?, count=? WHERE id=?",
                         (now, 'Сбор завершён. Критерии пешей доступности и вторичного жилья следует сверить в объявлении.', len(items), run_id))
        if kind == 'browser':
            update_browser(status='authorized', message='Авторизован', last_success=now,
                           new_count=result['added'], relisted_count=result['relisted'], last_error=None)
        schedule_covers(item['id'] for item in items)
    except Exception as exc:
        message = str(exc) if isinstance(exc, source.SourceError) else 'Внутренняя ошибка сборщика; подробности в журнале сервера.'
        blocked = isinstance(exc, source.SourceBlocked)
        if isinstance(exc, source.SourceError):
            log.warning('Collection stopped: run=%s reason=%s', run_id, message)
        else:
            log.exception('Collection failed: run=%s', run_id)
        with database() as conn:
            conn.execute("UPDATE runs SET status=?, finished=?, message=? WHERE id=?", ('blocked' if blocked else 'error', datetime.now(timezone.utc).isoformat(), message, run_id))
        if kind == 'browser':
            status = exc.status if isinstance(exc, BrowserAttention) else 'error'
            update_browser(status=status, message=message,
                           **({} if blocked else {'last_error': message}))
    finally:
        lock.release()


@app.post('/api/collect', status_code=202, dependencies=[Depends(admin)])
def collect(filters: Filters):
    if not lock.acquire(blocking=False):
        raise HTTPException(409, 'Сбор уже выполняется')
    try:
        with database() as conn:
            last = conn.execute('SELECT started FROM runs ORDER BY id DESC LIMIT 1').fetchone()
            if last and (datetime.now(timezone.utc) - datetime.fromisoformat(last['started'])).total_seconds() < 60:
                raise HTTPException(429, 'Между запросами нужно подождать одну минуту')
            cursor = conn.execute('INSERT INTO runs(started,status,message,filters) VALUES (?,?,?,?)',
                                  (datetime.now(timezone.utc).isoformat(), 'running', 'Получаем одну страницу поиска Циан', filters.model_dump_json()))
            run_id = cursor.lastrowid
        threading.Thread(target=run_collection, args=(run_id, filters.model_dump()), daemon=True).start()
    except Exception:
        lock.release()
        raise
    return {'id': run_id, 'search_url': source.search_url(filters.model_dump())}



@app.get('/api/browser/status', dependencies=[Depends(admin)])
def live_browser_status():
    """No navigation; inspect the existing page, including after manual login."""
    try:
        current = CianBrowserSource().status()
        if current['status'] != 'unknown' and not current.get('running') and not lock.locked():
            update_browser(status=current['status'], message=current['message'],
                           manual_access=current.get('manual_access', False))
        return {**browser_state(), **current, 'available': True}
    except source.SourceError as exc:
        return {**browser_state(), 'available': False, 'connection_error': str(exc)}


def start_browser(filters):
    # Ignore historical run/client values; search URL is the server authority.
    filters = Filters(**browser_criteria())
    if not lock.acquire(blocking=False):
        raise HTTPException(409, 'Сбор уже выполняется')
    try:
        now = datetime.now(timezone.utc)
        with database() as conn:
            last = conn.execute("SELECT started FROM runs WHERE source='browser' ORDER BY id DESC LIMIT 1").fetchone()
            if last and (now - datetime.fromisoformat(last['started'])).total_seconds() < 60:
                raise HTTPException(429, 'Между ручными запусками браузера нужна одна минута')
            cursor = conn.execute('INSERT INTO runs(started,status,message,filters,source) VALUES (?,?,?,?,?)',
                                  (now.isoformat(), 'running', 'Поиск через браузер', filters.model_dump_json(), 'browser'))
            run_id = cursor.lastrowid
        threading.Thread(target=run_collection, args=(run_id, filters.model_dump(), 'browser'), daemon=True).start()
    except Exception:
        lock.release()
        raise
    return {'id': run_id}


class BrowserRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')


@app.post('/api/browser/collect', status_code=202, dependencies=[Depends(admin)])
def collect_browser(data: BrowserRequest = BrowserRequest()):
    return start_browser(Filters(**browser_criteria()))


def schedule_interval():
    # Conservative floor applies even to a mistakenly short environment value.
    return max(1800, int(os.getenv('BROWSER_SEARCH_INTERVAL_SECONDS', '1800')))


def scheduler_tick():
    if lock.locked():
        return
    saved = browser_state()
    # Pause after attention; only explicit user retry/live status can resume.
    if saved['status'] in ('login_required', 'confirmation_required'):
        return
    try:
        current = CianBrowserSource().status()
        update_browser(status=current['status'], message=current['message'],
                       manual_access=current.get('manual_access', False))
        if current['status'] not in ('authorized', 'unknown') or current.get('manual_access') or current.get('running'):
            return
        with database() as conn:
            previous = conn.execute("SELECT started,filters FROM runs WHERE source='browser' ORDER BY id DESC LIMIT 1").fetchone()
        if previous and (datetime.now(timezone.utc) - datetime.fromisoformat(previous['started'])).total_seconds() < schedule_interval():
            return
        filters = Filters(**browser_criteria())
        start_browser(filters)
    except HTTPException as exc:
        if exc.status_code not in (409, 429):
            raise
    except source.SourceError as exc:
        update_browser(last_error=str(exc))


def browser_scheduler(stop):
    if os.getenv('BROWSER_SCHEDULE_ENABLED', 'false').lower() not in ('1', 'true', 'yes'):
        return
    try:
        interval = schedule_interval()
    except ValueError:
        log.error('BROWSER_SEARCH_INTERVAL_SECONDS must be an integer; scheduler disabled')
        return
    # No immediate collection on container startup/restart.
    while not stop.wait(interval):
        try:
            scheduler_tick()
        except Exception:
            log.exception('Browser scheduler tick failed')


class ImportConfirmation(BaseModel):
    token: str = Field(min_length=32, max_length=128)


@app.post('/api/import/preview', dependencies=[Depends(admin)])
async def preview_import(request: Request):
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > MAX_BYTES:
            raise HTTPException(413, 'CSV должен быть не больше 5 МБ.')
    try:
        parsed = await run_in_threadpool(CianCsvSource().parse, bytes(content))
    except source.SourceError as exc:
        raise HTTPException(422, str(exc)) from exc
    now = datetime.now(timezone.utc).timestamp()
    token = secrets.token_urlsafe(32)
    items = parsed['items']
    with database() as conn:
        conn.execute('DELETE FROM import_previews WHERE created<?', (now - 1800,))
        # Bound disk consumption, including repeated previews never confirmed.
        if conn.execute('SELECT count(*) FROM import_previews').fetchone()[0] >= 10:
            raise HTTPException(429, 'Слишком много предварительных просмотров. Подождите 30 минут.')
        conn.execute('SAVEPOINT preview_matching')
        try:
            counts = save_items(conn,items,datetime.now(timezone.utc).isoformat(),imported=True)
        finally:
            conn.execute('ROLLBACK TO preview_matching')
            conn.execute('RELEASE preview_matching')
        conn.execute('INSERT INTO import_previews VALUES (?,?,?)', (token, now, json.dumps(items, ensure_ascii=False)))
    return {'token': token, 'found': parsed['found'], 'unique': len(items), 'duplicates': parsed['duplicates'],
            **counts, 'fields': parsed['fields'],
            'sample': items[:5], 'expires_in': 1800}


@app.post('/api/import/cancel', dependencies=[Depends(admin)])
def cancel_import(confirmation: ImportConfirmation):
    with database() as conn:
        conn.execute('DELETE FROM import_previews WHERE token=?', (confirmation.token,))
    return {'message': 'Импорт отменён. Объявления не изменены.'}


@app.post('/api/import/confirm', dependencies=[Depends(admin)])
def confirm_import(confirmation: ImportConfirmation):
    now = datetime.now(timezone.utc)
    with database() as conn:
        # Serialize token consumption and updates in the same transaction.
        conn.execute('BEGIN IMMEDIATE')
        preview = conn.execute('SELECT * FROM import_previews WHERE token=?', (confirmation.token,)).fetchone()
        if not preview or preview['created'] < now.timestamp() - 1800:
            raise HTTPException(409, 'Предпросмотр истёк или уже импортирован. Выберите CSV заново.')
        items = json.loads(preview['payload'])
        result = save_items(conn, items, now.isoformat(), imported=True)
        conn.execute('DELETE FROM import_previews WHERE token=?', (confirmation.token,))
    schedule_covers(item['id'] for item in items)
    log.info('CSV import complete: added=%s updated=%s', result['added'], result['updated'])
    return {**result, 'count': len(items), 'message': 'CSV импортирован. Старые объявления сохранены.'}


@app.get('/api/export.xlsx', dependencies=[Depends(admin)])
def export():
    book = Workbook()
    sheet = book.active
    sheet.title = 'Объявления'
    sheet.append(['ID', 'Название', 'Цена, ₽', 'Адрес', 'Ссылка', 'Впервые найдено (UTC)', 'Последнее обновление (UTC)', 'ЖК', 'Площадь, м²', 'Этаж', 'Метро', 'Время до метро', 'Источник', 'Последний импорт (UTC)', 'Текст карточки', 'Описание', 'Пешком до метро, мин', 'Статус', 'Статус изменил', 'Дата изменения статуса (UTC)', 'Оценка риелтора', 'Комментарий риелтора', 'Риелторскую оценку изменил', 'Дата риелторской оценки (UTC)', 'Ипотека / мес. (6 млн / 18% / 15 лет)', 'Property ID', 'Все CIAN ID', 'Перепубликаций', 'Мин. замеченная цена, ₽', 'Макс. замеченная цена, ₽', 'Возможный дубль, %', 'История цен (UTC)', 'Публикации: даты и состояние'])
    with database() as conn:
        items = sorted(grouped_listing_data(conn), key=lambda item: (item['price'] is None, item['price'] or 0))
        for item in items:
            candidate = item.get('duplicate_candidate') or {}
            values = [
                item['id'], item['title'], item['price'], item['address'], item['url'], item['first_seen'], item['last_seen'],
                item['complex_name'], item['area'], item['floor'], item['metro'], item['metro_time'], item['source'],
                item['last_imported'], item['card_text'], item['description'], item['metro_walk_minutes'], item['review_status'],
                item.get('review_updated_by_name'), item['review_updated_at'], item['realtor_status'], item['realtor_comment'],
                item.get('realtor_updated_by_name'), item['realtor_updated_at'], item['mortgage_payment'], item.get('property_id'),
                ' → '.join(item.get('listing_ids') or [item['id']]), item.get('relist_count', 0), item.get('price_min'),
                item.get('price_max'), candidate.get('confidence'),
                '\n'.join(f"{entry['observed_at']} · CIAN {entry['listing_id']} · {entry['price']} ₽" for entry in item['price_history']),
                '\n'.join(f"CIAN {entry['id']} · {entry['first_seen']} — {entry['last_seen']} · {entry['status']}" for entry in item['publication_history']),
            ]
            sheet.append([("'" + value if isinstance(value, str) and value.startswith(('=', '+', '-', '@')) else value) for value in values])
    visits_sheet = book.create_sheet('Просмотры')
    visits_sheet.append(['Property ID','Текущий CIAN ID','Адрес','ID просмотра','Дата встречи (UTC)',
                         'Состояние','Результат','До просмотра','После просмотра','Состоялся (UTC)',
                         'Изменил','Изменено (UTC)','Причины отказа'])
    for item in items:
        for visit in item['visits']:
            values = [item['property_id'],item['id'],item['address'],visit['id'],visit['scheduled_at'],
                      visit['status'],visit['outcome'],visit['preparation'],visit['comment'],
                      visit['completed_at'],visit['updated_by_name'] or visit['updated_by_username'],visit['updated_at'],
                      ', '.join(viewings.REJECTION_REASONS[reason] for reason in visit['rejection_reasons'])]
            visits_sheet.append([("'" + value if isinstance(value,str) and value.startswith(('=','+','-','@')) else value) for value in values])
    visits_sheet.freeze_panes = 'A2'
    visits_sheet.auto_filter.ref = visits_sheet.dimensions
    sheet.freeze_panes = 'A2'
    sheet.auto_filter.ref = sheet.dimensions
    stream = io.BytesIO()
    book.save(stream)
    return Response(stream.getvalue(), media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', headers={'Content-Disposition': 'attachment; filename="xata.xlsx"'})

app.mount('/static', StaticFiles(directory=Path(__file__).parent / 'static'), name='static')
