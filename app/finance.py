"""Derived annuity payment; deliberately not stored in SQLite."""
DOWN_PAYMENT = 6_000_000
ANNUAL_RATE = 0.18
MONTHS = 15 * 12


def mortgage_payment(price):
    if price is None:
        return None
    principal = max(price - DOWN_PAYMENT, 0)
    rate = ANNUAL_RATE / 12
    factor = (1 + rate) ** MONTHS
    return principal * rate * factor / (factor - 1)
