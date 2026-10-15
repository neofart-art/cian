import io
import json
import logging
import os
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from openpyxl import Workbook
from pydantic import BaseModel, Field
from app import source

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('xata')
DATA = Path(os.getenv('DATA_DIR', 'data'))
DATA.mkdir(parents=True, exist_ok=True)
DB = DATA / 'xata.sqlite3'
security = HTTPBasic(auto_error=False)
lock = threading.Lock()


def auth(credentials: HTTPBasicCredentials | None = Depends(security)):
    username, password = os.getenv('APP_USER', ''), os.getenv('APP_PASSWORD', '')
    if not username or len(password) < 16:
        raise HTTPException(503, 'Задайте APP_USER и APP_PASSWORD длиной от 16 символов.')
    if not credentials or not (secrets.compare_digest(credentials.username.encode(), username.encode()) and secrets.compare_digest(credentials.password.encode(), password.encode())):
        raise HTTPException(401, 'Требуется вход', headers={'WWW-Authenticate': 'Basic'})


@contextmanager
def database():
    with sqlite3.connect(DB, timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        yield conn


with database() as conn:
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS listings (id TEXT PRIMARY KEY, title TEXT, price INTEGER, address TEXT, url TEXT, first_seen TEXT, last_seen TEXT);
    CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, started TEXT, finished TEXT, status TEXT, message TEXT, filters TEXT, count INTEGER DEFAULT 0);
    UPDATE runs SET status='error', message='Процесс был перезапущен во время сбора' WHERE status='running';
    ''')

app = FastAPI(title='Хата — поиск квартир', docs_url=None, redoc_url=None)


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
    if request.method == 'POST':
        origin = request.headers.get('origin')
        expected = os.getenv('PUBLIC_URL', '').rstrip('/')
        if origin and origin != (expected or str(request.base_url).rstrip('/')):
            return Response('Недопустимый Origin', status_code=403)
        if request.headers.get('sec-fetch-site') == 'cross-site':
            return Response(status_code=403)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'same-origin'
    response.headers['Content-Security-Policy'] = "default-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'self'"
    return response


@app.get('/', dependencies=[Depends(auth)])
def index():
    return FileResponse(Path(__file__).parent / 'static/index.html')


@app.get('/api/state', dependencies=[Depends(auth)])
def state():
    with database() as conn:
        listings = [dict(row) for row in conn.execute('SELECT * FROM listings ORDER BY last_seen DESC, price')]
        run = conn.execute('SELECT * FROM runs ORDER BY id DESC LIMIT 1').fetchone()
    return {'listings': listings, 'run': dict(run) if run else None}


def run_collection(run_id, filters):
    try:
        items = source.collect(filters)
        now = datetime.now(timezone.utc).isoformat()
        with database() as conn:
            for item in items:
                conn.execute('''INSERT INTO listings VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                title=excluded.title,price=excluded.price,address=excluded.address,url=excluded.url,last_seen=excluded.last_seen''',
                (item['id'], item['title'], item['price'], item['address'], item['url'], now, now))
            conn.execute("UPDATE runs SET status='done', finished=?, message=?, count=? WHERE id=?",
                         (now, 'Сбор завершён. Критерии пешей доступности и вторичного жилья следует сверить в объявлении.', len(items), run_id))
    except Exception as exc:
        message = str(exc) if isinstance(exc, source.SourceError) else 'Внутренняя ошибка сборщика; подробности в журнале сервера.'
        log.exception('Collection failed: run=%s', run_id)
        with database() as conn:
            conn.execute("UPDATE runs SET status='error', finished=?, message=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), message, run_id))
    finally:
        lock.release()


@app.post('/api/collect', status_code=202, dependencies=[Depends(auth)])
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


@app.get('/api/export.xlsx', dependencies=[Depends(auth)])
def export():
    book = Workbook()
    sheet = book.active
    sheet.title = 'Объявления'
    sheet.append(['ID', 'Название', 'Цена, ₽', 'Адрес', 'Ссылка', 'Впервые найдено (UTC)', 'Последний сбор (UTC)'])
    with database() as conn:
        for row in conn.execute('SELECT * FROM listings ORDER BY price'):
            sheet.append([("'" + value if isinstance(value, str) and value.startswith(('=', '+', '-', '@')) else value) for value in row])
    sheet.freeze_panes = 'A2'
    sheet.auto_filter.ref = sheet.dimensions
    stream = io.BytesIO()
    book.save(stream)
    return Response(stream.getvalue(), media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', headers={'Content-Disposition': 'attachment; filename="xata.xlsx"'})

app.mount('/static', StaticFiles(directory=Path(__file__).parent / 'static'), name='static')
