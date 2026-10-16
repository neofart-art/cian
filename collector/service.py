"""One persistent headed Chromium, serialized jobs, manual challenges only."""
import asyncio
import fcntl
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from playwright.async_api import async_playwright, Error as PlaywrightError

from app.source_types import SourceError

from collector.page import (ATTENTION, AUTH_SELECTOR, CARD_SELECTOR, CHALLENGE_SELECTOR,
                            LOGIN_SELECTOR, EMPTY_SELECTOR, VISIBLE_NODES, EXTRACT_CARDS,
                            UNRECOGNIZED_CARDS, classify, normalize_card, validate_url)

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('xata.browser')

DOM_TIMEOUT_MS = 4500
SNAPSHOT_TIMEOUT_SECONDS = 3
NAVIGATION_TIMEOUT_MS = 20000
DOM_WAIT_SECONDS = 8
READINESS_TIMEOUT = 'Выдача ЦИАН не успела загрузиться за отведённое время. Повторите поиск позже.'

PROFILE = Path(os.getenv('BROWSER_PROFILE_DIR', '/profile'))
MANUAL = os.getenv('BROWSER_MANUAL_ACCESS', '0') == '1'


def auth(authorization: str = Header(default='')):
    token = os.getenv('BROWSER_API_TOKEN', '')
    if len(token) < 16 or not secrets.compare_digest(authorization, f'Bearer {token}'):
        raise HTTPException(401, 'Unauthorized')


class Search(BaseModel):
    url: str = Field(max_length=4096)


class Collector:
    def __init__(self, context):
        self.context = context
        self.page = None
        self.lock = asyncio.Lock()
        self.current = 'unknown'

    async def visible(self, selector):
        return await asyncio.wait_for(
            self.page.locator(selector).evaluate_all(VISIBLE_NODES),
            timeout=SNAPSHOT_TIMEOUT_SECONDS)

    async def inspect(self, http_status=200):
        text = await self.page.locator('body').inner_text(timeout=DOM_TIMEOUT_MS)
        # Snapshot selector matches instead of keeping live nth-locators.
        self.current = classify(
            self.page.url, text,
            challenge=await self.visible(CHALLENGE_SELECTOR),
            login=await self.visible(LOGIN_SELECTOR),
            authorized=await self.visible(AUTH_SELECTOR),
            http_status=http_status)
        label = {'confirmation_required': 'challenge', 'login_required': 'login'}.get(self.current, self.current)
        log.info('browser DOM classified as %s', label)
        return self.current

    def result(self, status, **kwargs):
        message = (ATTENTION if status == 'confirmation_required' else
                   ATTENTION + '. Требуется вход в ЦИАН.' if status == 'login_required' else
                   'Сессия ещё не проверена после запуска браузера' if status == 'unknown' else
                   'Авторизован' if status == 'authorized' else 'Ошибка browser collector')
        return dict(status=status, message=message, **kwargs)

    async def collect(self, url):
        validate_url(url)
        if MANUAL or self.lock.locked():
            raise HTTPException(409, 'Browser busy or manual access enabled')
        async with self.lock:
            stage = 'navigation'
            try:
                response = await self.page.goto(
                    url, wait_until='domcontentloaded', timeout=NAVIGATION_TIMEOUT_MS)
                status_code = response.status if response else 200
                log.info('browser navigation completed http_status=%d', status_code)
                if status_code in (401, 403, 429):
                    return self.result('login_required' if status_code == 401 else 'confirmation_required', items=[])
                if status_code >= 400:
                    return self.result('error', items=[], error=f'ЦИАН вернул HTTP {status_code}')
                # Retry only reads of this page, never navigation or confirmation.
                # Each attempt shares the same hard deadline, including body.inner_text.
                loop = asyncio.get_running_loop()
                deadline = loop.time() + DOM_WAIT_SECONDS
                attempts = 0
                readiness_error = READINESS_TIMEOUT
                stage = 'classification_loading'
                while True:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        log.warning('browser readiness deadline exceeded stage=%s attempts=%d', stage, attempts)
                        return self.result('error', items=[], error=readiness_error)
                    attempts += 1
                    stage = 'classification_loading'
                    try:
                        async with asyncio.timeout(remaining):
                            status = await self.inspect(status_code)
                            if status in ('confirmation_required', 'login_required'):
                                return self.result(status, items=[])
                            stage = 'waiting_for_cards'
                            ready = status == 'authorized' and (
                                await self.page.locator(CARD_SELECTOR).count() or await self.visible(EMPTY_SELECTOR))
                    except (PlaywrightError, asyncio.TimeoutError) as exc:
                        # Error messages can contain URLs/HTML. Log type/stage only.
                        log.info('browser DOM still loading stage=%s attempt=%d error_type=%s',
                                 stage, attempts, type(exc).__name__)
                        readiness_error = READINESS_TIMEOUT
                    else:
                        if ready:
                            break
                        readiness_error = UNRECOGNIZED_CARDS if status == 'authorized' else READINESS_TIMEOUT
                    remaining = deadline - loop.time()
                    if remaining > 0:
                        await asyncio.sleep(min(.25, remaining))
                stage = 'classification_before_snapshot'
                status = await self.inspect(status_code)
                if status == 'unknown':
                    return self.result('error', items=[], error='Не удалось подтвердить авторизацию по DOM. Проверьте меню профиля на OMV.')
                if status != 'authorized':
                    return self.result(status, items=[])

                stage = 'card_snapshot'
                snapshot = await asyncio.wait_for(
                    self.page.locator(CARD_SELECTOR).evaluate_all(EXTRACT_CARDS),
                    timeout=SNAPSHOT_TIMEOUT_SECONDS)
                log.info('browser snapshot matching_card_nodes=%d', snapshot['matching'])
                items = {}
                normalized = 0
                skipped = snapshot['skipped']
                stage = 'normalization'
                for raw in snapshot['cards']:
                    try:
                        item = normalize_card(raw)
                    except (SourceError, ValueError, TypeError, KeyError, AttributeError):
                        skipped += 1
                        continue
                    normalized += 1
                    items[item['id']] = item
                log.info('browser snapshot normalized_cards=%d unique_cards=%d skipped_cards=%d',
                         normalized, len(items), skipped)
                # Challenges still discard the whole batch, including a late challenge.
                stage = 'classification_after_snapshot'
                status = await self.inspect(status_code)
                if status != 'authorized':
                    return self.result(status, items=[])
                if not items and not (snapshot['empty'] and snapshot['visible'] == 0):
                    log.warning('browser authorized listing DOM unrecognized')
                    return self.result('error', items=[], error=UNRECOGNIZED_CARDS)
                return self.result('authorized', items=list(items.values()))
            except (PlaywrightError, asyncio.TimeoutError, ValueError, SourceError) as exc:
                # Do not log exception messages/tracebacks: Playwright can include URLs/HTML.
                log.warning('browser collection error stage=%s error_type=%s', stage, type(exc).__name__)
                try:
                    state = await self.inspect()
                    if state in ('confirmation_required', 'login_required'):
                        return self.result(state, items=[])
                except (PlaywrightError, asyncio.TimeoutError):
                    log.warning('browser collection error stage=classification_recovery')
                return self.result('error', items=[], error=f'Не удалось прочитать выдачу ({type(exc).__name__}). Проверьте браузер вручную.')


