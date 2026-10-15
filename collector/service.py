"""One persistent headed Chromium, serialized jobs, manual challenges only."""
import asyncio
import fcntl
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from playwright.async_api import async_playwright, Error as PlaywrightError

from app.source_types import SourceError

from collector.page import (ATTENTION, AUTH_SELECTOR, CARD_SELECTOR, CHALLENGE_SELECTOR,
                            LOGIN_SELECTOR, EXTRACT_CARD, classify, normalize_card, validate_url)

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
        for node in await self.page.locator(selector).all():
            if await node.is_visible():
                return True
        return False

    async def inspect(self, http_status=200):
        text = await self.page.locator('body').inner_text(timeout=3000)
        # An OTP/password form is always a manual action, even alongside old cards.
        self.current = classify(
            self.page.url, text,
            challenge=await self.visible(CHALLENGE_SELECTOR),
            login=await self.visible(LOGIN_SELECTOR),
            authorized=await self.visible(AUTH_SELECTOR),
            http_status=http_status)
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
            try:
                response = await self.page.goto(url, wait_until='domcontentloaded', timeout=45000)
                status_code = response.status if response else 200
                if status_code in (401, 403, 429):
                    return self.result('login_required' if status_code == 401 else 'confirmation_required', items=[])
                if status_code >= 400:
                    return self.result('error', items=[], error=f'ЦИАН вернул HTTP {status_code}')
                # Poll only this rendered page; no retries/navigation/challenge solving.
                for _ in range(60):
                    status = await self.inspect(status_code)
                    if status in ('confirmation_required', 'login_required'):
                        return self.result(status, items=[])
                    cards = self.page.locator(CARD_SELECTOR)
                    if status == 'authorized' and (await cards.count() or await self.visible('[data-name="EmptyOffers"], [data-name="NothingFound"]')):
                        break
                    await asyncio.sleep(.5)
                status = await self.inspect(status_code)
                if status == 'unknown':
                    return self.result('error', items=[], error='Не удалось подтвердить авторизацию по DOM. Проверьте меню профиля на OMV.')
                if status != 'authorized':
                    return self.result(status, items=[])
                items = {}
                for card in (await self.page.locator(CARD_SELECTOR).all())[:100]:
                    if not await card.is_visible():
                        continue
                    raw = await card.evaluate(EXTRACT_CARD)
                    # Invalid/missing offer links are unsupported layout, never a successful empty result.
                    item = normalize_card(raw)
                    items[item['id']] = item
                # Abort the whole batch if a challenge appeared during extraction.
                status = await self.inspect(status_code)
                if status != 'authorized':
                    return self.result(status, items=[])
                if not items and not await self.visible('[data-name="EmptyOffers"], [data-name="NothingFound"]'):
                    return self.result('error', items=[], error='Не распознана обычная выдача ЦИАН. Требуется проверка DOM на OMV.')
                return self.result('authorized', items=list(items.values()))
            except (PlaywrightError, ValueError, SourceError) as exc:
                # A navigation timeout may already show a challenge. Inspect once,
                # without retrying navigation or interacting with the challenge.
                try:
                    state = await self.inspect()
                    if state in ('confirmation_required', 'login_required'):
                        return self.result(state, items=[])
                except PlaywrightError:
                    pass
                # No URL, HTML, cookies or login data in error responses/logs.
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
    except PlaywrightError:
        return collector.result('error', running=collector.lock.locked(), manual_access=MANUAL)


@app.post('/collect', dependencies=[Depends(auth)])
async def collect(search: Search):
    try:
        validate_url(search.url)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return await app.state.collector.collect(search.url)
