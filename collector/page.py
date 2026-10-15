"""Only rendered user-facing DOM. No embedded state, API, tokens or cookies."""
import re
from urllib.parse import urlsplit

from app.csv_source import listing_id, parse_card_text

ATTENTION = 'Требуется ручное подтверждение в браузере'
CARD_SELECTOR = '[data-name="CardComponent"], [data-name="OfferCard"]'
AUTH_SELECTOR = '[data-name="UserAvatar"], [data-name="ProfileMenu"]'
LOGIN_SELECTOR = 'button:has-text("Войти"), a:has-text("Войти"), input[type="password"], input[autocomplete="one-time-code"]'
CHALLENGE_SELECTOR = 'iframe[src*="captcha"], input[name*="captcha"], [data-name="Captcha"]'


def validate_url(url):
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or '').lower()
        if (parsed.scheme != 'https' or parsed.username or parsed.password or
                parsed.port not in (None, 443) or not (host == 'cian.ru' or host.endswith('.cian.ru'))):
            raise ValueError()
        if len(url) > 4096:
            raise ValueError()
    except ValueError as exc:
        raise ValueError('Разрешён только HTTPS search URL на cian.ru без учётных данных.') from exc
    return url


def classify(url, text, *, challenge=False, login=False, authorized=False, http_status=200):
    """Challenge wins over cards/auth; inspect visible text, never HTML/scripts."""
    lower = text.lower()
    path = urlsplit(url).path.lower()
    if (challenge or http_status in (403, 429) or
            any(marker in path for marker in ('captcha', 'challenge', 'verify', 'tmgrdfrend')) or
            any(marker in lower for marker in (
                'подтвердите, что вы человек', 'докажите, что вы не робот',
                'вы не робот', 'доступ ограничен', 'проверка безопасности', 'дополнительная проверка', 'подтвердите действие',
                'пройдите проверку', 'подтвердите вход', 'введите код',
                'подтвердите номер', 'подозрительная активность', 'captcha'))):
        return 'confirmation_required'
    if login or any(marker in path for marker in ('/authenticate', '/login', '/signin')):
        return 'login_required'
    return 'authorized' if authorized else 'unknown'


def normalize_card(card):
    """Best effort optional fields, preserve visible card text for verification."""
    url = card.get('url', '')
    item_id = listing_id(url)
    text = card.get('text', '')[:10000]
    item = dict(parse_card_text(text), id=item_id, url=url, card_text=text)
    title = card.get('title', '').strip()
    if title:
        item['title'] = title[:10000]
    price_text = card.get('price', '')
    price = re.search(r'(\d[\d\s\u00a0\u202f]*)\s*(?:₽|руб)', price_text, re.I)
    if price:
        item['price'] = int(re.sub(r'\s', '', price[1]))
    area = re.search(r'(\d+(?:[,.]\d+)?)\s*м[²2]', title or text)
    if area:
        item['area'] = float(area[1].replace(',', '.'))
    for field in ('address', 'complex_name', 'metro', 'metro_time'):
        if card.get(field):
            item[field] = card[field][:10000]
    return item


# All text comes from innerText of visible DOM elements.
EXTRACT_CARD = r"""el => {
  const visible = node => !!node && !!(node.offsetWidth || node.offsetHeight || node.getClientRects().length)
      && getComputedStyle(node).visibility !== 'hidden';
  const text = selector => {
    const node = [...el.querySelectorAll(selector)].find(visible);
    return node ? node.innerText.trim() : '';
  };
  const link = [...el.querySelectorAll('a[href*="/sale/flat/"]')].find(visible);
  return {
    url: link ? link.href : '',
    title: text('[data-mark="OfferTitle"], [data-mark="OfferSubtitle"], [data-name="Title"]'),
    price: text('[data-mark="MainPrice"], [data-name="Price"]'),
    address: [...el.querySelectorAll('[data-name="GeoLabel"]')].filter(visible).map(n => n.innerText).join(', '),
    complex_name: text('[data-name="ResidentialComplex"]'),
    metro: text('[data-name="UndergroundLabel"]'),
    metro_time: text('[data-name="TimeLabel"]'),
    text: el.innerText
  };
}"""
