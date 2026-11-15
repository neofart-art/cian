"""Observe the SAME open page as noVNC; no navigation, scrolling, or imports."""
import asyncio
import hashlib
from urllib.parse import parse_qs, urlsplit

from fastapi import HTTPException
from playwright.async_api import Error as PlaywrightError

from collector.page import CARD_SELECTOR, EXTRACT_CARDS


SCOPED_SNAPSHOT = r'''selector => {
  const roots = selector ? [...document.querySelectorAll(selector)] : [];
  if (roots.length !== 1) return {scope_confirmed:false, containers:roots.length};
  const root = roots[0];
  if (!(root.offsetWidth || root.offsetHeight || root.getClientRects().length))
    return {scope_confirmed:false, containers:1};
  const nodes = [...root.querySelectorAll(''' + repr(CARD_SELECTOR) + r''')];
  return {...(''' + EXTRACT_CARDS + r''')(nodes), scope_confirmed:true, containers:1};
}'''

PREVIEW_DOM = r'''selector => {
  const visible = n => n.isConnected && !!(n.offsetWidth || n.offsetHeight || n.getClientRects().length)
    && getComputedStyle(n).visibility !== 'hidden';
  const roots = selector ? [...document.querySelectorAll(selector)] : [];
  const cards = [...document.querySelectorAll(''' + repr(CARD_SELECTOR) + r''')].filter(visible).slice(0,100);
  const offers = cards.map(card => {
    const link = [...card.querySelectorAll('a[href*="/sale/flat/"]')].find(visible);
    let id = null;
    try {const u = new URL(link.href); if (/(^|\.)cian\.ru$/.test(u.hostname))
      id = /^\/sale\/flat\/(\d+)\/?$/.exec(u.pathname)?.[1] || null;} catch (_) {}
    const ancestors = [];
    for (let p = card.parentElement; p && ancestors.length < 6; p = p.parentElement)
      ancestors.push({tag:p.tagName.toLowerCase(), data_name:(p.getAttribute('data-name') || '').slice(0,80),
                      classes:[...p.classList].slice(0,3).map(v=>v.slice(0,80))});
    return {id, in_selected_container:roots.length===1 && roots[0].contains(card), ancestors};
  }).filter(o => o.id);
  const headings = [...document.querySelectorAll('h1,h2,h3,[role="heading"]')]
    .filter(n => visible(n) && !n.closest(''' + repr(CARD_SELECTOR) + r''')).slice(0,30)
    .map(n => n.innerText.trim().slice(0,120));
  return {offers, headings, containers:roots.length, viewport:{width:innerWidth,height:innerHeight,scroll_y:Math.round(scrollY)},
          truncated:document.querySelectorAll(''' + repr(CARD_SELECTOR) + r''').length > 100};
}'''


def same_search(actual, configured):
    """Compare search constraints, never expose saved-search/account query values."""
    a, b = urlsplit(actual), urlsplit(configured)
    if any(p.scheme != 'https' or p.username or p.password or
           not ((p.hostname or '') == 'cian.ru' or (p.hostname or '').endswith('.cian.ru'))
           or p.port not in (None,443) for p in (a,b)):
        return False
    if a.hostname.removeprefix('www.') != b.hostname.removeprefix('www.'):
        return False
    if a.path.rstrip('/') != '/cat.php' or b.path.rstrip('/') != '/cat.php':
        return False
    def query(parts):
        values = parse_qs(parts.query, keep_blank_values=True, max_num_fields=200)
        return {k: sorted(v) for k, v in values.items()
                if k not in ('context', 'mlSearchSessionGuid', 'saved_search_id')}
    return query(a) == query(b)


async def preview_results(collector, selector='', configured=''):
    if collector.lock.locked():
        raise HTTPException(409, 'Browser busy; preview did not start collection')
    async with collector.lock:
        try:
            async with asyncio.timeout(10):
                state = await collector.inspect()
                if state != 'authorized':
                    return {'mode':'preview_no_import', 'status':state, 'offers':[]}
                before = collector.page.url
                result = await collector.page.evaluate(PREVIEW_DOM, selector)
                if collector.page.url != before:
                    raise HTTPException(409, 'Page changed during preview; retry')
                return dict(result, mode='preview_no_import', status=state,
                            query_matches_configured_search=same_search(before,configured),
                            page_url_sha256=hashlib.sha256(before.encode()).hexdigest())
        except (PlaywrightError, asyncio.TimeoutError, ValueError):
            raise HTTPException(503, 'Could not read current page; no collection started') from None
