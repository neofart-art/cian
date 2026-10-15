"""Local CSV source: never makes network requests."""
import csv
import io
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit
from app.source_types import SourceError

MAX_BYTES = 5 * 1024 * 1024
MAX_ROWS = 10000


def normalized(value):
    return re.sub(r'[^a-zа-я0-9]', '', value.lower().replace('ё', 'е').replace('²', '2'))


ALIASES = {
    'url': ('url', 'ссылка', 'ссылка на объявление', 'ссылка на циан', 'url объявления', 'link'),
    'id': ('id', 'id объявления', 'номер объявления', 'cian id'),
    'title': ('название', 'заголовок', 'title', 'объявление'),
    'price': ('цена', 'цена руб', 'цена ₽', 'стоимость', 'стоимость руб', 'price', 'price rub'),
    'address': ('адрес', 'address', 'жк/адрес', 'жк адрес', 'местоположение'),
    'complex_name': ('жк', 'жилой комплекс', 'название жк', 'complex', 'complex name'),
    'area': ('площадь', 'общая площадь', 'площадь м²', 'общая площадь м²', 'area', 'total area'),
    'floor': ('этаж', 'этаж/этажность', 'floor'),
    'metro': ('метро', 'станция метро', 'metro'),
    'card_text': ('card_text', 'текст карточки', 'полный текст'),
    'description': ('description', 'описание'),
    'metro_time': ('время до метро', 'до метро', 'минут до метро', 'метро мин', 'время до метро мин', 'metro time', 'metro minutes'),
}
LOOKUP = {normalized(alias): key for key, aliases in ALIASES.items() for alias in aliases}


def listing_id(url):
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ('https', 'http') or parsed.username or parsed.password or parsed.port not in (None, 80, 443):
            raise ValueError()
        if not parsed.hostname or not (parsed.hostname == 'cian.ru' or parsed.hostname.endswith('.cian.ru')):
            raise ValueError()
        match = re.fullmatch(r'/sale/flat/(\d+)/?', parsed.path)
        if not match:
            raise ValueError()
        return str(int(match[1]))
    except ValueError as exc:
        raise SourceError('Ссылка должна вести на объявление о продаже квартиры: https://www.cian.ru/sale/flat/123/.') from exc


def number(value, label, integer=False):
    if not value:
        return None
    cleaned = re.sub(r'(?i)(руб\.?|₽|rub|м²|м2|m²|m2|кв\.?\s*м\.?)', '', value)
    cleaned = re.sub(r'\s+', '', cleaned).replace(',', '.')
    try:
        result = Decimal(cleaned)
        if not result.is_finite() or result <= 0 or result > Decimal('1000000000000'):
            raise ValueError()
        if integer and result != result.to_integral_value():
            raise ValueError()
        return int(result) if integer else float(result)
    except (InvalidOperation, ValueError) as exc:
        raise SourceError(f'Поле «{label}»: ожидается положительное число, получено «{value[:60]}».') from exc


def parse_card_text(text):
    """Conservative best effort; raw text is always kept for manual verification."""
    result = {}
    lines = [re.sub(r'\s+', ' ', line).strip() for line in text.splitlines() if line.strip()]
    floor = re.search(r'(?<![\d.,])(\d{1,3})\s*/\s*(\d{1,3})\s*(?:эт(?:аж)?[а-я.]*)(?![а-я])', text, re.I)
    if floor:
        result['floor'] = f'{floor[1]}/{floor[2]}'
    else:
        floor = re.search(r'(?<![\d.,])(\d{1,3})(?:-?[йы])?\s*этаж(?:\s+из\s+(\d{1,3}))?', text, re.I)
        if floor:
            result['floor'] = floor[1] + ('/' + floor[2] if floor[2] else '')
        else:
            floor = re.search(r'этаж\s*[:—-]?\s*(\d{1,3})(?:\s*(?:из|/)\s*(\d{1,3}))?', text, re.I)
            if floor:
                result['floor'] = floor[1] + ('/' + floor[2] if floor[2] else '')
    for position, line in enumerate(lines):
        if not result.get('address') and re.match(r'^(?:г\.?\s*)?Москва(?:\s*,|$)', line, re.I):
            result['address'] = re.split(r'\s+(?:м\.|метро)\s', line, maxsplit=1, flags=re.I)[0].strip(' ,;')
        if not result.get('address'):
            address = re.search(r'(?<![А-Яа-я])Москва\s*,\s*(.+?)(?=\s+(?:м\.|метро)\s|[;|]|$)', line)
            if address:
                result['address'] = 'Москва, ' + address[1].strip()
        if not result.get('complex_name'):
            complex_match = re.match(r'^ЖК\s+(.+)', line, re.I)
            if complex_match:
                result['complex_name'] = complex_match[1].strip(' «»"')
        if not result.get('metro'):
            metro = re.search(r'(?:^|[\s,;|])(?:м\.\s*|метро\s*[:—-]?\s*)([А-ЯЁA-Z][^;|\n]+)', line)
            if metro:
                station = re.split(r'\s*[,·•|]\s*|\s+[—–]\s+|\s+\d+\s*мин', metro[1], maxsplit=1)[0].strip(' «»"')
                if station:
                    result['metro'] = station
        # Do not derive walking minutes from unqualified times or transport times.
        walk = re.search(r'(\d{1,3})\s*мин(?:ут[а-я]*)?\.?\s*(?:пешком|пеш(?:ая|ей)\s+(?:доступность|ходьб[а-я]*))', line, re.I)
        if not walk:
            walk = re.search(r'пешком\s*[:—-]?\s*(\d{1,3})\s*мин', line, re.I)
        metro_context = bool(re.search(r'(?:метро|(?:^|\s)м\.)', line, re.I)) or (
            bool(result.get('metro')) and bool(re.fullmatch(r'\d{1,3}\s*мин(?:ут[а-я]*)?\.?\s*пешком[.!]?', line, re.I))
            and any(re.match(r'^(?:м\.|метро)', previous, re.I) for previous in lines[max(0, position - 2):position]))
        if walk and metro_context and 'metro_walk_minutes' not in result:
            result['metro_walk_minutes'] = int(walk[1])
            result['metro_time'] = f'{int(walk[1])} мин. пешком'
        elif 'metro_time' not in result:
            transport = re.search(r'(\d{1,3})\s*мин\.?\s*(?:на транспорте|транспортом)', line, re.I)
            if transport:
                result['metro_time'] = f'{int(transport[1])} мин. транспортом'
    if text:
        parts = re.split(r'(?:^|\n)Описание\s*[:\n]\s*', text, maxsplit=1, flags=re.I)
        result['description'] = parts[-1].strip() if len(parts) > 1 else text
    return result


