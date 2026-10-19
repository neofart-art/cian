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
