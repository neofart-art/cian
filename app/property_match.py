"""Deterministic local matching of Cian relistings to the same real property."""
from dataclasses import dataclass
from difflib import SequenceMatcher
import re

AUTO_MATCH_SCORE = 85
POSSIBLE_MATCH_SCORE = 65


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
    return re.sub(r"[^a-zа-я0-9]+", " ", text).strip()


def _building_parts(address):
    house = re.search(r"(?:^| )д ([0-9]+[a-zа-я]?)\b", address)
    corpus = re.search(r"(?:^| )корп ([0-9]+[a-zа-я]?)\b", address)
    structure = re.search(r"(?:^| )стр ([0-9]+[a-zа-я]?)\b", address)
    return (
        house.group(1) if house else None,
        corpus.group(1) if corpus else None,
        structure.group(1) if structure else None,
    )


def normalize_text(value):
    text = _plain(value)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"\b(?:телефон|звоните|агент|риелтор|собственник)\b", " ", text)
    return re.sub(r"[^a-zа-я0-9]+", " ", text).strip()


def _first_floor(value):
    match = re.search(r"\d{1,3}", str(value or ""))
    return int(match.group()) if match else None


def _ratio(a, b):
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a[:5000], b[:5000]).ratio()


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
            reasons.append("floor-different")

    complex_a = normalize_text(item.get("complex_name"))
    complex_b = normalize_text(candidate.get("complex_name"))
    if complex_a and complex_b and complex_a == complex_b:
        score += 10
        reasons.append("complex")

    text_a = normalize_text(item.get("description") or item.get("card_text"))
    text_b = normalize_text(candidate.get("description") or candidate.get("card_text"))
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
    strong_identity = bool(address_a and address_b and area_close and floor_same and text_ratio >= 0.58)
    return MatchResult(score, tuple(reasons), strong_identity)


def best_match(item, candidates):
    best = None
    for property_id, candidate in candidates:
        result = listing_match(item, candidate)
        if best is None or result.score > best[2].score:
            best = (property_id, candidate, result)
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


def record_price(conn, property_id, listing_id, observed_at, price):
    if price is None:
        return False
    last = conn.execute(
        "SELECT price FROM price_history WHERE property_id=? ORDER BY id DESC LIMIT 1", (property_id,)
    ).fetchone()
    if last and int(last["price"]) == int(price):
        return False
    conn.execute(
        "INSERT INTO price_history(property_id,listing_id,observed_at,price) VALUES (?,?,?,?)",
        (property_id, listing_id, observed_at, int(price)),
    )
    return True


def candidate_rows(conn, incoming_ids=()):
    """Latest listing per property, excluding properties represented in this input batch."""
    incoming = {str(value) for value in incoming_ids}
    excluded_properties = set()
    if incoming:
        placeholders = ",".join("?" for _ in incoming)
        excluded_properties = {
            row["property_id"] for row in conn.execute(
                f"SELECT DISTINCT property_id FROM listings WHERE id IN ({placeholders}) AND property_id IS NOT NULL",
                tuple(incoming),
            )
        }
    rows = conn.execute(
        "SELECT * FROM listings WHERE property_id IS NOT NULL ORDER BY COALESCE(last_seen,first_seen) DESC,id DESC"
    ).fetchall()
    seen = set()
    result = []
    for row in rows:
        property_id = row["property_id"]
        if property_id in seen or property_id in excluded_properties or str(row["id"]) in incoming:
            continue
        seen.add(property_id)
        result.append((property_id, dict(row)))
    return result


def sync_property_reviews(conn, property_id):
    """Keep user-owned review fields identical across every publication of a property."""
    rows = conn.execute(
        "SELECT * FROM listings WHERE property_id=? ORDER BY COALESCE(last_seen,first_seen) DESC,id DESC",
        (property_id,),
    ).fetchall()
    if not rows:
        return

    def newest(field, state_field, default_state):
        dated = [row for row in rows if row[field]]
        if dated:
            return max(dated, key=lambda row: row[field])
        meaningful = [row for row in rows if row[state_field] != default_state]
        return meaningful[0] if meaningful else rows[0]

    review = newest("review_updated_at", "review_status", "new")
    realtor = newest("realtor_updated_at", "realtor_status", "pending")
    conn.execute(
        """UPDATE listings SET
           review_status=?,review_updated_at=?,review_updated_by=?,review_version=?,
           realtor_status=?,realtor_comment=?,realtor_updated_at=?,realtor_updated_by=?,realtor_version=?
           WHERE property_id=?""",
        (
            review["review_status"], review["review_updated_at"], review["review_updated_by"], review["review_version"],
            realtor["realtor_status"], realtor["realtor_comment"], realtor["realtor_updated_at"],
            realtor["realtor_updated_by"], realtor["realtor_version"], property_id,
        ),
    )