class CianCsvSource:
    name = 'ЦИАН CSV'

    def parse(self, content):
        if not content or len(content) > MAX_BYTES:
            raise SourceError('Выберите непустой CSV размером не больше 5 МБ.')
        encodings = ('utf-16',) if content.startswith((b'\xff\xfe', b'\xfe\xff')) else ('utf-8-sig', 'cp1251')
        text = None
        for encoding in encodings:
            try:
                text = content.decode(encoding)
                break
            except UnicodeError:
                continue
        if text is None or '\x00' in text:
            raise SourceError('Не удалось прочитать CSV. Поддерживаются UTF-8, UTF-16 с BOM и Windows-1251.')
        if re.search(r'[\x01-\x08\x0b\x0c\x0e-\x1f]', text):
            raise SourceError('CSV содержит недопустимые управляющие символы. Пересохраните файл как текстовый CSV.')
        # Excel may prepend its delimiter directive.
        lines = text.splitlines(keepends=True)
        separator = None
        if lines and re.fullmatch(r'sep=[;,\t]\s*', lines[0], re.I):
            separator = lines.pop(0)[4]
            text = ''.join(lines)
        if separator is None:
            header = next((line for line in lines if line.strip()), '')
            try:
                separator = csv.Sniffer().sniff(header, delimiters=';,\t').delimiter
            except csv.Error:
                separator = ';'
        reader = csv.reader(io.StringIO(text, newline=''), delimiter=separator, strict=True)
        try:
            headers = next((row for row in reader if any(cell.strip() for cell in row)), None)
            if not headers:
                raise SourceError('В CSV отсутствует строка заголовков.')
            mapping = {}
            for index, header in enumerate(headers):
                field = LOOKUP.get(normalized(header))
                if field:
                    if field in mapping.values():
                        raise SourceError(f'Дублируется столбец «{header}». Оставьте один столбец для каждого поля.')
                    mapping[index] = field
            if 'url' not in mapping.values():
                raise SourceError('Нет столбца со ссылкой. Ожидается «Ссылка», «Ссылка на объявление» или URL. Разделитель: точка с запятой, запятая или табуляция.')
            rows = {}
            found = 0
            for values in reader:
                if not any(cell.strip() for cell in values):
                    continue
                found += 1
                if found > MAX_ROWS:
                    raise SourceError('В CSV больше 10 000 записей. Разделите файл на части.')
                if len(values) != len(headers):
                    raise SourceError(f'Строка {reader.line_num}: количество полей не совпадает с заголовком. Проверьте разделитель и кавычки.')
                item = {field: values[index].strip() for index, field in mapping.items()}
                if any(len(value) > 10000 for value in item.values()):
                    raise SourceError(f'Строка {reader.line_num}: слишком длинное значение поля.')
                try:
                    item_id = listing_id(item['url'])
                    if item.get('id') and (not item['id'].isdigit() or int(item['id']) != int(item_id)):
                        raise SourceError('ID не совпадает с ID в ссылке объявления.')
                    item.update(id=item_id, price=number(item.get('price', ''), 'Цена', True),
                                area=number(item.get('area', ''), 'Площадь'))
                except SourceError as exc:
                    raise SourceError(f'Строка {reader.line_num}: {exc}') from exc
                extracted = parse_card_text(item.get('card_text', ''))
                item = {**extracted, **{key: value for key, value in item.items() if value not in (None, '')}}
                item['source'] = self.name
                # Last occurrence wins, but empty cells never erase supplied values.
                previous = rows.get(item_id, {})
                rows[item_id] = {**previous, **{key: value for key, value in item.items() if value not in (None, '')}}
                if item.get('metro_time'):
                    rows[item_id]['metro_walk_minutes'] = item.get('metro_walk_minutes')
            if not found:
                raise SourceError('В CSV есть заголовки, но нет объявлений.')
            return {'items': list(rows.values()), 'found': found, 'duplicates': found - len(rows),
                    'fields': list(mapping.values()), 'encoding': encoding}
        except csv.Error as exc:
            raise SourceError(f'Ошибка структуры CSV около строки {reader.line_num}: проверьте кавычки и разделители.') from exc
