import io
import json
import logging
import os
import secrets
import sqlite3
import threading
from contextlib import contextmanager, asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response, RedirectResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from openpyxl import Workbook
from pydantic import BaseModel, Field
from typing import Literal
from app.auth import AuthStore, UNSAFE
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
        'review_status': "TEXT NOT NULL DEFAULT 'new' CHECK(review_status IN ('new','viewed','rejected'))",
        'review_updated_at': 'TEXT',
        'review_updated_by': 'INTEGER REFERENCES users(id)',
        'review_version': 'INTEGER NOT NULL DEFAULT 0',
    }.items():
        if name not in columns:
            conn.execute(f'ALTER TABLE listings ADD COLUMN {name} {kind}')
    conn.execute("""CREATE INDEX IF NOT EXISTS listings_review_order ON listings(
        CASE review_status WHEN 'new' THEN 0 WHEN 'viewed' THEN 1 ELSE 2 END,first_seen DESC,id)""")

LISTING_QUERY = '''SELECT l.*,u.display_name AS review_updated_by_name,u.username AS review_updated_by_username
                   FROM listings l LEFT JOIN users u ON u.id=l.review_updated_by'''
LISTING_ORDER = " ORDER BY CASE l.review_status WHEN 'new' THEN 0 WHEN 'viewed' THEN 1 ELSE 2 END,l.first_seen DESC,l.id"


def browser_state():
    with database() as conn:
        return json.loads(conn.execute('SELECT payload FROM browser_state WHERE id=1').fetchone()[0])


def update_browser(**changes):
    with database() as conn:
        conn.execute('BEGIN IMMEDIATE')
        state = json.loads(conn.execute('SELECT payload FROM browser_state WHERE id=1').fetchone()[0])
        state.update(changes)
        conn.execute('UPDATE browser_state SET payload=? WHERE id=1', (json.dumps(state, ensure_ascii=False),))


LISTING_FIELDS = ('title', 'price', 'address', 'url', 'complex_name', 'area', 'floor', 'metro', 'metro_time', 'source', 'card_text', 'description', 'metro_walk_minutes')


def save_items(conn, items, now, imported=False):
    added = updated = 0
    for item in items:
        previous = conn.execute('SELECT * FROM listings WHERE id=?', (item['id'],)).fetchone()
        merged = dict(previous) if previous else {'title': 'Квартира'}
        merged.update({key: item[key] for key in LISTING_FIELDS if item.get(key) not in (None, '')})
        if item.get('metro_time'):
            merged['metro_walk_minutes'] = item.get('metro_walk_minutes')
        values = [merged.get(field) for field in LISTING_FIELDS]
        if previous:
            conn.execute('UPDATE listings SET ' + ','.join(f'{field}=?' for field in LISTING_FIELDS) +
                         ',last_seen=?,last_imported=? WHERE id=?',
                         (*values, now, now if imported else previous['last_imported'], item['id']))
            updated += 1
        else:
            fields = ('id', *LISTING_FIELDS, 'first_seen', 'last_seen', 'last_imported')
            conn.execute('INSERT INTO listings (' + ','.join(fields) + ') VALUES (' + ','.join('?' for _ in fields) + ')',
                         (item['id'], *values, now, now, now if imported else None))
            added += 1
    return {'added': added, 'updated': updated}


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
    response.headers['Content-Security-Policy'] = "default-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'self'"
    return response


@app.get('/')
def index(request: Request):
    if not security.principal(request):
        return RedirectResponse('/login', status_code=303)
    return FileResponse(Path(__file__).parent / 'static/index.html')


@app.get('/login')
def login_page(request: Request):
    if security.principal(request):
        return RedirectResponse('/', status_code=303)
    return FileResponse(Path(__file__).parent / 'static/login.html')


