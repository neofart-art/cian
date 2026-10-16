"""Local users and opaque server-side sessions; no HTTP Basic fallback."""
import hashlib
import logging
import os
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Literal

from argon2 import PasswordHasher, Type
from argon2.exceptions import VerificationError, InvalidHashError
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

log = logging.getLogger('xata')
COOKIE = '__Host-xata_session'
LOGIN_COOKIE = '__Host-xata_login'
SESSION_SECONDS = 7 * 24 * 3600
UNSAFE = {'POST', 'PUT', 'PATCH', 'DELETE'}
USER_COLUMNS = 'id,username,display_name,role,enabled,created_at,updated_at'
HASHER = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1, type=Type.ID)
DUMMY_HASH = HASHER.hash(secrets.token_urlsafe(32))
HASH_SLOTS = threading.BoundedSemaphore(2)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def username(value):
    value = value.strip().casefold()
    if not value or len(value) > 128 or any(ord(char) < 32 for char in value):
        raise ValueError('Логин должен содержать от 1 до 128 печатных символов.')
    return value


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)


class CreateUser(BaseModel):
    model_config = ConfigDict(extra='forbid')
    username: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=12, max_length=1024)
    role: Literal['admin', 'reviewer'] = 'reviewer'
    enabled: bool = True

    @field_validator('username')
    @classmethod
    def valid_username(cls, value):
        return username(value)

    @field_validator('display_name')
    @classmethod
    def valid_name(cls, value):
        if not value.strip():
            raise ValueError('Укажите имя.')
        return value.strip()


class UpdateUser(BaseModel):
    model_config = ConfigDict(extra='forbid')
    username: str | None = Field(default=None, min_length=1, max_length=128)
    display_name: str | None = Field(default=None, min_length=1, max_length=80)
    password: str | None = Field(default=None, min_length=12, max_length=1024)
    role: Literal['admin', 'reviewer'] | None = None
    enabled: bool | None = None

    @field_validator('username')
    @classmethod
    def valid_username(cls, value):
        return username(value) if value is not None else None

    @field_validator('display_name')
    @classmethod
    def valid_name(cls, value):
        if value is not None and not value.strip():
            raise ValueError('Укажите имя.')
        return value.strip() if value is not None else None


