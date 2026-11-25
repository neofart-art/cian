"""Parse only the visible offer header, without seller-prose travel guesses."""
import re
from app.csv_source import listing_id, parse_card_text, travel_duration


PARAMETERS = re.compile(
    r'(?P<rooms>\d+)\s*-\s*комн\.\s*квартира,\s*'
    r'(?P<area>\d+(?:[,.]\d+)?)\s*м[²2],\s*'
    r'(?P<floor>\d+)\s*/\s*(?P<storeys>\d+)\s*этаж',
    re.I,
)


def visible_header(text):
    lines = []
    for raw in text.splitlines():
        line = re.sub(r'\s+', ' ', raw).strip()
        if (re.match(r'^(?:г\.?\s*)?Москва(?:,|$)', line, re.I)
                or re.match(r'^(?:от\s+)?\d[\d\s]*\s*(?:₽|руб)', line, re.I)
                or re.match(r'^Описание(?:\s*[:：]|\s*$)', line, re.I)):
            break
        if line:
            lines.append(line)
    return lines


def header_station(lines, expected):
    """Keep the displayed station and its immediately following time together."""
    def clean(value):
        return re.sub(r'^(?:метро\s*[:—-]?\s*|м\.\s*)', '', value, flags=re.I).strip()
    for index in range(1, len(lines)):
        duration = travel_duration(lines[index])
        if not duration:
            continue
        name = clean(lines[index - 1])
        # The final pair before the address is Cian's visible station header.
        # An exact extracted station match also supports multiple station pairs.
        same_station = name.casefold().replace('ё', 'е') == clean(expected or '').casefold().replace('ё', 'е')
        if not (same_station or index == len(lines) - 1):
            continue
        if not name or re.search(r'\d|[₽²/]', name) or PARAMETERS.fullmatch(name):
            continue
        return name, duration
    return None


def normalize_browser_card(card):
    text = card.get('text', '')[:10000]
    item = dict(parse_card_text(text), id=listing_id(card.get('url', '')),
                url=card.get('url', ''), card_text=text)
    title = card.get('title', '').strip()
    if title:
        item['title'] = title[:10000]
    lines = visible_header(text)
    parameters = next((PARAMETERS.fullmatch(line) for line in lines
                       if PARAMETERS.fullmatch(line)), None)
    if parameters:
        item['title'] = parameters[0]
        item['area'] = float(parameters['area'].replace(',', '.'))
        item['floor'] = parameters['floor'] + '/' + parameters['storeys']
    else:
        area = re.search(r'(\d+(?:[,.]\d+)?)\s*м[²2]', title)
        if area:
            item['area'] = float(area[1].replace(',', '.'))
    price = re.search(r'(\d[\d\s\u00a0\u202f]*)\s*(?:₽|руб)',
                      card.get('price', ''), re.I)
    if price:
        item['price'] = int(re.sub(r'\s', '', price[1]))
    for field in ('address', 'complex_name', 'metro'):
        if card.get(field):
            item[field] = card[field][:10000]
    item.pop('metro_time', None)
    item.pop('metro_walk_minutes', None)
    pair = header_station(lines, card.get('metro'))
    if pair:
        item['metro'], duration = pair
        item.update(duration)
    # Never fall back to a duration elsewhere in the seller's description.
    image = card.get('cover_image_url')
    if isinstance(image, str) and len(image) <= 4096:
        item['cover_image_url'] = image
    if card.get('search_section') == 'recommendation':
        item['search_section'] = 'recommendation'
    return item
