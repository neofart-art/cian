"""Validate browser cards before upsert; never infer a district from a metro name.

CIAN_DISTRICT_POLICY binds a verified name allowlist to the URL's selected IDs.
Unknown fields are held out, rather than silently passing URL constraints.
"""
import json
import math
import re
import unicodedata
from collections import Counter
from urllib.parse import parse_qs, urlsplit

from app.csv_source import travel_duration
from app.source_types import SourceError


def normalized(value):
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', str(value))).strip().casefold().replace('ё', 'е')


def number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


class Admission:
    def __init__(self, url, filters, district_policy='{}'):
        self.query = parse_qs(urlsplit(url).query, max_num_fields=200)
        self.max_price = filters['max_price']
        self.minutes = filters['metro_minutes']
        ids = [v for k, values in self.query.items() if re.fullmatch(r'district\[\d+\]', k) for v in values]
        self.districts_required = bool(ids)
        try:
            policy = json.loads(district_policy)
            if not isinstance(policy, dict):
                raise ValueError()
            if policy:
                bound = policy.get('ids')
                names = policy.get('names')
                if (not isinstance(bound, list) or not isinstance(names, list)
                    or any(not isinstance(v, str) or not v.isascii() or not v.isdigit() for v in bound)
                    or set(bound) != set(ids)
                    or any(not isinstance(v, str) or not v.strip() for v in names)
                    or len({normalized(v) for v in names}) != len(set(ids))):
                    raise ValueError()
            self.allowed_names = {normalized(v) for v in policy.get('names', [])}
        except (TypeError, ValueError):
            raise SourceError('CIAN_DISTRICT_POLICY: названия районов не подтверждены для текущего набора ID.') from None
        self.missing_district_names = len(set(ids)) if not policy else 0
        self.min_area = self.parameter('mintarea')
        self.rooms = [n for n in range(1, 7) if self.query.get(f'room{n}') == ['1']]
        self.unchecked = sorted(k for k in self.query if k in (
            'minlift', 'object_type[0]', 'flat_share', 'only_flat',
            'sost_type[0]', 'demolished_in_moscow_programm',
        ) or k.startswith('repair['))

    def parameter(self, name):
        values = self.query.get(name, [])
        if not values:
            return None
        if len(values) != 1 or number(values[0]) is None or float(values[0]) < 0:
            raise SourceError('Не удалось однозначно прочитать числовые условия поиска.')
        return float(values[0])

    def reasons(self, item):
        reasons = []
        text = normalized(item.get('card_text') or '')
        if item.get('search_section') == 'recommendation':
            reasons.append('recommendation')
        # Independent user policy: do not guess the meaning of electronic_trading=2.
        if re.search(r'\bаукцион\w*|\b(?:электронн\w*|городск\w*)\s+торг\w*|\bторги\b', text):
            reasons.append('auction')
        price = number(item.get('price'))
        if price is None or price <= 0:
            reasons.append('price_unknown')
        elif price > self.max_price:
            reasons.append('price')
        if self.min_area is not None:
            area = number(item.get('area'))
            if area is None or area <= 0:
                reasons.append('area_unknown')
            elif area < self.min_area:
                reasons.append('area')
        if self.rooms:
            rooms = re.search(r'\b(\d+)\s*-?\s*комн', normalized(item.get('title') or ''))
            if not rooms:
                reasons.append('rooms_unknown')
            elif int(rooms[1]) not in self.rooms:
                reasons.append('rooms')
        if self.query.get('only_foot') == ['2']:
            # The raw explicit mode wins over a stale/inferred walk-minute field.
            duration = travel_duration(item.get('metro_time'))
            if not duration:
                reasons.append('walk_unknown')
            elif duration['metro_walk_minutes'] is None:
                reasons.append('transport')
            elif duration['metro_walk_minutes'] > self.minutes:
                reasons.append('walk_time')
        floor = re.fullmatch(r'\s*(\d+)\s*/\s*(\d+)\s*', str(item.get('floor') or ''))
        if self.query.get('is_first_floor') == ['0']:
            if not floor:
                reasons.append('floor_unknown')
            elif int(floor[1]) <= 1:
                reasons.append('first_floor')
        # minfloorn means building storeys, not the apartment floor.
        storeys = self.parameter('minfloorn')
        if storeys is not None:
            if not floor:
                reasons.append('storeys_unknown')
            elif int(floor[2]) < storeys:
                reasons.append('storeys')
        if self.districts_required:
            match = re.search(r'(?:^|,)\s*р(?:айо)?-?н\s+([^,]+)', normalized(item.get('address') or ''))
            if not match:
                reasons.append('district_unknown')
            elif normalized(match[1]) not in self.allowed_names:
                reasons.append('district_unverified' if self.missing_district_names else 'district')
        return reasons

    def apply(self, items):
        admitted, rejected = [], Counter()
        rejected_cards = 0
        for item in items:
            if not isinstance(item, dict):
                rejected['malformed_card'] += 1
                rejected_cards += 1
                continue
            reasons = self.reasons(item)
            if reasons:
                rejected.update(set(reasons))
                rejected_cards += 1
            else:
                admitted.append(item)
        return admitted, dict(received=len(items), admitted=len(admitted), rejected=rejected_cards,
                             reasons=dict(rejected), missing_district_names=self.missing_district_names,
                             not_independently_verified=self.unchecked)