class AuthStore:
    def __init__(self, database):
        self.database = database

    def migrate(self):
        with self.database() as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    display_name TEXT NOT NULL, password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('admin','reviewer')),
                    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
                    csrf_token TEXT NOT NULL, expires REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
                CREATE TABLE IF NOT EXISTS login_challenges (
                    token_hash TEXT PRIMARY KEY, csrf_token TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS login_attempts (
                    bucket TEXT PRIMARY KEY, failed INTEGER NOT NULL, started REAL NOT NULL);
            ''')
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute('SELECT count(*) FROM users').fetchone()[0]:
                return
            login, password = os.getenv('APP_USER', ''), os.getenv('APP_PASSWORD', '')
            try:
                login = username(login)
                if len(password) < 16 or len(password) > 1024:
                    raise ValueError()
            except ValueError:
                log.warning('Bootstrap requires APP_USER and APP_PASSWORD of at least 16 characters')
                return
            timestamp = now_iso()
            conn.execute('INSERT INTO users(username,display_name,password_hash,role,created_at,updated_at) VALUES (?,?,?,?,?,?)',
                         (login, os.getenv('APP_USER', '').strip()[:80], HASHER.hash(password), 'admin', timestamp, timestamp))
            log.info('Admin bootstrap complete')

    def principal(self, request):
        value = request.cookies.get(COOKIE, '')
        if not value or len(value) > 128:
            return None
        with self.database() as conn:
            row = conn.execute('''SELECT u.id,u.username,u.display_name,u.role,u.enabled,u.created_at,u.updated_at,
                                         s.csrf_token FROM sessions s JOIN users u ON u.id=s.user_id
                                  WHERE s.token_hash=? AND s.expires>? AND u.enabled=1''',
                               (digest(value), time.time())).fetchone()
        if not row:
            return None
        request.state.csrf_token = row['csrf_token']
        return {key: row[key] for key in USER_COLUMNS.split(',')}

    def current_user(self, request: Request):
        user = self.principal(request)
        if not user:
            raise HTTPException(401, 'Требуется вход')
        if request.method in UNSAFE:
            supplied = request.headers.get('X-CSRF-Token', '')
            if not secrets.compare_digest(supplied.encode(), request.state.csrf_token.encode()):
                raise HTTPException(403, 'Недопустимый CSRF-токен')
        return user

    def admin_dependency(self):
        def admin(user=Depends(self.current_user)):
            if user['role'] != 'admin':
                raise HTTPException(403, 'Доступно только администратору')
            return user
        return admin

    def router(self):
        router = APIRouter()
        admin = self.admin_dependency()

        @router.get('/api/auth/csrf')
        def login_csrf():
            token, csrf, now = secrets.token_urlsafe(32), secrets.token_urlsafe(32), time.time()
            with self.database() as conn:
                conn.execute('BEGIN IMMEDIATE')
                conn.execute('DELETE FROM login_challenges WHERE expires<=?', (now,))
                conn.execute('''DELETE FROM login_challenges WHERE token_hash IN
                                (SELECT token_hash FROM login_challenges ORDER BY expires DESC LIMIT -1 OFFSET 199)''')
                conn.execute('INSERT INTO login_challenges VALUES (?,?,?)', (digest(token), csrf, now + 600))
            response = JSONResponse({'csrf_token': csrf})
            response.set_cookie(LOGIN_COOKIE, token, max_age=600, secure=True, httponly=True, samesite='strict', path='/')
            return response

        @router.post('/api/auth/login')
        def login(data: Login, request: Request):
            now = time.time()
            token = request.cookies.get(LOGIN_COOKIE, '')
            with self.database() as conn:
                conn.execute('BEGIN IMMEDIATE')
                challenge = conn.execute('SELECT * FROM login_challenges WHERE token_hash=? AND expires>?',
                                         (digest(token), now)).fetchone()
                if not challenge or not secrets.compare_digest(request.headers.get('X-CSRF-Token', '').encode(), challenge['csrf_token'].encode()):
                    raise HTTPException(403, 'Обновите страницу входа и повторите попытку.')
                conn.execute('DELETE FROM login_challenges WHERE token_hash=?', (digest(token),))
            bucket = digest(request.client.host if request.client else 'unknown')
            with self.database() as conn:
                conn.execute('DELETE FROM login_attempts WHERE started<?', (now - 900,))
                attempt = conn.execute('SELECT * FROM login_attempts WHERE bucket=?', (bucket,)).fetchone()
                if attempt and attempt['failed'] >= 20:
                    raise HTTPException(429, 'Слишком много попыток входа. Подождите 15 минут.')
                if not conn.execute('SELECT count(*) FROM users').fetchone()[0]:
                    raise HTTPException(503, 'Задайте APP_USER и APP_PASSWORD длиной от 16 символов для первого входа.')
                try:
                    login_name = username(data.username)
                except ValueError:
                    login_name = ''
                row = conn.execute('SELECT * FROM users WHERE username=?', (login_name,)).fetchone()
            if not HASH_SLOTS.acquire(blocking=False):
                raise HTTPException(429, 'Вход занят. Повторите попытку через несколько секунд.')
            try:
                try:
                    valid = HASHER.verify(row['password_hash'] if row else DUMMY_HASH, data.password)
                except (VerificationError, InvalidHashError):
                    valid = False
            finally:
                HASH_SLOTS.release()
            if not row or not row['enabled'] or not valid:
                with self.database() as conn:
                    conn.execute('''INSERT INTO login_attempts VALUES (?,1,?)
                                    ON CONFLICT(bucket) DO UPDATE SET failed=failed+1''', (bucket, now))
                raise HTTPException(401, 'Неверный логин или пароль')
            session, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            with self.database() as conn:
                conn.execute('BEGIN IMMEDIATE')
                # A concurrent disable/password reset must invalidate an in-flight login too.
                current = conn.execute('SELECT * FROM users WHERE id=?', (row['id'],)).fetchone()
                if not current or not current['enabled'] or current['password_hash'] != row['password_hash']:
                    raise HTTPException(401, 'Неверный логин или пароль')
                # This is a per-address failure budget, shared behind NPM.
                # A valid reviewer login must not reset failed admin-login attempts.
                conn.execute('DELETE FROM sessions WHERE expires<=? OR token_hash=?',
                             (now, digest(request.cookies.get(COOKIE, ''))))
                conn.execute('INSERT INTO sessions VALUES (?,?,?,?)', (digest(session), row['id'], csrf, now + SESSION_SECONDS))
                user = dict(conn.execute('SELECT ' + USER_COLUMNS + ' FROM users WHERE id=?', (row['id'],)).fetchone())
            response = JSONResponse({'user': user, 'csrf_token': csrf})
            response.set_cookie(COOKIE, session, max_age=SESSION_SECONDS, secure=True, httponly=True, samesite='lax', path='/')
            response.delete_cookie(LOGIN_COOKIE, secure=True, httponly=True, samesite='strict', path='/')
            log.info('Login user_id=%s', row['id'])
            return response

        @router.get('/api/auth/me')
        def me(request: Request, user=Depends(self.current_user)):
            return {'user': user, 'csrf_token': request.state.csrf_token}

        @router.post('/api/auth/logout')
        def logout(request: Request, user=Depends(self.current_user)):
            with self.database() as conn:
                conn.execute('DELETE FROM sessions WHERE token_hash=?', (digest(request.cookies.get(COOKIE, '')),))
            response = JSONResponse({'message': 'Вы вышли из ХАТЫ'})
            response.delete_cookie(COOKIE, secure=True, httponly=True, samesite='lax', path='/')
            log.info('Logout user_id=%s', user['id'])
            return response

        @router.get('/api/users', dependencies=[Depends(admin)])
        def users():
            with self.database() as conn:
                return {'users': [dict(row) for row in conn.execute('SELECT ' + USER_COLUMNS + ' FROM users ORDER BY id')]}

        @router.post('/api/users', status_code=201)
        def create(data: CreateUser, actor=Depends(admin)):
            timestamp = now_iso()
            password_hash = HASHER.hash(data.password)
            try:
                with self.database() as conn:
                    cursor = conn.execute('''INSERT INTO users(username,display_name,password_hash,role,enabled,created_at,updated_at)
                                             VALUES (?,?,?,?,?,?,?)''',
                                          (data.username, data.display_name, password_hash, data.role, data.enabled, timestamp, timestamp))
                    user = dict(conn.execute('SELECT ' + USER_COLUMNS + ' FROM users WHERE id=?', (cursor.lastrowid,)).fetchone())
            except sqlite3.IntegrityError as exc:
                raise HTTPException(409, 'Такой логин уже существует') from exc
            log.info('User created actor_id=%s user_id=%s', actor['id'], user['id'])
            return user

        @router.patch('/api/users/{user_id}')
        def update(user_id: int, data: UpdateUser, actor=Depends(admin)):
            changes = data.model_dump(exclude_none=True)
            if 'password' in changes:
                changes['password_hash'] = HASHER.hash(changes.pop('password'))
            try:
                with self.database() as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    old = conn.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
                    if not old:
                        raise HTTPException(404, 'Пользователь не найден')
                    remains_admin = changes.get('role', old['role']) == 'admin' and changes.get('enabled', old['enabled'])
                    if old['role'] == 'admin' and old['enabled'] and not remains_admin:
                        count = conn.execute("SELECT count(*) FROM users WHERE role='admin' AND enabled=1").fetchone()[0]
                        if count <= 1:
                            raise HTTPException(409, 'Нельзя отключить или понизить последнего активного администратора')
                    if changes:
                        changes['updated_at'] = now_iso()
                        conn.execute('UPDATE users SET ' + ','.join(f'{key}=?' for key in changes) + ' WHERE id=?',
                                     (*changes.values(), user_id))
                    if 'password_hash' in changes or changes.get('enabled') is False:
                        conn.execute('DELETE FROM sessions WHERE user_id=?', (user_id,))
                    user = dict(conn.execute('SELECT ' + USER_COLUMNS + ' FROM users WHERE id=?', (user_id,)).fetchone())
            except sqlite3.IntegrityError as exc:
                raise HTTPException(409, 'Такой логин уже существует') from exc
            log.info('User updated actor_id=%s user_id=%s', actor['id'], user_id)
            return user

        return router
