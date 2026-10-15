"""Replaceable listing source. No challenge solving or private API access."""
import json
import re
from html import unescape
from urllib.parse import urlencode
import httpx


class SourceError(Exception):
    pass


def search_url(filters):
    return 'https://www.cian.ru/cat.php?' + urlencode({
        'deal_type': 'sale', 'engine_version': 2, 'offer_type': 'flat',
        'region': 1, 'room1': 1, 'object_type[0]': 1,
        'maxprice': filters['max_price'], 'foot_min': filters['metro_minutes'],
        'only_foot': 2,
    })


def parse_page(html):
    lower = html.lower()
    if any(marker in lower for marker in ('captcha', 'докажите, что вы не робот', 'доступ ограничен')):
        raise SourceError('Циан запросил CAPTCHA или ограничил доступ. Сбор остановлен.')
    scripts = re.findall(r'<script\b[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.S | re.I)
    offers = []
    def visit(value):
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            if value.get('@type') in ('Apartment', 'Product', 'RealEstateListing'):
                offer = value.get('offers') or {}
                if isinstance(offer, list):
                    offer = offer[0] if offer else {}
                url = value.get('url') or offer.get('url') or ''
                match = re.fullmatch(r'https://(?:www\.)?cian\.ru/sale/flat/(\d+)/?', url)
                price = offer.get('price')
                if match and price is not None:
                    try:
                        price = int(float(str(price).replace(' ', '')))
                    except (ValueError, TypeError):
                        return
                    address = value.get('address') or {}
                    if isinstance(address, dict):
                        address = ', '.join(str(address[k]) for k in ('addressLocality', 'streetAddress') if address.get(k))
                    offers.append({'id': match[1], 'title': str(value.get('name', 'Квартира')), 'price': price,
                                   'address': str(address), 'url': url})
            for child in value.values():
                if isinstance(child, (dict, list)):
                    visit(child)
    for script in scripts:
        try:
            visit(json.loads(unescape(script)))
        except (ValueError, TypeError):
            continue
    if not offers:
        raise SourceError('В доступном HTML нет поддерживаемых данных объявлений. Возможно, изменилась разметка или нужна авторизация. Пустая выдача не подтверждена.')
    return list({item['id']: item for item in offers}.values())


def collect(filters):
    try:
        with httpx.Client(timeout=30, follow_redirects=False, headers={'User-Agent': 'XataCollector/0.1 (personal property search)'}) as client:
            response = client.get(search_url(filters))
        if 'captcha' in response.headers.get('location', '').lower():
            raise SourceError('Циан перенаправил запрос на CAPTCHA. Сбор остановлен, обход защиты не выполняется.')
        if response.status_code != 200:
            raise SourceError(f'Циан вернул HTTP {response.status_code}. Сбор остановлен, обход ограничений не выполняется.')
        return [item for item in parse_page(response.text) if item['price'] <= filters['max_price']]
    except httpx.HTTPError as exc:
        raise SourceError(f'Ошибка подключения к Циан ({type(exc).__name__}). Проверьте доступ с сервера.') from exc
