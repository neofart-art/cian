"""One persistent headed Chromium, serialized jobs, manual challenges only."""
import asyncio
import fcntl
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, ConfigDict
from playwright.async_api import async_playwright, Error as PlaywrightError

from app.source_types import SourceError
from collector.diagnostics import diagnose_current_page

from collector.page import (ATTENTION, AUTH_SELECTOR, CARD_SELECTOR, CHALLENGE_SELECTOR,
                            LOGIN_SELECTOR, EMPTY_SELECTOR, VISIBLE_NODES, CARD_SNAPSHOT,
                            PAGINATION_SNAPSHOT, SCROLL_RESULTS, CARD_PROGRESS, next_search_url,
                            UNRECOGNIZED_CARDS, classify, normalize_card, validate_url)

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('xata.browser')

DOM_TIMEOUT_MS = 4500
SNAPSHOT_TIMEOUT_SECONDS = 3
NAVIGATION_TIMEOUT_MS = 20000
DOM_WAIT_SECONDS = 8
# Hard bounds keep the job inside the app's 110 s HTTP timeout.
JOB_TIMEOUT_SECONDS = 90
MAX_ITEMS = 100
MAX_PAGES = 4
MAX_SCROLLS = 6
NO_NEW_ID_ATTEMPTS = 2
PAGE_SCROLL_SECONDS = 6
# Start another navigation only with its normal timeout + readiness + final check reserve.
FINAL_CHECK_RESERVE_SECONDS = 8
MIN_PAGE_START_SECONDS = NAVIGATION_TIMEOUT_MS / 1000 + DOM_WAIT_SECONDS + SNAPSHOT_TIMEOUT_SECONDS
HEALTH_TIMEOUT_SECONDS = 2
SCROLL_WAIT_SECONDS = .5
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
        self.page_crashed = False
        self.runtime_events = dict(page_crash=0, page_close=0, context_close=0, browser_disconnect=0)

    def watch_runtime(self):
        # Log event names/counters only; Playwright event objects may contain URLs.
        def event(name):
            self.runtime_events[name] += 1
            if name == 'page_crash':
                self.page_crashed = True
            self.current = 'error'
            log.warning('browser runtime event=%s count=%d', name, self.runtime_events[name])
        self.page.on('crash', lambda *_: event('page_crash'))
        self.page.on('close', lambda *_: event('page_close'))
        self.page.on('domcontentloaded', lambda *_: setattr(self, 'page_crashed', False))
        self.context.on('close', lambda *_: event('context_close'))
        browser = self.context.browser
        if browser is not None:
            browser.on('disconnected', lambda *_: event('browser_disconnect'))

    def runtime_status(self):
        browser = getattr(self.context, 'browser', None)
        return dict(self.runtime_events, page_crashed=self.page_crashed,
            page_closed=bool(getattr(self.page, 'is_closed', lambda: False)()),
            browser_connected=browser.is_connected() if browser is not None else None)

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
            self.coverage = dict(pages=0, scrolls=0, snapshots=0, progress_probes=0,
                matching_node_observations=0, dom_skipped_observations=0,
                normalized_observations=0, normalization_errors=0, over_snapshot_limit_observations=0,
                duplicate_observations=0, unique_cards=0, pages_detail=[])
            items = {}
            loop = asyncio.get_running_loop()
            started = loop.time()
            job_deadline = started + JOB_TIMEOUT_SECONDS
            reason = 'error'
            result = None
            stage = 'page'
            try:
                async with asyncio.timeout(JOB_TIMEOUT_SECONDS):
                    visited = set()
                    for page_number in range(1, MAX_PAGES + 1):
                        if page_number > 1 and job_deadline - loop.time() < MIN_PAGE_START_SECONDS + FINAL_CHECK_RESERVE_SECONDS:
                            reason = 'time_budget'
                            break
                        if url in visited:
                            reason = 'repeated_page'
                            break
                        visited.add(url)
                        self.coverage['pages'] += 1
                        page_started = loop.time()
                        page_before = len(items)
                        detail = dict(number=page_number, scrolls=0, new_ids=0)
                        self.coverage['pages_detail'].append(detail)
                        result = await self._read_page(url)
                        observed_ids = set()
                        stalls = 0
                        scroll_deadline = min(loop.time() + PAGE_SCROLL_SECONDS,
                                              job_deadline - FINAL_CHECK_RESERVE_SECONDS)
                        for scroll_number in range(MAX_SCROLLS + 1):
                            if result['status'] != 'authorized':
                                reason = result['status']
                                break
                            if result.get('items'):
                                for item in result['items']:
                                    if item['id'] in items:
                                        self.coverage['duplicate_observations'] += 1
                                    if item['id'] in items or len(items) < MAX_ITEMS:
                                        items[item['id']] = item
                                    observed_ids.add(item['id'])
                                # Do not normalize/re-absorb an unchanged snapshot.
                                result = dict(result, items=[])
                            detail['new_ids'] = len(items) - page_before
                            self.coverage['unique_cards'] = len(items)
                            if len(items) >= MAX_ITEMS:
                                reason = 'item_limit'
                                break
                            if result.get('empty'):
                                detail['scroll_stop_reason'] = 'empty'
                                break
                            if scroll_number == MAX_SCROLLS:
                                detail.update(scroll_stop_reason='scroll_limit', scroll_limit_reached=True)
                                break
                            if loop.time() >= scroll_deadline:
                                detail['scroll_stop_reason'] = 'scroll_time_budget'
                                break
                            stage = 'scroll'
                            await asyncio.wait_for(self.page.evaluate(SCROLL_RESULTS),
                                                   SNAPSHOT_TIMEOUT_SECONDS)
                            self.coverage['scrolls'] += 1
                            detail['scrolls'] += 1
                            await asyncio.sleep(SCROLL_WAIT_SECONDS)
                            stage = 'classification_after_scroll'
                            state = await self.inspect()
                            if state != 'authorized':
                                result = self.result(state, items=[])
                                reason = state
                                break
                            stage = 'card_progress'
                            progress = await asyncio.wait_for(self.page.evaluate(CARD_PROGRESS),
                                                              SNAPSHOT_TIMEOUT_SECONDS)
                            self.coverage['progress_probes'] += 1
                            new_ids = set(progress['ids']) - observed_ids
                            observed_ids.update(progress['ids'])
                            log.info('browser progress page=%d new_ids=%d unchanged_attempts=%d',
                                     page_number, len(new_ids), stalls + 1 if not new_ids else 0)
                            if not new_ids:
                                stalls += 1
                                if stalls >= NO_NEW_ID_ATTEMPTS:
                                    detail['scroll_stop_reason'] = 'no_new_ids'
                                    break
                                continue
                            stalls = 0
                            stage = 'page_after_scroll'
                            result = await self._read_page(None)
                        detail['elapsed_seconds'] = round(loop.time() - page_started, 3)
                        if result['status'] != 'authorized' or reason == 'item_limit':
                            break
                        if page_number > 1 and len(items) == page_before:
                            reason = 'no_new_ids'
                            break
                        stage = 'pagination'
                        pagination = await asyncio.wait_for(
                            self.page.evaluate(PAGINATION_SNAPSHOT), SNAPSHOT_TIMEOUT_SECONDS)
                        state = await self.inspect()
                        if state != 'authorized':
                            result = self.result(state, items=[])
                            reason = state
                            break
                        if not pagination['href']:
                            reason = 'unsupported_next_control' if pagination['unsupported'] else 'no_next_link'
                            break
                        if page_number == MAX_PAGES:
                            reason = 'page_limit'
                            break
                        try:
                            url = next_search_url(self.page.url, pagination['href'])
                        except ValueError:
                            reason = 'unsafe_or_changed_pagination'
                            break
                        stage = 'page'
                    # Also check after normal budget/limit stops; an error never saves a partial batch.
                    if result is not None and result['status'] == 'authorized':
                        stage = 'classification_final'
                        state = await asyncio.wait_for(self.inspect(), FINAL_CHECK_RESERVE_SECONDS)
                        if state != 'authorized':
                            result = self.result(state, items=[])
                            reason = state
            except (PlaywrightError, asyncio.TimeoutError, ValueError) as exc:
                log.warning('browser collection error stage=%s error_type=%s', stage, type(exc).__name__)
                reason = ('job_deadline' if isinstance(exc, asyncio.TimeoutError)
                          and asyncio.get_running_loop().time() - started >= JOB_TIMEOUT_SECONDS else 'error')
                result = self.result('error', items=[], error='Сбор остановлен на ошибке или общем лимите времени. Старые объявления сохранены.')
            self.coverage.update(exhaustive=False, stop_reason=reason,
                incomplete=(reason != 'no_next_link' or any(p.get('scroll_stop_reason') in ('scroll_limit', 'scroll_time_budget') for p in self.coverage['pages_detail'])
                            or self.coverage['over_snapshot_limit_observations'] > 0),
                elapsed_seconds=round(asyncio.get_running_loop().time() - started, 3))
            log.info('browser coverage pages=%d unique_cards=%d scrolls=%d stop_reason=%s',
                     self.coverage['pages'], len(items), self.coverage['scrolls'], reason)
            # A challenge/error on ANY page discards the entire batch.
            if result is None or result['status'] != 'authorized':
                result = result or self.result('error', items=[], error=READINESS_TIMEOUT)
                result['items'] = []
                return dict(result, coverage=self.coverage)
            return self.result('authorized', items=list(items.values()), coverage=self.coverage)

    async def _read_page(self, url):
        stage = 'navigation'
        try:
            status_code = 200
            if url is not None:
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
                    runtime = self.runtime_status()
                    if runtime['page_closed'] or runtime['page_crashed'] or runtime['context_close'] or runtime['browser_connected'] is False:
                        log.warning('browser unavailable stage=%s runtime=%s', stage, runtime)
                        return self.result('error', items=[], error='Страница или процесс Chromium недоступны. Профиль не удалён; проверьте browser service.')
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
                self.page.evaluate(CARD_SNAPSHOT),
                timeout=SNAPSHOT_TIMEOUT_SECONDS)
            self.coverage['snapshots'] += 1
            self.coverage['matching_node_observations'] += snapshot['matching']
            self.coverage['over_snapshot_limit_observations'] += max(0, snapshot['matching'] - 100)
            self.coverage['dom_skipped_observations'] += snapshot['skipped']
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
                    self.coverage['normalization_errors'] += 1
                    continue
                normalized += 1
                self.coverage['normalized_observations'] += 1
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
            return self.result('authorized', items=list(items.values()), empty=snapshot['empty'])
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
            collector.watch_runtime()
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
    runtime = collector.runtime_status()
    if runtime['page_closed'] or runtime['page_crashed'] or runtime['context_close'] or runtime['browser_connected'] is False:
        log.warning('browser health unavailable runtime=%s', runtime)
        raise HTTPException(503, 'Chromium page/context unavailable')
    try:
        # A failed ping does not prove Chromium disconnected (navigation can race).
        await asyncio.wait_for(collector.page.evaluate('1'), HEALTH_TIMEOUT_SECONDS)
    except (PlaywrightError, asyncio.TimeoutError) as exc:
        log.warning('browser health probe failed error_type=%s runtime=%s',
                    type(exc).__name__, collector.runtime_status())
        raise HTTPException(503, 'Chromium health probe failed') from exc
    return {'status': 'ok'}


