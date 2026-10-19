"""Deterministic local matching of Cian relistings to the same real property."""
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
import math
from difflib import SequenceMatcher
import re

AUTO_MATCH_SCORE = 85
POSSIBLE_MATCH_SCORE = 65
AUTO_MATCH_MARGIN = 15


@dataclass(frozen=True)
class MatchResult:
    score: int
    reasons: tuple[str, ...]
    strong_identity: bool

    @property
    def auto(self):
        return self.score >= AUTO_MATCH_SCORE and self.strong_identity

    @property
    def possible(self):
        return self.score >= POSSIBLE_MATCH_SCORE


def _plain(value):
    return re.sub(r"\s+", " ", str(value or "").lower().replace("ё", "е")).strip()


def normalize_address(value):
    text = _plain(value)
    replacements = (
        (r"\bг(?:ород)?\.?\s*москва\b", ""),
        (r"\bулица\b|\bул\.", " ул "),
        (r"\bпроспект\b|\bпр-т\b|\bпросп\.", " проспект "),
        (r"\bпереулок\b|\bпер\.", " переулок "),
        (r"\bшоссе\b|\bш\.", " шоссе "),
        (r"\bдом\b|\bд\.", " д "),
        (r"\bкорпус\b|\bкорп\.", " корп "),
        (r"\bстроение\b|\bстр\.", " стр "),
    )
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text)
    text = re.sub(r"(\d+)\s*к\s*(\d+)\b", r"\1 корп \2", text)
    text = re.sub(r"\b(?:дом|д)\.?\s*(?=\d)", " д ", text)
    return re.sub(r"[^a-zа-я0-9/]+", " ", text).strip()


def _building_parts(address):
    house = re.search(r"(?:^| )д ([0-9]+(?:/[0-9]+)?[a-zа-я]?)\b", address)
    if not house:
        without_suffix = re.sub(r"(?:^| )(?:корп|стр|кв|квартира) [0-9]+[a-zа-я]?\b", "", address)
        house = re.search(r"(?:^| )([0-9]+(?:/[0-9]+)?[a-zа-я]?)$", without_suffix)
    corpus = re.search(r"(?:^| )корп ([0-9]+[a-zа-я]?)\b", address)
    structure = re.search(r"(?:^| )стр ([0-9]+[a-zа-я]?)\b", address)
    apartment = re.search(r"(?:^| )(?:кв|квартира) ([0-9]+[a-zа-я]?)\b", address)
    return tuple(match.group(1) if match else None for match in (house, corpus, structure, apartment))


def normalize_text(value):
    text = _plain(value)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"\b(?:телефон|звоните|агент|риелтор|собственник)\b", " ", text)
    return re.sub(r"[^a-zа-я0-9]+", " ", text).strip()


def _first_floor(value):
    match = re.match(r"\s*(-?\d{1,3})(?:\s*/\s*\d{1,3})?(?:\s|$)", str(value or ""))
    return int(match.group(1)) if match else None


def _ratio(a, b):
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a[:2000], b[:2000], autojunk=True).ratio()