@asynccontextmanager
async def lifespan(app):
    PROFILE.mkdir(parents=True, exist_ok=True)
    profile_lock = (PROFILE / '.xata-owner.lock').open('a')
    fcntl.flock(profile_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                str(PROFILE / 'chromium'), headless=False, viewport={'width': 1280, 'height': 900},
                # Ordinary Playwright defaults; no stealth, UA or fingerprint overrides.
            )
            context.set_default_timeout(DOM_TIMEOUT_MS)
            context.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)
            collector = Collector(context)
            collector.page = context.pages[0] if context.pages else await context.new_page()
            for extra in context.pages[1:]:
                await extra.close()

            async def restrict_navigation(route):
                if route.request.is_navigation_request() and route.request.frame == collector.page.main_frame:
                    try:
                        validate_url(route.request.url)
                    except ValueError:
                        await route.abort()
                        return
                await route.continue_()

            await context.route('**/*', restrict_navigation)
            app.state.collector = collector
            # No automatic external navigation at startup, including after CAPTCHA.
            try:
                yield
            finally:
                await context.close()
    finally:
        profile_lock.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)


@app.get('/healthz')
async def health():
    collector = app.state.collector
    try:
        # Persistent contexts need not expose a separate Browser object.
        await collector.page.evaluate('1')
    except PlaywrightError as exc:
        raise HTTPException(503, 'Chromium disconnected') from exc
    return {'status': 'ok'}


@app.get('/status', dependencies=[Depends(auth)])
async def status():
    collector = app.state.collector
    try:
        state = (collector.current if collector.lock.locked() else
                 'unknown' if collector.page.url == 'about:blank' else await collector.inspect())
        return collector.result(state, running=collector.lock.locked(), manual_access=MANUAL)
    except (PlaywrightError, asyncio.TimeoutError):
        return collector.result('error', running=collector.lock.locked(), manual_access=MANUAL)


@app.post('/collect', dependencies=[Depends(auth)])
async def collect(search: Search):
    try:
        validate_url(search.url)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return await app.state.collector.collect(search.url)