@app.get('/api/state')
def state(user=Depends(auth)):
    with database() as conn:
        # Read listings and their run from one SQLite snapshot.
        conn.execute('BEGIN')
        listings = [dict(row) for row in conn.execute(LISTING_QUERY + LISTING_ORDER)]
        run = conn.execute('SELECT * FROM runs ORDER BY id DESC LIMIT 1').fetchone()
    result = {'listings': listings, 'user': user}
    if user['role'] == 'admin':
        result.update(run=dict(run) if run else None, browser=browser_state())
    return result


class Review(BaseModel):
    status: Literal['new', 'viewed', 'rejected']


@app.post('/api/listings/{listing_id}/review')
def review_listing(listing_id: str, data: Review, user=Depends(auth)):
    timestamp = datetime.now(timezone.utc).isoformat()
    with database() as conn:
        conn.execute('BEGIN IMMEDIATE')
        cursor = conn.execute('''UPDATE listings SET review_status=?,review_updated_at=?,review_updated_by=?,
                                 review_version=review_version+1 WHERE id=?''',
                              (data.status, timestamp, user['id'], listing_id))
        if not cursor.rowcount:
            raise HTTPException(404, 'Объявление не найдено')
        item = dict(conn.execute(LISTING_QUERY + ' WHERE l.id=?', (listing_id,)).fetchone())
    log.info('Review changed user_id=%s listing_id=%s', user['id'], listing_id)
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
                           new_count=result['added'], last_error=None)
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


@app.post('/api/browser/collect', status_code=202, dependencies=[Depends(admin)])
def collect_browser(filters: Filters):
    return start_browser(filters)


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
        filters = Filters.model_validate_json(previous['filters']) if previous else Filters()
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
        existing = {row['id'] for row in conn.execute('SELECT id FROM listings')}
        conn.execute('INSERT INTO import_previews VALUES (?,?,?)', (token, now, json.dumps(items, ensure_ascii=False)))
    updated = sum(item['id'] in existing for item in items)
    return {'token': token, 'found': parsed['found'], 'unique': len(items), 'duplicates': parsed['duplicates'],
            'added': len(items) - updated, 'updated': updated, 'fields': parsed['fields'],
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
    log.info('CSV import complete: added=%s updated=%s', result['added'], result['updated'])
    return {**result, 'count': len(items), 'message': 'CSV импортирован. Старые объявления сохранены.'}


@app.get('/api/export.xlsx', dependencies=[Depends(admin)])
def export():
    book = Workbook()
    sheet = book.active
    sheet.title = 'Объявления'
    sheet.append(['ID', 'Название', 'Цена, ₽', 'Адрес', 'Ссылка', 'Впервые найдено (UTC)', 'Последнее обновление (UTC)', 'ЖК', 'Площадь, м²', 'Этаж', 'Метро', 'Время до метро', 'Источник', 'Последний импорт (UTC)', 'Текст карточки', 'Описание', 'Пешком до метро, мин', 'Статус', 'Статус изменил', 'Дата изменения статуса (UTC)'])
    with database() as conn:
        for row in conn.execute('''SELECT l.id,l.title,l.price,l.address,l.url,l.first_seen,l.last_seen,l.complex_name,l.area,l.floor,l.metro,l.metro_time,l.source,l.last_imported,l.card_text,l.description,l.metro_walk_minutes,l.review_status,u.display_name,l.review_updated_at FROM listings l LEFT JOIN users u ON u.id=l.review_updated_by ORDER BY l.price'''):
            sheet.append([("'" + value if isinstance(value, str) and value.startswith(('=', '+', '-', '@')) else value) for value in row])
    sheet.freeze_panes = 'A2'
    sheet.auto_filter.ref = sheet.dimensions
    stream = io.BytesIO()
    book.save(stream)
    return Response(stream.getvalue(), media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', headers={'Content-Disposition': 'attachment; filename="xata.xlsx"'})

app.mount('/static', StaticFiles(directory=Path(__file__).parent / 'static'), name='static')
