"""One server-side browser criterion set; no browser-search client overrides."""
import os
from urllib.parse import parse_qs, urlsplit

DEFAULT_MAX_PRICE = 14_500_000
DEFAULT_METRO_MINUTES = 20


def browser_criteria():
    price, minutes = DEFAULT_MAX_PRICE, DEFAULT_METRO_MINUTES
    url = os.getenv('CIAN_SEARCH_URL', '')
    if url:
        try:
            parsed = urlsplit(url)
            host = (parsed.hostname or '').lower()
            if (parsed.scheme != 'https' or parsed.username or parsed.password or
                    parsed.port not in (None, 443) or not (host == 'cian.ru' or host.endswith('.cian.ru')) or len(url) > 4096):
                raise ValueError('Invalid Cian search URL')
            query = parse_qs(parsed.query, max_num_fields=200)
            def parameter(name, fallback, low, high):
                values = query.get(name, [])
                if len(values) != 1 or not values[0].isascii() or not values[0].isdigit():
                    return fallback
                value = int(values[0])
                return value if low <= value <= high else fallback
            price = parameter('maxprice', price, 100_000, 1_000_000_000)
            minutes = parameter('foot_min', minutes, 1, 60)
        except ValueError:
            # Never reflect/log raw URLs. Collector keeps navigation validation.
            pass
    return {'max_price': price, 'metro_minutes': minutes}
