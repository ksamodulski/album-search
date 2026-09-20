"""Read prices off a page and make them comparable.

Shops write prices a dozen ways - "129,99 zl", "1 299,00 zl", "PLN 129.99",
"od 24.99 EUR", "$21.98" - and a comma may be a decimal point or a thousands
separator depending on the shop's locale. `parse_money` hides all of it.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from .domain import Money

_CURRENCY_SIGNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"z[lł]\b|\bPLN\b", re.IGNORECASE), "PLN"),
    (re.compile(r"€|\bEUR\b", re.IGNORECASE), "EUR"),
    (re.compile(r"£|\bGBP\b", re.IGNORECASE), "GBP"),
    (re.compile(r"\bUSD\b|(?<![A-Z])\$", re.IGNORECASE), "USD"),
    (re.compile(r"\bCZK\b|\bKč\b", re.IGNORECASE), "CZK"),
)

# A number with optional thousands groups and an optional 1-2 digit fraction.
_NUMBER = re.compile(r"\d{1,3}(?:[   .,]\d{3})*(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?")

# Prices below this are almost always shipping, a "per month" instalment, or
# a stray rating; above it, a phone number or a catalogue id.
MIN_PLAUSIBLE = Decimal("1")
MAX_PLAUSIBLE = Decimal("100000")


def parse_money(text: str, default_currency: str = "PLN") -> Money | None:
    """Extract the price from a snippet of shop text.

    Returns the *lowest* plausible number found, which is what a shop's
    "129,99 zl (was 159,99 zl)" block should resolve to. `None` when the text
    holds no plausible price.
    """
    if not text:
        return None
    currency = _detect_currency(text) or default_currency
    candidates: list[Decimal] = []
    for match in _NUMBER.finditer(text.replace(" ", " ")):
        value = _to_decimal(match.group(0))
        if value is not None and MIN_PLAUSIBLE <= value <= MAX_PLAUSIBLE:
            candidates.append(value)
    if not candidates:
        return None
    return Money(min(candidates), currency)


def looks_like_price(text: str) -> bool:
    """Cheap test used by calibration to spot price-bearing nodes."""
    return parse_money(text) is not None and bool(_detect_currency(text))


def convert(money: Money, target: str, rates: dict[str, Decimal]) -> Money:
    """Convert using a table of "how many `target` units one unit is worth".

    With target PLN, `{"EUR": 4.30}` reads as "one euro is 4.30 zloty".
    Unknown currencies are returned untouched so a missing rate degrades to
    "cannot compare" rather than a silently wrong number.
    """
    if money.currency == target:
        return money
    rate = rates.get(money.currency)
    if not rate:
        return money
    return Money((money.amount * rate).quantize(Decimal("0.01")), target)


def _detect_currency(text: str) -> str | None:
    for pattern, code in _CURRENCY_SIGNS:
        if pattern.search(text):
            return code
    return None


def _to_decimal(token: str) -> Decimal | None:
    """Resolve a locale-ambiguous number string into a Decimal."""
    cleaned = token.strip().replace(" ", "").replace(" ", "")
    last_sep = max(cleaned.rfind(","), cleaned.rfind("."))
    if last_sep == -1:
        normalized = cleaned
    else:
        fraction_len = len(cleaned) - last_sep - 1
        head = re.sub(r"[.,]", "", cleaned[:last_sep])
        tail = cleaned[last_sep + 1 :]
        # 1-2 trailing digits means a decimal part; exactly 3 means thousands.
        normalized = f"{head}.{tail}" if fraction_len <= 2 else head + tail
    try:
        return Decimal(normalized)
    except InvalidOperation:
        return None