def identity_text(item):
    text = normalize_text(item.get("description") or item.get("card_text"))
    # Rendered address/price/area/metro are not independent description evidence.
    for field in ("title", "address", "complex_name", "metro", "floor"):
        value = normalize_text(item.get(field))
        if value:
            text = text.replace(value, " ")
    text = re.sub(r"\b\d[\d ]*\s*(?:руб|м2|мин|этаж)\w*\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def informative_text(text):
    marketing = set("квартира светлая уютная просторная большая кухня окна во двор один собственник "
                    "продажа продается продаю звоните агент риелтор ремонт хороший хорошая состояние "
                    "отличное удобная планировка документы готовы быстрый выход на сделку без "
                    "обременений ипотека торг возможен рядом метро".split())
    words = set(text.split())
    distinctive = [word for word in words if len(word) >= 4 and word not in marketing]
    return len(text) >= 50 and len(words) >= 8 and len(distinctive) >= 3


def listing_match(item, candidate):
    """Return a conservative score for two listings being the same apartment.

    Automatic merging needs strong identity evidence in addition to a score.
    This intentionally prefers false negatives over joining two different flats.
    """
    reasons = []
    score = 0

    address_a = normalize_address(item.get("address"))
    address_b = normalize_address(candidate.get("address"))
    if address_a and address_b:
        building_a, building_b = _building_parts(address_a), _building_parts(address_b)
        if building_a[0] and building_b[0] and building_a[0] != building_b[0]:
            return MatchResult(0, ("different-building",), False)
        if building_a[1] and building_b[1] and building_a[1] != building_b[1]:
            return MatchResult(0, ("different-building",), False)
        if building_a[2] and building_b[2] and building_a[2] != building_b[2]:
            return MatchResult(0, ("different-building",), False)
        if building_a[3] and building_b[3] and building_a[3] != building_b[3]:
            return MatchResult(0, ("different-apartment",), False)
        ratio = _ratio(address_a, address_b)
        if address_a == address_b:
            score += 40
            reasons.append("address")
        elif ratio >= 0.93:
            score += 34
            reasons.append("address-near")
        else:
            return MatchResult(0, ("different-address",), False)
    else:
        # Without a usable address there is not enough evidence to silently merge.
        reasons.append("address-missing")

    area_a, area_b = item.get("area"), candidate.get("area")
    area_close = False
    if area_a is not None and area_b is not None:
        diff = abs(float(area_a) - float(area_b))
        if not math.isfinite(diff):
            return MatchResult(0, ("invalid-area",), False)
        if diff <= 0.3:
            score += 20
            area_close = True
            reasons.append("area")
        elif diff <= 1.0:
            score += 10
            reasons.append("area-near")
        elif diff > 2.5:
            score -= 30
            reasons.append("area-different")

    floor_a, floor_b = _first_floor(item.get("floor")), _first_floor(candidate.get("floor"))
    floor_same = False
    if floor_a is not None and floor_b is not None:
        if floor_a == floor_b:
            score += 15
            floor_same = True
            reasons.append("floor")
        else:
            score -= 30
            return MatchResult(0, ("floor-different",), False)

    complex_a = normalize_text(item.get("complex_name"))
    complex_b = normalize_text(candidate.get("complex_name"))
    if complex_a and complex_b and complex_a == complex_b:
        score += 10
        reasons.append("complex")

    text_a, text_b = identity_text(item), identity_text(candidate)
    text_ratio = _ratio(text_a, text_b)
    if text_ratio >= 0.78:
        score += 15
        reasons.append("text-strong")
    elif text_ratio >= 0.58:
        score += 10
        reasons.append("text")
    elif text_ratio >= 0.38:
        score += 5
        reasons.append("text-weak")

    price_a, price_b = item.get("price"), candidate.get("price")
    if price_a and price_b:
        delta = abs(int(price_a) - int(price_b)) / max(int(price_a), int(price_b))
        if delta <= 0.05:
            score += 5
            reasons.append("price")
        elif delta <= 0.10:
            score += 2
            reasons.append("price-near")

    metro_a, metro_b = normalize_text(item.get("metro")), normalize_text(candidate.get("metro"))
    if metro_a and metro_b and metro_a == metro_b:
        score += 3
        reasons.append("metro")

    score = max(0, min(100, score))
    strong_identity = bool(address_a and address_a == address_b
                           and _building_parts(address_a)[0] and _building_parts(address_a) == _building_parts(address_b)
                           and area_close and floor_same and text_ratio >= 0.78
                           and informative_text(text_a) and informative_text(text_b))
    if not strong_identity:
        score = min(score, AUTO_MATCH_SCORE - 1)
    return MatchResult(score, tuple(reasons), strong_identity)


def best_match(item, candidates):
    ranked = [(pid, row, listing_match(item, row)) for pid, row in candidates]
    ranked.sort(key=lambda result: (-result[2].score, result[0]))
    if not ranked:
        return None
    best = ranked[0]
    if len(ranked) > 1 and ranked[1][2].possible and best[2].score - ranked[1][2].score <= AUTO_MATCH_MARGIN:
        best = (best[0], best[1], replace(best[2], score=min(best[2].score, AUTO_MATCH_SCORE - 1),
                                       reasons=best[2].reasons + ("ambiguous-candidates",), strong_identity=False))
    return best


def create_property(conn, item, now):
    cursor = conn.execute(
        """INSERT INTO properties(created_at,updated_at,canonical_address,canonical_area,canonical_floor,canonical_complex_name)
           VALUES (?,?,?,?,?,?)""",
        (now, now, item.get("address"), item.get("area"), item.get("floor"), item.get("complex_name")),
    )
    return cursor.lastrowid


def touch_property(conn, property_id, item, now):
    conn.execute(
        """UPDATE properties SET updated_at=?,
           canonical_address=COALESCE(NULLIF(?,''),canonical_address),
           canonical_area=COALESCE(?,canonical_area),
           canonical_floor=COALESCE(NULLIF(?,''),canonical_floor),
           canonical_complex_name=COALESCE(NULLIF(?,''),canonical_complex_name)
           WHERE id=?""",
        (now, item.get("address"), item.get("area"), item.get("floor"), item.get("complex_name"), property_id),
    )


def identity_revision(conn):
    return conn.execute('SELECT revision FROM property_state WHERE id=1').fetchone()[0]


def bump_identity(conn):
    conn.execute('UPDATE property_state SET revision=revision+1 WHERE id=1')


def root_property(conn, property_id):
    seen = set()
    while property_id is not None and property_id not in seen:
        seen.add(property_id)
        row = conn.execute('SELECT merged_into FROM properties WHERE id=?', (property_id,)).fetchone()
        if not row or row[0] is None:
            return property_id
        property_id = row[0]
    raise ValueError('Property alias cycle')


def record_price(conn, property_id, listing_id, observed_at, price):
    """Compare per-publication observations, not prices of alternating ads."""
    if price is None:
        return False
    price = int(price)
    previous = conn.execute('SELECT price FROM listing_price_state WHERE listing_id=?', (listing_id,)).fetchone()
    if previous and previous[0] == price:
        return False
    conn.execute('''INSERT INTO listing_price_state(listing_id,price,observed_at) VALUES (?,?,?)
                    ON CONFLICT(listing_id) DO UPDATE SET price=excluded.price,observed_at=excluded.observed_at''',
                 (listing_id, price, observed_at))
    last = conn.execute('SELECT price FROM price_history WHERE property_id=? ORDER BY observed_at DESC,id DESC LIMIT 1',
                        (property_id,)).fetchone()
    if not previous and last and last[0] == price:
        return False
    conn.execute('INSERT INTO price_history(property_id,listing_id,observed_at,price) VALUES (?,?,?,?)',
                 (property_id, listing_id, observed_at, price))
    return True


def distinct_properties(conn, left, right):
    return bool(conn.execute('''SELECT 1 FROM identity_decisions d
        JOIN listings a ON a.id=d.listing_a JOIN listings b ON b.id=d.listing_b
        WHERE d.decision='different' AND
          ((a.property_id=? AND b.property_id=?) OR (a.property_id=? AND b.property_id=?)) LIMIT 1''',
        (left, right, right, left)).fetchone())


def save_decision(conn, left, right, decision, now, user_id):
    left, right = sorted((str(left), str(right)))
    conn.execute('INSERT INTO identity_decisions(listing_a,listing_b,decision,decided_at,decided_by) VALUES (?,?,?,?,?)',
                 (left, right, decision, now, user_id))


def candidate_rows(conn, incoming_ids=()):
    incoming = {str(value) for value in incoming_ids}
    # No unbounded IN (...): large CSV imports must not hit SQLite's variable limit.
    rows = conn.execute('''SELECT l.* FROM properties p JOIN listings l ON l.id=p.current_listing_id
                           WHERE p.merged_into IS NULL ORDER BY p.id''').fetchall()
    return [(row['property_id'], dict(row)) for row in rows]


def timestamp_key(value):
    try:
        date = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return date.replace(tzinfo=date.tzinfo or timezone.utc).timestamp()
    except (ValueError, AttributeError):
        return float('-inf')


REVIEW_FIELDS = ('review_status','review_updated_at','review_updated_by','review_version',
                 'realtor_status','realtor_comment','realtor_updated_at','realtor_updated_by','realtor_version')


def sync_property_reviews(conn, property_id, *, merging=False):
    rows = conn.execute('SELECT * FROM listings WHERE property_id=? ORDER BY rowid', (property_id,)).fetchall()
    if not rows:
        return
    def newest(date, state, default):
        dated = [row for row in rows if row[date]]
        return max(dated, key=lambda row: (timestamp_key(row[date]), row['id'])) if dated else next(
            (row for row in rows if row[state] != default), rows[0])
    review = newest('review_updated_at', 'review_status', 'new')
    realtor = newest('realtor_updated_at', 'realtor_status', 'pending')
    family_version = max(row['review_version'] for row in rows) + int(merging)
    realtor_version = max(row['realtor_version'] for row in rows) + int(merging)
    conn.execute('''UPDATE listings SET review_status=?,review_updated_at=?,review_updated_by=?,review_version=?,
                    realtor_status=?,realtor_comment=?,realtor_updated_at=?,realtor_updated_by=?,realtor_version=?
                    WHERE property_id=?''',
                 (review['review_status'],review['review_updated_at'],review['review_updated_by'],family_version,
                  realtor['realtor_status'],realtor['realtor_comment'],realtor['realtor_updated_at'],
                  realtor['realtor_updated_by'],realtor_version,property_id))


def merge_properties(conn, source, target, now):
    if source == target:
        return
    if distinct_properties(conn, source, target):
        raise ValueError('Ранее эти объекты отмечены как разные квартиры')
    # Keep lossless review snapshots before an explicitly confirmed merge.
    for row in conn.execute('SELECT * FROM listings WHERE property_id IN (?,?)', (source, target)).fetchall():
        snapshot = json.dumps({name: row[name] for name in REVIEW_FIELDS}, ensure_ascii=False)
        conn.execute('''INSERT INTO property_merge_audit(source_property_id,target_property_id,listing_id,merged_at,review_snapshot)
                        VALUES (?,?,?,?,?)''', (source, target, row['id'], now, snapshot))
    conn.execute('UPDATE listings SET property_id=? WHERE property_id=?', (target, source))
    conn.execute('UPDATE price_history SET property_id=? WHERE property_id=?', (target, source))
    conn.execute('UPDATE properties SET merged_into=?,updated_at=? WHERE id=?', (target, now, source))
    sync_property_reviews(conn, target, merging=True)
    row = conn.execute('SELECT * FROM listings WHERE property_id=? ORDER BY first_seen DESC,rowid DESC LIMIT 1',
                       (target,)).fetchone()
    conn.execute('UPDATE properties SET current_listing_id=? WHERE id=?', (row['id'], target))
    touch_property(conn, target, dict(row), now)
    bump_identity(conn)