@app.get('/status', dependencies=[Depends(auth)])
async def status():
    collector = app.state.collector
    try:
        state = (collector.current if collector.lock.locked() else
                 'unknown' if collector.page.url == 'about:blank' else await collector.inspect())
        return collector.result(state, running=collector.lock.locked(), manual_access=MANUAL,
                                runtime=collector.runtime_status())
    except (PlaywrightError, asyncio.TimeoutError):
        return collector.result('error', running=collector.lock.locked(), manual_access=MANUAL,
                                runtime=collector.runtime_status())


@app.post('/collect', dependencies=[Depends(auth)])
async def collect(search: Search):
    try:
        validate_url(search.url)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return await app.state.collector.collect(search.url)


class DiagnosticIDs(BaseModel):
    model_config=ConfigDict(extra='forbid')
    ids:list[Annotated[str,Field(pattern=r'^[0-9]{1,32}$',strict=True)]]=Field(min_length=1,max_length=20)
    max_price:int|None=Field(default=None,ge=100000,le=1000000000)


@app.post('/diagnostics/dom',dependencies=[Depends(auth)])
async def diagnostic_dom(request:DiagnosticIDs):
    return await diagnose_current_page(app.state.collector,
        list(dict.fromkeys(str(int(value)) for value in request.ids)),request.max_price)
